"""HS2 regression coverage, including both frequencies, recovery and existing-level compatibility."""
import contextlib
import csv
import gzip
import io
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


class HS2API:
    def __init__(self, bad_slice=None, empty_slice=None):
        self.headers = {}
        self.calls = []
        self.bad_slice = bad_slice
        self.empty_slice = empty_slice

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def get(self, url, timeout=None):
        query = parse_qs(urlsplit(url).query)
        drills = query['drilldowns'][0].split(',')
        assert 'HS2 2 Digit' in drills and 'HS2' not in drills and 'HS6' not in drills
        assert query['Product Level'] == ['2']
        monthly = 'Month' in query
        period = query['Month' if monthly else 'Year'][0]
        state, flow = query['State'][0], query['Flow'][0]
        selection = (period, state, flow)
        self.calls.append(selection)
        values = list(map(int, query['limit'][0].split(',')))
        offset, size = values if len(values) == 2 else (0, values[0])
        source_rows = []
        for row in records(3):
            source = {}
            for key, value in row.items():
                if key == 'HS6 ID':
                    source['HS2 2 Digit ID'] = value  # Preserve leading zeros, even in internal IDs.
                elif key == 'HS6':
                    source['HS2 2 Digit'] = value
                elif key == 'Year' and monthly:
                    source.update({'Month ID': period, 'Month': period[:4] + '-' + period[4:]})
                else:
                    source[key] = value
            source.update({'State ID': state, 'Municipality ID': state.zfill(2) + '001',
                           'Flow ID': flow, 'Flow': 'Imports' if flow == '1' else 'Exports',
                           'Product Level': '6' if selection == self.bad_slice else '2'})
            if not monthly:
                source['Year'] = period
            source_rows.append(source)
        return Response(url, {'data': [] if selection == self.empty_slice else
                              source_rows[offset:offset + min(size, 2)]})


class HS2Tests(unittest.TestCase):
    def test_correct_hierarchy_native_template_and_repeated_drilldowns(self):
        split = URL.replace('%2CCountry&measures', '&drilldowns=Country&measures')
        template = d.QueryTemplate(split, product_level=2)
        chunk = d.Chunk(2025, '02', '2', product_level=2)
        result = template.url(chunk, '0,4000')
        query = parse_qs(urlsplit(result).query)
        self.assertEqual(query['Product Level'], ['2'])
        self.assertIn('HS2 2 Digit', query['drilldowns'][0].split(','))
        self.assertEqual(query['drilldowns'][1], 'Country')
        self.assertEqual(chunk.filename, 'bcmm_hs2_annual_y2025_s02_f2.csv')
        native = URL.replace('Product+Level=6', 'Product+Level=2').replace('HS6', 'HS2+2+Digit')
        self.assertEqual(d.QueryTemplate(native, product_level=2).product_name, 'HS2 2 Digit')
        for bad in (native.replace('HS2+2+Digit', 'HS2'), URL + '&Country=usa'):
            with self.assertRaises(d.ValidationError):
                d.QueryTemplate(bad, product_level=2)
        with self.assertRaises(d.ValidationError):
            template.url(d.Chunk(2025, '02', '2'), '1')

    def test_native_hs2_url_does_not_change_existing_level_acceptance(self):
        native = URL.replace('Product+Level=6', 'Product+Level=2').replace('HS6', 'HS2+2+Digit')
        for level in (4, 6):
            with self.subTest(level=level), self.assertRaises(d.ValidationError):
                d.QueryTemplate(native, product_level=level)

    def test_monthly_url_and_date_year_alias(self):
        template = MonthlyQueryTemplate(URL, product_level=2)
        chunk = d.Chunk(2025, '02', '2', 12, 2)
        query = parse_qs(urlsplit(template.url(chunk, '0,4000')).query)
        self.assertEqual(query['Month'], ['202512'])
        self.assertNotIn('Year', query)
        self.assertEqual(query['Product Level'], ['2'])
        self.assertIn('HS2 2 Digit', query['drilldowns'][0].split(','))
        self.assertIn('Month', query['drilldowns'][0].split(','))
        split_url = URL.replace('%2CCountry&measures', '&drilldowns=Country&measures')
        split = MonthlyQueryTemplate(split_url, product_level=2)
        repeated = parse_qs(urlsplit(split.url(chunk, '1')).query)['drilldowns']
        self.assertEqual(repeated[1], 'Country')
        self.assertNotIn('Country', repeated[0].split(','))
        self.assertEqual(chunk.filename, 'bcmm_hs2_monthly_y2025_m12_s02_f2.csv')
        alias_url = URL.replace('Year=2017', 'Date+Year=2017').replace('%2CYear%2C', '%2CDate+Year%2C')
        alias = MonthlyQueryTemplate(alias_url, product_level=2)
        row = {'HS2 2 Digit ID': '0101', 'HS2 2 Digit': 'Product',
               'Month ID': '201701', 'Month': '2017-01'}
        normalized = alias.normalize_rows([row])[0]
        self.assertEqual(list(normalized), ['HS2 ID', 'HS2', 'Date Year', 'Month'])
        self.assertEqual(normalized['HS2 ID'], '0101')

    def test_hs6_existing_fingerprints_and_filenames_are_unchanged(self):
        annual = d.QueryTemplate(URL)
        monthly = MonthlyQueryTemplate(URL)
        self.assertEqual(annual.fingerprint(annual.reference),
                         '362ee9f9b50cfe9fe2f6ba056d48ba67300f7fa8f391bd93759a9859707af4aa')
        self.assertEqual(monthly.fingerprint(monthly.reference),
                         '95853f075840aab686a9d0af3b8f1f16443d3497db63b27956afab7e98346d87')
        self.assertEqual(annual.reference.filename, 'bcmm_hs6_annual_y2017_s01_f1.csv')
        self.assertEqual(monthly.reference.filename, 'bcmm_hs6_monthly_y2017_m01_s01_f1.csv')
        source = records(1)
        self.assertIs(annual.normalize_rows(source), source)

    def test_only_product_headers_change_and_wrong_level_or_schema_is_rejected(self):
        template = d.QueryTemplate(URL, product_level=2)
        source = {}
        for key, value in records(1)[0].items():
            source[{'HS6 ID': 'HS2 2 Digit ID', 'HS6': 'HS2 2 Digit'}.get(key, key)] = value
        source['Product Level'] = '2'
        normalized = template.normalize_rows([source])[0]
        expected = {'HS2 ID' if k == 'HS6 ID' else 'HS2' if k == 'HS6' else k: v
                    for k, v in records(1)[0].items()}
        expected['Product Level'] = '2'
        self.assertEqual(list(normalized), list(expected))
        self.assertEqual(normalized, expected)
        schema = d.schema_for(normalized, False, 2)
        d.validate_row(normalized, schema, template.reference, {})
        with self.assertRaises(d.ValidationError):
            d.validate_row(dict(normalized, **{'Product Level': '6'}), schema, template.reference, {})
        with self.assertRaises(d.ValidationError):
            d.schema_for(records(1)[0], False, 2)
        with self.assertRaises(d.ValidationError):
            template.normalize_rows([dict(source, **{'HS2 ID': 'collision'})])

    def test_annual_and_monthly_failures_empty_chunks_recovery_and_offline_resume(self):
        for frequency in ('annual', 'monthly'):
            with self.subTest(frequency=frequency), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bad = ('200602' if frequency == 'monthly' else '2006', '1', '1')
                empty = ('200601' if frequency == 'monthly' else '2006', '1', '2')
                args = ['--product-level', '2', '--frequency', frequency, '--output-dir', str(root),
                        '--start-year', '2006', '--end-year', '2006', '--states', '01',
                        '--chunk-attempts', '2', '--request-pause', '0', '--chunk-pause', '0',
                        '--retry-wait', '0', '--retry-wait-max', '0']
                if frequency == 'monthly':
                    args += ['--months', '01-02']
                with patch.object(runner.requests, 'Session', return_value=HS2API(bad, empty)):
                    self.assertEqual(runner.main(args), 1)
                ledger = runner.FailureLedger(root)
                self.assertEqual(len(ledger.rows), 1)
                self.assertEqual(next(iter(ledger.rows.values()))['attempts_this_run'], '2')
                fixed = HS2API(empty_slice=empty)
                with patch.object(runner.requests, 'Session', return_value=fixed):
                    self.assertEqual(runner.main(args + ['--retry-failed']), 0)
                requested = [selection for selection in fixed.calls if selection[0].startswith('2006')]
                self.assertTrue(requested and all(selection == bad for selection in requested))
                self.assertEqual(runner.FailureLedger(root).rows, {})
                manifest = d.Manifest(root / 'manifest.csv')
                completed = [r for r in manifest.rows if r['completed'] == '1']
                self.assertEqual(len(completed), 4 if frequency == 'monthly' else 2)
                self.assertTrue(all(r['prod_level'] == '2' for r in completed))
                self.assertEqual(sum(r['status'] == 'downloaded_empty' for r in completed), 1)
                record = next(r for r in completed if r['status'] == 'downloaded')
                with (root / 'raw' / record['filename']).open(encoding='utf-8', newline='') as handle:
                    reader = csv.DictReader(handle)
                    columns = reader.fieldnames
                    rows = list(reader)
                self.assertEqual(len(rows), 3)
                expected_columns = ['HS2 ID' if k == 'HS6 ID' else 'HS2' if k == 'HS6' else k
                                    for k in records(1)[0]]
                if frequency == 'monthly':
                    expected_columns += ['Month']
                self.assertEqual(columns, expected_columns)
                self.assertEqual(rows[0]['HS2 ID'], '000000')
                self.assertEqual(rows[0]['Trade Value'], '123.4500')
                with gzip.open(root / record['response_archive'], 'rt', encoding='utf-8') as handle:
                    raw = json.loads(json.loads(next(handle))['body'])['data'][0]
                self.assertIn('HS2 2 Digit ID', raw)
                self.assertNotIn('HS2 ID', raw)
                if frequency == 'monthly':
                    self.assertIn('Month ID', raw)
                    self.assertNotIn('Month ID', columns)
                with patch.object(runner.requests, 'Session', side_effect=AssertionError('Resume made HTTP calls')):
                    self.assertEqual(runner.main(args), 0)
                summary = json.loads((root / 'run_summary.json').read_text())
                self.assertEqual(summary['skipped'], len(completed))

    def test_mixed_product_levels_and_frequencies_rejected_before_network_or_overwrite(self):
        for filename in ('bcmm_hs6_annual_y2017_s01_f1.csv',
                         'bcmm_hs2_monthly_y2017_m01_s01_f1.csv'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'raw').mkdir()
                raw = root / 'raw' / filename
                raw.write_text('existing data', encoding='utf-8')
                summary = root / 'run_summary.json'
                summary.write_text('existing summary', encoding='utf-8')
                with patch.object(runner.requests, 'Session', side_effect=AssertionError('Unexpected HTTP')):
                    self.assertEqual(runner.main(['--product-level', '2', '--output-dir', str(root)]), 2)
                self.assertEqual(raw.read_text(encoding='utf-8'), 'existing data')
                self.assertFalse((root / 'manifest.csv').exists())
                self.assertEqual(summary.read_text(encoding='utf-8'), 'existing summary')

    def test_hs2_defaults_and_dry_run_do_not_create_outputs(self):
        self.assertEqual(runner.parse_args(['--product-level', '2']).output_dir.name, 'bcmm_hs2_annual')
        self.assertEqual(runner.parse_args(['--product-level', '2', '--frequency', 'monthly']).output_dir.name,
                         'bcmm_hs2_monthly')
        with tempfile.TemporaryDirectory() as directory:
            for frequency, count in (('annual', 1280), ('monthly', 15360)):
                root = Path(directory) / frequency
                out = io.StringIO()
                with contextlib.redirect_stdout(out), patch.object(runner.requests, 'Session',
                                                                  side_effect=AssertionError('Unexpected HTTP')):
                    self.assertEqual(runner.main(['--product-level', '2', '--frequency', frequency,
                                                  '--output-dir', str(root), '--dry-run']), 0)
                self.assertIn(f'{count} chunks', out.getvalue())
                self.assertIn('HS2+2+Digit', out.getvalue())
                self.assertFalse(root.exists())


    def test_existing_hs4_fingerprints_are_unchanged(self):
        annual = d.QueryTemplate(URL, product_level=4)
        monthly = MonthlyQueryTemplate(URL, product_level=4)
        self.assertEqual(annual.fingerprint(annual.reference),
                         'a0aa80b0873db2b23ff4443e14fad3ccbd40c9d4e9dfd5ca62a881d959d38b9e')
        self.assertEqual(monthly.fingerprint(monthly.reference),
                         'b863f8ffa52c524590218f6568645513ef457ed11a694be4475039136f4fed7a')

    def test_existing_full_scope_requests_fingerprints_and_filenames_are_unchanged(self):
        baselines = {
            (4, 'annual'): 'c304415cad2d22cec709e4c4dc531691c138c2a14a7cb5f21b66b84e6162b4be',
            (4, 'monthly'): '40d200412b87d209317b26e27626c9cab7ec2818a79b8218b71e137ffffede90',
            (6, 'annual'): '93d145c2fd4f5d790762d1710b62e7746cc8f501c62f73a17570a5ff89b41a0f',
            (6, 'monthly'): 'b2849842ba02736b49583beea9339d4077755b911c21e4edd9210aaf60645d7a',
        }
        for (level, frequency), baseline in baselines.items():
            with self.subTest(level=level, frequency=frequency):
                cls = MonthlyQueryTemplate if frequency == 'monthly' else d.QueryTemplate
                t = cls(URL, product_level=level)
                chunks = [d.Chunk(y, f'{state:02d}', str(flow), m, level)
                          for y in range(2006, 2026)
                          for m in (range(1, 13) if frequency == 'monthly' else (None,))
                          for state in range(1, 33) for flow in (1, 2)]
                self.assertEqual(d.digest([[c.filename, t.url(c, '0,4000'), t.fingerprint(c)]
                                           for c in chunks]), baseline)

    def test_hs2_rejects_existing_hs4_and_hs6_directories(self):
        for level in (4, 6):
            for frequency in ('annual', 'monthly'):
                with self.subTest(level=level, frequency=frequency), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / 'raw').mkdir()
                    name = d.Chunk(2017, '01', '1', 1 if frequency == 'monthly' else None, level).filename
                    raw = root / 'raw' / name
                    raw.write_bytes(b'existing data')
                    summary = root / 'run_summary.json'
                    summary.write_bytes(b'existing summary')
                    with patch.object(runner.requests, 'Session', side_effect=AssertionError('Unexpected HTTP')):
                        self.assertEqual(runner.main(['--product-level', '2', '--frequency', frequency,
                                                      '--output-dir', str(root)]), 2)
                    self.assertEqual(raw.read_bytes(), b'existing data')
                    self.assertEqual(summary.read_bytes(), b'existing summary')
                    self.assertFalse((root / 'manifest.csv').exists())

    def test_active_output_lock_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / '.download.lock'
            contents = '{"pid": 12345, "started": "active"}'
            lock.write_text(contents)
            with patch.object(runner, 'process_running', return_value=True), \
                    patch.object(runner.requests, 'Session', side_effect=AssertionError('Unexpected HTTP')):
                self.assertEqual(runner.main(['--product-level', '2', '--output-dir', str(root)]), 2)
            self.assertEqual(lock.read_text(), contents)
            self.assertFalse((root / 'run_summary.json').exists())

    def test_lower_level_annual_entry_point_supports_hs2(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = ['--product-level', '2', '--api-url-file', str(Path(__file__).with_name('base_api_url.txt')),
                    '--output-dir', str(root), '--test-live', '--pause', '0']
            with patch.object(d.requests, 'Session', return_value=HS2API()):
                self.assertEqual(d.main(args), 0)
            manifest = d.Manifest(root / 'manifest.csv')
            chunk = d.Chunk(2017, '01', '1', product_level=2)
            self.assertEqual(manifest.completed(chunk.filename)['n_rows'], '3')
            t = d.QueryTemplate(URL, product_level=2)
            self.assertTrue(d.resume_ok(root, chunk, t, manifest))


if __name__ == '__main__':
    unittest.main()
