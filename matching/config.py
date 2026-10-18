from __future__ import annotations

from dataclasses import dataclass, field
import os


@dataclass(frozen=True)
class ScoreWeights:
    required_skills: float = 0.25
    preferred_skills: float = 0.10
    semantic: float = 0.12
    role: float = 0.20
    experience: float = 0.10
    seniority: float = 0.06
    location: float = 0.07
    education: float = 0.04
    preferences: float = 0.03
    salary: float = 0.02
    freshness: float = 0.01

    def __post_init__(self) -> None:
        if abs(sum(vars(self).values()) - 1.0) > 1e-9:
            raise ValueError("score weights must sum to 1")


@dataclass(frozen=True)
class MatchingSettings:
    processing_version: str = "job-v2"
    normalization_version: str = "normalize-v1"
    embedding_version: str = "hashing-v1"
    algorithm_version: str = "eligibility-hybrid-score-v3"
    prompt_version: str = "explanation-v1"
    semantic_limit: int = 700
    lexical_limit: int = 700
    retrieval_limit: int = 1000
    scoring_limit: int = 1000
    rerank_limit: int = 50
    default_limit: int = 20
    rrf_k: int = 60
    semantic_weight: float = 1.0
    lexical_weight: float = 1.0
    embedding_dimensions: int = 384
    pool_size: int = 8
    # At most this many recommendations from one company in a result page
    # (soft: overflow fills remaining slots only when nothing else is eligible).
    per_company_cap: int = 3
    # Share of total feature weight that counts as complete evidence; below it,
    # scores are shrunk toward 0.5 in proportion to the missing evidence.
    full_evidence_coverage: float = 0.6
    # Raw BGE cosine similarity is mapped linearly from [floor, ceiling] to
    # [0, 1]: unrelated job text sits near the floor, near-duplicates at the top.
    semantic_floor: float = 0.55
    semantic_ceiling: float = 0.85
    score_weights: ScoreWeights = field(default_factory=ScoreWeights)

    @classmethod
    def from_env(cls) -> "MatchingSettings":
        integer = lambda name, default: int(os.getenv(name, str(default)))
        floating = lambda name, default: float(os.getenv(name, str(default)))
        return cls(
            processing_version=os.getenv("MATCHING_PROCESSING_VERSION", "job-v2"),
            normalization_version=os.getenv("MATCHING_NORMALIZATION_VERSION", "normalize-v1"),
            embedding_version=os.getenv("MATCHING_EMBEDDING_VERSION", "hashing-v1"),
            algorithm_version=os.getenv("MATCHING_ALGORITHM_VERSION", "eligibility-hybrid-score-v3"),
            prompt_version=os.getenv("MATCHING_PROMPT_VERSION", "explanation-v1"),
            semantic_limit=integer("MATCHING_SEMANTIC_LIMIT", 700),
            lexical_limit=integer("MATCHING_LEXICAL_LIMIT", 700),
            retrieval_limit=integer("MATCHING_RETRIEVAL_LIMIT", 1000),
            scoring_limit=integer("MATCHING_SCORING_LIMIT", 1000),
            rerank_limit=integer("MATCHING_RERANK_LIMIT", 50),
            default_limit=integer("MATCHING_DEFAULT_LIMIT", 20),
            rrf_k=integer("MATCHING_RRF_K", 60),
            semantic_weight=floating("MATCHING_SEMANTIC_WEIGHT", 1.0),
            lexical_weight=floating("MATCHING_LEXICAL_WEIGHT", 1.0),
            embedding_dimensions=integer("MATCHING_EMBEDDING_DIMENSIONS", 384),
            pool_size=integer("MATCHING_POOL_SIZE", 8),
            per_company_cap=integer("MATCHING_PER_COMPANY_CAP", 3),
            semantic_floor=floating("MATCHING_SEMANTIC_FLOOR", 0.55),
            semantic_ceiling=floating("MATCHING_SEMANTIC_CEILING", 0.85),
        )
