from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import threading
from typing import Protocol

from .schemas import (CandidateProfile, CandidateProfileDraft, Recommendation, RecommendationStatus,
                      RecommendationSummary, UserRecommendation)


_STATE_FIELDS = {"status", "is_new", "first_recommended_at", "last_recommended_at", "seen_at"}


def _recommendation_payload(value: Recommendation) -> dict:
    return value.model_dump(exclude=_STATE_FIELDS)


class UserStateStore(Protocol):
    def save_profile(self, profile: CandidateProfile) -> None: ...
    def latest_profile(self, user_id: str) -> CandidateProfile | None: ...
    def save_profile_draft(self, draft: CandidateProfileDraft) -> CandidateProfileDraft: ...
    def latest_profile_draft(self, user_id: str) -> CandidateProfileDraft | None: ...
    def record_recommendations(self, user_id: str, profile_hash: str,
                               algorithm_version: str,
                               values: list[Recommendation]) -> list[UserRecommendation]: ...
    def recommendations(self, user_id: str, *, include_dismissed: bool = False,
                        limit: int = 200) -> list[UserRecommendation]: ...
    def mark_seen(self, user_id: str, job_ids: list[str]) -> int: ...
    def set_status(self, user_id: str, job_id: str,
                   status: RecommendationStatus) -> UserRecommendation | None: ...
    def summary(self, user_id: str) -> RecommendationSummary: ...


class MemoryUserStateStore:
    def __init__(self) -> None:
        self._profiles: dict[str, tuple[datetime, CandidateProfile]] = {}
        self._profile_drafts: dict[str, CandidateProfileDraft] = {}
        self._recommendations: dict[tuple[str, str], UserRecommendation] = {}
        self._guard = threading.RLock()

    def save_profile(self, profile: CandidateProfile) -> None:
        with self._guard:
            self._profiles[profile.user_id] = (datetime.now(timezone.utc), profile.model_copy(deep=True))

    def latest_profile(self, user_id: str) -> CandidateProfile | None:
        with self._guard:
            value = self._profiles.get(user_id)
            return value[1].model_copy(deep=True) if value else None

    def save_profile_draft(self, draft: CandidateProfileDraft) -> CandidateProfileDraft:
        value = draft.model_copy(update={"updated_at": datetime.now(timezone.utc)}, deep=True)
        with self._guard:
            self._profile_drafts[draft.user_id] = value
        return value.model_copy(deep=True)

    def latest_profile_draft(self, user_id: str) -> CandidateProfileDraft | None:
        with self._guard:
            value = self._profile_drafts.get(user_id)
            return value.model_copy(deep=True) if value else None

    def record_recommendations(self, user_id: str, profile_hash: str,
                               algorithm_version: str,
                               values: list[Recommendation]) -> list[UserRecommendation]:
        del profile_hash, algorithm_version
        now = datetime.now(timezone.utc)
        recorded = []
        with self._guard:
            for value in values:
                key = (user_id, value.job_id)
                previous = self._recommendations.get(key)
                item = UserRecommendation(
                    **_recommendation_payload(value),
                    status=previous.status if previous else RecommendationStatus.recommended,
                    is_new=previous is None or previous.seen_at is None,
                    first_recommended_at=previous.first_recommended_at if previous else now,
                    last_recommended_at=now,
                    seen_at=previous.seen_at if previous else None,
                )
                self._recommendations[key] = item
                recorded.append(item.model_copy(deep=True))
        return recorded

    def recommendations(self, user_id: str, *, include_dismissed: bool = False,
                        limit: int = 200) -> list[UserRecommendation]:
        with self._guard:
            values = [deepcopy(value) for (owner, _), value in self._recommendations.items()
                      if owner == user_id and (include_dismissed or value.status != RecommendationStatus.dismissed)]
        values.sort(key=lambda item: item.last_recommended_at, reverse=True)
        return values[:limit]

    def mark_seen(self, user_id: str, job_ids: list[str]) -> int:
        now, changed = datetime.now(timezone.utc), 0
        requested = set(job_ids)
        with self._guard:
            for (owner, job_id), value in self._recommendations.items():
                if owner == user_id and value.seen_at is None and (not requested or job_id in requested):
                    value.seen_at = now
                    value.is_new = False
                    changed += 1
        return changed

    def set_status(self, user_id: str, job_id: str,
                   status: RecommendationStatus) -> UserRecommendation | None:
        with self._guard:
            value = self._recommendations.get((user_id, job_id))
            if value is None:
                return None
            value.status = status
            return value.model_copy(deep=True)

    def summary(self, user_id: str) -> RecommendationSummary:
        values = self.recommendations(user_id, include_dismissed=True, limit=1_000_000)
        return RecommendationSummary(
            total=sum(item.status != RecommendationStatus.dismissed for item in values),
            unread=sum(item.seen_at is None and item.status != RecommendationStatus.dismissed for item in values),
            saved=sum(item.status == RecommendationStatus.saved for item in values),
            applied=sum(item.status == RecommendationStatus.applied for item in values),
        )


class PostgresUserStateStore:
    """Durable, account-scoped recommendation state stored in Neon/PostgreSQL."""

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string
        self._draft_fallback: dict[str, CandidateProfileDraft] = {}
        self._draft_guard = threading.RLock()

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
            from pgvector.psycopg import register_vector
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("PostgreSQL user state requires psycopg") from exc
        connection = psycopg.connect(self.connection_string, row_factory=dict_row)
        register_vector(connection)
        return connection

    def save_profile(self, profile: CandidateProfile) -> None:
        payload = json.dumps(profile.model_dump(mode="json"))
        with self._connect() as conn:
            conn.execute(
                """insert into candidate_profiles
                   (user_id, profile_hash, profile_json, embedding, embedding_model, embedding_version, processing_version, processed_at)
                   values (%s::uuid,%s,%s::jsonb,%s,%s,%s,%s,now())
                   on conflict (user_id, profile_hash) do update set
                     profile_json=excluded.profile_json, embedding=excluded.embedding,
                     embedding_model=excluded.embedding_model, embedding_version=excluded.embedding_version,
                     processing_version=excluded.processing_version, processed_at=now()""",
                (profile.user_id, profile.profile_hash, payload, profile.embedding or None,
                 profile.embedding_model, profile.embedding_version, profile.processing_version),
            )

    def latest_profile(self, user_id: str) -> CandidateProfile | None:
        with self._connect() as conn:
            row = conn.execute(
                "select profile_json from candidate_profiles where user_id=%s::uuid order by processed_at desc limit 1",
                (user_id,),
            ).fetchone()
        return CandidateProfile.model_validate(row["profile_json"]) if row else None

    def save_profile_draft(self, draft: CandidateProfileDraft) -> CandidateProfileDraft:
        value = draft.model_copy(update={"updated_at": datetime.now(timezone.utc)}, deep=True)
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """insert into candidate_profile_drafts (user_id, draft_json, updated_at)
                       values (%s::uuid,%s::jsonb,now())
                       on conflict (user_id) do update set
                         draft_json=excluded.draft_json, updated_at=now()
                       returning updated_at""",
                    (draft.user_id, json.dumps(draft.form_data)),
                ).fetchone()
            value = draft.model_copy(update={"updated_at": row["updated_at"]})
        except Exception:
            # Keep development usable when Neon is temporarily unavailable or at
            # its storage limit. The browser also keeps an account-scoped copy.
            pass
        with self._draft_guard:
            self._draft_fallback[draft.user_id] = value
        return value

    def latest_profile_draft(self, user_id: str) -> CandidateProfileDraft | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "select draft_json, updated_at from candidate_profile_drafts where user_id=%s::uuid",
                    (user_id,),
                ).fetchone()
        except Exception:
            with self._draft_guard:
                value = self._draft_fallback.get(user_id)
                return value.model_copy(deep=True) if value else None
        if not row:
            with self._draft_guard:
                value = self._draft_fallback.get(user_id)
                return value.model_copy(deep=True) if value else None
        return CandidateProfileDraft(user_id=user_id, form_data=row["draft_json"], updated_at=row["updated_at"])

    @staticmethod
    def _from_row(row) -> UserRecommendation:
        payload = dict(row["recommendation_json"])
        for field in _STATE_FIELDS:
            payload.pop(field, None)
        return UserRecommendation(
            **payload,
            status=row["status"],
            is_new=row["seen_at"] is None,
            first_recommended_at=row["first_recommended_at"],
            last_recommended_at=row["last_recommended_at"],
            seen_at=row["seen_at"],
        )

    def record_recommendations(self, user_id: str, profile_hash: str,
                               algorithm_version: str,
                               values: list[Recommendation]) -> list[UserRecommendation]:
        with self._connect() as conn:
            for value in values:
                conn.execute(
                    """insert into user_job_recommendations
                       (user_id, job_id, profile_hash, algorithm_version, recommendation_json)
                       values (%s::uuid,%s,%s,%s,%s::jsonb)
                       on conflict (user_id, job_id) do update set
                         profile_hash=excluded.profile_hash,
                         algorithm_version=excluded.algorithm_version,
                         recommendation_json=excluded.recommendation_json,
                         last_recommended_at=now()""",
                    (user_id, value.job_id, profile_hash, algorithm_version,
                     json.dumps(value.model_dump(mode="json", exclude=_STATE_FIELDS))),
                )
        current = {item.job_id: item for item in self.recommendations(user_id, include_dismissed=True, limit=1000)}
        return [current[value.job_id] for value in values if value.job_id in current]

    def recommendations(self, user_id: str, *, include_dismissed: bool = False,
                        limit: int = 200) -> list[UserRecommendation]:
        where = "user_id=%s::uuid" if include_dismissed else "user_id=%s::uuid and status<>'dismissed'"
        with self._connect() as conn:
            rows = conn.execute(
                f"""select recommendation_json, status, first_recommended_at, last_recommended_at, seen_at
                    from user_job_recommendations where {where}
                    order by last_recommended_at desc limit %s""",
                (user_id, limit),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def mark_seen(self, user_id: str, job_ids: list[str]) -> int:
        with self._connect() as conn:
            if job_ids:
                result = conn.execute(
                    "update user_job_recommendations set seen_at=coalesce(seen_at,now()) where user_id=%s::uuid and job_id=any(%s) and seen_at is null",
                    (user_id, job_ids),
                )
            else:
                result = conn.execute(
                    "update user_job_recommendations set seen_at=now() where user_id=%s::uuid and seen_at is null",
                    (user_id,),
                )
            return result.rowcount

    def set_status(self, user_id: str, job_id: str,
                   status: RecommendationStatus) -> UserRecommendation | None:
        with self._connect() as conn:
            row = conn.execute(
                """update user_job_recommendations set status=%s, status_updated_at=now()
                   where user_id=%s::uuid and job_id=%s
                   returning recommendation_json, status, first_recommended_at, last_recommended_at, seen_at""",
                (status.value, user_id, job_id),
            ).fetchone()
        return self._from_row(row) if row else None

    def summary(self, user_id: str) -> RecommendationSummary:
        with self._connect() as conn:
            row = conn.execute(
                """select
                     count(*) filter (where status<>'dismissed') as total,
                     count(*) filter (where seen_at is null and status<>'dismissed') as unread,
                     count(*) filter (where status='saved') as saved,
                     count(*) filter (where status='applied') as applied
                   from user_job_recommendations where user_id=%s::uuid""",
                (user_id,),
            ).fetchone()
        return RecommendationSummary(**row)
