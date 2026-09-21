#!/usr/bin/env python3
"""Validate and combine all 1,280 annual BCMM HS6 chunks into one CSV."""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from pathlib import Path

import download_bcmm_hs6_annual as d

HERE = Path(__file__).resolve().parent
ROOT = HERE / "bcmm_hs6_annual"
OUTPUT = ROOT / "bcmm_hs6_annual_2006_2025_all.csv"
REPORT = ROOT / "combined_manifest.json"
EXPECTED_COLUMNS = [
    "Municipality ID", "Municipality", "HS6 ID", "HS6", "Flow ID", "Flow",
    "Year", "State ID", "State", "Product Level", "Country ID", "Country", "Trade Value",
]


def atomic_json(path: Path, value: dict) -> None:
    part = path.with_suffix(path.suffix + ".part")
    with part.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    d.replace_with_retry(part, path)


def unresolved_failures(root: Path) -> list[dict]:
    path = root / "failed_chunks.csv"
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def expected_chunks() -> list[d.Chunk]:
    return [d.Chunk(year, f"{state:02d}", str(flow))
            for year in range(2006, 2026)
            for state in range(1, 33)
            for flow in (1, 2)]


def main() -> int:
    started = time.monotonic()
    if unresolved_failures(ROOT):
        print("ERROR: failed_chunks.csv contains unresolved chunks; refusing to combine.", file=sys.stderr)
        return 2
    if (ROOT / ".download.lock").exists():
        print("ERROR: downloader lock exists; do not combine while downloading.", file=sys.stderr)
        return 2

    manifest = d.Manifest(ROOT / "manifest.csv")
    chunks = expected_chunks()
    expected_names = {chunk.filename for chunk in chunks}
    raw_files = {path.name for path in (ROOT / "raw").glob("*.csv")}
    if raw_files != expected_names:
        print(f"ERROR: raw file set differs: missing={len(expected_names-raw_files)}, "
              f"unexpected={len(raw_files-expected_names)}", file=sys.stderr)
        return 2

    part = OUTPUT.with_suffix(OUTPUT.suffix + ".part")
    lock = ROOT / ".combine.lock"
    if lock.exists():
        print(f"ERROR: combine lock exists: {lock}", file=sys.stderr)
        return 2

    lock.write_text(json.dumps({"pid": os.getpid(), "started": d.utc_now()}), encoding="utf-8")
    total_rows = 0
    try:
        with part.open("w", encoding="utf-8", newline="") as out_handle:
            writer = csv.writer(out_handle, quoting=csv.QUOTE_ALL, lineterminator="\n")
            writer.writerow(EXPECTED_COLUMNS)
            for index, chunk in enumerate(chunks, 1):
                record = manifest.completed(chunk.filename)
                if record is None or record["terminal_empty"] != "1" or record["boundary_checks"] != "1":
                    raise d.ValidationError(f"No validated completion evidence: {chunk.filename}")
                path = ROOT / "raw" / chunk.filename
                if path.stat().st_size != int(record["file_size"]):
                    raise d.ValidationError(f"File-size mismatch: {chunk.filename}")
                if d.file_sha256(path) != record["sha256"]:
                    raise d.ValidationError(f"SHA256 mismatch: {chunk.filename}")
                expected_header = json.loads(record["columns"])
                if expected_header != EXPECTED_COLUMNS:
                    raise d.ValidationError(f"Unexpected schema in manifest: {chunk.filename}: {expected_header}")

                rows = 0
                with path.open(encoding="utf-8", newline="") as in_handle:
                    reader = csv.reader(in_handle)
                    header = next(reader, None)
                    if header != EXPECTED_COLUMNS:
                        raise d.ValidationError(f"Unexpected CSV header: {chunk.filename}: {header}")
                    for row in reader:
                        if len(row) != len(EXPECTED_COLUMNS):
                            raise d.ValidationError(
                                f"Malformed row {rows + 2} in {chunk.filename}: {len(row)} columns")
                        writer.writerow(row)
                        rows += 1
                if rows != int(record["n_rows"]):
                    raise d.ValidationError(
                        f"Row-count mismatch: {chunk.filename}: file={rows}, manifest={record['n_rows']}")
                total_rows += rows
                if index % 32 == 0 or index == len(chunks):
                    out_handle.flush()
                    print(f"Validated and combined {index}/{len(chunks)} chunks; {total_rows:,} rows", flush=True)
            out_handle.flush()
            os.fsync(out_handle.fileno())

        d.replace_with_retry(part, OUTPUT)
        report = {
            "status": "completed",
            "created_at": d.utc_now(),
            "filename": OUTPUT.name,
            "years": "2006-2025",
            "states": "01-32",
            "flows": "1,2",
            "prod_level": 6,
            "measures": ["Trade Value"],
            "columns": EXPECTED_COLUMNS,
            "n_chunks": len(chunks),
            "n_rows": total_rows,
            "file_size": OUTPUT.stat().st_size,
            "sha256": d.file_sha256(OUTPUT),
            "source_manifest": "manifest.csv",
            "unresolved_failures": 0,
            "reconciliation_status": "not_performed",
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        atomic_json(REPORT, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except (OSError, csv.Error, ValueError, d.ValidationError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        lock.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
