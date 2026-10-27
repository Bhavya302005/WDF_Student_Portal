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


class RowsToFreeTests(unittest.TestCase):
    def free(self, **overrides) -> int:
        values = dict(live_bytes=800e6, new_bytes=100e6, physical_jobs_bytes=850e6, headroom_bytes=0,
                      avg_row_bytes=5e3, buffer=1.0)
        values.update(overrides)
        from db.neon_upsert import rows_to_free
        return rows_to_free(**values)

    def test_nothing_to_free_when_new_rows_fit_in_existing_space_or_headroom(self) -> None:
        self.assertEqual(self.free(new_bytes=40e6), 0)                    # 800+40 <= 850: reuses freed space
        self.assertEqual(self.free(headroom_bytes=60e6), 0)               # 800+100 <= 850+60
        self.assertEqual(self.free(avg_row_bytes=0), 0)

    def test_frees_just_enough_rows_for_the_overflow(self) -> None:
        self.assertEqual(self.free(), 10_000)                             # 50 MB over / 5 KB per row
        self.assertEqual(self.free(headroom_bytes=25e6), 5_000)
        self.assertEqual(self.free(buffer=1.05), 10_500)


class MapJobTests(unittest.TestCase):
    def test_repost_csv_flags_keep_true_false_and_unknown_separate(self):
        for value, expected in ((True, True), ('True', True), ('False', False), ('', None), (None, None), ('unknown', None)):
            self.assertIs(map_job({'id': 'repost', 'job_title': 'Buyer', 'reposted': value})['reposted'], expected)

    def test_canonical_fields_win_over_aliases_regardless_of_order(self) -> None:
        row = map_job({
            "id": "a", "job_title": "Buyer",
            "skills": "SAP, Excel, Procurement", "skills_required": '["SAP"]', "skills_preferred": "['Procurement']",
            "scraped_at": "2026-10-18 05:00:00", "posted_at": "2026-10-09",
        })
        self.assertEqual(_unwrap(row["skills_required"]), ["SAP", "Excel"])
        self.assertEqual(_unwrap(row["skills_preferred"]), ["Procurement"])
        self.assertEqual(row["posted_at"].date().isoformat(), "2026-10-09")

    def test_posted_date_never_uses_scrape_time(self) -> None:
        created = map_job({"id": "a", "job_title": "Buyer", "scraped_at": "2026-10-18", "created_at": "2026-10-01"})
        self.assertEqual(created["posted_at"].date().isoformat(), "2026-10-01")
        scraped = map_job({"id": "b", "job_title": "Buyer", "scraped_at": "2026-10-18"})
        self.assertIsNone(scraped["posted_at"])

    def test_scrape_time_fallbacks_and_future_dates_are_not_posting_dates(self) -> None:
        # Scrapers write datetime.now() when the board gives no date.
        fallback = map_job({"id": "a", "job_title": "Buyer", "scraped_at": "2026-10-18T05:00:00+00:00",
                            "created_at": "2026-10-18T05:00:02+00:00"})
        self.assertIsNone(fallback["posted_at"])
        future = map_job({"id": "b", "job_title": "Buyer", "posted_at": "2099-01-01"})
        self.assertIsNone(future["posted_at"])
        real = map_job({"id": "c", "job_title": "Buyer", "scraped_at": "2026-10-18T05:00:00+00:00",
                        "posted_at": "2026-10-11T09:00:00+00:00"})
        self.assertEqual(real["posted_at"].date().isoformat(), "2026-10-11")

    def test_sections_contained_in_description_are_not_stored_twice(self) -> None:
        row = map_job({"id": "a", "job_title": "Buyer",
                       "description": "About us.\n\nRequirements:\n* 5 years of SAP\n* Excel",
                       "basic_qualifications": "5 years of SAP\nExcel",
                       "preferred_qualifications": "An MBA (not in the description)"})
        self.assertIsNone(row["basic_qualifications"])
        self.assertEqual(row["preferred_qualifications"], "An MBA (not in the description)")

    def test_extraction_keeps_values_but_drops_evidence(self) -> None:
        evidence = {"field": "description", "text": "x" * 500, "start": 0, "end": 500}
        row = map_job({"id": "a", "job_title": "Buyer", "extraction": {
            "version": "jd-v4", "source_fields": {"description": "y" * 5000},
            "skills": [{"name": "SAP", "requirement": "required", "source": "jd", "evidence": evidence}],
            "work_mode": {"value": "Remote", "status": "explicit", "source": "jd", "evidence": evidence},
            "salary_candidates": [{"min": 1, "max": 2, "currency": "USD", "period": "year", "evidence": evidence}],
        }})
        slim = _unwrap(row["extraction"])
        self.assertEqual(slim["skills"], [{"name": "SAP", "requirement": "required", "source": "jd"}])
        self.assertEqual(slim["work_mode"], {"value": "Remote", "status": "explicit", "source": "jd"})
        self.assertNotIn("source_fields", slim)
        self.assertNotIn("evidence", slim["salary_candidates"][0])

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
