from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProcessingStatus(str, Enum):
    processing = "PROCESSING"
    ready = "READY"
    failed = "FAILED"


class SkillMatchType(str, Enum):
    exact = "EXACT"
    alias = "ALIAS"
    close = "CLOSE"
    related = "RELATED"
    missing = "MISSING"


class JobIntelligence(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_id: str
    title: str
    normalized_title: str
    company: str = ""
    cleaned_text: str = ""
    matching_text: str = ""
    role_family: Optional[str] = None
    role_subfamily: Optional[str] = None
    domain: Optional[str] = None
    seniority: Optional[str] = None
    required_skills: List[str] = Field(default_factory=list)
    preferred_skills: List[str] = Field(default_factory=list)
    experience_min_years: Optional[float] = None
    experience_max_years: Optional[float] = None
    education_requirements: List[str] = Field(default_factory=list)
    location: Optional[str] = None
    country: Optional[str] = None
    work_mode: Optional[str] = None
    employment_type: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_period: Optional[str] = None
    posted_at: Optional[datetime] = None
    source_board: Optional[str] = None
    job_url: Optional[str] = None
    apply_url: Optional[str] = None
    # Set from the full description in SQL when known; None means "detect from
    # cleaned_text" (see work_authorization.authorized_for).
    sponsorship_unavailable: Optional[bool] = None
    citizenship_required: Optional[bool] = None
    embedding: List[float] = Field(default_factory=list)
    lexical_terms: List[str] = Field(default_factory=list)
    active: bool = True
    source_hash: str
    artifact_hashes: Dict[str, str] = Field(default_factory=dict)
    processing_version: str
    normalization_version: str
    embedding_model: str
    embedding_version: str
    processing_status: ProcessingStatus = ProcessingStatus.ready
    processed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CandidateProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    user_id: str
    full_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    linkedin: Optional[str] = None
    websites: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    professional_skills: List[str] = Field(default_factory=list)
    project_skills: List[str] = Field(default_factory=list)
    roles: List[str] = Field(default_factory=list)
    professional_roles: List[str] = Field(default_factory=list)
    role_families: List[str] = Field(default_factory=list)
    domains: List[str] = Field(default_factory=list)
    professional_years: Optional[float] = None
    internship_years: Optional[float] = None
    education: List[str] = Field(default_factory=list)
    certifications: List[str] = Field(default_factory=list)
    locations: List[str] = Field(default_factory=list)
    remote_preference: Optional[bool] = None
    work_mode_preferences: List[str] = Field(default_factory=list)
    employment_preferences: List[str] = Field(default_factory=list)
    minimum_salary: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_flexible: bool = True
    work_authorization: Optional[str] = None
    requires_sponsorship: Optional[bool] = None
    willing_to_relocate: Optional[bool] = None
    strict_role_constraint: bool = True
    strict_work_mode_constraint: bool = True
    strict_employment_constraint: bool = True
    strict_experience_constraint: bool = False
    max_job_age_days: Optional[int] = None
    blocked_companies: List[str] = Field(default_factory=list)
    career_level: Optional[str] = None
    hard_location_constraint: bool = False
    embedding: List[float] = Field(default_factory=list)
    profile_hash: str = ""
    embedding_model: str = ""
    embedding_version: str = ""
    processing_version: str = "profile-v1"

    @model_validator(mode="after")
    def include_evidence_scoped_skills(self) -> "CandidateProfile":
        self.skills = list(dict.fromkeys(self.skills + self.professional_skills + self.project_skills))
        return self


class CandidateProfileDraft(BaseModel):
    user_id: str
    form_data: Dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RecommendationRequest(BaseModel):
    user_id: str
    profile: Union[CandidateProfile, Dict[str, Any]]
    limit: int = Field(default=20, ge=1, le=1000)
    roles: List[str] = Field(default_factory=list)
    locations: List[str] = Field(default_factory=list)
    remote_only: bool = False
    work_modes: List[str] = Field(default_factory=list)
    employment_types: List[str] = Field(default_factory=list)


class FeatureScores(BaseModel):
    required_skills: Optional[float] = None
    preferred_skills: Optional[float] = None
    semantic: Optional[float] = None
    role: Optional[float] = None
    experience: Optional[float] = None
    seniority: Optional[float] = None
    location: Optional[float] = None
    education: Optional[float] = None
    preferences: Optional[float] = None
    salary: Optional[float] = None
    freshness: Optional[float] = None


class Recommendation(BaseModel):
    job_id: str
    rank: int = 0
    title: str = ""
    company: str = ""
    location: Optional[str] = None
    country: Optional[str] = None
    work_mode: Optional[str] = None
    employment_type: Optional[str] = None
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_currency: Optional[str] = None
    salary_period: Optional[str] = None
    posted_at: Optional[datetime] = None
    source_board: Optional[str] = None
    job_url: Optional[str] = None
    apply_url: Optional[str] = None
    description_excerpt: str = ""
    match_score: float
    label: str
    matched_required_skills: List[str] = Field(default_factory=list)
    matched_preferred_skills: List[str] = Field(default_factory=list)
    missing_required_skills: List[str] = Field(default_factory=list)
    missing_preferred_skills: List[str] = Field(default_factory=list)
    why_match: str
    evidence_coverage: float
    feature_scores: FeatureScores
    status: str = "recommended"
    is_new: bool = False
    first_recommended_at: Optional[datetime] = None
    last_recommended_at: Optional[datetime] = None
    seen_at: Optional[datetime] = None


class RecommendationStatus(str, Enum):
    recommended = "recommended"
    saved = "saved"
    applied = "applied"
    dismissed = "dismissed"


class UserRecommendation(Recommendation):
    status: RecommendationStatus = RecommendationStatus.recommended
    is_new: bool = True
    first_recommended_at: datetime
    last_recommended_at: datetime
    seen_at: Optional[datetime] = None


class RecommendationHistoryResponse(BaseModel):
    recommendations: List[UserRecommendation]
    unread_count: int = 0


class RecommendationSeenRequest(BaseModel):
    user_id: str
    job_ids: List[str] = Field(default_factory=list)


class RecommendationActionRequest(BaseModel):
    user_id: str
    status: RecommendationStatus


class RecommendationSummary(BaseModel):
    total: int = 0
    unread: int = 0
    saved: int = 0
    applied: int = 0


class RecommendationResponse(BaseModel):
    algorithm_version: str
    profile_hash: str
    recommendations: List[Recommendation]


class QuestionKind(str, Enum):
    text = "text"
    number = "number"
    boolean = "boolean"
    single_select = "single_select"
    multi_select = "multi_select"


class EditableQuestion(BaseModel):
    id: str
    label: str
    help_text: str = ""
    kind: QuestionKind
    required: bool = False
    confirmed: bool = False
    answer: Any = None
    options: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)


class CandidateIntakeResponse(BaseModel):
    user_id: str
    resume_hash: str
    parser_version: str
    profile: CandidateProfile
    questions: List[EditableQuestion]
    evidence: Dict[str, List[str]] = Field(default_factory=dict)


class ResumeTextRequest(BaseModel):
    user_id: str
    resume_text: str = Field(min_length=1, max_length=500_000)


class ResumeRecommendationRequest(BaseModel):
    user_id: str
    resume_text: str = Field(min_length=1, max_length=500_000)
    answers: Dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=20, ge=1, le=1000)


class ResumeRecommendationResponse(BaseModel):
    intake: CandidateIntakeResponse
    recommendations: RecommendationResponse


class IntakeAnswersRequest(BaseModel):
    intake: CandidateIntakeResponse
    answers: Dict[str, Any] = Field(default_factory=dict)
