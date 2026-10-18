# Matching Engine Production-Readiness Audit

Date: 2026-10-07  
Verdict: **Not production-ready for 200k jobs yet.** The global-data model is correct and PostgreSQL retrieval is bounded, but the production ingestion consumer, persistent repository/cache implementation, and multi-instance locking path are incomplete.

## Scope and environment

- Isolated PostgreSQL 17.10 development instance; no production systems modified.
- pgvector 0.8.7, 384 dimensions, HNSW cosine index and GIN lexical index.
- 200,000 synthetic jobs: 10 roles, 6 domains, 10 primary skills, 7 seniority levels, varied experience, 8 locations/countries and 3 work modes.
- 1,000 base vectors were made job-unique before final ANN measurements.
- Database configured with 512 MB `shared_buffers` and 512 MB `maintenance_work_mem`.
- 500k was not run: the 200k application benchmark already failed readiness latency and architecture gates, so the added cost would not change the release decision.

## Audited data flow

```text
jobs upsert
  -> matching_job_queue (one coalesced row per job_id)
  -> claim_job_intelligence_batch() / artifact lease
  -> JobProcessor(job_id)
  -> artifact input hashes + processing/model versions
  -> job_intelligence (one global row per job)
  -> shared HNSW + GIN + metadata indexes

candidate profile
  -> global ANN + lexical retrieval with DB eligibility filters
  -> RRF (<= 1,000 candidates)
  -> deterministic scoring (<= 200)
  -> deterministic explanation
  -> recommendation cache keyed by profile + request + algorithm + corpus generation
```

The intended boundary is present: job processing has no `user_id` input and recommendation code never invokes `JobProcessor`. Candidate-specific logic begins only after retrieval of global rows.

## Compute-once / cross-user proof

The exact required scenario was executed in PostgreSQL:

| Measurement | Result |
|---|---:|
| User A result set | 10,000 |
| User B result set | 7,000 |
| Overlap | 3,000 |
| Changed global rows | 0 |
| Job embedding calls during requests | 0 |
| LLM calls during requests | 0 |
| Reuse of overlapping artifacts | 100% |
| Duplicate global computation | 0% |

Before and after both selections, all 200,000 rows had the same maximum `processed_at` and identical aggregate source-hash checksum. The execution path is retrieval -> hydration -> candidate scoring; it contains no normalization, skill extraction, role/domain classification or job embedding call.

## Idempotency and concurrency

- Eight simultaneous queue consumers claimed 800 rows: 800 unique IDs, zero duplicates (`FOR UPDATE SKIP LOCKED`).
- Sixteen simultaneous claims for one artifact: one winner, fifteen rejected.
- Sixteen identical recommendation requests: one actual retrieval, fifteen cache hits; wall time 392 ms.
- Same-process request/profile locks use double-checked caching.
- PostgreSQL leases exist, but the current production worker does not yet connect `JobProcessor` to those leases and persistent storage. Therefore cross-process processing safety is proven at the SQL primitive level, not end to end.

## Hashes, versions and caches

Verified/fixed during audit:

- Global artifact identity is `job_id` plus artifact input hash and processing/model versions; never `user_id + job_id`.
- Taxonomy content hash now participates in skill and role artifact hashes.
- Normalization, processing and embedding versions participate in their dependent artifact hashes.
- Candidate profile hashes exclude `user_id`, stale embeddings, supplied hashes and unknown input fields.
- Request list fields are sorted before hashing, preventing order-only cache misses.
- Request cache keys include candidate profile hash, filters, limit, algorithm version and a constant-time PostgreSQL corpus generation.
- Any global insert/update/delete increments corpus generation, preventing stale cached matches after updates or expiration.
- Algorithm changes still require operators to bump `algorithm_version`; this is not automatic.

## Dependency invalidation

| Change | Invalidated artifacts | Embedding regenerated |
|---|---|---:|
| Logo only | none | no |
| Location only | location, embedding | yes |
| Required skills | skills, embedding | yes |
| Description | normalized text, skills, embedding | yes |
| Expiration/status | status only | no |
| Embedding model/version | embedding only | yes |
| Taxonomy content | skills and role; embedding if matching text changes | conditional |

All matrix cases pass automated regression tests. Location is embedded intentionally, so a location change correctly affects the matching representation.

## Hidden O(N) audit

1. `HybridRetriever` calls `ready_jobs()` and scans all jobs. It is safe only as the documented local/test adapter and must never back the production API.
2. `PostgresHybridRetriever` is bounded by configured ANN/lexical limits and hydrates at most 1,000 rows. It does not scan/process the corpus in Python.
3. Common lexical queries can still rank a large GIN match set inside PostgreSQL; this is index-assisted but data-dependent.
4. `worker.process_file()` calls `ready_jobs()` for a final count, an offline O(N) operation.
5. Scoring is O(K), where K is capped at 200; RRF is O(R), capped at two lists of 700.

The API uses dependency injection and has no production application factory. Deployment must explicitly inject `PostgresHybridRetriever`; accidentally using `local_stack()` would restore the forbidden O(N) path.

## 200k benchmark results

### Ingestion and indexing

| Operation | Result |
|---|---:|
| `jobs` insert plus queue trigger | 200k in 4.686 s (42,680 rows/s) |
| Global intelligence insert plus live HNSW maintenance | 200k in 68.227 s (2,931 rows/s) |
| Bulk vector update without HNSW | 200k in 13.733 s (14,564 rows/s) |
| HNSW rebuild | 43.721 s (4,574 rows/s) |

These are synthetic-vector/index figures, not BGE model embedding throughput. A real embedding-provider benchmark remains required.

### Warm component latency (30 samples)

| Component | P50 | P95 | P99 | Max |
|---|---:|---:|---:|---:|
| Semantic ANN with role filter | 27.58 ms | 42.25 ms | 55.55 ms | 75.31 ms |
| Lexical GIN retrieval | 0.40 ms | 0.56 ms | 0.58 ms | 4.83 ms |
| Python RRF | 0.29 ms | 0.45 ms | 0.45 ms | 2.22 ms |
| Hydrate up to 1,000 rows | 63.17 ms | 88.74 ms | 95.46 ms | 97.97 ms |
| Score 200 candidates | 10.14 ms | 14.53 ms | 15.81 ms | 24.28 ms |
| Total retrieval | 135.88 ms | 223.60 ms | 235.90 ms | 377.68 ms |

### End-to-end latency (40 forced cache misses)

| Percentile | Latency |
|---|---:|
| P50 | 153.43 ms |
| P95 | 435.65 ms |
| P99 | 475.53 ms |
| Mean | 207.50 ms |

### Concurrent requests

- Sixteen identical requests: 392 ms wall time, one retrieval, 15 cache hits (93.75% hit rate), zero duplicate retrievals.
- Sixteen overlapping but distinct candidate requests: 1,372 ms wall time; per-request P50 805 ms, P95/P99 approximately 1,296 ms; 16 retrievals.
- Mixed benchmark database delta: 858,777 buffer hits, 68,320 reads, no temp spill, 256 committed transactions.

## Database metrics

- Fresh initial intelligence relation: 513 MB; queue: 104 MB.
- Final temporary database: 1,786 MB after a deliberately cancelled indexed bulk update created bloat. This demonstrates the need for controlled re-embedding/reindex procedures.
- Cumulative buffer hit ratio during the entire audit was approximately 99.65%; the final mixed concurrent run hit ratio was approximately 92.63%.
- PostgreSQL cumulative active time: 662,526 ms across 330 sessions.
- `track_io_timing` was disabled, so read/write time was not available.
- A reliable CPU-percent sample was not captured during peak load. Production certification therefore still requires CPU, RSS, IOPS and connection-pool telemetry under a controlled load harness.

## LLM and embedding call scaling

- New job: one job embedding when its embedding input/version is new.
- Unchanged job: zero extraction and embedding calls.
- Logo-only/status-only update: zero embedding calls.
- Skill/location/description update: one embedding call because matching text changes.
- Recommendation request: zero job embeddings and zero LLM calls.
- Gemini disabled: all tests and recommendations pass using grounded deterministic explanations.
- There is no active Gemini explanation implementation in the online service, so LLM calls cannot scale with retrieved jobs; optional shortlist explanations remain future work.

## Top five measured bottlenecks

1. Hydration and Pydantic conversion of up to 1,000 wide rows: P50 63 ms.
2. Filtered semantic ANN: P50 28 ms and data-dependent planner behavior.
3. Multi-user concurrent retrieval: P95 about 1.30 s for 16 overlapping distinct requests.
4. Live HNSW maintenance: roughly 2,931 intelligence rows/s versus 14,564 rows/s without the index.
5. Common-query end-to-end tail: P95 436 ms even on a local socket with no network/API gateway.

## Top five architectural risks

1. No PostgreSQL `MatchingRepository`/queue consumer persists `JobProcessor` output; the supplied worker is memory/file based.
2. SQL leases and Python processing are not integrated into one transaction/lease lifecycle, so distributed end-to-end exactly-once behavior is incomplete.
3. Candidate/profile/match caches are process-local; the `job_matches` table and Redis are not wired into the service.
4. The production embedding provider is absent; feature hashing is only a fallback and synthetic vectors do not establish ranking quality or real embedding throughput.
5. Deployment can accidentally inject the O(N) local retriever because no production-only factory/startup guard exists.

Additional risks: no connection pool, no persistent metrics exporter, no labeled relevance benchmark, no ANN recall measurement, and no 500k/1M run.

## Required work before 200k production

1. Implement a PostgreSQL repository and queue worker that atomically claims, processes and persists global artifacts with retry/lease recovery.
2. Wire a pooled `PostgresHybridRetriever` into a production application factory; fail startup if the in-memory retriever is selected outside test/dev.
3. Persist/share candidate and recommendation caches across API instances and validate corpus-generation invalidation.
4. Integrate and benchmark the chosen BGE/Sentence Transformers provider with batching and versioned backfill.
5. Add controlled load tests with CPU/RSS/IOPS/connection metrics and ANN recall, plus the 100–300-example relevance evaluation.

## Before 500k/1M

- Pass the 200k gates above first.
- Re-run HNSW parameter/recall experiments with real unique embeddings and production filter cardinalities.
- Partition or compact wide hydrated data, benchmark covering/narrow projections, and define online/offline reindex strategy.
- Benchmark 500k and 1M with production connection pooling, cache service and concurrent traffic distribution.
- Consider Qdrant/OpenSearch only if measured PostgreSQL quality, latency or operational limits fail; current evidence does not yet justify migration.

## Final conclusion

The implementation now has the correct conceptual and SQL retrieval boundary:

```text
GLOBAL JOB INTELLIGENCE (one row and one embedding per job/version)
  -> shared indexes
  -> all users retrieve the same artifacts
  -> candidate-specific bounded ranking
```

It does **not** process jobs per user, and the 10k/7k/3k proof showed zero global recomputation. However, the system is not ready to operate this architecture reliably across production processes because the persistent ingestion worker/repository and distributed caches are unfinished. The correct release decision is **no-go for 200k production today**, followed by the five completion items above.
