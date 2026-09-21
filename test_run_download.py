import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
import download_bcmm_hs6_annual as d
import run_download as runner
from test_download_bcmm_hs6_annual import Response, records


class FakeAPI:
    def __init__(self, bad_state=None):
        self.headers = {}
        self.calls = []
        self.bad_state = bad_state

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, timeout=None):
        params = parse_qs(urlsplit(url).query)
        year, state, flow = (params[name][0] for name in ("Year", "State", "Flow"))
        self.calls.append((year, state, flow, timeout))
        values = list(map(int, params["limit"][0].split(",")))
        offset, size = values if len(values) == 2 else (0, values[0])
        rows = records(3)
        for row in rows:
            row.update({"Year": year, "State ID": state.zfill(2),
                        "Municipality ID": state.zfill(2) + "001", "Flow ID": flow,
                        "Flow": "Imports" if flow == "1" else "Exports"})
            if year == "2006" and state == self.bad_state:
                row["Year"] = "2005"  # A validation error must retry, then allow the next chunk.
        return Response(url, {"data": rows[offset:offset + min(size, 2)]})


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.args = ["--api-url-file", str(runner.HERE / "base_api_url.txt"),
                     "--output-dir", str(self.root), "--start-year", "2006", "--end-year", "2006",
                     "--states", "01,02", "--flows", "1", "--request-pause", "0",
                     "--chunk-pause", "0", "--retry-wait", "0", "--retry-wait-max", "0"]

    def tearDown(self):
        self.tmp.cleanup()

    def run_fake(self, api, extra=None):
        with patch.object(runner.requests, "Session", return_value=api), patch.object(runner.time, "sleep"):
            return runner.main(self.args + (extra or []))

    def test_ten_failed_validation_attempts_then_next_chunk_and_retry_failed(self):
        api = FakeAPI(bad_state="1")
        self.assertEqual(self.run_fake(api), 1)
        bad_filename = d.Chunk(2006, "01", "1").filename
        good_filename = d.Chunk(2006, "02", "1").filename
        ledger = runner.FailureLedger(self.root)
        self.assertEqual(ledger.rows[bad_filename]["attempts_this_run"], "10")
        self.assertFalse((self.root / "raw" / bad_filename).exists())
        self.assertTrue((self.root / "raw" / good_filename).exists())
        self.assertEqual(sum(y == "2006" and s == "1" for y, s, _, _ in api.calls), 10)
        self.assertTrue(all(t == (60, 300) for _, _, _, t in api.calls))
        with ledger.event_path.open(encoding="utf-8", newline="") as handle:
            failed = [r for r in csv.DictReader(handle) if r["status"] == "failed"]
        self.assertEqual(len(failed), 10)
        # Only the failed chunk is selected on the explicit recovery run.
        fixed = FakeAPI()
        self.assertEqual(self.run_fake(fixed, ["--retry-failed"]), 0)
        self.assertFalse(any(y == "2006" and s == "2" for y, s, _, _ in fixed.calls))
        self.assertEqual(runner.FailureLedger(self.root).rows, {})
        self.assertTrue((self.root / "raw" / bad_filename).exists())
        # A normal restart verifies all completed files without any HTTP calls.
        resumed = FakeAPI()
        self.assertEqual(self.run_fake(resumed), 0)
        self.assertEqual(resumed.calls, [])
        summary = json.loads((self.root / "run_summary.json").read_text())
        self.assertEqual(summary["skipped"], 2)

    def test_bad_completed_file_is_logged_without_overwrite_and_queue_continues(self):
        self.assertEqual(self.run_fake(FakeAPI()), 0)
        path = self.root / "raw" / d.Chunk(2006, "01", "1").filename
        path.write_text("changed source file")
        self.assertEqual(self.run_fake(FakeAPI()), 1)
        self.assertEqual(path.read_text(), "changed source file")
        self.assertIn(path.name, runner.FailureLedger(self.root).rows)

    def test_config_defaults_are_independent_of_working_directory(self):
        args = runner.parse_args([])
        self.assertEqual(args.api_url_file, runner.HERE / "base_api_url.txt")
        self.assertEqual(args.output_dir, runner.HERE / "bcmm_hs6_annual")
        self.assertEqual(args.chunk_attempts, 10)
        self.assertEqual((args.connect_timeout, args.read_timeout), (60, 300))
        self.assertEqual((args.request_pause, args.chunk_pause), (2, 10))

    def test_chunk_retry_delays_and_cap(self):
        args = runner.parse_args([])
        with patch.object(runner.time, "sleep") as sleep:
            for attempt in range(1, 6):
                runner.wait_before_retry(attempt, args)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [20, 40, 80, 120, 120])

    def test_dead_lock_removed_but_live_lock_retained(self):
        path = self.root / ".download.lock"
        path.write_text('{"pid": 12345}')
        with patch.object(runner, "process_running", return_value=True):
            runner.clear_dead_lock(self.root)
        self.assertTrue(path.exists())
        with patch.object(runner, "process_running", return_value=False):
            runner.clear_dead_lock(self.root)
        self.assertFalse(path.exists())

    def test_new_connect_timeout_and_page_retry_spacing(self):
        class FlakyAPI(FakeAPI):
            count = 0
            def get(self, url, timeout=None):
                self.count += 1
                if self.count < 3:
                    raise requests.Timeout("temporary timeout")
                self.timeout = timeout
                return Response(url, {"data": []})
        api = FlakyAPI()
        client = d.Client(api, pause=0, timeout=300, connect_timeout=60,
                          max_retries=2, retry_base=10, retry_cap=60)
        with patch.object(d.time, "sleep") as sleep:
            self.assertEqual(client.get("https://example.test"), [])
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [10, 20])
        self.assertEqual(api.timeout, (60, 300))


if __name__ == "__main__":
    unittest.main()
