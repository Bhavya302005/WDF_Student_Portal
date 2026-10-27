from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from job_extraction.engine import extract_job
from job_extraction.enrich_csv import enrich_csv


DESCRIPTION = (
    "Responsibilities: Lead sourcing and supplier negotiations. "
    "Required Qualifications: 5+ years of procurement experience, a Bachelor's degree, SAP and Excel. "
    "Preferred Qualifications: MBA and SQL experience."
)


class EnrichmentTests(unittest.TestCase):
    def test_projects_derived_and_section_fields(self) -> None:
        job = extract_job({"job_title": "Procurement Manager", "description": DESCRIPTION})
        self.assertEqual(job["experience_min"], 5.0)
        self.assertIn("SAP", job["skills_required"])
        self.assertIn("SQL", job["skills_preferred"])
        self.assertTrue(job["education_requirements"])
        self.assertIn("Lead sourcing", job["key_responsibilities"])
        self.assertIn("5+ years", job["basic_qualifications"])
        self.assertIn("MBA", job["preferred_qualifications"])

    def test_csv_enrichment_regenerates_index_and_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["Index_No", "id", "job_title", "description"])
                writer.writeheader()
                writer.writerow({"Index_No": 99, "id": "one", "job_title": "Procurement Manager", "description": DESCRIPTION})
            self.assertEqual(enrich_csv(path, "LinkedIn"), 1)
            with path.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["Index_No"], "1")
            self.assertEqual(row["source_board"], "LinkedIn")
            self.assertEqual(row["experience_min"], "5.0")
            self.assertIn("Lead sourcing", row["key_responsibilities"])

    def test_indeed_footer_labels(self) -> None:
        footer = "Lead sourcing.\n\n**Job Type:** Full-time\n\nPay: $80,000.00 - $95,000.00 per year\n\nWork Location: In person"
        job = extract_job({"job_title": "Buyer", "description": footer, "source_board": "Indeed"})
        self.assertEqual(job["job_type"], "full_time")
        self.assertEqual(job["work_mode"], "Onsite")
        self.assertEqual((job["salary_min"], job["salary_max"], job["salary_period"]), (80000, 95000, "year"))

    def test_inline_and_hybrid_labels(self) -> None:
        text = "We build things. Job Type: Part-time. Work Location: Hybrid remote in Austin, TX 78701"
        job = extract_job({"job_title": "Buyer", "description": text})
        self.assertEqual(job["job_type"], "part_time")
        self.assertEqual(job["work_mode"], "Hybrid")

    def test_labels_without_known_values_are_ignored(self) -> None:
        text = "Job Type: ------------\nWork Location: 100 Gold Street, Manhattan"
        job = extract_job({"job_title": "Buyer", "description": text})
        self.assertEqual(job["job_type"], "")
        self.assertEqual(job["work_mode"], "")

    def test_conflicting_job_type_labels_stay_unresolved(self) -> None:
        job = extract_job({"job_title": "Buyer", "description": "Job Types: Full-time, Contract"})
        self.assertEqual(job["job_type"], "")

    def test_shard_repr_fields_keep_board_values(self) -> None:
        # Scraper shards are csv.DictWriter output: nested values are Python reprs.
        scraped = extract_job({
            "job_title": "Buyer", "description": "Lead sourcing for our plants.", "source_board": "Indeed",
            "structured_fields": {"job_type": "fulltime",
                                  "salary": {"min": 70000.0, "max": 90000.0, "currency": "USD", "period": "yearly"}},
        })
        scraped.pop("structured_fields", None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(scraped), quoting=csv.QUOTE_NONNUMERIC)
                writer.writeheader()
                writer.writerow(scraped)
            enrich_csv(path, "Indeed")
            with path.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
        self.assertEqual(row["job_type"], "full_time")
        self.assertEqual((row["salary_min"], row["salary_max"]), ("70000.0", "90000.0"))


class ExtractionV5Tests(unittest.TestCase):
    """Phrasings from real postings that jd-v4 missed or misread."""

    def extract(self, description, title="Buyer", **raw):
        return extract_job({"job_title": title, "description": description, **raw})

    def test_escaped_greenhouse_html_keeps_structure_and_drops_markup(self) -> None:
        html = ("&lt;h3&gt;What you&#39;ll do&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;Own supplier negotiations&lt;/li&gt;&lt;/ul&gt;"
                "&lt;h3&gt;Who you are&lt;/h3&gt;&lt;ul&gt;&lt;li&gt;5+ years of procurement experience&lt;/li&gt;&lt;/ul&gt;"
                "&lt;h3&gt;Benefits&lt;/h3&gt;&lt;p&gt;Health insurance&lt;/p&gt;")
        job = self.extract(html)
        self.assertNotIn("<", job["description"])
        self.assertIn("supplier negotiations", job["key_responsibilities"])
        self.assertEqual(job["basic_qualifications"], "5+ years of procurement experience")  # stops at Benefits

    def test_experience_phrasings(self) -> None:
        cases = {"A minimum of 6 years’ experience in Procurement.": 6.0,
                 "5 years' experience with SAP.": 5.0,
                 "Five (5) years of related experience required.": 5.0,
                 "10+ years of semiconductor sourcing, category management.": 10.0}
        for text, years in cases.items():
            self.assertEqual(self.extract(text)["experience_min"], years, text)
        for text in ("Must be 16 years of age or older.", "a 2 year FTC as it transitions.", "Over 100 years of history in steel."):
            self.assertIsNone(self.extract(text)["experience_min"], text)

    def test_job_type_phrasings(self) -> None:
        self.assertEqual(self.extract("This is a full-time, on-site position in Casper, Wyoming.")["job_type"], "full_time")
        self.assertEqual(self.extract("Schedule: Part-Time")["job_type"], "part_time")
        self.assertEqual(self.extract("Employment Status: Regular Full-Time")["job_type"], "full_time")
        self.assertEqual(self.extract("Poste en CDI à Brest.", title="Chef de Chantier (H/F)")["job_type"], "full_time")
        self.assertEqual(self.extract("Employment Type: Full-time or Part-time")["job_type"], "")  # offers both
        self.assertEqual(self.extract("Our CDI specialists review clinical documentation.")["job_type"], "")  # US job title
        self.assertEqual(self.extract("Buenas prácticas de conciliación.")["job_type"], "")  # "good practices"

    def test_work_mode_phrasings(self) -> None:
        self.assertEqual(self.extract("You will work 3 days a week in our NYC office.")["work_mode"], "Hybrid")
        self.assertEqual(self.extract("This role is remote within the US.")["work_mode"], "Remote")
        self.assertEqual(self.extract("Analyze data.", location="Remote - US")["work_mode"], "Remote")
        self.assertEqual(self.extract("We build hybrid cloud infrastructure on AWS.")["work_mode"], "")
        self.assertEqual(self.extract("Analyze imagery.", title="Remote Sensing Scientist")["work_mode"], "")

    def test_salary_ranges(self) -> None:
        dutch = self.extract("Salaris tussen €2.650,00 en €2.800,00 bruto per maand")
        self.assertEqual((dutch["salary_min"], dutch["salary_max"], dutch["salary_currency"], dutch["salary_period"]),
                         (2650, 2800, "EUR", "month"))
        zones = self.extract("Zone 1 pay range: $120,000 — $150,000 USD per year\nZone 2 pay range: $100,000 — $130,000 USD per year")
        self.assertEqual((zones["salary_min"], zones["salary_max"]), (100000, 150000))
        stray = self.extract("We raised $160M. The base pay range is $130,000 - $186,000 USD. Enjoy a $3,000 stipend.")
        self.assertEqual((stray["salary_min"], stray["salary_max"]), (130000, 186000))
        retirement = self.extract("Benefits include a 401k match and paid leave.")
        self.assertIsNone(retirement["salary_min"])

    def test_preferred_lines_without_a_heading(self) -> None:
        job = self.extract("Requirements:\nBachelor's degree in Supply Chain\nExperience with SAP Ariba is a plus\n"
                           "MBA preferred\nBenefits:\nDental")
        self.assertIn("SAP Ariba is a plus", job["preferred_qualifications"])
        self.assertIn("MBA preferred", job["preferred_qualifications"])
        self.assertNotIn("Dental", job["basic_qualifications"])



def pay(description: str, location: str = "") -> str:
    return extract_job({"job_title": "x", "description": description, "location": location})["salary"]


class SalarySelectionTests(unittest.TestCase):
    """Cases taken from stored jobs where the pay was stated but not picked up."""

    def test_pay_is_found_next_to_perks_and_add_ons(self) -> None:
        self.assertEqual(pay("Base Pay: $18.00 per hour, plus $1.25 per hour shift differential for 2nd shift"), "$18 per hour")
        self.assertEqual(pay("Signing Bonus: $1,000\nTo be paid out upon graduation. Compensation: This role pays "
                             "$45,000 - $55,000 annually."), "$45,000 - $55,000 per year")
        self.assertEqual(pay("What we offer: Sign on bonus Competitive Compensation: $118,000 - $135,000, model based on "
                             "productivity"), "$118,000 - $135,000 per year")
        self.assertEqual(pay("Monthly Health Care Allowance - €450/month. Growth Wallet - €1,200/year. "
                             "Gross salary €48,000 - €56,000 per year", "Berlin"), "€48,000 - €56,000 per year")

    def test_currency_and_period_come_from_the_job_when_unstated(self) -> None:
        self.assertEqual(pay("Earn $15.50 Per Hour"), "$15.50 per hour")
        self.assertEqual(pay("Salary: $85,000 - $95,000"), "$85,000 - $95,000 per year")
        self.assertEqual(pay("Hourly rate of $32.76 + shift allowances", "Sydney, NSW"), "AU$32.76 per hour")
        self.assertEqual(pay("Minimum Salary/Wage\nUSD$ 12.00 Hr.\nMaximum Salary/Wage\nUSD$ 20.00 Hr."), "$12 - $20 per hour")
        self.assertEqual(pay("Compensation Range: $271,000 - $216,800"), "$216,800 - $271,000 per year")

    def test_one_range_per_country_or_state_picks_the_jobs_own(self) -> None:
        both = ("For candidates based in the US, the salary range is $118,500 to $216,500 USD. For candidates based "
                "out of Canada, the salary range is $171,500 to $201,000 CAD.")
        self.assertEqual(pay(both, "Toronto, ON"), "CA$171,500 - CA$201,000 per year")
        states = "New Jersey Base Pay Range:\n$30,000 — $50,000 USD\nCalifornia Base Pay Range:\n$90,000 — $140,000 USD"
        self.assertEqual(pay(states, "Newark, New Jersey"), "$30,000 - $50,000 per year")
        self.assertEqual(pay(states, "San Jose, California"), "$90,000 - $140,000 per year")
        self.assertEqual(pay(states, "Remote"), "")

    def test_money_that_is_not_pay_is_ignored(self) -> None:
        for text in ("Excellent benefit package includes medical, dental, 401k, tuition reimbursement",
                     "Around the clock clinical support by phone $1,200 nursing referral bonuses 6 weeks",
                     "prospect and close large strategic deals (+$500K) with Enterprise accounts",
                     "SIGN ON BONUS:\nFull-time (30 hours or more a week) - $3,000.00\nPart-time (29 hours or less a week) - $2,000.00",
                     "0-25 miles: $32\n25-40 miles: $48.50\n40-75 miles: $72.00\n75+ miles: $90",
                     "generated annual sales of CHF 11.76 billion. To fulfil our goals",
                     "managing commercial projects costs of at least $5 Million. 3+ Years experience",
                     "Reembolso en botas de hasta $150 Acceso a entrenamiento"):
            self.assertEqual(pay(text), "", text)
        self.assertEqual(pay("Salary: Up to £100,000 and equity. We raised $5,000,000 from investors", "London, UK"),
                         "£100,000 per year")

    def test_reported_salary_regressions(self) -> None:
        cases = [
            ("Salary Range: $90-110K", "$90,000 - $110,000 per year"),
            ("The base salary range is between $131,480 and $207,000 per year.",
             "$131,480 - $207,000 per year"),
            ("Pay: Up to $29/hr", "$29 per hour"),
            ("Pay Range: $30.72 - $35.29", "$30.72 - $35.29 per hour"),
        ]
        for description, expected in cases:
            with self.subTest(description=description):
                self.assertEqual(pay(description, "Houston, TX"), expected)
        development = extract_job({"job_title": "x", "location": "Mountain View, CA",
                                   "description": "Professional Development: $1500 per year Professional Development Budget"})
        self.assertEqual(development["salary"], "")
        self.assertEqual(development["extraction"]["salary_candidates"], [])

    def test_measurements_and_business_scale_money_are_not_candidates(self) -> None:
        false_salary = (
            "The company has five manufacturing facilities encompassing 1.2m sq. feet.",
            "Experience managing procurement risks for $100M+ capital projects.",
            "Managed $250M annual spend and delivered $12M in cost savings.",
            "Oversaw a portfolio valued at $1.5M.",
            "Owned a $75M book of business.",
            "Managed a $500K capital project budget.",
        )
        for description in false_salary:
            with self.subTest(description=description):
                job = extract_job({"job_title": "x", "location": "Houston, TX", "description": description})
                self.assertEqual(job["salary"], "")
                self.assertEqual(job["extraction"]["salary_candidates"], [])

        # Large real compensation is retained when employee pay is explicit.
        self.assertEqual(pay("Total compensation: $1.2M per year", "Houston, TX"),
                         "$1,200,000 per year (total compensation)")

    def test_only_on_target_earnings_is_used_and_labelled(self) -> None:
        self.assertEqual(pay("OTE: $140,000 - $200,000 per year"), "$140,000 - $200,000 per year (OTE)")


def years(description: str):
    job = extract_job({"job_title": "x", "description": description})
    return job["experience_min"], job["experience_max"]


class ExperienceTests(unittest.TestCase):
    def test_requirement_forms(self) -> None:
        self.assertEqual(years("lenders 2+ years in business development or sales roles Track record of"), (2.0, None))
        self.assertEqual(years("*Work Experience: * 2-4 years *Supervisory Experience: * 1-3 years"), (2.0, 4.0))
        self.assertEqual(years("Experience:\nTechnical support or help desk: 3–4 years (Required)"), (3.0, 4.0))
        self.assertEqual(years("4 Years in a similar role Comfort working with Microsoft Office"), (4.0, None))
        self.assertEqual(years("Minimum solutioning experience of 10+ years with various segments"), (10.0, None))
        self.assertEqual(years("Basic Qualifications: 5 years with BS/BA; 9 years with a high school diploma."), (5.0, None))
        self.assertEqual(years("What We're Looking For\n7+ years of experience in procurement, sourcing"), (7.0, None))
        self.assertEqual(years("Must-have:\n3–4 years of experience building native mobile applications"), (3.0, 4.0))
        self.assertEqual(years("Bachelor's degree and 5+ years (Staff) or 8+ years (Senior) of relevant experience"), (5.0, None))
        self.assertEqual(years("Qualifications\n3-8 years of Project Management, Assistant Project Management"), (3.0, 8.0))
        self.assertEqual(years("project superintendent; 3-5 years minimum. OSHA 10"), (3.0, 5.0))
        self.assertEqual(years("More than 1 year of experience working with individuals"), (1.0, None))
        self.assertEqual(years("1+ years of experience delivering exceptional customer service"), (1.0, None))
        self.assertEqual(years("Solid experience (2+ years) in Software Testing"), (2.0, None))

    def test_ages_and_company_history_are_not_requirements(self) -> None:
        for text in ("For more than 30 years, Bosch Service Solutions offers services", "Must be at least 18 years old.",
                     "Building on 21 years of continuous growth and success", "With 60 years of experience across industries",
                     "has spent nearly 20 years bringing communities together", "children from 2-15 years old",
                     "4-week sabbatical after 4 years at Proxymity", "We have been 30 years in business serving clients",
                     "a contractor with more than 30 years of experience delivering projects",
                     "Requires 2-3 years of college-level courses"):
            self.assertEqual(years(text), (None, None), text)


class WorkModeTests(unittest.TestCase):
    def mode(self, description: str) -> str:
        return extract_job({"job_title": "x", "description": description})["work_mode"]

    def test_stated_arrangements(self) -> None:
        self.assertEqual(self.mode("This position is designated as onsite."), "Onsite")
        self.assertEqual(self.mode("These positions are ON-SITE in Aurora Colorado."), "Onsite")
        self.assertEqual(self.mode("Willing to work 5 days in the office per HR guideline"), "Onsite")
        self.assertEqual(self.mode("Work environment: Fully onsite during regular business hours"), "Onsite")
        self.assertEqual(self.mode("Eligible for some hybrid work flexibility, but regular in-office presence is expected."), "Hybrid")

    def test_mentions_that_are_not_the_arrangement(self) -> None:
        for text in ("providing on-site Manufacturing Engineering oversight", "present information in person to stakeholders",
                     "hybrid-electric aircraft that takes off", "We support remote monitoring integration"):
            self.assertEqual(self.mode(text), "", text)

    def test_an_intern_title_beats_a_full_time_board_label(self) -> None:
        job = extract_job({"job_title": "IT Support Intern", "description": "Resolve issues.", "structured_fields": {"job_type": "Full time"}})
        self.assertEqual(job["job_type"], "internship")

    def test_board_values_in_free_text(self) -> None:
        job = extract_job({"job_title": "x", "description": "Great role.", "structured_fields": {"work_mode": "In-Office"}})
        self.assertEqual(job["work_mode"], "Onsite")



class LegacyPassTests(unittest.TestCase):
    def test_pipeline_final_pass_keeps_the_extractors_results(self) -> None:
        from job_extraction.jd_enricher import enrich
        text = ("With 60 years of experience across industries, we offer medical, dental, 401k. "
                "Strong contract negotiation skills. We use hybrid cloud tools.")
        job = enrich(extract_job({"job_title": "Buyer", "description": text}))
        self.assertEqual((job["salary_min"], job["experience_min"], job["work_mode"], job["job_type"]), (None, None, "", ""))
        # Rows that never went through the extractor still get the old best-effort fill.
        self.assertEqual(enrich({"job_title": "Buyer", "description": "Fully remote. 5+ years of SAP."})["work_mode"], "remote")


if __name__ == "__main__":
    unittest.main()
