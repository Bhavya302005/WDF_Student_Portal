"""
Fresh or reposted: LinkedIn jobs, told apart without logging in.

LinkedIn shows a reposted job with its latest listing date only ("2 days ago"), but a repost keeps the job's
id, and job ids grow steadily with the day a job was first created. So the id dates the first posting:
a job listed this week with an id from September was reposted.

The calibration (highest job id seen per day) lives in the main jobs database (linkedin_job_id_days),
starts from ids whose first-posting day was read from LinkedIn, and learns a point a day from new jobs.
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
        """The day a job with this id was first posted (interpolated), or None before the calibration starts."""
        if not self.ids or job_id < self.ids[0]:
            return None
        if job_id >= self.ids[-1]:
            return self.days[-1]
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
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date() if value else None
    except ValueError:
        return None


def annotate(rows: list[dict], calibration: Calibration) -> int:
    """Set reposted / first_posted_at on LinkedIn rows (posted_at is the date the board lists). Returns reposts."""
    reposts = 0
    for row in rows:
        job_id = linkedin_id(row.get("job_url"))
        listed = _day(row.get("posted_at"))
        if not job_id or not listed:
            continue
        first = calibration.first_day(job_id)
        if first is None:  # older than anything calibrated: first posted before the earliest day known
            row["reposted"] = True
            row["first_posted_at"] = None
        else:
            row["reposted"] = (listed - first).days >= REPOST_DAYS
            row["first_posted_at"] = datetime.combine(min(first, listed), datetime.min.time(), timezone.utc)
        reposts += bool(row["reposted"])
    return reposts


def todays_points(rows: list[dict]) -> dict[date, int]:
    """Highest id per listing day among this batch's recent LinkedIn jobs: the newest job created that day."""
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=2)
    points: dict[date, int] = {}
    for row in rows:
        job_id, listed = linkedin_id(row.get("job_url")), _day(row.get("posted_at"))
        if job_id and listed and listed >= cutoff:
            points[listed] = max(points.get(listed, 0), job_id)
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
