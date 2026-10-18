# Matching engine

`matching` adds a global, reusable intelligence layer to the existing `jobs` ingestion pipeline.
It deliberately keeps global job artifacts separate from candidate-specific scores.
The eligibility and scoring contract is documented in `matching/MATCHING_POLICY.md`.

1. Apply `matching/sql/schema.sql` to the existing Supabase/PostgreSQL database.
2. Install API dependencies with `pip install -r requirements-api.txt`.
   `PostgresHybridRetriever` also needs `psycopg-pool>=3.2,<4` for its
   connection pool (`MATCHING_POOL_SIZE`, default 8).
3. Inject the production PostgreSQL repository/retriever into `RecommendationService`; `local_stack()` is only the deterministic local/test adapter.
4. Process ingestion events with `JobProcessor.process()`. Its artifact hashes make unchanged events no-ops and its storage lock/lease prevents stampedes.
5. Serve `create_app(service, intake)` behind the existing backend. Omitting
   `intake` preserves the structured-profile-only API.

The migration installs a trigger that coalesces existing `jobs` upserts into
`matching_job_queue`. Workers claim batches with `claim_job_intelligence_batch()`,
read each claimed job's current row from `public.jobs` (the queue stores only a
change hash) and pass it to `JobProcessor`; this keeps ingestion asynchronous
and ensures online recommendation never processes the corpus.

While `job_intelligence` is empty, recommendations come from a bounded fallback
over `public.jobs`: hard filters in SQL, title/role candidate generation, and the
same scorer. Measure ranking changes with the read-only benchmark:

```bash
NEON_DATABASE_URL=... python -m matching.benchmark --output bench.json
```

It reports nDCG@10, precision@10, strong@10 (exact target role), duplicate rate,
company concentration, hard-filter violations and latency for nine personas.
Its labels are independent title rules — a weak proxy for human judgement, fit
for comparing versions, not for absolute quality claims.

The default hashing embedder is an offline fallback, not the intended production quality model. A BGE/Sentence Transformers implementation can be injected through `EmbeddingProvider` without changing ingestion, retrieval, or scoring.

## Semantic index (separate database)

Vectors live in their own Postgres database so they do not compete with
`public.jobs` for storage. Set `MATCHING_DATABASE_URL` (next to
`NEON_DATABASE_URL` in `.env.local`) to enable it; without it, or while it is
empty or unreachable, retrieval falls back to title retrieval automatically.

- Model: `BAAI/bge-small-en-v1.5` via `fastembed` (384-d, ONNX on CPU, no torch).
  Profiles are embedded as queries (BGE instruction prefix), jobs as passages.
- Storage: `matching/sql/vector_store.sql` — `job_vectors` (half-precision
  vector + filter columns), an HNSW index and `match_job_vectors()` with the
  same hard filters as the title path. Applied automatically by the sync.
- Sync: `python -m matching.vector_sync` embeds only jobs whose title, skills or
  description changed since the last run; `--full` re-checks everything and
  removes vectors of jobs deleted from `public.jobs`. Run it after each weekly
  scrape. The first full run embeds the whole corpus (hours on a laptop CPU);
  later runs only embed new and changed jobs.
- CI: `.github/workflows/vector_index.yml` runs the same sync on up to 20
  parallel runners (`--shard K/N`, disjoint by a hash of the job id), weekly
  after the scrape or on demand (`mode: full` to rebuild). Full runs drop the
  ANN index, load in bulk and build it once at the end. Same model and text on
  every runner, so vectors are identical to a single-machine run. Shards are
  split by content, so identical postings (22% of the corpus) land on one
  runner and reuse a single model run's vector. Needs the
  repository secrets `NEON_DATABASE_URL` and `MATCHING_DATABASE_URL`.
- Retrieval: ANN results and title candidates are fused with RRF on ids, only
  the winners are hydrated from `public.jobs`, and every candidate gets a real
  similarity, mapped from `[MATCHING_SEMANTIC_FLOOR, MATCHING_SEMANTIC_CEILING]`
  (default 0.55-0.85) onto the scorer's 0-1 scale.

Integration tests need local PostgreSQL with pgvector:
`TEST_DATABASE_URL=dbname=matching_jobs_test TEST_VECTOR_DATABASE_URL=dbname=matching_vectors_test python -m unittest tests.test_vector_store`.

Run `python -m benchmarks.matching.run_synthetic --jobs 50000`; use the same
query mix against the pgvector functions at 200k, 500k, and 1M rows before
selecting HNSW parameters or introducing another vector store.

## Resume intake and editable questions

The candidate path is intentionally separate from global job intelligence:

`resume -> evidence-scoped profile -> editable answers -> candidate embedding -> retrieval/ranking`

`ResumeParser` extracts professional experience, internships, projects, skills,
roles, education, and certifications. Professional, internship, and project
evidence remain separate so project work is not counted as employment. The
same normalized resume is parsed once per parser/taxonomy version and safely
reused across concurrent requests; candidate answers and embeddings remain
candidate-specific.

Question definitions live in `matching/resume_questions.json`. Labels, help
text, required flags, choices, and ordering can be edited without changing the
parser. Submitted answers override inferred values before the existing
`RecommendationService` runs.

Endpoints enabled when `create_app` receives an intake service:

- `POST /api/v1/candidate-profile/parse` — parse pasted resume text.
- `POST /api/v1/candidate-profile/parse-file?user_id=...` — parse PDF, DOCX,
  TXT, MD, or RTF uploads.
- `POST /api/v1/candidate-profile/apply-answers` — validate and apply edited
  answers to a parsed intake.
- `POST /api/v1/recommendations/jobs/from-resume` — one-call text intake,
  answer application, retrieval, and deterministic ranking.

For local development, `local_intake_stack()` returns the repository, job
processor, recommendation service, and candidate intake service.

## Hirevia frontend integration

The development application enables CORS, resume-file intake, editable answers,
and enriched recommendation cards (job metadata, score evidence, and apply URL).
Start it with an existing scraper JSON export:

```bash
MATCHING_JOBS_FILE=greenhouse_api_66roles_jobs_us_strict.json \
  .venv_312/bin/uvicorn matching.server:app --reload --port 8000
```

Without a database URL, `matching.server` is a development-only in-memory app.
With `NEON_DATABASE_URL` (or `DATABASE_URL`) it uses `PostgresHybridRetriever`
for the globally precomputed job corpus and durable account state.

## Signed-in user feed persistence

Apply `matching/sql/user_state.sql` once in the Neon SQL editor. When
`NEON_DATABASE_URL` (or `DATABASE_URL`) is set, `matching.server` reads the
shared `job_intelligence` corpus from Neon and persists each account's latest
profile, recommendation history, unread state, and saved/applied/dismissed
actions. Without a database URL it intentionally falls back to in-memory state
for local tests.

The browser sends the Neon Auth JWT on every profile and recommendation call.
Set `NEON_AUTH_JWKS_URL` on the backend to require and verify those tokens;
leaving it unset is supported only for local development.

The API integration regression test is included in the normal suite:

```bash
.venv_312/bin/python -m unittest discover -s tests -v
```
