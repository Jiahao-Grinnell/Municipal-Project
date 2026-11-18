#!/usr/bin/env python3
"""Write chunk_api_urls.csv: the exact DataMexico API request used for every chunk in a download folder."""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FOLDERS = [f"bcmm_hs{level}_{frequency}" for level in (2, 4, 6) for frequency in ("annual", "monthly")]
NAME = re.compile(r"bcmm_hs(\d)_(annual|monthly)_y(\d{4})(?:_m(\d{2}))?_s(\d{2})_f(\d+)\.csv$")
COLUMNS = ["filename", "product_level", "frequency", "year", "month", "year_month", "state_id", "flow_id", "flow",
           "status", "n_rows", "n_pages", "page_size", "limit_order", "date_pulled_or_failed", "api_url_first_page"]
FAILED_COLUMNS = ["filename", "product_level", "frequency", "year", "month", "year_month", "state_id", "state",
                  "flow_id", "flow", "error", "last_failed_at", "api_url_first_page"]


def read_csv(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def export(root: Path) -> dict:
    """Latest completed attempt per chunk from manifest.csv, plus unresolved failures from failed_chunks.csv."""
    completed, flows = {}, {}
    for row in read_csv(root / "manifest.csv"):
        if row.get("flow_label"):
            flows[row["flow_id"]] = row["flow_label"]
        if row["completed"] == "1":
            completed[row["filename"]] = row
    failed = {row["filename"]: row for row in read_csv(root / "failed_chunks.csv")}
    rows = []
    for filename in sorted(set(completed) | set(failed)):
        match = NAME.match(filename)
        if not match:
            raise ValueError(f"Unexpected chunk filename in {root.name}: {filename}")
        level, frequency, year, month, state, flow = match.groups()
        common = dict(filename=filename, product_level=level, frequency=frequency, year=year, month=month or "",
                      year_month=f"{year}-{month}" if month else "", state_id=state, flow_id=flow,
                      flow=flows.get(flow, ""))
        if filename in completed:
            record = completed[filename]
            rows.append(dict(common, status=record["status"], n_rows=record["n_rows"], n_pages=record["n_pages"],
                             page_size=record.get("page_size_requested", ""),
                             limit_order=record.get("limit_order", ""),
                             date_pulled_or_failed=record.get("date_pulled", ""),
                             api_url_first_page=record["query_url"]))
        else:
            record = failed[filename]
            rows.append(dict(common, status=f"failed ({record.get('error_type', '')})", n_rows="", n_pages="",
                             page_size="", limit_order="", date_pulled_or_failed=record.get("last_failed_at", ""),
                             api_url_first_page=record.get("query_url", "")))
    write_rows(root / "chunk_api_urls.csv", COLUMNS, rows)
    failed_rows = [row for row in rows if row["status"].startswith("failed")]
    failed_output = root / "failed_chunk_api_urls.csv"
    if failed_rows:
        states = state_names(root, completed, {row["state_id"] for row in failed_rows})
        write_rows(failed_output, FAILED_COLUMNS,
                   [dict(row, state=states.get(row["state_id"], ""), error=error_summary(failed[row["filename"]]),
                         last_failed_at=row["date_pulled_or_failed"]) for row in failed_rows])
    else:
        failed_output.unlink(missing_ok=True)  # No unresolved failures left: drop any stale list.
    return dict(folder=root.name, chunks=len(rows), completed=len(completed), failed=len(failed_rows))


def error_summary(ledger):
    """'HTTP 502' rather than the generic ValidationError wrapper; otherwise the exception type."""
    match = re.match(r"HTTP \d{3}", ledger.get("error", ""))
    return match.group(0) if match else ledger.get("error_type", "")


def state_names(root, completed, state_ids):
    """State names read from the first data row of a downloaded chunk of each state."""
    names = {}
    for filename, record in completed.items():
        state = NAME.match(filename).group(5)
        if state in state_ids and state not in names and record["n_rows"] != "0":
            with (root / "raw" / filename).open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                names[state] = next(reader)["State"]
    return names


def write_rows(output, columns, rows):
    part = output.with_suffix(".csv.part")
    with part.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, columns, quoting=csv.QUOTE_ALL, lineterminator="\n", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    part.replace(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folders", nargs="*", type=Path,
                        help="Download folders containing manifest.csv (default: all six bcmm_hs*_ folders)")
    args = parser.parse_args(argv)
    folders = args.folders or [HERE / name for name in FOLDERS if (HERE / name / "manifest.csv").exists()]
    for folder in folders:
        print(export(folder))
    return 0


if __name__ == "__main__":
    sys.exit(main())
