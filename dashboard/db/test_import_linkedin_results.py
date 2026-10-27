from pathlib import Path
import tempfile
import unittest

# Reuse the driver's no-connection test stub when psycopg is not installed locally.
from db.test_neon_upsert import map_job
from db.import_linkedin_results import combine, read_workbook
from openpyxl import Workbook


class ImportTests(unittest.TestCase):
    def test_duplicate_keeps_full_description_and_explicit_repost_evidence(self):
        short = {'id': 'same', 'job_title': 'Buyer', 'job_url': 'https://www.linkedin.com/jobs/view/12345678',
                 'description': 'Short JD', 'reposted': 'True', 'reposted_source': 'linkedin_label',
                 'reposted_confidence': 'explicit', 'reposted_evidence': 'Reposted 2 days ago'}
        full = dict(short, description='A much longer and more detailed job description',
                    reposted=None, reposted_source='unknown', reposted_confidence='unknown')
        result = combine([short, full, short])
        self.assertEqual(len(result), 1)
        self.assertEqual(result['12345678']['description'], full['description'])
        self.assertEqual(result['12345678']['reposted_confidence'], 'explicit')
        self.assertIs(map_job(result['12345678'])['reposted'], True)

    def test_title_based_id_collision_does_not_drop_distinct_postings(self):
        row = {'id': 'same-title-id', 'job_title': 'Buyer',
               'job_url': 'https://www.linkedin.com/jobs/view/12345678', 'description': 'First posting'}
        other = dict(row, job_url='https://www.linkedin.com/jobs/view/12345679', description='Second posting')
        self.assertEqual(set(combine([row, other])), {'12345678', '12345679'})

    def test_only_all_sheet_is_imported_and_metadata_is_decoded(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            book = Workbook()
            sheet = book.active
            sheet.title = 'All'
            sheet.append(['id', 'job_title', 'job_url', 'description', 'extraction'])
            sheet.append(['one', 'Buyer', 'https://www.linkedin.com/jobs/view/12345678', 'Full JD', '{"version":"jd-v7"}'])
            book.create_sheet('Other').append(['Must not be imported'])
            book.save(folder / 'jobs.xlsx')
            rows, info = read_workbook(folder)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['source_board'], 'LinkedIn')
            self.assertEqual(rows[0]['extraction']['version'], 'jd-v7')
            self.assertEqual(info['rows'], 1)


if __name__ == '__main__':
    unittest.main()
