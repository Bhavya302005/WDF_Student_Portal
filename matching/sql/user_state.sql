-- Apply once in the Neon SQL editor after matching/sql/schema.sql.
-- Stores each signed-in user's feed and actions independently.
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
