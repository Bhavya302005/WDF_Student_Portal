-- Semantic index for the job corpus, kept in its own database (MATCHING_DATABASE_URL)
-- so vectors do not compete with public.jobs for storage. Idempotent migration.
-- Job details stay in the jobs database; this table holds only what ANN search
-- and hard filters need. There is no foreign key: public.jobs lives elsewhere.
create extension if not exists vector;
-- Load pgvector's library so its hnsw.* settings exist: without it, a non-superuser
-- (Neon) may not name them in match_job_vectors' SET clause below.
select '[1]'::halfvec;

create table if not exists public.job_vectors (
  job_id text primary key,
  -- Half precision: identical ranking for cosine search at half the storage.
  embedding halfvec(384) not null,
  title text not null,
  company_key text not null default '',
  role_family text,
  role_subfamily text,
  seniority_level real,
  location text,
  country text,
  work_mode text,
  employment_type text,
  experience_min_years real,
  posted_at timestamptz,
  active boolean not null default true,
  source_hash text not null,
  embedding_model text not null,
  embedding_version text not null,
  source_updated_at timestamptz,
  synced_at timestamptz not null default now()
);

-- The HNSW index lives in vector_store_index.sql: bulk loads build it once at the end.
create index if not exists job_vectors_source_updated_idx on public.job_vectors (source_updated_at);
-- Identical text embeds to an identical vector: duplicate postings (22% of the
-- corpus) reuse a stored vector by its source_hash instead of re-running the model.
create index if not exists job_vectors_source_hash_idx on public.job_vectors (source_hash);

create table if not exists public.job_vector_sync (
  singleton boolean primary key default true check (singleton),
  last_source_updated_at timestamptz,
  last_full_sync_at timestamptz,
  generation bigint not null default 1,
  updated_at timestamptz not null default now()
);
insert into public.job_vector_sync(singleton) values (true) on conflict do nothing;

-- Filter-first ANN. Filters mirror retrieval._source_filters and may only be
-- looser than the Python eligibility rules, which re-check every candidate.
create or replace function public.match_job_vectors(
  p_embedding halfvec(384), p_limit integer default 700,
  p_work_modes text[] default null, p_employment_types text[] default null,
  p_locations text[] default null, p_blocked_companies text[] default null,
  p_max_required_experience real default null, p_max_job_age_days integer default null)
returns table(job_id text, similarity real)
language sql stable
set hnsw.iterative_scan = 'relaxed_order'
set hnsw.ef_search = 200
set hnsw.max_scan_tuples = 50000 as $$
  with candidates as materialized (
    select v.job_id, v.embedding <=> p_embedding as distance
    from job_vectors v
    where v.active
      and (p_work_modes is null or v.work_mode = any(p_work_modes))
      and (p_employment_types is null or v.employment_type = any(p_employment_types))
      and (p_locations is null or v.location ilike any(p_locations) or v.work_mode = 'remote')
      and (p_blocked_companies is null or v.company_key <> all(p_blocked_companies))
      and (p_max_required_experience is null or v.experience_min_years is null
           or v.experience_min_years <= p_max_required_experience)
      and (p_max_job_age_days is null or v.posted_at is null
           or v.posted_at >= now() - make_interval(days => p_max_job_age_days))
    order by distance
    limit least(p_limit, 2000)
  )
  select job_id, (1 - distance)::real from candidates order by distance
$$;
