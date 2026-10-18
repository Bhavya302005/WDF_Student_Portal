-- ANN index for job_vectors. Applied after vector_store.sql; bulk backfills skip
-- it while loading and build it once afterwards, which is faster than maintaining
-- it per insert and yields a better-connected graph.
create index if not exists job_vectors_embedding_hnsw
  on public.job_vectors using hnsw (embedding halfvec_cosine_ops) where active;
