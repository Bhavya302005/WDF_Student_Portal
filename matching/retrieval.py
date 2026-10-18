from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
import threading
import time
from typing import Any, Iterable, Optional

from .config import MatchingSettings
from .embeddings import cosine
from .hashing import (canonical_employment_type, canonical_work_mode,
                      normalize_text, stable_hash)
from .ingestion import (_compensation, _datetime, _experience,
                        _extracted_education, _extracted_skills, _seniority,
                        _strings)
from .repository import MatchingRepository
from .schemas import CandidateProfile, JobIntelligence, RecommendationRequest
from .work_authorization import CITIZENSHIP_PATTERN, NO_SPONSORSHIP_PATTERN, authorized_for

log = logging.getLogger("matching.retrieval")


@dataclass(frozen=True)
class RetrievedJob:
    job: JobIntelligence
    # None when no embedding exists (source-table fallback): the scorer then
    # excludes semantic evidence instead of trusting a fabricated value.
    semantic_score: Optional[float]
    lexical_score: float
    rrf_score: float


class HybridRetriever:
    """Reference adapter. Production retrieval is implemented by matching/sql/schema.sql RPCs."""

    def __init__(self, repository: MatchingRepository, settings: MatchingSettings) -> None:
        self.repository, self.settings = repository, settings

    def retrieve(self, profile: CandidateProfile, request: RecommendationRequest) -> list[RetrievedJob]:
        jobs = [job for job in self.repository.ready_jobs() if self._eligible(job, profile, request)]
        query_terms = set(_terms(" ".join(profile.skills + profile.roles + profile.role_families + profile.domains)))
        semantic = sorted(((job, cosine(profile.embedding, job.embedding)) for job in jobs), key=lambda x: x[1], reverse=True)[:self.settings.semantic_limit]
        lexical = sorted(((job, self._lexical(query_terms, job)) for job in jobs), key=lambda x: x[1], reverse=True)[:self.settings.lexical_limit]
        semantic_ranks = {job.job_id: rank for rank, (job, _) in enumerate(semantic, 1)}
        lexical_ranks = {job.job_id: rank for rank, (job, _) in enumerate(lexical, 1)}
        values = {job.job_id: job for job, _ in semantic + lexical}
        sem_scores = {job.job_id: score for job, score in semantic}
        lex_scores = {job.job_id: score for job, score in lexical}
        fused = []
        for job_id, job in values.items():
            score = 0.0
            if job_id in semantic_ranks:
                score += self.settings.semantic_weight / (self.settings.rrf_k + semantic_ranks[job_id])
            if job_id in lexical_ranks:
                score += self.settings.lexical_weight / (self.settings.rrf_k + lexical_ranks[job_id])
            fused.append(RetrievedJob(job, sem_scores.get(job_id, 0.0), lex_scores.get(job_id, 0.0), score))
        return sorted(fused, key=lambda x: x.rrf_score, reverse=True)[:self.settings.retrieval_limit]

    def _eligible(self, job: JobIntelligence, profile: CandidateProfile, request: RecommendationRequest) -> bool:
        if normalize_text(job.company) in {normalize_text(x) for x in profile.blocked_companies}: return False
        work_modes = request.work_modes or (["remote"] if request.remote_only else [])
        if work_modes and canonical_work_mode(job.work_mode) not in {canonical_work_mode(x) for x in work_modes}: return False
        if request.employment_types and canonical_employment_type(job.employment_type) not in {canonical_employment_type(x) for x in request.employment_types}: return False
        if request.roles:
            wanted = {normalize_text(x) for x in request.roles}
            from .taxonomy.normalizer import default_taxonomy
            taxonomy = default_taxonomy()
            for role in request.roles:
                _, canonical = taxonomy.classify_role(role)
                if canonical:
                    wanted.add(normalize_text(canonical))
            if normalize_text(job.role_subfamily) not in wanted and normalize_text(job.normalized_title) not in wanted: return False
        locations = request.locations or (profile.locations if profile.hard_location_constraint else [])
        if locations and not any(normalize_text(x) in normalize_text(job.location) for x in locations) and normalize_text(job.work_mode) != "remote": return False
        if (profile.strict_experience_constraint and profile.professional_years is not None
                and job.experience_min_years is not None and profile.professional_years < job.experience_min_years):
            return False
        if profile.minimum_salary is not None and not profile.salary_flexible:
            if not _salary_meets(job, profile.minimum_salary, profile.salary_currency): return False
        if profile.max_job_age_days and job.posted_at:
            posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - posted).days > profile.max_job_age_days: return False
        return authorized_for(job, profile)

    @staticmethod
    def _lexical(query_terms: set[str], job: JobIntelligence) -> float:
        if not query_terms: return 0.0
        job_terms = set(job.lexical_terms)
        return len(query_terms & job_terms) / len(query_terms)


def reciprocal_rank_fusion(rankings: Iterable[list[str]], *, k: int = 60, weights: Iterable[float] | None = None) -> list[tuple[str, float]]:
    rankings = list(rankings)
    weights = list(weights or [1.0] * len(rankings))
    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        for rank, item in enumerate(ranking, 1):
            scores[item] = scores.get(item, 0.0) + weight / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def _terms(text: str) -> list[str]:
    return re.findall(r"[\w+#.]+", normalize_text(text))


class PostgresHybridRetriever:
    """Production retriever: database filtering + ANN/GIN retrieval, then bounded RRF."""

    # Cheap global state is cached briefly instead of costing a round trip per request.
    STATE_TTL_SECONDS = 30.0

    def __init__(self, connection_string: str, settings: MatchingSettings,
                 vector_connection_string: str | None = None) -> None:
        self.connection_string, self.settings = connection_string, settings
        # Optional semantic index in its own database (matching/sql/vector_store.sql).
        self.vector_connection_string = vector_connection_string
        self._pools: dict[str, Any] = {}
        self._pool_lock = threading.Lock()
        self._state: dict[str, tuple[float, Any]] = {}

    def _connection(self, database: str = "jobs"):
        """Pooled connection: opening a TLS session to Neon costs ~1.5 s per request."""
        if database not in self._pools:
            with self._pool_lock:
                if database not in self._pools:
                    try:
                        from psycopg.rows import dict_row
                        from psycopg_pool import ConnectionPool
                    except ImportError as exc:  # pragma: no cover - deployment dependency
                        raise RuntimeError("PostgreSQL retrieval requires psycopg and psycopg-pool") from exc
                    url = self.vector_connection_string if database == "vectors" else self.connection_string
                    self._pools[database] = ConnectionPool(
                        url, min_size=1, max_size=self.settings.pool_size,
                        kwargs={"row_factory": dict_row, "autocommit": True}, configure=_register_vector,
                        check=ConnectionPool.check_connection, open=True)
        return self._pools[database].connection()

    def close(self) -> None:
        """Close pooled connections (server shutdown, tests)."""
        with self._pool_lock:
            pools, self._pools = self._pools, {}
        for pool in pools.values():
            pool.close()

    def _vector_model(self) -> str | None:
        """Embedding model of the semantic index, or None when absent/unreachable."""
        if not self.vector_connection_string:
            return None
        def load() -> str | None:
            try:
                with self._connection("vectors") as conn:
                    row = conn.execute("select embedding_model from job_vectors where active limit 1").fetchone()
                return row["embedding_model"] if row else None
            except Exception as exc:  # the jobs-only path must keep working
                log.warning("semantic index unavailable: %s", exc)
                return None
        return self._cached("vector_model", load)

    def _cached(self, key: str, load):
        now = time.monotonic()
        hit = self._state.get(key)
        if hit and hit[0] > now:
            return hit[1]
        value = load()
        self._state[key] = (now + self.STATE_TTL_SECONDS, value)
        return value

    def _has_intelligence(self) -> bool:
        def load() -> bool:
            with self._connection() as conn:
                # A database without the optional schema.sql still gets the fallback.
                if conn.execute("select to_regclass('public.job_intelligence') as t").fetchone()["t"] is None:
                    return False
                row = conn.execute("select exists(select 1 from job_intelligence "
                                   "where active and processing_status='READY') as ready").fetchone()
            return bool(row["ready"])
        return self._cached("has_intelligence", load)

    def retrieve(self, profile: CandidateProfile, request: RecommendationRequest) -> list[RetrievedJob]:
        # Vectors from different models are not comparable: use the index only
        # when the profile was embedded by the same model as the jobs.
        if profile.embedding and self._vector_model() == profile.embedding_model:
            try:
                return self._retrieve_with_vectors(profile, request)
            except Exception as exc:
                log.warning("semantic retrieval failed, using title retrieval: %s", exc)
        if not self._has_intelligence():
            with self._connection() as conn:
                return self._retrieve_from_source_jobs(conn, profile, request)
        # Only explicit request roles are hard filters. Candidate profile roles
        # remain ranking evidence unless the caller deliberately promotes them.
        roles = list(request.roles)
        if roles:
            from .taxonomy.normalizer import default_taxonomy
            taxonomy = default_taxonomy()
            for role in roles.copy():
                _, canonical = taxonomy.classify_role(role)
                if canonical and canonical not in roles:
                    roles.append(canonical)
        roles = roles or None
        locations = request.locations or (profile.locations if profile.hard_location_constraint else None)
        work_modes = ([canonical_work_mode(value) for value in request.work_modes]
                      if request.work_modes else (["remote"] if request.remote_only else None))
        employment = ([canonical_employment_type(value) for value in request.employment_types]
                      if request.employment_types else None)
        blocked = profile.blocked_companies or None
        max_required_experience = profile.professional_years if profile.strict_experience_constraint else None
        minimum_salary = profile.minimum_salary if not profile.salary_flexible else None
        query = " ".join(profile.skills + profile.roles + profile.role_families + profile.domains)
        with self._connection() as conn:
            semantic = conn.execute(
                "select * from matching_semantic_jobs(%s::vector,%s::integer,%s::text[],%s::text[],%s::text[],%s::text[],%s::text[],%s::real,%s::real,%s::text,%s::integer)",
                (profile.embedding, self.settings.semantic_limit, roles, locations, work_modes, employment,
                 blocked, max_required_experience, minimum_salary, profile.salary_currency,
                 profile.max_job_age_days),
            ).fetchall()
            lexical = conn.execute(
                "select * from matching_lexical_jobs(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (query, self.settings.lexical_limit, roles, locations, work_modes, employment,
                 blocked, max_required_experience, minimum_salary, profile.salary_currency,
                 profile.max_job_age_days),
            ).fetchall()
            fused = reciprocal_rank_fusion(
                [[row["job_id"] for row in semantic], [row["job_id"] for row in lexical]],
                k=self.settings.rrf_k, weights=[self.settings.semantic_weight, self.settings.lexical_weight],
            )[:self.settings.retrieval_limit]
            if not fused:
                return self._retrieve_from_source_jobs(conn, profile, request)
            ids = [job_id for job_id, _ in fused]
            rows = conn.execute("select * from job_intelligence where job_id=any(%s)", (ids,)).fetchall()
        jobs = {}
        for row in rows:
            value = dict(row)
            raw_embedding = value.get("embedding")
            value["embedding"] = (raw_embedding.to_numpy().tolist()
                                  if hasattr(raw_embedding, "to_numpy") else list(raw_embedding or []))
            value["lexical_terms"] = _terms(value.get("matching_text") or "")
            value.pop("lexical", None)
            jobs[value["job_id"]] = JobIntelligence.model_validate(value)
        semantic_scores = {row["job_id"]: float(row["semantic_score"]) for row in semantic}
        lexical_scores = {row["job_id"]: float(row["lexical_score"]) for row in lexical}
        return [RetrievedJob(jobs[job_id], semantic_scores.get(job_id, 0.0),
                             lexical_scores.get(job_id, 0.0), score)
                for job_id, score in fused if job_id in jobs and authorized_for(jobs[job_id], profile)]

    def _retrieve_from_source_jobs(self, conn, profile: CandidateProfile,
                                   request: RecommendationRequest) -> list[RetrievedJob]:
        """Low-storage fallback while the job_intelligence index is empty.

        One round trip: hard filters run in SQL, candidates are ranked there by
        role-title evidence (taxonomy aliases weigh more than single words) and
        recency, and only then hydrated. Python re-checks every hard rule with the
        same eligibility code as the in-memory retriever. No embedding exists on
        this path, so semantic evidence is reported as unknown, not invented.
        """
        taxonomy = _taxonomy()
        rows = self._title_candidates(conn, profile, request)
        query_terms = _query_terms(profile)
        best_hits = max((row["title_hits"] for row in rows), default=0) or 1
        ranked: list[RetrievedJob] = []
        for row in rows:
            job = _source_job_intelligence(_raw(row), taxonomy)
            if not HybridRetriever._eligible(self, job, profile, request):
                continue
            lexical_score = _lexical_score(query_terms, job)
            title_score = row["title_hits"] / best_hits
            ranked.append(RetrievedJob(job, None, lexical_score,
                                       .5 * title_score + .5 * lexical_score))
        ranked.sort(key=lambda item: item.rrf_score, reverse=True)
        return ranked[:self.settings.retrieval_limit]

    def _title_candidates(self, conn, profile: CandidateProfile, request: RecommendationRequest,
                          hydrate: bool = True) -> list[dict[str, Any]]:
        """Jobs whose titles carry the candidate's role phrases, best evidence first.

        ``hydrate=False`` returns only ids and title evidence, for callers that
        fuse candidate lists before paying to read job details.
        """
        targets = list(dict.fromkeys(request.roles + profile.roles + profile.professional_roles))
        phrases, tokens = _role_query_terms(targets, _taxonomy())
        if not phrases and not tokens:
            tokens = [f"%{term}%" for term in dict.fromkeys(normalize_text(s) for s in profile.skills[:12])
                      if len(term) >= 3]
        if not phrases and not tokens:
            return []
        conditions, params = _source_filters(profile, request)
        params.update(phrases=phrases, tokens=tokens, any_title=phrases + tokens,
                      limit=min(1_000, max(300, request.limit * 15)))
        # Rank on narrow heap columns first; large TOASTed values (description,
        # extraction) are read only for the bounded winners, not every match.
        return conn.execute(
            f"""with candidates as materialized (
                  select id,
                         3 * (select count(*) from unnest(%(phrases)s::text[]) p where lower(job_title) like p)
                           + (select count(*) from unnest(%(tokens)s::text[]) p where lower(job_title) like p)
                           as title_hits,
                         coalesce(posted_at, updated_at) as recency
                  from jobs
                  where lower(job_title) like any(%(any_title)s) and {' and '.join(conditions)}
                  order by title_hits desc, recency desc nulls last
                  limit %(limit)s)
                select {_SOURCE_COLUMNS if hydrate else "j.id"}, c.title_hits
                from candidates c join jobs j on j.id = c.id
                order by c.title_hits desc, c.recency desc nulls last""",
            params,
        ).fetchall()

    def _retrieve_with_vectors(self, profile: CandidateProfile,
                               request: RecommendationRequest) -> list[RetrievedJob]:
        """Semantic ANN over every job, fused with title evidence, then hydrated.

        The semantic index finds jobs by meaning, including titles the role
        vocabulary misses; title candidates keep exact role matches in view.
        Both lists are fused with RRF and every candidate gets a real
        similarity, so the scorer never sees fabricated semantic evidence.
        """
        from pgvector import HalfVector
        query = HalfVector(profile.embedding)
        _, params = _source_filters(profile, request)
        with self._connection("vectors") as vectors:
            semantic = vectors.execute(
                "select * from match_job_vectors(%s, %s, %s, %s, %s, %s, %s, %s)",
                (query, self.settings.semantic_limit, params.get("work_modes"), params.get("employment"),
                 params.get("locations"), params.get("blocked"), params.get("max_experience"),
                 params.get("max_age"))).fetchall()
        similarity = {row["job_id"]: float(row["similarity"]) for row in semantic}
        with self._connection() as conn:
            titles = {str(row["id"]): row["title_hits"]
                      for row in self._title_candidates(conn, profile, request, hydrate=False)}
            # Fuse on ids first; job details are read only for the bounded winners.
            fused = reciprocal_rank_fusion(
                [sorted(similarity, key=lambda j: -similarity[j]), sorted(titles, key=lambda j: -titles[j])],
                k=self.settings.rrf_k, weights=[self.settings.semantic_weight, self.settings.lexical_weight],
            )[:min(self.settings.retrieval_limit, max(300, request.limit * 15))]
            rows = {str(row["id"]): row for row in conn.execute(
                f"select {_SOURCE_COLUMNS} from jobs j where j.id = any(%s)",
                ([job_id for job_id, _ in fused],)).fetchall()}
        unscored = [job_id for job_id, _ in fused if job_id not in similarity]
        if unscored:
            with self._connection("vectors") as vectors:
                similarity.update({row["job_id"]: float(row["similarity"]) for row in vectors.execute(
                    "select job_id, (1 - (embedding <=> %s))::real as similarity from job_vectors "
                    "where job_id = any(%s)", (query, unscored)).fetchall()})

        taxonomy, query_terms = _taxonomy(), _query_terms(profile)
        ranked: list[RetrievedJob] = []
        for job_id, score in fused:
            row = rows.get(job_id)
            if row is None:
                continue  # in the index but no longer in public.jobs
            job = _source_job_intelligence(_raw(row), taxonomy)
            if not HybridRetriever._eligible(self, job, profile, request):
                continue
            semantic_score = self._calibrated(similarity[job_id]) if job_id in similarity else None
            ranked.append(RetrievedJob(job, semantic_score, _lexical_score(query_terms, job), score))
        return ranked

    def _calibrated(self, similarity: float) -> float:
        """Map raw cosine (BGE puts unrelated text near 0.5) onto a 0-1 evidence scale."""
        low, high = self.settings.semantic_floor, self.settings.semantic_ceiling
        return max(0.0, min(1.0, (similarity - low) / (high - low)))

    def corpus_version(self) -> str:
        def load() -> str:
            with self._connection() as conn:
                row = conn.execute("select generation::text as generation from matching_state "
                                   "where singleton=true").fetchone()
            version = row["generation"]
            if self._vector_model():
                # A re-synced index changes results, so it must invalidate caches too.
                with self._connection("vectors") as vectors:
                    sync = vectors.execute("select generation::text as generation from job_vector_sync").fetchone()
                version += ":" + sync["generation"]
            return version
        return self._cached("corpus_version", load)


_SOURCE_COLUMNS = """j.id, j.job_title, j.company, j.skills_required, j.skills_preferred,
       j.experience_min, j.experience_max, j.experience, j.location, j.country,
       j.work_mode, j.is_remote, j.job_type, j.role_category, j.status, j.salary,
       j.salary_min, j.salary_max, j.salary_currency, j.salary_period, j.posted_at,
       j.source_board, j.job_url, j.apply_url, j.updated_at,
       left(j.description, 4000) as description,
       -- Over the full text: restrictions usually sit after the first 4000 chars.
       coalesce(j.description, '') ~* '""" + NO_SPONSORSHIP_PATTERN.replace("'", "''") + """' as sponsorship_unavailable,
       coalesce(j.description, '') ~* '""" + CITIZENSHIP_PATTERN.replace("'", "''") + """' as citizenship_required,
       j.extraction -> 'skills' as extraction_skills,
       j.extraction -> 'education' as extraction_education"""


def _raw(row: dict[str, Any]) -> dict[str, Any]:
    raw = dict(row)
    raw["extraction"] = {"skills": raw.pop("extraction_skills", None) or [],
                         "education": raw.pop("extraction_education", None) or []}
    return raw


def _query_terms(profile: CandidateProfile) -> set[str]:
    return set(_terms(" ".join(profile.skills + profile.roles + profile.role_families + profile.domains)))


def _lexical_score(query_terms: set[str], job: JobIntelligence) -> float:
    return len(query_terms & set(job.lexical_terms)) / len(query_terms) if query_terms else 0.0


def _register_vector(conn) -> None:
    try:
        from pgvector.psycopg import register_vector
        register_vector(conn)
    except Exception:  # vector type absent: only the source fallback can run
        pass


def _source_filters(profile: CandidateProfile, request: RecommendationRequest) -> tuple[list[str], dict[str, Any]]:
    """SQL pre-filters mirroring HybridRetriever._eligible.

    They may only ever be looser than the Python rules, never stricter, so a
    job the policy deems eligible is never dropped before scoring.
    """
    conditions = ["coalesce(lower(status), 'active') not in ('expired', 'deleted', 'closed')"]
    params: dict[str, Any] = {}
    if profile.blocked_companies:
        conditions.append("lower(coalesce(company, '')) <> all(%(blocked)s)")
        params["blocked"] = [str(x).casefold().strip() for x in profile.blocked_companies]
    work_modes = request.work_modes or (["remote"] if request.remote_only else [])
    if work_modes:
        conditions.append("(lower(coalesce(work_mode, '')) = any(%(work_modes)s)"
                          " or (coalesce(is_remote, false) and 'remote' = any(%(work_modes)s)))")
        params["work_modes"] = [canonical_work_mode(x) for x in work_modes]
    if request.employment_types:
        conditions.append("lower(coalesce(job_type, '')) = any(%(employment)s)")
        params["employment"] = [canonical_employment_type(x) for x in request.employment_types]
    locations = request.locations or (profile.locations if profile.hard_location_constraint else [])
    if locations:
        conditions.append("(location ilike any(%(locations)s) or lower(coalesce(work_mode, '')) = 'remote'"
                          " or coalesce(is_remote, false))")
        params["locations"] = [f"%{str(x).strip()}%" for x in locations]
    if profile.strict_experience_constraint and profile.professional_years is not None:
        conditions.append("(experience_min is null or experience_min <= %(max_experience)s)")
        params["max_experience"] = profile.professional_years
    if profile.max_job_age_days:
        conditions.append("(posted_at is null or posted_at >= now() - make_interval(days => %(max_age)s))")
        params["max_age"] = profile.max_job_age_days
    return conditions, params


def _role_query_terms(targets: Iterable[str], taxonomy) -> tuple[list[str], list[str]]:
    """LIKE patterns for title retrieval: role phrases plus distinctive words."""
    subfamilies = {taxonomy.classify_role(str(t))[1] for t in targets} - {None}
    phrases = {normalize_text(t) for t in targets if normalize_text(t)}
    for role in taxonomy.roles:
        if role["subfamily"] in subfamilies:
            phrases.update(normalize_text(alias) for alias in role["aliases"])
    tokens: set[str] = set()
    for phrase in phrases:
        for alias_source, aliases in _ROLE_EXPANSIONS.items():
            if alias_source in phrase:
                tokens.update(aliases)
        tokens.update(t for t in _terms(phrase) if len(t) >= 4 and t not in _GENERIC_ROLE_TERMS)
    tokens -= phrases
    return [f"%{p}%" for p in sorted(phrases)], [f"%{t}%" for t in sorted(tokens)]


def _salary_meets(job: JobIntelligence, minimum: float, currency: str | None) -> bool:
    if currency and normalize_text(job.salary_currency) != normalize_text(currency):
        return False
    annual_max = _annual_salary(job.salary_max if job.salary_max is not None else job.salary_min, job.salary_period)
    return annual_max is not None and annual_max >= minimum


def _taxonomy():
    from .taxonomy.normalizer import default_taxonomy
    return default_taxonomy()


# Words that appear across unrelated roles ("Technical Director", "Global
# Account Manager") and would flood title retrieval with noise.
_GENERIC_ROLE_TERMS = {
    "and", "management", "manager", "specialist", "associate", "senior",
    "junior", "lead", "head", "director", "officer", "professional",
    "technical", "global", "strategic", "staff", "principal", "assistant",
    "engineer", "engineering", "executive", "representative", "coordinator",
    "consultant", "supervisor", "analyst", "chain", "supply",
}

_ROLE_EXPANSIONS = {
    "procurement": ("purchasing", "buyer", "sourcing"),
    "purchasing": ("procurement", "buyer", "buying"),
    "buying": ("buyer", "purchasing", "procurement"),
    "sourcing": ("procurement", "purchasing"),
    "commodity": ("category", "procurement", "sourcing"),
    "supply chain": ("supply chain", "logistics"),
}


def _source_job_intelligence(raw: dict[str, Any], taxonomy) -> JobIntelligence:
    title = str(raw.get("job_title") or raw.get("title") or "Untitled")
    normalized_title = normalize_text(title)
    # Source rows already carry structured extraction. Avoid rerunning the
    # expensive free-text taxonomy scan for every request.
    cleaned = normalize_text(str(raw.get("description") or "")[:8_000])
    required = taxonomy.normalize_skills(_strings(raw.get("skills_required")) +
                                         _extracted_skills(raw, "required"))
    preferred = taxonomy.normalize_skills(_strings(raw.get("skills_preferred")) +
                                          _extracted_skills(raw, "preferred"))
    preferred = [value for value in preferred if value not in required]
    if not required:
        # Same rule as JobProcessor: without stored requirements, skills named
        # in the description are the requirements. Otherwise the heaviest
        # scoring feature silently drops out and scores collapse to role only.
        required = [s for s in taxonomy.extract_skills(cleaned) if s not in preferred]
    role_family, role_subfamily = taxonomy.classify_role(
        title + " " + str(raw.get("role_category") or ""))
    exp_min, exp_max = _experience(raw)
    salary_min, salary_max, salary_currency, salary_period = _compensation(raw)
    location = normalize_text(raw.get("location")) or None
    work_mode = canonical_work_mode(raw.get("work_mode")) or (
        "remote" if raw.get("is_remote") else None)
    qualifications = " ".join(str(raw.get(key) or "") for key in
                              ("basic_qualifications", "preferred_qualifications",
                               "key_responsibilities"))
    matching_text = " | ".join(filter(None, [
        normalized_title, normalize_text(raw.get("role_category")), role_subfamily,
        "required " + " ".join(required) if required else "",
        "preferred " + " ".join(preferred) if preferred else "",
        cleaned[:6000], normalize_text(qualifications)[:4000], location, work_mode,
    ]))
    return JobIntelligence(
        job_id=str(raw.get("id") or raw.get("job_id")), title=title,
        normalized_title=normalized_title, company=str(raw.get("company") or ""),
        cleaned_text=cleaned, matching_text=matching_text,
        role_family=role_family, role_subfamily=role_subfamily, domain=role_family,
        seniority=_seniority(title, raw), required_skills=required,
        preferred_skills=preferred, experience_min_years=exp_min,
        experience_max_years=exp_max,
        education_requirements=_strings(raw.get("education_requirements")) or
                               _extracted_education(raw),
        location=location, country=normalize_text(raw.get("country")) or None,
        work_mode=work_mode,
        employment_type=canonical_employment_type(raw.get("job_type")) or None,
        salary_min=salary_min, salary_max=salary_max,
        salary_currency=salary_currency, salary_period=salary_period,
        posted_at=_datetime(raw.get("posted_at") or raw.get("created_at")),
        source_board=normalize_text(raw.get("source_board")) or None,
        job_url=str(raw.get("job_url") or "") or None,
        apply_url=str(raw.get("apply_url") or "") or None,
        sponsorship_unavailable=raw.get("sponsorship_unavailable"),
        citizenship_required=raw.get("citizenship_required"),
        lexical_terms=sorted(set(_terms(matching_text))),
        active=True, source_hash=stable_hash([raw.get("id"), raw.get("updated_at")]),
        processing_version="direct-source-v1", normalization_version="normalize-v1",
        embedding_model="none", embedding_version="none",
    )


def _annual_salary(value: float | None, period: str | None) -> float | None:
    if value is None:
        return None
    multiplier = {"year": 1, "annual": 1, "annually": 1, "month": 12,
                  "week": 52, "day": 260, "hour": 2080}.get(normalize_text(period))
    return value * multiplier if multiplier else None
