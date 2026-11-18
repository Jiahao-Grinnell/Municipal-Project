"""HS4 regression coverage, including native hierarchy selection and HS6 compatibility."""
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


class HS4API:
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
        assert 'HS4 4 Digit' in drills and 'HS4' not in drills and 'HS6' not in drills
        assert query['Product Level'] == ['4']
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
                    source['HS4 4 Digit ID'] = value  # Preserve leading zeros, even in internal IDs.
                elif key == 'HS6':
                    source['HS4 4 Digit'] = value
                elif key == 'Year' and monthly:
                    source.update({'Month ID': period, 'Month': period[:4] + '-' + period[4:]})
                else:
                    source[key] = value
            source.update({'State ID': state, 'Municipality ID': state.zfill(2) + '001',
                           'Flow ID': flow, 'Flow': 'Imports' if flow == '1' else 'Exports',
                           'Product Level': '6' if selection == self.bad_slice else '4'})
            if not monthly:
                source['Year'] = period
            source_rows.append(source)
        return Response(url, {'data': [] if selection == self.empty_slice else
                              source_rows[offset:offset + min(size, 2)]})


class HS4Tests(unittest.TestCase):
    def test_correct_hierarchy_native_template_and_repeated_drilldowns(self):
        split = URL.replace('%2CCountry&measures', '&drilldowns=Country&measures')
        template = d.QueryTemplate(split, product_level=4)
        chunk = d.Chunk(2025, '02', '2', product_level=4)
        result = template.url(chunk, '0,4000')
        query = parse_qs(urlsplit(result).query)
        self.assertEqual(query['Product Level'], ['4'])
        self.assertIn('HS4 4 Digit', query['drilldowns'][0].split(','))
        self.assertEqual(query['drilldowns'][1], 'Country')
        self.assertEqual(chunk.filename, 'bcmm_hs4_annual_y2025_s02_f2.csv')
        native = URL.replace('Product+Level=6', 'Product+Level=4').replace('HS6', 'HS4+4+Digit')
        self.assertEqual(d.QueryTemplate(native, product_level=4).product_name, 'HS4 4 Digit')
        for bad in (native.replace('HS4+4+Digit', 'HS4'), URL + '&Country=usa'):
            with self.assertRaises(d.ValidationError):
                d.QueryTemplate(bad, product_level=4)
        with self.assertRaises(d.ValidationError):
            template.url(d.Chunk(2025, '02', '2'), '1')

    def test_monthly_url_and_date_year_alias(self):
        template = MonthlyQueryTemplate(URL, product_level=4)
        chunk = d.Chunk(2025, '02', '2', 12, 4)
        query = parse_qs(urlsplit(template.url(chunk, '0,4000')).query)
        self.assertEqual(query['Month'], ['202512'])
        self.assertNotIn('Year', query)
        self.assertEqual(query['Product Level'], ['4'])
        self.assertIn('HS4 4 Digit', query['drilldowns'][0].split(','))
        self.assertIn('Month', query['drilldowns'][0].split(','))
        split_url = URL.replace('%2CCountry&measures', '&drilldowns=Country&measures')
        split = MonthlyQueryTemplate(split_url, product_level=4)
        repeated = parse_qs(urlsplit(split.url(chunk, '1')).query)['drilldowns']
        self.assertEqual(repeated[1], 'Country')
        self.assertNotIn('Country', repeated[0].split(','))
        self.assertEqual(chunk.filename, 'bcmm_hs4_monthly_y2025_m12_s02_f2.csv')
        alias_url = URL.replace('Year=2017', 'Date+Year=2017').replace('%2CYear%2C', '%2CDate+Year%2C')
        alias = MonthlyQueryTemplate(alias_url, product_level=4)
        row = {'HS4 4 Digit ID': '0101', 'HS4 4 Digit': 'Product',
               'Month ID': '201701', 'Month': '2017-01'}
        normalized = alias.normalize_rows([row])[0]
        self.assertEqual(list(normalized), ['HS4 ID', 'HS4', 'Date Year', 'Month'])
        self.assertEqual(normalized['HS4 ID'], '0101')

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
        template = d.QueryTemplate(URL, product_level=4)
        source = {}
        for key, value in records(1)[0].items():
            source[{'HS6 ID': 'HS4 4 Digit ID', 'HS6': 'HS4 4 Digit'}.get(key, key)] = value
        source['Product Level'] = '4'
        normalized = template.normalize_rows([source])[0]
        expected = {'HS4 ID' if k == 'HS6 ID' else 'HS4' if k == 'HS6' else k: v
                    for k, v in records(1)[0].items()}
        expected['Product Level'] = '4'
        self.assertEqual(list(normalized), list(expected))
        self.assertEqual(normalized, expected)
        schema = d.schema_for(normalized, False, 4)
        d.validate_row(normalized, schema, template.reference, {})
        with self.assertRaises(d.ValidationError):
            d.validate_row(dict(normalized, **{'Product Level': '6'}), schema, template.reference, {})
        with self.assertRaises(d.ValidationError):
            d.schema_for(records(1)[0], False, 4)
        with self.assertRaises(d.ValidationError):
            template.normalize_rows([dict(source, **{'HS4 ID': 'collision'})])

    def test_annual_and_monthly_failures_empty_chunks_recovery_and_offline_resume(self):
        for frequency in ('annual', 'monthly'):
            with self.subTest(frequency=frequency), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                bad = ('200602' if frequency == 'monthly' else '2006', '1', '1')
                empty = ('200601' if frequency == 'monthly' else '2006', '1', '2')
                args = ['--product-level', '4', '--frequency', frequency, '--output-dir', str(root),
                        '--start-year', '2006', '--end-year', '2006', '--states', '01',
                        '--chunk-attempts', '2', '--request-pause', '0', '--chunk-pause', '0',
                        '--retry-wait', '0', '--retry-wait-max', '0']
                if frequency == 'monthly':
                    args += ['--months', '01-02']
                with patch.object(runner.requests, 'Session', return_value=HS4API(bad, empty)):
                    self.assertEqual(runner.main(args), 1)
                ledger = runner.FailureLedger(root)
                self.assertEqual(len(ledger.rows), 1)
                self.assertEqual(next(iter(ledger.rows.values()))['attempts_this_run'], '2')
                fixed = HS4API(empty_slice=empty)
                with patch.object(runner.requests, 'Session', return_value=fixed):
                    self.assertEqual(runner.main(args + ['--retry-failed']), 0)
                requested = [selection for selection in fixed.calls if selection[0].startswith('2006')]
                self.assertTrue(requested and all(selection == bad for selection in requested))
                self.assertEqual(runner.FailureLedger(root).rows, {})
                manifest = d.Manifest(root / 'manifest.csv')
                completed = [r for r in manifest.rows if r['completed'] == '1']
                self.assertEqual(len(completed), 4 if frequency == 'monthly' else 2)
                self.assertTrue(all(r['prod_level'] == '4' for r in completed))
                self.assertEqual(sum(r['status'] == 'downloaded_empty' for r in completed), 1)
                record = next(r for r in completed if r['status'] == 'downloaded')
                with (root / 'raw' / record['filename']).open(encoding='utf-8', newline='') as handle:
                    reader = csv.DictReader(handle)
                    columns = reader.fieldnames
                    rows = list(reader)
                self.assertEqual(len(rows), 3)
                expected_columns = ['HS4 ID' if k == 'HS6 ID' else 'HS4' if k == 'HS6' else k
                                    for k in records(1)[0]]
                if frequency == 'monthly':
                    expected_columns += ['Month']
                self.assertEqual(columns, expected_columns)
                self.assertEqual(rows[0]['HS4 ID'], '000000')
                self.assertEqual(rows[0]['Trade Value'], '123.4500')
                with gzip.open(root / record['response_archive'], 'rt', encoding='utf-8') as handle:
                    raw = json.loads(json.loads(next(handle))['body'])['data'][0]
                self.assertIn('HS4 4 Digit ID', raw)
                self.assertNotIn('HS4 ID', raw)
                if frequency == 'monthly':
                    self.assertIn('Month ID', raw)
                    self.assertNotIn('Month ID', columns)
                with patch.object(runner.requests, 'Session', side_effect=AssertionError('Resume made HTTP calls')):
                    self.assertEqual(runner.main(args), 0)
                summary = json.loads((root / 'run_summary.json').read_text())
                self.assertEqual(summary['skipped'], len(completed))

    def test_mixed_product_levels_and_frequencies_rejected_before_network_or_overwrite(self):
        for filename in ('bcmm_hs6_annual_y2017_s01_f1.csv',
                         'bcmm_hs4_monthly_y2017_m01_s01_f1.csv'):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'raw').mkdir()
                raw = root / 'raw' / filename
                raw.write_text('existing data', encoding='utf-8')
                summary = root / 'run_summary.json'
                summary.write_text('existing summary', encoding='utf-8')
                with patch.object(runner.requests, 'Session', side_effect=AssertionError('Unexpected HTTP')):
                    self.assertEqual(runner.main(['--product-level', '4', '--output-dir', str(root)]), 2)
                self.assertEqual(raw.read_text(encoding='utf-8'), 'existing data')
                self.assertFalse((root / 'manifest.csv').exists())
                self.assertEqual(summary.read_text(encoding='utf-8'), 'existing summary')

    def test_hs4_defaults_and_dry_run_do_not_create_outputs(self):
        self.assertEqual(runner.parse_args(['--product-level', '4']).output_dir.name, 'bcmm_hs4_annual')
        self.assertEqual(runner.parse_args(['--product-level', '4', '--frequency', 'monthly']).output_dir.name,
                         'bcmm_hs4_monthly')
        with tempfile.TemporaryDirectory() as directory:
            for frequency, count in (('annual', 1280), ('monthly', 15360)):
                root = Path(directory) / frequency
                out = io.StringIO()
                with contextlib.redirect_stdout(out), patch.object(runner.requests, 'Session',
                                                                  side_effect=AssertionError('Unexpected HTTP')):
                    self.assertEqual(runner.main(['--product-level', '4', '--frequency', frequency,
                                                  '--output-dir', str(root), '--dry-run']), 0)
                self.assertIn(f'{count} chunks', out.getvalue())
                self.assertIn('HS4+4+Digit', out.getvalue())
                self.assertFalse(root.exists())


if __name__ == '__main__':
    unittest.main()
