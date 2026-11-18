#!/usr/bin/env python3
"""Validate and combine all 1,280 annual BCMM HS2, HS4 or HS6 chunks into one CSV."""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import time
from pathlib import Path

import download_bcmm_hs6_annual as d

HERE = Path(__file__).resolve().parent
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


def expected_chunks(product_level: int = 6) -> list[d.Chunk]:
    return [d.Chunk(year, f"{state:02d}", str(flow), product_level=product_level)
            for year in range(2006, 2026)
            for state in range(1, 33)
            for flow in (1, 2)]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-level", type=int, choices=(2, 4, 6), default=6)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--api-url-file", type=Path, default=HERE / "base_api_url.txt")
    args = parser.parse_args(argv)
    root = args.output_dir or HERE / f"bcmm_hs{args.product_level}_annual"
    output = root / f"bcmm_hs{args.product_level}_annual_2006_2025_all.csv"
    report_path = root / "combined_manifest.json"
    columns = [name.replace("HS6", f"HS{args.product_level}") for name in EXPECTED_COLUMNS]
    started = time.monotonic()
    total_rows = 0
    try:
        # Acquire exclusive locks before reading the manifest or source files.
        with contextlib.ExitStack() as stack:
            stack.enter_context(d.output_lock(root))
            lock = root / ".combine.lock"
            with lock.open("x", encoding="utf-8") as handle:
                stack.callback(lock.unlink, missing_ok=True)
                json.dump({"pid": os.getpid(), "started": d.utc_now()}, handle)
            if unresolved_failures(root):
                raise d.ValidationError("failed_chunks.csv contains unresolved chunks; repair before combining.")
            manifest = d.Manifest(root / "manifest.csv")
            d.validate_output_scope(root, manifest, args.product_level, monthly=False)
            chunks = expected_chunks(args.product_level)
            expected_names = {chunk.filename for chunk in chunks}
            raw_files = {path.name for path in (root / "raw").glob("*.csv")}
            if raw_files != expected_names:
                raise d.ValidationError(f"Raw file set differs: missing={len(expected_names-raw_files)}, "
                                        f"unexpected={len(raw_files-expected_names)}")
            template = d.QueryTemplate(args.api_url_file.read_text(encoding="utf-8-sig").strip(),
                                       product_level=args.product_level)
            # Verify all sources and archived responses before starting the merge.
            for index, chunk in enumerate(chunks, 1):
                if not d.resume_ok(root, chunk, template, manifest):
                    raise d.ValidationError(f"Incomplete chunk: {chunk.filename}")
                record = manifest.completed(chunk.filename)
                if json.loads(record["columns"]) != columns:
                    raise d.ValidationError(f"Unexpected schema: {chunk.filename}")
                if index % 128 == 0 or index == len(chunks):
                    print(f"Audited {index}/{len(chunks)} chunks (CSV and response hashes, rows, pagination)",
                          flush=True)
            part = output.with_suffix(output.suffix + ".part")
            with part.open("w", encoding="utf-8", newline="") as out_handle:
                writer = csv.writer(out_handle, quoting=csv.QUOTE_ALL, lineterminator="\n")
                writer.writerow(columns)
                for index, chunk in enumerate(chunks, 1):
                    record = manifest.completed(chunk.filename)
                    if record is None or record["terminal_empty"] != "1" or record["boundary_checks"] != "1":
                        raise d.ValidationError(f"No validated completion evidence: {chunk.filename}")
                    path = root / "raw" / chunk.filename
                    if path.stat().st_size != int(record["file_size"]):
                        raise d.ValidationError(f"File-size mismatch: {chunk.filename}")
                    if d.file_sha256(path) != record["sha256"]:
                        raise d.ValidationError(f"SHA256 mismatch: {chunk.filename}")
                    expected_header = json.loads(record["columns"])
                    if expected_header != columns:
                        raise d.ValidationError(f"Unexpected schema in manifest: {chunk.filename}: {expected_header}")

                    rows = 0
                    with path.open(encoding="utf-8", newline="") as in_handle:
                        reader = csv.reader(in_handle)
                        header = next(reader, None)
                        if header != columns:
                            raise d.ValidationError(f"Unexpected CSV header: {chunk.filename}: {header}")
                        for row in reader:
                            if len(row) != len(columns):
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
                        print(f"Combined {index}/{len(chunks)} chunks; {total_rows:,} rows", flush=True)
                out_handle.flush()
                os.fsync(out_handle.fileno())

            d.replace_with_retry(part, output)
            report = {
                "status": "completed",
                "created_at": d.utc_now(),
                "filename": output.name,
                "years": "2006-2025",
                "states": "01-32",
                "flows": "1,2",
                "prod_level": args.product_level,
                "measures": ["Trade Value"],
                "columns": columns,
                "n_chunks": len(chunks),
                "n_rows": total_rows,
                "file_size": output.stat().st_size,
                "sha256": d.file_sha256(output),
                "source_manifest": "manifest.csv",
                "source_manifest_sha256": d.file_sha256(root / "manifest.csv"),
                "response_archives_verified": True,
                "unresolved_failures": 0,
                "reconciliation_status": "not_performed",
                "elapsed_seconds": round(time.monotonic() - started, 3),
            }
            atomic_json(report_path, report)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
    except (OSError, csv.Error, ValueError, d.ValidationError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
