from __future__ import annotations
import html
import re
from datetime import datetime, timezone
from typing import Any


REMOTE_PATTERNS = [
    r"\bremote\b",
    r"\bwork\s+from\s+home\b",
    r"\bwfh\b",
    r"\banywhere\b",
]

HYBRID_PATTERNS = [
    r"\bhybrid\b",
    r"\b\d+\s+days?\s+(?:a\s+)?week\s+(?:in|at)\s+(?:the\s+)?office\b",
]

ONSITE_PATTERNS = [
    r"\bonsite\b",
    r"\bon-site\b",
    r"\bin[-\s]?office\b",
    r"\bin[-\s]?person\b",
]

JOB_TYPE_PATTERNS = {
    # (?<!non-) excludes "non-internship experience" (a common phrase in
    # postings requiring PRIOR professional, not intern-level, experience).
    "internship": [r"(?<!non-)\binternship\b", r"(?<!non-)\bintern\b", r"\bco-op\b", r"\bworking student\b", r"\bsummer intern\b"],
    "contract": [r"\bcontract\b", r"\bcontractor\b", r"\bc2c\b", r"\bw2\b", r"\b1099\b", r"\bfixed term\b"],
    "part_time": [r"\bpart-time\b", r"\bpart\s+time\b"],
    "freelance": [r"\bfreelance\b", r"\bfreelancer\b"],
    "temporary": [r"\btemp\b", r"\btemporary\b"],
    "full_time": [r"\bfull-time\b", r"\bfull\s+time\b", r"\bpermanent\b"]
}

COMMON_SKILLS = [
    "Python",
    "Java",
    "C++",
    "C#",
    "Go",
    "Golang",
    "Rust",
    "JavaScript",
    "TypeScript",
    "React",
    "Angular",
    "Vue",
    "Node.js",
    "Node",
    "Next.js",
    "Django",
    "Flask",
    "FastAPI",
    "SQL",
    "PostgreSQL",
    "MySQL",
    "MongoDB",
    "NoSQL",
    "Redis",
    "AWS",
    "GCP",
    "Azure",
    "Docker",
    "Kubernetes",
    "Terraform",
    "Machine Learning",
    "Deep Learning",
    "NLP",
    "LLM",
    "PyTorch",
    "TensorFlow",
    "Pandas",
    "Spark",
    "Kafka",
]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_iso_datetime(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except ValueError:
        return text


def extract_posted_at(raw: dict[str, Any]) -> str | None:
    for key in (
        "posted_at",
        "published_at",
        "publishedAt",
        "datePosted",
        "created_at",
        "createdAt",
        "updated_at",
        "updatedAt",
        "releasedDate",
        "published_on",
        "timestamp",
    ):
        parsed = parse_iso_datetime(raw.get(key))
        if parsed:
            return parsed
    return None


# Stripped from the content before matching REMOTE_PATTERNS: "remote
# locations/areas" describe geography (e.g. "serving customers in remote
# locations"), not a work arrangement; "not/non/no remote" is a negation
# that a bare \bremote\b match can't distinguish from an affirmative one.
_REMOTE_GEOGRAPHIC_RE = re.compile(r"\bremote\s+(?:location|area|region|site|communit)\w*\b")
_REMOTE_NEGATION_RE = re.compile(r"\b(?:not|non|no|isn't|aren't|without)[\s-]{0,15}?(?:a\s+|fully\s+|100%\s+)?remote\b")


def extract_work_mode(title: str = "", location: str = "", description: str = "") -> str:
    from job_extraction.engine import work_mode
    return work_mode("\n".join((title, location, description)))["value"] or ""


def extract_job_type(title: str = "", description: str = "") -> str:
    from job_extraction.engine import employment_type
    return employment_type(title + "\n" + description)["value"] or ""


def extract_salary(text: str, compensation: dict[str, Any] | None = None) -> str:
    from job_extraction.engine import extract_job
    return extract_job({"description": text, "compensation": compensation})["salary"]


def detect_salary_currency(salary_text: str) -> str:
    from job_extraction.engine import currency_of
    return currency_of(salary_text) or ""


def _expand_salary_number(value: str) -> int | float:
    from job_extraction.engine import _MONEY, amount
    match = _MONEY.search(value)
    if not match:
        raise ValueError("No amount")
    return amount(match["n1"], match["u1"])


def parse_salary_range(salary: str) -> tuple[int | float | None, int | float | None]:
    from job_extraction.engine import salary_candidates
    candidates = salary_candidates("Salary: " + salary)
    return (candidates[0]["min"], candidates[0]["max"]) if len(candidates) == 1 else (None, None)


def extract_experience(text: str) -> str:
    cleaned = clean_text(text)
    patterns = [
        r"\b\d+\s*(?:\+|plus)?\s*(?:-|to|–|—)?\s*\d*\s*(?:\+|plus)?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:relevant\s+)?experience\b",
        r"\b\d+\s*(?:\+|plus)?\s*(?:-|to|–|—)?\s*\d*\s*(?:\+|plus)?\s*(?:years?|yrs?)\s+experience\b",
        r"\b(?:minimum|min\.?|at least)\s+\d+\s*(?:\+|plus)?\s*(?:years?|yrs?)\b",
        r"\b\d+\s*(?:\+|plus)?\s*(?:years?|yrs?)\s+(?:building|working|developing|engineering|in)\b",
        r"\b\d+\s*(?:\+|plus)\s*(?:years?|yrs?)\b",  # Standalone "5+ years"
        r"\b\d+\s*-\s*\d+\s*(?:years?|yrs?)\b",  # Standalone "3-5 years"
        # Levels
        r"\bentry[- ]level\b",
        r"\bmid[- ]level\b",
        r"\bsenior[- ]level\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            return match.group(0).strip(" .,:;")
    return ""


def parse_experience_range(experience: str) -> tuple[int | None, int | None]:
    if not experience:
        return None, None
        
    exp = experience.lower()
    
    # Handle text keywords
    if "entry" in exp:
        return 0, 2
    if "mid" in exp:
        return 3, 5
    if "senior" in exp and not re.search(r"\d", exp):
        return 5, None

    # Handle numeric patterns
    # Range: "3-5 years"
    range_match = re.search(r"(\d+)\s*(?:-|to|–|—)\s*(\d+)", exp)
    if range_match:
        return int(range_match.group(1)), int(range_match.group(2))
        
    # Min/Plus: "5+ years", "minimum 3 years"
    min_match = re.search(r"(\d+)", exp)
    if min_match:
        val = int(min_match.group(1))
        if "+" in exp or "plus" in exp or "min" in exp or "least" in exp:
            return val, None
        return val, val

    return None, None


def extract_skills(text: str) -> str:
    from job_extraction.engine import skill_candidates
    return ", ".join(dict.fromkeys(s["name"] for s in skill_candidates(text)))


def enrich_raw_job(raw: dict[str, Any]) -> dict[str, Any]:
    from job_extraction import extract_job
    enriched = extract_job(raw)
    enriched["job_title"] = clean_text(raw.get("job_title") or raw.get("title") or raw.get("jobTitle")) or "Unknown Title"
    enriched["location"] = clean_text(raw.get("location") or raw.get("location_str") or raw.get("jobLocation"))
    enriched["posted_at"] = extract_posted_at(raw)
    return enriched
