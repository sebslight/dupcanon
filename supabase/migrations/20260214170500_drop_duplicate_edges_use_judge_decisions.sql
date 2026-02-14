-- Migrate from duplicate_edges to judge_decisions as the single source of truth.

-- Backfill existing duplicate_edges history into judge_decisions.
do $$
begin
  if to_regclass('public.duplicate_edges') is not null then
    insert into public.judge_decisions (
      repo_id,
      type,
      from_item_id,
      to_item_id,
      model_is_duplicate,
      final_status,
      confidence,
      reasoning,
      relation,
      root_cause_match,
      scope_relation,
      path_match,
      certainty,
      veto_reason,
      min_edge,
      llm_provider,
      llm_model,
      created_by,
      created_at
    )
    select
      e.repo_id,
      e.type,
      e.from_item_id,
      e.to_item_id,
      true,
      e.status,
      e.confidence,
      e.reasoning,
      null,
      null,
      null,
      null,
      null,
      null,
      0,
      e.llm_provider,
      e.llm_model,
      e.created_by,
      e.created_at
    from public.duplicate_edges e
    where not exists (
      select 1
      from public.judge_decisions j
      where
        j.repo_id = e.repo_id
        and j.type = e.type
        and j.from_item_id = e.from_item_id
        and j.to_item_id = e.to_item_id
        and j.model_is_duplicate = true
        and j.final_status = e.status
        and j.confidence = e.confidence
        and coalesce(j.reasoning, '') = coalesce(e.reasoning, '')
        and j.llm_provider = e.llm_provider
        and j.llm_model = e.llm_model
        and j.created_by = e.created_by
        and j.created_at = e.created_at
    );
  end if;
end $$;

-- Ensure accepted-edge uniqueness now that accepted edges live in judge_decisions.
create unique index if not exists uq_judge_decisions_one_accepted_outgoing
  on public.judge_decisions (repo_id, type, from_item_id)
  where final_status = 'accepted';

create index if not exists idx_judge_decisions_to_item_final_status
  on public.judge_decisions (to_item_id, final_status);

-- Consistency trigger for judge_decisions (repo/type coherence with items).
create or replace function public.validate_judge_decision_consistency()
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
    raise exception 'judge_decisions.from_item_id % does not exist', new.from_item_id;
  end if;

  if from_repo_id <> new.repo_id then
    raise exception 'judge decision repo mismatch: decision repo %, from repo %', new.repo_id, from_repo_id;
  end if;

  if from_type <> new.type then
    raise exception 'judge decision type mismatch: decision type %, from type %', new.type, from_type;
  end if;

  if new.to_item_id is not null then
    select i.repo_id, i.type
      into to_repo_id, to_type
    from public.items i
    where i.id = new.to_item_id;

    if not found then
      raise exception 'judge_decisions.to_item_id % does not exist', new.to_item_id;
    end if;

    if to_repo_id <> new.repo_id then
      raise exception 'judge decision repo mismatch: decision repo %, to repo %', new.repo_id, to_repo_id;
    end if;

    if to_type <> new.type then
      raise exception 'judge decision type mismatch: decision type %, to type %', new.type, to_type;
    end if;

    if new.from_item_id = new.to_item_id then
      raise exception 'judge decision cannot point to itself (%).', new.from_item_id;
    end if;
  end if;

  return new;
end;
$$;

drop trigger if exists trg_validate_judge_decision_consistency on public.judge_decisions;
create trigger trg_validate_judge_decision_consistency
before insert or update on public.judge_decisions
for each row
execute function public.validate_judge_decision_consistency();

-- duplicate_edges is no longer used.
drop table if exists public.duplicate_edges cascade;
drop function if exists public.validate_duplicate_edge_consistency();
