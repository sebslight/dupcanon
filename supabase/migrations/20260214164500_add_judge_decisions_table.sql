-- Judge decision telemetry table for confidence matrix + auditability

create table if not exists public.judge_decisions (
  id bigserial primary key,
  repo_id bigint not null references public.repos(id) on delete cascade,
  type text not null check (type in ('issue', 'pr')),
  from_item_id bigint not null references public.items(id) on delete cascade,
  candidate_set_id bigint references public.candidate_sets(id) on delete set null,
  to_item_id bigint references public.items(id) on delete set null,
  model_is_duplicate boolean not null,
  final_status text not null check (final_status in ('accepted', 'rejected', 'skipped')),
  confidence real not null check (confidence >= 0 and confidence <= 1),
  reasoning text,
  relation text,
  root_cause_match text,
  scope_relation text,
  path_match text,
  certainty text,
  veto_reason text,
  min_edge real not null check (min_edge >= 0 and min_edge <= 1),
  llm_provider text not null,
  llm_model text not null,
  created_by text not null,
  created_at timestamptz not null default timezone('utc', now()),
  check (
    relation is null
    or relation in ('same_instance', 'related_followup', 'partial_overlap', 'different')
  ),
  check (
    root_cause_match is null
    or root_cause_match in ('same', 'adjacent', 'different')
  ),
  check (
    scope_relation is null
    or scope_relation in (
      'same_scope', 'source_subset', 'source_superset', 'partial_overlap', 'different_scope'
    )
  ),
  check (
    path_match is null
    or path_match in ('same', 'different', 'unknown')
  ),
  check (
    certainty is null
    or certainty in ('sure', 'unsure')
  ),
  check (
    not model_is_duplicate
    or to_item_id is not null
  )
);

create index if not exists idx_judge_decisions_repo_type_created
  on public.judge_decisions (repo_id, type, created_at desc);

create index if not exists idx_judge_decisions_from_item
  on public.judge_decisions (from_item_id, created_at desc);

create index if not exists idx_judge_decisions_final_status
  on public.judge_decisions (final_status, created_at desc);
