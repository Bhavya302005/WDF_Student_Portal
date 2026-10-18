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


if __name__ == "__main__":
    unittest.main()
