"""LinkedIn public search paging (no network).  Run: python -m unittest linkedin.test_linkedin_guest"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import linkedin_guest_search as guest  # noqa: E402
import linkedin_68_roles as roles  # noqa: E402

CARD = """<li><div class="base-card" data-entity-urn="urn:li:jobPosting:{id}">
<h3 class="base-search-card__title">  {title} </h3>
<h4 class="base-search-card__subtitle"><a href="x">{company}</a></h4>
<span class="job-search-card__location">Austin, TX</span>
<span class="job-search-card__salary-info">$80,000.00 - $95,000.00</span>
<time class="job-search-card__listdate" datetime="2026-10-23">1 day ago</time></div></li>"""


def page(ids, title="Buyer"):
    return "".join(CARD.format(id=i, title=title, company=f"Co {i}") for i in ids)


class GuestSearchTests(unittest.TestCase):
    def test_cards_are_read(self):
        (row,) = guest.parse_cards(page([4471]))
        self.assertEqual(("4471", "Buyer", "Co 4471", "Austin, TX", 80000.0, 95000.0, "year"),
                         (row["id"], row["title"], row["company"], row["location"], row["min_amount"],
                          row["max_amount"], row["interval"]))
        self.assertEqual("https://www.linkedin.com/jobs/view/4471", row["job_url"])

    def test_batches_step_ten_at_a_time_and_flag_the_end(self):
        calls = []

        def fake_get(url):
            start = int(url.rsplit("start=", 1)[1])
            calls.append(start)
            return mock.Mock(status_code=200, text=page(range(start, start + 10)) if start < 30 else "")
        with mock.patch.object(guest, "_http", return_value=mock.Mock(get=fake_get)), mock.patch.object(guest.time, "sleep"):
            first = guest.search_batch("Buyer", "United States", 24, 0, 50)
        self.assertEqual([0, 10, 20, 30], calls)  # 10 per request, no skipped results
        self.assertEqual(30, len(first))
        self.assertTrue(first.attrs["end"])

    def test_rate_limit_is_raised_not_taken_for_the_end(self):
        with mock.patch.object(guest, "_http", return_value=mock.Mock(get=lambda url: mock.Mock(status_code=429))):
            with self.assertRaisesRegex(RuntimeError, "429"):
                guest.search_batch("Buyer", "United States", 24, 0, 50)


class PagingTests(unittest.TestCase):
    def run_role(self, batches):
        frames = []
        for rows, end in batches:
            frame = pd.DataFrame(rows)
            frame.attrs["end"] = end
            frames.append(frame)
        with mock.patch.object(roles, "search_batch", side_effect=frames + [pd.DataFrame()]), \
                mock.patch.object(roles.time, "sleep"), \
                mock.patch.object(roles, "enrich_jobs_after_details", side_effect=lambda jobs: jobs):
            return roles.scrape_role("Buyer", "United States", 24, set(), fetch_description=False)

    @staticmethod
    def rows(ids, title="Buyer"):
        return [{"title": title, "company": f"Co {i}", "location": "TX", "job_url": f"https://www.linkedin.com/jobs/view/{i}"}
                for i in ids]

    def test_a_short_batch_does_not_end_the_role(self):
        jobs, stats = self.run_role([(self.rows(range(0, 12)), False), (self.rows(range(12, 62)), False),
                                     (self.rows(range(62, 70)), True)])
        self.assertEqual(70, len(jobs))  # the short first batch (12) did not stop it
        self.assertEqual(70, stats["found"])
        self.assertFalse(stats["capped"])

    def test_paging_stops_when_titles_no_longer_match(self):
        jobs, _ = self.run_role([(self.rows(range(0, 50)), False),
                                 (self.rows(range(50, 100), "Forklift Driver"), False),
                                 (self.rows(range(100, 150), "Nurse"), False),
                                 (self.rows(range(150, 200)), False)])
        self.assertEqual(150, len(jobs))  # two batches without a Buyer title: the last one is never asked for

    def test_reaching_linkedins_limit_is_reported(self):
        batches = [(self.rows(range(i, i + 50)), i + 50 >= 1000) for i in range(0, 1000, 50)]
        _, stats = self.run_role(batches)
        self.assertTrue(stats["capped"])



class ShardResilienceTests(unittest.TestCase):
    """main() with a stubbed LinkedIn: a blocked or slow shard still finishes and hands over what it has."""

    def run_main(self, outcomes, roles_list, argv=("x", "--shard", "1/1", "--post-time", "day"), budget_left=None):
        calls, covered = [], []

        def fake_scrape(role, **kwargs):
            calls.append(role)
            kind = outcomes[role].pop(0) if isinstance(outcomes[role], list) else outcomes[role]
            if kind == "blocked":
                return [], {"batches": 0, "hit_429": True, "found": 0, "capped": False, "blocked": True}
            return [{"id": f"{role}-1", "job_title": role}], {"batches": 2, "hit_429": False, "found": 1,
                                                              "capped": False, "blocked": False}
        saved = []
        self.last = (calls, saved, covered)  # readable even when main() exits with the partial-shard code
        with mock.patch.object(roles, "ROLES", roles_list), mock.patch.object(roles, "scrape_role", fake_scrape), \
                mock.patch.object(roles.time, "sleep"), mock.patch.object(roles, "save_state"), \
                mock.patch.object(roles, "load_state", return_value={"completed_roles": [], "total_jobs": 0, "seen_ids": []}), \
                mock.patch.object(roles, "append_jobs_to_disk", side_effect=saved.extend), \
                mock.patch.object(roles, "export_strict_datasets"), mock.patch.object(roles, "load_existing_jobs", return_value=[]), \
                mock.patch.object(roles, "write_coverage", side_effect=lambda rows, _t: covered.extend(rows)), \
                mock.patch.object(sys, "argv", list(argv)):
            if budget_left is not None:
                real = roles.time.monotonic
                with mock.patch.object(roles.time, "monotonic", side_effect=budget_left(real)):
                    roles.main()
            else:
                roles.main()
        return calls, saved, covered

    def test_three_blocked_roles_in_a_row_stop_the_shard_but_keep_what_was_collected(self):
        names = ["A", "B", "C", "D", "E", "F"]
        with self.assertRaises(SystemExit) as stopped:  # a partial shard exits 3 after saving
            self.run_main({"A": "ok", "B": "blocked", "C": "blocked", "D": "blocked", "E": "ok", "F": "ok"}, names)
        self.assertEqual(roles.PARTIAL_EXIT_CODE, stopped.exception.code)
        calls, saved, covered = self.last
        self.assertEqual(["A", "B", "C", "D"], calls[:4])
        self.assertEqual(["A-1"], [job["id"] for job in saved])  # A's jobs were kept
        notes = {role: note for role, _, _, note in covered}
        self.assertIn("blocked", notes["B"])
        self.assertIn("not run", notes["E"])  # the rest of the shard is reported, not silently lost

    def test_blocked_roles_are_tried_once_more_after_a_rest(self):
        names = ["A", "B", "C"]
        calls, saved, _ = self.run_main({"A": "ok", "B": ["blocked", "ok"], "C": "ok"}, names)
        self.assertEqual(["A", "B", "C", "B"], calls)  # B retried at the end
        self.assertEqual({"A-1", "B-1", "C-1"}, {job["id"] for job in saved})

    def test_the_time_budget_ends_the_shard_cleanly(self):
        names = ["A", "B", "C"]
        ticks = iter(range(0, 10_000, 1))

        def clock(real):
            return lambda: next(ticks) * 60.0  # one minute per call

        with self.assertRaises(SystemExit) as stopped:
            self.run_main({n: "ok" for n in names}, names,
                          argv=("x", "--shard", "1/1", "--post-time", "day", "--time-budget", "5"), budget_left=clock)
        self.assertEqual(roles.PARTIAL_EXIT_CODE, stopped.exception.code)
        calls, saved, covered = self.last
        self.assertLess(len(calls), 3)  # stopped before every role ran
        self.assertTrue(any("not run" in note for _, _, _, note in covered))


if __name__ == "__main__":
    unittest.main()
