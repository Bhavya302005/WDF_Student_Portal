"""Onboarding answers that loosen or tighten recommendations."""
from __future__ import annotations

import unittest

from matching.config import MatchingSettings
from matching.retrieval import RetrievedJob
from matching.schemas import CandidateProfile, JobIntelligence
from matching.scoring import DeterministicScorer
from matching.taxonomy.normalizer import default_taxonomy
from matching.work_authorization import authorized_for


def job(text: str = "", **fields) -> JobIntelligence:
    values = dict(job_id="j", title="Backend Engineer", normalized_title="backend engineer",
                  cleaned_text=text, source_hash="h", processing_version="t",
                  normalization_version="t", embedding_model="t", embedding_version="t")
    values.update(fields)
    return JobIntelligence(**values)


def profile(**fields) -> CandidateProfile:
    return CandidateProfile(user_id="u", **fields)


class SponsorshipTests(unittest.TestCase):
    NO_SPONSOR = [
        "We are unable to sponsor visas for this role.",
        "Applicants must be authorized to work in the US without sponsorship.",
        "this company will not provide visa sponsorship",
        "we don t offer h1b sponsorship",  # normalized text has no apostrophes
        "Sponsorship is not available for this position.",
        "No visa sponsorship.",
    ]

    def test_postings_that_refuse_sponsorship_are_excluded_for_candidates_who_need_it(self) -> None:
        needs = profile(requires_sponsorship=True)
        for text in self.NO_SPONSOR:
            self.assertFalse(authorized_for(job(text.lower()), needs), text)
        self.assertTrue(authorized_for(job("we sponsor visas and offer relocation"), needs))
        self.assertTrue(authorized_for(job("we do not discriminate on any basis"), needs))
        self.assertTrue(authorized_for(job("export laws without sponsorship for an export license"), needs))

    def test_candidates_who_do_not_need_sponsorship_see_everything(self) -> None:
        for answer in (profile(requires_sponsorship=False), profile(),
                       profile(work_authorization="citizen or permanent resident")):
            self.assertTrue(authorized_for(job("we are unable to sponsor visas"), answer))

    def test_sponsorship_need_is_inferred_from_authorization_status(self) -> None:
        student = profile(work_authorization="temporary or student authorization")
        self.assertFalse(authorized_for(job("must be authorized to work without sponsorship"), student))
        # An explicit answer wins over the inference.
        explicit = profile(work_authorization="temporary or student authorization", requires_sponsorship=False)
        self.assertTrue(authorized_for(job("must be authorized to work without sponsorship"), explicit))

    def test_citizenship_requirements_exclude_only_stated_non_citizens(self) -> None:
        posting = job("u.s. citizenship required. active secret clearance")
        self.assertFalse(authorized_for(posting, profile(work_authorization="authorized without sponsorship")))
        self.assertTrue(authorized_for(posting, profile(work_authorization="citizen or permanent resident")))
        self.assertTrue(authorized_for(posting, profile(work_authorization="prefer not to say")))

    def test_flag_from_full_description_wins_over_truncated_text(self) -> None:
        self.assertFalse(authorized_for(job("", sponsorship_unavailable=True), profile(requires_sponsorship=True)))


class RelocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scorer = DeterministicScorer(default_taxonomy(), MatchingSettings())

    def score(self, willing, **fields):
        candidate = profile(roles=["Backend Engineer"], locations=["Austin"], willing_to_relocate=willing)
        return self.scorer.score(candidate, RetrievedJob(job(**fields), None, 0.0, 0.0))

    def test_relocation_softens_a_location_mismatch(self) -> None:
        elsewhere = dict(location="boston, ma", work_mode="onsite")
        willing, unknown, unwilling = (self.score(w, **elsewhere) for w in (True, None, False))
        self.assertEqual(willing.feature_scores.location, .6)
        self.assertEqual(unknown.feature_scores.location, 0.0)
        self.assertGreater(willing.match_score, unknown.match_score)
        self.assertLessEqual(unwilling.match_score, 59)

    def test_remote_and_matching_jobs_are_unaffected(self) -> None:
        for fields in (dict(location="austin, tx", work_mode="onsite"), dict(location="boston, ma", work_mode="remote")):
            self.assertEqual(self.score(False, **fields).match_score, self.score(None, **fields).match_score)


if __name__ == "__main__":
    unittest.main()
