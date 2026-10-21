"""Company discovery from ATS apply links (URL shapes taken from real LinkedIn/Indeed exports)."""
from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import discover_ats_companies as d


def company(url: str, name: str = ""):
    found = d.parse_ats_url(url, name)
    return (found.board, found.key) if found else None


class ParseTests(unittest.TestCase):
    def test_slug_boards(self) -> None:
        cases = {
            "https://job-boards.greenhouse.io/instawork/jobs/4721730006": ("Greenhouse", "instawork"),
            "https://boards.greenhouse.io/embed/job_app?for=Acme&token=1": ("Greenhouse", "acme"),
            "https://jobs.lever.co/cagents/c246c99b-28a7-479a-8de3-6512e3381ffa?lever-source=Indeed": ("Lever", "cagents"),
            "https://jobs.ashbyhq.com/tae-power-solutions/948ce701-7fa2-4644-a1ad-06644efa28e0?utm_source=x": ("Ashby", "tae-power-solutions"),
            "https://apply.workable.com/acme-corp/j/ABC123/": ("Workable", "acme-corp"),
            "https://jobs.smartrecruiters.com/TurnerTownsend/744000155363138-senior-project-manager": ("SmartRecruiters", "TurnerTownsend"),
            "https://jobs.smartrecruiters.com/oneclick-ui/company/Visa/publication/1": ("SmartRecruiters", "Visa"),
            "https://myspi.bamboohr.com/careers/306?source=indeed": ("BambooHR", "myspi"),
            "https://jobs.jobvite.com/acme/job/oABC": ("Jobvite", "acme"),
            "https://jobs.jobvite.com/careers/acme/job/oABC": ("Jobvite", "acme"),
        }
        for url, expected in cases.items():
            self.assertEqual(company(url), expected, url)

    def test_links_without_a_company_are_ignored(self) -> None:
        for url in ("https://apply.workable.com/j/CC61A28970", "https://app.jobvite.com/CompanyJobs/Job.aspx?j=o3RXAfwb",
                    "https://jobs.ashbyhq.com/948ce701-7fa2-4644-a1ad-06644efa28e0", "https://boards.greenhouse.io/embed/job_app?token=1",
                    "https://www.linkedin.com/jobs/view/123", "https://grnh.se/8n7um43o2us", "", "not a url"):
            self.assertIsNone(company(url), url)

    def test_workday_tenant_and_site(self) -> None:
        found = d.parse_ats_url("https://simon.wd1.myworkdayjobs.com/en-US/Simon/job/Livermore-CA/Assistant-Director_R1", "Simon Property")
        self.assertEqual((found.key, found.name, found.url), ("simon/simon", "Simon Property", "https://simon.wd1.myworkdayjobs.com/Simon"))
        found = d.parse_ats_url("https://generac.wd5.myworkdayjobs.com/External/job/Holdrege-NE/Buyer_JR15540")
        self.assertEqual((found.key, found.name, found.url), ("generac/external", "Generac", "https://generac.wd5.myworkdayjobs.com/External"))

    def test_icims_forms(self) -> None:
        self.assertEqual(company("https://careers-novelis.icims.com/jobs/55782/job"), ("iCIMS", "novelis"))
        self.assertEqual(company("https://uscareers-acme.icims.com/jobs/1/job"), ("iCIMS", "uscareers-acme"))
        self.assertEqual(company("https://jobs-acme.icims.com/jobs/1/job"), ("iCIMS", "https://jobs-acme.icims.com"))

    def test_adp_company_id(self) -> None:
        url = "https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid=b5d87377-1bbe-4787-8322-eb5c7abdc9c7&ccId=19000101_000001&lang=en_US"
        found = d.parse_ats_url(url, "Acme Foods")
        self.assertEqual((found.key, found.name), ("b5d87377-1bbe-4787-8322-eb5c7abdc9c7/19000101_000001", "Acme Foods"))
        self.assertIn("cid=b5d87377-1bbe-4787-8322-eb5c7abdc9c7", found.url)

    def test_unsupported_platforms_are_recognised(self) -> None:
        self.assertEqual(d.unsupported_platform("https://recruiting.paylocity.com/Recruiting/Jobs/Details/1"), "Paylocity")
        self.assertEqual(d.unsupported_platform("https://career41.sapsf.com/career?company=eaton"), "SuccessFactors")
        self.assertIsNone(d.unsupported_platform("https://jobs.lever.co/acme/1"))


class ListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for rel, text in {"greenhouse/greenhouse_companies.txt": "acme\n#disabled-co\nplain",   # no trailing newline
                          "greenhouse/greenhouse_404_companies.txt": "dead-co\n",
                          "lever/lever_companies.txt": "leverco\n",
                          "workday/workday.csv": "name,slug,url\nAcme,acme/careers,https://acme.wd5.myworkdayjobs.com/careers\n"}.items():
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    jobs = [("https://job-boards.greenhouse.io/ACME/jobs/1", "", "Acme"),          # known (case-insensitive)
            ("https://boards.greenhouse.io/disabled-co/jobs/2", "", "Disabled"),   # known but disabled
            ("https://boards.greenhouse.io/dead-co/jobs/3", "", "Dead"),           # known dead
            ("https://boards.greenhouse.io/newco/jobs/4", "", "NewCo"),
            ("https://boards.greenhouse.io/newco/jobs/5", "", "NewCo"),            # same company twice: one entry
            ("https://acme.wd5.myworkdayjobs.com/en-US/Other/job/x", "", "Acme"),  # new site, known tenant: skipped
            ("https://zeta.wd1.myworkdayjobs.com/Zeta_Careers/job/y", "", "Zeta Inc"),
            ("https://www.indeed.com/viewjob?jk=1", "", "x")]

    def test_only_unknown_companies_are_new_and_counted_once(self) -> None:
        result = d.discover(self.jobs, d.load_known(self.root))
        self.assertEqual({board: [(c.key, n) for c, n in items] for board, items in result["new"].items()},
                         {"Greenhouse": [("newco", 2)], "Workday": [("zeta/zeta_careers", 1)]})
        self.assertEqual(result["already"], {"Greenhouse": 3, "Workday": 1})

    def test_apply_appends_in_each_lists_format_and_is_idempotent(self) -> None:
        known = d.load_known(self.root)
        written = d.append_companies(d.discover(self.jobs, known)["new"], self.root)
        self.assertEqual(written, {"Greenhouse": 1, "Workday": 1})
        self.assertEqual((self.root / "greenhouse/greenhouse_companies.txt").read_text(), "acme\n#disabled-co\nplain\nnewco\n")
        with (self.root / "workday/workday.csv").open() as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(rows[-1], {"name": "Zeta Inc", "slug": "zeta/zeta_careers", "url": "https://zeta.wd1.myworkdayjobs.com/Zeta_Careers"})
        again = d.discover(self.jobs, d.load_known(self.root))
        self.assertEqual(again["new"], {})

    def test_short_links_are_resolved_through_the_supplied_resolver(self) -> None:
        jobs = [("https://grnh.se/abc", "", "Short Co")]
        result = d.discover(jobs, d.load_known(self.root), lambda urls: {urls[0]: "https://job-boards.greenhouse.io/shortco/jobs/9?gh_src=x"})
        self.assertEqual([c.key for c, _ in result["new"]["Greenhouse"]], ["shortco"])
        result = d.discover(jobs, d.load_known(self.root), lambda urls: {urls[0]: "https://www.company.com/careers?gh_jid=9"})
        self.assertEqual(result["new"], {})  # the company's own site: no slug to learn


if __name__ == "__main__":
    unittest.main()
