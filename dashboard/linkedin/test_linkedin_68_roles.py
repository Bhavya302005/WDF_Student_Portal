from __future__ import annotations

import unittest

from linkedin_68_roles import (
    _merge_job_details,
    _parse_linkedin_detail_html,
    enrich_jobs_after_details,
)


SAMPLE_JOB_PAGE = """
<html>
  <head>
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Senior Procurement Manager",
        "hiringOrganization": {"@type": "Organization", "name": "Example Corp"},
        "datePosted": "2026-10-15",
        "employmentType": "FULL_TIME",
        "jobLocationType": "TELECOMMUTE",
        "skills": "Strategic Sourcing, SQL, SAP",
        "experienceRequirements": "5+ years of procurement experience",
        "educationRequirements": "Bachelor's degree",
        "industry": "Manufacturing",
        "jobLocation": {
          "@type": "Place",
          "address": {
            "addressLocality": "Austin",
            "addressRegion": "TX",
            "addressCountry": "US"
          }
        },
        "baseSalary": {
          "@type": "MonetaryAmount",
          "currency": "USD",
          "value": {"minValue": 120000, "maxValue": 150000, "unitText": "YEAR"}
        },
        "description": "<h2>Responsibilities</h2><p>Lead sourcing and vendor negotiations.</p><h2>Basic Qualifications</h2><p>5+ years of procurement experience and SQL.</p><h2>Preferred Qualifications</h2><p>MBA and SAP experience.</p>"
      }
    </script>
  </head>
  <body>
    <ul>
      <li class="description__job-criteria-item">
        <h3>Seniority level</h3><span class="description__job-criteria-text">Mid-Senior level</span>
      </li>
      <li class="description__job-criteria-item">
        <h3>Job function</h3><span class="description__job-criteria-text">Purchasing</span>
      </li>
      <li class="description__job-criteria-item">
        <h3>Industries</h3><span class="description__job-criteria-text">Manufacturing</span>
      </li>
    </ul>
    <code id="applyUrl">https://www.linkedin.com/redir/redirect?url=https%3A%2F%2Fexample.com%2Fapply%2F123</code>
  </body>
</html>
"""


class LinkedInDetailParserTests(unittest.TestCase):
    def test_repost_label_survives_detail_merge_and_enrichment(self):
        html = SAMPLE_JOB_PAGE.replace('<body>', '<body><section class="top-card-layout">'
                                       '<span class="posted-time-ago__text">Reposted 2 days ago</span></section>')
        details = _parse_linkedin_detail_html(html)
        self.assertTrue(details['reposted'])
        job = {'job_title': 'Buyer', 'description': '', 'reposted': None}
        _merge_job_details(job, details)
        enriched = enrich_jobs_after_details([job])[0]
        self.assertTrue(enriched['reposted'])
        self.assertEqual(enriched['reposted_source'], 'linkedin_label')
        self.assertNotIn('reposted', _parse_linkedin_detail_html(SAMPLE_JOB_PAGE))

    def test_extracts_and_enriches_required_fields(self) -> None:
        details = _parse_linkedin_detail_html(SAMPLE_JOB_PAGE)
        self.assertEqual(details["job_title"], "Senior Procurement Manager")
        self.assertEqual(details["company"], "Example Corp")
        self.assertEqual(details["posted_at"], "2026-10-15")
        self.assertEqual(details["job_type"], "full time")
        self.assertEqual(details["work_mode"], "remote")
        self.assertEqual(details["salary_min"], 120000)
        self.assertEqual(details["salary_max"], 150000)
        self.assertEqual(details["salary_currency"], "USD")
        self.assertEqual(details["apply_url"], "https://example.com/apply/123")
        self.assertIn("Strategic Sourcing", details["skills"])
        self.assertIn("Bachelor", details["education_requirements"])
        self.assertIn("Lead sourcing", details["key_responsibilities"])
        self.assertIn("5+ years", details["basic_qualifications"])

        raw_job = {
            "id": "linkedin-test-123",
            "search_role": "Procurement Manager",
            "role_category": "Procurement Manager",
            "job_title": "Procurement Manager",
            "company": "Example Corp",
            "location": "Austin, TX",
            "job_url": "https://www.linkedin.com/jobs/view/123",
            "apply_url": "https://www.linkedin.com/jobs/view/123",
            "description": "",
            "source_board": "LinkedIn",
            "structured_fields": {},
        }
        _merge_job_details(raw_job, details)
        enriched = enrich_jobs_after_details([raw_job])[0]

        self.assertEqual(enriched["country"], "US")
        self.assertEqual(enriched["posted_at"], "2026-10-15T00:00:00+00:00")
        self.assertEqual(enriched["experience_min"], 5.0)
        self.assertIn("SQL", enriched["skills_required"])
        self.assertEqual(enriched["job_type"], "full_time")
        self.assertEqual(enriched["work_mode"], "Remote")
        self.assertEqual(enriched["role_category"], "Procurement Manager")

if __name__ == "__main__":
    unittest.main()
