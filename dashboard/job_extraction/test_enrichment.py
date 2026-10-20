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


if __name__ == "__main__":
    unittest.main()
