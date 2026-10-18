from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re

from .config import MatchingSettings
from .hashing import canonical_employment_type, canonical_work_mode, normalize_text
from .retrieval import RetrievedJob
from .schemas import CandidateProfile, FeatureScores, Recommendation
from .taxonomy.normalizer import Taxonomy


@dataclass
class SkillEvidence:
    matched: list[str]
    missing: list[str]
    score: float | None


class DeterministicScorer:
    def __init__(self, taxonomy: Taxonomy, settings: MatchingSettings) -> None:
        self.taxonomy, self.settings = taxonomy, settings

    def score(self, profile: CandidateProfile, retrieved: RetrievedJob) -> Recommendation:
        job = retrieved.job
        required = self._skills(profile.skills, job.required_skills)
        preferred = self._skills(profile.skills, job.preferred_skills)
        role = self._role(profile, job.role_family, job.role_subfamily, job.title)
        experience = self._experience(profile.professional_years, job.experience_min_years, job.experience_max_years)
        seniority = _seniority_fit(_profile_level(profile), job.title)
        location = self._location(profile, job.location, job.country, job.work_mode)
        education = self._education(profile.education, job.education_requirements)
        preferences = self._preference(profile, job.employment_type, job.work_mode)
        salary = self._salary(profile, job)
        freshness = self._freshness(profile, job)
        features = FeatureScores(required_skills=required.score, preferred_skills=preferred.score,
            semantic=retrieved.semantic_score, role=role, experience=experience, seniority=seniority, location=location,
            education=education, preferences=preferences, salary=salary, freshness=freshness)
        values = features.model_dump()
        weights = vars(self.settings.score_weights)
        known_weight = sum(weights[key] for key, value in values.items() if value is not None)
        weighted = sum(weights[key] * value for key, value in values.items() if value is not None)
        score = weighted / known_weight if known_weight else 0.0
        coverage = known_weight / sum(weights.values())
        # Thin evidence (e.g. a title-only posting) shrinks toward neutral, so it
        # can rank and be shown but never outranks a well-evidenced equal match.
        # Semantic similarity is excluded: it is computed from the same text and
        # is present for every embedded job, so it is not independent evidence.
        structured = {k: w for k, w in weights.items() if k != "semantic"}
        structured_known = sum(w for k, w in structured.items() if values[k] is not None)
        confidence = min(1.0, structured_known / sum(structured.values()) / self.settings.full_evidence_coverage)
        score = .5 + (score - .5) * confidence
        if required.score is not None and required.score < .5:
            score = min(score, .59)
        if role is not None and role < .75:
            score = min(score, .69)
        if experience is not None and experience < .5:
            score = min(score, .59)
        if seniority is not None and seniority < .5:
            score = min(score, .69)
        if location == 0.0 and _unwilling_to_move(profile, job):
            score = min(score, .59)
        label = "High" if score >= .75 else "Medium" if score >= .50 else "Low"
        why = explanation(required, preferred, role, experience, location)
        return Recommendation(job_id=job.job_id, title=job.title, company=job.company,
            location=job.location, country=job.country, work_mode=job.work_mode,
            employment_type=job.employment_type, salary_min=job.salary_min, salary_max=job.salary_max,
            salary_currency=job.salary_currency, salary_period=job.salary_period, posted_at=job.posted_at,
            source_board=job.source_board, job_url=job.job_url, apply_url=job.apply_url,
            description_excerpt=job.cleaned_text[:500], match_score=round(score * 100, 2), label=label,
            matched_required_skills=required.matched, matched_preferred_skills=preferred.matched,
            missing_required_skills=required.missing, missing_preferred_skills=preferred.missing,
            why_match=why, evidence_coverage=round(coverage, 4), feature_scores=features)

    def _skills(self, candidate: list[str], wanted: list[str]) -> SkillEvidence:
        if not wanted: return SkillEvidence([], [], None)
        matched, missing, total = [], [], 0.0
        for skill in wanted:
            best = max(({"EXACT": 1.0, "RELATED": .55, "MISSING": 0.0}[self.taxonomy.relation(have, skill)] for have in candidate), default=0.0)
            (matched if best else missing).append(skill)
            total += best
        return SkillEvidence(matched, missing, total / len(wanted))

    def _role(self, profile: CandidateProfile, family: str | None, subfamily: str | None,
              title: str = "") -> float | None:
        if not family and not subfamily:
            # An unclassified title is not neutral evidence: without this, any
            # job outside the taxonomy escaped the role penalty entirely.
            similarity = _title_similarity(profile.roles + profile.professional_roles, title)
            return None if similarity is None else .9 * similarity
        base = self._subfamily_role(profile, family, subfamily)
        # A subfamily is coarse ("Procurement" holds both Specialist and Manager),
        # so closeness to the candidate's stated target title refines it. The
        # processor appends inferred subfamily names to roles; those are
        # categories, not titles, and would make every title in them "exact".
        subfamilies = {normalize_text(r["subfamily"]) for r in self.taxonomy.roles}
        stated = [r for r in profile.roles if normalize_text(r) not in subfamilies]
        closeness = _title_similarity(stated or profile.roles, title)
        return base if closeness is None else base * (.5 + .5 * closeness)

    def _subfamily_role(self, profile: CandidateProfile, family: str | None, subfamily: str | None) -> float:
        target = self._canonical_roles(profile.roles)
        history = set(self._canonical_roles(profile.professional_roles))
        role = normalize_text(subfamily)
        target_score = _ranked(role, target)
        if target_score is not None and role in history: return target_score
        if target_score is not None: return .65 * target_score
        software_roles = {"software engineering", "backend engineering", "frontend engineering", "full stack engineering"}
        if role in software_roles and "software engineering" in target:
            return .85 if "software engineering" in history else .65
        if role in history: return .8
        if normalize_text(family) in {normalize_text(x) for x in profile.role_families}: return .55
        return 0.0

    def _canonical_roles(self, values: list[str]) -> list[str]:
        result = []
        for value in values:
            _, canonical = self.taxonomy.classify_role(value)
            normalized = normalize_text(canonical or value)
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _experience(years: float | None, low: float | None, high: float | None) -> float | None:
        if years is None or (low is None and high is None): return None
        if low is not None and years < low: return max(0.0, 1.0 - (low - years) / max(low, 1.0))
        if high is not None and years > high + 3: return max(.5, 1.0 - (years - high) / 10.0)
        return 1.0

    @staticmethod
    def _location(profile: CandidateProfile, location: str | None, country: str | None, work_mode: str | None) -> float | None:
        preferred_modes = {canonical_work_mode(value) for value in profile.work_mode_preferences if normalize_text(value) != "no preference"}
        if preferred_modes and canonical_work_mode(work_mode) in preferred_modes:
            return _ranked(canonical_work_mode(work_mode), [canonical_work_mode(x) for x in profile.work_mode_preferences])
        if profile.remote_preference and canonical_work_mode(work_mode) == "remote": return 1.0
        if not profile.locations or (not location and not country):
            return 0.0 if preferred_modes and work_mode else None
        wanted = [normalize_text(x) for x in profile.locations]
        actual = normalize_text(f"{location or ''} {country or ''}")
        matches = [index for index, value in enumerate(wanted) if value in actual]
        if matches:
            return _rank_value(min(matches), len(wanted))
        if canonical_work_mode(work_mode) == "remote":
            return .4
        # Elsewhere and on site: fine for someone willing to relocate.
        return .6 if profile.willing_to_relocate else 0.0

    @staticmethod
    def _education(candidate: list[str], required: list[str]) -> float | None:
        if not required: return None
        if not candidate: return None
        text = normalize_text(" ".join(candidate))
        return 1.0 if any(normalize_text(x) in text for x in required) else 0.0

    @staticmethod
    def _preference(profile: CandidateProfile, employment: str | None, work_mode: str | None) -> float | None:
        values = []
        if profile.employment_preferences and employment:
            score = _ranked(canonical_employment_type(employment), [canonical_employment_type(x) for x in profile.employment_preferences])
            values.append(score or 0.0)
        if profile.work_mode_preferences and work_mode:
            score = _ranked(canonical_work_mode(work_mode), [canonical_work_mode(x) for x in profile.work_mode_preferences])
            values.append(score or 0.0)
        return sum(values) / len(values) if values else None

    @staticmethod
    def _salary(profile: CandidateProfile, job) -> float | None:
        if profile.minimum_salary is None:
            return None
        if profile.salary_currency and normalize_text(job.salary_currency) != normalize_text(profile.salary_currency):
            return None
        from .retrieval import _annual_salary
        value = _annual_salary(job.salary_max if job.salary_max is not None else job.salary_min, job.salary_period)
        if value is None:
            return None
        if value >= profile.minimum_salary:
            return 1.0
        return max(0.0, value / max(profile.minimum_salary, 1.0)) if profile.salary_flexible else 0.0

    @staticmethod
    def _freshness(profile: CandidateProfile, job) -> float | None:
        if not profile.max_job_age_days or not job.posted_at:
            return None
        posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=timezone.utc)
        age = max(0, (datetime.now(timezone.utc) - posted).days)
        return max(0.0, 1.0 - age / profile.max_job_age_days)


def explanation(required: SkillEvidence, preferred: SkillEvidence, role: float | None,
                experience: float | None, location: float | None) -> str:
    parts: list[str] = []
    if required.matched: parts.append(f"Matches required skills: {', '.join(required.matched[:6])}.")
    if required.missing: parts.append(f"Missing required skills: {', '.join(required.missing[:6])}.")
    if preferred.matched: parts.append(f"Matches preferred skills: {', '.join(preferred.matched[:4])}.")
    if experience == 1: parts.append("Professional experience fits the stated range.")
    if role == 1: parts.append("The target role is an exact role match.")
    if location == 1: parts.append("Location or work-mode preference matches.")
    return " ".join(parts) or "The available evidence indicates an adjacent match; review the job details."


# Career level on one scale for titles and candidates: 0 intern/early career,
# 1 junior, 2 mid, 3 senior, 4 lead/staff, 5 director/head, 6 executive.
# A cohort year in the title ("LDN SEE 2027 - Software Engineering") marks a
# campus or graduate programme, not an experienced hire.
_EARLY = (r"\b(?:intern|internship|co op|apprentice|stagiaire|werkstudent|new grad|graduate program|"
          r"early careers?|campus|insight|summer (?:analyst|associate)|20[2-3]\d)\b")
_EXECUTIVE = r"\b(?:vp|vice president|chief|cpo|cfo|cto|coo|ceo|svp|evp)\b"
_DIRECTOR = r"\b(?:director|head of|head)\b"
_LEAD = r"\b(?:principal|staff|lead)\b"
_SENIOR = r"\b(?:senior|sr)\b"
_JUNIOR = (r"\b(?:junior|jr|entry level|entry|assistant|associate|graduate|trainee|"
           r"sales development representative|business development representative|sdr|bdr)\b")
_ROLE_STOPWORDS = {"senior", "sr", "junior", "jr", "lead", "staff", "principal", "head", "of", "the",
                   "and", "i", "ii", "iii", "iv", "associate", "assistant", "global", "remote"}


def _title_level(title: str) -> float:
    text = normalize_text(title).replace("-", " ")
    has = lambda pattern: re.search(pattern, text) is not None
    if has(_EARLY): return 0
    if has(_EXECUTIVE): return 6
    if has(_DIRECTOR): return 5
    if has(r"\bmanager\b"):
        # Management is its own track: a manager sits between senior and lead,
        # "Senior Manager" above lead, "Assistant Manager" near junior/mid.
        return 4.5 if has(_SENIOR) or has(_LEAD) else 2 if has(_JUNIOR) else 3.5
    if has(_LEAD): return 4
    if has(_SENIOR): return 3
    if has(_JUNIOR): return 1
    return 2  # no level word: postings without one are overwhelmingly mid-level


def _candidate_level(years: float) -> int:
    return 0 if years < .5 else 1 if years < 2 else 2 if years < 5 else 3 if years < 8 else 4 if years < 12 else 5


# Target career level answers (resume_questions.json and the onboarding chips)
# on the _title_level scale.
_CAREER_LEVELS = {"intern": 0, "entry-level": .5, "entry level": .5, "entry": .5, "junior": 1,
                  "mid": 2, "senior": 3, "lead": 4, "staff": 4, "principal": 4, "director": 5}


def _profile_level(profile: CandidateProfile) -> float | None:
    """The stated target level wins; otherwise infer it from professional years."""
    stated = _CAREER_LEVELS.get(normalize_text(profile.career_level))
    if stated is not None:
        return stated
    return None if profile.professional_years is None else _candidate_level(profile.professional_years)


def _unwilling_to_move(profile: CandidateProfile, job) -> bool:
    """On-site work outside every preferred location, for someone who said no to relocating."""
    return (profile.willing_to_relocate is False and bool(profile.locations)
            and bool(job.location or job.country) and canonical_work_mode(job.work_mode) != "remote")


def _seniority_fit(level: float | None, title: str) -> float | None:
    """1.0 at the candidate's level, 0.75 one level apart, 0.3 two, 0 beyond."""
    if level is None:
        return None
    gap = abs(_title_level(title) - level)
    return 1.0 if gap <= .5 else .75 if gap <= 1 else .3 if gap <= 2 else 0.0


def _stem(word: str) -> str:
    for suffix in ("ing", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            return word[:-len(suffix)]
    return word


def _role_words(text: str) -> set[str]:
    return {_stem(w) for w in re.findall(r"[\w+#]+", normalize_text(text))} - _ROLE_STOPWORDS


def _title_similarity(roles: list[str], title: str) -> float | None:
    """Best share of a target role's distinctive words found in the job title."""
    title_words = _role_words(title)
    best = None
    for role in roles:
        words = _role_words(role)
        if words:
            share = len(words & title_words) / len(words)
            best = share if best is None else max(best, share)
    return best


def _ranked(value: str, preferences: list[str]) -> float | None:
    try:
        return _rank_value(preferences.index(value), len(preferences))
    except ValueError:
        return None


def _rank_value(index: int, count: int) -> float:
    if count <= 1:
        return 1.0
    return max(.6, 1.0 - (.4 * index / (count - 1)))
