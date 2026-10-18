from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
import threading
from typing import Iterator, Protocol

from .schemas import CandidateProfile, JobIntelligence, Recommendation


class MatchingRepository(Protocol):
    def get_job(self, job_id: str) -> JobIntelligence | None: ...
    def save_job(self, job: JobIntelligence) -> None: ...
    def ready_jobs(self) -> list[JobIntelligence]: ...
    def get_profile(self, profile_hash: str) -> CandidateProfile | None: ...
    def save_profile(self, profile: CandidateProfile) -> None: ...
    def get_matches(self, profile_hash: str, algorithm_version: str) -> list[Recommendation] | None: ...
    def save_matches(self, profile_hash: str, algorithm_version: str, values: list[Recommendation]) -> None: ...
    def job_lock(self, job_id: str) -> Iterator[None]: ...


class MemoryRepository:
    """Thread-safe adapter used by tests/local development; production uses PostgreSQL indexes."""

    def __init__(self) -> None:
        self.jobs: dict[str, JobIntelligence] = {}
        self.profiles: dict[str, CandidateProfile] = {}
        self.matches: dict[tuple[str, str], list[Recommendation]] = {}
        self.metrics: Counter[str] = Counter()
        self._guard = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}
        self._corpus_version = 0

    def get_job(self, job_id: str) -> JobIntelligence | None:
        with self._guard:
            value = self.jobs.get(job_id)
            return value.model_copy(deep=True) if value else None

    def save_job(self, job: JobIntelligence) -> None:
        with self._guard:
            self.jobs[job.job_id] = job.model_copy(deep=True)
            self._corpus_version += 1
            self.matches.clear()

    def corpus_version(self) -> str:
        with self._guard:
            return str(self._corpus_version)

    def ready_jobs(self) -> list[JobIntelligence]:
        with self._guard:
            return [v.model_copy(deep=True) for v in self.jobs.values() if v.active and v.processing_status.value == "READY"]

    def get_profile(self, profile_hash: str) -> CandidateProfile | None:
        with self._guard:
            value = self.profiles.get(profile_hash)
            return value.model_copy(deep=True) if value else None

    def save_profile(self, profile: CandidateProfile) -> None:
        with self._guard:
            self.profiles[profile.profile_hash] = profile.model_copy(deep=True)

    def get_matches(self, profile_hash: str, algorithm_version: str) -> list[Recommendation] | None:
        with self._guard:
            value = self.matches.get((profile_hash, algorithm_version))
            if value is not None:
                self.metrics["retrieval_cache_hits"] += 1
                return deepcopy(value)
            return None

    def save_matches(self, profile_hash: str, algorithm_version: str, values: list[Recommendation]) -> None:
        with self._guard:
            self.matches[(profile_hash, algorithm_version)] = deepcopy(values)

    @contextmanager
    def job_lock(self, job_id: str) -> Iterator[None]:
        with self._guard:
            lock = self._locks.setdefault(job_id, threading.Lock())
        with lock:
            yield
