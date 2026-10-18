"""Vector sync and semantic retrieval against real, disposable PostgreSQL databases.

    TEST_DATABASE_URL=dbname=matching_jobs_test \
    TEST_VECTOR_DATABASE_URL=dbname=matching_vectors_test \
    python -m unittest tests.test_vector_store

Skipped unless both are set; refuses Neon hosts. Needs the pgvector extension.
"""
from __future__ import annotations

import os
import unittest

from matching.config import MatchingSettings
from matching.embeddings import HashingEmbeddingProvider

JOBS_URL = os.getenv("TEST_DATABASE_URL", "")
VECTORS_URL = os.getenv("TEST_VECTOR_DATABASE_URL", "")

JOBS_SCHEMA = """
DROP TABLE IF EXISTS public.jobs, public.matching_state;
CREATE TABLE public.jobs (
  id text PRIMARY KEY, job_title text NOT NULL, company text, description text,
  skills_required jsonb NOT NULL DEFAULT '[]', skills_preferred jsonb NOT NULL DEFAULT '[]',
  experience_min real, experience_max real, experience text, location text, country text,
  work_mode text, is_remote boolean, job_type text, role_category text,
  status text NOT NULL DEFAULT 'active', salary text, salary_min real, salary_max real,
  salary_currency text, salary_period text, posted_at timestamptz, source_board text,
  extraction jsonb, job_url text, apply_url text,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.matching_state (singleton boolean PRIMARY KEY DEFAULT true, generation bigint NOT NULL DEFAULT 1);
INSERT INTO public.matching_state VALUES (true, 1);
"""

JOBS = [
    ("backend", "Backend Engineer", "Python services, PostgreSQL, Kubernetes and AWS for payments.", "remote"),
    # An unusual title the role vocabulary does not cover, but the same work.
    ("platform", "Member of Payments Platform Crew", "Python services, PostgreSQL and AWS for payments APIs.", "remote"),
    ("chef", "Pastry Chef", "Croissants, laminated dough and wedding cakes.", "onsite"),
    ("onsite", "Backend Engineer", "Python services and PostgreSQL in our office.", "onsite"),
]


class Embeddings(HashingEmbeddingProvider):
    """Deterministic stand-in for BGE; similarity follows shared words."""

    def __init__(self) -> None:
        super().__init__(384, "hashing-test")
        self.model_name = "test-hashing"
        self.texts_embedded = 0

    def embed(self, texts):
        self.texts_embedded += len(texts)
        return super().embed(texts)


@unittest.skipUnless(JOBS_URL and VECTORS_URL, "TEST_DATABASE_URL / TEST_VECTOR_DATABASE_URL not set")
class VectorStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if "neon.tech" in JOBS_URL + VECTORS_URL:
            raise unittest.SkipTest("refusing to run destructive tests against Neon")
        import psycopg
        cls.psycopg = psycopg

    def setUp(self) -> None:
        with self.psycopg.connect(JOBS_URL, autocommit=True) as conn:
            conn.execute(JOBS_SCHEMA)
            for job_id, title, description, mode in JOBS:
                conn.execute("INSERT INTO public.jobs (id, job_title, company, description, work_mode, job_type,"
                             " skills_required) VALUES (%s, %s, %s, %s, %s, 'full time', '[\"Python\"]')",
                             (job_id, title, f"Co {job_id}", description, mode))
        with self.psycopg.connect(VECTORS_URL, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS public.job_vectors, public.job_vector_sync")
        self.embeddings = Embeddings()

    def sync(self, **kwargs):
        from matching.vector_sync import sync
        return sync(JOBS_URL, VECTORS_URL, self.embeddings, **kwargs)

    def jobs(self, sql: str, *params) -> None:
        with self.psycopg.connect(JOBS_URL, autocommit=True) as conn:
            conn.execute(sql, params)

    def vectors(self, sql: str):
        with self.psycopg.connect(VECTORS_URL) as conn:
            return conn.execute(sql).fetchall()

    def test_sync_embeds_once_and_tracks_changes(self) -> None:
        self.assertEqual(self.sync(full=True)["embedded"], 4)
        again = self.sync(full=True)
        self.assertEqual((again["embedded"], again["unchanged"]), (0, 4))

        self.jobs("UPDATE public.jobs SET description = 'Sourdough only', updated_at = now() WHERE id = 'chef'")
        self.jobs("UPDATE public.jobs SET status = 'expired', updated_at = now() WHERE id = 'onsite'")
        changed = self.sync()
        self.assertEqual((changed["embedded"], changed["metadata_updated"]), (1, 1))
        self.assertEqual(dict(self.vectors("SELECT job_id, active FROM job_vectors"))["onsite"], False)

        self.jobs("DELETE FROM public.jobs WHERE id = 'chef'")
        self.assertEqual(self.sync(full=True)["removed"], 1)
        self.assertNotIn("chef", dict(self.vectors("SELECT job_id, active FROM job_vectors")))

    def test_parallel_shards_cover_every_job_once_then_finalize_indexes(self) -> None:
        from datetime import datetime, timezone
        from matching.vector_sync import finalize, prepare_bulk
        self.sync(full=True)  # an existing index, as on a re-backfill
        prepare_bulk(VECTORS_URL)
        self.assertEqual(self.indexes(), [])
        self.vectors_execute("TRUNCATE job_vectors")
        before = self.vectors("SELECT last_source_updated_at FROM job_vector_sync")
        started = datetime.now(timezone.utc)
        runs = [self.sync(full=True, shard=(k, 3), bulk=True) for k in range(3)]
        self.assertEqual(sum(run["read"] for run in runs), len(JOBS))  # disjoint and complete
        self.assertEqual(self.vectors("SELECT count(*) FROM job_vectors"), [(len(JOBS),)])
        self.assertTrue(all(not run["complete"] for run in runs))
        self.assertEqual(self.vectors("SELECT last_source_updated_at FROM job_vector_sync"), before)
        self.assertEqual(self.indexes(), [])  # bulk shards never build the ANN index

        self.jobs("DELETE FROM public.jobs WHERE id = 'chef'")
        result = finalize(JOBS_URL, VECTORS_URL, synced_at=started, reconcile=True)
        self.assertEqual((result["index_built"], result["removed"], result["vectors"]), (True, 1, len(JOBS) - 1))
        self.assertEqual(self.indexes(), ["job_vectors_embedding_hnsw"])
        self.assertEqual(self.vectors("SELECT last_source_updated_at FROM job_vector_sync"), [(started,)])

    def indexes(self) -> list[str]:
        return [r[0] for r in self.vectors("SELECT indexname FROM pg_indexes WHERE tablename = 'job_vectors' "
                                           "AND indexname LIKE '%hnsw%'")]

    def vectors_execute(self, sql: str) -> None:
        with self.psycopg.connect(VECTORS_URL, autocommit=True) as conn:
            conn.execute(sql)

    def test_identical_postings_are_embedded_once_and_share_a_shard(self) -> None:
        for city in ("Austin, TX", "Boston, MA", "Denver, CO"):
            self.jobs("INSERT INTO public.jobs (id, job_title, company, description, location) "
                      "VALUES (%s, 'Buyer', 'Acme', 'Purchase parts from suppliers.', %s)", "dup-" + city[:3], city)
        runs = [self.sync(full=True, shard=(k, 4), bulk=True) for k in range(4)]
        dup_shards = [run for run in runs if run["reused"]]
        self.assertEqual(len(dup_shards), 1)  # all three copies landed on one runner
        self.assertEqual(dup_shards[0]["reused"], 2)
        self.assertEqual(self.embeddings.texts_embedded, len(JOBS) + 1)  # one model run per distinct text
        vectors = {r[0]: r[1] for r in self.vectors("SELECT job_id, embedding::text FROM job_vectors "
                                                     "WHERE job_id LIKE 'dup-%'")}
        self.assertEqual(len(set(vectors.values())), 1)

    def test_partial_run_leaves_sync_state_untouched(self) -> None:
        self.sync(limit=2, page=2)
        self.assertEqual(self.vectors("SELECT last_source_updated_at FROM job_vector_sync"), [(None,)])

    def test_semantic_retrieval_finds_unusual_titles_and_respects_filters(self) -> None:
        from matching.retrieval import PostgresHybridRetriever
        from matching.schemas import CandidateProfile, RecommendationRequest
        self.sync(full=True)
        self.jobs("UPDATE public.jobs SET status = 'expired' WHERE id = 'onsite'")
        retriever = PostgresHybridRetriever(JOBS_URL, MatchingSettings(semantic_floor=0.0, semantic_ceiling=1.0),
                                            VECTORS_URL)
        self.addCleanup(retriever.close)
        text = "python postgresql aws payments services backend engineer"
        profile = CandidateProfile(user_id="u", roles=["Backend Engineer"], skills=["Python"],
                                   embedding=self.embeddings.embed_query([text])[0],
                                   embedding_model=self.embeddings.model_name)
        found = retriever.retrieve(profile, RecommendationRequest(user_id="u", profile={}, remote_only=True))
        ids = [item.job.job_id for item in found]
        self.assertIn("platform", ids)  # no role words in its title: found by meaning
        self.assertNotIn("onsite", ids)  # onsite and expired
        self.assertTrue(all(item.semantic_score is not None for item in found))
        self.assertEqual(ids.index("backend") < ids.index("platform"), True)

    def test_model_mismatch_falls_back_to_title_retrieval(self) -> None:
        from matching.retrieval import PostgresHybridRetriever
        from matching.schemas import CandidateProfile, RecommendationRequest
        self.sync(full=True)
        retriever = PostgresHybridRetriever(JOBS_URL, MatchingSettings(), VECTORS_URL)
        self.addCleanup(retriever.close)
        profile = CandidateProfile(user_id="u", roles=["Backend Engineer"], embedding=[0.1] * 384,
                                   embedding_model="some-other-model")
        found = retriever.retrieve(profile, RecommendationRequest(user_id="u", profile={}))
        self.assertTrue(found)
        self.assertTrue(all(item.semantic_score is None for item in found))


if __name__ == "__main__":
    unittest.main()
