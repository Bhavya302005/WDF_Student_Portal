"""Keep the semantic index (MATCHING_DATABASE_URL) in step with public.jobs.

    python -m matching.vector_sync            # incremental: jobs changed since last run
    python -m matching.vector_sync --full     # every job; also drops vectors of deleted jobs

Parallel bulk backfill (see .github/workflows/vector_index.yml):

    python -m matching.vector_sync --prepare-bulk             # once: drop the ANN index
    python -m matching.vector_sync --full --shard 3/20 --bulk  # on N machines at once
    python -m matching.vector_sync --finalize --synced-at T --reconcile   # once: rebuild

Reads the jobs database read-only. A job is re-embedded only when its
embedding text changes; status/metadata changes update the row without a new
embedding, and identical rows are not rewritten at all.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Iterable, Iterator

from .embeddings import EmbeddingProvider, FastEmbedProvider
from .hashing import canonical_employment_type, canonical_work_mode, normalize_text, stable_hash
from .scoring import _title_level
from .taxonomy.normalizer import Taxonomy, default_taxonomy

log = logging.getLogger("matching.vector_sync")

SCHEMA_PATH = Path(__file__).with_name("sql") / "vector_store.sql"
INDEX_PATH = Path(__file__).with_name("sql") / "vector_store_index.sql"
# Title, skills and the opening of the description carry the matching signal;
# 1k characters (~250 tokens) embeds ~2x faster than the full 512-token window.
DESCRIPTION_CHARS = 1_000
INACTIVE_STATUSES = {"expired", "deleted", "closed"}

_SOURCE_SQL = """
select id, job_title, company, left(description, %(chars)s) as description,
       skills_required, location, country, work_mode, is_remote, job_type,
       role_category, status, experience_min, posted_at, updated_at
from public.jobs
where id > %(after)s {changed} {shard}
order by id
limit %(page)s
"""

_CONTENT_KEY = ("coalesce(job_title, '') || '|' || coalesce(role_category, '') || '|' || "
                "coalesce(skills_required::text, '') || '|' || left(coalesce(description, ''), %(chars)s)")

_METADATA = ("title", "company_key", "role_family", "role_subfamily", "seniority_level", "location",
             "country", "work_mode", "employment_type", "experience_min_years", "posted_at", "active",
             "source_updated_at")

_UPSERT_SQL = (
    "insert into public.job_vectors (job_id, embedding, source_hash, embedding_model, embedding_version, "
    + ", ".join(_METADATA) + ") values (%(job_id)s, %(embedding)s, %(source_hash)s, %(embedding_model)s, "
    "%(embedding_version)s, " + ", ".join(f"%({c})s" for c in _METADATA) + ")\n"
    "on conflict (job_id) do update set embedding = excluded.embedding, source_hash = excluded.source_hash, "
    "embedding_model = excluded.embedding_model, embedding_version = excluded.embedding_version, "
    + ", ".join(f"{c} = excluded.{c}" for c in _METADATA) + ", synced_at = now()"
)

# Same row, new metadata only (status, filters): keep the stored embedding, and
# skip the write entirely when nothing differs so re-syncs do not bloat storage.
_METADATA_SQL = (
    "update public.job_vectors set " + ", ".join(f"{c} = %({c})s" for c in _METADATA)
    + ", synced_at = now() where job_id = %(job_id)s and ("
    + ", ".join(_METADATA) + ") is distinct from (" + ", ".join(f"%({c})s" for c in _METADATA) + ")"
)


def embedding_text(row: dict[str, Any]) -> str:
    """The text a job is embedded from: title first, then role, skills, description."""
    skills = row.get("skills_required") or []
    if isinstance(skills, str):
        skills = [s.strip() for s in skills.split(",")]
    parts = [
        str(row.get("job_title") or "").strip(),
        str(row.get("role_category") or "").replace("_", " ").strip(),
        ("Skills: " + ", ".join(str(s) for s in skills[:25])) if skills else "",
        " ".join(str(row.get("description") or "").split())[:DESCRIPTION_CHARS],
    ]
    return ". ".join(p for p in parts if p)


def vector_row(row: dict[str, Any], taxonomy: Taxonomy, provider: EmbeddingProvider) -> dict[str, Any]:
    title = str(row.get("job_title") or "")
    family, subfamily = taxonomy.classify_role(title + " " + str(row.get("role_category") or ""))
    text = embedding_text(row)
    return {
        "job_id": str(row["id"]), "title": title, "_text": text,
        "source_hash": stable_hash([provider.version, text]),
        "embedding_model": provider.model_name, "embedding_version": provider.version,
        "company_key": str(row.get("company") or "").casefold().strip(),
        "role_family": family, "role_subfamily": subfamily, "seniority_level": _title_level(title),
        "location": row.get("location") or None, "country": row.get("country") or None,
        "work_mode": canonical_work_mode(row.get("work_mode")) or ("remote" if row.get("is_remote") else None),
        "employment_type": canonical_employment_type(row.get("job_type")) or None,
        "experience_min_years": row.get("experience_min"), "posted_at": row.get("posted_at"),
        "active": normalize_text(row.get("status") or "active") not in INACTIVE_STATUSES,
        "source_updated_at": row.get("updated_at"),
    }


def _pages(jobs_conn, since: datetime | None, page: int,
           shard: tuple[int, int] | None = None) -> Iterator[list[dict[str, Any]]]:
    after = ""
    changed = "and updated_at > %(since)s" if since else ""
    # Shards split by a hash of the fields the embedding text is built from, so
    # every job lands in exactly one shard and identical postings land together,
    # where all but the first reuse its vector instead of re-running the model.
    in_shard = ("and mod(hashtext(" + _CONTENT_KEY + ")::bigint + 2147483648, %(shards)s) = %(shard)s"
                if shard else "")
    while True:
        rows = jobs_conn.execute(_SOURCE_SQL.format(changed=changed, shard=in_shard), {
            "chars": DESCRIPTION_CHARS, "after": after, "page": page, "since": since,
            "shard": shard[0] if shard else None, "shards": shard[1] if shard else None}).fetchall()
        if not rows:
            return
        yield rows
        after = rows[-1]["id"]


def _assign_embeddings(vectors, provider: EmbeddingProvider, items: list[dict[str, Any]]) -> int:
    """Give each item its vector, running the model once per distinct text.

    The same model on the same text yields the same vector, so reusing a stored
    or batch-mate's vector changes nothing in the results. Returns how many
    items were served without running the model.
    """
    from pgvector import HalfVector
    if not items:
        return 0
    hashes = list(dict.fromkeys(item["source_hash"] for item in items))
    known = {row["source_hash"]: row["embedding"] for row in vectors.execute(
        "select distinct on (source_hash) source_hash, embedding from job_vectors where source_hash = any(%s)",
        (hashes,)).fetchall()}
    texts = {item["source_hash"]: item["_text"] for item in items if item["source_hash"] not in known}
    for source_hash, vector in zip(texts, provider.embed(list(texts.values()))):
        known[source_hash] = HalfVector(vector)
    for item in items:
        item["embedding"] = known[item["source_hash"]]
    return len(items) - len(texts)


def _connect(url: str, *, read_only: bool = False):
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row
    if read_only:
        url = make_conninfo(url, options="-c default_transaction_read_only=on")
    return psycopg.connect(url, autocommit=True, row_factory=dict_row)


# Parallel shard runners all apply the idempotent schema at start-up; concurrent
# CREATE OR REPLACE FUNCTION fails with "tuple concurrently updated", so take turns.
_SCHEMA_LOCK = 7_412_093_001


def _apply_schema(vectors, *, index: bool) -> None:
    from pgvector.psycopg import register_vector
    vectors.execute("select pg_advisory_lock(%s)", (_SCHEMA_LOCK,))
    try:
        vectors.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        if index:
            vectors.execute(INDEX_PATH.read_text(encoding="utf-8"))
    finally:
        vectors.execute("select pg_advisory_unlock(%s)", (_SCHEMA_LOCK,))
    register_vector(vectors)


def _remove_missing(vectors, job_ids: Iterable[str]) -> int:
    """Vectors are derived data: drop those whose job no longer exists."""
    with vectors.transaction(), vectors.cursor() as cur:
        cur.execute("create temp table seen_jobs (job_id text primary key) on commit drop")
        with cur.copy("copy seen_jobs (job_id) from stdin") as copy:
            for job_id in job_ids:
                copy.write_row((job_id,))
        cur.execute("delete from job_vectors v where not exists "
                    "(select 1 from seen_jobs s where s.job_id = v.job_id)")
        return cur.rowcount


def _mark_synced(vectors, newest: datetime | None, full: bool) -> None:
    vectors.execute(
        "update job_vector_sync set last_source_updated_at = %s, generation = generation + 1, "
        "last_full_sync_at = case when %s then now() else last_full_sync_at end, updated_at = now()",
        (newest, full))


def _size(vectors) -> str:
    return vectors.execute("select pg_size_pretty(pg_database_size(current_database())) as size").fetchone()["size"]


def sync(jobs_url: str, vectors_url: str, provider: EmbeddingProvider, *, full: bool = False,
         page: int = 256, limit: int | None = None, shard: tuple[int, int] | None = None,
         bulk: bool = False) -> dict[str, Any]:
    """Embed new and changed jobs.

    ``shard=(k, n)`` processes only the k-th of n disjoint slices of the jobs,
    so n machines can backfill in parallel. Sharded and limited runs never
    delete vectors or advance the sync point; ``finalize`` does that once all
    shards are done. ``bulk`` skips (re)creating the ANN index while loading.
    """
    from pgvector import HalfVector

    taxonomy = default_taxonomy()
    stats = {"read": 0, "embedded": 0, "reused": 0, "metadata_updated": 0, "unchanged": 0, "removed": 0}
    started = time.perf_counter()
    with _connect(vectors_url) as vectors, _connect(jobs_url, read_only=True) as jobs:
        _apply_schema(vectors, index=not bulk)
        state = vectors.execute("select last_source_updated_at from job_vector_sync").fetchone()
        # Overlap by an hour so rows committed during the previous run are not missed.
        since = None if full or not state["last_source_updated_at"] else state["last_source_updated_at"] - timedelta(hours=1)
        newest = state["last_source_updated_at"]
        seen: list[str] = []
        for rows in _pages(jobs, since, page, shard):
            batch = [vector_row(r, taxonomy, provider) for r in rows]
            stats["read"] += len(batch)
            seen.extend(item["job_id"] for item in batch)
            for item in batch:
                if item["source_updated_at"] and (newest is None or item["source_updated_at"] > newest):
                    newest = item["source_updated_at"]
            stored = {r["job_id"]: r["source_hash"] for r in vectors.execute(
                "select job_id, source_hash from job_vectors where job_id = any(%s)",
                ([item["job_id"] for item in batch],)).fetchall()}
            to_embed = [item for item in batch if stored.get(item["job_id"]) != item["source_hash"]]
            stats["reused"] += _assign_embeddings(vectors, provider, to_embed)
            with vectors.transaction(), vectors.cursor() as cur:
                if to_embed:
                    cur.executemany(_UPSERT_SQL, to_embed)
                    stats["embedded"] += len(to_embed)
                rest = [item for item in batch if item["job_id"] in stored and "embedding" not in item]
                if rest:
                    # executemany pipelines the statements: one round trip, not one per row.
                    cur.executemany(_METADATA_SQL, rest)
                    stats["metadata_updated"] += cur.rowcount
                    stats["unchanged"] += len(rest) - cur.rowcount
            log.info("synced %d jobs (%d written, %d reused an identical text's vector)",
                     stats["read"], stats["embedded"], stats["reused"])
            if limit and stats["read"] >= limit:
                break
        complete = not limit and shard is None
        if full and complete:
            stats["removed"] = _remove_missing(vectors, seen)
        if complete:
            _mark_synced(vectors, newest, full)
        size = _size(vectors)
    stats.update(seconds=round(time.perf_counter() - started, 1), database_size=size,
                 mode="full" if full else "incremental", complete=complete,
                 shard=f"{shard[0]}/{shard[1]}" if shard else None)
    return stats


def prepare_bulk(vectors_url: str) -> dict[str, Any]:
    """Drop the ANN index before a parallel backfill; ``finalize`` rebuilds it.

    Search keeps working meanwhile (exact scan, slower), and retrieval falls
    back to title search if the index database is unavailable.
    """
    with _connect(vectors_url) as vectors:
        _apply_schema(vectors, index=False)
        vectors.execute("drop index if exists job_vectors_embedding_hnsw")
        return {"prepared": True, "vectors": vectors.execute("select count(*) as n from job_vectors").fetchone()["n"]}


def finalize(jobs_url: str, vectors_url: str, *, synced_at: datetime | None, reconcile: bool) -> dict[str, Any]:
    """Build the ANN index (no-op when present) after sharded runs.

    ``synced_at`` advances the sync point and must be when the run *started*,
    so jobs changed while shards ran are picked up next time; pass it only
    when every shard succeeded. ``reconcile`` (full runs) also prunes vectors
    of jobs deleted from public.jobs.
    """
    started = time.perf_counter()
    stats: dict[str, Any] = {"index_built": False, "removed": 0, "reconciled": reconcile}
    with _connect(vectors_url) as vectors:
        _apply_schema(vectors, index=False)
        # One index build is far cheaper than maintaining HNSW per insert.
        vectors.execute("set maintenance_work_mem = '256MB'")
        vectors.execute(INDEX_PATH.read_text(encoding="utf-8"))
        stats["index_built"] = True
        if reconcile:
            with _connect(jobs_url, read_only=True) as jobs:
                ids, after = [], ""
                while True:
                    rows = jobs.execute("select id from public.jobs where id > %s order by id limit 50000",
                                        (after,)).fetchall()
                    if not rows:
                        break
                    ids.extend(str(r["id"]) for r in rows)
                    after = rows[-1]["id"]
            stats["removed"] = _remove_missing(vectors, ids)
        if synced_at is not None:
            _mark_synced(vectors, synced_at, reconcile)
        vectors.execute("analyze job_vectors")
        stats.update(vectors=vectors.execute("select count(*) as n from job_vectors").fetchone()["n"],
                     database_size=_size(vectors), seconds=round(time.perf_counter() - started, 1))
    return stats


def _env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        from dotenv import dotenv_values
        value = dotenv_values(Path(__file__).resolve().parents[1] / ".env.local").get(name)
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


def _shard(value: str) -> tuple[int, int]:
    k, n = (int(x) for x in value.split("/"))
    if not 0 <= k < n:
        raise argparse.ArgumentTypeError("shard must be K/N with 0 <= K < N")
    return k, n


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--full", action="store_true", help="re-check every job and drop vectors of deleted jobs")
    parser.add_argument("--limit", type=int, help="stop after this many jobs (testing; leaves sync state unchanged)")
    parser.add_argument("--page", type=int, default=256)
    parser.add_argument("--shard", type=_shard, help="process only slice K of N (parallel backfill)")
    parser.add_argument("--bulk", action="store_true", help="do not (re)create the ANN index while loading")
    parser.add_argument("--prepare-bulk", action="store_true", help="drop the ANN index before a parallel backfill")
    parser.add_argument("--finalize", action="store_true", help="build the ANN index after a backfill")
    parser.add_argument("--reconcile", action="store_true", help="with --finalize: prune vectors of deleted jobs")
    parser.add_argument("--synced-at", type=datetime.fromisoformat,
                        help="with --finalize: advance the sync point to when the run started")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    vectors_url = _env("MATCHING_DATABASE_URL")
    if args.prepare_bulk:
        stats = prepare_bulk(vectors_url)
    elif args.finalize:
        if args.reconcile and not args.synced_at:
            raise SystemExit("--reconcile needs --synced-at (when the backfill started)")
        stats = finalize(_env("NEON_DATABASE_URL"), vectors_url, synced_at=args.synced_at, reconcile=args.reconcile)
    else:
        stats = sync(_env("NEON_DATABASE_URL"), vectors_url, FastEmbedProvider(), full=args.full,
                     page=args.page, limit=args.limit, shard=args.shard, bulk=args.bulk)
    print(json.dumps(stats, default=str))


if __name__ == "__main__":
    main()
