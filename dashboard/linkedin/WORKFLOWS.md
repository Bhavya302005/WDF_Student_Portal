# Three LinkedIn workflows in WDF

Use Actions to start any of these independently:

| Workflow | Catalog range | Default runners | Roles per runner |
| --- | --- | --- | --- |
| LinkedIn Roles 1-23 | 1–23 | 8 | 3, 3, 3, 3, 3, 3, 3, 2 |
| LinkedIn Roles 24-45 | 24–45 | 8 | 3, 3, 3, 3, 3, 3, 2, 2 |
| LinkedIn Roles 46-68 | 46–68 | 8 | 3, 3, 3, 3, 3, 3, 3, 2 |

The latter two call `linkedin_roles_slice.yml` as a reusable workflow, so all
three use the same scraper, extraction, repost logic and export code. Filters
narrow each workflow's own range before sharding; overlapping names cannot pull
roles from another workflow. Leave filters blank to run its entire range.

Each run produces one Excel workbook containing only the **All** sheet. Artifact
names and filenames include the range. Runs remain independent: start all three
if all 68 roles are needed. With the defaults that creates 24 scrape jobs, subject
to the account's concurrent-runner limits. Each job gets its own hosted runner;
using one repository does not give all jobs one fixed IP. Unique public IPs are
not guaranteed by GitHub. See [GitHub-hosted runners](https://docs.github.com/en/actions/concepts/runners/github-hosted-runners).

## Migration from getworkin_2 and getworkin_3

Both former repos had identical scraper/library files; only workflow ranges and
role defaults differed. Their enrichment engine was jd-v6, ahead of WDF's jd-v5.
WDF now uses jd-v7 from Getworkin, which includes those v6 changes plus the later
project-budget/measurement false-positive safeguards. Their matching-field
adapter is also preserved, so its older fallback parsers do not overwrite
evidence-checked extraction results. The role catalog and guest-search code
already match; the shared scraper retains the newer sharding and repost fixes.

Full Git-history bundles, repository metadata and all 125 non-expired Actions
artifacts were backed up locally before removing the redundant repositories.
Secret values cannot be exported from GitHub; the workflows use WDF's existing
secrets. No new scraping or database write is part of the migration validation.
