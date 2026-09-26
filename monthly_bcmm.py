"""Monthly query adapter; archives retain the unmodified source responses."""
import re
from urllib.parse import quote_plus, urlunsplit

import download_bcmm_hs6_annual as d


class MonthlyQueryTemplate(d.QueryTemplate):
    """Derive a monthly query from the validated annual URL configuration."""

    def __init__(self, url):
        super().__init__(url)
        self.year_column = next(x for x in self.tokens("drilldowns")
                                if x in ("Year", "Date Year"))
        self.reference = d.Chunk(self.reference.year, self.reference.state,
                                 self.reference.flow, 1)

    def url(self, chunk, limit):
        if chunk.month is None or not 1 <= chunk.month <= 12:
            raise d.ValidationError("Monthly queries require a month in 1..12.")
        changes = {"State": chunk.state if self.padded_state else str(int(chunk.state)),
                   "Flow": chunk.flow, "Product Level": "6", "limit": limit,
                   "drilldowns": ",".join("Month" if x in ("Year", "Date Year") else x
                                          for x in self.tokens("drilldowns"))}
        segments = []
        seen = set()
        for raw, key, value in self.segments:
            if key == self.year_key:
                continue
            if key in changes:
                segments.append(raw.split("=", 1)[0] + "=" + quote_plus(changes[key]))
                seen.add(key)
            else:
                segments.append(raw)
        segments.append(f"Month={chunk.year:04d}{chunk.month:02d}")
        if "limit" not in seen:
            segments.append("limit=" + quote_plus(limit))
        return urlunsplit(self.parts._replace(query="&".join(segments)))

    def fingerprint(self, chunk):
        return d.digest(["monthly-v1", super().fingerprint(chunk), self.year_column])

    def normalize_rows(self, rows):
        """Retain annual column names/order and append Month as YYYY-MM.

        Verify both source month fields before deriving Year. Original Month ID
        remains in response archives; no extra Month ID column enters the CSV.
        """
        output = []
        for row in rows:
            month_id, month = row.get("Month ID"), row.get("Month")
            if (not isinstance(month_id, str) or
                    not re.fullmatch(r"[0-9]{4}(0[1-9]|1[0-2])", month_id) or
                    month != month_id[:4] + "-" + month_id[4:]):
                raise d.ValidationError(f"Missing/inconsistent source month: {month_id!r}, {month!r}")
            if "Year" in row or "Date Year" in row:
                raise d.ValidationError("Unexpected annual field in monthly response.")
            result = {}
            for key, value in row.items():
                if key == "Month ID":
                    result[self.year_column] = month_id[:4]
                elif key != "Month":
                    result[key] = value
            result["Month"] = month
            output.append(result)
        return output


def month_ids(spec):
    months = [int(x) for x in d.state_ids(spec)]
    if any(x > 12 for x in months):
        raise ValueError("Months must be in 01..12.")
    return months
