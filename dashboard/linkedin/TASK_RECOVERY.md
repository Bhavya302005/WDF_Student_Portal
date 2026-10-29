LinkedIn workflows run one nationwide task per selected role, followed by five
disjoint state groups per role requiring expansion. A group contains at most 11
regions. The three role ranges remain 1–23, 24–45 and 46–80. The runners input
limits simultaneous tasks; it no longer assigns several roles to one time budget.

Each result batch is journaled and flushed before details are fetched or its
cursor advances. Individual successful detail responses are journaled too. A
worker saves a checkpoint and CSV snapshot on normal completion, budget expiry
and handled interruption. Its uploaded journal/checkpoint can recover data newer
than the last CSV snapshot. Unexpected worker errors remain visible failures;
exit 3 means saved partial data and is displayed as a warning instead.

To continue:

1. Open the same workflow and choose Run workflow.
2. Use the same role selection and post-time window.
3. Set resume_run_id to the earlier run's numeric ID.
4. Read crawl_progress.json and the workflow summary for pending regions,
   missing JDs and capped searches.

Matching task checkpoints restore completed regions and rewind one batch in an
unfinished region to accommodate shifted search results. Retained postings are
deduplicated by LinkedIn posting ID, not title/company/location. Later thin rows
cannot erase an already fetched description. Different posting IDs remain
separate even when their titles and locations match.

The delivery job merges earlier results with current CSVs and journals, including
failed/partial workers. It preserves raw CSV, enriched CSV, progress JSON and
checkpoints separately from Excel. Artifacts are requested for 30 days, subject
to repository retention/storage policy. The workflow does not expire database
jobs based on partial coverage.

Older runs (including the 330-minute shard in the screenshot) did not upload
page-level checkpoints. Their legacy shard CSVs can still be restored and merged
while the artifacts exist, but their exact page/state cursor cannot be recovered.
New tasks will enumerate those searches again rather than guess a cursor.

LinkedIn can still limit, block or inconsistently return results. Searches capped
at 1,000 remain labelled capped and need narrower segmentation; a cursor cannot
advance through that source limit. A force-stopped/deleted runner before artifact
upload is not guaranteed recoverable without an external durable sink. This
workflow preserves fetched data through its normal budget and retry paths and
does not claim exhaustive LinkedIn coverage.
