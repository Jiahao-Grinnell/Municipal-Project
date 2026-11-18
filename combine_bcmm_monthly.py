#!/usr/bin/env python3
"""Audit monthly BCMM chunks and combine 2006-2025 into four five-year CSVs."""
from __future__ import annotations

import argparse
import collections
import contextlib
import csv
import json
import os
import shutil
import sys
import time
from pathlib import Path

import download_bcmm_hs6_annual as d
from combine_bcmm_chunks import EXPECTED_COLUMNS, atomic_json, unresolved_failures
from monthly_bcmm import MonthlyQueryTemplate

HERE = Path(__file__).resolve().parent
START_YEAR, END_YEAR = 2006, 2025


class CompletionIndex:
    """Read-only index of the latest completed records for the integrity checker."""
    def __init__(self, manifest):
        self.records = {row["filename"]: row for row in manifest.rows if row["completed"] == "1"}

    def completed(self, filename):
        return self.records.get(filename)


def expected_chunks(product_level):
    return [d.Chunk(year, f"{state:02d}", str(flow), month, product_level)
            for year in range(START_YEAR, END_YEAR + 1)
            for month in range(1, 13) for state in range(1, 33) for flow in (1, 2)]


def period_start(year):
    return START_YEAR + (year - START_YEAR) // 5 * 5


LEDGER_COLUMNS = ["error_type", "attempts_this_run", "last_failed_at", "error", "query_url"]
MISSING_COLUMNS = ["filename", "year", "month", "year_month", "state_id", "state", "flow_id", "flow",
                   "municipality_scope", "n_municipalities_observed_same_state_year", "period_file"] + LEDGER_COLUMNS
MISSING_MUNICIPALITY_COLUMNS = ["filename", "year", "month", "year_month", "state_id", "state", "flow_id", "flow",
                                "municipality_id", "municipality", "municipality_source"]


def observed_labels(root, chunks, columns):
    """State/flow names and municipalities seen in downloaded chunks, keyed by (state, year)."""
    position = {name: columns.index(name) for name in ("Municipality ID", "Municipality", "Flow ID", "Flow", "State")}
    states, flows, municipalities = {}, {}, collections.defaultdict(dict)
    for chunk in chunks:
        with (root / "raw" / chunk.filename).open(encoding="utf-8", newline="") as source:
            reader = csv.reader(source)
            next(reader, None)
            for row in reader:
                states[chunk.state] = row[position["State"]]
                flows[chunk.flow] = row[position["Flow"]]
                municipalities[chunk.state, chunk.year][row[position["Municipality ID"]]] = row[position["Municipality"]]
    return states, flows, municipalities


def stage_missing(root, destination, missing, present, failed, level, columns):
    """List excluded chunks (one row each) and the municipalities they cover (one row per municipality)."""
    wanted = {(chunk.state, chunk.year) for chunk in missing}
    states, flows, municipalities = observed_labels(
        root, [chunk for chunk in present if (chunk.state, chunk.year) in wanted], columns)
    names = ("missing_chunks.csv", "missing_chunks_municipalities.csv")
    parts = [destination / (name + ".part") for name in names]
    n_municipality_rows = 0
    with parts[0].open("w", encoding="utf-8", newline="") as chunk_output, \
            parts[1].open("w", encoding="utf-8", newline="") as municipality_output:
        chunk_writer = csv.DictWriter(chunk_output, MISSING_COLUMNS, quoting=csv.QUOTE_ALL, lineterminator="\n")
        municipality_writer = csv.DictWriter(municipality_output, MISSING_MUNICIPALITY_COLUMNS,
                                             quoting=csv.QUOTE_ALL, lineterminator="\n")
        chunk_writer.writeheader()
        municipality_writer.writeheader()
        for chunk in missing:
            first = period_start(chunk.year)
            ledger = failed[chunk.filename]
            known = municipalities.get((chunk.state, chunk.year), {})
            common = dict(filename=chunk.filename, year=chunk.year, month=f"{chunk.month:02d}",
                          year_month=f"{chunk.year:04d}-{chunk.month:02d}", state_id=chunk.state,
                          state=states.get(chunk.state, ""), flow_id=chunk.flow, flow=flows.get(chunk.flow, ""))
            chunk_writer.writerow(dict(common, municipality_scope="all municipalities in state (whole chunk missing)",
                                       n_municipalities_observed_same_state_year=len(known),
                                       period_file=f"bcmm_hs{level}_monthly_{first}_{min(first + 4, END_YEAR)}.csv",
                                       **{name: ledger.get(name, "") for name in LEDGER_COLUMNS}))
            for municipality_id in sorted(known, key=lambda value: value.zfill(5)):
                municipality_writer.writerow(dict(
                    common, municipality_id=municipality_id, municipality=known[municipality_id],
                    municipality_source=f"observed in other downloaded {chunk.year} chunks of state {chunk.state}"))
                n_municipality_rows += 1
        for handle in (chunk_output, municipality_output):
            handle.flush()
            os.fsync(handle.fileno())
    return [dict(filename=names[0], n_chunks=len(missing), file_size=parts[0].stat().st_size,
                 sha256=d.file_sha256(parts[0])),
            dict(filename=names[1], n_rows=n_municipality_rows, file_size=parts[1].stat().st_size,
                 sha256=d.file_sha256(parts[1]))]


def stage_period(root, destination, first_year, chunks, index, columns, year_column):
    last_year = min(first_year + 4, END_YEAR)
    level = chunks[0].product_level
    filename = f"bcmm_hs{level}_monthly_{first_year}_{last_year}.csv"
    part = destination / (filename + ".part")
    year_rows = collections.Counter()
    positions = [columns.index(name) for name in (year_column, "Month", "State ID", "Flow ID", "Product Level")]
    total = 0
    with part.open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writerow(columns)
        for number, chunk in enumerate(chunks, 1):
            record = index.completed(chunk.filename)
            path = root / "raw" / chunk.filename
            # Repeat the source hash immediately before copying, after the full pre-audit.
            if path.stat().st_size != int(record["file_size"]) or d.file_sha256(path) != record["sha256"]:
                raise d.ValidationError(f"Source changed after audit: {chunk.filename}")
            expected = (str(chunk.year), f"{chunk.year:04d}-{chunk.month:02d}", chunk.state, chunk.flow, str(level))
            count = 0
            with path.open(encoding="utf-8", newline="") as source:
                reader = csv.reader(source)
                if next(reader, None) != columns:
                    raise d.ValidationError(f"Unexpected CSV header: {chunk.filename}")
                for row in reader:
                    if len(row) != len(columns):
                        raise d.ValidationError(f"Malformed row {count + 2}: {chunk.filename}")
                    actual = tuple(row[p].zfill(2) if i == 2 else row[p] for i, p in enumerate(positions))
                    if actual != expected:
                        raise d.ValidationError(f"Row outside requested month/state/flow/level: {chunk.filename}")
                    writer.writerow(row)
                    count += 1
            if count != int(record["n_rows"]):
                raise d.ValidationError(f"Row-count mismatch: {chunk.filename}")
            total += count
            year_rows[str(chunk.year)] += count
            if number % 512 == 0 or number == len(chunks):
                print(f"Staged {first_year}-{last_year}: {number}/{len(chunks)} chunks, {total:,} rows", flush=True)
        output.flush()
        os.fsync(output.fileno())
    return dict(filename=filename, years=f"{first_year}-{last_year}", n_chunks=len(chunks),
                n_rows=total, rows_by_year=dict(sorted(year_rows.items())),
                file_size=part.stat().st_size, sha256=d.file_sha256(part))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-level", type=int, choices=(2, 4, 6), default=6)
    parser.add_argument("--output-dir", type=Path, help="Source monthly directory containing raw/ and manifest.csv")
    parser.add_argument("--api-url-file", type=Path, default=HERE / "base_api_url.txt")
    parser.add_argument("--allow-missing", action="store_true",
                        help="Combine despite chunks in failed_chunks.csv; they are excluded and listed in "
                             "combined_5year/missing_chunks.csv. Any other gap still stops the merge.")
    args = parser.parse_args(argv)
    root = (args.output_dir or HERE / f"bcmm_hs{args.product_level}_monthly").resolve()
    destination = root / "combined_5year"
    started = time.monotonic()
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(d.output_lock(root))
            lock = root / ".combine.lock"
            with lock.open("x", encoding="utf-8") as handle:
                stack.callback(lock.unlink, missing_ok=True)
                json.dump(dict(pid=os.getpid(), started=d.utc_now()), handle)
            failed = {row["filename"]: row for row in unresolved_failures(root)}
            if failed and not args.allow_missing:
                raise d.ValidationError("Unresolved failed chunks; repair failed_chunks.csv before combining, "
                                        "or pass --allow-missing to exclude and list them.")
            manifest_path = root / "manifest.csv"
            manifest_hash = d.file_sha256(manifest_path)
            manifest = d.Manifest(manifest_path)
            d.validate_output_scope(root, manifest, args.product_level, monthly=True)
            index = CompletionIndex(manifest)
            all_chunks = expected_chunks(args.product_level)
            if set(failed) - {chunk.filename for chunk in all_chunks}:
                raise d.ValidationError(f"Failure ledger lists chunks outside the expected scope: "
                                        f"{sorted(set(failed) - {chunk.filename for chunk in all_chunks})[:5]}")
            if set(failed) & set(index.records):
                raise d.ValidationError(f"Failure ledger lists completed chunks: "
                                        f"{sorted(set(failed) & set(index.records))[:5]}")
            # Only ledger-documented failures may be excluded; every other chunk must be complete.
            missing = [chunk for chunk in all_chunks if chunk.filename in failed]
            chunks = [chunk for chunk in all_chunks if chunk.filename not in failed]
            expected = {chunk.filename for chunk in chunks}
            raw = {path.name for path in (root / "raw").glob("*.csv")}
            if raw != expected or set(index.records) != expected:
                raise d.ValidationError(f"Chunk coverage differs: missing_raw={len(expected-raw)}, "
                                        f"unexpected_raw={len(raw-expected)}, "
                                        f"missing_completed={len(expected-set(index.records))}, "
                                        f"unexpected_completed={len(set(index.records)-expected)}")
            template = MonthlyQueryTemplate(args.api_url_file.read_text(encoding="utf-8-sig").strip(),
                                            product_level=args.product_level)
            columns = [name.replace("HS6", f"HS{args.product_level}") if name != "Year"
                       else template.year_column for name in EXPECTED_COLUMNS] + ["Month"]
            expected_rows = 0
            source_bytes = 0
            for number, chunk in enumerate(chunks, 1):
                if not d.resume_ok(root, chunk, template, index):
                    raise d.ValidationError(f"Incomplete chunk: {chunk.filename}")
                record = index.completed(chunk.filename)
                if json.loads(record["columns"]) != columns:
                    raise d.ValidationError(f"Unexpected manifest columns: {chunk.filename}")
                if (record["year"], record["ent_id"], record["flow_id"]) != (str(chunk.year), chunk.state, chunk.flow):
                    raise d.ValidationError(f"Manifest cuts disagree with filename: {chunk.filename}")
                expected_rows += int(record["n_rows"])
                source_bytes += int(record["file_size"])
                if number % 512 == 0 or number == len(chunks):
                    print(f"Audited {number}/{len(chunks)} monthly chunks (CSV/archive hashes, rows, pagination)", flush=True)
            if shutil.disk_usage(root).free < source_bytes * 2:
                raise d.ValidationError("Insufficient free space for staging the merged CSVs.")
            # All sources must pass before any output CSV is staged.
            destination.mkdir(exist_ok=True)
            groups = collections.defaultdict(list)
            for chunk in chunks:
                groups[period_start(chunk.year)].append(chunk)
            files = [stage_period(root, destination, first, group, index, columns, template.year_column)
                     for first, group in sorted(groups.items())]
            for info in files:
                info["n_missing_chunks"] = sum(period_start(chunk.year) == int(info["years"][:4]) for chunk in missing)
            missing_info = stage_missing(root, destination, missing, chunks, failed, args.product_level, columns)
            total = sum(info["n_rows"] for info in files)
            if total != expected_rows or d.file_sha256(manifest_path) != manifest_hash:
                raise d.ValidationError("Source manifest or total row count changed during merging.")
            report = dict(status="committing", created_at=d.utc_now(), frequency="monthly", prod_level=args.product_level,
                          grouping="five-year", years=f"{START_YEAR}-{END_YEAR}", states="01-32", flows="1,2",
                          measures=["Trade Value"], columns=columns, source_root=str(root),
                          source_manifest="../manifest.csv", source_manifest_sha256=manifest_hash,
                          response_archives_verified=True, unresolved_failures=len(missing),
                          coverage="complete" if not missing else "incomplete",
                          n_expected_chunks=len(all_chunks), n_chunks=len(chunks), n_missing_chunks=len(missing),
                          missing_lists=missing_info, n_rows=total,
                          n_empty_chunks=sum(int(r["n_rows"]) == 0 for r in index.records.values()),
                          reconciliation_status="not_performed", files=files)
            report_path = destination / "combined_manifest.json"
            # Stage every group before replacing final outputs. An interrupted commit is explicit in the report.
            atomic_json(report_path, report)
            for info in files + missing_info:
                final = destination / info["filename"]
                d.replace_with_retry(destination / (info["filename"] + ".part"), final)
            report.update(status="completed", elapsed_seconds=round(time.monotonic() - started, 3))
            atomic_json(report_path, report)
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            return 0
    except KeyboardInterrupt:
        print("ERROR: interrupted; source chunks retained; staged .part files are not completed outputs.", file=sys.stderr)
        return 130
    except (OSError, ValueError, csv.Error, d.ValidationError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
