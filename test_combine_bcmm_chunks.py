"""Integrity and data-preservation checks for the annual merger."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import combine_bcmm_chunks as c
import download_bcmm_hs6_annual as d


class CombineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "raw").mkdir()
        (self.root / "responses").mkdir()

    def prepare(self, level=4):
        self.level = level
        self.columns = [s.replace("HS6", f"HS{level}") for s in c.EXPECTED_COLUMNS]
        self.chunks = [d.Chunk(2006, "01", str(f), product_level=level) for f in (1, 2)]
        self.values = ["01001", 'M?rida, "centro"', "00001234", "Caf?", "1", "Imports",
                       "2006", "01", "State", str(level), "usa", "United States", "12.3400"]
        template = d.QueryTemplate((c.HERE / "base_api_url.txt").read_text().strip(),
                                   product_level=level)
        self.manifest = d.Manifest(self.root / "manifest.csv")
        for index, chunk in enumerate(self.chunks):
            path = self.root / "raw" / chunk.filename
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
                writer.writerow(self.columns)
                if index == 0:
                    writer.writerow(self.values)
            archive = self.root / "responses" / (chunk.filename + ".jsonl")
            archive.write_text('{"data": []}\n', encoding="utf-8")
            self.manifest.append(dict(filename=chunk.filename, prod_level=level, year=2006,
                                      ent_id="01", flow_id=chunk.flow, completed=1,
                                      status="downloaded" if index == 0 else "downloaded_empty",
                                      terminal_empty=1, boundary_checks=1,
                                      query_fingerprint=template.fingerprint(chunk),
                                      sha256=d.file_sha256(path), file_size=path.stat().st_size,
                                      response_archive=archive.relative_to(self.root).as_posix(),
                                      response_sha256=d.file_sha256(archive),
                                      columns=json.dumps(self.columns), n_rows=1 if index == 0 else 0))
        self.output = self.root / f"bcmm_hs{level}_annual_2006_2025_all.csv"

    def run_merge(self, default_level=False):
        args = ["--output-dir", str(self.root)]
        if not default_level:
            args += ["--product-level", str(self.level)]
        with patch.object(c, "expected_chunks", return_value=self.chunks), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = c.main(args)
        self.assertFalse((self.root / ".download.lock").exists())
        self.assertFalse((self.root / ".combine.lock").exists())
        return result

    def test_hs4_preserves_values_and_empty_chunk(self):
        self.prepare()
        self.assertEqual(self.run_merge(), 0)
        with self.output.open(encoding="utf-8", newline="") as handle:
            self.assertEqual(list(csv.reader(handle)), [self.columns, self.values])
        report = json.loads((self.root / "combined_manifest.json").read_text())
        self.assertEqual(report["n_rows"], 1)
        self.assertEqual(report["n_chunks"], 2)
        self.assertEqual(report["prod_level"], 4)
        self.assertEqual(report["sha256"], d.file_sha256(self.output))
        self.assertTrue(report["response_archives_verified"])

    def test_hs2_preserves_values_and_empty_chunk(self):
        self.prepare(2)
        self.assertEqual(self.run_merge(), 0)
        with self.output.open(encoding="utf-8", newline="") as handle:
            self.assertEqual(list(csv.reader(handle)), [self.columns, self.values])
        report = json.loads((self.root / "combined_manifest.json").read_text())
        self.assertEqual(report["prod_level"], 2)
        self.assertEqual(report["n_rows"], 1)
        self.assertEqual(report["sha256"], d.file_sha256(self.output))

    def test_hs6_remains_default(self):
        self.prepare(6)
        self.assertEqual(self.run_merge(default_level=True), 0)

    def test_failed_ledger_blocks_merge(self):
        self.prepare()
        (self.root / "failed_chunks.csv").write_text("filename\nfailed.csv\n")
        self.assertEqual(self.run_merge(), 2)
        self.assertFalse(self.output.exists())

    def test_modified_csv_and_response_block_before_merge(self):
        for location in ("raw", "responses"):
            with self.subTest(location=location):
                self.prepare()
                path = next((self.root / location).iterdir())
                path.write_bytes(path.read_bytes() + b"changed")
                self.assertEqual(self.run_merge(), 2)
                self.assertFalse(self.output.exists())
                self.assertFalse(self.output.with_suffix(".csv.part").exists())

    def test_missing_chunk_blocks_merge(self):
        self.prepare()
        (self.root / "raw" / self.chunks[1].filename).unlink()
        self.assertEqual(self.run_merge(), 2)
        self.assertFalse(self.output.exists())

    def test_bad_row_preserves_existing_output(self):
        self.prepare()
        path = self.root / "raw" / self.chunks[0].filename
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(self.columns)
            writer.writerow(self.values[:-1])
        record = dict(self.manifest.completed(self.chunks[0].filename))
        record.update(sha256=d.file_sha256(path), file_size=path.stat().st_size)
        self.manifest.append(record)
        self.output.write_bytes(b"previous output")
        self.assertEqual(self.run_merge(), 2)
        self.assertEqual(self.output.read_bytes(), b"previous output")


if __name__ == "__main__":
    unittest.main()
