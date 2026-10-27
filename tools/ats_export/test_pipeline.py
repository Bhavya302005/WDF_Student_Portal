import argparse
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pipeline import merge, partitions, run_slice, write_json
from job_extraction.engine import extract_job


class PipelineTests(unittest.TestCase):
    def test_partitions_cover_every_board_once_and_ignore_disabled(self):
        boards = [{'provider': p, 'board': str(i), 'company': str(i)}
                  for p in ('adp', 'icims') for i in range(103)]
        boards += [dict(boards[0]), {'provider': 'adp', 'board': 'disabled', 'status': 'disabled'}]
        slices = partitions(boards, 50)
        keys = [(b['provider'], b['board']) for s in slices for b in s['boards']]
        self.assertEqual(len(slices), 6)
        self.assertEqual(len(keys), 206)
        self.assertEqual(len(set(keys)), 206)
        self.assertEqual(slices, partitions(list(reversed(boards)), 50))

    def test_max_boards_is_per_provider(self):
        boards = [{'provider': p, 'board': str(i)} for p in ('adp', 'icims') for i in range(10)]
        self.assertEqual(sum(len(s['boards']) for s in partitions(boards, 5, 3)), 6)

    def test_reject_matrix_overflow(self):
        with self.assertRaisesRegex(ValueError, '256'):
            partitions([{'provider': 'adp', 'board': str(i)} for i in range(257)], 1)

    def test_full_catalog_expected_matrix_size(self):
        slices = partitions(json.loads((Path(__file__).parent / 'boards.json').read_text()), 50)
        self.assertEqual(len(slices), 121)
        self.assertEqual(sum(len(s['boards']) for s in slices), 5993)

    def test_merge_preserves_multiline_csv_and_distinct_requisitions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {'catalog_hash': 'test', 'boards': {'adp': 1},
                        'slices': [{'id': 'adp-000', 'provider': 'adp', 'count': 1}]}
            write_json(root / 'manifest.json', manifest)
            folder = root / 'slices/ats-slice-adp-000'
            row = {'source_board': 'Adp', 'job_title': 'Engineer', 'company': 'Company',
                   'description': 'Line 1, comma\nLine 2 "quotes"', 'job_url': 'https://example.com/jobs/1',
                   'skills_required': ['Python', 'SQL']}
            write_json(folder / 'jobs.json', [row, dict(row, job_url=row['job_url'] + '#apply'),
                                              dict(row, job_url='https://example.com/jobs/2')])
            write_json(folder / 'crawl-report.json', {'slice_id': 'adp-000',
                       'boards': [{'provider': 'adp', 'board': 'test', 'status': 'ok'}],
                       'failed_boards': 0, 'process_exit_code': 0})
            args = argparse.Namespace(manifest=root / 'manifest.json', input=root / 'slices',
                                      output=root / 'csv', github_summary=None)
            self.assertEqual(merge(args), 0)
            with (args.output / 'adp_usa_jobs.csv').open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]['description'], row['description'])
            self.assertEqual(json.loads(rows[0]['skills_required']), ['Python', 'SQL'])
            summary = json.loads((args.output / 'summary.json').read_text())
            self.assertEqual(summary['duplicates_removed'], 1)
            # Missing slices must fail completeness while preserving readable exports.
            manifest['slices'].append({'id': 'icims-000', 'provider': 'icims', 'count': 1})
            write_json(args.manifest, manifest)
            self.assertEqual(merge(args), 1)
            summary = json.loads((args.output / 'summary.json').read_text())
            self.assertFalse(summary['all_slices_received'])
            self.assertEqual(summary['total_jobs'], 2)
            self.assertEqual(summary['missing_or_incomplete_slices'], ['icims-000'])

    def test_salary_range_and_project_budget_safeguards(self):
        row = extract_job({'job_title': 'Engineer', 'description':
                           'Salary Range: $90-110K per year. Experience managing $100M+ capital projects. '
                           'Facilities encompassing 1.2m sq. ft.'})
        self.assertEqual((row['salary_min'], row['salary_max']), (90000, 110000))
        values = [c['min'] for c in row['extraction']['salary_candidates']]
        self.assertNotIn(100000000, values)
        self.assertNotIn(1200000, values)

    def test_partial_crawl_still_exports_jobs_and_error_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = argparse.Namespace(output=root / 'output', binary=root / 'binary',
                                      catalog=root / 'catalog.json', provider='adp',
                                      state=root / 'state', workers=2, board_timeout='5m', slice_id='adp-000')
            def fake_run(command):
                write_json(args.output / 'raw-jobs.json', [{'source_board': 'Adp',
                           'job_title': 'Engineer', 'description': 'Salary: $90-110K per year.',
                           'job_url': 'https://example.com/jobs/1'}])
                write_json(args.output / 'crawl-report.json', {'failed_boards': 1, 'boards': []})
                return argparse.Namespace(returncode=1)
            with patch('pipeline.subprocess.run', side_effect=fake_run):
                self.assertEqual(run_slice(args), 1)
            rows = json.loads((args.output / 'jobs.json').read_text())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['salary_max'], 110000)
            report = json.loads((args.output / 'crawl-report.json').read_text())
            self.assertEqual(report['process_exit_code'], 1)
            self.assertEqual(report['slice_id'], 'adp-000')


if __name__ == '__main__':
    unittest.main()
