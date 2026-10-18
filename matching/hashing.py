from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Iterable


def normalize_text(value: Any) -> str:
    text = str(value or "").casefold()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^\w+#. -]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_work_mode(value: Any) -> str:
    text = normalize_text(value).replace("_", " ").replace("-", " ")
    if text in {"onsite", "on site", "in office", "in person"}:
        return "onsite"
    if text in {"remote", "hybrid"}:
        return text
    return normalize_text(value)


def canonical_employment_type(value: Any) -> str:
    text = normalize_text(value).replace("_", " ").replace("-", " ")
    aliases = {"fulltime": "full time", "parttime": "part time", "intern": "internship",
               "contractor": "contract", "fixed term": "contract", "freelance": "contract"}
    compact = text.replace(" ", "")
    return aliases.get(text, aliases.get(compact, text))


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def pick(source: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {field: source.get(field) for field in fields}


def relevant_source_hash(job: dict[str, Any]) -> str:
    """Hash matching inputs only: logos and scrape timestamps are deliberately absent."""
    return stable_hash(pick(job, (
        "job_title", "title", "description", "skills_required", "skills_preferred",
        "experience_min", "experience_max", "experience", "location", "country",
        "work_mode", "is_remote", "job_type", "role_category", "education_requirements",
        "salary_min", "salary_max", "salary_currency", "salary_period", "posted_at",
        "posting_date", "created_at", "source_board",
        "basic_qualifications", "preferred_qualifications", "key_responsibilities", "extraction",
        "job_url", "url", "apply_url",
    )))
