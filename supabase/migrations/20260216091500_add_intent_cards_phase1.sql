-- Phase 1 storage foundation for intent-card representation.

-- Candidate-set provenance for raw vs intent retrieval paths.
alter table public.candidate_sets
  add column if not exists representation text not null default 'raw';

alter table public.candidate_sets
  add column if not exists representation_version text;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'candidate_sets_representation_check'
      and conrelid = 'public.candidate_sets'::regclass
  ) then
    alter table public.candidate_sets
      add constraint candidate_sets_representation_check
      check (representation in ('raw', 'intent'));
  end if;
end $$;

create index if not exists idx_candidate_sets_repo_type_rep_status_created
  on public.candidate_sets (repo_id, type, representation, status, created_at desc);

-- Intent-card sidecar table.
create table if not exists public.intent_cards (
  id bigserial primary key,
  item_id bigint not null references public.items(id) on delete cascade,
  source_content_hash text not null,
  schema_version text not null,
  extractor_provider text not null,
  extractor_model text not null,
  prompt_version text not null,
  card_json jsonb not null,
  card_text_for_embedding text not null,
  embedding_render_version text not null,
  status text not null check (status in ('fresh', 'stale', 'failed')),
  insufficient_context boolean not null default false,
  error_class text,
  error_message text,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  unique (item_id, source_content_hash, schema_version, prompt_version)
);

drop trigger if exists set_intent_cards_updated_at on public.intent_cards;
create trigger set_intent_cards_updated_at
before update on public.intent_cards
for each row
execute function public.set_updated_at_timestamp();

create index if not exists idx_intent_cards_item_schema_prompt_created
  on public.intent_cards (item_id, schema_version, prompt_version, created_at desc);
create index if not exists idx_intent_cards_item_latest_fresh
  on public.intent_cards (item_id, created_at desc)
  where status = 'fresh';
create index if not exists idx_intent_cards_status_created
  on public.intent_cards (status, created_at desc);

-- Intent-card embedding table.
create table if not exists public.intent_embeddings (
  id bigserial primary key,
  intent_card_id bigint not null references public.intent_cards(id) on delete cascade,
  model text not null,
  dim integer not null check (dim = 3072),
  embedding vector(3072) not null,
  embedded_card_hash text not null,
  created_at timestamptz not null default timezone('utc', now()),
  unique (intent_card_id, model)
);

create index if not exists idx_intent_embeddings_model
  on public.intent_embeddings (model);
create index if not exists idx_intent_embeddings_intent_card_id
  on public.intent_embeddings (intent_card_id);
