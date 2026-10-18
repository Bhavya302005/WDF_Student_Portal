from __future__ import annotations

from typing import Protocol

from .schemas import CandidateProfile, Recommendation


class Reranker(Protocol):
    version: str
    def rerank(self, profile: CandidateProfile, recommendations: list[Recommendation], limit: int) -> list[Recommendation]: ...


class NoOpReranker:
    version = "none-v1"
    def rerank(self, profile: CandidateProfile, recommendations: list[Recommendation], limit: int) -> list[Recommendation]:
        return recommendations[:limit]
