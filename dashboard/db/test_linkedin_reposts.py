from datetime import date, datetime, timezone
import unittest

from bs4 import BeautifulSoup
from db.linkedin_reposts import Calibration, annotate, header_evidence, todays_points


class RepostEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.calibration = Calibration([(date(2026, 10, 1), 4400000000),
                                        (date(2026, 10, 11), 4410000000)])

    def row(self, job_id=4405000000, listed='2026-10-10', **extra):
        return dict(job_url=f'https://www.linkedin.com/jobs/view/{job_id}', posted_at=listed, **extra)

    def test_job_header_label_wins_over_id_estimate_and_csv_roundtrip(self):
        html = '<section class="top-card-layout"><span class="posted-time-ago__text">Reposted 2 days ago</span></section>'
        evidence = header_evidence(BeautifulSoup(html, 'html.parser'))
        self.assertTrue(evidence['reposted'])
        row = self.row(job_id=4999999999, **evidence)
        row['reposted'] = 'True'  # CSVs serialize booleans as strings.
        annotate([row], self.calibration)
        self.assertIs(row['reposted'], True)
        self.assertEqual(row['reposted_source'], 'linkedin_label')
        self.assertEqual(row['repost_status'], 'Reposted')

    def test_description_or_related_job_mentions_do_not_flag_this_job(self):
        html = '<section class="top-card-layout"><span class="posted-time-ago__text">2 days ago</span></section>'
        html += '<div class="description__text">Help us analyze reposted content.</div>'
        html += '<aside><span class="posted-time-ago__text">Reposted 1 day ago</span></aside>'
        self.assertEqual(header_evidence(BeautifulSoup(html, 'html.parser')), {})
        self.assertEqual(header_evidence(BeautifulSoup('<html>Reposted</html>', 'html.parser')), {})

    def test_new_id_beyond_sample_range_is_unknown_not_clamped(self):
        row = self.row(job_id=4999999999, listed='2026-10-28')
        self.assertIsNone(self.calibration.first_day(4999999999))
        annotate([row], self.calibration)
        self.assertIsNone(row['reposted'])
        self.assertEqual(row['repost_status'], 'Unknown')
        self.assertIsNone(row['first_posted_at_estimate'])

    def test_bounded_estimates_are_labeled_and_do_not_invent_original_dates(self):
        old = self.row(listed='2026-10-10')
        fresh = self.row(listed='2026-10-06')
        annotate([old, fresh], self.calibration)
        self.assertIs(old['reposted'], True)
        self.assertIs(fresh['reposted'], False)
        self.assertEqual(old['reposted_confidence'], 'estimated')
        self.assertEqual(fresh['repost_status'], 'Not reposted (estimated)')
        self.assertEqual(old['first_posted_at_estimate'], '2026-10-06T00:00:00+00:00')
        self.assertNotIn('first_posted_at', old)

    def test_before_oldest_point_needs_sufficient_listing_date_gap(self):
        newer_listing = self.row(job_id=4399999999, listed='2026-10-10')
        old_listing = self.row(job_id=4399999999, listed='2026-09-01')
        annotate([newer_listing, old_listing], self.calibration)
        self.assertIs(newer_listing['reposted'], True)
        self.assertIsNone(old_listing['reposted'])

    def test_legacy_default_false_and_missing_dates_are_unknown(self):
        row = self.row(job_id=4999999999, is_reposted='False')
        missing = self.row(listed='invalid')
        annotate([row, missing], self.calibration)
        self.assertIsNone(row['reposted'])
        self.assertIsNone(missing['reposted'])

    def test_estimated_reannotation_is_stable_and_can_gain_page_evidence(self):
        row = self.row()
        annotate([row], self.calibration)
        expected = dict(row)
        annotate([row], self.calibration)
        self.assertEqual(row, expected)
        row.update(reposted=True, reposted_source='linkedin_label', reposted_confidence='explicit')
        annotate([row], self.calibration)
        self.assertEqual(row['repost_status'], 'Reposted')

    def test_learning_never_uses_latest_listing_or_estimated_date(self):
        today = datetime.now(timezone.utc).date().isoformat()
        row = self.row(listed=today)
        self.assertEqual(todays_points([row]), {})
        row.update(first_posted_at=today, first_posted_at_source='linkedin_original_date')
        self.assertTrue(todays_points([row]))


if __name__ == '__main__':
    unittest.main()
