from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import unittest

from matching.config import MatchingSettings
from matching.embeddings import HashingEmbeddingProvider
from matching.factory import local_stack
from matching.ingestion import JobProcessor
from matching.evaluation import evaluate
from matching.repository import MemoryRepository
from matching.schemas import RecommendationRequest
from matching.taxonomy import Taxonomy


def job(**overrides):
    value = {
        "id": "job-a", "job_title": "Backend Software Engineer", "company": "Acme",
        "description": "Build Python FastAPI services backed by PostgreSQL. AWS is preferred.",
        "skills_required": ["Python", "FastAPI", "Postgres"], "skills_preferred": ["AWS"],
        "experience_min": 2, "experience_max": 5, "location": "Bengaluru, India",
        "work_mode": "Hybrid", "job_type": "Full Time", "status": "active",
    }
    value.update(overrides)
    return value


def request(user="user-a", role="Software Engineer"):
    return RecommendationRequest(user_id=user, profile={
        "user_id": user, "skills": ["Python", "FastAPI", "PostgreSQL", "AWS"],
        "professional_skills": ["Python", "FastAPI"], "project_skills": ["AWS"],
        "roles": [role], "professional_years": 3, "locations": ["Bengaluru"],
        "remote_preference": False, "employment_preferences": ["full time"],
    })


class CountingEmbeddings(HashingEmbeddingProvider):
    def __init__(self):
        super().__init__(64)
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        return super().embed(texts)


class MatchingEngineTests(unittest.TestCase):
    def test_cross_user_global_job_artifacts_are_reused(self):
        repo, processor, service = local_stack(MatchingSettings(embedding_dimensions=64))
        _, initial = processor.process(job())
        self.assertIn("embedding", initial)
        service.recommend(request("user-a", "SDE"))
        service.recommend(request("user-b", "Software Engineer"))
        _, repeated = processor.process(job())
        self.assertEqual(set(), repeated)
        self.assertEqual(1, repo.metrics["embeddings_generated"])
        self.assertEqual(1, repo.metrics["job_processing_cache_hits"])

    def test_dependency_aware_updates(self):
        settings = MatchingSettings(embedding_dimensions=64)
        repo, embeddings = MemoryRepository(), CountingEmbeddings()
        processor = JobProcessor(repo, embeddings, Taxonomy(), settings)
        processor.process(job())
        original = repo.get_job("job-a")
        _, logo_change = processor.process(job(company_logo="https://cdn/new.png"))
        self.assertEqual(set(), logo_change)
        self.assertEqual(1, embeddings.calls)
        _, link_change = processor.process(job(job_url="https://example.test/jobs/job-a"))
        self.assertEqual({"display"}, link_change)
        self.assertEqual(1, embeddings.calls)
        _, skill_change = processor.process(job(job_url="https://example.test/jobs/job-a",
                                                skills_required=["Python", "FastAPI", "Redis"]))
        self.assertEqual({"skills", "embedding"}, skill_change)
        updated = repo.get_job("job-a")
        self.assertEqual(original.artifact_hashes["role"], updated.artifact_hashes["role"])
        self.assertEqual(original.artifact_hashes["normalized"], updated.artifact_hashes["normalized"])
        self.assertEqual(2, embeddings.calls)

    def test_concurrent_duplicate_ingestion_runs_expensive_work_once(self):
        settings = MatchingSettings(embedding_dimensions=64)
        repo, embeddings = MemoryRepository(), CountingEmbeddings()
        processor = JobProcessor(repo, embeddings, Taxonomy(), settings)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: processor.process(job()), range(16)))
        self.assertEqual(1, embeddings.calls)
        self.assertEqual(1, repo.metrics["embeddings_generated"])

    def test_concurrent_identical_recommendations_share_cached_result(self):
        repo, processor, service = local_stack(MatchingSettings(embedding_dimensions=64))
        processor.process(job())
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: service.recommend(request()), range(16)))
        self.assertTrue(all(x.recommendations == responses[0].recommendations for x in responses))
        self.assertGreaterEqual(repo.metrics["retrieval_cache_hits"], 15)

    def test_llm_free_grounded_explanation_and_unknown_coverage(self):
        _, processor, service = local_stack(MatchingSettings(embedding_dimensions=64))
        processor.process(job(education_requirements=None))
        result = service.recommend(request()).recommendations[0]
        self.assertIn("python", result.matched_required_skills)
        self.assertIn("Matches required skills", result.why_match)
        self.assertIsNone(result.feature_scores.education)
        self.assertGreater(result.match_score, 70)
        self.assertLess(result.evidence_coverage, 1)

    def test_required_skills_outweigh_preferred_skills(self):
        _, processor, service = local_stack(MatchingSettings(embedding_dimensions=64))
        processor.process(job(skills_required=["Python", "Redis"], skills_preferred=["AWS", "React", "Node.js"]))
        result = service.recommend(request()).recommendations[0]
        self.assertIn("redis", result.missing_required_skills)
        self.assertLess(result.feature_scores.required_skills, 1)

    def test_evaluation_metrics(self):
        metrics = evaluate([([3, 2, 0, 1], 2)])
        self.assertEqual(1.0, metrics["mrr"])
        self.assertGreater(metrics["ndcg@10"], .9)

    def test_update_dependency_matrix_and_cache_invalidation(self):
        repo, processor, service = local_stack(MatchingSettings(embedding_dimensions=64))
        processor.process(job())
        service.recommend(request())
        self.assertTrue(repo.matches)
        cases = [
            ({"company_logo": "new.png"}, set()),
            ({"location": "Mumbai, India"}, {"location", "embedding"}),
            ({"skills_required": ["Python", "Redis"]}, {"skills", "embedding"}),
            ({"description": "Build Python and Redis APIs."}, {"normalized", "skills", "embedding"}),
            ({"status": "expired"}, {"status"}),
        ]
        current = job()
        for update, expected in cases:
            current.update(update)
            _, changed = processor.process(current)
            self.assertEqual(expected, changed)
        self.assertFalse(repo.matches)

    def test_profile_and_request_cache_keys_are_canonical(self):
        repo, processor, service = local_stack(MatchingSettings(embedding_dimensions=64))
        processor.process(job())
        first = request()
        first.roles = ["Software Engineer", "Backend Engineering"]
        second = request("user-b")
        second.roles = list(reversed(first.roles))
        second.profile = {**second.profile.model_dump(), "ignored_extra": "must not fragment hash"}
        one = service.recommend(first)
        two = service.recommend(second)
        self.assertEqual(one.profile_hash, two.profile_hash)
        self.assertEqual(1, repo.metrics["retrieval_cache_hits"])

    def test_documented_hard_constraints_remove_ineligible_jobs(self):
        _, processor, service = local_stack(MatchingSettings(embedding_dimensions=64))
        recent = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        base = dict(
            job_title="Backend Software Engineer", description="Build Python FastAPI services.",
            skills_required=["Python", "FastAPI"], experience_min=2, experience_max=5,
            location="San Jose, California", work_mode="Remote", job_type="Full Time",
            salary_min=150_000, salary_max=190_000, salary_currency="USD", salary_period="year",
            posted_at=recent,
        )
        processor.process(job(id="eligible", company="Allowed", **base))
        processor.process(job(id="onsite", company="Allowed", **{**base, "work_mode": "Onsite"}))
        processor.process(job(id="wrong-role", company="Allowed", **{**base, "job_title": "Product Manager"}))
        processor.process(job(id="wrong-city", company="Allowed", **{**base, "location": "Seattle, Washington", "work_mode": "Onsite"}))
        processor.process(job(id="stale", company="Allowed", **{**base, "posted_at": old}))
        processor.process(job(id="underpaid", company="Allowed", **{**base, "salary_max": 120_000}))
        processor.process(job(id="blocked", company="BlockedCo", **base))
        processor.process(job(id="too-senior", company="Allowed", **{**base, "experience_min": 7}))

        result = service.recommend(RecommendationRequest(
            user_id="strict-user",
            profile={
                "user_id": "strict-user", "skills": ["Python", "FastAPI"],
                "professional_skills": ["Python", "FastAPI"],
                "roles": ["Backend Engineer"], "professional_roles": ["Backend Engineer"],
                "professional_years": 4, "locations": ["San Jose, California"],
                "hard_location_constraint": True, "work_mode_preferences": ["remote"],
                "employment_preferences": ["full time"], "minimum_salary": 140_000,
                "salary_currency": "USD", "salary_flexible": False,
                "strict_experience_constraint": True, "max_job_age_days": 30,
                "blocked_companies": ["BlockedCo"],
            },
            roles=["Backend Engineering"], locations=["San Jose, California"],
            work_modes=["remote"], employment_types=["full time"], limit=1000,
        ))

        self.assertEqual(["eligible"], [item.job_id for item in result.recommendations])
        self.assertEqual("High", result.recommendations[0].label)

    def test_desired_title_without_resume_evidence_cannot_be_high_match(self):
        _, processor, service = local_stack(MatchingSettings(embedding_dimensions=64))
        processor.process(job(skills_required=["Python", "FastAPI"], skills_preferred=[]))
        result = service.recommend(RecommendationRequest(user_id="career-change", profile={
            "user_id": "career-change", "skills": ["procurement"],
            "professional_skills": ["procurement"], "roles": ["Software Engineer"],
            "professional_roles": ["Supply Chain Manager"], "professional_years": 5,
        }))
        match = result.recommendations[0]
        self.assertNotEqual("High", match.label)
        self.assertLessEqual(match.match_score, 59)

    def test_salary_and_freshness_updates_do_not_regenerate_embedding(self):
        settings = MatchingSettings(embedding_dimensions=64)
        repo, embeddings = MemoryRepository(), CountingEmbeddings()
        processor = JobProcessor(repo, embeddings, Taxonomy(), settings)
        processor.process(job(salary_min=100_000, posted_at="2026-09-01"))
        _, salary_change = processor.process(job(salary_min=120_000, posted_at="2026-09-01"))
        _, date_change = processor.process(job(salary_min=120_000, posted_at="2026-09-02"))
        self.assertEqual({"compensation"}, salary_change)
        self.assertEqual({"freshness"}, date_change)
        self.assertEqual(1, embeddings.calls)

    def test_401k_benefit_is_not_persisted_as_salary(self):
        _, processor, _ = local_stack(MatchingSettings(embedding_dimensions=64))
        processed, _ = processor.process(job(
            salary="compensation package including health coverage and 401K contributions",
            salary_min=401000, salary_max=401000, salary_currency="USD",
        ))
        self.assertIsNone(processed.salary_min)
        self.assertIsNone(processed.salary_max)
        self.assertIsNone(processed.salary_currency)

    def test_corrupt_salary_ranges_are_hidden_instead_of_guessed(self):
        _, processor, _ = local_stack(MatchingSettings(embedding_dimensions=64))
        reversed_range, _ = processor.process(job(
            id="reversed", salary="$146,000 - $1", salary_min=146000,
            salary_max=1, salary_currency="USD", salary_period="year",
        ))
        implausible, _ = processor.process(job(
            id="implausible", salary="$65,000 - $68,000", salary_min=65_000_000_000,
            salary_max=68000, salary_currency="USD", salary_period="year",
        ))
        valid, _ = processor.process(job(
            id="valid", salary="$120,000 - $160,000", salary_min=120000,
            salary_max=160000, salary_currency="USD", salary_period="year",
        ))
        self.assertEqual((None, None), (reversed_range.salary_min, reversed_range.salary_max))
        self.assertEqual((None, None), (implausible.salary_min, implausible.salary_max))
        self.assertEqual((120000, 160000), (valid.salary_min, valid.salary_max))

    def test_basic_and_preferred_qualification_artifacts_are_kept_separate(self):
        repo, processor, _ = local_stack(MatchingSettings(embedding_dimensions=64))
        processor.process(job(
            description="Join the platform team.", skills_required=[], skills_preferred=[],
            basic_qualifications="Required experience with Python and FastAPI.",
            preferred_qualifications="AWS experience preferred. Bachelor's degree in computer science.",
            extraction={
                "skills": [
                    {"name": "C#", "requirement": "required"},
                    {"name": "Azure", "requirement": "preferred"},
                ],
                "education": [{"value": "Bachelor's degree", "requirement": "preferred"}],
            },
        ))
        stored = repo.get_job("job-a")
        self.assertEqual(["c#", "python", "fastapi"], stored.required_skills)
        self.assertEqual(["azure", "aws"], stored.preferred_skills)
        self.assertEqual(["Bachelor's degree"], stored.education_requirements)


if __name__ == "__main__":
    unittest.main()
