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

UPSERT_SQL = """
INSERT INTO public.jobs (
    id, job_title, company, description,
    skills_required, skills_preferred,
    experience_min, experience_max, experience,
    location, country, work_mode, is_remote,
    job_type, role_category, status,
    salary, salary_min, salary_max, salary_currency, salary_period,
    posted_at, source_board,
    basic_qualifications, preferred_qualifications, key_responsibilities,
    extraction, job_url, apply_url
)
VALUES (
    %(id)s, %(job_title)s, %(company)s, %(description)s,
    %(skills_required)s, %(skills_preferred)s,
    %(experience_min)s, %(experience_max)s, %(experience)s,
    %(location)s, %(country)s, %(work_mode)s, %(is_remote)s,
    %(job_type)s, %(role_category)s, %(status)s,
    %(salary)s, %(salary_min)s, %(salary_max)s, %(salary_currency)s, %(salary_period)s,
    %(posted_at)s, %(source_board)s,
    %(basic_qualifications)s, %(preferred_qualifications)s, %(key_responsibilities)s,
    %(extraction)s, %(job_url)s, %(apply_url)s
)
ON CONFLICT (id) DO UPDATE SET
    job_title              = EXCLUDED.job_title,
    company                = EXCLUDED.company,
    description            = EXCLUDED.description,
    skills_required        = EXCLUDED.skills_required,
    skills_preferred       = EXCLUDED.skills_preferred,
    experience_min         = EXCLUDED.experience_min,
    experience_max         = EXCLUDED.experience_max,
    experience             = EXCLUDED.experience,
    location               = EXCLUDED.location,
    country                = EXCLUDED.country,
    work_mode              = EXCLUDED.work_mode,
    is_remote              = EXCLUDED.is_remote,
    job_type               = EXCLUDED.job_type,
    role_category          = EXCLUDED.role_category,
    status                 = EXCLUDED.status,
    salary                 = EXCLUDED.salary,
    salary_min             = EXCLUDED.salary_min,
    salary_max             = EXCLUDED.salary_max,
    salary_currency        = EXCLUDED.salary_currency,
    salary_period          = EXCLUDED.salary_period,
    posted_at              = EXCLUDED.posted_at,
    source_board           = EXCLUDED.source_board,
    basic_qualifications   = EXCLUDED.basic_qualifications,
    preferred_qualifications = EXCLUDED.preferred_qualifications,
    key_responsibilities   = EXCLUDED.key_responsibilities,
    extraction             = EXCLUDED.extraction,
    job_url                = EXCLUDED.job_url,
    apply_url              = EXCLUDED.apply_url,
    updated_at             = now();
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

    extraction = _normalize_json_object(job.get("extraction"))

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
        "description":             _text(job.get("description") or job.get("job_description")),
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
        # Scrape time is only a last resort when no board posting date exists.
        "posted_at":               _parse_posted_at(job.get("posted_at") or job.get("created_at") or job.get("scraped_at")),
        "source_board":            job.get("source_board") or job.get("scraper_type"),
        "basic_qualifications":    _text(job.get("basic_qualifications")),
        "preferred_qualifications": _text(job.get("preferred_qualifications")),
        "key_responsibilities":    _text(job.get("key_responsibilities")),
        "extraction":              Jsonb(extraction) if extraction is not None else None,
        "job_url":                 _text(job.get("job_url") or job.get("url") or job.get("link")),
        "apply_url":               _text(job.get("apply_url") or job.get("apply_link")),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def save_jobs(jobs: list[dict], batch_size: int = 500) -> int:
    """
    Upsert jobs into public.jobs on Neon.

    Parameters
    ----------
    jobs        : list of raw scraper job dicts
    batch_size  : rows per executemany call (500 is a safe default)

    Returns
    -------
    Number of rows successfully upserted.

    Raises
    ------
    KeyError    – if NEON_DATABASE_URL is not set (intentional; fail loudly)
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
    total = 0

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            for i in range(0, len(unique_rows), batch_size):
                batch = unique_rows[i : i + batch_size]
                try:
                    cur.executemany(UPSERT_SQL, batch)
                    conn.commit()
                    total += len(batch)
                    log.info(f"  ✓ Batch {i // batch_size + 1}: {len(batch)} rows upserted.")
                except Exception as exc:
                    conn.rollback()
                    log.error(f"  ✗ Batch {i // batch_size + 1} failed: {exc}")

    log.info(f"Neon upsert complete. Total: {total} rows.")
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
