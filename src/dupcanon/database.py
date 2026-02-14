from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from psycopg import Connection, connect
from psycopg.rows import dict_row
from psycopg.types.json import Json

from dupcanon.models import (
    AcceptedDuplicateEdge,
    CandidateNeighbor,
    CandidateSourceItem,
    CanonicalNode,
    ClosePlanEntry,
    CloseRunRecord,
    EmbeddingItem,
    ItemPayload,
    ItemType,
    JudgeCandidate,
    JudgeWorkItem,
    PlanCloseItem,
    RepoMetadata,
    RepoRef,
    StateFilter,
    TypeFilter,
    UpsertResult,
    semantic_content_hash,
)


class DatabaseError(RuntimeError):
    pass


def _vector_literal(values: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in values) + "]"


class Database:
    def __init__(self, db_url: str) -> None:
        self.db_url = db_url

    def _connect(self) -> Connection[Any]:
        # Supabase IPv4 pooler runs transaction pooling and doesn't support server-side
        # prepared statements. Disable psycopg auto-prepare for compatibility.
        return connect(self.db_url, prepare_threshold=None)

    def upsert_repo(self, repo_metadata: RepoMetadata) -> int:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                insert into public.repos (github_repo_id, org, name)
                values (%s, %s, %s)
                on conflict (github_repo_id)
                do update set org = excluded.org, name = excluded.name
                returning id
                """,
                (repo_metadata.github_repo_id, repo_metadata.org, repo_metadata.name),
            )
            row = cur.fetchone()
            if row is None:
                msg = "failed to upsert repo"
                raise DatabaseError(msg)
            return int(row["id"])

    def get_repo_id(self, repo: RepoRef) -> int | None:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select id
                from public.repos
                where org = %s and name = %s
                """,
                (repo.org, repo.name),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return int(row["id"])

    def list_known_items(
        self, *, repo_id: int, type_filter: TypeFilter
    ) -> list[tuple[ItemType, int]]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            if type_filter == TypeFilter.ALL:
                cur.execute(
                    """
                    select type, number
                    from public.items
                    where repo_id = %s
                    order by id asc
                    """,
                    (repo_id,),
                )
            else:
                cur.execute(
                    """
                    select type, number
                    from public.items
                    where repo_id = %s and type = %s
                    order by id asc
                    """,
                    (repo_id, type_filter.value),
                )

            rows = cur.fetchall()

        result: list[tuple[ItemType, int]] = []
        for row in rows:
            result.append((ItemType(str(row["type"])), int(row["number"])))
        return result

    def list_items_for_embedding(
        self,
        *,
        repo_id: int,
        type_filter: TypeFilter,
        model: str,
    ) -> list[EmbeddingItem]:
        query = """
            select
                i.id as item_id,
                i.type,
                i.number,
                i.title,
                i.body,
                i.content_hash,
                e.embedded_content_hash
            from public.items i
            left join public.embeddings e
                on e.item_id = i.id and e.model = %s
            where i.repo_id = %s
        """

        params: list[Any] = [model, repo_id]
        if type_filter != TypeFilter.ALL:
            query += " and i.type = %s"
            params.append(type_filter.value)

        query += " order by i.id asc"

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

        result: list[EmbeddingItem] = []
        for row in rows:
            result.append(
                EmbeddingItem(
                    item_id=int(row["item_id"]),
                    type=ItemType(str(row["type"])),
                    number=int(row["number"]),
                    title=str(row["title"]),
                    body=row.get("body"),
                    content_hash=str(row["content_hash"]),
                    embedded_content_hash=(
                        str(row["embedded_content_hash"])
                        if row.get("embedded_content_hash") is not None
                        else None
                    ),
                )
            )
        return result

    def upsert_embedding(
        self,
        *,
        item_id: int,
        model: str,
        dim: int,
        embedding: list[float],
        embedded_content_hash: str,
        created_at: datetime,
    ) -> None:
        vector_literal = _vector_literal(embedding)

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.embeddings (
                    item_id,
                    model,
                    dim,
                    embedding,
                    embedded_content_hash,
                    created_at
                ) values (
                    %s, %s, %s, %s::vector, %s, %s
                )
                on conflict (item_id, model)
                do update set
                    dim = excluded.dim,
                    embedding = excluded.embedding,
                    embedded_content_hash = excluded.embedded_content_hash,
                    created_at = excluded.created_at
                """,
                (
                    item_id,
                    model,
                    dim,
                    vector_literal,
                    embedded_content_hash,
                    created_at,
                ),
            )

    def list_candidate_source_items(
        self,
        *,
        repo_id: int,
        type_filter: TypeFilter,
        model: str,
    ) -> list[CandidateSourceItem]:
        query = """
            select
                i.id as item_id,
                i.number,
                i.content_version,
                (e.item_id is not null) as has_embedding
            from public.items i
            left join public.embeddings e
                on e.item_id = i.id and e.model = %s
            where i.repo_id = %s
        """

        params: list[Any] = [model, repo_id]
        if type_filter != TypeFilter.ALL:
            query += " and i.type = %s"
            params.append(type_filter.value)

        query += " order by i.id asc"

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

        result: list[CandidateSourceItem] = []
        for row in rows:
            result.append(
                CandidateSourceItem(
                    item_id=int(row["item_id"]),
                    number=int(row["number"]),
                    content_version=int(row["content_version"]),
                    has_embedding=bool(row["has_embedding"]),
                )
            )
        return result

    def count_fresh_candidate_sets_for_item(self, *, item_id: int) -> int:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select count(*) as n
                from public.candidate_sets
                where item_id = %s and status = 'fresh'
                """,
                (item_id,),
            )
            row = cur.fetchone()

        if row is None:
            return 0
        return int(row["n"])

    def mark_candidate_sets_stale_for_item(self, *, item_id: int) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update public.candidate_sets
                set status = 'stale'
                where item_id = %s and status = 'fresh'
                """,
                (item_id,),
            )
            return cur.rowcount

    def create_candidate_set(
        self,
        *,
        repo_id: int,
        item_id: int,
        item_type: ItemType,
        embedding_model: str,
        k: int,
        min_score: float,
        include_states: list[str],
        item_content_version: int,
        created_at: datetime,
    ) -> int:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                insert into public.candidate_sets (
                    repo_id,
                    item_id,
                    type,
                    embedding_model,
                    k,
                    min_score,
                    include_states,
                    created_at,
                    status,
                    item_content_version
                ) values (
                    %s, %s, %s, %s, %s, %s, %s::text[], %s, 'fresh', %s
                )
                returning id
                """,
                (
                    repo_id,
                    item_id,
                    item_type.value,
                    embedding_model,
                    k,
                    min_score,
                    include_states,
                    created_at,
                    item_content_version,
                ),
            )
            row = cur.fetchone()

        if row is None:
            msg = "failed to create candidate_set"
            raise DatabaseError(msg)

        return int(row["id"])

    def create_candidate_set_members(
        self,
        *,
        candidate_set_id: int,
        neighbors: list[CandidateNeighbor],
        created_at: datetime,
    ) -> None:
        if not neighbors:
            return

        with self._connect() as conn, conn.cursor() as cur:
            for neighbor in neighbors:
                cur.execute(
                    """
                    insert into public.candidate_set_members (
                        candidate_set_id,
                        candidate_item_id,
                        score,
                        rank,
                        created_at
                    ) values (
                        %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        candidate_set_id,
                        neighbor.candidate_item_id,
                        neighbor.score,
                        neighbor.rank,
                        created_at,
                    ),
                )

    def find_candidate_neighbors(
        self,
        *,
        repo_id: int,
        item_id: int,
        item_type: ItemType,
        model: str,
        include_states: list[str],
        k: int,
        min_score: float,
    ) -> list[CandidateNeighbor]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select
                    e2.item_id as candidate_item_id,
                    (1 - (e1.embedding <=> e2.embedding))::double precision as score
                from public.embeddings e1
                join public.items i1 on i1.id = e1.item_id
                join public.embeddings e2 on e2.model = e1.model and e2.item_id <> e1.item_id
                join public.items i2 on i2.id = e2.item_id
                where
                    e1.item_id = %s
                    and e1.model = %s
                    and i1.repo_id = %s
                    and i1.type = %s
                    and i2.repo_id = i1.repo_id
                    and i2.type = i1.type
                    and i2.state = any(%s::text[])
                    and (1 - (e1.embedding <=> e2.embedding)) >= %s
                order by (e1.embedding <=> e2.embedding) asc
                limit %s
                """,
                (
                    item_id,
                    model,
                    repo_id,
                    item_type.value,
                    include_states,
                    min_score,
                    k,
                ),
            )
            rows = cur.fetchall()

        result: list[CandidateNeighbor] = []
        for idx, row in enumerate(rows, start=1):
            result.append(
                CandidateNeighbor(
                    candidate_item_id=int(row["candidate_item_id"]),
                    score=float(row["score"]),
                    rank=idx,
                )
            )
        return result

    def list_candidate_sets_for_judging(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
        allow_stale: bool,
    ) -> list[JudgeWorkItem]:
        status_predicate = (
            "cs.status in ('fresh', 'stale')" if allow_stale else "cs.status = 'fresh'"
        )
        freshness_order = (
            "case when cs.status = 'fresh' then 0 else 1 end, cs.created_at desc"
            if allow_stale
            else "cs.created_at desc"
        )

        query = f"""
            with selected_sets as (
                select distinct on (cs.item_id)
                    cs.id as candidate_set_id,
                    cs.status as candidate_set_status,
                    cs.item_id
                from public.candidate_sets cs
                where
                    cs.repo_id = %s
                    and cs.type = %s
                    and {status_predicate}
                order by cs.item_id, {freshness_order}
            )
            select
                s.candidate_set_id,
                s.candidate_set_status,
                src.id as source_item_id,
                src.number as source_number,
                src.type as source_type,
                src.state as source_state,
                src.title as source_title,
                src.body as source_body,
                m.rank,
                m.score,
                cand.id as candidate_item_id,
                cand.number as candidate_number,
                cand.state as candidate_state,
                cand.title as candidate_title,
                cand.body as candidate_body
            from selected_sets s
            join public.items src
                on src.id = s.item_id
            left join public.candidate_set_members m
                on m.candidate_set_id = s.candidate_set_id
            left join public.items cand
                on cand.id = m.candidate_item_id
            order by s.candidate_set_id asc, m.rank asc
        """

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, (repo_id, item_type.value))
            rows = cur.fetchall()

        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            candidate_set_id = int(row["candidate_set_id"])
            current = grouped.get(candidate_set_id)
            if current is None:
                status = str(row["candidate_set_status"])
                if status not in {"fresh", "stale"}:
                    msg = f"invalid candidate set status: {status}"
                    raise DatabaseError(msg)

                current = {
                    "candidate_set_id": candidate_set_id,
                    "candidate_set_status": status,
                    "source_item_id": int(row["source_item_id"]),
                    "source_number": int(row["source_number"]),
                    "source_type": ItemType(str(row["source_type"])),
                    "source_state": StateFilter(str(row["source_state"])),
                    "source_title": str(row["source_title"]),
                    "source_body": row.get("source_body"),
                    "candidates": [],
                }
                grouped[candidate_set_id] = current

            candidate_item_id = row.get("candidate_item_id")
            if candidate_item_id is None:
                continue

            current["candidates"].append(
                JudgeCandidate(
                    candidate_item_id=int(candidate_item_id),
                    number=int(row["candidate_number"]),
                    state=StateFilter(str(row["candidate_state"])),
                    title=str(row["candidate_title"]),
                    body=row.get("candidate_body"),
                    score=float(row["score"]),
                    rank=int(row["rank"]),
                )
            )

        result: list[JudgeWorkItem] = []
        for candidate_set_id in sorted(grouped):
            result.append(JudgeWorkItem(**grouped[candidate_set_id]))
        return result

    def has_accepted_duplicate_edge(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
        from_item_id: int,
    ) -> bool:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select 1
                from public.duplicate_edges
                where
                    repo_id = %s
                    and type = %s
                    and from_item_id = %s
                    and status = 'accepted'
                limit 1
                """,
                (repo_id, item_type.value, from_item_id),
            )
            row = cur.fetchone()

        return row is not None

    def insert_duplicate_edge(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
        from_item_id: int,
        to_item_id: int,
        confidence: float,
        reasoning: str,
        llm_provider: str,
        llm_model: str,
        created_by: str,
        status: str,
        created_at: datetime,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.duplicate_edges (
                    repo_id,
                    type,
                    from_item_id,
                    to_item_id,
                    confidence,
                    reasoning,
                    llm_provider,
                    llm_model,
                    created_by,
                    created_at,
                    status
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    repo_id,
                    item_type.value,
                    from_item_id,
                    to_item_id,
                    confidence,
                    reasoning,
                    llm_provider,
                    llm_model,
                    created_by,
                    created_at,
                    status,
                ),
            )

    def replace_accepted_duplicate_edge(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
        from_item_id: int,
        to_item_id: int,
        confidence: float,
        reasoning: str,
        llm_provider: str,
        llm_model: str,
        created_by: str,
        created_at: datetime,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update public.duplicate_edges
                set status = 'rejected'
                where
                    repo_id = %s
                    and type = %s
                    and from_item_id = %s
                    and status = 'accepted'
                """,
                (repo_id, item_type.value, from_item_id),
            )
            cur.execute(
                """
                insert into public.duplicate_edges (
                    repo_id,
                    type,
                    from_item_id,
                    to_item_id,
                    confidence,
                    reasoning,
                    llm_provider,
                    llm_model,
                    created_by,
                    created_at,
                    status
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'accepted'
                )
                """,
                (
                    repo_id,
                    item_type.value,
                    from_item_id,
                    to_item_id,
                    confidence,
                    reasoning,
                    llm_provider,
                    llm_model,
                    created_by,
                    created_at,
                ),
            )

    def list_accepted_duplicate_edges(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
    ) -> list[tuple[int, int]]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select from_item_id, to_item_id
                from public.duplicate_edges
                where
                    repo_id = %s
                    and type = %s
                    and status = 'accepted'
                order by id asc
                """,
                (repo_id, item_type.value),
            )
            rows = cur.fetchall()

        result: list[tuple[int, int]] = []
        for row in rows:
            result.append((int(row["from_item_id"]), int(row["to_item_id"])))
        return result

    def list_accepted_duplicate_edges_with_confidence(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
    ) -> list[AcceptedDuplicateEdge]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select from_item_id, to_item_id, confidence
                from public.duplicate_edges
                where
                    repo_id = %s
                    and type = %s
                    and status = 'accepted'
                order by id asc
                """,
                (repo_id, item_type.value),
            )
            rows = cur.fetchall()

        result: list[AcceptedDuplicateEdge] = []
        for row in rows:
            result.append(
                AcceptedDuplicateEdge(
                    from_item_id=int(row["from_item_id"]),
                    to_item_id=int(row["to_item_id"]),
                    confidence=float(row["confidence"]),
                )
            )
        return result

    def list_items_for_close_planning(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
    ) -> list[PlanCloseItem]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                with edge_items as (
                    select from_item_id as item_id
                    from public.duplicate_edges
                    where repo_id = %s and type = %s and status = 'accepted'
                    union
                    select to_item_id as item_id
                    from public.duplicate_edges
                    where repo_id = %s and type = %s and status = 'accepted'
                )
                select
                    i.id as item_id,
                    i.number,
                    i.state,
                    i.author_login,
                    i.assignees,
                    i.comment_count,
                    i.review_comment_count,
                    i.created_at_gh
                from edge_items e
                join public.items i on i.id = e.item_id
                order by i.id asc
                """,
                (repo_id, item_type.value, repo_id, item_type.value),
            )
            rows = cur.fetchall()

        result: list[PlanCloseItem] = []
        for row in rows:
            raw_assignees = row.get("assignees")
            assignees_unknown = False
            assignees: list[str] = []
            if raw_assignees is None:
                assignees = []
            elif isinstance(raw_assignees, list):
                for value in raw_assignees:
                    if isinstance(value, str):
                        assignees.append(value)
                    else:
                        assignees_unknown = True
            else:
                assignees_unknown = True

            result.append(
                PlanCloseItem(
                    item_id=int(row["item_id"]),
                    number=int(row["number"]),
                    state=StateFilter(str(row["state"])),
                    author_login=row.get("author_login"),
                    assignees=assignees,
                    assignees_unknown=assignees_unknown,
                    comment_count=int(row["comment_count"]),
                    review_comment_count=int(row["review_comment_count"]),
                    created_at_gh=row.get("created_at_gh"),
                )
            )
        return result

    def create_close_run(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
        mode: str,
        min_confidence_close: float,
        created_by: str,
        created_at: datetime,
    ) -> int:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                insert into public.close_runs (
                    repo_id,
                    type,
                    mode,
                    min_confidence_close,
                    created_by,
                    created_at
                ) values (
                    %s, %s, %s, %s, %s, %s
                )
                returning id
                """,
                (
                    repo_id,
                    item_type.value,
                    mode,
                    min_confidence_close,
                    created_by,
                    created_at,
                ),
            )
            row = cur.fetchone()

        if row is None:
            msg = "failed to create close_run"
            raise DatabaseError(msg)

        return int(row["id"])

    def create_close_run_item(
        self,
        *,
        close_run_id: int,
        item_id: int,
        canonical_item_id: int,
        action: str,
        skip_reason: str | None,
        created_at: datetime,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.close_run_items (
                    close_run_id,
                    item_id,
                    canonical_item_id,
                    action,
                    skip_reason,
                    created_at
                ) values (
                    %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    close_run_id,
                    item_id,
                    canonical_item_id,
                    action,
                    skip_reason,
                    created_at,
                ),
            )

    def get_close_run_record(self, *, close_run_id: int) -> CloseRunRecord | None:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select
                    cr.id as close_run_id,
                    cr.repo_id,
                    r.org,
                    r.name,
                    cr.type,
                    cr.mode,
                    cr.min_confidence_close
                from public.close_runs cr
                join public.repos r
                    on r.id = cr.repo_id
                where cr.id = %s
                """,
                (close_run_id,),
            )
            row = cur.fetchone()

        if row is None:
            return None

        raw_mode = str(row["mode"])
        if raw_mode not in {"plan", "apply"}:
            msg = f"invalid close_runs.mode value: {raw_mode}"
            raise DatabaseError(msg)
        mode = cast(Literal["plan", "apply"], raw_mode)

        return CloseRunRecord(
            close_run_id=int(row["close_run_id"]),
            repo_id=int(row["repo_id"]),
            repo_full_name=f"{row['org']}/{row['name']}",
            item_type=ItemType(str(row["type"])),
            mode=mode,
            min_confidence_close=float(row["min_confidence_close"]),
        )

    def list_close_plan_entries(self, *, close_run_id: int) -> list[ClosePlanEntry]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select
                    cri.item_id,
                    i.number as item_number,
                    cri.canonical_item_id,
                    canonical.number as canonical_number,
                    cri.action,
                    cri.skip_reason
                from public.close_run_items cri
                join public.items i
                    on i.id = cri.item_id
                join public.items canonical
                    on canonical.id = cri.canonical_item_id
                where cri.close_run_id = %s
                order by i.number asc
                """,
                (close_run_id,),
            )
            rows = cur.fetchall()

        result: list[ClosePlanEntry] = []
        for row in rows:
            raw_action = str(row["action"])
            if raw_action not in {"close", "skip"}:
                msg = f"invalid close_run_items.action value: {raw_action}"
                raise DatabaseError(msg)
            action = cast(Literal["close", "skip"], raw_action)

            result.append(
                ClosePlanEntry(
                    item_id=int(row["item_id"]),
                    item_number=int(row["item_number"]),
                    canonical_item_id=int(row["canonical_item_id"]),
                    canonical_number=int(row["canonical_number"]),
                    action=action,
                    skip_reason=row.get("skip_reason"),
                )
            )

        return result

    def update_close_run_item_apply_result(
        self,
        *,
        close_run_id: int,
        item_id: int,
        applied_at: datetime,
        gh_result: dict[str, Any],
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update public.close_run_items
                set applied_at = %s,
                    gh_result = %s
                where close_run_id = %s
                  and item_id = %s
                """,
                (applied_at, Json(gh_result), close_run_id, item_id),
            )
            if cur.rowcount != 1:
                msg = (
                    "close_run_items row not found for "
                    f"close_run_id={close_run_id} item_id={item_id}"
                )
                raise DatabaseError(msg)

    def list_nodes_for_canonicalization(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
    ) -> list[CanonicalNode]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                with edge_items as (
                    select from_item_id as item_id
                    from public.duplicate_edges
                    where repo_id = %s and type = %s and status = 'accepted'
                    union
                    select to_item_id as item_id
                    from public.duplicate_edges
                    where repo_id = %s and type = %s and status = 'accepted'
                )
                select
                    i.id as item_id,
                    i.number,
                    i.state,
                    i.author_login,
                    i.comment_count,
                    i.review_comment_count,
                    i.created_at_gh
                from edge_items e
                join public.items i on i.id = e.item_id
                order by i.id asc
                """,
                (repo_id, item_type.value, repo_id, item_type.value),
            )
            rows = cur.fetchall()

        result: list[CanonicalNode] = []
        for row in rows:
            result.append(
                CanonicalNode(
                    item_id=int(row["item_id"]),
                    number=int(row["number"]),
                    state=StateFilter(str(row["state"])),
                    author_login=row.get("author_login"),
                    comment_count=int(row["comment_count"]),
                    review_comment_count=int(row["review_comment_count"]),
                    created_at_gh=row.get("created_at_gh"),
                )
            )
        return result

    def inspect_item_change(self, *, repo_id: int, item: ItemPayload) -> UpsertResult:
        content_hash = semantic_content_hash(item_type=item.type, title=item.title, body=item.body)

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select content_hash
                from public.items
                where repo_id = %s and type = %s and number = %s
                """,
                (repo_id, item.type.value, item.number),
            )
            existing = cur.fetchone()

        if existing is None:
            return UpsertResult(inserted=True, content_changed=True)

        previous_hash = str(existing["content_hash"])
        content_changed = previous_hash != content_hash
        return UpsertResult(inserted=False, content_changed=content_changed)

    def upsert_item(self, *, repo_id: int, item: ItemPayload, synced_at: datetime) -> UpsertResult:
        content_hash = semantic_content_hash(item_type=item.type, title=item.title, body=item.body)

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select id, content_hash, content_version
                from public.items
                where repo_id = %s and type = %s and number = %s
                """,
                (repo_id, item.type.value, item.number),
            )
            existing = cur.fetchone()

            if existing is None:
                cur.execute(
                    """
                    insert into public.items (
                        repo_id,
                        type,
                        number,
                        url,
                        title,
                        body,
                        state,
                        author_login,
                        assignees,
                        labels,
                        comment_count,
                        review_comment_count,
                        created_at_gh,
                        updated_at_gh,
                        closed_at_gh,
                        content_hash,
                        content_version,
                        last_synced_at
                    ) values (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    """,
                    (
                        repo_id,
                        item.type.value,
                        item.number,
                        item.url,
                        item.title,
                        item.body,
                        item.state.value,
                        item.author_login,
                        Json(item.assignees),
                        Json(item.labels),
                        item.comment_count,
                        item.review_comment_count,
                        item.created_at_gh,
                        item.updated_at_gh,
                        item.closed_at_gh,
                        content_hash,
                        1,
                        synced_at,
                    ),
                )
                return UpsertResult(inserted=True, content_changed=True)

            previous_hash = str(existing["content_hash"])
            previous_version = int(existing["content_version"])
            content_changed = previous_hash != content_hash
            next_version = previous_version + 1 if content_changed else previous_version

            current_item_id = int(existing["id"])

            cur.execute(
                """
                update public.items
                set
                    url = %s,
                    title = %s,
                    body = %s,
                    state = %s,
                    author_login = %s,
                    assignees = %s,
                    labels = %s,
                    comment_count = %s,
                    review_comment_count = %s,
                    created_at_gh = %s,
                    updated_at_gh = %s,
                    closed_at_gh = %s,
                    content_hash = %s,
                    content_version = %s,
                    last_synced_at = %s
                where id = %s
                """,
                (
                    item.url,
                    item.title,
                    item.body,
                    item.state.value,
                    item.author_login,
                    Json(item.assignees),
                    Json(item.labels),
                    item.comment_count,
                    item.review_comment_count,
                    item.created_at_gh,
                    item.updated_at_gh,
                    item.closed_at_gh,
                    content_hash,
                    next_version,
                    synced_at,
                    current_item_id,
                ),
            )

            if content_changed:
                cur.execute(
                    """
                    update public.candidate_sets
                    set status = 'stale'
                    where item_id = %s and status = 'fresh'
                    """,
                    (current_item_id,),
                )

            return UpsertResult(inserted=False, content_changed=content_changed)

    def refresh_item_metadata(
        self, *, repo_id: int, item: ItemPayload, synced_at: datetime
    ) -> bool:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                update public.items
                set
                    url = %s,
                    state = %s,
                    author_login = %s,
                    assignees = %s,
                    labels = %s,
                    comment_count = %s,
                    review_comment_count = %s,
                    updated_at_gh = %s,
                    closed_at_gh = %s,
                    last_synced_at = %s
                where repo_id = %s and type = %s and number = %s
                """,
                (
                    item.url,
                    item.state.value,
                    item.author_login,
                    Json(item.assignees),
                    Json(item.labels),
                    item.comment_count,
                    item.review_comment_count,
                    item.updated_at_gh,
                    item.closed_at_gh,
                    synced_at,
                    repo_id,
                    item.type.value,
                    item.number,
                ),
            )
            return cur.rowcount > 0


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
