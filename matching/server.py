from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .api import create_app
from .factory import local_intake_stack, postgres_intake_stack
from .user_state import MemoryUserStateStore, PostgresUserStateStore


# Local backend configuration lives at the monorepo root. Existing process
# environment values still win in deployed environments.
load_dotenv(Path(__file__).resolve().parents[1] / ".env.local", override=False)


def build_development_app():
    """Development-only API used by the Hirevia frontend integration."""
    database_url = os.getenv("NEON_DATABASE_URL") or os.getenv("DATABASE_URL")
    if database_url:
        repository, recommendations, intake = postgres_intake_stack(
            database_url, vector_connection_string=os.getenv("MATCHING_DATABASE_URL"))
        processor = None
    else:
        repository, processor, recommendations, intake = local_intake_stack()
    jobs_file = os.getenv("MATCHING_JOBS_FILE")
    if jobs_file:
        payload = json.loads(Path(jobs_file).read_text(encoding="utf-8"))
        if processor is None:
            raise RuntimeError("MATCHING_JOBS_FILE cannot be combined with Neon database retrieval")
        for raw in payload:
            processor.process(raw)
    origins = [value.strip() for value in os.getenv(
        "MATCHING_ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    ).split(",") if value.strip()]
    user_state = PostgresUserStateStore(database_url) if database_url else MemoryUserStateStore()
    app = create_app(recommendations, intake, origins, user_state=user_state)
    app.state.matching_repository = repository
    return app


app = build_development_app()
