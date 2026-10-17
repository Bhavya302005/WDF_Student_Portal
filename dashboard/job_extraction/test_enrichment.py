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


if __name__ == "__main__":
    unittest.main()
