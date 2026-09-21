"""Offline regression tests. Run: python -m unittest -v"""
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
import download_bcmm_hs6_annual as d

URL = Path(__file__).with_name("base_api_url.txt").read_text().strip()


def records(n=7):
    return [{"Municipality ID": "01001", "Municipality": "Aguascalientes",
             "HS6 ID": f"{i:06d}", "HS6": "Product", "Flow ID": "1", "Flow": "Imports",
             "Year": "2017", "State ID": "01", "State": "Aguascalientes",
             "Product Level": "6", "Country ID": "usa", "Country": "United States",
             "Trade Value": "123.4500"} for i in range(n)]


class Response:
    def __init__(self, url, payload, status=200, headers=None):
        self.url = url
        self.text = json.dumps(payload)
        self.status_code = status
        self.headers = headers or {}

    def close(self):
        pass


class Session:
    def __init__(self, rows=None, cap=2, order="offset-rows", behavior=None):
        self.rows = records() if rows is None else rows
        self.cap = cap
        self.order = order
        self.calls = []
        self.behavior = behavior

    def get(self, url, timeout=None):
        limit = parse_qs(urlsplit(url).query)["limit"][0]
        nums = list(map(int, limit.split(",")))
        if len(nums) == 1:
            offset, size = 0, nums[0]
        elif self.order == "offset-rows":
            offset, size = nums
        else:
            size, offset = nums
        self.calls.append((offset, size))
        if self.behavior:
            value = self.behavior(offset, size, len(self.calls))
            if isinstance(value, Exception):
                raise value
            if value is not None:
                return Response(url, value)
        return Response(url, {"data": self.rows[offset:offset + min(size, self.cap)]})


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "logs").mkdir()
        self.template = d.QueryTemplate(URL)
        self.chunk = self.template.reference
        self.manifest = d.Manifest(self.root / "manifest.csv")

    def tearDown(self):
        self.tmp.cleanup()

    def download(self, session=None, template=None, flows=None):
        session = session or Session()
        client = d.Client(session, pause=0, max_retries=0)
        return d.download_chunk(client, template or self.template, self.chunk, self.root,
                                self.manifest, "offset-rows", list(records()[0]),
                                {} if flows is None else flows, 5)

    def test_hidden_cap_short_pages_do_not_terminate(self):
        session = Session(cap=2)
        result = self.download(session)
        self.assertEqual(session.calls[:5], [(0, 5), (2, 5), (4, 5), (6, 5), (7, 5)])
        self.assertEqual(result["n_rows"], 7)
        self.assertEqual(result["n_pages"], 4)
        self.assertEqual(result["terminal_empty"], 1)
        self.assertEqual(result["boundary_checks"], 1)
        with (self.root / "raw" / self.chunk.filename).open(newline="") as handle:
            output = list(csv.DictReader(handle))
        self.assertEqual(output, records())
        self.assertEqual(output[0]["HS6 ID"], "000000")
        self.assertTrue(d.resume_ok(self.root, self.chunk, self.template, self.manifest))

    def test_live_limit_order_and_reverse_order(self):
        for order in ("offset-rows", "rows-offset"):
            with self.subTest(order=order):
                observed = {}
                result, columns = d.verify_pagination(d.Client(Session(order=order), pause=0),
                                                       self.template, observed)
                self.assertEqual(result, order)
                self.assertEqual(observed, {"1": "Imports"})
                self.assertIn("Country ID", columns)

    def test_preflight_rejects_all_empty_or_ignored_limit(self):
        for session in (Session(rows=[]), Session(behavior=lambda *_: {"data": records(2)})):
            with self.assertRaises(d.ValidationError):
                d.verify_pagination(d.Client(session, pause=0), self.template, {})

    def test_preflight_rejects_ignored_offset(self):
        session = Session(behavior=lambda offset, size, _: {"data": records(min(size, 2))})
        with self.assertRaisesRegex(d.ValidationError, "Offset"):
            d.verify_pagination(d.Client(session, pause=0), self.template, {})

    def test_repeated_page_fails_and_keeps_part(self):
        session = Session(behavior=lambda *_: {"data": records(2)})
        with self.assertRaisesRegex(d.ValidationError, "Repeated page"):
            self.download(session)
        self.assertFalse((self.root / "raw" / self.chunk.filename).exists())
        self.assertTrue(list((self.root / "raw").glob("*.part")))
        self.assertEqual(self.manifest.rows[-1]["status"], "failed")

    def test_duplicate_across_different_pages_is_not_dropped(self):
        rows = records()
        rows[3] = {**rows[0], "Trade Value": "999"}
        with self.assertRaisesRegex(d.ValidationError, "Duplicate logical key"):
            self.download(Session(rows=rows))

    def test_duplicate_within_page_fails(self):
        with self.assertRaisesRegex(d.ValidationError, "Duplicate logical key"):
            self.download(Session(rows=[records()[0], records()[0]]))

    def test_malformed_wrappers_and_error_with_empty_data(self):
        for payload in ({"foo": []}, {"data": None}, {"data": [1]}, {"data": [], "error": "bad"},
                        {"data": [], "records": []}, {"data": [], "source": [{"name": "wrong"}]}):
            with self.subTest(payload=payload), self.assertRaises(d.ValidationError):
                d.extract_rows(json.dumps(payload))
        with self.assertRaises(d.ValidationError):
            d.extract_rows('{"data": [], "data": []}')

    def test_numeric_tokens_and_null_preserved_without_float_rounding(self):
        result = d.extract_rows('[{"ID": 9007199254740993, "code": "010121", "x": 1.2300, "v": null}]')
        self.assertEqual(result[0], {"ID": "9007199254740993", "code": "010121", "x": "1.2300", "v": None})

    def test_raw_internal_hs6_id_is_not_truncated(self):
        rows = records(1)
        rows[0]["HS6 ID"] = "1020312"
        self.download(Session(rows=rows))
        self.assertIn('"1020312"', (self.root / "raw" / self.chunk.filename).read_text())

    def test_wrong_filters_and_missing_fields_fail(self):
        for field, value in (("Year", "2018"), ("State ID", "02"), ("Municipality ID", "02001"),
                             ("Product Level", "4"), ("Flow ID", "2"), ("Country ID", None)):
            with self.subTest(field=field):
                rows = records(1)
                rows[0][field] = value
                with self.assertRaises(d.ValidationError):
                    self.download(Session(rows=rows))
        rows = records(1)
        del rows[0]["Trade Value"]
        with self.assertRaises(d.ValidationError):
            self.download(Session(rows=rows))

    def test_optional_firms_and_strict_firms(self):
        with self.assertRaisesRegex(d.ValidationError, "Number of Firms"):
            d.QueryTemplate(URL, require_firms=True)
        strict = d.QueryTemplate(URL.replace("measures=Trade+Value", "measures=Trade+Value%2CNumber+of+Firms"))
        with self.assertRaisesRegex(d.ValidationError, "firms"):
            self.download(template=strict)
        rows = records(1)
        rows[0]["Number of Firms"] = "4"
        result = self.download(Session(rows=rows), template=strict)
        self.assertEqual(result["number_of_firms_present"], 1)

    def test_aliases_preserve_original_headers(self):
        rows = records(1)
        rows[0]["Date Year"] = rows[0].pop("Year")
        rows[0]["Country / Region ID"] = rows[0].pop("Country ID")
        rows[0]["Country / Region"] = rows[0].pop("Country")
        result = self.download(Session(rows=rows))
        self.assertIn("Date Year", json.loads(result["columns"]))

    def test_schema_drift_fails(self):
        rows = records()
        rows[2]["new_column"] = "x"
        with self.assertRaisesRegex(d.ValidationError, "Schema drift"):
            self.download(Session(rows=rows))

    def test_flow_mapping_conflict_from_previous_run_fails(self):
        with self.assertRaisesRegex(d.ValidationError, "Inconsistent Flow"):
            self.download(flows={"1": "Exports"})

    def test_preserve_repeated_parameters_and_encoding(self):
        source = URL.replace("Product+Level", "Product%20Level") + "&Flow=1&locale=en&locale=en"
        template = d.QueryTemplate(source)
        result = template.page_url(d.Chunk(2020, "09", "2"), 1000, 25, "offset-rows")
        self.assertIn("Product%20Level=6", result)
        self.assertEqual(result.count("Flow=2"), 2)
        self.assertIn("&locale=en&locale=en", result)
        self.assertIn("State=9", result)
        self.assertIn("limit=25%2C1000", result)
        repeated_drills = URL.replace("%2CCountry&measures", "&drilldowns=Country&measures")
        self.assertIn("&drilldowns=Country", d.QueryTemplate(repeated_drills).url(self.chunk, "1"))

    def test_padded_state_in_template_is_retained(self):
        template = d.QueryTemplate(URL.replace("State=1&", "State=01&"))
        self.assertIn("State=09", template.url(d.Chunk(2018, "09", "1"), "1"))

    def test_conflicting_repeated_parameters_and_hidden_filters_fail(self):
        for suffix in ("&Flow=2", "&Country=usa", "&cuts%5B0%5D=x", "&top=10", "&Month=1"):
            with self.subTest(suffix=suffix), self.assertRaises(d.ValidationError):
                d.QueryTemplate(URL + suffix)
        with self.assertRaises(d.ValidationError):
            d.QueryTemplate(URL.replace("%2CCountry", "%2CContinent"))

    def test_resume_rejects_modified_file_and_changed_query(self):
        self.download()
        altered = d.QueryTemplate(URL + "&locale=en")
        with self.assertRaisesRegex(d.ValidationError, "Query changed"):
            d.resume_ok(self.root, self.chunk, altered, self.manifest)
        path = self.root / "raw" / self.chunk.filename
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(d.ValidationError, "Checksum"):
            d.resume_ok(self.root, self.chunk, self.template, self.manifest)

    def test_resume_rejects_orphan(self):
        (self.root / "raw").mkdir()
        (self.root / "raw" / self.chunk.filename).write_text("plausible,header\n")
        with self.assertRaisesRegex(d.ValidationError, "Orphan"):
            d.resume_ok(self.root, self.chunk, self.template, self.manifest)

    def test_failed_force_preserves_previous_valid_csv(self):
        self.download()
        path = self.root / "raw" / self.chunk.filename
        original = path.read_bytes()
        with self.assertRaises(requests.Timeout):
            self.download(Session(behavior=lambda *_: requests.Timeout("network down")))
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(d.resume_ok(self.root, self.chunk, self.template, self.manifest))

    def test_successful_force_records_previous_hash(self):
        first = self.download()
        second = self.download()
        self.assertEqual(second["replaces_sha256"], first["sha256"])
        self.assertNotEqual(first["response_archive"], second["response_archive"])

    def test_empty_chunk_is_not_a_zero_observation(self):
        result = self.download(Session(rows=[]))
        self.assertEqual(result["n_rows"], 0)
        self.assertEqual(result["number_of_firms_present"], "unverified_empty")
        with (self.root / "raw" / self.chunk.filename).open(newline="") as handle:
            self.assertEqual(len(list(csv.reader(handle))), 1)

    def test_premature_empty_page_fails_boundary_check(self):
        session = Session(behavior=lambda offset, size, _: {"data": []} if offset == 4 and size == 5 else None)
        with self.assertRaisesRegex(d.ValidationError, "Terminal boundary"):
            self.download(session)

    def test_first_page_changes_during_download(self):
        def changed(offset, size, count):
            if count > 5 and offset == 0:
                rows = records(2)
                rows[0]["Trade Value"] = "999"
                return {"data": rows}
        with self.assertRaisesRegex(d.ValidationError, "First page changed"):
            self.download(Session(behavior=changed))

    def test_retry_rate_limit_then_success_and_persistent_error(self):
        class RetrySession:
            count = 0
            def get(self, url, timeout=None):
                self.count += 1
                return Response(url, {"data": records(1)}, 429 if self.count == 1 else 200,
                                {"Retry-After": "2"})
        client = d.Client(RetrySession(), pause=0, max_retries=1)
        with patch.object(d.time, "sleep") as sleep:
            self.assertEqual(len(client.get(URL)), 1)
            sleep.assert_called_with(2.0)
        self.assertEqual(client.retries, 1)
        self.assertEqual(client.errors, 1)
        client = d.Client(Session(behavior=lambda *_: requests.Timeout("timeout")), pause=0, max_retries=2)
        with patch.object(d.time, "sleep"), self.assertRaises(requests.Timeout):
            client.get(URL + "&limit=2")
        self.assertEqual(client.retries, 2)

    def test_concurrent_output_lock_rejected(self):
        with d.output_lock(self.root):
            with self.assertRaises(d.ValidationError):
                with d.output_lock(self.root):
                    self.fail("must not acquire twice")
        self.assertFalse((self.root / ".download.lock").exists())

    def test_generated_fixture_round_trip(self):
        rows = records(37)
        result = self.download(Session(rows=rows, cap=2))
        with (self.root / "raw" / self.chunk.filename).open(encoding="utf-8", newline="") as handle:
            self.assertEqual(list(csv.DictReader(handle)), rows)
        self.assertEqual(result["n_rows"], 37)

    def test_resume_rejects_changed_response_archive(self):
        result = self.download()
        (self.root / result["response_archive"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(d.ValidationError, "response archive"):
            d.resume_ok(self.root, self.chunk, self.template, self.manifest)

    def test_interruption_records_failure_without_final_csv(self):
        class InterruptedSession:
            def get(self, *args, **kwargs):
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.download(InterruptedSession())
        self.assertEqual(self.manifest.rows[-1]["status"], "interrupted")
        self.assertFalse((self.root / "raw" / self.chunk.filename).exists())

    def test_http_server_errors_retry_but_400_does_not(self):
        for status in (500, 502, 503, 504, 400):
            class ErrorSession:
                count = 0
                def get(self, url, timeout=None):
                    self.count += 1
                    return Response(url, {"error": "test failure"}, status)
            session = ErrorSession()
            client = d.Client(session, pause=0, max_retries=1)
            with patch.object(d.time, "sleep"), self.assertRaises(d.ValidationError):
                client.get(URL)
            self.assertEqual(session.count, 1 if status == 400 else 2)

    def test_windows_replace_lock_retried_without_deleting_old_file(self):
        source, target = self.root / "source", self.root / "target"
        source.write_text("new")
        target.write_text("old")
        replace = d.os.replace
        attempts = []
        def transient(source_arg, target_arg):
            attempts.append(1)
            if len(attempts) == 1:
                self.assertEqual(target.read_text(), "old")
                exc = PermissionError("Windows reader has the destination open")
                exc.winerror = 32
                raise exc
            replace(source_arg, target_arg)
        with patch.object(d.os, "replace", side_effect=transient), patch.object(d.time, "sleep"):
            d.replace_with_retry(source, target)
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(len(attempts), 2)

    def test_persistent_replace_lock_fails_with_old_file_intact(self):
        source, target = self.root / "source", self.root / "target"
        source.write_text("new")
        target.write_text("old")
        exc = PermissionError("locked")
        exc.winerror = 5
        with patch.object(d.os, "replace", side_effect=exc) as replacement, \
                patch.object(d.time, "sleep"), self.assertRaises(PermissionError):
            d.replace_with_retry(source, target, attempts=3)
        self.assertEqual(replacement.call_count, 3)
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(source.read_text(), "new")


if __name__ == "__main__":
    unittest.main()
