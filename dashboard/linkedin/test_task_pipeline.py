import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from task_pipeline import (TaskStore, crawl, literal, merge_record, plan, posting_key,
                           read_records, report, HERE)


def row(number, description='Full description'):
    return {'id': 'same-legacy-id', 'job_title': 'Buyer', 'company': 'Example', 'location': 'TX',
            'description': description, 'job_url': f'https://www.linkedin.com/jobs/view/{number}',
            'search_role': 'Buyer'}


class PlanningTests(unittest.TestCase):
    def test_each_role_has_independent_national_task_and_disjoint_state_groups(self):
        catalog = literal(HERE/'linkedin_68_roles.py', 'ROLES')
        states = literal(HERE/'linkedin_guest_search.py', 'US_STATES')
        for selected in [catalog[:23], catalog[23:45], catalog[45:]]:
            national = plan(selected, 'national')
            expanded = plan(selected, 'states', mode='all')
            self.assertEqual([t['role'] for t in national], selected)
            self.assertEqual(len(expanded), len(selected)*5)
            self.assertLessEqual(len(expanded), 256)
            self.assertEqual(len({t['id'] for t in expanded}), len(expanded))
            for role in selected:
                locations = [location for t in expanded if t['role'] == role for location in t['locations']]
                self.assertEqual(locations, [f'{s}, United States' for s in states])
                self.assertLessEqual(max(len(t['locations']) for t in expanded), 11)

    def test_auto_expands_capped_pending_and_missing_tasks(self):
        manifests = [dict(role='Buyer', kind='national', complete=True, capped=False),
                     dict(role='Purchasing', kind='national', complete=True, capped=True),
                     dict(role='Sourcing', kind='national', complete=False, capped=False)]
        tasks = plan(['Buyer', 'Purchasing', 'Sourcing', 'Procurement'], 'states', manifests=manifests)
        self.assertEqual({t['role'] for t in tasks}, {'Purchasing', 'Sourcing', 'Procurement'})
        self.assertEqual(len(tasks), 15)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.task = {'id': 'national-test-0', 'role': 'Buyer', 'kind': 'national', 'locations': ['United States']}

    def test_same_title_company_location_different_postings_are_not_dropped(self):
        store = TaskStore(self.root, self.task, 'week')
        store.checkpoint('United States', [row(100), row(200)], {'next_offset': 50})
        store.snapshot()
        self.assertEqual(set(store.jobs), {'linkedin-100', 'linkedin-200'})
        records = list(read_records(store.csv_path))
        self.assertEqual({r['id'] for r in records}, {'linkedin-100', 'linkedin-200'})

    def test_cursor_never_advances_before_data_is_durable(self):
        store = TaskStore(self.root, self.task, 'week')
        with patch.object(store, 'save', side_effect=OSError('simulated checkpoint write interruption')):
            with self.assertRaises(OSError):
                store.checkpoint('United States', [row(100)], {'next_offset': 50})
        persisted = json.loads(store.state_path.read_text())
        self.assertNotIn('United States', persisted['locations'])
        self.assertEqual(len(list(read_records(store.journal))), 1)
        resumed = TaskStore(self.root, self.task, 'week')
        self.assertIn('linkedin-100', resumed.jobs)

    def test_resume_copies_old_rows_and_keeps_cursor_with_finished_regions(self):
        store = TaskStore(self.root/'old', self.task, 'week')
        store.checkpoint('United States', [row(100)], {'next_offset': 100, 'done': False})
        resumed = TaskStore(self.root/'new', self.task, 'week', self.root/'old')
        self.assertEqual(resumed.state['locations']['United States']['next_offset'], 100)
        self.assertIn('linkedin-100', resumed.jobs)
        resumed.checkpoint('United States', [row(200)], {'next_offset': 150})
        resumed.snapshot()
        self.assertEqual(len(list(read_records(resumed.journal))), 2)

    def test_torn_last_record_does_not_destroy_previous_jobs(self):
        store = TaskStore(self.root, self.task, 'week')
        store.checkpoint('United States', [row(100)], {'next_offset': 50})
        with store.journal.open('ab') as handle: handle.write(b'{"id": "unfinished\xff')
        resumed = TaskStore(self.root, self.task, 'week')
        self.assertEqual(set(resumed.jobs), {'linkedin-100'})
        resumed.checkpoint('United States', [row(200)], {'next_offset': 100})
        self.assertEqual(len(list(read_records(resumed.journal))), 2)

    def test_listing_only_row_cannot_erase_existing_jd(self):
        result = merge_record(row(100), row(100, ''))
        self.assertEqual(result['description'], 'Full description')
        self.assertEqual(posting_key(row(100)), posting_key({**row(100), 'job_url': row(100)['job_url']+'?trk=test'}))

    def test_verified_new_detail_can_update_an_older_longer_jd(self):
        result = merge_record(row(100), {**row(100, 'Updated JD'), '_linkedin_detail_fetched': True})
        self.assertEqual(result['description'], 'Updated JD')

    def test_resume_rejects_a_different_publication_window(self):
        store = TaskStore(self.root/'old', self.task, 'week')
        store.checkpoint('United States', [row(100)], {'next_offset': 50})
        with self.assertRaisesRegex(ValueError, 'same post_time'):
            TaskStore(self.root/'new', self.task, 'day', self.root/'old')

    def test_failed_planning_cannot_report_complete(self):
        store = TaskStore(self.root, self.task, 'week')
        store.checkpoint('United States', [row(100)], {'done': True})
        store.snapshot()
        with patch.dict('os.environ', {'STATE_PLAN_RESULT': 'failure'}):
            value = report(self.root, self.root/'progress.json', [self.task], store.csv_path)
        self.assertFalse(value['complete'])
        self.assertTrue(value['pipeline_error'])
        self.assertEqual(value['unique_postings_saved'], 1)

    def test_expected_task_without_artifact_is_reported_pending(self):
        result = report(self.root, self.root/'progress.json', [self.task])
        self.assertFalse(result['complete'])
        self.assertEqual(result['pending_regions'][0]['role'], 'Buyer')
        self.assertEqual(result['pending_regions'][0]['reason'], 'not run')

    def test_detail_failure_keeps_the_search_page_and_a_retryable_cursor(self):
        import pandas as pd
        import linkedin_68_roles as scraper
        frame = pd.DataFrame([{'title': 'Buyer', 'company': 'Example', 'location': 'TX',
                              'job_url': 'https://www.linkedin.com/jobs/view/1234567890'}])
        frame.attrs['end'] = True
        with patch.object(scraper, 'search_batch', return_value=frame), \
             patch.object(scraper, 'fetch_job_details_with_retry', side_effect=RuntimeError('details unavailable')), \
             patch.object(scraper, '_fetch_one_job_details', side_effect=RuntimeError('still unavailable')), \
             patch.object(scraper.time, 'sleep'), patch.object(scraper, '_deadline', None):
            status = crawl(self.task, 'week', self.root, minutes=10)
        self.assertEqual(status, 3)
        restored = TaskStore(self.root, self.task, 'week')
        self.assertEqual(len(restored.jobs), 1)
        self.assertFalse(restored.state['locations']['United States']['done'])
        self.assertEqual(restored.state['locations']['United States']['next_offset'], 0)


if __name__ == '__main__': unittest.main()
