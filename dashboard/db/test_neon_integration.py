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
