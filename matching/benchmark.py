"""Offline recommendation-quality benchmark against the real job corpus.

    python -m matching.benchmark            # NEON_DATABASE_URL from env/.env.local

Relevance labels come from title rules written independently of the matcher's
taxonomy, so the benchmark does not grade the matcher with its own vocabulary.
They are a weak proxy for human judgement: use them to compare versions of the
algorithm, not as an absolute quality claim. The connection is read-only.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
from statistics import mean
import time
from typing import Any

from .evaluation import ndcg_at, precision_at
from .hashing import canonical_work_mode, normalize_text


@dataclass(frozen=True)
class Persona:
    name: str
    profile: dict[str, Any]
    strong: str                 # grade 3: the target role itself
    related: str = r"$^"        # grade 2: same family, different specialisation
    adjacent: str = r"$^"       # grade 1: plausible but weak
    mismatch: str = r"$^"       # caps the grade at 1 (e.g. seniority far off)
    request: dict[str, Any] = field(default_factory=dict)

    def grade(self, title: str) -> int:
        text = title.casefold()
        value = 3 if re.search(self.strong, text) else 2 if re.search(self.related, text) \
            else 1 if re.search(self.adjacent, text) else 0
        return min(value, 1) if re.search(self.mismatch, text) else value


_SENIOR = r"\b(?:director|head of|vp|vice president|chief|principal)\b"
_JUNIOR = r"\b(?:intern|internship|junior|assistant|entry)\b"

PERSONAS = [
    Persona("procurement_manager", {
        "roles": ["Procurement Manager"], "professional_roles": ["Senior Buyer", "Procurement Manager"],
        "skills": ["SAP", "SAP Ariba", "Negotiation", "Supplier Relationship Management", "Contract Management",
                   "Spend Analysis", "Excel"], "professional_years": 8},
        strong=r"\bprocurement\b.*\b(?:manager|lead)\b|\bpurchasing manager\b",
        related=r"\b(?:procurement|purchasing|sourcing|category manager|commodity)\b",
        adjacent=r"\b(?:supply chain|buyer|vendor|supplier)\b", mismatch=_JUNIOR),
    Persona("junior_buyer", {
        "roles": ["Buyer"], "professional_roles": ["Purchasing Assistant"],
        "skills": ["Purchasing", "Excel", "Inventory Management", "SAP"], "professional_years": 1.5},
        strong=r"\b(?:buyer|purchasing (?:agent|assistant|associate|coordinator|specialist))\b",
        related=r"\b(?:procurement|purchasing|sourcing)\b",
        adjacent=r"\b(?:supply chain|inventory|materials)\b", mismatch=_SENIOR + r"|\bsenior\b"),
    Persona("strategic_sourcing", {
        "roles": ["Strategic Sourcing Manager"], "professional_roles": ["Category Manager", "Sourcing Manager"],
        "skills": ["Category Management", "RFP", "Negotiation", "Spend Analysis", "Coupa"], "professional_years": 10},
        strong=r"\bsourcing\b|\bcategory manag",
        related=r"\b(?:procurement|purchasing|commodity)\b",
        adjacent=r"\b(?:supply chain|supplier|vendor)\b", mismatch=_JUNIOR),
    Persona("supply_chain_analyst", {
        "roles": ["Supply Chain Analyst"], "professional_roles": ["Supply Chain Analyst"],
        "skills": ["SQL", "Excel", "Tableau", "Forecasting", "Inventory Management"], "professional_years": 3},
        strong=r"\bsupply chain\b.*\banalyst\b|\b(?:demand|inventory) (?:planner|analyst)\b",
        related=r"\b(?:supply chain|logistics|planning|procurement analyst|purchasing analyst)\b",
        adjacent=r"\b(?:operations analyst|data analyst|buyer)\b", mismatch=_SENIOR),
    Persona("backend_engineer", {
        "roles": ["Backend Engineer"], "professional_roles": ["Software Engineer"],
        "skills": ["Python", "Django", "PostgreSQL", "AWS", "Docker", "Kubernetes"], "professional_years": 5},
        strong=r"\bback[ -]?end\b|\b(?:software|platform|api) (?:engineer|developer)\b",
        related=r"\b(?:full[ -]?stack|software|python|developer|sde)\b",
        adjacent=r"\b(?:devops|sre|data engineer|infrastructure)\b", mismatch=_JUNIOR + "|" + _SENIOR),
    Persona("remote_backend_engineer", {
        "roles": ["Backend Engineer"], "professional_roles": ["Software Engineer"],
        "skills": ["Python", "Go", "PostgreSQL", "AWS"], "professional_years": 6},
        strong=r"\bback[ -]?end\b|\b(?:software|platform|api) (?:engineer|developer)\b",
        related=r"\b(?:full[ -]?stack|software|python|developer|sde)\b",
        adjacent=r"\b(?:devops|sre|data engineer|infrastructure)\b", mismatch=_JUNIOR + "|" + _SENIOR,
        request={"remote_only": True}),
    Persona("data_analyst", {
        "roles": ["Data Analyst"], "professional_roles": ["Data Analyst"],
        "skills": ["SQL", "Tableau", "Python", "Excel", "Power BI"], "professional_years": 3},
        strong=r"\b(?:data|business intelligence|bi|reporting|analytics) analyst\b",
        related=r"\b(?:analyst|analytics|data scien)\b",
        adjacent=r"\b(?:data engineer|insights)\b", mismatch=_SENIOR),
    Persona("account_executive", {
        "roles": ["Account Executive"], "professional_roles": ["Sales Representative", "Account Executive"],
        "skills": ["Salesforce", "B2B Sales", "Negotiation", "CRM", "Prospecting"], "professional_years": 4},
        strong=r"\baccount executive\b|\b(?:sales|business development) (?:representative|executive|manager)\b",
        related=r"\b(?:sales|account manager|business development|bdr|sdr)\b",
        adjacent=r"\b(?:customer success|partnerships)\b", mismatch=_SENIOR),
    Persona("technical_recruiter", {
        "roles": ["Technical Recruiter"], "professional_roles": ["Recruiter"],
        "skills": ["Sourcing", "Applicant Tracking Systems", "LinkedIn Recruiter", "Interviewing"],
        "professional_years": 3},
        strong=r"\brecruit", related=r"\b(?:talent acquisition|talent partner|sourcer)\b",
        adjacent=r"\b(?:human resources|hr |people (?:partner|operations))\b", mismatch=_SENIOR),
]


def _duplicate_rate(items: list[dict[str, Any]]) -> float:
    keys = Counter((normalize_text(i["title"]), normalize_text(i["company"])) for i in items)
    return sum(count - 1 for count in keys.values()) / len(items) if items else 0.0


def evaluate_persona(service, persona: Persona, limit: int = 20) -> dict[str, Any]:
    from .schemas import RecommendationRequest
    started = time.perf_counter()
    response = service.recommend(RecommendationRequest(
        user_id="00000000-0000-0000-0000-00000000benc"[:36], profile=persona.profile,
        limit=limit, **persona.request))
    latency = time.perf_counter() - started
    items = [r.model_dump() for r in response.recommendations]
    labels = [persona.grade(i["title"]) for i in items]
    companies = Counter(normalize_text(i["company"]) for i in items)
    violations = 0
    if persona.request.get("remote_only"):
        violations = sum(canonical_work_mode(i.get("work_mode")) != "remote" for i in items)
    return {
        "persona": persona.name, "returned": len(items), "latency_s": round(latency, 2),
        "ndcg@10": round(ndcg_at(labels, 10), 3) if labels else 0.0,
        "precision@10": round(precision_at(labels, 10), 3),
        # nDCG only judges the order of what was returned; this catches a
        # ranking that never surfaces the exact target role at all.
        "strong@10": round(precision_at(labels, 10, relevant=3), 3),
        "duplicate_rate": round(_duplicate_rate(items), 3),
        "top_company_share": round(max(companies.values()) / len(items), 3) if items else 0.0,
        "constraint_violations": violations,
        "top5": [f"{i['title']} @ {i['company']} [{g}]" for i, g in zip(items[:5], labels)],
    }


def run(service, limit: int = 20) -> dict[str, Any]:
    rows = [evaluate_persona(service, persona, limit) for persona in PERSONAS]
    keys = ("ndcg@10", "precision@10", "strong@10", "duplicate_rate", "top_company_share", "latency_s")
    summary = {key: round(mean(row[key] for row in rows), 3) for key in keys}
    summary["constraint_violations"] = sum(row["constraint_violations"] for row in rows)
    summary["empty_results"] = sum(row["returned"] == 0 for row in rows)
    return {"summary": summary, "personas": rows}


def _read_only_url(name: str = "NEON_DATABASE_URL", required: bool = True) -> str | None:
    from psycopg.conninfo import make_conninfo
    url = os.getenv(name)
    if not url:
        from dotenv import dotenv_values
        url = dotenv_values(Path(__file__).resolve().parents[1] / ".env.local").get(name)
    if not url:
        if required:
            raise SystemExit(f"{name} is not set")
        return None
    return make_conninfo(url, options="-c default_transaction_read_only=on")


def main() -> None:
    import argparse
    from .factory import postgres_intake_stack
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-vectors", action="store_true", help="ignore MATCHING_DATABASE_URL")
    args = parser.parse_args()
    vectors = None if args.no_vectors else _read_only_url("MATCHING_DATABASE_URL", required=False)
    _, service, _ = postgres_intake_stack(_read_only_url(), vector_connection_string=vectors)
    result = run(service, args.limit)
    text = json.dumps(result, indent=2, default=str)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
