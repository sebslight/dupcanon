-- Source-representation provenance for judge decisions and close runs.

alter table public.judge_decisions
  add column if not exists representation text not null default 'raw';

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'judge_decisions_representation_check'
      and conrelid = 'public.judge_decisions'::regclass
  ) then
    alter table public.judge_decisions
      add constraint judge_decisions_representation_check
      check (representation in ('raw', 'intent'));
  end if;
end $$;

-- Backfill representation from linked candidate sets where available.
update public.judge_decisions jd
set representation = cs.representation
from public.candidate_sets cs
where jd.candidate_set_id = cs.id
  and jd.representation <> cs.representation;

-- Accepted-edge uniqueness is now representation-scoped.
drop index if exists public.uq_judge_decisions_one_accepted_outgoing;

create unique index if not exists uq_judge_decisions_one_accepted_outgoing_by_repr
  on public.judge_decisions (repo_id, type, from_item_id, representation)
  where final_status = 'accepted';

create index if not exists idx_judge_decisions_repo_type_repr_created
  on public.judge_decisions (repo_id, type, representation, created_at desc);

alter table public.judge_audit_runs
  add column if not exists representation text not null default 'raw';

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'judge_audit_runs_representation_check'
      and conrelid = 'public.judge_audit_runs'::regclass
  ) then
    alter table public.judge_audit_runs
      add constraint judge_audit_runs_representation_check
      check (representation in ('raw', 'intent'));
  end if;
end $$;

create index if not exists idx_judge_audit_runs_repo_type_repr_created
  on public.judge_audit_runs (repo_id, type, representation, created_at desc);

alter table public.close_runs
  add column if not exists representation text not null default 'raw';

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'close_runs_representation_check'
      and conrelid = 'public.close_runs'::regclass
  ) then
    alter table public.close_runs
      add constraint close_runs_representation_check
      check (representation in ('raw', 'intent'));
  end if;
end $$;

create index if not exists idx_close_runs_repo_type_repr_created
  on public.close_runs (repo_id, type, representation, created_at desc);
