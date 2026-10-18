from __future__ import annotations

import sys
import types
import unittest

try:
    import psycopg  # noqa: F401
except ImportError:  # map_job needs no connection; stub the driver for local runs.
    json_module = types.ModuleType("psycopg.types.json")
    json_module.Jsonb = lambda obj: obj
    sys.modules.update({
        "psycopg": types.ModuleType("psycopg"),
        "psycopg.types": types.ModuleType("psycopg.types"),
        "psycopg.types.json": json_module,
    })

from db.neon_upsert import map_job


def _unwrap(value):
    return getattr(value, "obj", value)


class MapJobTests(unittest.TestCase):
    def test_canonical_fields_win_over_aliases_regardless_of_order(self) -> None:
        row = map_job({
            "id": "a", "job_title": "Buyer",
            "skills": "SAP, Excel, Procurement", "skills_required": '["SAP"]', "skills_preferred": "['Procurement']",
            "scraped_at": "2026-10-18 05:00:00", "posted_at": "2026-10-09",
        })
        self.assertEqual(_unwrap(row["skills_required"]), ["SAP", "Excel"])
        self.assertEqual(_unwrap(row["skills_preferred"]), ["Procurement"])
        self.assertEqual(row["posted_at"].date().isoformat(), "2026-10-09")

    def test_posted_date_falls_back_to_created_then_scraped(self) -> None:
        created = map_job({"id": "a", "job_title": "Buyer", "scraped_at": "2026-10-18", "created_at": "2026-10-01"})
        self.assertEqual(created["posted_at"].date().isoformat(), "2026-10-01")
        scraped = map_job({"id": "b", "job_title": "Buyer", "scraped_at": "2026-10-18"})
        self.assertEqual(scraped["posted_at"].date().isoformat(), "2026-10-18")

    def test_blank_csv_values_are_stored_as_null(self) -> None:
        row = map_job({"id": "a", "job_title": "Buyer", "basic_qualifications": "", "salary": " ", "salary_min": ""})
        self.assertIsNone(row["basic_qualifications"])
        self.assertIsNone(row["salary"])
        self.assertIsNone(row["salary_min"])

    def test_engine_values_map_to_neon_vocabulary(self) -> None:
        row = map_job({"id": "a", "job_title": "Buyer", "work_mode": "Hybrid", "job_type": "full_time", "is_remote": "False"})
        self.assertEqual((row["work_mode"], row["job_type"], row["is_remote"]), ("hybrid", "full time", False))


if __name__ == "__main__":
    unittest.main()
