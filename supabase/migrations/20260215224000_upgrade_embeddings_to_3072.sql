-- Upgrade embedding dimensionality from 768 -> 3072.
-- This requires re-embedding; existing rows are cleared before schema change.
-- pgvector ANN indexes (ivfflat/hnsw) on this host cannot index vectors > 2000 dims,
-- so the old cosine ANN index is removed for 3072-dimensional embeddings.

truncate table public.embeddings;

drop index if exists public.idx_embeddings_embedding_cosine;
drop index if exists public.idx_embeddings_embedding_cosine_hnsw;

alter table public.embeddings
  drop constraint if exists embeddings_dim_check;

alter table public.embeddings
  alter column dim type integer,
  alter column embedding type vector(3072);

alter table public.embeddings
  add constraint embeddings_dim_check check (dim = 3072);
