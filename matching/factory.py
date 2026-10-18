from __future__ import annotations

from dataclasses import replace

from .config import MatchingSettings
from .embeddings import FastEmbedProvider, HashingEmbeddingProvider
from .ingestion import JobProcessor
from .profile import CandidateProcessor
from .resume import CandidateIntakeService, ResumeParser
from .repository import MemoryRepository
from .retrieval import HybridRetriever
from .retrieval import PostgresHybridRetriever
from .scoring import DeterministicScorer
from .service import RecommendationService
from .taxonomy import Taxonomy


def local_stack(settings: MatchingSettings | None = None):
    """Dependency graph for local use/tests; production injects a PostgreSQL repository/retriever."""
    settings = settings or MatchingSettings.from_env()
    repository = MemoryRepository()
    taxonomy = Taxonomy()
    embeddings = HashingEmbeddingProvider(settings.embedding_dimensions, settings.embedding_version)
    processor = JobProcessor(repository, embeddings, taxonomy, settings)
    profiles = CandidateProcessor(repository, embeddings, taxonomy, settings)
    service = RecommendationService(repository, profiles, HybridRetriever(repository, settings),
                                    DeterministicScorer(taxonomy, settings), settings)
    return repository, processor, service


def local_intake_stack(settings: MatchingSettings | None = None):
    repository, processor, service = local_stack(settings)
    intake = CandidateIntakeService(ResumeParser(Taxonomy()))
    return repository, processor, service, intake


def postgres_intake_stack(connection_string: str, settings: MatchingSettings | None = None,
                          vector_connection_string: str | None = None):
    """API stack that ranks the shared Neon corpus while keeping request caches local.

    With ``vector_connection_string`` (the semantic index database), profiles are
    embedded with the same model as the indexed jobs so their vectors compare.
    """
    settings = settings or MatchingSettings.from_env()
    repository = MemoryRepository()
    taxonomy = Taxonomy()
    if vector_connection_string:
        embeddings = FastEmbedProvider()
        # The profile hash includes embedding_version, so switching models
        # can never reuse a cached profile embedded by the other model.
        settings = replace(settings, embedding_version=embeddings.version)
    else:
        embeddings = HashingEmbeddingProvider(settings.embedding_dimensions, settings.embedding_version)
    profiles = CandidateProcessor(repository, embeddings, taxonomy, settings)
    service = RecommendationService(
        repository,
        profiles,
        PostgresHybridRetriever(connection_string, settings, vector_connection_string),
        DeterministicScorer(taxonomy, settings),
        settings,
    )
    intake = CandidateIntakeService(ResumeParser(taxonomy))
    return repository, service, intake
