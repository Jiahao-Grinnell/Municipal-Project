#!/usr/bin/env python3
"""Sequential, audited annual BCMM HS6 extraction. See README.md before production.

The URL is supplied by the user, never reconstructed from an assumed endpoint.
Trade Value only is authorized for this project; --require-number-of-firms opts
back into the original requirement. No missing cells or HS code mappings are made.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, quote_plus, urlsplit, urlunsplit

import requests

START_YEAR = 2006
END_YEAR = 2025
CUBE = "economy_foreign_trade_mun"
VERSION = "3.0"
LOG = logging.getLogger("bcmm")


class ValidationError(RuntimeError):
    """An extraction cannot be accepted without investigation."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def replace_with_retry(source: Path, target: Path, attempts: int = 12) -> None:
    """Atomic replacement can briefly conflict with Windows readers/indexers.

    Never delete the destination to get around a lock. Keep the old file intact
    and retry only Windows access/sharing/lock violations, with a bounded delay.
    """
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == attempts - 1:
                raise
            delay = min(2.0, 0.1 * 2 ** attempt)
            LOG.warning("Temporary Windows file lock replacing %s; retry %s/%s in %.1fs",
                        target, attempt + 1, attempts - 1, delay)
            time.sleep(delay)


@dataclass(frozen=True)
class Chunk:
    year: int
    state: str
    flow: str
    month: int | None = None

    @property
    def filename(self) -> str:
        if self.month is not None:
            return f"bcmm_hs6_monthly_y{self.year}_m{self.month:02d}_s{self.state}_f{self.flow}.csv"
        return f"bcmm_hs6_annual_y{self.year}_s{self.state}_f{self.flow}.csv"


class QueryTemplate:
    """Adapter for the flat LogicLayer query actually supplied by the user.

    Keep original query segments (including repeated keys, order and encoding).
    Unknown query grammars/filters fail closed rather than quietly restricting scope.
    Encoded Viz Builder cuts[] URLs are UI URLs, not this API's flat grammar.
    """

    def __init__(self, url: str, require_firms: bool = False):
        self.parts = urlsplit(url.strip())
        if self.parts.scheme not in ("https", "http") or not self.parts.netloc:
            raise ValidationError("Provide a full copied API URL.")
        if self.parts.fragment or self.parts.username or self.parts.password:
            raise ValidationError("URL fragments and embedded credentials are not supported.")
        self.segments = []
        for raw in self.parts.query.split("&"):
            pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
            if len(pairs) != 1:
                raise ValidationError("Malformed query segment.")
            key, value = pairs[0]
            self.segments.append((raw, key, value))
        allowed = {"cube", "drilldowns", "measures", "Year", "Date Year", "State",
                   "Flow", "Product Level", "limit", "locale"}
        unknown = {key for _, key, _ in self.segments} - allowed
        if unknown:
            raise ValidationError(f"Unreviewed query parameters {sorted(unknown)}. Copy the flat "
                                  "API URL, removing extra cuts, filters, top-N or monthly settings.")
        if self.one("cube") != CUBE:
            raise ValidationError(f"Expected cube={CUBE}.")
        years = [key for key in ("Year", "Date Year") if self.values(key)]
        if len(years) != 1:
            raise ValidationError("Expected exactly one annual cut name: Year or Date Year.")
        self.year_key = years[0]
        drills = self.tokens("drilldowns")
        expected = {"Municipality", "HS6", "Flow", "State", "Product Level", "Country"}
        if set(drills) not in (expected | {"Year"}, expected | {"Date Year"}):
            raise ValidationError(f"Unexpected drilldowns {drills}; require Municipality, HS6, "
                                  "Flow, Year/Date Year, State, Product Level, Country only.")
        measures = self.tokens("measures")
        if "Trade Value" not in measures or set(measures) - {"Trade Value", "Number of Firms"}:
            raise ValidationError(f"Unexpected measures: {measures}.")
        if require_firms and "Number of Firms" not in measures:
            raise ValidationError("Number of Firms required but absent from copied URL.")
        self.require_firms = require_firms or "Number of Firms" in measures
        if self.one("Product Level") != "6":
            raise ValidationError("The copied URL must already have Product Level=6.")
        state = self.one("State")
        flow = self.one("Flow")
        year = self.one(self.year_key)
        if not re.fullmatch(r"[0-9]{1,2}", state) or not 1 <= int(state) <= 32:
            raise ValidationError("Template must have one state cut in 1..32.")
        if not re.fullmatch(r"[0-9]{4}", year) or not re.fullmatch(r"[A-Za-z0-9_-]+", flow):
            raise ValidationError("Template must have one annual year and one flow ID.")
        self.padded_state = state.startswith("0")
        self.reference = Chunk(int(year), state.zfill(2), flow)
        # Conflicting repeated cuts/limits cannot be interpreted safely.
        if self.values("limit"):
            self.one("limit")

    def values(self, key: str) -> list[str]:
        return [value for _, name, value in self.segments if name == key]

    def one(self, key: str) -> str:
        values = self.values(key)
        if not values or len(set(values)) != 1:
            raise ValidationError(f"Missing or conflicting repeated parameter {key!r}: {values}")
        return values[0]

    def tokens(self, key: str) -> list[str]:
        return [token.strip() for value in self.values(key) for token in value.split(",")]

    def url(self, chunk: Chunk, limit: str) -> str:
        changes = {self.year_key: str(chunk.year), "State": chunk.state if self.padded_state
                   else str(int(chunk.state)), "Flow": chunk.flow, "Product Level": "6",
                   "limit": limit}
        segments = []
        seen = set()
        for raw, key, _ in self.segments:
            if key in changes:
                segments.append(raw.split("=", 1)[0] + "=" + quote_plus(changes[key]))
                seen.add(key)
            else:
                segments.append(raw)
        if "limit" not in seen:
            segments.append("limit=" + quote_plus(limit))
        return urlunsplit(self.parts._replace(query="&".join(segments)))

    def page_url(self, chunk: Chunk, size: int, offset: int, order: str) -> str:
        limit = f"{offset},{size}" if order == "offset-rows" else f"{size},{offset}"
        return self.url(chunk, limit)

    def fingerprint(self, chunk: Chunk) -> str:
        return digest([VERSION, self.url(chunk, "1"), self.require_firms])


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(f"Duplicate JSON object key {key!r}.")
        result[key] = value
    return result


def extract_rows(text: str) -> list[dict]:
    def bad_constant(value):
        raise ValidationError(f"Non-finite JSON constant {value}")
    try:
        # All JSON number tokens remain exact strings: no float rounding or lost digits.
        payload = json.loads(text, parse_int=str, parse_float=str,
                             parse_constant=bad_constant, object_pairs_hook=unique_object)
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Non-JSON/malformed response: {text[:300]!r}") from exc
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        if any(payload.get(k) for k in ("error", "errors")):
            raise ValidationError(f"API error: {str(payload)[:800]}")
        candidates = [payload[k] for k in ("data", "records", "results") if k in payload]
        if len(candidates) != 1 or not isinstance(candidates[0], list):
            raise ValidationError(f"Unexpected response wrapper; top-level keys={list(payload)}")
        rows = candidates[0]
        if "source" in payload:
            source = payload["source"]
            if not isinstance(source, list) or not source or any(
                not isinstance(item, dict) or item.get("name") != CUBE for item in source
            ):
                raise ValidationError(f"Unexpected response source metadata: {source!r}")
    else:
        raise ValidationError(f"Unexpected response root type: {type(payload).__name__}")
    if any(not isinstance(row, dict) or not row for row in rows):
        raise ValidationError("Response records must be nonempty JSON objects.")
    return rows


ALIASES = {
    "municipality_id": ("Municipality ID",), "municipality": ("Municipality",),
    "hs6_id": ("HS6 ID", "HS6 Code"), "hs6": ("HS6",),
    "flow_id": ("Flow ID",), "flow": ("Flow",), "year": ("Date Year", "Year"),
    "state_id": ("State ID",), "state": ("State",), "level": ("Product Level",),
    "country_id": ("Country / Region ID", "Country ID", "Country/Region ID", "Country Region ID"),
    "country": ("Country / Region", "Country", "Country/Region", "Country Region"),
    "value": ("Trade Value",), "firms": ("Number of Firms",),
}


def name_key(value: str) -> str:
    return re.sub(r"[\s_]+", " ", value).strip().casefold()


def schema_for(row: dict, require_firms: bool) -> dict[str, str]:
    schema = {}
    for role, aliases in ALIASES.items():
        matches = [key for key in row if name_key(key) in {name_key(a) for a in aliases}]
        if not matches and role == "firms" and not require_firms:
            continue
        if len(matches) != 1:
            raise ValidationError(f"Missing/ambiguous {role} field; columns={list(row)}")
        schema[role] = matches[0]
    return schema


def observe_flow(mapping: dict[str, str], flow: str, label: str) -> None:
    if flow in mapping and mapping[flow] != label:
        raise ValidationError(f"Inconsistent Flow ID {flow}: {mapping[flow]!r} versus {label!r}")
    if flow not in mapping:
        LOG.info("Observed Flow ID %s -> %s", flow, label)
    mapping[flow] = label


def validate_row(row: dict, schema: dict, chunk: Chunk, flows: dict) -> str:
    if chunk.month is not None and row.get("Month") != f"{chunk.year:04d}-{chunk.month:02d}":
        raise ValidationError(f"Wrong Month: {row.get('Month')!r} for {chunk.filename}")
    for key, value in row.items():
        if isinstance(value, (dict, list, bool)):
            raise ValidationError(f"Unexpected non-scalar/boolean value in {key}: {value!r}")
    def identifier(role):
        value = row[schema[role]]
        if not isinstance(value, str) or not value.strip():
            raise ValidationError(f"Missing or non-string {role}: {value!r}")
        return value
    for role in ("municipality", "hs6", "country", "state"):
        identifier(role)
    mun, state = identifier("municipality_id"), identifier("state_id")
    if not re.fullmatch(r"[0-9]{1,5}", mun) or not re.fullmatch(r"[0-9]{1,2}", state):
        raise ValidationError(f"Unexpected INEGI identifiers: municipality={mun!r}, state={state!r}")
    if state.zfill(2) != chunk.state or mun.zfill(5)[:2] != chunk.state:
        raise ValidationError(f"Geography outside requested state {chunk.state}: {mun}, {state}")
    for role, expected in (("year", str(chunk.year)), ("level", "6"), ("flow_id", chunk.flow)):
        if identifier(role) != expected:
            raise ValidationError(f"Wrong {role}: {row[schema[role]]!r}; expected {expected!r}")
    observe_flow(flows, chunk.flow, identifier("flow"))
    # API HS6 IDs in the real sample are 7/8 digits. Never truncate them to six!
    return json.dumps([str(chunk.year), state.zfill(2), mun.zfill(5), identifier("hs6_id"),
                       identifier("country_id"), chunk.flow] +
                      ([row["Month"]] if chunk.month is not None else []), ensure_ascii=False)


class Client:
    def __init__(self, session, pause=0.5, timeout=90.0, max_retries=6,
                 connect_timeout=20.0, retry_base=1.0, retry_cap=60.0):
        self.session, self.pause, self.timeout = session, pause, timeout
        self.max_retries = max_retries
        self.connect_timeout = connect_timeout
        self.retry_base, self.retry_cap = retry_base, retry_cap
        self.archive = None
        self.retries = 0
        self.errors = 0
        self.last_request_end = 0.0
        self.transform_rows = None

    def get(self, url: str, purpose="page") -> list[dict]:
        for attempt in range(self.max_retries + 1):
            wait = self.pause - (time.monotonic() - self.last_request_end)
            if wait > 0:
                time.sleep(wait)
            event = {"time": utc_now(), "url": url, "purpose": purpose, "attempt": attempt}
            response = None
            retryable = False
            try:
                response = self.session.get(url, timeout=(self.connect_timeout, self.timeout))
                response.encoding = "utf-8"
                event.update(status=response.status_code, final_url=response.url, body=response.text)
                if response.status_code != 200:
                    retryable = response.status_code in (429, 500, 502, 503, 504)
                    raise ValidationError(f"HTTP {response.status_code}: {response.text[:300]!r}")
                rows = extract_rows(response.text)
                return self.transform_rows(rows) if self.transform_rows else rows
            except (requests.RequestException, ValidationError) as exc:
                retryable = retryable or isinstance(exc, (requests.ConnectionError,
                                                         requests.Timeout,
                                                         requests.exceptions.ChunkedEncodingError))
                self.errors += 1
                event["error"] = str(exc)
                if not retryable or attempt == self.max_retries:
                    raise
                delay = min(self.retry_cap, self.retry_base * 2.0 ** attempt)
                if response is not None and response.headers.get("Retry-After"):
                    retry_after = response.headers["Retry-After"]
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        try:
                            delay = max(delay, (parsedate_to_datetime(retry_after) -
                                               datetime.now(timezone.utc)).total_seconds())
                        except (ValueError, TypeError):
                            pass
                self.retries += 1
                LOG.warning("Request failed (%s); retry %s/%s in %.1fs", exc, attempt + 1,
                            self.max_retries, delay)
            finally:
                self.last_request_end = time.monotonic()
                if self.archive is not None:
                    self.archive.write(json.dumps(event, ensure_ascii=False) + "\n")
                    self.archive.flush()
                if response is not None:
                    response.close()
            time.sleep(delay)
        raise AssertionError("Unreachable")


def verify_pagination(client: Client, template: QueryTemplate, flows: dict) -> tuple[str, list[str]]:
    """Verify order and offset movement using the known nonempty template slice."""
    chunk = template.reference
    a = client.get(template.url(chunk, "0,2"), "preflight_offset_rows")
    b = client.get(template.url(chunk, "2,0"), "preflight_rows_offset")
    if bool(a) == bool(b):
        raise ValidationError("Cannot establish pagination order: both probes empty or both nonempty. "
                              "Use a known nonempty base slice; investigate ignored limits.")
    order, first = ("offset-rows", a) if a else ("rows-offset", b)
    if len(first) > 2:
        raise ValidationError("API ignored the two-row probe limit.")
    schema = schema_for(first[0], template.require_firms)
    for row in first:
        if set(row) != set(first[0]):
            raise ValidationError("Schema drift during preflight.")
        validate_row(row, schema, chunk, flows)
    second = client.get(template.page_url(chunk, 1, 1, order), "preflight_offset_check")
    if len(first) == 2 and second != first[1:2]:
        raise ValidationError("Offset probe disagrees with second row: unstable data/order or ignored offset.")
    if len(first) == 1 and second:
        # A hidden one-row cap is valid; verify offset 0 with the same one-row size.
        if len(second) != 1 or second == first:
            raise ValidationError("Offset did not advance during one-row-cap probe.")
        validate_row(second[0], schema, chunk, flows)
    LOG.info("Verified limit order: %s; columns: %s", order, list(first[0]))
    if "firms" not in schema:
        LOG.warning("Number of Firms absent: Trade Value-only mode explicitly authorized by user.")
    return order, list(first[0])


MANIFEST_FIELDS = [
    "attempt_id", "filename", "prod_level", "year", "ent_id", "flow_id", "flow_label",
    "n_rows", "n_pages", "completed", "status", "query_url", "query_fingerprint",
    "date_pulled", "page_size_requested", "limit_order", "retry_count", "http_api_errors",
    "file_size", "sha256", "columns", "number_of_firms_present", "terminal_empty",
    "terminal_offset", "first_page_sha256", "last_page_sha256", "boundary_checks",
    "response_archive", "response_sha256", "reconciliation_status", "replaces_sha256", "notes",
]


class Manifest:
    def __init__(self, path: Path):
        self.path = path
        self.rows = []
        if path.exists():
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != MANIFEST_FIELDS:
                    raise ValidationError("Incompatible manifest schema; use a new output directory.")
                self.rows = list(reader)
                if any(None in row or any(v is None for v in row.values()) for row in self.rows):
                    raise ValidationError("Malformed manifest row.")

    def append(self, row: dict) -> None:
        new_rows = self.rows + [{k: str(row.get(k, "")) for k in MANIFEST_FIELDS}]
        part = self.path.with_suffix(".csv.part")
        with part.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
            writer.writeheader()
            writer.writerows(new_rows)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(part, self.path)
        self.rows = new_rows

    def completed(self, filename: str):
        return next((r for r in reversed(self.rows)
                     if r["filename"] == filename and r["completed"] == "1"), None)


def resume_ok(root: Path, chunk: Chunk, template: QueryTemplate, manifest: Manifest) -> bool:
    path = root / "raw" / chunk.filename
    record = manifest.completed(chunk.filename)
    if not path.exists() and record is None:
        return False
    if record is None or not path.exists():
        raise ValidationError(f"Orphan/missing raw file for {chunk.filename}; inspect and use --force.")
    if record["terminal_empty"] != "1" or record["boundary_checks"] != "1":
        raise ValidationError(f"Missing pagination completion evidence for {chunk.filename}.")
    if record["query_fingerprint"] != template.fingerprint(chunk):
        raise ValidationError(f"Query changed for {chunk.filename}; use a new directory or --force.")
    if file_sha256(path) != record["sha256"] or str(path.stat().st_size) != record["file_size"]:
        raise ValidationError(f"Checksum/size mismatch for {path}; inspect and use --force.")
    archive = root / record["response_archive"]
    if not archive.is_file() or file_sha256(archive) != record["response_sha256"]:
        raise ValidationError(f"Missing/changed response archive for {chunk.filename}.")
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        if next(reader, None) != json.loads(record["columns"]):
            raise ValidationError("Completed CSV header changed.")
        if sum(1 for _ in reader) != int(record["n_rows"]):
            raise ValidationError("Completed CSV row count changed.")
    LOG.info("Resume verified: %s (%s rows)", chunk.filename, record["n_rows"])
    return True


def download_chunk(client: Client, template: QueryTemplate, chunk: Chunk, root: Path,
                   manifest: Manifest, order: str, reference_columns: list[str], flows: dict,
                   page_size: int) -> dict:
    attempt = uuid.uuid4().hex
    raw = root / "raw"
    raw.mkdir(exist_ok=True)
    archive_dir = root / "responses"
    archive_dir.mkdir(exist_ok=True)
    final = raw / chunk.filename
    part = raw / (chunk.filename + f".{attempt}.part")
    archive = archive_dir / (chunk.filename + f".{attempt}.jsonl.gz")
    db_path = root / "logs" / (attempt + ".sqlite.part")
    first_url = template.page_url(chunk, page_size, 0, order)
    row = dict(attempt_id=attempt, filename=chunk.filename, prod_level=6, year=chunk.year,
               ent_id=chunk.state, flow_id=chunk.flow, n_rows=0, n_pages=0, completed=0,
               status="started", query_url=first_url, query_fingerprint=template.fingerprint(chunk),
               date_pulled=utc_now(), page_size_requested=page_size, limit_order=order,
               terminal_empty=0, boundary_checks=0, reconciliation_status="not_performed",
               response_archive=archive.relative_to(root).as_posix(),
               replaces_sha256=file_sha256(final) if final.exists() else "")
    manifest.append(row)
    before_retries, before_errors = client.retries, client.errors
    offset = 0
    columns = reference_columns
    schema = None
    first_page = last_record = None
    db = None
    try:
        db = sqlite3.connect(db_path)
        db.execute("CREATE TABLE keys (key TEXT PRIMARY KEY, page INTEGER, row_number INTEGER)")
        db.execute("CREATE TABLE pages (hash TEXT PRIMARY KEY)")
        with gzip.open(archive, "wt", encoding="utf-8") as evidence, \
                part.open("w", encoding="utf-8", newline="") as handle:
            client.archive = evidence
            writer = None
            while True:
                url = template.page_url(chunk, page_size, offset, order)
                records = client.get(url)
                if not records:
                    row.update(terminal_empty=1, terminal_offset=offset)
                    break
                if len(records) > page_size:
                    raise ValidationError("Server returned more rows than requested; limit is unreliable.")
                if schema is None:
                    columns = list(records[0])
                    schema = schema_for(records[0], template.require_firms)
                    row["columns"] = json.dumps(columns, ensure_ascii=False)
                    row["number_of_firms_present"] = int("firms" in schema)
                    LOG.info("%s columns=%s", chunk.filename, columns)
                    writer = csv.DictWriter(handle, fieldnames=columns, quoting=csv.QUOTE_ALL)
                    writer.writeheader()
                    first_page = records
                    row["first_page_sha256"] = digest(records)
                # Order-independent full-page fingerprint, plus exact key uniqueness on disk.
                signature = digest(sorted(digest(record) for record in records))
                try:
                    db.execute("INSERT INTO pages VALUES (?)", (signature,))
                except sqlite3.IntegrityError as exc:
                    raise ValidationError(f"Repeated page at offset {offset}; pagination may be ignored.") from exc
                for index, record in enumerate(records):
                    if set(record) != set(columns):
                        raise ValidationError(f"Schema drift at offset {offset}: {list(record)}")
                    key = validate_row(record, schema, chunk, flows)
                    try:
                        db.execute("INSERT INTO keys VALUES (?, ?, ?)", (key, row["n_pages"], index))
                    except sqlite3.IntegrityError as exc:
                        prior = db.execute("SELECT page, row_number FROM keys WHERE key=?", (key,)).fetchone()
                        raise ValidationError(f"Duplicate logical key {key}; previous page/row={prior}, "
                                              f"current offset/row={offset}/{index}. No rows dropped.") from exc
                    writer.writerow(record)
                db.commit()
                handle.flush()
                offset += len(records)  # A short page never signals completion.
                row.update(n_rows=offset, n_pages=row["n_pages"] + 1,
                           last_page_sha256=digest(records), flow_label=flows[chunk.flow])
                last_record = records[-1]
                LOG.info("%s page=%s rows=%s total=%s", chunk.filename, row["n_pages"], len(records), offset)
            if first_page is not None:
                # Replay both boundaries. This is a diagnostic, not a snapshot guarantee.
                end = client.get(template.page_url(chunk, 2, offset - 1, order), "terminal_boundary")
                if end != [last_record]:
                    raise ValidationError("Terminal boundary disagrees: premature empty page or changing data/order.")
                replay = client.get(first_url, "first_page_replay")
                if replay != first_page:
                    raise ValidationError("First page changed during extraction; retry this chunk later.")
            else:
                # A schema cannot be inferred from []. Record the verified reference header
                # and leave firms presence unverified for this empty chunk.
                if client.get(template.url(chunk, "1"), "empty_chunk_confirmation"):
                    raise ValidationError("Empty chunk contradicts a single-row request.")
                writer = csv.DictWriter(handle, fieldnames=columns, quoting=csv.QUOTE_ALL)
                writer.writeheader()
                row.update(columns=json.dumps(columns, ensure_ascii=False),
                           number_of_firms_present="unverified_empty")
            row["boundary_checks"] = 1
            handle.flush()
            os.fsync(handle.fileno())
        client.archive = None
        row.update(file_size=part.stat().st_size, sha256=file_sha256(part),
                   response_sha256=file_sha256(archive), retry_count=client.retries - before_retries,
                   http_api_errors=client.errors - before_errors, date_pulled=utc_now())
        # Previous valid CSV survives every network/validation failure, even with --force.
        # A crash between replacement and manifest update becomes an orphan/hash mismatch,
        # never a silently accepted completed file.
        replace_with_retry(part, final)
        row.update(completed=1, status="downloaded" if offset else "downloaded_empty",
                   notes="Empty terminal page and boundary checks passed; controls/units/coverage "
                         "not reconciled. Raw IDs preserved; no standard HS6 mapping derived.")
        manifest.append(row)
        return row
    except BaseException as exc:
        client.archive = None
        row.update(completed=0, status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                   retry_count=client.retries - before_retries,
                   http_api_errors=client.errors - before_errors, notes=f"{type(exc).__name__}: {exc}",
                   date_pulled=utc_now())
        if archive.exists():
            row["response_sha256"] = file_sha256(archive)
        manifest.append(row)
        raise
    finally:
        client.archive = None
        if db is not None:
            db.close()
        if row["completed"] == 1:
            db_path.unlink(missing_ok=True)


@contextmanager
def output_lock(root: Path):
    lock = root / ".download.lock"
    try:
        handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise ValidationError(f"Output locked: {lock}. If its process has stopped, remove only this "
                              "lock file manually and restart.") from exc
    try:
        with handle:
            handle.write(json.dumps({"pid": os.getpid(), "started": utc_now()}))
        yield
    finally:
        lock.unlink(missing_ok=True)


def state_ids(spec: str) -> list[str]:
    result = set()
    for item in spec.split(","):
        limits = item.strip().split("-")
        if len(limits) not in (1, 2) or any(not x.isdigit() for x in limits):
            raise ValueError("States must be numbers/ranges, e.g. 01-32 or 01,09,19.")
        lo, hi = int(limits[0]), int(limits[-1])
        if not 1 <= lo <= hi <= 32:
            raise ValueError("State IDs must be in 01..32.")
        result.update(f"{value:02d}" for value in range(lo, hi + 1))
    return sorted(result)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--api-url", help="Actual API URL copied from live Viz Builder")
    source.add_argument("--api-url-file", type=Path, help="Text file containing the copied API URL")
    parser.add_argument("--start-year", type=int, default=START_YEAR)
    parser.add_argument("--end-year", type=int, default=END_YEAR)
    parser.add_argument("--states", default="01-32")
    parser.add_argument("--flows", default="1,2", help="Raw flow IDs, without assumed meanings")
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--output-dir", type=Path, default=Path("bcmm_hs6_annual"))
    parser.add_argument("--max-retries", type=int, default=6)
    parser.add_argument("--pause", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-live", action="store_true",
                        help="Download only the year/state/flow embedded in the supplied URL")
    parser.add_argument("--require-number-of-firms", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not 1900 <= args.start_year <= args.end_year <= 9999:
        parser.error("Invalid year range.")
    if args.page_size < 1 or args.max_retries < 0 or args.pause < 0 or args.timeout <= 0:
        parser.error("Invalid page size, retries, pause or timeout.")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        url = args.api_url or args.api_url_file.read_text(encoding="utf-8-sig").strip()
        template = QueryTemplate(url, args.require_number_of_firms)
        flows_selected = list(dict.fromkeys(x.strip() for x in args.flows.split(",")))
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", x) for x in flows_selected):
            raise ValidationError("Invalid flow IDs.")
        chunks = ([template.reference] if args.test_live else
                  [Chunk(year, state, flow) for year in range(args.start_year, args.end_year + 1)
                   for state in state_ids(args.states) for flow in flows_selected])
        LOG.info("Scope: %s annual HS6 state/year/flow chunks; default years are a requested scope, "
                 "not a live coverage certification.", len(chunks))
        if args.dry_run:
            print("No requests/writes. Pagination order will be verified live before download.")
            for chunk in chunks[:6]:
                print(chunk)
                print("offset,rows candidate: " + template.page_url(chunk, args.page_size, 0, "offset-rows"))
                print("rows,offset candidate: " + template.page_url(chunk, args.page_size, 0, "rows-offset"))
            return 0
        root = args.output_dir.resolve()
        root.mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(exist_ok=True)
        with output_lock(root):
            handler = logging.FileHandler(root / "logs" / f"run_{uuid.uuid4().hex}.log", encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            LOG.addHandler(handler)
            try:
                manifest = Manifest(root / "manifest.csv")
                observed = {}
                for record in manifest.rows:
                    if record["completed"] == "1" and record["flow_label"]:
                        observe_flow(observed, record["flow_id"], record["flow_label"])
                pending = [c for c in chunks if args.force or not resume_ok(root, c, template, manifest)]
                if pending:
                    with requests.Session() as session:
                        session.headers.update({"User-Agent": f"BCMM-research-downloader/{VERSION}",
                                                "Accept": "application/json"})
                        client = Client(session, args.pause, args.timeout, args.max_retries)
                        preflight_path = root / "logs" / f"preflight_{uuid.uuid4().hex}.jsonl.gz"
                        with gzip.open(preflight_path, "wt", encoding="utf-8") as evidence:
                            client.archive = evidence
                            order, columns = verify_pagination(client, template, observed)
                        client.archive = None
                        for i, chunk in enumerate(pending, 1):
                            LOG.info("Chunk %s/%s: %s", i, len(pending), chunk)
                            result = download_chunk(client, template, chunk, root, manifest, order,
                                                    columns, observed, args.page_size)
                            LOG.info("Saved %s rows=%s pages=%s firms=%s terminal_empty=%s",
                                     result["filename"], result["n_rows"], result["n_pages"],
                                     result["number_of_firms_present"], result["terminal_empty"])
                LOG.info("Observed flow mappings: %s", observed)
                LOG.info("Requested chunks downloaded/resume-verified. Reconciliation, units and "
                         "calendar coverage remain separate checks; no suppression inference made.")
            except BaseException:
                LOG.exception("Run stopped; preserving completed chunks and failure evidence.")
                raise
            finally:
                LOG.removeHandler(handler)
                handler.close()
        return 0
    except KeyboardInterrupt:
        LOG.error("Interrupted; incomplete chunks will restart from offset zero.")
        return 130
    except (ValidationError, ValueError, OSError, requests.RequestException, sqlite3.Error):
        LOG.exception("Extraction stopped. Inspect logs and manifest; no failed chunk was skipped.")
        return 2


if __name__ == "__main__":
    sys.exit(main())
