"""Work-authorization restrictions stated in job postings.

Postings say "we are unable to sponsor visas" or "US citizenship required" in
free text, usually near the end. A candidate who needs sponsorship (or is not a
citizen) cannot take those jobs, so this is a hard rule, not a preference.

The patterns use only syntax shared by Python ``re`` and PostgreSQL regular
expressions (no ``\\b``), so the same text runs in Python on normalized text and
in SQL over the full description. Normalized text drops apostrophes, hence
``won\\W?t`` and ``u\\W?s``.
"""
from __future__ import annotations

import re

from .hashing import normalize_text
from .schemas import CandidateProfile, JobIntelligence

_KIND = r"(visa\s+|immigration\s+|employment\s+|work\s+|h\W?1\W?b\s+)*"

NO_SPONSORSHIP_PATTERN = "|".join([
    # "we are unable to sponsor", "will not provide visa sponsorship", "cannot offer sponsorship"
    r"(unable|not able|cannot|can not|will not|won\W?t|do not|does not|don\W?t|doesn\W?t|are not|is not)\s+"
    r"(to\s+)?(currently\s+)?((provide|offer|support)\s+)?" + _KIND + r"sponsor",
    r"no\s+" + _KIND + r"sponsorship",
    # "authorized to work without sponsorship", "without the need for visa sponsorship"
    r"without\s+(the\s+)?((need|requiring)\s+(for\s+)?)?((current|now)\s+or\s+(in\s+the\s+)?future\s+)?"
    # Not export-control boilerplate: "...without sponsorship for an export license".
    + _KIND + r"sponsorship(?!\s+for\s+an?\s+export)",
    r"sponsorship\s+(is\s+not|will\s+not|cannot|can\s+not|isn\W?t|won\W?t)\s+(be\s+)?"
    r"(available|offered|provided|considered)",
    r"sponsorship\s+not\s+available",
    r"not\s+eligible\s+for\s+" + _KIND + r"sponsorship",
])

CITIZENSHIP_PATTERN = "|".join([
    r"(u\W?\s?s\W?|united states)\s+citizens?(hip)?\s+(is\s+)?(required|only)",
    r"must\s+be\s+(a\s+)?(u\W?\s?s\W?|united states)\s+citizen",
    r"requires?\s+(u\W?\s?s\W?|united states)\s+citizenship",
    r"(top\s+secret|secret|ts\W?sci)\s+(security\s+)?clearance",
    r"security\s+clearance\s+(is\s+)?required",
])

_NO_SPONSORSHIP = re.compile(NO_SPONSORSHIP_PATTERN, re.IGNORECASE)
_CITIZENSHIP = re.compile(CITIZENSHIP_PATTERN, re.IGNORECASE)

# Answers to the onboarding "work authorization" question (resume_questions.json).
_NEEDS_SPONSORSHIP = {"temporary or student authorization", "not currently authorized"}
_CITIZEN = "citizen or permanent resident"


def needs_sponsorship(profile: CandidateProfile) -> bool:
    """Explicit answer first; otherwise inferred from the authorization status."""
    if profile.requires_sponsorship is not None:
        return profile.requires_sponsorship
    return normalize_text(profile.work_authorization) in _NEEDS_SPONSORSHIP


def excluded_by_citizenship(profile: CandidateProfile) -> bool:
    """Only a stated non-citizen status excludes; unknown or undisclosed does not."""
    status = normalize_text(profile.work_authorization)
    return bool(status) and status not in {_CITIZEN, "prefer not to say"}


def authorized_for(job: JobIntelligence, profile: CandidateProfile) -> bool:
    sponsorship = needs_sponsorship(profile)
    citizenship = excluded_by_citizenship(profile)
    if not (sponsorship or citizenship):
        return True
    text = f"{job.cleaned_text} {job.matching_text}"
    if sponsorship:
        unavailable = job.sponsorship_unavailable
        if unavailable is None:
            unavailable = _NO_SPONSORSHIP.search(text) is not None
        if unavailable:
            return False
    if citizenship:
        required = job.citizenship_required
        if required is None:
            required = _CITIZENSHIP.search(text) is not None
        if required:
            return False
    return True
