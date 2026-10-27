"""
db/neon_upsert.py
=================
Push scraped jobs to the Neon Postgres `public.jobs` table.

Environment
-----------
NEON_DATABASE_URL   – required. Postgres connection string of the main jobs database
                      (it also holds the users' saved and applied jobs).
JOBS_DATABASE_URL_2 … JOBS_DATABASE_URL_9
                    – optional further Neon projects that store more jobs. Jobs already
                      stored stay where they are; new jobs go where there is most room
                      (JOBS_DB_MAX_MB, or JOBS_DB_MAX_MB_<n> per database). The matching
                      API searches all of them.

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
from datetime import datetime, timedelta, timezone
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
    # LinkedIn: whether the job is a repost and when it was first posted (linkedin_reposts.py), and the
    # person who posted it ("Meet the hiring team", read from the public job page)
    "reposted", "first_posted_at", "poster_name", "poster_title", "poster_url",
)

# Assigning EXCLUDED.<col> writes a fresh TOAST copy even when the text is
# identical, so a weekly re-upsert used to double the table's storage. Keeping
# the stored value whenever it is unchanged reuses the existing TOAST data.
LARGE_COLUMNS = ("description", "basic_qualifications", "preferred_qualifications",
                 "key_responsibilities", "extraction")

# Everything read out of the description. A re-scrape that comes back without the
# description (Workday fetches details only for new postings; a detail request can time
# out) keeps the stored values instead of wiping them.
DESCRIPTION_COLUMNS = ("description", "skills_required", "skills_preferred", "experience_min", "experience_max",
                       "experience", "salary", "salary_min", "salary_max", "salary_currency", "salary_period",
                       "basic_qualifications", "preferred_qualifications", "key_responsibilities", "extraction")
# Board fields that the description may have supplied: keep the stored one when the thin row has none.
_FILL_COLUMNS = ("work_mode", "is_remote", "job_type")
_THIN = "(nullif(EXCLUDED.description, '') IS NULL AND nullif(jobs.description, '') IS NOT NULL)"

_CONTENT_COLUMNS = [column for column in COLUMNS if column != "id"]


def _new_value(column: str) -> str:
    """The value a conflicting row ends up with."""
    if column == "posted_at":
        # A posting date only moves back: boards report their last-edit time
        # (Greenhouse updated_at), and an edit must not make an old job look new.
        # LEAST ignores NULL, so a newly found date still fills an empty one.
        return "LEAST(jobs.posted_at, EXCLUDED.posted_at)"
    if column == "first_posted_at":
        return "LEAST(jobs.first_posted_at, EXCLUDED.first_posted_at)"
    if column == "reposted":
        return "coalesce(EXCLUDED.reposted, jobs.reposted)"
    if column.startswith("poster_"):
        # A page fetched without its hiring team (or a non-LinkedIn row) keeps the poster found before
        return f"coalesce(nullif(EXCLUDED.{column}, ''), jobs.{column})"
    if column in DESCRIPTION_COLUMNS:
        return f"CASE WHEN {_THIN} THEN jobs.{column} ELSE EXCLUDED.{column} END"
    if column in _FILL_COLUMNS:
        return f"CASE WHEN {_THIN} THEN coalesce(nullif(EXCLUDED.{column}::text, ''), jobs.{column}::text)::{_TYPES[column]} ELSE EXCLUDED.{column} END"
    return f"EXCLUDED.{column}"


_TYPES = {"work_mode": "text", "is_remote": "boolean", "job_type": "text"}
_CHANGED = ("(" + ", ".join(f"jobs.{c}" for c in _CONTENT_COLUMNS) + ") IS DISTINCT FROM ("
            + ", ".join(_new_value(c) for c in _CONTENT_COLUMNS) + ")")


def _assignment(column: str) -> str:
    value = _new_value(column)
    if column in LARGE_COLUMNS:
        return (f"{column} = CASE WHEN jobs.{column} IS DISTINCT FROM {value} "
                f"THEN {value} ELSE jobs.{column} END")
    return f"{column} = {value}"


UPSERT_SQL = (
    "INSERT INTO public.jobs (" + ", ".join(COLUMNS) + ", last_seen_at)\n"
    "VALUES (" + ", ".join(f"%({c})s" for c in COLUMNS) + ", now())\n"
    "ON CONFLICT (id) DO UPDATE SET\n    "
    + ",\n    ".join(_assignment(c) for c in _CONTENT_COLUMNS)
    + f",\n    updated_at = CASE WHEN {_CHANGED} THEN now() ELSE jobs.updated_at END"
    + ",\n    last_seen_at = now();"
)

# Idempotent and metadata-only (no table rewrite) on PostgreSQL 11+.
MIGRATION_SQL = """ALTER TABLE public.jobs ADD COLUMN IF NOT EXISTS last_seen_at timestamptz,
  ADD COLUMN IF NOT EXISTS reposted boolean, ADD COLUMN IF NOT EXISTS first_posted_at timestamptz,
  ADD COLUMN IF NOT EXISTS poster_name text, ADD COLUMN IF NOT EXISTS poster_title text,
  ADD COLUMN IF NOT EXISTS poster_url text"""

# Soft expiry only: a job that reappears is reactivated by the next upsert. A run that scraped
# only some boards (boards = their lower-cased names) only retires jobs of those boards.
_IN_BOARDS = "(%(boards)s::text[] IS NULL OR lower(source_board) = ANY(%(boards)s::text[]))"
EXPIRE_SQL = f"""
UPDATE public.jobs SET status = 'expired', updated_at = now()
WHERE status = 'active'
  AND coalesce(last_seen_at, updated_at) < now() - make_interval(days => %(days)s)
  AND {_IN_BOARDS}
"""

# Deleting cascades to user feed rows, so never purge jobs a user saved or applied to.
PURGE_SQL = """
DELETE FROM public.jobs j
WHERE j.status = 'expired'
  AND j.updated_at <= now() - make_interval(days => %(days)s)
  AND NOT {protected}
RETURNING j.id
"""

# Recommendations cannot reference jobs in other databases by foreign key, so the rows of
# removed jobs are deleted here (saved and applied jobs are never removed).
FORGET_SQL = """
DELETE FROM public.user_job_recommendations
WHERE job_id = ANY(%(ids)s) AND status NOT IN ('saved', 'applied')
"""

# Further jobs databases get the same table (no triggers: matching reads public.jobs directly)
JOBS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS public.jobs (
  id text PRIMARY KEY, job_title text NOT NULL, company text, description text,
  skills_required jsonb NOT NULL DEFAULT '[]'::jsonb, skills_preferred jsonb NOT NULL DEFAULT '[]'::jsonb,
  experience_min real, experience_max real, experience text, location text, country text,
  work_mode text, is_remote boolean, job_type text, role_category text,
  status text NOT NULL DEFAULT 'active', salary text, salary_min real, salary_max real,
  salary_currency text, salary_period text, posted_at timestamptz, source_board text,
  basic_qualifications text, preferred_qualifications text, key_responsibilities text,
  extraction jsonb, job_url text, apply_url text,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz
)"""


# Recommendations may point at jobs in any database (same as matching/sql/multi_jobs_db.sql)
UNTIE_RECOMMENDATIONS_SQL = """
DO $$ BEGIN
  IF to_regclass('public.user_job_recommendations') IS NOT NULL THEN
    ALTER TABLE public.user_job_recommendations DROP CONSTRAINT IF EXISTS user_job_recommendations_job_id_fkey;
  END IF;
END $$"""


def job_database_urls() -> list[str]:
    """The main jobs database first, then any further ones (JOBS_DATABASE_URL_2 … _9)."""
    extra = [os.environ[key] for key in (f"JOBS_DATABASE_URL_{n}" for n in range(2, 10)) if os.getenv(key)]
    return [os.environ["NEON_DATABASE_URL"], *extra]


def _limit_mb(index: int) -> str:
    """Storage limit of jobs database number index+1 (JOBS_DB_MAX_MB_<n>, else JOBS_DB_MAX_MB)."""
    return (os.getenv(f"JOBS_DB_MAX_MB_{index + 1}") or os.getenv("JOBS_DB_MAX_MB", "")).strip()


def _copy_ids(cur, table: str, ids) -> None:
    cur.execute(f"CREATE TEMP TABLE IF NOT EXISTS {table} (id text PRIMARY KEY) ON COMMIT DROP")
    with cur.copy(f"COPY {table} (id) FROM STDIN") as copy:
        for job_id in ids:
            copy.write_row((job_id,))


def protected_job_ids(main_url: str) -> set[str]:
    """Jobs a user saved or applied to (kept in the main database's user tables)."""
    with psycopg.connect(main_url) as conn:
        if conn.execute("SELECT to_regclass('public.user_job_recommendations')").fetchone()[0] is None:
            return set()
        return {row[0] for row in conn.execute(
            "SELECT DISTINCT job_id FROM public.user_job_recommendations WHERE status IN ('saved', 'applied')")}


def place_jobs(rows: list[dict], urls: list[str]) -> list[list[dict]]:
    """Which database each job goes to: where it is already stored, else the one with most free room.

    New jobs are split in proportion to each database's room under its limit (evenly when every
    database is full), so all of them fill up together.
    """
    if len(urls) == 1:
        return [rows]
    ids = [row["id"] for row in rows]
    located: dict[str, int] = {}
    room: list[float] = []
    MiB = 1024 * 1024
    for index, url in enumerate(urls):
        with psycopg.connect(url) as conn:
            conn.execute(JOBS_TABLE_SQL if index else UNTIE_RECOMMENDATIONS_SQL)
            conn.execute(MIGRATION_SQL)
            with conn.cursor() as cur:
                _copy_ids(cur, "incoming_ids", ids)
                for (job_id,) in cur.execute("SELECT i.id FROM incoming_ids i JOIN public.jobs j ON j.id = i.id"):
                    located.setdefault(job_id, index)
                size = cur.execute("SELECT pg_database_size(current_database())").fetchone()[0]
            conn.commit()
        # Without a limit set, a database is weighed as a 1 GB Neon project (it is not trimmed though)
        limit = float(_limit_mb(index) or 1024)
        room.append(max(limit * MiB * float(os.getenv("JOBS_DB_SAFETY_PCT", "92")) / 100 - size, 0.0))
    new = [row for row in rows if row["id"] not in located]
    weights = room if sum(room) > 0 else [1.0] * len(urls)
    shares = [int(len(new) * weight / sum(weights)) for weight in weights]
    shares[max(range(len(urls)), key=lambda k: weights[k])] += len(new) - sum(shares)
    groups: list[list[dict]] = [[row for row in rows if located.get(row["id"]) == k] for k in range(len(urls))]
    start = 0
    for index, share in enumerate(shares):
        groups[index] += new[start:start + share]
        start += share
    for index, group in enumerate(groups):
        log.info(f"  Jobs database {index + 1}: {len(group)} jobs "
                 f"({sum(1 for row in group if row['id'] not in located)} new)")
    _summary("- Jobs placed: " + ", ".join(f"database {k + 1}: {len(g)}" for k, g in enumerate(groups)))
    return groups

# ---------------------------------------------------------------------------
# Storage budget: make room before writing
# ---------------------------------------------------------------------------

# Jobs a run may remove to make room, in this order: already expired first, then the
# longest unseen. Never a job seen in this run (incoming_ids) or one a user saved or
# applied to. Deleting cascades to feed rows, so those protections matter.
_LOCAL_PROTECTED = ("EXISTS (SELECT 1 FROM public.user_job_recommendations r "
                    "WHERE r.job_id = j.id AND r.status IN ('saved', 'applied'))")
# Other jobs databases have no user tables: the saved/applied ids are copied in from the main one
_COPIED_PROTECTED = "EXISTS (SELECT 1 FROM protected_ids p WHERE p.id = j.id)"

VICTIM_ORDER_SQL = """
FROM public.jobs j
WHERE NOT EXISTS (SELECT 1 FROM incoming_ids i WHERE i.id = j.id)
  AND NOT {protected}
ORDER BY (j.status = 'expired') DESC,
         coalesce(j.last_seen_at, j.updated_at) ASC,
         coalesce(j.posted_at, j.created_at) ASC
LIMIT %(count)s
"""

_TOAST_OVERHEAD = 1.08  # chunk headers and page padding on top of the stored size of long text


def rows_to_free(*, live_bytes: float, new_bytes: float, physical_jobs_bytes: float,
                 headroom_bytes: float, avg_row_bytes: float, buffer: float = 1.05) -> int:
    """How many existing rows must go so `new_bytes` of new rows fit without outgrowing the budget.

    Deleted rows leave space inside the files that later inserts reuse (after VACUUM), so the
    database only grows when live data exceeds what the files already hold plus the headroom
    left under the budget. Pure arithmetic, so it is easy to test.
    """
    overflow = (live_bytes + new_bytes) - (physical_jobs_bytes + headroom_bytes)
    if overflow <= 0 or avg_row_bytes <= 0:
        return 0
    return int(-(-overflow * buffer // avg_row_bytes))


def _summary(text: str) -> None:
    """Show a line in the GitHub Actions run summary, when running there."""
    path = os.getenv("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")


def _storage_numbers(cur) -> dict:
    """Cost of one job, measured from the real table: the main table and indexes per row,
    plus the long text (stored out of line) from a sample."""
    row_count = cur.execute("SELECT count(*) FROM public.jobs").fetchone()[0]
    # Only text over ~2 KB is stored out of line; shorter values already sit in the main
    # table's rows, which are measured by the heap size below (counting them twice overshot).
    long_text = " + ".join(
        f"CASE WHEN pg_column_size({col}) > 2000 THEN pg_column_size({col}) ELSE 0 END"
        for col in ("description", "extraction", "basic_qualifications", "preferred_qualifications", "key_responsibilities"))
    sample = cur.execute(f"SELECT avg({long_text}) FROM public.jobs TABLESAMPLE SYSTEM (2)").fetchone()[0]
    if sample is None:  # tiny tables can sample empty
        sample = cur.execute(f"SELECT avg({long_text}) FROM public.jobs").fetchone()[0] or 0
    heap = cur.execute("SELECT pg_relation_size('public.jobs')").fetchone()[0]
    indexes = cur.execute("SELECT pg_indexes_size('public.jobs')").fetchone()[0]
    total_jobs = cur.execute("SELECT pg_total_relation_size('public.jobs')").fetchone()[0]
    database = cur.execute("SELECT pg_database_size(current_database())").fetchone()[0]
    per_row = ((heap + indexes) / row_count + float(sample) * _TOAST_OVERHEAD) if row_count else 0.0
    return {"rows": row_count, "avg_row_bytes": per_row, "live_bytes": per_row * row_count,
            "physical_jobs_bytes": total_jobs, "database_bytes": database}


def make_room(conn, incoming_ids: list[str], *, database_url: str | None = None, index: int = 0,
              protected_ids: set[str] | None = None, removed: list[str] | None = None) -> int:
    """Delete the stalest jobs so this run's new jobs fit under JOBS_DB_MAX_MB. Returns rows deleted.

    Environment:
      JOBS_DB_MAX_MB          storage limit of the Neon project in MB; unset = no limit enforced
      JOBS_DB_SAFETY_PCT      use at most this share of the limit (default 92)
      JOB_RETENTION_MAX_DELETE_PCT  most one run may delete, as a share of the table (default 35)
      JOB_RETENTION_DRY_RUN   "true" = only report what would be deleted
    """
    limit_mb = _limit_mb(index)
    if not limit_mb:
        return 0
    MiB = 1024 * 1024
    budget = float(limit_mb) * MiB * float(os.getenv("JOBS_DB_SAFETY_PCT", "92")) / 100
    dry_run = os.getenv("JOB_RETENTION_DRY_RUN", "").lower() == "true"
    # Dead row versions only count as reusable space once vacuumed; measure after that.
    conn.commit()
    _vacuum(database_url or os.environ["NEON_DATABASE_URL"])
    with conn.cursor() as cur:
        if protected_ids is None:
            if cur.execute("SELECT to_regclass('public.user_job_recommendations')").fetchone()[0] is None:
                log.warning("Storage budget skipped: cannot verify saved/applied jobs are protected.")
                return 0
            protected = _LOCAL_PROTECTED
        else:
            _copy_ids(cur, "protected_ids", protected_ids)
            protected = _COPIED_PROTECTED
        victims = VICTIM_ORDER_SQL.format(protected=protected)
        numbers = _storage_numbers(cur)
        cur.execute("CREATE TEMP TABLE incoming_ids (id text PRIMARY KEY) ON COMMIT DROP")
        with cur.copy("COPY incoming_ids (id) FROM STDIN") as copy:
            for job_id in incoming_ids:
                copy.write_row((job_id,))
        new_rows = cur.execute(
            "SELECT count(*) FROM incoming_ids i WHERE NOT EXISTS (SELECT 1 FROM public.jobs j WHERE j.id = i.id)"
        ).fetchone()[0]
        headroom = max(0.0, budget - numbers["database_bytes"])
        needed = rows_to_free(live_bytes=numbers["live_bytes"], new_bytes=new_rows * numbers["avg_row_bytes"],
                              physical_jobs_bytes=numbers["physical_jobs_bytes"], headroom_bytes=headroom,
                              avg_row_bytes=numbers["avg_row_bytes"])
        line = (f"Storage: database {numbers['database_bytes'] / MiB:.0f} MB of {float(limit_mb):.0f} MB "
                f"(budget {budget / MiB:.0f} MB), {numbers['rows']} jobs, {new_rows} new in this run, "
                f"~{numbers['avg_row_bytes'] / 1024:.1f} KB per job.")
        log.info(line)
        if needed == 0:
            _summary(f"- {line} Enough room, nothing removed.")
            conn.rollback()
            return 0
        cap = int(numbers["rows"] * float(os.getenv("JOB_RETENTION_MAX_DELETE_PCT", "35")) / 100)
        count = min(needed, cap)
        if count < needed:
            log.warning(f"Needs to free {needed} jobs but one run may remove at most {cap}; removing {cap}.")
        if dry_run:
            rows = cur.execute(f"SELECT j.source_board, j.status, count(*) FROM (SELECT j.* {victims}) j "
                               "GROUP BY 1, 2 ORDER BY 3 DESC", {"count": count}).fetchall()
            log.info(f"DRY RUN: would remove {count} jobs: {rows}")
            _summary(f"- {line} **Dry run:** would remove {count} stalest jobs: {rows}")
            conn.rollback()
            return 0
        cur.execute(f"DELETE FROM public.jobs WHERE id IN (SELECT j.id {victims}) RETURNING id", {"count": count})
        gone = [row[0] for row in cur.fetchall()]
        deleted = len(gone)
    conn.commit()
    if removed is not None:
        removed += gone
    log.info(f"Removed {deleted} of the stalest jobs to make room for {new_rows} new ones.")
    _summary(f"- {line} Removed **{deleted}** stalest jobs (expired or longest unseen) to make room.")
    return deleted


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


# Scrapers that find no date fall back to datetime.now() (Greenhouse, Lever,
# Ashby, Teamtailor, the Google-based ones), which reads as "posted today".
# A "posting date" this close to the scrape is that fallback, not a date.
_SCRAPE_FALLBACK = timedelta(minutes=10)


def _posted_at(job: dict[str, Any]) -> datetime | None:
    """The board's posting date, or None when it is missing, invented or in the future.

    None is honest: readers fall back to jobs.created_at, when we first saw the job.
    """
    posted = _parse_posted_at(job.get("posted_at") or job.get("created_at"))
    if posted is None:
        return None
    now = datetime.now(timezone.utc)
    if posted > now + timedelta(days=1):
        return None
    scraped = _parse_posted_at(job.get("scraped_at"))
    if scraped is not None and abs(posted - scraped) <= _SCRAPE_FALLBACK:
        return None
    return posted


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
        "posted_at":               _posted_at(job),
        "source_board":            job.get("source_board") or job.get("scraper_type"),
        "basic_qualifications":    _section(job.get("basic_qualifications"), description),
        "preferred_qualifications": _section(job.get("preferred_qualifications"), description),
        "key_responsibilities":    _section(job.get("key_responsibilities"), description),
        "extraction":              Jsonb(extraction) if extraction is not None else None,
        "job_url":                 _text(job.get("job_url") or job.get("url") or job.get("link")),
        "apply_url":               _text(job.get("apply_url") or job.get("apply_link")),
        "reposted":                _normalize_reposted(job.get("reposted")),
        "first_posted_at":         _parse_posted_at(job.get("first_posted_at")),
        "poster_name":             _text(job.get("poster_name")),
        "poster_title":            _text(job.get("poster_title")),
        "poster_url":              _text(job.get("poster_url")),
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


def _expire_and_purge(conn, seen_count: int, expire_days: int, purge_days: int | None,
                      protected_ids: set[str] | None = None, removed: list[str] | None = None,
                      boards: list[str] | None = None) -> None:
    scope = {"boards": [board.lower() for board in boards] if boards else None}
    with conn.cursor() as cur:
        active = cur.execute(f"SELECT count(*) FROM public.jobs WHERE status = 'active' AND {_IN_BOARDS}",
                             scope).fetchone()[0]
        # A partial run (one source, or a broken scraper) must not expire the rest.
        if seen_count < 0.5 * active:
            log.warning(f"Expiry skipped: run saw {seen_count} jobs, under half of {active} active.")
            return
        cur.execute(EXPIRE_SQL, {"days": expire_days, **scope})
        log.info(f"Expired {cur.rowcount} jobs not seen for {expire_days} days.")
        if purge_days is not None:
            protected = None
            if protected_ids is not None:
                _copy_ids(cur, "protected_ids", protected_ids)
                protected = _COPIED_PROTECTED
            elif cur.execute("SELECT to_regclass('public.user_job_recommendations')").fetchone()[0] is None:
                log.warning("Purge skipped: cannot verify saved/applied jobs are protected.")
            else:
                protected = _LOCAL_PROTECTED
            if protected:
                cur.execute(PURGE_SQL.format(protected=protected), {"days": purge_days})
                gone = [row[0] for row in cur.fetchall()]
                if removed is not None:
                    removed += gone
                log.info(f"Purged {len(gone)} jobs expired more than {purge_days} days ago.")
    conn.commit()


def save_jobs(jobs: list[dict], batch_size: int = 500, *, expire_missing: bool = False,
              expire_boards: list[str] | None = None, vacuum_every: int = 10_000) -> int:
    """
    Upsert jobs into public.jobs on Neon.

    Parameters
    ----------
    jobs           : list of raw scraper job dicts
    batch_size     : rows per executemany call (500 is a safe default)
    expire_missing : after a fully successful upsert, mark jobs not seen for
                     JOB_EXPIRE_DAYS (default 14, two missed weekly runs) as
                     expired, and delete jobs expired for JOB_PURGE_EXPIRED_DAYS
                     (default 7; set it empty to keep expired jobs). Saved and
                     applied jobs are never deleted.
    expire_boards  : boards this run scraped (e.g. ["Greenhouse", "LinkedIn"]); only their
                     jobs are expired. None = every board.
    vacuum_every   : VACUUM after this many written rows to bound bloat.

    Returns
    -------
    Number of rows successfully upserted.

    Raises
    ------
    KeyError     – if NEON_DATABASE_URL is not set (intentional; fail loudly)
    RuntimeError – if any batch failed; successful batches stay committed.
    """
    urls = job_database_urls()  # fails if NEON_DATABASE_URL is missing

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
    _mark_linkedin_reposts(unique_rows, urls[0])

    log.info(f"Upserting {len(unique_rows)} unique jobs to Neon public.jobs "
             f"({len(urls)} database{'s' if len(urls) > 1 else ''}) ...")
    groups = place_jobs(unique_rows, urls)
    # Further databases protect what users saved or applied to in the main one
    protected = protected_job_ids(urls[0]) if len(urls) > 1 else None
    total, failures, removed = 0, [], []
    for index, (url, group) in enumerate(zip(urls, groups)):
        if not group:
            continue
        written, failed = _save_into(url, group, index, batch_size=batch_size, expire_missing=expire_missing,
                                     vacuum_every=vacuum_every, protected_ids=protected if index else None,
                                     removed=removed, expire_boards=expire_boards)
        total += written
        failures += [f"database {index + 1} {failure}" for failure in failed]
    if removed and len(urls) > 1:
        _forget_recommendations(urls[0], removed)

    log.info(f"Neon upsert complete. Total: {total} rows.")
    if failures:
        raise RuntimeError(f"{len(failures)} of {-(-len(unique_rows) // batch_size)} batches failed "
                           f"({total}/{len(unique_rows)} rows written). First: {failures[0]}")
    return total


def _normalize_reposted(value) -> bool | None:
    """CSV booleans survive ingestion; blank or missing flags stay unknown."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
        return value.strip().lower() == 'true'
    return None


def _mark_linkedin_reposts(rows: list[dict], main_url: str) -> None:
    """Preserve scraped flags; use bounded ID estimates only where evidence is absent."""
    linkedin = [row for row in rows if (row.get("source_board") or "").lower() == "linkedin"]
    if not linkedin:
        return
    try:
        from db import linkedin_reposts
    except ImportError:
        import linkedin_reposts  # run from inside db/
    try:
        with psycopg.connect(main_url) as conn:
            calibration = linkedin_reposts.load(conn)
            reposts = linkedin_reposts.annotate(linkedin, calibration)
            linkedin_reposts.learn(conn, linkedin_reposts.todays_points(linkedin))
            conn.commit()
        log.info(f"LinkedIn: {reposts} of {len(linkedin)} jobs are reposts")
        _summary(f"- LinkedIn repost flags: {reposts} of {len(linkedin)} (explicit evidence or ID estimates)")
    except Exception as exc:  # the flag is extra information; never block saving jobs on it
        log.warning(f"  Repost check skipped: {exc}")


def _forget_recommendations(main_url: str, removed: list[str]) -> None:
    try:
        with psycopg.connect(main_url) as conn:
            if conn.execute("SELECT to_regclass('public.user_job_recommendations')").fetchone()[0] is None:
                return
            deleted = conn.execute(FORGET_SQL, {"ids": removed}).rowcount
            conn.commit()
        log.info(f"Removed {deleted} recommendations of {len(removed)} deleted jobs.")
    except Exception as exc:  # leftovers only cost a little space; never fail the run on them
        log.warning(f"  Recommendation cleanup skipped: {exc}")


def _save_into(database_url: str, unique_rows: list[dict], index: int, *, batch_size: int, expire_missing: bool,
               vacuum_every: int, protected_ids: set[str] | None, removed: list[str] | None = None,
               expire_boards: list[str] | None = None) -> tuple[int, list[str]]:
    """Upsert one database's share of the jobs, making room and expiring old jobs there."""
    total = since_vacuum = 0
    failures: list[str] = []
    with psycopg.connect(database_url) as conn:
        if index:
            conn.execute(JOBS_TABLE_SQL)
        conn.execute(MIGRATION_SQL)
        conn.commit()
        make_room(conn, [row["id"] for row in unique_rows], database_url=database_url, index=index,
                  protected_ids=protected_ids, removed=removed)
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
            # Expired jobs are deleted after a week by default; without this the table only grew.
            purge = os.getenv("JOB_PURGE_EXPIRED_DAYS", "7")
            _expire_and_purge(conn, len(unique_rows), int(os.getenv("JOB_EXPIRE_DAYS", "14")),
                              int(purge) if purge.strip() else None, protected_ids, removed, expire_boards)
    return total, failures


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
