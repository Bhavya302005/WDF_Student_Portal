from __future__ import annotations

from typing import Protocol

from .schemas import CandidateProfile, JobIntelligence, Recommendation


class ExplanationProvider(Protocol):
    model_name: str
    model_version: str
    prompt_version: str

    def explain(self, profile: CandidateProfile, job: JobIntelligence,
                evidence: Recommendation) -> str: ...


class DeterministicExplanationProvider:
    """Always-available fallback. It only renders already validated evidence."""
    model_name = "template"
    model_version = "template-v1"
    prompt_version = "explanation-v1"

    def explain(self, profile: CandidateProfile, job: JobIntelligence,
                evidence: Recommendation) -> str:
        return evidence.why_match
