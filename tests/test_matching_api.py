from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from matching.api import create_app
from matching.config import MatchingSettings
from matching.factory import local_intake_stack


RESUME = b"""Amin Example
PROFESSIONAL EXPERIENCE
Backend Software Engineer, Acme
Jan 2020 - Present
Built Python and FastAPI services backed by PostgreSQL on AWS.
SKILLS
Python, FastAPI, PostgreSQL, AWS
EDUCATION
Bachelor's in Computer Science
"""


class MatchingApiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.repository, jobs, service, intake = local_intake_stack(
            MatchingSettings(embedding_dimensions=64)
        )
        jobs.process({
            "id": "backend-1",
            "job_title": "Backend Software Engineer",
            "company": "Acme",
            "description": "Build Python FastAPI APIs using PostgreSQL and AWS.",
            "skills_required": ["Python", "FastAPI", "PostgreSQL"],
            "skills_preferred": ["AWS"],
            "experience_min": 2,
            "location": "Bengaluru, India",
            "work_mode": "Hybrid",
            "job_type": "Full Time",
            "source_board": "Greenhouse",
            "job_url": "https://example.test/jobs/backend-1",
            "apply_url": "https://example.test/jobs/backend-1/apply",
        })
        self.client = TestClient(create_app(
            service, intake, allowed_origins=["http://localhost:5173"]
        ))

    def test_frontend_resume_questionnaire_and_recommendation_flow(self):
        parsed_response = self.client.post(
            "/api/v1/candidate-profile/parse-file?user_id=user-a",
            files={"file": ("resume.txt", RESUME, "text/plain")},
        )
        self.assertEqual(200, parsed_response.status_code)
        parsed = parsed_response.json()
        self.assertTrue(parsed["questions"])

        answers = {
            question["id"]: question["answer"]
            for question in parsed["questions"]
        }
        answers.update({
            "target_roles": ["Backend Engineer"],
            "confirmed_skills": ["Python", "FastAPI", "PostgreSQL", "AWS"],
            "professional_years": 4,
            "work_modes": ["hybrid"],
            "employment_types": ["full time"],
            "hard_location_constraint": False,
            "work_authorization": "authorized without sponsorship",
            "requires_sponsorship": False,
        })
        confirmed_response = self.client.post(
            "/api/v1/candidate-profile/apply-answers",
            json={"intake": parsed, "answers": answers},
        )
        self.assertEqual(200, confirmed_response.status_code)
        confirmed = confirmed_response.json()

        recommendation_response = self.client.post(
            "/api/v1/recommendations/jobs",
            json={
                "user_id": "user-a",
                "profile": confirmed["profile"],
                "limit": 50,
                "roles": confirmed["profile"]["roles"],
                "work_modes": confirmed["profile"]["work_mode_preferences"],
                "employment_types": confirmed["profile"]["employment_preferences"],
            },
        )
        self.assertEqual(200, recommendation_response.status_code)
        recommendation = recommendation_response.json()["recommendations"][0]
        self.assertEqual("backend-1", recommendation["job_id"])
        self.assertEqual("Backend Software Engineer", recommendation["title"])
        self.assertEqual("https://example.test/jobs/backend-1/apply", recommendation["apply_url"])
        self.assertIn("python", recommendation["matched_required_skills"])
        self.assertTrue(recommendation["is_new"])
        self.assertEqual("recommended", recommendation["status"])
        self.assertEqual(1, self.repository.metrics["embeddings_generated"])

        latest = self.client.get("/api/v1/candidate-profile/latest?user_id=user-a")
        self.assertEqual(200, latest.status_code)
        self.assertEqual(confirmed["profile"]["profile_hash"], latest.json()["profile_hash"])

        edited_profile = {**latest.json(), "roles": ["Platform Engineer"], "minimum_salary": 150000}
        updated = self.client.put("/api/v1/candidate-profile", json=edited_profile)
        self.assertEqual(200, updated.status_code)
        self.assertIn("Platform Engineer", updated.json()["roles"])
        self.assertEqual(150000, updated.json()["minimum_salary"])
        restored = self.client.get("/api/v1/candidate-profile/latest?user_id=user-a")
        self.assertEqual(updated.json()["profile_hash"], restored.json()["profile_hash"])

        history = self.client.get("/api/v1/recommendations/jobs/history?user_id=user-a")
        self.assertEqual(1, history.json()["unread_count"])
        self.assertEqual("backend-1", history.json()["recommendations"][0]["job_id"])
        self.assertEqual([], self.client.get(
            "/api/v1/recommendations/jobs/history?user_id=user-b"
        ).json()["recommendations"])

        seen = self.client.post(
            "/api/v1/recommendations/jobs/seen",
            json={"user_id": "user-a", "job_ids": ["backend-1"]},
        )
        self.assertEqual(0, seen.json()["unread"])

        saved = self.client.patch(
            "/api/v1/recommendations/jobs/backend-1/status",
            json={"user_id": "user-a", "status": "saved"},
        )
        self.assertEqual("saved", saved.json()["status"])
        summary = self.client.get("/api/v1/recommendations/summary?user_id=user-a")
        self.assertEqual(1, summary.json()["saved"])

    def test_cors_preflight_allows_frontend(self):
        response = self.client.options(
            "/api/v1/recommendations/jobs",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(200, response.status_code)
        self.assertEqual(
            "http://localhost:5173",
            response.headers["access-control-allow-origin"],
        )

    def test_account_scoped_onboarding_draft(self):
        saved = self.client.put(
            "/api/v1/candidate-profile/draft",
            json={"user_id": "user-a", "form_data": {"roleSearch": "Platform Engineer"}},
        )
        self.assertEqual(200, saved.status_code)
        restored = self.client.get("/api/v1/candidate-profile/draft?user_id=user-a")
        self.assertEqual("Platform Engineer", restored.json()["form_data"]["roleSearch"])
        self.assertIsNone(self.client.get(
            "/api/v1/candidate-profile/draft?user_id=user-b"
        ).json())


if __name__ == "__main__":
    unittest.main()
