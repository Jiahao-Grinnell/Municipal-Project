import csv
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import download_bcmm_hs6_annual as d
import run_download as runner
from monthly_bcmm import MonthlyQueryTemplate
from test_download_bcmm_hs6_annual import URL, Response, records


class MonthlyAPI:
    def __init__(self, bad_month=None, empty_month=None):
        self.headers = {}
        self.calls = []
        self.bad_month = bad_month
        self.empty_month = empty_month

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, timeout=None):
        query = parse_qs(urlsplit(url).query)
        self.calls.append(query)
        month = query['Month'][0]
        assert 'Year' not in query and 'Date Year' not in query
        assert 'Month' in query['drilldowns'][0].split(',')
        state, flow = query['State'][0], query['Flow'][0]
        values = list(map(int, query['limit'][0].split(',')))
        offset, size = values if len(values) == 2 else (0, values[0])
        rows = records(3)
        for row in rows:
            del row['Year']
            actual = month if month != self.bad_month else month[:4] + '12'
            row.update({'Month ID': actual, 'Month': actual[:4] + '-' + actual[4:],
                        'State ID': state, 'Municipality ID': state.zfill(2) + '001',
                        'Flow ID': flow, 'Flow': 'Imports' if flow == '1' else 'Exports'})
        return Response(url, {'data': [] if month == self.empty_month else rows[offset:offset + min(size, 2)]})


class MonthlyTests(unittest.TestCase):
    def test_url_and_output_schema(self):
        template = MonthlyQueryTemplate(URL)
        chunk = d.Chunk(2025, '02', '2', 12)
        query = parse_qs(urlsplit(template.url(chunk, '0,4000')).query)
        self.assertEqual(query['Month'], ['202512'])
        self.assertEqual(query['State'], ['2'])
        self.assertEqual(query['Flow'], ['2'])
        row = records(1)[0]
        expected = list(row) + ['Month']
        source = {}
        for key, value in row.items():
            if key == 'Year':
                source.update({'Month ID': '201701', 'Month': '2017-01'})
            else:
                source[key] = value
        normalized = template.normalize_rows([source])[0]
        self.assertEqual(list(normalized), expected)
        self.assertEqual(normalized, dict(row, Month='2017-01'))
        for bad in ('201713', '2017-01', None):
            with self.assertRaises(d.ValidationError):
                template.normalize_rows([dict(source, **{'Month ID': bad})])
        with self.assertRaises(d.ValidationError):
            template.normalize_rows([dict(source, Month='2017-02')])
        with self.assertRaises(d.ValidationError):
            d.validate_row(normalized, d.schema_for(normalized, False),
                           d.Chunk(2017, '01', '1', 2), {})

    def test_full_run_failure_retry_empty_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = ['--frequency', 'monthly', '--output-dir', str(root),
                    '--start-year', '2006', '--end-year', '2006', '--months', '01-03',
                    '--states', '01', '--flows', '1', '--chunk-attempts', '2',
                    '--request-pause', '0', '--chunk-pause', '0', '--retry-wait', '0',
                    '--retry-wait-max', '0']
            with patch.object(runner.requests, 'Session', return_value=MonthlyAPI('200602', '200603')):
                self.assertEqual(runner.main(args), 1)
            manifest = d.Manifest(root / 'manifest.csv')
            failed = d.Chunk(2006, '01', '1', 2)
            self.assertEqual(runner.FailureLedger(root).rows[failed.filename]['attempts_this_run'], '2')
            complete = [r for r in manifest.rows if r['completed'] == '1']
            self.assertEqual(len(complete), 2)
            self.assertTrue(any(r['status'] == 'downloaded_empty' for r in complete))
            first = complete[0]
            with (root / 'raw' / first['filename']).open(newline='') as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]['Year'], '2006')
            self.assertEqual(rows[0]['Month'], '2006-01')
            self.assertNotIn('Month ID', rows[0])
            with gzip.open(root / first['response_archive'], 'rt', encoding='utf-8') as handle:
                body = json.loads(json.loads(next(handle))['body'])
            self.assertIn('Month ID', body['data'][0])
            with patch.object(runner.requests, 'Session', return_value=MonthlyAPI()):
                self.assertEqual(runner.main(args + ['--retry-failed']), 0)
            with patch.object(runner.requests, 'Session', side_effect=AssertionError('Unexpected network')):
                self.assertEqual(runner.main(args), 0)
            summary = json.loads((root / 'run_summary.json').read_text())
            self.assertEqual(summary['skipped'], 3)
            self.assertEqual(runner.FailureLedger(root).rows, {})
            self.assertEqual(runner.main(['--output-dir', str(root)]), 2)

    def test_defaults_and_invalid_month(self):
        self.assertEqual(runner.parse_args(['--frequency', 'monthly']).output_dir.name, 'bcmm_hs6_monthly')
        self.assertEqual(runner.parse_args([]).output_dir.name, 'bcmm_hs6_annual')
        for args in (['--frequency', 'monthly', '--months', '13'], ['--months', '01']):
            with self.assertRaises(SystemExit):
                runner.parse_args(args)


if __name__ == '__main__':
    unittest.main()
