"""
Evidence-first LinkedIn job repost classification.

A job-header Reposted label is explicit evidence. Job-ID calibration is only an
estimate: sparse historical samples cannot prove an original posting date or
classify IDs newer than the samples. Missing evidence remains unknown.
"""
from __future__ import annotations

import bisect
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

REPOST_DAYS = 3  # listed this many days after it was first posted: a repost

# (day, job id) read from LinkedIn's own first-posting dates (originalListedAt), October 2026
SEED = [
    (date(2026, 8, 7), 4448494515), (date(2026, 8, 24), 4458507100), (date(2026, 9, 1), 4459260872),
    (date(2026, 9, 5), 4463653649), (date(2026, 9, 18), 4466936390), (date(2026, 9, 26), 4470547066),
    (date(2026, 10, 5), 4474291436), (date(2026, 10, 6), 4474407866), (date(2026, 10, 13), 4476605264),
    (date(2026, 10, 20), 4478639942), (date(2026, 10, 23), 4482821751),
]

TABLE_SQL = """create table if not exists public.linkedin_job_id_days (
  day date primary key, max_id bigint not null, updated_at timestamptz not null default now())"""

_ID = re.compile(r"linkedin\.com/jobs/view/(?:[^/?]*-)?(\d{8,})")


def linkedin_id(url: str | None) -> int | None:
    match = _ID.search(url or "")
    return int(match.group(1)) if match else None


class Calibration:
    """Day <-> job id, from points (day, highest id created that day); ids only grow with the days."""

    def __init__(self, points: Iterable[tuple[date, int]]) -> None:
        best: dict[date, int] = {}
        for day, job_id in points:
            best[day] = max(best.get(day, 0), int(job_id))
        days = sorted(best)
        ids, top = [], 0
        for day in days:
            top = max(top, best[day])  # a later day never has a lower highest id
            ids.append(top)
        self.days, self.ids = days, ids

    def first_day(self, job_id: int) -> date | None:
        """Estimate within the sampled ID range only; never clamp newer IDs to an old day."""
        if not self.ids or job_id < self.ids[0] or job_id > self.ids[-1]:
            return None
        if job_id == self.ids[-1]:
            return self.days[-1]
        if job_id == self.ids[0]:
            return self.days[0]
        i = bisect.bisect_left(self.ids, job_id)
        low_day, low_id, high_day, high_id = self.days[i - 1], self.ids[i - 1], self.days[i], self.ids[i]
        if high_id == low_id:
            return high_day
        span = (high_day - low_day).days
        return low_day + timedelta(days=round(span * (job_id - low_id) / (high_id - low_id)))


def _day(value) -> date | None:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
        return parsed.astimezone(timezone.utc).date() if parsed and parsed.tzinfo else (parsed.date() if parsed else None)
    except (ValueError, TypeError):
        return None


def header_evidence(soup) -> dict:
    """Read the job's own header, never arbitrary JD text or recommended jobs."""
    header = soup.select_one('.top-card-layout, .topcard, .job-details-jobs-unified-top-card, .jobs-unified-top-card')
    if header is None:
        return {}
    for node in header.select('.posted-time-ago__text, .posted-time-ago__text--new, '
                              '.jobs-unified-top-card__posted-date, '
                              '.job-details-jobs-unified-top-card__primary-description-container'):
        label = node.get_text(' ', strip=True)
        if re.search(r'\breposted\b', label, re.I) and not re.search(r'\bnot\s+reposted\b', label, re.I):
            return {'reposted': True, 'reposted_source': 'linkedin_label',
                    'reposted_confidence': 'explicit', 'reposted_evidence': label}
    return {}


def _boolean(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
        return value.strip().lower() == 'true'
    return None


def annotate(rows: list[dict], calibration: Calibration) -> int:
    """Prefer scraped evidence, otherwise provide a labeled, bounded ID estimate."""
    reposts = 0
    for row in rows:
        row.setdefault('first_posted_at_estimate', None)
        row.setdefault('reposted_evidence', '')
        existing = _boolean(row.get('reposted'))
        legacy = _boolean(row.get('is_reposted'))
        if existing is not None and row.get('reposted_source') != 'job_id_estimate':
            row['reposted'] = existing
            row['reposted_source'] = row.get('reposted_source') or 'existing'
            row['reposted_confidence'] = row.get('reposted_confidence') or 'unverified'
            reposts += existing
            continue
        if legacy is True:
            row.update(reposted=True, reposted_source='legacy_scrape', reposted_confidence='unverified')
            reposts += 1
            continue
        row.update(reposted=None, reposted_source='unknown', reposted_confidence='unknown',
                   reposted_evidence='', first_posted_at_estimate=None)
        job_id = linkedin_id(row.get("job_url"))
        listed = _day(row.get("posted_at"))
        if not job_id or not listed:
            continue
        first = calibration.first_day(job_id)
        if first is None:
            if calibration.ids and job_id < calibration.ids[0] and (listed - calibration.days[0]).days >= REPOST_DAYS:
                row['reposted'] = True
                row['reposted_evidence'] = 'ID predates the oldest calibration sample; listing is newer.'
        elif listed >= first:
            row["reposted"] = (listed - first).days >= REPOST_DAYS
            row['first_posted_at_estimate'] = datetime.combine(first, datetime.min.time(), timezone.utc).isoformat()
            row['reposted_evidence'] = f'ID-based date estimate {first.isoformat()}; listed {listed.isoformat()}.'
        if row['reposted'] is not None:
            row.update(reposted_source='job_id_estimate', reposted_confidence='estimated')
        reposts += bool(row["reposted"])
    for row in rows:
        value = _boolean(row.get('reposted'))
        if value is None:
            row['repost_status'] = 'Unknown'
        else:
            label = 'Reposted' if value else 'Not reposted'
            confidence = row.get('reposted_confidence')
            row['repost_status'] = label if confidence == 'explicit' else f'{label} ({confidence or "unverified"})'
    return reposts


def todays_points(rows: list[dict]) -> dict[date, int]:
    """Learn only explicitly sourced original dates, never a repost's latest listing date."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=2)
    points: dict[date, int] = {}
    for row in rows:
        job_id = linkedin_id(row.get('job_url'))
        original = _day(row.get('first_posted_at'))
        if job_id and original and original >= cutoff and row.get('first_posted_at_source') == 'linkedin_original_date':
            points[original] = max(points.get(original, 0), job_id)
    return points


def load(conn) -> Calibration:
    conn.execute(TABLE_SQL)
    stored = conn.execute("select day, max_id from public.linkedin_job_id_days").fetchall()
    return Calibration([*SEED, *((day, max_id) for day, max_id in stored)])


def learn(conn, points: dict[date, int]) -> None:
    for day, max_id in points.items():
        conn.execute("""insert into public.linkedin_job_id_days (day, max_id) values (%s, %s)
                        on conflict (day) do update set max_id = greatest(linkedin_job_id_days.max_id, excluded.max_id),
                        updated_at = now()""", (day, max_id))
