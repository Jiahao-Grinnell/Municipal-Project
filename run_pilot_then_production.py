"""Run the user-approved pilot, audit it, then start resumable production.

The audit gate fails closed; a failed or empty pilot never starts production.
Run from this project directory. Original source CSVs are only read/checksummed.
"""
import argparse
import csv
import json
import logging
import os
import sqlite3
import sys
import traceback
import uuid
from pathlib import Path
from unittest.mock import patch

import download_bcmm_hs6_annual as d


PILOT_GROUPS = [(2017, "01,02,08,19"), (2006, "01"), (2025, "01")]
PILOT_CHUNKS = [d.Chunk(year, state, flow) for year, states in PILOT_GROUPS
                for state in d.state_ids(states) for flow in ("1", "2")]


def save_report(path, report):
    report["updated_at"] = d.utc_now()
    part = path.with_suffix(".json.part")
    with part.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    d.replace_with_retry(part, path)


def invoke(url_file, output, year=None, states="01-32", flows="1,2", page_size=4000):
    args = ["--api-url-file", str(url_file), "--output-dir", str(output),
            "--page-size", str(page_size), "--states", states, "--flows", flows]
    if year is not None:
        args += ["--start-year", str(year), "--end-year", str(year)]
    else:
        args += ["--start-year", "2006", "--end-year", "2025"]
    status = d.main(args)
    if status:
        raise d.ValidationError(f"Downloader exited {status}; output={output}, year={year}, states={states}")


def audit(root, template, chunks):
    manifest = d.Manifest(root / "manifest.csv")
    results = []
    observed = {}
    database = root / "logs" / "acceptance_keys.sqlite"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS keys (key TEXT PRIMARY KEY)")
        for chunk in chunks:
            if not d.resume_ok(root, chunk, template, manifest):
                raise d.ValidationError(f"Missing completed chunk: {chunk}")
            record = manifest.completed(chunk.filename)
            if int(record["n_rows"]) == 0:
                raise d.ValidationError(f"Empty pilot chunk requires investigation: {chunk}")
            db.execute("DELETE FROM keys")
            n_rows = 0
            with (root / "raw" / chunk.filename).open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                schema = None
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise d.ValidationError(f"Malformed CSV row in {chunk.filename}")
                    if schema is None:
                        schema = d.schema_for(row, template.require_firms)
                    key = d.validate_row(row, schema, chunk, observed)
                    db.execute("INSERT INTO keys VALUES (?)", (key,))
                    n_rows += 1
            db.commit()
            if n_rows != int(record["n_rows"]):
                raise d.ValidationError(f"Audit row count mismatch: {chunk.filename}")
            results.append({k: record[k] for k in (
                "filename", "year", "ent_id", "flow_id", "flow_label", "n_rows", "n_pages",
                "sha256", "terminal_empty", "boundary_checks", "number_of_firms_present",
                "retry_count", "reconciliation_status")})
    db.close()
    database.unlink()
    return results


def compare_records(first, second, database):
    """Compare every field of every row, ignoring only CSV quoting and row order."""
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE IF NOT EXISTS records (record TEXT PRIMARY KEY, balance INTEGER)")
        db.execute("DELETE FROM records")
        headers = None
        counts = []
        for path, sign in ((first, 1), (second, -1)):
            count = 0
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if headers is None:
                    headers = set(reader.fieldnames)
                elif headers != set(reader.fieldnames):
                    raise d.ValidationError("Different schemas in page-size comparison.")
                for row in reader:
                    canonical = json.dumps(row, ensure_ascii=False, sort_keys=True)
                    db.execute("INSERT INTO records VALUES (?, ?) ON CONFLICT(record) DO UPDATE "
                               "SET balance=balance+excluded.balance", (canonical, sign))
                    count += 1
            counts.append(count)
            db.commit()
        differences = db.execute("SELECT COUNT(*) FROM records WHERE balance != 0").fetchone()[0]
    db.close()
    database.unlink()
    if differences:
        raise d.ValidationError(f"Page-size comparison differs in {differences} distinct records.")
    return {"rows_first": counts[0], "rows_second": counts[1], "different_records": differences,
            "all_fields_equal": True, "sha256_first": d.file_sha256(first),
            "sha256_second": d.file_sha256(second)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url-file", type=Path, default=Path("base_api_url.txt"))
    parser.add_argument("--source-csv", type=Path, action="append", required=True)
    parser.add_argument("--pilot-dir", type=Path, default=Path("bcmm_hs6_pilot"))
    parser.add_argument("--production-dir", type=Path, default=Path("bcmm_hs6_annual"))
    args = parser.parse_args()
    pilot = args.pilot_dir.resolve()
    alternate = pilot.parent / (pilot.name + "_alternate_pages")
    production = args.production_dir.resolve()
    if len({pilot, alternate, production}) != 3:
        parser.error("Pilot, alternate, and production directories must differ.")
    pilot.mkdir(parents=True, exist_ok=True)
    report_path = pilot / "execution_report.json"
    if report_path.exists():
        history = pilot / "execution_history"
        history.mkdir(exist_ok=True)
        (history / f"report_{uuid.uuid4().hex}.json").write_bytes(report_path.read_bytes())
    report = {"started_at": d.utc_now(), "status": "pilot_running",
              "production_directory": str(production), "source_csvs": [],
              "reconciliation_status": "not_performed"}
    for path in args.source_csv:
        report["source_csvs"].append({"path": str(path.resolve()), "sha256_before": d.file_sha256(path)})
    save_report(report_path, report)
    try:
        template = d.QueryTemplate(args.api_url_file.read_text(encoding="utf-8-sig").strip())
        for year, states in PILOT_GROUPS:
            invoke(args.api_url_file, pilot, year, states)
        report["pilot_chunks"] = audit(pilot, template, PILOT_CHUNKS)
        largest = max(report["pilot_chunks"], key=lambda item: int(item["n_rows"]))
        report["largest_chunk"] = largest["filename"]
        report["status"] = "alternate_page_size_check"
        save_report(report_path, report)
        invoke(args.api_url_file, alternate, int(largest["year"]), largest["ent_id"],
               largest["flow_id"], page_size=1000)
        chunk = d.Chunk(int(largest["year"]), largest["ent_id"], largest["flow_id"])
        audit(alternate, template, [chunk])
        report["page_size_comparison"] = compare_records(
            pilot / "raw" / chunk.filename, alternate / "raw" / chunk.filename,
            pilot / "logs" / "comparison.sqlite")
        # Any attempted network access during resume is a hard failure of this check.
        before = d.file_sha256(pilot / "manifest.csv")
        with patch.object(d.requests.Session, "get", side_effect=AssertionError("Resume attempted network access")):
            for year, states in PILOT_GROUPS:
                invoke(args.api_url_file, pilot, year, states)
        after = d.file_sha256(pilot / "manifest.csv")
        if before != after:
            raise d.ValidationError("Resume altered the pilot manifest.")
        report["resume_check"] = {"passed": True, "network_requests": 0, "manifest_unchanged": True}
        for source in report["source_csvs"]:
            source["sha256_after"] = d.file_sha256(Path(source["path"]))
            if source["sha256_before"] != source["sha256_after"]:
                raise d.ValidationError("An original source CSV changed during the pilot.")
        report["pilot_passed_at"] = d.utc_now()
        report["status"] = "production_running"
        save_report(report_path, report)
        invoke(args.api_url_file, production)
        manifest = d.Manifest(production / "manifest.csv")
        total_rows = 0
        for year in range(2006, 2026):
            for state in d.state_ids("01-32"):
                for flow in ("1", "2"):
                    chunk = d.Chunk(year, state, flow)
                    if not d.resume_ok(production, chunk, template, manifest):
                        raise d.ValidationError(f"Production chunk missing: {chunk}")
                    total_rows += int(manifest.completed(chunk.filename)["n_rows"])
        report.update(status="production_downloaded", production_chunks=1280,
                      production_rows=total_rows, finished_at=d.utc_now())
        save_report(report_path, report)
        return 0
    except BaseException as exc:
        report.update(status="stopped", failed_stage=report["status"],
                      error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        save_report(report_path, report)
        logging.exception("Pilot/production stopped; inspect execution_report.json and manifests.")
        return 130 if isinstance(exc, KeyboardInterrupt) else 2


if __name__ == "__main__":
    sys.exit(main())
