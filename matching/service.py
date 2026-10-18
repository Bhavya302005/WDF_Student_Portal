from __future__ import annotations

import re

from .config import MatchingSettings
from .hashing import normalize_text, stable_hash
from .profile import CandidateProcessor
from .repository import MatchingRepository
from .retrieval import HybridRetriever
from .schemas import Recommendation, RecommendationRequest, RecommendationResponse
from .scoring import DeterministicScorer


class RecommendationService:
    def __init__(self, repository: MatchingRepository, profiles: CandidateProcessor,
                 retriever: HybridRetriever, scorer: DeterministicScorer,
                 settings: MatchingSettings) -> None:
        self.repository, self.profiles, self.retriever, self.scorer, self.settings = repository, profiles, retriever, scorer, settings

    def recommend(self, request: RecommendationRequest) -> RecommendationResponse:
        profile = self.profiles.process(request.user_id, request.profile)
        version_provider = getattr(self.retriever, "corpus_version", None)
        corpus_version = version_provider() if version_provider else getattr(self.repository, "corpus_version", lambda: "0")()
        query_hash = stable_hash([profile.profile_hash, sorted(request.roles), sorted(request.locations),
                                  request.remote_only, sorted(request.work_modes), sorted(request.employment_types), request.limit,
                                  corpus_version])
        cached = self.repository.get_matches(query_hash, self.settings.algorithm_version)
        if cached is None:
            with self.repository.job_lock("request:" + query_hash):
                cached = self.repository.get_matches(query_hash, self.settings.algorithm_version)
                if cached is None:
                    retrieved = self.retriever.retrieve(profile, request)
                    scored = [self.scorer.score(profile, item) for item in retrieved[:self.settings.scoring_limit]]
                    ranked = _collapse_duplicates(sorted(scored, key=lambda x: x.match_score, reverse=True))
                    cached = _diversify(ranked, request.limit, self.settings.per_company_cap)
                    for rank, item in enumerate(cached, 1): item.rank = rank
                    self.repository.save_matches(query_hash, self.settings.algorithm_version, cached)
        return RecommendationResponse(algorithm_version=self.settings.algorithm_version,
                                      profile_hash=profile.profile_hash, recommendations=cached)


def _collapse_duplicates(ranked: list[Recommendation]) -> list[Recommendation]:
    """Keep the best-scored copy of a posting listed on several boards.

    The same opening appears on LinkedIn, Indeed and the company ATS under
    different ids; showing all of them wastes the user's result slots.
    """
    # Location is deliberately not part of the key: one opening advertised for
    # several cities is a single choice for the candidate, not several.
    seen: set[tuple[frozenset[str], str]] = set()
    unique = []
    for item in ranked:
        # Word set, not string: "(Python/Go)" and "(Go/Python)" are one posting.
        key = (frozenset(re.findall(r"[\w+#]+", normalize_text(item.title))), normalize_text(item.company))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _diversify(ranked: list[Recommendation], limit: int, per_company: int) -> list[Recommendation]:
    """Cap one company's share of the page; overflow only backfills empty slots."""
    if per_company <= 0:
        return ranked[:limit]
    counts: dict[str, int] = {}
    chosen, overflow = [], []
    for item in ranked:
        company = normalize_text(item.company)
        if counts.get(company, 0) < per_company:
            counts[company] = counts.get(company, 0) + 1
            chosen.append(item)
        else:
            overflow.append(item)
        if len(chosen) == limit:
            return chosen
    return (chosen + overflow)[:limit]
