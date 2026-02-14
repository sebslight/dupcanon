-- Judge audit tables for sampled cheap-vs-strong model comparison.

create table if not exists public.judge_audit_runs (
  id bigserial primary key,
  repo_id bigint not null references public.repos(id) on delete cascade,
  type text not null check (type in ('issue', 'pr')),
  sample_policy text not null check (sample_policy in ('random_uniform')),
  sample_seed bigint not null,
  sample_size_requested integer not null check (sample_size_requested > 0),
  sample_size_actual integer not null default 0 check (sample_size_actual >= 0),
  candidate_set_status text not null default 'fresh' check (candidate_set_status = 'fresh'),
  source_state_filter text not null default 'open' check (source_state_filter = 'open'),
  min_edge real not null check (min_edge >= 0 and min_edge <= 1),
  cheap_llm_provider text not null,
  cheap_llm_model text not null,
  strong_llm_provider text not null,
  strong_llm_model text not null,
  status text not null default 'running' check (status in ('running', 'completed', 'failed')),
  compared_count integer not null default 0 check (compared_count >= 0),
  tp integer not null default 0 check (tp >= 0),
  fp integer not null default 0 check (fp >= 0),
  fn integer not null default 0 check (fn >= 0),
  tn integer not null default 0 check (tn >= 0),
  conflict integer not null default 0 check (conflict >= 0),
  incomplete integer not null default 0 check (incomplete >= 0),
  created_by text not null,
  created_at timestamptz not null default timezone('utc', now()),
  completed_at timestamptz
);

create index if not exists idx_judge_audit_runs_repo_type_created
  on public.judge_audit_runs (repo_id, type, created_at desc);

create table if not exists public.judge_audit_run_items (
  id bigserial primary key,
  audit_run_id bigint not null references public.judge_audit_runs(id) on delete cascade,
  source_item_id bigint not null references public.items(id) on delete cascade,
  source_number integer not null,
  source_state text not null check (source_state = 'open'),
  candidate_set_id bigint not null references public.candidate_sets(id) on delete restrict,
  cheap_model_is_duplicate boolean not null,
  cheap_final_status text not null check (cheap_final_status in ('accepted', 'rejected', 'skipped')),
  cheap_to_item_id bigint references public.items(id) on delete set null,
  cheap_confidence real not null check (cheap_confidence >= 0 and cheap_confidence <= 1),
  cheap_veto_reason text,
  cheap_reasoning text,
  strong_model_is_duplicate boolean not null,
  strong_final_status text not null check (strong_final_status in ('accepted', 'rejected', 'skipped')),
  strong_to_item_id bigint references public.items(id) on delete set null,
  strong_confidence real not null check (strong_confidence >= 0 and strong_confidence <= 1),
  strong_veto_reason text,
  strong_reasoning text,
  outcome_class text not null check (
    outcome_class in ('tp', 'fp', 'fn', 'tn', 'conflict', 'incomplete')
  ),
  created_at timestamptz not null default timezone('utc', now()),
  unique (audit_run_id, source_item_id),
  check (cheap_final_status <> 'accepted' or cheap_to_item_id is not null),
  check (strong_final_status <> 'accepted' or strong_to_item_id is not null)
);

create index if not exists idx_judge_audit_items_run_outcome
  on public.judge_audit_run_items (audit_run_id, outcome_class);

create index if not exists idx_judge_audit_items_run_source
  on public.judge_audit_run_items (audit_run_id, source_item_id);
