from __future__ import annotations

import unittest

from matching.config import MatchingSettings
from matching.factory import local_stack
from matching.retrieval import _role_query_terms, _source_filters, _source_job_intelligence
from matching.schemas import CandidateProfile, FeatureScores, Recommendation, RecommendationRequest
from matching.scoring import _candidate_level, _profile_level, _seniority_fit, _title_level
from matching.service import _collapse_duplicates, _diversify
from matching.taxonomy import Taxonomy


def job(job_id, title, company="Acme", **overrides):
    value = {
        "id": job_id, "job_title": title, "company": company,
        "description": "Own supplier negotiations and contracts in SAP and Excel.",
        "skills_required": ["SAP", "Negotiation", "Excel"], "location": "Austin, TX",
        "work_mode": "onsite", "job_type": "full time", "status": "active",
    }
    value.update(overrides)
    return value


def recommend(jobs, profile, **request):
    _, processor, service = local_stack(MatchingSettings())
    for item in jobs:
        processor.process(item)
    response = service.recommend(RecommendationRequest(user_id="u", profile=profile, limit=10, **request))
    return response.recommendations


PROCUREMENT_MANAGER = {"roles": ["Procurement Manager"], "professional_roles": ["Procurement Manager"],
                       "skills": ["SAP", "Negotiation", "Excel"], "professional_years": 8}


def rec(job_id, title, company, score):
    return Recommendation(job_id=job_id, title=title, company=company, match_score=score, label="High",
                          why_match="", evidence_coverage=1.0, feature_scores=FeatureScores())


class SeniorityTests(unittest.TestCase):
    def test_title_levels(self) -> None:
        self.assertEqual(_title_level("Software Engineering Intern"), 0)
        self.assertEqual(_title_level("LDN SEE 2027 - Software Engineering"), 0)
        self.assertEqual(_title_level("Junior Buyer"), 1)
        self.assertEqual(_title_level("Buyer"), 2)  # no level word: mid
        self.assertEqual(_title_level("Senior Buyer"), 3)
        self.assertEqual(_title_level("Procurement Manager"), 3.5)
        self.assertEqual(_title_level("Assistant Procurement Manager"), 2)
        self.assertEqual(_title_level("Senior Procurement Manager"), 4.5)
        self.assertEqual(_title_level("Director, Strategic Sourcing"), 5)
        self.assertEqual(_title_level("VP Procurement"), 6)

    def test_fit_degrades_with_level_gap(self) -> None:
        self.assertEqual(_seniority_fit(_candidate_level(8), "Procurement Manager"), 1.0)
        self.assertEqual(_seniority_fit(_candidate_level(8), "Senior Buyer"), .75)
        self.assertEqual(_seniority_fit(_candidate_level(1), "Senior Buyer"), .3)
        self.assertEqual(_seniority_fit(_candidate_level(1), "Director of Procurement"), 0.0)
        self.assertIsNone(_seniority_fit(None, "Senior Buyer"))

    def test_stated_career_level_overrides_years(self) -> None:
        from matching.schemas import CandidateProfile
        self.assertEqual(_profile_level(CandidateProfile(user_id="u", professional_years=1, career_level="senior")), 3)
        self.assertEqual(_profile_level(CandidateProfile(user_id="u", professional_years=1)), 1)
        self.assertIsNone(_profile_level(CandidateProfile(user_id="u")))


class RankingTests(unittest.TestCase):
    def test_exact_title_outranks_same_family_different_function(self) -> None:
        results = recommend([job("specialist", "Procurement Specialist"), job("manager", "Procurement Manager")],
                            PROCUREMENT_MANAGER)
        self.assertEqual([r.job_id for r in results], ["manager", "specialist"])

    def test_unclassified_role_is_penalised_not_ignored(self) -> None:
        # "Staff Electrical Engineer" used to escape the role penalty when the
        # taxonomy could not classify it, ranking first for a backend engineer.
        unknown = job("pastry", "Pastry Chef", skills_required=["Python"])
        backend = job("backend", "Backend Engineer", skills_required=["Python"])
        results = recommend([unknown, backend], {"roles": ["Backend Engineer"], "skills": ["Python"],
                                                 "professional_years": 4})
        self.assertEqual(results[0].job_id, "backend")
        self.assertLess(results[-1].feature_scores.role, .5)

    def test_title_only_posting_cannot_be_high(self) -> None:
        bare = job("bare", "Procurement Manager", description="", skills_required=[], location=None,
                   work_mode=None, job_type=None)
        result = recommend([bare], PROCUREMENT_MANAGER)[0]
        self.assertNotEqual(result.label, "High")
        self.assertLess(result.evidence_coverage, .6)

    def test_seniority_gap_caps_below_high(self) -> None:
        result = recommend([job("dir", "Director of Procurement")],
                           {**PROCUREMENT_MANAGER, "professional_years": 1})[0]
        self.assertLess(result.match_score, 75)


class ResultPageTests(unittest.TestCase):
    def test_cross_board_duplicates_collapse_to_best_copy(self) -> None:
        ranked = [rec("li", "Buyer", "Acme", 90), rec("indeed", "buyer", "ACME", 80), rec("other", "Buyer", "Beta", 70)]
        self.assertEqual([r.job_id for r in _collapse_duplicates(ranked)], ["li", "other"])
        reordered = [rec("a", "Backend Engineer (Python/Go)", "Acme", 90), rec("b", "Backend Engineer (Go/Python)", "Acme", 80)]
        self.assertEqual([r.job_id for r in _collapse_duplicates(reordered)], ["a"])

    def test_company_cap_is_soft(self) -> None:
        ranked = [rec(f"a{i}", f"Role {i}", "Acme", 99 - i) for i in range(5)] + [rec("b", "Role", "Beta", 50)]
        page = _diversify(ranked, 4, 3)
        self.assertEqual([r.job_id for r in page], ["a0", "a1", "a2", "b"])
        # With nothing else eligible, capped results still fill the page.
        self.assertEqual(len(_diversify(ranked[:5], 5, 3)), 5)


class SourceFallbackTests(unittest.TestCase):
    def test_role_terms_use_taxonomy_aliases_and_drop_generic_words(self) -> None:
        phrases, tokens = _role_query_terms(["Technical Recruiter"], Taxonomy())
        self.assertIn("%talent acquisition%", phrases)
        self.assertNotIn("%technical%", tokens)  # matched "Technical Director" before

    def test_sql_prefilters_mirror_hard_rules(self) -> None:
        profile = CandidateProfile(user_id="u", blocked_companies=["Acme"], strict_experience_constraint=True,
                                   professional_years=3, max_job_age_days=30)
        request = RecommendationRequest(user_id="u", profile={}, remote_only=True, employment_types=["Full-time"])
        conditions, params = _source_filters(profile, request)
        self.assertEqual(params["work_modes"], ["remote"])
        self.assertEqual(params["employment"], ["full time"])
        self.assertEqual(params["blocked"], ["acme"])
        self.assertEqual(params["max_experience"], 3)
        self.assertEqual(len(conditions), 6)

    def test_missing_stored_skills_fall_back_to_description(self) -> None:
        intelligence = _source_job_intelligence(
            {"id": "x", "job_title": "Buyer", "description": "Daily work in SAP and Excel.", "skills_required": []},
            Taxonomy())
        self.assertIn("sap", intelligence.required_skills)
        self.assertIn("excel", intelligence.required_skills)


if __name__ == "__main__":
    unittest.main()
