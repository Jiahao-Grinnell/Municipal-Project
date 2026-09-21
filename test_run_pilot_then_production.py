import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import run_pilot_then_production as runner


class PilotGateTests(unittest.TestCase):
    def test_record_comparison_checks_values_and_multiplicity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = root / "a.csv", root / "b.csv"
            def write(path, rows):
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["ID", "value"])
                    writer.writerows(rows)
            write(a, [("001", "1.2300"), ("002", "2")])
            write(b, [("002", "2"), ("001", "1.2300")])
            self.assertTrue(runner.compare_records(a, b, root / "compare.sqlite")["all_fields_equal"])
            for rows in ([("001", "1.23"), ("002", "2")],
                         [("001", "1.2300"), ("002", "2"), ("002", "2")]):
                write(b, rows)
                with self.assertRaises(runner.d.ValidationError):
                    runner.compare_records(a, b, root / "compare.sqlite")

    def test_downloader_failure_is_not_ignored(self):
        with patch.object(runner.d, "main", return_value=2):
            with self.assertRaisesRegex(runner.d.ValidationError, "exited 2"):
                runner.invoke(Path("base_api_url.txt"), Path("not_created"), 2017, "01")

    def test_pilot_has_exactly_12_unique_requested_chunks(self):
        self.assertEqual(len(runner.PILOT_CHUNKS), 12)
        self.assertEqual(len(set(runner.PILOT_CHUNKS)), 12)
        self.assertEqual({c.year for c in runner.PILOT_CHUNKS}, {2006, 2017, 2025})


if __name__ == "__main__":
    unittest.main()
