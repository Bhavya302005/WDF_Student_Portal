"""
db/neon_upsert.py
=================
Push scraped jobs to the Neon Postgres `public.jobs` table.

Environment
-----------
NEON_DATABASE_URL   – required. Postgres connection string.
                      The workflow exposes it via:
                        env:
                          DATABASE_URL: ${{ secrets.NEON_DATABASE_URL }}

Rules
-----
- Never falls back to a hardcoded URL.
- Never prints the connection string in logs.
- Only writes to public.jobs. Matching tables are managed by DB triggers.
- posted_at must be timezone-aware UTC.
- skills_required / skills_preferred / extraction must be wrapped in Jsonb.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

log = logging.getLogger("neon_upsert")

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

COLUMNS = (
    "id", "job_title", "company", "description",
    "skills_required", "skills_preferred",
    "experience_min", "experience_max", "experience",
    "location", "country", "work_mode", "is_remote",
    "job_type", "role_category", "status",
    "salary", "salary_min", "salary_max", "salary_currency", "salary_period",
    "posted_at", "source_board",
    "basic_qualifications", "preferred_qualifications", "key_responsibilities",
    "extraction", "job_url", "apply_url",
)

# Assigning EXCLUDED.<col> writes a fresh TOAST copy even when the text is
# identical, so a weekly re-upsert used to double the table's storage. Keeping
# the stored value whenever it is unchanged reuses the existing TOAST data.
LARGE_COLUMNS = ("description", "basic_qualifications", "preferred_qualifications",
                 "key_responsibilities", "extraction")

_CONTENT_COLUMNS = [column for column in COLUMNS if column != "id"]
_CHANGED = ("(" + ", ".join(f"jobs.{c}" for c in _CONTENT_COLUMNS) + ") IS DISTINCT FROM ("
            + ", ".join(f"EXCLUDED.{c}" for c in _CONTENT_COLUMNS) + ")")


def _assignment(column: str) -> str:
    if column in LARGE_COLUMNS:
        return (f"{column} = CASE WHEN jobs.{column} IS DISTINCT FROM EXCLUDED.{column} "
                f"THEN EXCLUDED.{column} ELSE jobs.{column} END")
    return f"{column} = EXCLUDED.{column}"


UPSERT_SQL = (
    "INSERT INTO public.jobs (" + ", ".join(COLUMNS) + ", last_seen_at)\n"
    "VALUES (" + ", ".join(f"%({c})s" for c in COLUMNS) + ", now())\n"
    "ON CONFLICT (id) DO UPDATE SET\n    "
    + ",\n    ".join(_assignment(c) for c in _CONTENT_COLUMNS)
    + f",\n    updated_at = CASE WHEN {_CHANGED} THEN now() ELSE jobs.updated_at END"
    + ",\n    last_seen_at = now();"
)

# Idempotent and metadata-only (no table rewrite) on PostgreSQL 11+.
MIGRATION_SQL = "ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS last_seen_at timestamptz"

# Soft expiry only: a job that reappears is reactivated by the next upsert.
EXPIRE_SQL = """
UPDATE public.jobs SET status = 'expired', updated_at = now()
WHERE status = 'active'
  AND coalesce(last_seen_at, updated_at) < now() - make_interval(days => %(days)s)
"""

# Deleting cascades to user feed rows, so never purge jobs a user saved or applied to.
PURGE_SQL = """
DELETE FROM public.jobs j
WHERE j.status = 'expired'
  AND j.updated_at <= now() - make_interval(days => %(days)s)
  AND NOT EXISTS (
      SELECT 1 FROM public.user_job_recommendations r
      WHERE r.job_id = j.id AND r.status IN ('saved', 'applied'))
"""

# ---------------------------------------------------------------------------
# Known field aliases from different scrapers
# ---------------------------------------------------------------------------

# Maps scraper field names → Neon schema field names
_FIELD_ALIASES: dict[str, str] = {
    "title":          "job_title",
    "job_title":      "job_title",
    "name":           "job_title",
    "company_name":   "company",
    "employer":       "company",
    "job_link":       "job_url",
    "url":            "job_url",
    "link":           "job_url",
    "apply_link":     "apply_url",
    "application_url": "apply_url",
    "date_posted":    "posted_at",
    "posted_date":    "posted_at",
    "job_description": "description",
    "desc":           "description",
    "skills":         "skills_required",
    "required_skills": "skills_required",
    "preferred_skills": "skills_preferred",
    "employment_type": "job_type",
    "type":           "job_type",
    "remote":         "is_remote",
    "scraper_type":   "source_board",
}

# Allowed work_mode values
_WORK_MODE_MAP: dict[str, str] = {
    "remote":   "remote",
    "hybrid":   "hybrid",
    "onsite":   "onsite",
    "on-site":  "onsite",
    "on_site":  "onsite",
    "office":   "onsite",
    "in-office": "onsite",
}

# Allowed job_type values
_JOB_TYPE_MAP: dict[str, str] = {
    "full time":   "full time",
    "fulltime":    "full time",
    "full-time":   "full time",
    "part time":   "part time",
    "parttime":    "part time",
    "part-time":   "part time",
    "contract":    "contract",
    "contractor":  "contract",
    "internship":  "internship",
    "intern":      "internship",
    "temporary":   "temporary",
    "temp":        "temporary",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stable_id(job: dict) -> str:
    """Derive a stable ID from job_url > explicit id > hash of title+company."""
    raw_id = (
        job.get("id")
        or job.get("job_url")
        or job.get("url")
        or job.get("job_link")
        or job.get("link")
    )
    if raw_id:
        # Strip tracking params to keep IDs stable across runs
        raw_id = re.sub(r"[?&](utm_[^&]+|trk=[^&]+|ref=[^&]+)", "", str(raw_id))
        return raw_id[:1024]

    # Fallback: deterministic hash
    seed = f"{job.get('job_title', '')}|{job.get('title', '')}|{job.get('company', '')}|{job.get('company_name', '')}"
    return "hash-" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_posted_at(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _text(value: Any) -> str | None:
    """CSV round-trips turn missing values into ''; store them as NULL."""
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _normalize_skills(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(s).strip() for s in value if s]
    if isinstance(value, str):
        # JSON first, then Python-repr lists written by csv.DictWriter.
        for parse in (json.loads, ast.literal_eval):
            try:
                parsed = parse(value)
            except (ValueError, SyntaxError):
                continue
            if isinstance(parsed, list):
                return [str(s).strip() for s in parsed if s]
            break
        # Comma-separated fallback
        return [s.strip() for s in value.split(",") if s.strip()]
    return []


def _normalize_json_object(value: Any) -> dict[str, Any] | None:
    """Decode JSON objects serialized through CSV; reject non-object payloads."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _normalize_work_mode(value: Any) -> str | None:
    if not value:
        return None
    return _WORK_MODE_MAP.get(str(value).lower().strip())


def _normalize_job_type(value: Any) -> str | None:
    if not value:
        return None
    key = str(value).lower().strip().replace("-", " ").replace("_", " ")
    return _JOB_TYPE_MAP.get(key)


def _normalize_is_remote(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).lower() in ("true", "1", "yes", "remote")


def _words(value: Any) -> str:
    return re.sub(r"\W+", " ", str(value or "").lower()).strip()


def _section(value: Any, description: str | None) -> str | None:
    """Store a JD section only when the description does not already contain it.

    Sections are normally excerpts of the description and can be re-derived
    from it; storing them again roughly doubled the table's text volume.
    """
    text = _text(value)
    if text and description and _words(text) in _words(description):
        return None
    return text


def _slim_extraction(extraction: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep extracted values and their provenance, not the evidence snippets.

    Evidence text and source_fields copies are reproducible from the stored
    description and board fields, and dominate the JSON's size.
    """
    if extraction is None:
        return None
    pick = lambda item, keys: {k: item[k] for k in keys if isinstance(item, dict) and item.get(k) is not None}
    value_keys = ("value", "status", "source")
    slim: dict[str, Any] = {
        "version": extraction.get("version"),
        "input_hash": extraction.get("input_hash"),
        "description_sha256": extraction.get("description_sha256"),
        "salary_status": extraction.get("salary_status"),
        "salary_candidates": [pick(c, ("min", "max", "currency", "period", "component", "location", "status", "source"))
                              for c in extraction.get("salary_candidates") or []],
        "skills": [pick(s, ("name", "requirement", "source")) for s in extraction.get("skills") or []],
        "experience": [pick(e, ("min", "max", "requirement")) for e in extraction.get("experience") or []],
        "education": [pick(e, ("value", "requirement")) for e in extraction.get("education") or []],
    }
    for key in ("work_mode", "job_type"):
        if isinstance(extraction.get(key), dict):
            slim[key] = pick(extraction[key], value_keys)
    return {k: v for k, v in slim.items() if v not in (None, [], {})}


# ---------------------------------------------------------------------------
# Core mapper
# ---------------------------------------------------------------------------

def map_job(raw: dict) -> dict:
    """Map a raw scraper job dict to a Neon public.jobs row dict."""
    # 1. Apply field aliases. A canonical field always wins over an alias,
    # whatever the column order: rows carry both `skills` (every skill) and
    # `skills_required`, and the alias must not shadow the real value.
    job: dict[str, Any] = {key: val for key, val in raw.items() if _FIELD_ALIASES.get(key, key) == key}
    for key, val in raw.items():
        canonical = _FIELD_ALIASES.get(key, key)
        if canonical != key and job.get(canonical) in (None, ""):
            job[canonical] = val

    extraction = _slim_extraction(_normalize_json_object(job.get("extraction")))
    description = _text(job.get("description") or job.get("job_description"))

    # Required = explicit requirements plus unqualified mentions, never the
    # skills a JD explicitly marks as preferred.
    preferred = _normalize_skills(job.get("skills_preferred"))
    required = _normalize_skills(job.get("skills_required"))
    for skill in _normalize_skills(raw.get("skills")):
        if skill not in required and skill not in preferred:
            required.append(skill)

    return {
        "id":                      _stable_id(raw),
        "job_title":               _text(job.get("job_title") or job.get("title")) or "",
        "company":                 _text(job.get("company") or job.get("company_name")),
        "description":             description,
        "skills_required":         Jsonb(required),
        "skills_preferred":        Jsonb(preferred),
        "experience_min":          _parse_float(job.get("experience_min")),
        "experience_max":          _parse_float(job.get("experience_max")),
        "experience":              _text(job.get("experience")),
        "location":                _text(job.get("location")),
        "country":                 _text(job.get("country")),
        "work_mode":               _normalize_work_mode(job.get("work_mode")),
        "is_remote":               _normalize_is_remote(job.get("is_remote") or job.get("remote")),
        "job_type":                _normalize_job_type(job.get("job_type") or job.get("employment_type")),
        "role_category":           _text(job.get("role_category")),
        "status":                  job.get("status") or "active",
        "salary":                  _text(job.get("salary")),
        "salary_min":              _parse_float(job.get("salary_min")),
        "salary_max":              _parse_float(job.get("salary_max")),
        "salary_currency":         _text(job.get("salary_currency")),
        "salary_period":           _text(job.get("salary_period")),
        # Never substitute scrape time: an unknown posting date stays unknown
        # instead of making every job look freshly posted.
        "posted_at":               _parse_posted_at(job.get("posted_at") or job.get("created_at")),
        "source_board":            job.get("source_board") or job.get("scraper_type"),
        "basic_qualifications":    _section(job.get("basic_qualifications"), description),
        "preferred_qualifications": _section(job.get("preferred_qualifications"), description),
        "key_responsibilities":    _section(job.get("key_responsibilities"), description),
        "extraction":              Jsonb(extraction) if extraction is not None else None,
        "job_url":                 _text(job.get("job_url") or job.get("url") or job.get("link")),
        "apply_url":               _text(job.get("apply_url") or job.get("apply_link")),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _vacuum(database_url: str) -> None:
    """Make dead row versions reusable so later batches do not grow the database."""
    try:
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute("VACUUM public.jobs")
    except Exception as exc:  # maintenance only; never block ingestion on it
        log.warning(f"  VACUUM skipped: {exc}")


def _expire_and_purge(conn, seen_count: int, expire_days: int, purge_days: int | None) -> None:
    with conn.cursor() as cur:
        active = cur.execute("SELECT count(*) FROM public.jobs WHERE status = 'active'").fetchone()[0]
        # A partial run (one source, or a broken scraper) must not expire the rest.
        if seen_count < 0.5 * active:
            log.warning(f"Expiry skipped: run saw {seen_count} jobs, under half of {active} active.")
            return
        cur.execute(EXPIRE_SQL, {"days": expire_days})
        log.info(f"Expired {cur.rowcount} jobs not seen for {expire_days} days.")
        if purge_days is not None:
            if cur.execute("SELECT to_regclass('public.user_job_recommendations')").fetchone()[0] is None:
                log.warning("Purge skipped: cannot verify saved/applied jobs are protected.")
            else:
                cur.execute(PURGE_SQL, {"days": purge_days})
                log.info(f"Purged {cur.rowcount} jobs expired more than {purge_days} days ago.")
    conn.commit()


def save_jobs(jobs: list[dict], batch_size: int = 500, *, expire_missing: bool = False,
              vacuum_every: int = 10_000) -> int:
    """
    Upsert jobs into public.jobs on Neon.

    Parameters
    ----------
    jobs           : list of raw scraper job dicts
    batch_size     : rows per executemany call (500 is a safe default)
    expire_missing : after a fully successful upsert, mark jobs not seen for
                     JOB_EXPIRE_DAYS (default 21) as expired. Deleting expired
                     jobs only happens when JOB_PURGE_EXPIRED_DAYS is set.
    vacuum_every   : VACUUM after this many written rows to bound bloat.

    Returns
    -------
    Number of rows successfully upserted.

    Raises
    ------
    KeyError     – if NEON_DATABASE_URL is not set (intentional; fail loudly)
    RuntimeError – if any batch failed; successful batches stay committed.
    """
    database_url = os.environ["NEON_DATABASE_URL"]  # fail if missing

    if not jobs:
        log.info("save_jobs: no jobs to upsert.")
        return 0

    rows = [map_job(j) for j in jobs]

    # De-duplicate by id (keep last seen)
    seen: dict[str, dict] = {}
    for row in rows:
        if row["job_title"]:  # job_title is the only required content field
            seen[row["id"]] = row
    unique_rows = list(seen.values())

    log.info(f"Upserting {len(unique_rows)} unique jobs to Neon public.jobs ...")
    total = since_vacuum = 0
    failures: list[str] = []

    with psycopg.connect(database_url) as conn:
        conn.execute(MIGRATION_SQL)
        conn.commit()
        with conn.cursor() as cur:
            for i in range(0, len(unique_rows), batch_size):
                batch = unique_rows[i : i + batch_size]
                number = i // batch_size + 1
                try:
                    cur.executemany(UPSERT_SQL, batch)
                    conn.commit()
                    total += len(batch)
                    since_vacuum += len(batch)
                    log.info(f"  ✓ Batch {number}: {len(batch)} rows upserted.")
                except Exception as exc:
                    conn.rollback()
                    failures.append(f"batch {number}: {exc}")
                    log.error(f"  ✗ Batch {number} failed: {exc}")
                if since_vacuum >= vacuum_every:
                    _vacuum(database_url)
                    since_vacuum = 0
        if since_vacuum:
            _vacuum(database_url)
        if expire_missing and not failures:
            purge = os.getenv("JOB_PURGE_EXPIRED_DAYS")
            _expire_and_purge(conn, len(unique_rows), int(os.getenv("JOB_EXPIRE_DAYS", "21")),
                              int(purge) if purge else None)

    log.info(f"Neon upsert complete. Total: {total} rows.")
    if failures:
        raise RuntimeError(f"{len(failures)} of {-(-len(unique_rows) // batch_size)} batches failed "
                           f"({total}/{len(unique_rows)} rows written). First: {failures[0]}")
    return total


# ---------------------------------------------------------------------------
# CLI helper (for direct testing: python -m db.neon_upsert jobs.json)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print("Usage: python -m db.neon_upsert <jobs.json>")
        sys.exit(1)

    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)

    jobs_list = data if isinstance(data, list) else []
    n = save_jobs(jobs_list)
    print(f"Done. {n} jobs upserted.")
