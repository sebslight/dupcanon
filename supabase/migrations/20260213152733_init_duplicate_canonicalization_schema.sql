-- Duplicate Canonicalization CLI v1 schema
-- Target: Supabase Postgres + pgvector

create extension if not exists vector;

-- ------------------------------------------------------------
-- Utilities
-- ------------------------------------------------------------

create or replace function public.set_updated_at_timestamp()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = timezone('utc', now());
  return new;
end;
$$;

-- ------------------------------------------------------------
-- Core tables
-- ------------------------------------------------------------

create table if not exists public.repos (
  id bigserial primary key,
  github_repo_id bigint not null unique,
  org text not null,
  name text not null,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (org, name)
);

create trigger set_repos_updated_at
before update on public.repos
for each row
execute function public.set_updated_at_timestamp();

create table if not exists public.items (
  id bigserial primary key,
  repo_id bigint not null references public.repos(id) on delete cascade,
  type text not null check (type in ('issue', 'pr')),
  number integer not null,
  url text not null,
  title text not null,
  body text,
  state text not null check (state in ('open', 'closed')),
  author_login text,
  assignees jsonb,
  labels jsonb,
  comment_count integer not null default 0 check (comment_count >= 0),
  review_comment_count integer not null default 0 check (review_comment_count >= 0),
  created_at_gh timestamptz,
  updated_at_gh timestamptz,
  closed_at_gh timestamptz,
  content_hash text not null,
  content_version integer not null default 1 check (content_version > 0),
  last_synced_at timestamptz not null,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (repo_id, type, number)
);

create trigger set_items_updated_at
before update on public.items
for each row
execute function public.set_updated_at_timestamp();

create index if not exists idx_items_repo_type_state on public.items (repo_id, type, state);
create index if not exists idx_items_repo_type_number on public.items (repo_id, type, number);
create index if not exists idx_items_updated_at_gh on public.items (updated_at_gh);

create table if not exists public.embeddings (
  id bigserial primary key,
  item_id bigint not null references public.items(id) on delete cascade,
  model text not null,
  dim integer not null check (dim = 768),
  embedding vector(768) not null,
  embedded_content_hash text not null,
  created_at timestamptz not null default timezone('utc', now()),
  unique (item_id, model)
);

create index if not exists idx_embeddings_model on public.embeddings (model);
create index if not exists idx_embeddings_item_id on public.embeddings (item_id);
create index if not exists idx_embeddings_embedding_cosine
  on public.embeddings
  using ivfflat (embedding vector_cosine_ops)
  with (lists = 100);

create table if not exists public.candidate_sets (
  id bigserial primary key,
  repo_id bigint not null references public.repos(id) on delete cascade,
  item_id bigint not null references public.items(id) on delete cascade,
  type text not null check (type in ('issue', 'pr')),
  embedding_model text not null,
  k integer not null check (k > 0),
  min_score real not null,
  include_states text[] not null,
  created_at timestamptz not null default timezone('utc', now()),
  status text not null check (status in ('fresh', 'stale')),
  item_content_version integer not null check (item_content_version > 0),
  check (array_length(include_states, 1) >= 1),
  check (include_states <@ array['open', 'closed']::text[])
);

create index if not exists idx_candidate_sets_repo_type_status
  on public.candidate_sets (repo_id, type, status, created_at desc);
create index if not exists idx_candidate_sets_item_id
  on public.candidate_sets (item_id, created_at desc);

create table if not exists public.candidate_set_members (
  candidate_set_id bigint not null references public.candidate_sets(id) on delete cascade,
  candidate_item_id bigint not null references public.items(id) on delete cascade,
  score real not null,
  rank integer not null check (rank > 0),
  created_at timestamptz not null default timezone('utc', now()),
  primary key (candidate_set_id, candidate_item_id),
  unique (candidate_set_id, rank)
);

create index if not exists idx_candidate_set_members_candidate_item
  on public.candidate_set_members (candidate_item_id);

create table if not exists public.duplicate_edges (
  id bigserial primary key,
  repo_id bigint not null references public.repos(id) on delete cascade,
  type text not null check (type in ('issue', 'pr')),
  from_item_id bigint not null references public.items(id) on delete cascade,
  to_item_id bigint not null references public.items(id) on delete cascade,
  confidence real not null check (confidence >= 0 and confidence <= 1),
  reasoning text,
  llm_provider text not null,
  llm_model text not null,
  created_by text not null,
  created_at timestamptz not null default timezone('utc', now()),
  status text not null check (status in ('accepted', 'rejected')),
  check (from_item_id <> to_item_id)
);

create index if not exists idx_duplicate_edges_repo_type_status
  on public.duplicate_edges (repo_id, type, status, created_at desc);
create index if not exists idx_duplicate_edges_to_item
  on public.duplicate_edges (to_item_id, status);
create unique index if not exists uq_duplicate_edges_one_accepted_outgoing
  on public.duplicate_edges (repo_id, type, from_item_id)
  where status = 'accepted';

create table if not exists public.close_runs (
  id bigserial primary key,
  repo_id bigint not null references public.repos(id) on delete cascade,
  type text not null check (type in ('issue', 'pr')),
  mode text not null check (mode in ('plan', 'apply')),
  min_confidence_close real not null check (min_confidence_close >= 0 and min_confidence_close <= 1),
  created_by text not null,
  created_at timestamptz not null default timezone('utc', now())
);

create index if not exists idx_close_runs_repo_type_created
  on public.close_runs (repo_id, type, created_at desc);

create table if not exists public.close_run_items (
  close_run_id bigint not null references public.close_runs(id) on delete cascade,
  item_id bigint not null references public.items(id) on delete cascade,
  canonical_item_id bigint not null references public.items(id) on delete cascade,
  action text not null check (action in ('close', 'skip')),
  skip_reason text,
  applied_at timestamptz,
  gh_result jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  primary key (close_run_id, item_id),
  check (
    (action = 'close' and skip_reason is null)
    or (action = 'skip' and skip_reason is not null)
  ),
  check (
    action = 'skip' or item_id <> canonical_item_id
  )
);

create index if not exists idx_close_run_items_action
  on public.close_run_items (close_run_id, action);

-- ------------------------------------------------------------
-- Consistency triggers
-- ------------------------------------------------------------

create or replace function public.validate_candidate_set_consistency()
returns trigger
language plpgsql
as $$
declare
  item_repo_id bigint;
  item_type text;
begin
  select i.repo_id, i.type
    into item_repo_id, item_type
  from public.items i
  where i.id = new.item_id;

  if not found then
    raise exception 'candidate_sets.item_id % does not exist', new.item_id;
  end if;

  if item_repo_id <> new.repo_id then
    raise exception 'candidate_sets.repo_id % does not match item repo_id %', new.repo_id, item_repo_id;
  end if;

  if item_type <> new.type then
    raise exception 'candidate_sets.type % does not match item type %', new.type, item_type;
  end if;

  return new;
end;
$$;

create trigger trg_validate_candidate_set_consistency
before insert or update on public.candidate_sets
for each row
execute function public.validate_candidate_set_consistency();

create or replace function public.validate_candidate_set_member_consistency()
returns trigger
language plpgsql
as $$
declare
  set_repo_id bigint;
  set_type text;
  cand_repo_id bigint;
  cand_type text;
begin
  select cs.repo_id, cs.type
    into set_repo_id, set_type
  from public.candidate_sets cs
  where cs.id = new.candidate_set_id;

  if not found then
    raise exception 'candidate_set_members.candidate_set_id % does not exist', new.candidate_set_id;
  end if;

  select i.repo_id, i.type
    into cand_repo_id, cand_type
  from public.items i
  where i.id = new.candidate_item_id;

  if not found then
    raise exception 'candidate_set_members.candidate_item_id % does not exist', new.candidate_item_id;
  end if;

  if set_repo_id <> cand_repo_id then
    raise exception 'candidate member repo mismatch: set repo % candidate repo %', set_repo_id, cand_repo_id;
  end if;

  if set_type <> cand_type then
    raise exception 'candidate member type mismatch: set type % candidate type %', set_type, cand_type;
  end if;

  return new;
end;
$$;

create trigger trg_validate_candidate_set_member_consistency
before insert or update on public.candidate_set_members
for each row
execute function public.validate_candidate_set_member_consistency();

create or replace function public.validate_duplicate_edge_consistency()
returns trigger
language plpgsql
as $$
declare
  from_repo_id bigint;
  from_type text;
  to_repo_id bigint;
  to_type text;
begin
  select i.repo_id, i.type
    into from_repo_id, from_type
  from public.items i
  where i.id = new.from_item_id;

  if not found then
    raise exception 'duplicate_edges.from_item_id % does not exist', new.from_item_id;
  end if;

  select i.repo_id, i.type
    into to_repo_id, to_type
  from public.items i
  where i.id = new.to_item_id;

  if not found then
    raise exception 'duplicate_edges.to_item_id % does not exist', new.to_item_id;
  end if;

  if from_repo_id <> new.repo_id or to_repo_id <> new.repo_id then
    raise exception 'duplicate edge repo mismatch: edge repo %, from repo %, to repo %', new.repo_id, from_repo_id, to_repo_id;
  end if;

  if from_type <> new.type or to_type <> new.type then
    raise exception 'duplicate edge type mismatch: edge type %, from type %, to type %', new.type, from_type, to_type;
  end if;

  return new;
end;
$$;

create trigger trg_validate_duplicate_edge_consistency
before insert or update on public.duplicate_edges
for each row
execute function public.validate_duplicate_edge_consistency();

create or replace function public.validate_close_run_item_consistency()
returns trigger
language plpgsql
as $$
declare
  run_repo_id bigint;
  run_type text;
  item_repo_id bigint;
  item_type text;
  canonical_repo_id bigint;
  canonical_type text;
begin
  select cr.repo_id, cr.type
    into run_repo_id, run_type
  from public.close_runs cr
  where cr.id = new.close_run_id;

  if not found then
    raise exception 'close_run_items.close_run_id % does not exist', new.close_run_id;
  end if;

  select i.repo_id, i.type
    into item_repo_id, item_type
  from public.items i
  where i.id = new.item_id;

  if not found then
    raise exception 'close_run_items.item_id % does not exist', new.item_id;
  end if;

  select i.repo_id, i.type
    into canonical_repo_id, canonical_type
  from public.items i
  where i.id = new.canonical_item_id;

  if not found then
    raise exception 'close_run_items.canonical_item_id % does not exist', new.canonical_item_id;
  end if;

  if run_repo_id <> item_repo_id or run_repo_id <> canonical_repo_id then
    raise exception 'close_run_items repo mismatch: run repo %, item repo %, canonical repo %', run_repo_id, item_repo_id, canonical_repo_id;
  end if;

  if run_type <> item_type or run_type <> canonical_type then
    raise exception 'close_run_items type mismatch: run type %, item type %, canonical type %', run_type, item_type, canonical_type;
  end if;

  return new;
end;
$$;

create trigger trg_validate_close_run_item_consistency
before insert or update on public.close_run_items
for each row
execute function public.validate_close_run_item_consistency();
