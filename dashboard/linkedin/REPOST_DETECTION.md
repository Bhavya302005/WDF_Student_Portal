# LinkedIn job repost evidence

The scraper reads the current job header's **Reposted** label during the existing
detail request. This is the primary evidence; it requires no additional request.
Text in the JD or in related-job cards is ignored. An ordinary “2 days ago” label
or a missing header does not prove that a job is new.

The slice workflow then applies `db/linkedin_reposts.py` to the merged rows:

1. Preserve an explicit scraped repost label, including after CSV serialization.
2. Where evidence is absent, allow a **labeled ID estimate** within the historical
   calibration range. Older-than-range IDs only suggest a repost when the listing
   date is sufficiently later than the earliest sample.
3. IDs newer than the last sample, missing dates and unsupported cases stay
   **Unknown**. They are not assigned the last calibration date.

Excel/CSV columns include `reposted`, `repost_status`, `reposted_source`,
`reposted_confidence`, `reposted_evidence`, and `first_posted_at_estimate`.
The boolean is blank for Unknown. Estimates are identified as estimated, including
negative estimates. The estimated date is kept separate from `first_posted_at`;
the classifier never fabricates a verified original posting date.

The calibration is a heuristic, not a LinkedIn guarantee. It no longer learns
original dates from the latest listing date, which could itself be a repost.
Only an explicitly sourced `first_posted_at` with
`first_posted_at_source=linkedin_original_date` can add a new sample automatically.
Legacy `is_reposted=False` values are not proof: the removed scraper implementation
defaulted that flag to false even when a page could not be read.

This classifies job postings, separately from the hiring-post scraper's social
reshare detection (“someone reposted this”). No social-post logic is changed.
