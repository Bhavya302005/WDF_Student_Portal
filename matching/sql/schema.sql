-- Global job intelligence for PostgreSQL 15 + pgvector. Idempotent migration.
create extension if not exists vector;
create extension if not exists pgcrypto;

create table if not exists public.job_intelligence (
  job_id text primary key references public.jobs(id) on delete cascade,
  title text not null,
  normalized_title text not null,
  company text,
  cleaned_text text not null default '',
  matching_text text not null default '',
  role_family text,
  role_subfamily text,
  domain text,
  seniority text,
  required_skills text[] not null default '{}',
  preferred_skills text[] not null default '{}',
  experience_min_years real,
  experience_max_years real,
  education_requirements text[] not null default '{}',
  location text,
  country text,
  work_mode text,
  employment_type text,
  salary_min real,
  salary_max real,
  salary_currency text,
  salary_period text,
  posted_at timestamptz,
  source_board text,
  job_url text,
  apply_url text,
  active boolean not null default true,
  embedding vector(384),
  lexical tsvector generated always as
    (to_tsvector('english', coalesce(normalized_title,'') || ' ' || coalesce(matching_text,''))) stored,
  source_hash text not null,
  artifact_hashes jsonb not null default '{}',
  processing_version text not null,
  normalization_version text not null,
  embedding_model text not null,
  embedding_version text not null,
  processing_status text not null check (processing_status in ('PROCESSING','READY','FAILED')),
  processing_error text,
  processed_at timestamptz not null default now()
);

alter table public.job_intelligence add column if not exists salary_min real;
alter table public.job_intelligence add column if not exists salary_max real;
alter table public.job_intelligence add column if not exists salary_currency text;
alter table public.job_intelligence add column if not exists salary_period text;
alter table public.job_intelligence add column if not exists posted_at timestamptz;
alter table public.job_intelligence add column if not exists source_board text;
alter table public.job_intelligence add column if not exists job_url text;
alter table public.job_intelligence add column if not exists apply_url text;

create index if not exists job_intelligence_embedding_hnsw
  on public.job_intelligence using hnsw (embedding vector_cosine_ops) where active and processing_status='READY';
create index if not exists job_intelligence_lexical_gin on public.job_intelligence using gin (lexical);
create index if not exists job_intelligence_filter_idx
  on public.job_intelligence (active, processing_status, role_family, role_subfamily, seniority, country, work_mode, employment_type);
create index if not exists job_intelligence_required_skills_gin on public.job_intelligence using gin (required_skills);
create index if not exists job_intelligence_freshness_idx on public.job_intelligence(posted_at desc) where active and processing_status='READY';
create index if not exists job_intelligence_company_idx on public.job_intelligence(lower(company));

create table if not exists public.candidate_profiles (
  user_id uuid not null,
  profile_hash text not null,
  profile_json jsonb not null,
  embedding vector(384),
  embedding_model text not null,
  embedding_version text not null,
  processing_version text not null,
  processed_at timestamptz not null default now(),
  primary key (user_id, profile_hash)
);
create index if not exists candidate_profiles_hash_idx on public.candidate_profiles(profile_hash);

-- Autosaved onboarding fields are kept separately from confirmed, processed profiles.
create table if not exists public.candidate_profile_drafts (
  user_id uuid primary key,
  draft_json jsonb not null default '{}',
  updated_at timestamptz not null default now()
);

create table if not exists public.job_matches (
  user_id uuid not null,
  candidate_profile_hash text not null,
  request_hash text not null,
  job_id text not null references public.jobs(id) on delete cascade,
  match_score real not null,
  feature_scores jsonb not null,
  evidence_coverage real not null,
  explanation text not null,
  algorithm_version text not null,
  created_at timestamptz not null default now(),
  primary key (user_id, candidate_profile_hash, request_hash, job_id, algorithm_version)
);
create index if not exists job_matches_lookup_idx
  on public.job_matches(user_id, candidate_profile_hash, request_hash, algorithm_version, match_score desc);

-- Durable, per-account feed state.  The recommendation snapshot keeps old
-- sessions readable even when ranking code or job presentation fields evolve.
create table if not exists public.user_job_recommendations (
  user_id uuid not null,
  job_id text not null references public.jobs(id) on delete cascade,
  profile_hash text not null,
  algorithm_version text not null,
  recommendation_json jsonb not null,
  status text not null default 'recommended'
    check (status in ('recommended','saved','applied','dismissed')),
  first_recommended_at timestamptz not null default now(),
  last_recommended_at timestamptz not null default now(),
  seen_at timestamptz,
  status_updated_at timestamptz not null default now(),
  primary key (user_id, job_id)
);
create index if not exists user_job_recommendations_feed_idx
  on public.user_job_recommendations(user_id, last_recommended_at desc)
  where status <> 'dismissed';
create index if not exists user_job_recommendations_unread_idx
  on public.user_job_recommendations(user_id, first_recommended_at desc)
  where seen_at is null and status <> 'dismissed';

create table if not exists public.matching_processing_claims (
  artifact_key text primary key,
  owner_id uuid not null,
  lease_until timestamptz not null,
  updated_at timestamptz not null default now()
);

create table if not exists public.matching_metrics (
  bucket timestamptz not null default date_trunc('hour', now()),
  metric text not null,
  value bigint not null default 0,
  primary key (bucket, metric)
);

create table if not exists public.matching_state (
  singleton boolean primary key default true check (singleton),
  generation bigint not null default 1,
  updated_at timestamptz not null default now()
);
insert into public.matching_state(singleton) values(true) on conflict do nothing;

create or replace function public.bump_matching_generation() returns trigger language plpgsql as $$
begin
  update matching_state set generation=generation+1, updated_at=now() where singleton=true;
  return coalesce(new, old);
end $$;
drop trigger if exists job_intelligence_bump_generation on public.job_intelligence;
create trigger job_intelligence_bump_generation after insert or update or delete on public.job_intelligence
for each statement execute function public.bump_matching_generation();

create table if not exists public.matching_job_queue (
  job_id text primary key references public.jobs(id) on delete cascade,
  payload jsonb not null,
  source_hash text not null,
  status text not null default 'PENDING' check (status in ('PENDING','PROCESSING','DONE','FAILED')),
  attempts integer not null default 0,
  available_at timestamptz not null default now(),
  locked_at timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists matching_job_queue_pending_idx
  on public.matching_job_queue(status, available_at) where status in ('PENDING','FAILED');

-- Existing scraper upserts automatically create one coalesced async event per job.
-- to_jsonb keeps this migration compatible with the current jobs table as it evolves.
-- The payload is only hashed, never stored: workers read the current row from
-- public.jobs. Storing it duplicated every description (several hundred MB).
create or replace function public.enqueue_job_intelligence() returns trigger language plpgsql as $$
declare
  body jsonb;
  matching_payload jsonb;
  input_hash text;
begin
  body := to_jsonb(new);
  matching_payload := jsonb_build_object(
    'id', body->>'id', 'job_title', body->>'job_title', 'company', body->>'company',
    'description', body->>'description', 'skills_required', body->'skills_required',
    'skills_preferred', body->'skills_preferred', 'experience_min', body->'experience_min',
    'experience_max', body->'experience_max', 'experience', body->>'experience',
    'location', body->>'location', 'country', body->>'country', 'work_mode', body->>'work_mode',
    'is_remote', body->'is_remote', 'job_type', body->>'job_type',
    'role_category', body->>'role_category', 'status', coalesce(body->>'status', 'active'),
    'salary_min', body->'salary_min', 'salary_max', body->'salary_max',
    'salary_currency', body->>'salary_currency', 'salary_period', body->>'salary_period',
    'posted_at', coalesce(body->'posted_at', body->'posting_date', body->'created_at'),
    'source_board', body->>'source_board'
    , 'basic_qualifications', body->>'basic_qualifications'
    , 'preferred_qualifications', body->>'preferred_qualifications'
    , 'key_responsibilities', body->>'key_responsibilities'
    , 'extraction', body->'extraction'
    , 'job_url', coalesce(body->>'job_url', body->>'url')
    , 'apply_url', body->>'apply_url'
  );
  input_hash := encode(digest(matching_payload::text, 'sha256'), 'hex');
  insert into matching_job_queue(job_id, payload, source_hash)
  values (new.id::text, '{}'::jsonb, input_hash)
  on conflict (job_id) do update set
    payload=excluded.payload, source_hash=excluded.source_hash, status='PENDING',
    available_at=now(), updated_at=now(), last_error=null
  where matching_job_queue.source_hash <> excluded.source_hash;
  return new;
end $$;

drop trigger if exists jobs_enqueue_intelligence on public.jobs;
create trigger jobs_enqueue_intelligence after insert or update on public.jobs
for each row execute function public.enqueue_job_intelligence();

-- Workers claim bounded batches with SKIP LOCKED; recommendation requests never do this work.
create or replace function public.claim_job_intelligence_batch(p_limit integer default 100)
returns setof public.matching_job_queue language plpgsql security definer set search_path=public as $$
begin
  return query
  with claimed as (
    select q.job_id from matching_job_queue q
    where q.status in ('PENDING','FAILED') and q.available_at <= now()
    order by q.available_at, q.created_at for update skip locked limit least(p_limit, 1000)
  )
  update matching_job_queue q set status='PROCESSING', locked_at=now(), attempts=q.attempts+1, updated_at=now()
  from claimed where q.job_id=claimed.job_id returning q.*;
end $$;

-- Atomic lease prevents concurrent workers from performing the same expensive artifact operation.
create or replace function public.claim_matching_artifact(p_key text, p_owner uuid, p_lease_seconds integer default 300)
returns boolean language plpgsql security definer set search_path=public as $$
begin
  insert into matching_processing_claims(artifact_key, owner_id, lease_until)
  values (p_key, p_owner, now() + make_interval(secs => p_lease_seconds))
  on conflict (artifact_key) do update set
    owner_id=excluded.owner_id, lease_until=excluded.lease_until, updated_at=now()
  where matching_processing_claims.lease_until < now() or matching_processing_claims.owner_id=p_owner;
  return found;
end $$;

-- ANN retrieval is global and filter-first; callers fuse these ranks with lexical ranks via RRF.
drop function if exists public.matching_semantic_jobs(vector, integer);
drop function if exists public.matching_semantic_jobs(vector, integer, text[], text[], text[], text[]);
create or replace function public.matching_semantic_jobs(
  p_embedding vector(384), p_limit integer default 700,
  p_roles text[] default null, p_locations text[] default null,
  p_work_modes text[] default null, p_employment_types text[] default null,
  p_blocked_companies text[] default null, p_max_required_experience real default null,
  p_minimum_salary real default null, p_salary_currency text default null,
  p_max_job_age_days integer default null)
returns table(job_id text, semantic_score real)
language sql stable
set hnsw.iterative_scan='relaxed_order'
set hnsw.ef_search=200
set hnsw.max_scan_tuples=50000 as $$
  with candidates as materialized (
    select j.job_id, j.embedding <=> p_embedding as distance
    from job_intelligence j
    where j.active and j.processing_status='READY' and j.embedding is not null
      and (p_roles is null or j.role_subfamily = any(p_roles))
      and (p_locations is null or exists (select 1 from unnest(p_locations) loc where j.location ilike '%' || loc || '%') or j.country = any(p_locations) or j.work_mode='remote')
      and (p_work_modes is null or j.work_mode = any(p_work_modes))
      and (p_employment_types is null or j.employment_type = any(p_employment_types))
      and (p_blocked_companies is null or lower(j.company) <> all(select lower(x) from unnest(p_blocked_companies) x))
      and (p_max_required_experience is null or j.experience_min_years is null or j.experience_min_years <= p_max_required_experience)
      and (p_minimum_salary is null or (
        (p_salary_currency is null or upper(j.salary_currency)=upper(p_salary_currency)) and
        coalesce(j.salary_max,j.salary_min) * case lower(j.salary_period)
          when 'year' then 1 when 'annual' then 1 when 'annually' then 1 when 'month' then 12
          when 'week' then 52 when 'day' then 260 when 'hour' then 2080 else null end >= p_minimum_salary))
      and (p_max_job_age_days is null or j.posted_at is null or j.posted_at >= now() - make_interval(days => p_max_job_age_days))
    order by distance limit least(p_limit, 5000)
  )
  select candidates.job_id, (1-distance)::real from candidates order by distance
$$;

drop function if exists public.matching_lexical_jobs(text, integer);
drop function if exists public.matching_lexical_jobs(text, integer, text[], text[], text[], text[]);
create or replace function public.matching_lexical_jobs(
  p_query text, p_limit integer default 700,
  p_roles text[] default null, p_locations text[] default null,
  p_work_modes text[] default null, p_employment_types text[] default null,
  p_blocked_companies text[] default null, p_max_required_experience real default null,
  p_minimum_salary real default null, p_salary_currency text default null,
  p_max_job_age_days integer default null)
returns table(job_id text, lexical_score real) language sql stable as $$
  select j.job_id, ts_rank_cd(j.lexical, websearch_to_tsquery('english', p_query))::real
  from job_intelligence j
  where j.active and j.processing_status='READY'
    and j.lexical @@ websearch_to_tsquery('english', p_query)
    and (p_roles is null or j.role_subfamily = any(p_roles))
    and (p_locations is null or exists (select 1 from unnest(p_locations) loc where j.location ilike '%' || loc || '%') or j.country = any(p_locations) or j.work_mode='remote')
    and (p_work_modes is null or j.work_mode = any(p_work_modes))
    and (p_employment_types is null or j.employment_type = any(p_employment_types))
    and (p_blocked_companies is null or lower(j.company) <> all(select lower(x) from unnest(p_blocked_companies) x))
    and (p_max_required_experience is null or j.experience_min_years is null or j.experience_min_years <= p_max_required_experience)
    and (p_minimum_salary is null or (
      (p_salary_currency is null or upper(j.salary_currency)=upper(p_salary_currency)) and
      coalesce(j.salary_max,j.salary_min) * case lower(j.salary_period)
        when 'year' then 1 when 'annual' then 1 when 'annually' then 1 when 'month' then 12
        when 'week' then 52 when 'day' then 260 when 'hour' then 2080 else null end >= p_minimum_salary))
    and (p_max_job_age_days is null or j.posted_at is null or j.posted_at >= now() - make_interval(days => p_max_job_age_days))
  order by 2 desc limit least(p_limit, 5000)
$$;
