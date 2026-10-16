#!/usr/bin/env python3
"""Report field completeness for merged scraper outputs.

The audit is intentionally read-only. It accepts CSV or JSON files and glob
patterns, prints a compact Markdown table, and appends the same table to the
GitHub Actions step summary when available.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from pathlib import Path
from typing import Any, Iterable

FIELD_GROUPS: dict[str, tuple[str, ...]] = {
    "title": ("job_title", "title", "jobTitle"),
    "company": ("company", "company_name", "employer"),
    "location": ("location", "location_str", "jobLocation"),
    "url": ("job_url", "url", "job_link", "link"),
    "description": ("description", "job_description", "content"),
    "posted": ("posted_at", "date_posted", "created_at", "scraped_at"),
    "salary": ("salary", "salary_min", "salary_max", "compensation"),
    "skills": ("skills_required", "skills_preferred", "skills"),
    "work mode": ("work_mode", "is_remote"),
    "experience": ("experience", "experience_min", "experience_max"),
    "job type": ("job_type", "employment_type"),
}

def populated(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return str(value).strip().lower() not in {"", "none", "null", "nan", "[]", "{}"}


def has_any(row: dict[str, Any], aliases: Iterable[str]) -> bool:
    return any(populated(row.get(key)) for key in aliases)


def read_rows(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    if path.suffix.lower() == ".json":
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, list):
            return [row for row in payload if isinstance(row, dict)]
        if isinstance(payload, dict):
            for key in ("jobs", "results", "data"):
                if isinstance(payload.get(key), list):
                    return [row for row in payload[key] if isinstance(row, dict)]
    return []


def resolve(patterns: list[str]) -> list[Path]:
    found: dict[str, Path] = {}
    for pattern in patterns:
        matches = glob.glob(pattern, recursive=True)
        if not matches and Path(pattern).is_file():
            matches = [pattern]
        for match in matches:
            path = Path(match)
            if path.is_file() and path.suffix.lower() in {".csv", ".json"}:
                found[str(path.resolve())] = path
    return sorted(found.values(), key=lambda path: str(path))


def percentage(rows: list[dict[str, Any]], aliases: tuple[str, ...]) -> float:
    if not rows:
        return 0.0
    return 100.0 * sum(has_any(row, aliases) for row in rows) / len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit scraper output field completeness")
    parser.add_argument("patterns", nargs="+", help="CSV/JSON paths or glob patterns")
    parser.add_argument("--warn-description-below", type=float, default=80.0)
    args = parser.parse_args()

    paths = resolve(args.patterns)
    if not paths:
        print("No job output files matched the supplied paths.")
        return 1

    headers = ["File", "Rows", *FIELD_GROUPS]
    table = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    warnings: list[str] = []
    for path in paths:
        try:
            rows = read_rows(path)
        except (OSError, ValueError, csv.Error, json.JSONDecodeError) as exc:
            warnings.append(f"{path}: unreadable ({type(exc).__name__})")
            continue
        if not rows:
            warnings.append(f"{path}: no job rows")
            continue
        rates = {name: percentage(rows, aliases) for name, aliases in FIELD_GROUPS.items()}
        cells = [str(path), str(len(rows)), *(f"{rates[name]:.1f}%" for name in FIELD_GROUPS)]
        table.append("| " + " | ".join(cells) + " |")
        if rates["description"] < args.warn_description_below:
            warnings.append(f"{path}: description coverage is {rates['description']:.1f}%")

    report = "\n".join(["## Job field completeness", "", *table])
    if warnings:
        report += "\n\n### Warnings\n\n" + "\n".join(f"- {message}" for message in warnings)
    print(report)

    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(report + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
