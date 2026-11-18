"""Integrity, period boundaries, and exact-value preservation for monthly merging."""
import contextlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import combine_bcmm_monthly as c
import download_bcmm_hs6_annual as d
from monthly_bcmm import MonthlyQueryTemplate


class MonthlyCombineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'raw').mkdir()
        (self.root / 'responses').mkdir()
        self.columns = [name.replace('HS6', 'HS4') for name in c.EXPECTED_COLUMNS] + ['Month']
        self.template = MonthlyQueryTemplate((c.HERE / 'base_api_url.txt').read_text().strip(), product_level=4)
        self.manifest = d.Manifest(self.root / 'manifest.csv')
        self.chunks = [d.Chunk(year, '01', flow, month, 4) for year, month, flow in
                       ((2006, 1, '1'), (2010, 12, '2'), (2011, 1, '1'), (2015, 12, '2'),
                        (2016, 1, '1'), (2020, 12, '2'), (2021, 1, '1'), (2025, 12, '2'))]
        self.rows = {}
        for number, chunk in enumerate(self.chunks):
            values = ['01001', 'M\u00e9rida, "centro"\nMunicipio', '00001234', 'Caf\u00e9', chunk.flow,
                      'Imports' if chunk.flow == '1' else 'Exports', str(chunk.year), '01', 'State', '4',
                      'usa', 'United States', '12345678901234567890.3400', f'{chunk.year}-{chunk.month:02d}']
            self.write_chunk(chunk, [] if number == 3 else [values])
        self.destination = self.root / 'combined_5year'

    def write_chunk(self, chunk, rows):
        self.rows[chunk.filename] = rows
        path = self.root / 'raw' / chunk.filename
        with path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle, quoting=csv.QUOTE_ALL)
            writer.writerow(self.columns)
            writer.writerows(rows)
        archive = self.root / 'responses' / (chunk.filename + '.jsonl')
        archive.write_text('{"data": []}\n', encoding='utf-8')
        self.manifest.append(dict(filename=chunk.filename, prod_level=4, year=chunk.year, ent_id=chunk.state,
                                  flow_id=chunk.flow, completed=1, status='downloaded' if rows else 'downloaded_empty',
                                  terminal_empty=1, boundary_checks=1,
                                  query_fingerprint=self.template.fingerprint(chunk),
                                  sha256=d.file_sha256(path), file_size=path.stat().st_size,
                                  response_archive=archive.relative_to(self.root).as_posix(),
                                  response_sha256=d.file_sha256(archive), columns=json.dumps(self.columns), n_rows=len(rows)))

    def run_merge(self, *extra):
        with patch.object(c, 'expected_chunks', return_value=self.chunks), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = c.main(['--product-level', '4', '--output-dir', str(self.root), *extra])
        self.assertFalse((self.root / '.download.lock').exists())
        self.assertFalse((self.root / '.combine.lock').exists())
        return result

    def test_four_periods_preserve_monthly_values_and_empty_chunks(self):
        self.assertEqual(self.run_merge(), 0)
        report = json.loads((self.destination / 'combined_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'completed')
        self.assertEqual(report['n_rows'], 7)
        self.assertEqual(report['n_chunks'], 8)
        self.assertEqual(report['n_empty_chunks'], 1)
        self.assertEqual(len(report['files']), 4)
        for first, info in zip((2006, 2011, 2016, 2021), report['files']):
            self.assertEqual(info['filename'], f'bcmm_hs4_monthly_{first}_{first+4}.csv')
            path = self.destination / info['filename']
            expected_rows = [row for chunk in self.chunks if first <= chunk.year <= first + 4
                             for row in self.rows[chunk.filename]]
            with path.open(encoding='utf-8', newline='') as handle:
                self.assertEqual(list(csv.reader(handle)), [self.columns] + expected_rows)
            self.assertEqual(info['n_rows'], len(expected_rows))
            self.assertEqual(info['sha256'], d.file_sha256(path))
        self.assertFalse(list(self.destination.glob('*.part')))

    def test_corrupt_csv_or_archive_blocks_all_outputs(self):
        for directory in ('raw', 'responses'):
            with self.subTest(directory=directory):
                path = next((self.root / directory).iterdir())
                original = path.read_bytes()
                path.write_bytes(original + b'corruption')
                self.assertEqual(self.run_merge(), 2)
                self.assertFalse(self.destination.exists())
                path.write_bytes(original)

    def test_missing_chunk_blocks_all_outputs(self):
        (self.root / 'raw' / self.chunks[-1].filename).unlink()
        self.assertEqual(self.run_merge(), 2)
        self.assertFalse(self.destination.exists())

    def test_failure_ledger_blocks_all_outputs(self):
        (self.root / 'failed_chunks.csv').write_text('filename\nfailed.csv\n')
        self.assertEqual(self.run_merge(), 2)
        self.assertFalse(self.destination.exists())

    def fail_chunk(self, chunk):
        """Turn a chunk into an unresolved failure: no raw file, no completed record, listed in the ledger."""
        (self.root / 'raw' / chunk.filename).unlink()
        manifest = self.root / 'manifest.csv'
        with manifest.open(encoding='utf-8', newline='') as handle:
            rows = [row for row in csv.DictReader(handle) if row['filename'] != chunk.filename]
        with manifest.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        with (self.root / 'failed_chunks.csv').open('a', encoding='utf-8', newline='') as handle:
            writer = csv.writer(handle)
            if handle.tell() == 0:
                writer.writerow(['filename', 'year', 'ent_id', 'flow_id', 'attempts_this_run', 'last_failed_at',
                                 'error_type', 'error', 'query_url'])
            writer.writerow([chunk.filename, chunk.year, chunk.state, chunk.flow, 10, '2026-10-15T00:00:00+00:00',
                             'ConnectTimeout', 'timed out', 'https://example.invalid/q'])

    def read_dicts(self, filename):
        with (self.destination / filename).open(encoding='utf-8', newline='') as handle:
            return list(csv.DictReader(handle))

    def test_allow_missing_excludes_and_lists_ledger_failures(self):
        # A same-state, same-year chunk supplies the state/flow names and municipality list.
        neighbour = d.Chunk(2025, '01', '1', 11, 4)
        self.chunks.append(neighbour)
        base = ['01001', 'Mérida', '00001234', 'Café', '1', 'Imports', '2025', '01', 'Aguascalientes',
                '4', 'usa', 'United States', '5', '2025-11']
        self.write_chunk(neighbour, [base, ['01005'] + base[1:], ['1002', 'Asientos'] + base[2:]])
        failed = self.chunks[-2]
        self.fail_chunk(failed)
        self.assertEqual(self.run_merge(), 2)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.run_merge('--allow-missing'), 0)
        report = json.loads((self.destination / 'combined_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual((report['status'], report['coverage']), ('completed', 'incomplete'))
        self.assertEqual((report['n_expected_chunks'], report['n_chunks'], report['n_missing_chunks']), (9, 8, 1))
        self.assertEqual(report['n_rows'], 9)
        self.assertEqual([info['n_missing_chunks'] for info in report['files']], [0, 0, 0, 1])
        listed = self.read_dicts('missing_chunks.csv')
        self.assertEqual([(r['filename'], r['year'], r['month'], r['year_month'], r['state_id'], r['state'],
                           r['flow_id'], r['n_municipalities_observed_same_state_year'], r['period_file'],
                           r['error_type']) for r in listed],
                         [(failed.filename, '2025', '12', '2025-12', '01', 'Aguascalientes', '2', '3',
                           'bcmm_hs4_monthly_2021_2025.csv', 'ConnectTimeout')])
        municipalities = self.read_dicts('missing_chunks_municipalities.csv')
        self.assertEqual([(r['year_month'], r['state_id'], r['municipality_id'], r['municipality'])
                          for r in municipalities],
                         [('2025-12', '01', '01001', 'Mérida'), ('2025-12', '01', '1002', 'Asientos'),
                          ('2025-12', '01', '01005', 'Mérida')])
        self.assertEqual([info['sha256'] for info in report['missing_lists']],
                         [d.file_sha256(self.destination / name)
                          for name in ('missing_chunks.csv', 'missing_chunks_municipalities.csv')])
        self.assertEqual(report['missing_lists'][1]['n_rows'], 3)
        self.assertFalse(list(self.destination.glob('*.part')))

    def test_allow_missing_still_blocks_undocumented_gaps(self):
        self.fail_chunk(self.chunks[-1])
        (self.root / 'raw' / self.chunks[0].filename).unlink()
        self.assertEqual(self.run_merge('--allow-missing'), 2)
        self.assertFalse(self.destination.exists())

    def test_allow_missing_rejects_ledger_entry_for_completed_chunk(self):
        self.fail_chunk(self.chunks[-1])
        with (self.root / 'failed_chunks.csv').open('a', encoding='utf-8', newline='') as handle:
            csv.writer(handle).writerow([self.chunks[0].filename] + [''] * 8)
        self.assertEqual(self.run_merge('--allow-missing'), 2)
        self.assertFalse(self.destination.exists())

    def test_complete_merge_writes_header_only_missing_list(self):
        self.assertEqual(self.run_merge(), 0)
        for name, header in (('missing_chunks.csv', c.MISSING_COLUMNS),
                             ('missing_chunks_municipalities.csv', c.MISSING_MUNICIPALITY_COLUMNS)):
            with (self.destination / name).open(encoding='utf-8', newline='') as handle:
                self.assertEqual(list(csv.reader(handle)), [header])

    def test_wrong_month_does_not_replace_existing_outputs(self):
        chunk = self.chunks[-1]
        row = list(self.rows[chunk.filename][0])
        row[-1] = '2025-11'
        self.write_chunk(chunk, [row])
        self.destination.mkdir()
        final = self.destination / 'bcmm_hs4_monthly_2006_2010.csv'
        final.write_bytes(b'previous completed output')
        self.assertEqual(self.run_merge(), 2)
        self.assertEqual(final.read_bytes(), b'previous completed output')
        self.assertFalse((self.destination / 'combined_manifest.json').exists())

    def test_annual_input_is_rejected(self):
        path = self.root / 'raw' / self.chunks[0].filename
        path.rename(path.with_name('bcmm_hs4_annual_y2006_s01_f1.csv'))
        self.assertEqual(self.run_merge(), 2)
        self.assertFalse(self.destination.exists())

    def test_existing_download_lock_is_preserved(self):
        lock = self.root / '.download.lock'
        lock.write_text('{"pid":12345}')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(c.main(['--product-level', '4', '--output-dir', str(self.root)]), 2)
        self.assertEqual(lock.read_text(), '{"pid":12345}')
        self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
