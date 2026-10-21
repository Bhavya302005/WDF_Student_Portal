"""save_jobs against a real, disposable PostgreSQL database.

Run with:  TEST_DATABASE_URL=dbname=neon_upsert_test python3 -m unittest db.test_neon_integration
Skipped when TEST_DATABASE_URL is unset; refuses to run against Neon hosts.
"""
from __future__ import annotations

import os
import random
import string
import unittest
from unittest import mock

URL = os.getenv("TEST_DATABASE_URL", "")

SCHEMA = """
DROP TABLE IF EXISTS public.user_job_recommendations, public.jobs;
CREATE TABLE public.jobs (
  id text PRIMARY KEY, job_title text NOT NULL, company text, description text,
  skills_required jsonb NOT NULL DEFAULT '[]', skills_preferred jsonb NOT NULL DEFAULT '[]',
  experience_min real, experience_max real, experience text, location text, country text,
  work_mode text, is_remote boolean, job_type text, role_category text,
  status text NOT NULL DEFAULT 'active', salary text, salary_min real, salary_max real,
  salary_currency text, salary_period text, posted_at timestamptz, source_board text,
  basic_qualifications text, preferred_qualifications text, key_responsibilities text,
  extraction jsonb, job_url text, apply_url text,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT short_titles CHECK (length(job_title) < 200)
);
CREATE TABLE public.user_job_recommendations (
  user_id uuid NOT NULL, job_id text NOT NULL REFERENCES public.jobs(id) ON DELETE CASCADE,
  status text NOT NULL, PRIMARY KEY (user_id, job_id)
);
"""


@unittest.skipUnless(URL, "TEST_DATABASE_URL not set")
class SaveJobsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if "neon.tech" in URL:
            raise unittest.SkipTest("refusing to run destructive tests against Neon")
        import psycopg
        cls.psycopg = psycopg
        from db import neon_upsert
        cls.module = neon_upsert

    def setUp(self) -> None:
        with self.psycopg.connect(URL, autocommit=True) as conn:
            conn.execute(SCHEMA)
        self.env = mock.patch.dict(os.environ, {"NEON_DATABASE_URL": URL}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def query(self, sql: str, *params):
        with self.psycopg.connect(URL) as conn:
            return conn.execute(sql, params).fetchall()

    def execute(self, sql: str, *params) -> None:
        with self.psycopg.connect(URL, autocommit=True) as conn:
            conn.execute(sql, params)

    @staticmethod
    def jobs(count: int, text: str = "") -> list[dict]:
        rng = random.Random(7)
        return [{"id": f"job-{i}", "job_title": f"Buyer {i}", "source_board": "Greenhouse",
                 "description": text or "".join(rng.choices(string.ascii_letters + " ", k=20_000))}
                for i in range(count)]

    def table_bytes(self) -> int:
        return self.query("SELECT pg_total_relation_size('public.jobs')")[0][0]

    def test_a_rescrape_without_the_description_keeps_what_was_read_from_it(self) -> None:
        full = {"id": "wd-1", "job_title": "Buyer", "source_board": "Workday", "work_mode": "Hybrid",
                "description": "Pay range: $80,000 - $95,000 per year. 5+ years of SAP experience required.",
                "salary_min": 80000, "salary_max": 95000, "salary_currency": "USD", "salary_period": "year",
                "experience_min": 5, "skills_required": ["SAP"], "location": "Austin, TX"}
        self.module.save_jobs([full], vacuum_every=10**9)
        before = self.query("SELECT updated_at FROM public.jobs WHERE id = 'wd-1'")[0][0]
        # Next week the list page only: no description, no salary, no work mode, a new location.
        thin = {"id": "wd-1", "job_title": "Buyer", "source_board": "Workday", "location": "Austin, Texas"}
        self.module.save_jobs([thin], vacuum_every=10**9)
        row = self.query("SELECT description, salary_min, experience_min, skills_required::text, work_mode, location, updated_at "
                         "FROM public.jobs WHERE id = 'wd-1'")[0]
        self.assertTrue(row[0].startswith("Pay range"))
        self.assertEqual((float(row[1]), float(row[2]), row[4]), (80000.0, 5.0, "hybrid"))
        self.assertIn("SAP", row[3])
        self.assertEqual(row[5], "Austin, Texas")       # list-page fields still update
        self.assertGreater(row[6], before)              # and the change is recorded
        # Seen again unchanged: nothing to update.
        self.module.save_jobs([thin], vacuum_every=10**9)
        self.assertEqual(self.query("SELECT updated_at FROM public.jobs WHERE id = 'wd-1'")[0][0], row[6])
        # A full row with a new description replaces everything, including clearing the salary.
        self.module.save_jobs([{**full, "description": "Updated posting, no pay stated.", "salary_min": None, "salary_max": None}],
                              vacuum_every=10**9)
        self.assertEqual(self.query("SELECT description, salary_min FROM public.jobs WHERE id = 'wd-1'")[0],
                         ("Updated posting, no pay stated.", None))

    def test_posted_date_only_moves_back(self) -> None:
        job = {"id": "gh-1", "job_title": "Buyer", "source_board": "Greenhouse", "description": "x"}
        self.module.save_jobs([{**job, "posted_at": "2026-09-09T08:00:00+00:00"}], vacuum_every=10**9)
        # An edit on the board (Greenhouse updated_at) reports a later date: keep the first.
        self.module.save_jobs([{**job, "posted_at": "2026-09-29T08:00:00+00:00"}], vacuum_every=10**9)
        self.module.save_jobs([job], vacuum_every=10**9)  # no date this time: keep it
        posted = self.query("SELECT posted_at::date::text FROM public.jobs WHERE id = 'gh-1'")[0][0]
        self.assertEqual(posted, "2026-09-09")
        # An earlier date found later (first_published) does replace it.
        self.module.save_jobs([{**job, "posted_at": "2026-09-01T08:00:00+00:00"}], vacuum_every=10**9)
        self.assertEqual(self.query("SELECT posted_at::date::text FROM public.jobs WHERE id = 'gh-1'")[0][0],
                         "2026-09-01")

    def test_unchanged_reupsert_does_not_rewrite_text_or_bump_updated_at(self) -> None:
        jobs = self.jobs(300)
        self.module.save_jobs(jobs, vacuum_every=10**9)
        before_size = self.table_bytes()
        before = dict(self.query("SELECT id, updated_at FROM public.jobs"))
        self.module.save_jobs(jobs, vacuum_every=10**9)
        # A plain `SET description = EXCLUDED.description` doubles the size here.
        self.assertLess(self.table_bytes(), before_size * 1.2)
        after = self.query("SELECT id, updated_at, last_seen_at FROM public.jobs")
        self.assertTrue(all(updated == before[job_id] for job_id, updated, _ in after))
        self.assertTrue(all(seen is not None for _, _, seen in after))

    def test_changed_content_bumps_updated_at(self) -> None:
        self.module.save_jobs(self.jobs(2, "first version of the description"))
        before = dict(self.query("SELECT id, updated_at FROM public.jobs"))
        changed = self.jobs(2, "first version of the description")
        changed[0]["description"] = "second version"
        self.module.save_jobs(changed)
        after = dict(self.query("SELECT id, updated_at FROM public.jobs"))
        self.assertGreater(after["job-0"], before["job-0"])
        self.assertEqual(after["job-1"], before["job-1"])

    def test_failed_batch_raises_after_committing_good_batches(self) -> None:
        jobs = self.jobs(4, "text")
        jobs[3]["job_title"] = "x" * 300  # violates the CHECK constraint
        with self.assertRaises(RuntimeError) as raised:
            self.module.save_jobs(jobs, batch_size=2)
        self.assertIn("1 of 2 batches failed", str(raised.exception))
        self.assertEqual(self.query("SELECT count(*) FROM public.jobs")[0][0], 2)

    def test_expiry_and_purge_protect_saved_and_applied_jobs(self) -> None:
        self.module.save_jobs(self.jobs(10, "text"))
        stale = ("job-7", "job-8", "job-9")
        self.execute("UPDATE public.jobs SET last_seen_at = now() - interval '30 days' WHERE id = ANY(%s)", list(stale))
        self.execute("INSERT INTO public.user_job_recommendations VALUES "
                     "('00000000-0000-0000-0000-000000000001', 'job-9', 'applied')")
        with mock.patch.dict(os.environ, {"JOB_PURGE_EXPIRED_DAYS": "0"}):
            self.module.save_jobs(self.jobs(7, "text"), expire_missing=True)
        statuses = dict(self.query("SELECT id, status FROM public.jobs"))
        self.assertNotIn("job-7", statuses)
        self.assertNotIn("job-8", statuses)
        self.assertEqual(statuses["job-9"], "expired")  # applied: expired but kept
        self.assertEqual({statuses[f"job-{i}"] for i in range(7)}, {"active"})

    def database_mb(self) -> float:
        return self.query("SELECT pg_database_size(current_database())")[0][0] / 2**20

    def fill_for_budget(self):
        """600 jobs; 0-99 unseen for 60 days, 100-199 for 30 days, the rest recent; job-0 saved; job-150 expired."""
        self.module.save_jobs(self.jobs(600), vacuum_every=10**9)
        self.execute("UPDATE public.jobs SET last_seen_at = now() - interval '60 days' WHERE id = ANY(%s)",
                     [f"job-{i}" for i in range(100)])
        self.execute("UPDATE public.jobs SET last_seen_at = now() - interval '30 days' WHERE id = ANY(%s)",
                     [f"job-{i}" for i in range(100, 200)])
        self.execute("UPDATE public.jobs SET status = 'expired' WHERE id = 'job-150'")
        self.execute("INSERT INTO public.user_job_recommendations VALUES "
                     "('00000000-0000-0000-0000-000000000001', 'job-0', 'saved')")
        # This run re-sees job-300..599 and brings 100 new jobs, with no headroom left under the limit.
        refreshed = self.jobs(600)[300:]
        brand_new = [{**job, "id": f"new-{i}", "job_title": f"New {i}"} for i, job in enumerate(self.jobs(100))]
        return refreshed + brand_new

    def test_storage_budget_removes_the_stalest_unprotected_jobs_to_fit_new_ones(self) -> None:
        incoming = self.fill_for_budget()
        limit = {"JOBS_DB_MAX_MB": str(self.database_mb() / 0.92)}  # budget == current size: no headroom
        with mock.patch.dict(os.environ, limit):
            self.module.save_jobs(incoming, vacuum_every=10**9)
        ids = {row[0] for row in self.query("SELECT id FROM public.jobs")}
        removed = {f"job-{i}" for i in range(600)} - ids
        self.assertTrue(70 <= len(removed) <= 160, len(removed))              # about the 100 new jobs' worth
        self.assertIn("job-150", removed)                                      # expired goes first
        self.assertNotIn("job-0", removed)                                     # saved: protected
        self.assertTrue(removed <= {f"job-{i}" for i in range(1, 300)})        # only unseen jobs are candidates
        self.assertTrue({f"job-{i}" for i in range(1, 70)} <= removed)         # stalest (60 days) before 30-day ones
        self.assertTrue({f"job-{i}" for i in range(300, 600)} <= ids)          # seen in this run: kept
        self.assertEqual(sum(1 for i in ids if i.startswith("new-")), 100)     # and the new ones all fit

    def test_storage_budget_dry_run_and_no_limit_delete_nothing(self) -> None:
        incoming = self.fill_for_budget()
        before = self.query("SELECT count(*) FROM public.jobs")[0][0]
        with mock.patch.dict(os.environ, {"JOBS_DB_MAX_MB": str(self.database_mb() / 0.92), "JOB_RETENTION_DRY_RUN": "true"}):
            self.module.save_jobs(incoming, vacuum_every=10**9)
        self.assertEqual(self.query("SELECT count(*) FROM public.jobs")[0][0], before + 100)  # only the inserts
        self.execute("DELETE FROM public.jobs WHERE starts_with(id, 'new-')")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("JOBS_DB_MAX_MB", None)
            self.module.save_jobs(incoming, vacuum_every=10**9)
        self.assertEqual(self.query("SELECT count(*) FROM public.jobs")[0][0], before + 100)

    def test_storage_budget_never_deletes_more_than_the_cap(self) -> None:
        incoming = self.fill_for_budget()
        with mock.patch.dict(os.environ, {"JOBS_DB_MAX_MB": str(self.database_mb() / 0.92),
                                          "JOB_RETENTION_MAX_DELETE_PCT": "5"}):
            self.module.save_jobs(incoming, vacuum_every=10**9)
        self.assertGreaterEqual(self.query("SELECT count(*) FROM public.jobs")[0][0], 600 - 30)  # 5% of 600

    def test_expired_jobs_are_deleted_by_default_after_a_week(self) -> None:
        self.module.save_jobs(self.jobs(6, "text"))
        self.execute("UPDATE public.jobs SET status = 'expired', updated_at = now() - interval '10 days' WHERE id IN ('job-4', 'job-5')")
        self.execute("UPDATE public.jobs SET status = 'expired', updated_at = now() - interval '2 days' WHERE id = 'job-3'")
        self.execute("UPDATE public.jobs SET last_seen_at = now() - interval '20 days' WHERE id = 'job-1'")
        # A run that sees job-0 and job-2 (2 of the 3 active jobs, so it counts as complete).
        self.module.save_jobs([self.jobs(6, "text")[0], self.jobs(6, "text")[2]], expire_missing=True)
        status = dict(self.query("SELECT id, status FROM public.jobs"))
        self.assertEqual(status, {"job-0": "active", "job-1": "expired",  # unseen 20 days (> 14): expired now, deleted later
                                  "job-2": "active", "job-3": "expired"})  # expired 2 days ago: kept; 4 and 5 (10 days): deleted

    def test_partial_run_does_not_expire_unseen_jobs(self) -> None:
        self.module.save_jobs(self.jobs(10, "text"))
        self.execute("UPDATE public.jobs SET last_seen_at = now() - interval '30 days' WHERE id <> 'job-0'")
        self.module.save_jobs(self.jobs(1, "text"), expire_missing=True)
        self.assertEqual(self.query("SELECT count(*) FROM public.jobs WHERE status = 'expired'")[0][0], 0)

    def test_reappearing_expired_job_is_reactivated(self) -> None:
        self.module.save_jobs(self.jobs(1, "text"))
        self.execute("UPDATE public.jobs SET status = 'expired'")
        self.module.save_jobs(self.jobs(1, "text"))
        self.assertEqual(self.query("SELECT status FROM public.jobs")[0][0], "active")


if __name__ == "__main__":
    unittest.main()
