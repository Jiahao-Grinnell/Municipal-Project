#!/usr/bin/env python3
"""Run/resume annual BCMM extraction from Anaconda Prompt: python run_download.py.

Defaults use base_api_url.txt and bcmm_hs6_annual beside this file. No Codex
session is required. Each failed chunk gets up to 10 full attempts per launch;
exhausted chunks are recorded and the remaining queue continues.
"""
from __future__ import annotations

import argparse
import csv
import ctypes
import gzip
import json
import logging
import math
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import requests
import download_bcmm_hs6_annual as d

HERE = Path(__file__).resolve().parent
CHUNK_ATTEMPTS = 10
CONNECT_TIMEOUT = 60
READ_TIMEOUT = 300
PAGE_SIZE = 4000
REQUEST_PAUSE = 2
CHUNK_PAUSE = 10
RETRY_WAIT = 20
RETRY_WAIT_MAX = 120
FAILURE_FIELDS = ["filename", "year", "ent_id", "flow_id", "attempts_this_run", "run_id",
                  "last_failed_at", "error_type", "error", "query_url", "attempt_id",
                  "n_rows_before_failure", "n_pages_before_failure", "response_archive", "log_file"]
EVENT_FIELDS = ["time", "run_id", "filename", "attempt", "status", "attempt_id", "error"]
LOG = d.LOG


def atomic_json(path: Path, value: dict) -> None:
    part = path.with_suffix(path.suffix + ".part")
    with part.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    d.replace_with_retry(part, path)


class FailureLedger:
    """Current unresolved failures; manifest and attempt log retain full history."""
    def __init__(self, root: Path):
        self.path = root / "failed_chunks.csv"
        self.event_path = root / "chunk_attempts.csv"
        self.rows = {}
        if self.path.exists():
            with self.path.open(encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != FAILURE_FIELDS:
                    raise d.ValidationError("Unexpected failed_chunks.csv schema.")
                for row in reader:
                    if None in row or any(v is None for v in row.values()):
                        raise d.ValidationError("Malformed failure ledger.")
                    self.rows[row["filename"]] = row

    def save(self):
        part = self.path.with_suffix(".csv.part")
        with part.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FAILURE_FIELDS)
            writer.writeheader()
            writer.writerows(self.rows.values())
            handle.flush()
            os.fsync(handle.fileno())
        d.replace_with_retry(part, self.path)

    def clear(self, filename):
        if filename in self.rows:
            del self.rows[filename]
            self.save()

    def event(self, run_id, chunk, attempt, status, attempt_id="", error=""):
        exists = self.event_path.exists() and self.event_path.stat().st_size > 0
        with self.event_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=EVENT_FIELDS)
            if not exists:
                writer.writeheader()
            writer.writerow(dict(time=d.utc_now(), run_id=run_id, filename=chunk.filename,
                                 attempt=attempt, status=status, attempt_id=attempt_id, error=error))
            handle.flush()
            os.fsync(handle.fileno())

    def fail(self, chunk, attempt, run_id, exc, query_url, log_file, record=None):
        record = record or {}
        self.rows[chunk.filename] = dict(
            filename=chunk.filename, year=chunk.year, ent_id=chunk.state, flow_id=chunk.flow,
            attempts_this_run=attempt, run_id=run_id, last_failed_at=d.utc_now(),
            error_type=type(exc).__name__, error=str(exc), query_url=query_url,
            attempt_id=record.get("attempt_id", ""), n_rows_before_failure=record.get("n_rows", ""),
            n_pages_before_failure=record.get("n_pages", ""),
            response_archive=record.get("response_archive", ""), log_file=str(log_file))
        self.save()


def wait_before_retry(attempt, args):
    delay = min(args.retry_wait_max, args.retry_wait * 2 ** min(attempt - 1, 20))
    LOG.info("Waiting %.0f seconds before next complete-chunk attempt...", delay)
    time.sleep(delay)


def latest_attempt(manifest, filename, previous_length):
    return next((r for r in reversed(manifest.rows[previous_length:]) if r["filename"] == filename), {})


def run_chunk(client, template, chunk, root, manifest, order, columns, flows,
              args, ledger, run_id, log_file):
    """Retry the entire chunk, never append to a failed attempt's partial CSV."""
    for attempt in range(1, args.chunk_attempts + 1):
        LOG.info("%s: chunk attempt %s/%s", chunk.filename, attempt, args.chunk_attempts)
        before_length = len(manifest.rows)
        ledger.event(run_id, chunk, attempt, "started")
        attempt_flows = dict(flows)  # A failed attempt must not poison cross-chunk labels.
        try:
            result = d.download_chunk(client, template, chunk, root, manifest, order,
                                      columns, attempt_flows, args.page_size)
            flows.update(attempt_flows)
            ledger.event(run_id, chunk, attempt, "validated", result["attempt_id"])
            ledger.clear(chunk.filename)
            return result
        except KeyboardInterrupt as exc:
            record = latest_attempt(manifest, chunk.filename, before_length)
            ledger.fail(chunk, attempt, run_id, exc, template.page_url(chunk, args.page_size, 0, order),
                        log_file, record)
            ledger.event(run_id, chunk, attempt, "interrupted", record.get("attempt_id", ""), "Ctrl+C")
            raise
        except (d.ValidationError, requests.RequestException) as exc:
            record = latest_attempt(manifest, chunk.filename, before_length)
            # Persist immediately, not just on the tenth failure, so interruptions
            # and --retry-failed can also find partially retried chunks.
            ledger.fail(chunk, attempt, run_id, exc, template.page_url(chunk, args.page_size, 0, order),
                        log_file, record)
            ledger.event(run_id, chunk, attempt, "failed", record.get("attempt_id", ""), str(exc))
            LOG.warning("%s attempt %s failed: %s", chunk.filename, attempt, exc)
            if attempt < args.chunk_attempts:
                wait_before_retry(attempt, args)
        except (OSError, sqlite3.Error) as exc:
            # Disk-full, damaged manifest, or persistent write permissions are
            # storage failures: do not march through 1,280 chunks without evidence.
            record = latest_attempt(manifest, chunk.filename, before_length)
            ledger.fail(chunk, attempt, run_id, exc, template.page_url(chunk, args.page_size, 0, order),
                        log_file, record)
            ledger.event(run_id, chunk, attempt, "storage_error", record.get("attempt_id", ""), str(exc))
            raise
    LOG.error("%s failed all %s attempts; recorded in failed_chunks.csv; continuing queue.",
              chunk.filename, args.chunk_attempts)
    return None


def process_running(pid: int) -> bool:
    if pid <= 0:
        return True  # Malformed locks must not trigger unsafe process/group checks.
    if os.name == "nt":
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # Only ERROR_INVALID_PARAMETER proves no such PID.
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def clear_dead_lock(root):
    path = root / ".download.lock"
    if not path.exists():
        return
    try:
        pid = int(json.loads(path.read_text(encoding="utf-8"))["pid"])
    except (ValueError, KeyError):
        raise d.ValidationError(f"Unreadable lock {path}; inspect before removing it.")
    if not process_running(pid):
        LOG.warning("Removing stale lock from exited process %s", pid)
        path.unlink()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-url-file", type=Path, default=HERE / "base_api_url.txt")
    p.add_argument("--output-dir", type=Path, default=HERE / "bcmm_hs6_annual")
    p.add_argument("--start-year", type=int, default=d.START_YEAR)
    p.add_argument("--end-year", type=int, default=d.END_YEAR)
    p.add_argument("--states", default="01-32")
    p.add_argument("--flows", default="1,2", choices=["1", "2", "1,2"])
    p.add_argument("--page-size", type=int, default=PAGE_SIZE)
    p.add_argument("--chunk-attempts", type=int, default=CHUNK_ATTEMPTS)
    p.add_argument("--connect-timeout", type=float, default=CONNECT_TIMEOUT)
    p.add_argument("--read-timeout", type=float, default=READ_TIMEOUT)
    p.add_argument("--request-pause", type=float, default=REQUEST_PAUSE)
    p.add_argument("--chunk-pause", type=float, default=CHUNK_PAUSE)
    p.add_argument("--retry-wait", type=float, default=RETRY_WAIT)
    p.add_argument("--retry-wait-max", type=float, default=RETRY_WAIT_MAX)
    p.add_argument("--retry-failed", action="store_true", help="Only retry recorded unresolved chunks")
    p.add_argument("--dry-run", action="store_true", help="Print scope/config without writing or requesting data")
    args = p.parse_args(argv)
    if not 1900 <= args.start_year <= args.end_year <= 9999:
        p.error("Invalid year range")
    if args.page_size < 1 or args.chunk_attempts < 1:
        p.error("Page size and chunk attempts must be positive")
    for name in ("connect_timeout", "read_timeout", "request_pause", "chunk_pause", "retry_wait", "retry_wait_max"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or ("timeout" in name and value == 0):
            p.error(f"Invalid {name}")
    if args.retry_wait_max < args.retry_wait:
        p.error("--retry-wait-max must be >= --retry-wait")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    root = args.output_dir.resolve()
    run_id = uuid.uuid4().hex
    summary = dict(run_id=run_id, started_at=d.utc_now(), status="starting", skipped=0,
                   downloaded=0, failed=0, current_chunk="", reconciliation_status="not_performed")
    handler = None
    locked = False
    ledger = None
    try:
        template = d.QueryTemplate(args.api_url_file.read_text(encoding="utf-8-sig").strip())
        chunks = [d.Chunk(year, state, flow) for year in range(args.start_year, args.end_year + 1)
                  for state in d.state_ids(args.states) for flow in args.flows.split(",")]
        if args.dry_run:
            print(f"No requests/writes. Scope: {len(chunks)} chunks, output={root}")
            print(f"Timeouts: connect={args.connect_timeout}s read={args.read_timeout}s; "
                  f"page size={args.page_size}; chunk attempts={args.chunk_attempts}")
            print(f"Pauses: requests={args.request_pause}s chunks={args.chunk_pause}s; "
                  f"retry waits={args.retry_wait}..{args.retry_wait_max}s")
            print("Only failure-ledger entries will be selected." if args.retry_failed else "Completed chunks will be verified and skipped.")
            return 0
        root.mkdir(parents=True, exist_ok=True)
        (root / "logs").mkdir(exist_ok=True)
        clear_dead_lock(root)
        with d.output_lock(root):
            locked = True
            log_file = root / "logs" / f"standalone_{run_id}.log"
            handler = logging.FileHandler(log_file, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            LOG.addHandler(handler)
            manifest = d.Manifest(root / "manifest.csv")
            ledger = FailureLedger(root)
            ledger.save()
            if args.retry_failed:
                chunks = [chunk for chunk in chunks if chunk.filename in ledger.rows]
            summary["selected"] = len(chunks)
            summary["log_file"] = str(log_file)
            summary["settings"] = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}
            pending = []
            observed = {}
            for record in manifest.rows:
                if record["completed"] == "1" and record["flow_label"]:
                    d.observe_flow(observed, record["flow_id"], record["flow_label"])
            for chunk in chunks:
                try:
                    if d.resume_ok(root, chunk, template, manifest):
                        summary["skipped"] += 1
                        ledger.clear(chunk.filename)
                    else:
                        pending.append(chunk)
                except d.ValidationError as exc:
                    # Do not overwrite an existing file with broken provenance.
                    ledger.fail(chunk, 0, run_id, exc, template.url(chunk, "1"), log_file)
                    ledger.event(run_id, chunk, 0, "resume_validation_failed", error=str(exc))
                    summary["failed"] += 1
                    LOG.error("Existing chunk requires inspection; continuing other chunks: %s", exc)
            summary["pending"] = len(pending)
            summary["status"] = "running"
            atomic_json(root / "run_summary.json", summary)
            LOG.info("Selected %s | verified/skipped %s | pending %s | integrity issues %s",
                     len(chunks), summary["skipped"], len(pending), summary["failed"])
            if pending:
                with requests.Session() as session:
                    session.headers.update({"User-Agent": "BCMM-standalone-downloader/1.0", "Accept": "application/json"})
                    # Small page-level retries avoid discarding a large chunk for
                    # one transient network failure. Full chunk attempts remain <=10.
                    client = d.Client(session, pause=args.request_pause, timeout=args.read_timeout,
                                      max_retries=2, connect_timeout=args.connect_timeout,
                                      retry_base=10, retry_cap=60)
                    evidence_path = root / "logs" / f"preflight_{run_id}.jsonl.gz"
                    with gzip.open(evidence_path, "wt", encoding="utf-8") as evidence:
                        client.archive = evidence
                        for attempt in range(1, args.chunk_attempts + 1):
                            try:
                                reference_flows = dict(observed)
                                order, columns = d.verify_pagination(client, template, reference_flows)
                                observed.update(reference_flows)
                                break
                            except (d.ValidationError, requests.RequestException):
                                if attempt == args.chunk_attempts:
                                    raise
                                LOG.warning("Startup pagination check failed; retrying", exc_info=True)
                                wait_before_retry(attempt, args)
                    client.archive = None
                    for index, chunk in enumerate(pending, 1):
                        if index > 1:
                            LOG.info("Cooling down %.0f seconds between chunks", args.chunk_pause)
                            time.sleep(args.chunk_pause)
                        summary["current_chunk"] = chunk.filename
                        atomic_json(root / "run_summary.json", summary)
                        LOG.info("Queue %s/%s | %s", index, len(pending), chunk.filename)
                        result = run_chunk(client, template, chunk, root, manifest, order, columns,
                                           observed, args, ledger, run_id, log_file)
                        summary["downloaded" if result else "failed"] += 1
                        summary["current_chunk"] = ""
                        summary["updated_at"] = d.utc_now()
                        atomic_json(root / "run_summary.json", summary)
            summary.update(status="finished_with_failures" if summary["failed"] else "finished",
                           finished_at=d.utc_now(), unresolved_failures_all_scopes=len(ledger.rows))
            atomic_json(root / "run_summary.json", summary)
            LOG.info("FINISHED: skipped=%s downloaded=%s failed=%s. Failure list: %s",
                     summary["skipped"], summary["downloaded"], summary["failed"], ledger.path)
            return 1 if summary["failed"] else 0
    except (KeyboardInterrupt, d.ValidationError, OSError, sqlite3.Error, requests.RequestException) as exc:
        interrupted = isinstance(exc, KeyboardInterrupt)
        summary.update(status="interrupted" if interrupted else "stopped", error=f"{type(exc).__name__}: {exc}",
                       finished_at=d.utc_now())
        if locked:
            try:
                atomic_json(root / "run_summary.json", summary)
            except OSError:
                LOG.exception("Could not save run summary")
        LOG.error("%s. Completed chunks are retained; rerun the same command to resume.", summary["error"],
                  exc_info=not interrupted)
        return 130 if interrupted else 2
    finally:
        if handler is not None:
            LOG.removeHandler(handler)
            handler.close()


if __name__ == "__main__":
    sys.exit(main())
