# ADP and iCIMS US CSV export

The **ADP and iCIMS USA CSV (parallel slices)** Actions workflow crawls the
checked-in catalog of 1,179 ADP and 4,814 iCIMS board candidates. These are
candidate employer boards, not a promise that every board is still live or has
US openings. The catalog comes from Getworkin's public-board lists and independent
Common Crawl/public-dataset discovery; it does not use FreeHire's private board database.
Per-board provenance is retained in `boards.json`.

The prepare job builds the MIT-licensed FreeHire adapters at the exact commit in
`upstream.json`, tests the bridge, then partitions boards into disjoint slices.
The default is 50 boards per slice: **24 ADP slices + 97 iCIMS slices = 121 jobs**.
Up to 20 slices run concurrently, subject to the account's Actions runner limits.
Each slice processes eight boards concurrently and has its own detail cache.
No model API key, production database, browser, or local scraper is required.

ADP uses public paginated requisition JSON followed by requisition detail JSON.
iCIMS enumerates sitemaps and reads the job's server-rendered iframe and JobPosting
metadata. Both retain full readable descriptions and direct ATS job/application URLs.
Jobs require explicit US location evidence; vague worldwide/remote geography is
excluded. The bundled Getworkin jd-v7 extractor fills evidence-supported salary,
skills, experience, education, work mode, and job type. Unstated values stay blank.
It includes the salary safeguards against project budgets and physical measurements.
No paid model fallback runs in this workflow.

After all slices finish, download **adp-icims-usa-csv** from the run's Artifacts:

- `adp_usa_jobs.csv`
- `icims_usa_jobs.csv`
- `adp_icims_usa_jobs.csv`
- `summary.json` and `board-results.json` for coverage and failures

CSVs use UTF-8 with BOM, quote multiline descriptions, and preserve nested fields
as JSON cells. Exact duplicate provider/job URLs are removed; distinct requisitions
are retained. The merge reads one slice at a time to bound memory usage.

Failed or partial slices do not cancel other slices. Their successful results are
still uploaded and merged. The workflow remains failed when any board fails or a
slice is missing, so an incomplete crawl is not presented as fully successful.
Per-board deadlines are five minutes; very large or slow boards can return partial
results. Reports describe readable results, not guaranteed coverage of every live
job on the platform. Retry failed slices with **Re-run failed jobs**, or run again
after sites recover. Caches reuse previously hydrated details for seven days.

This branch starts the workflow on pushes affecting its files. After merging, use
**Actions → ADP and iCIMS USA CSV (parallel slices) → Run workflow**. `max_boards=0`
crawls the entire catalog; a positive number limits each provider. For a larger
catalog, increase `boards_per_slice` to remain within GitHub's 256-job matrix limit.
The partitioner rejects oversized matrices before launching crawlers. There is no
recurring schedule and no database write.

Upstream attribution: <https://github.com/strelov1/freehire> (MIT, see
`FREEHIRE_LICENSE`). Build tooling and the Go bridge originate in Getworkin's
`feat/ats-board-discovery` integration. The existing dashboard workflows are unchanged.
