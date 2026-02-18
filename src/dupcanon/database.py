from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, cast

from psycopg import Connection, connect
from psycopg.rows import dict_row
from psycopg.types.json import Json

from dupcanon.models import (
    AcceptedDuplicateEdge,
    CandidateItemContext,
    CandidateNeighbor,
    CandidateSourceItem,
    CanonicalNode,
    ClosePlanEntry,
    CloseRunRecord,
    EmbeddingItem,
    IntentCard,
    IntentCardRecord,
    IntentCardSourceItem,
    IntentCardStatus,
    IntentEmbeddingItem,
    ItemPayload,
    ItemType,
    JudgeAuditDisagreement,
    JudgeAuditRunReport,
    JudgeAuditSimulationRow,
    JudgeCandidate,
    JudgeWorkItem,
    PlanCloseItem,
    RepoMetadata,
    RepoRef,
    RepresentationSource,
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

    def get_latest_created_at_gh(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
    ) -> datetime | None:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select max(created_at_gh) as latest_created_at_gh
                from public.items
                where repo_id = %s and type = %s
                """,
                (repo_id, item_type.value),
            )
            row = cur.fetchone()

        if row is None:
            return None

        value = row.get("latest_created_at_gh")
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value

        msg = "invalid latest_created_at_gh value"
        raise DatabaseError(msg)

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

    def get_embedding_item_by_number(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
        number: int,
        model: str,
    ) -> EmbeddingItem | None:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
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
                where
                    i.repo_id = %s
                    and i.type = %s
                    and i.number = %s
                limit 1
                """,
                (model, repo_id, item_type.value, number),
            )
            row = cur.fetchone()

        if row is None:
            return None

        return EmbeddingItem(
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

    def list_item_context_by_ids(self, *, item_ids: list[int]) -> list[CandidateItemContext]:
        if not item_ids:
            return []

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select
                    i.id as item_id,
                    i.number,
                    i.state,
                    i.title,
                    i.body
                from public.items i
                where i.id = any(%s::bigint[])
                order by i.id asc
                """,
                (item_ids,),
            )
            rows = cur.fetchall()

        result: list[CandidateItemContext] = []
        for row in rows:
            result.append(
                CandidateItemContext(
                    item_id=int(row["item_id"]),
                    number=int(row["number"]),
                    state=StateFilter(str(row["state"])),
                    title=str(row["title"]),
                    body=row.get("body"),
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

    def upsert_intent_card(
        self,
        *,
        item_id: int,
        source_content_hash: str,
        schema_version: str,
        extractor_provider: str,
        extractor_model: str,
        prompt_version: str,
        card_json: IntentCard,
        card_text_for_embedding: str,
        embedding_render_version: str,
        status: IntentCardStatus,
        insufficient_context: bool,
        error_class: str | None,
        error_message: str | None,
        created_at: datetime,
    ) -> int:
        payload = Json(card_json.model_dump(mode="json"))

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                insert into public.intent_cards (
                    item_id,
                    source_content_hash,
                    schema_version,
                    extractor_provider,
                    extractor_model,
                    prompt_version,
                    card_json,
                    card_text_for_embedding,
                    embedding_render_version,
                    status,
                    insufficient_context,
                    error_class,
                    error_message,
                    created_at,
                    updated_at
                ) values (
                    %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s
                )
                on conflict (item_id, source_content_hash, schema_version, prompt_version)
                do update set
                    extractor_provider = excluded.extractor_provider,
                    extractor_model = excluded.extractor_model,
                    card_json = excluded.card_json,
                    card_text_for_embedding = excluded.card_text_for_embedding,
                    embedding_render_version = excluded.embedding_render_version,
                    status = excluded.status,
                    insufficient_context = excluded.insufficient_context,
                    error_class = excluded.error_class,
                    error_message = excluded.error_message,
                    updated_at = excluded.updated_at
                returning id
                """,
                (
                    item_id,
                    source_content_hash,
                    schema_version,
                    extractor_provider,
                    extractor_model,
                    prompt_version,
                    payload,
                    card_text_for_embedding,
                    embedding_render_version,
                    status.value,
                    insufficient_context,
                    error_class,
                    error_message,
                    created_at,
                    created_at,
                ),
            )
            row = cur.fetchone()

        if row is None:
            msg = "failed to upsert intent card"
            raise DatabaseError(msg)

        return int(row["id"])

    def get_latest_intent_card(
        self,
        *,
        item_id: int,
        schema_version: str,
        prompt_version: str,
        status: IntentCardStatus | None = None,
    ) -> IntentCardRecord | None:
        query = """
            select
                ic.id,
                ic.item_id,
                ic.source_content_hash,
                ic.schema_version,
                ic.extractor_provider,
                ic.extractor_model,
                ic.prompt_version,
                ic.card_json,
                ic.card_text_for_embedding,
                ic.embedding_render_version,
                ic.status,
                ic.insufficient_context,
                ic.error_class,
                ic.error_message,
                ic.created_at,
                ic.updated_at
            from public.intent_cards ic
            where
                ic.item_id = %s
                and ic.schema_version = %s
                and ic.prompt_version = %s
        """
        params: list[Any] = [item_id, schema_version, prompt_version]
        if status is not None:
            query += " and ic.status = %s"
            params.append(status.value)

        query += " order by ic.created_at desc, ic.id desc limit 1"

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, tuple(params))
            row = cur.fetchone()

        if row is None:
            return None

        card_payload = row.get("card_json")
        if not isinstance(card_payload, dict):
            msg = "invalid intent_cards.card_json payload"
            raise DatabaseError(msg)

        return IntentCardRecord(
            intent_card_id=int(row["id"]),
            item_id=int(row["item_id"]),
            source_content_hash=str(row["source_content_hash"]),
            schema_version=str(row["schema_version"]),
            extractor_provider=str(row["extractor_provider"]),
            extractor_model=str(row["extractor_model"]),
            prompt_version=str(row["prompt_version"]),
            card_json=IntentCard.model_validate(card_payload),
            card_text_for_embedding=str(row["card_text_for_embedding"]),
            embedding_render_version=str(row["embedding_render_version"]),
            status=IntentCardStatus(str(row["status"])),
            insufficient_context=bool(row["insufficient_context"]),
            error_class=(
                str(row["error_class"])
                if row.get("error_class") is not None
                else None
            ),
            error_message=(
                str(row["error_message"])
                if row.get("error_message") is not None
                else None
            ),
            created_at=cast(datetime, row["created_at"]),
            updated_at=cast(datetime, row["updated_at"]),
        )

    def list_items_for_intent_card_extraction(
        self,
        *,
        repo_id: int,
        type_filter: TypeFilter,
        state_filter: StateFilter = StateFilter.OPEN,
        schema_version: str,
        prompt_version: str,
        only_changed: bool = True,
    ) -> list[IntentCardSourceItem]:
        query = """
            select
                i.id as item_id,
                i.type,
                i.number,
                i.title,
                i.body,
                i.content_hash,
                latest.source_content_hash as latest_source_content_hash,
                latest.status as latest_status
            from public.items i
            left join lateral (
                select
                    ic.source_content_hash,
                    ic.status
                from public.intent_cards ic
                where
                    ic.item_id = i.id
                    and ic.schema_version = %s
                    and ic.prompt_version = %s
                order by ic.created_at desc, ic.id desc
                limit 1
            ) latest on true
            where
                i.repo_id = %s
        """
        params: list[Any] = [schema_version, prompt_version, repo_id]

        if type_filter != TypeFilter.ALL:
            query += " and i.type = %s"
            params.append(type_filter.value)

        if state_filter != StateFilter.ALL:
            query += " and i.state = %s"
            params.append(state_filter.value)

        if only_changed:
            query += """
                and (
                    latest.source_content_hash is null
                    or latest.source_content_hash <> i.content_hash
                    or latest.status <> 'fresh'
                )
            """

        query += """
            order by i.id asc
        """

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

        result: list[IntentCardSourceItem] = []
        for row in rows:
            latest_status_raw = row.get("latest_status")
            result.append(
                IntentCardSourceItem(
                    item_id=int(row["item_id"]),
                    type=ItemType(str(row["type"])),
                    number=int(row["number"]),
                    title=str(row["title"]),
                    body=row.get("body"),
                    content_hash=str(row["content_hash"]),
                    latest_source_content_hash=(
                        str(row["latest_source_content_hash"])
                        if row.get("latest_source_content_hash") is not None
                        else None
                    ),
                    latest_status=(
                        IntentCardStatus(str(latest_status_raw))
                        if latest_status_raw is not None
                        else None
                    ),
                )
            )

        return result

    def list_intent_cards_for_embedding(
        self,
        *,
        repo_id: int,
        type_filter: TypeFilter,
        schema_version: str,
        prompt_version: str,
        model: str,
    ) -> list[IntentEmbeddingItem]:
        query = """
            with latest_fresh as (
                select distinct on (ic.item_id)
                    ic.id,
                    ic.item_id,
                    ic.card_text_for_embedding
                from public.intent_cards ic
                where
                    ic.schema_version = %s
                    and ic.prompt_version = %s
                    and ic.status = 'fresh'
                order by ic.item_id, ic.created_at desc, ic.id desc
            )
            select
                lf.id as intent_card_id,
                i.id as item_id,
                i.type,
                i.number,
                lf.card_text_for_embedding,
                ie.embedded_card_hash
            from latest_fresh lf
            join public.items i on i.id = lf.item_id
            left join public.intent_embeddings ie
                on ie.intent_card_id = lf.id and ie.model = %s
            where i.repo_id = %s
        """
        params: list[Any] = [schema_version, prompt_version, model, repo_id]

        if type_filter != TypeFilter.ALL:
            query += " and i.type = %s"
            params.append(type_filter.value)

        query += " order by i.id asc"

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()

        result: list[IntentEmbeddingItem] = []
        for row in rows:
            result.append(
                IntentEmbeddingItem(
                    intent_card_id=int(row["intent_card_id"]),
                    item_id=int(row["item_id"]),
                    type=ItemType(str(row["type"])),
                    number=int(row["number"]),
                    card_text_for_embedding=str(row["card_text_for_embedding"]),
                    embedded_card_hash=(
                        str(row["embedded_card_hash"])
                        if row.get("embedded_card_hash") is not None
                        else None
                    ),
                )
            )

        return result

    def upsert_intent_embedding(
        self,
        *,
        intent_card_id: int,
        model: str,
        dim: int,
        embedding: list[float],
        embedded_card_hash: str,
        created_at: datetime,
    ) -> None:
        vector_literal = _vector_literal(embedding)

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.intent_embeddings (
                    intent_card_id,
                    model,
                    dim,
                    embedding,
                    embedded_card_hash,
                    created_at
                ) values (
                    %s, %s, %s, %s::vector, %s, %s
                )
                on conflict (intent_card_id, model)
                do update set
                    dim = excluded.dim,
                    embedding = excluded.embedding,
                    embedded_card_hash = excluded.embedded_card_hash,
                    created_at = excluded.created_at
                """,
                (
                    intent_card_id,
                    model,
                    dim,
                    vector_literal,
                    embedded_card_hash,
                    created_at,
                ),
            )

    def list_candidate_source_items(
        self,
        *,
        repo_id: int,
        type_filter: TypeFilter,
        model: str,
        source: RepresentationSource = RepresentationSource.RAW,
        state_filter: StateFilter = StateFilter.ALL,
        intent_schema_version: str | None = None,
        intent_prompt_version: str | None = None,
    ) -> list[CandidateSourceItem]:
        if source == RepresentationSource.RAW:
            query = """
                select
                    i.id as item_id,
                    i.number,
                    i.content_version,
                    (e.item_id is not null) as has_embedding,
                    null::boolean as has_intent_card
                from public.items i
                left join public.embeddings e
                    on e.item_id = i.id and e.model = %s
                where i.repo_id = %s
            """
            params: list[Any] = [model, repo_id]
        elif source == RepresentationSource.INTENT:
            if not intent_schema_version or not intent_prompt_version:
                msg = "intent schema/prompt versions are required for source=intent"
                raise ValueError(msg)

            query = """
                with latest_fresh as (
                    select distinct on (ic.item_id)
                        ic.id as intent_card_id,
                        ic.item_id
                    from public.intent_cards ic
                    where
                        ic.schema_version = %s
                        and ic.prompt_version = %s
                        and ic.status = 'fresh'
                    order by ic.item_id, ic.created_at desc, ic.id desc
                )
                select
                    i.id as item_id,
                    i.number,
                    i.content_version,
                    (ie.intent_card_id is not null) as has_embedding,
                    (lf.intent_card_id is not null) as has_intent_card
                from public.items i
                left join latest_fresh lf
                    on lf.item_id = i.id
                left join public.intent_embeddings ie
                    on ie.intent_card_id = lf.intent_card_id and ie.model = %s
                where i.repo_id = %s
            """
            params = [intent_schema_version, intent_prompt_version, model, repo_id]
        else:
            msg = f"unsupported representation source: {source.value}"
            raise ValueError(msg)

        if type_filter != TypeFilter.ALL:
            query += " and i.type = %s"
            params.append(type_filter.value)

        if state_filter != StateFilter.ALL:
            query += " and i.state = %s"
            params.append(state_filter.value)

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
                    has_intent_card=(
                        bool(row["has_intent_card"])
                        if row.get("has_intent_card") is not None
                        else None
                    ),
                )
            )
        return result

    def count_fresh_candidate_sets_for_item(
        self,
        *,
        item_id: int,
        representation: RepresentationSource | None = None,
    ) -> int:
        query = """
            select count(*) as n
            from public.candidate_sets
            where item_id = %s and status = 'fresh'
        """
        params: list[Any] = [item_id]

        if representation is not None:
            query += " and representation = %s"
            params.append(representation.value)

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, tuple(params))
            row = cur.fetchone()

        if row is None:
            return 0
        return int(row["n"])

    def mark_candidate_sets_stale_for_item(
        self,
        *,
        item_id: int,
        representation: RepresentationSource | None = None,
    ) -> int:
        query = """
            update public.candidate_sets
            set status = 'stale'
            where item_id = %s and status = 'fresh'
        """
        params: list[Any] = [item_id]

        if representation is not None:
            query += " and representation = %s"
            params.append(representation.value)

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(query, tuple(params))
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
        representation: RepresentationSource = RepresentationSource.RAW,
        representation_version: str | None = None,
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
                    item_content_version,
                    representation,
                    representation_version
                ) values (
                    %s, %s, %s, %s, %s, %s, %s::text[], %s, 'fresh', %s, %s, %s
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
                    representation.value,
                    representation_version,
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
        source: RepresentationSource = RepresentationSource.RAW,
        intent_schema_version: str | None = None,
        intent_prompt_version: str | None = None,
    ) -> list[CandidateNeighbor]:
        if source == RepresentationSource.RAW:
            query = """
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
            """
            params: tuple[Any, ...] = (
                item_id,
                model,
                repo_id,
                item_type.value,
                include_states,
                min_score,
                k,
            )
        elif source == RepresentationSource.INTENT:
            if not intent_schema_version or not intent_prompt_version:
                msg = "intent schema/prompt versions are required for source=intent"
                raise ValueError(msg)

            query = """
                with latest_fresh as (
                    select distinct on (ic.item_id)
                        ic.id as intent_card_id,
                        ic.item_id
                    from public.intent_cards ic
                    where
                        ic.schema_version = %s
                        and ic.prompt_version = %s
                        and ic.status = 'fresh'
                    order by ic.item_id, ic.created_at desc, ic.id desc
                )
                select
                    i2.id as candidate_item_id,
                    (1 - (e1.embedding <=> e2.embedding))::double precision as score
                from latest_fresh lf1
                join public.intent_embeddings e1
                    on e1.intent_card_id = lf1.intent_card_id and e1.model = %s
                join public.items i1 on i1.id = lf1.item_id
                join latest_fresh lf2
                    on lf2.item_id <> lf1.item_id
                join public.intent_embeddings e2
                    on e2.intent_card_id = lf2.intent_card_id and e2.model = e1.model
                join public.items i2 on i2.id = lf2.item_id
                where
                    lf1.item_id = %s
                    and i1.repo_id = %s
                    and i1.type = %s
                    and i2.repo_id = i1.repo_id
                    and i2.type = i1.type
                    and i2.state = any(%s::text[])
                    and (1 - (e1.embedding <=> e2.embedding)) >= %s
                order by (e1.embedding <=> e2.embedding) asc
                limit %s
            """
            params = (
                intent_schema_version,
                intent_prompt_version,
                model,
                item_id,
                repo_id,
                item_type.value,
                include_states,
                min_score,
                k,
            )
        else:
            msg = f"unsupported representation source: {source.value}"
            raise ValueError(msg)

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
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
                    and cs.representation = 'raw'
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
            where src.state = 'open'
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

    def list_candidate_sets_for_judge_audit(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
        sample_size: int,
        sample_seed: int,
    ) -> list[JudgeWorkItem]:
        query = """
            with latest_fresh as (
                select distinct on (cs.item_id)
                    cs.id as candidate_set_id,
                    cs.item_id
                from public.candidate_sets cs
                join public.items src on src.id = cs.item_id
                where
                    cs.repo_id = %s
                    and cs.type = %s
                    and cs.representation = 'raw'
                    and cs.status = 'fresh'
                    and src.state = 'open'
                    and exists (
                        select 1
                        from public.candidate_set_members m
                        where m.candidate_set_id = cs.id
                    )
                order by cs.item_id, cs.created_at desc
            ),
            sampled as (
                select
                    lf.candidate_set_id,
                    lf.item_id
                from latest_fresh lf
                order by md5(lf.item_id::text || ':' || %s::text)
                limit %s
            )
            select
                s.candidate_set_id,
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
            from sampled s
            join public.items src
                on src.id = s.item_id
            left join public.candidate_set_members m
                on m.candidate_set_id = s.candidate_set_id
            left join public.items cand
                on cand.id = m.candidate_item_id
            order by s.candidate_set_id asc, m.rank asc
        """

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, (repo_id, item_type.value, sample_seed, sample_size))
            rows = cur.fetchall()

        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            candidate_set_id = int(row["candidate_set_id"])
            current = grouped.get(candidate_set_id)
            if current is None:
                current = {
                    "candidate_set_id": candidate_set_id,
                    "candidate_set_status": "fresh",
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
                from public.judge_decisions
                where
                    repo_id = %s
                    and type = %s
                    and from_item_id = %s
                    and final_status = 'accepted'
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
        final_status: Literal["accepted", "rejected", "skipped"]
        if status == "accepted":
            final_status = "accepted"
        elif status == "rejected":
            final_status = "rejected"
        else:
            msg = f"invalid duplicate edge status: {status}"
            raise DatabaseError(msg)

        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.judge_decisions (
                    repo_id,
                    type,
                    from_item_id,
                    to_item_id,
                    model_is_duplicate,
                    final_status,
                    confidence,
                    reasoning,
                    min_edge,
                    llm_provider,
                    llm_model,
                    created_by,
                    created_at
                ) values (
                    %s, %s, %s, %s, true, %s, %s, %s, 0, %s, %s, %s, %s
                )
                """,
                (
                    repo_id,
                    item_type.value,
                    from_item_id,
                    to_item_id,
                    final_status,
                    confidence,
                    reasoning,
                    llm_provider,
                    llm_model,
                    created_by,
                    created_at,
                ),
            )

    def insert_judge_decision(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
        from_item_id: int,
        candidate_set_id: int | None,
        to_item_id: int | None,
        model_is_duplicate: bool,
        final_status: Literal["accepted", "rejected", "skipped"],
        confidence: float,
        reasoning: str,
        relation: str | None,
        root_cause_match: str | None,
        scope_relation: str | None,
        path_match: str | None,
        certainty: str | None,
        veto_reason: str | None,
        min_edge: float,
        llm_provider: str,
        llm_model: str,
        created_by: str,
        created_at: datetime,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.judge_decisions (
                    repo_id,
                    type,
                    from_item_id,
                    candidate_set_id,
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
                ) values (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                """,
                (
                    repo_id,
                    item_type.value,
                    from_item_id,
                    candidate_set_id,
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
                    created_at,
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
                update public.judge_decisions
                set final_status = 'rejected',
                    veto_reason = coalesce(veto_reason, 'superseded_by_rejudge')
                where
                    repo_id = %s
                    and type = %s
                    and from_item_id = %s
                    and final_status = 'accepted'
                """,
                (repo_id, item_type.value, from_item_id),
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
                from public.judge_decisions
                where
                    repo_id = %s
                    and type = %s
                    and final_status = 'accepted'
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
                from public.judge_decisions
                where
                    repo_id = %s
                    and type = %s
                    and final_status = 'accepted'
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
                    from public.judge_decisions
                    where repo_id = %s and type = %s and final_status = 'accepted'
                    union
                    select to_item_id as item_id
                    from public.judge_decisions
                    where repo_id = %s and type = %s and final_status = 'accepted'
                )
                select
                    i.id as item_id,
                    i.number,
                    i.state,
                    i.author_login,
                    i.title,
                    i.body,
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
                    title=row.get("title"),
                    body=row.get("body"),
                    assignees=assignees,
                    assignees_unknown=assignees_unknown,
                    comment_count=int(row["comment_count"]),
                    review_comment_count=int(row["review_comment_count"]),
                    created_at_gh=row.get("created_at_gh"),
                )
            )
        return result

    def create_judge_audit_run(
        self,
        *,
        repo_id: int,
        item_type: ItemType,
        sample_policy: str,
        sample_seed: int,
        sample_size_requested: int,
        sample_size_actual: int,
        min_edge: float,
        cheap_llm_provider: str,
        cheap_llm_model: str,
        strong_llm_provider: str,
        strong_llm_model: str,
        created_by: str,
        created_at: datetime,
    ) -> int:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                insert into public.judge_audit_runs (
                    repo_id,
                    type,
                    sample_policy,
                    sample_seed,
                    sample_size_requested,
                    sample_size_actual,
                    min_edge,
                    cheap_llm_provider,
                    cheap_llm_model,
                    strong_llm_provider,
                    strong_llm_model,
                    status,
                    created_by,
                    created_at
                ) values (
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 'running', %s, %s
                )
                returning id
                """,
                (
                    repo_id,
                    item_type.value,
                    sample_policy,
                    sample_seed,
                    sample_size_requested,
                    sample_size_actual,
                    min_edge,
                    cheap_llm_provider,
                    cheap_llm_model,
                    strong_llm_provider,
                    strong_llm_model,
                    created_by,
                    created_at,
                ),
            )
            row = cur.fetchone()

        if row is None:
            msg = "failed to create judge_audit_run"
            raise DatabaseError(msg)

        return int(row["id"])

    def insert_judge_audit_run_item(
        self,
        *,
        audit_run_id: int,
        source_item_id: int,
        source_number: int,
        source_state: StateFilter,
        candidate_set_id: int,
        cheap_model_is_duplicate: bool,
        cheap_final_status: Literal["accepted", "rejected", "skipped"],
        cheap_to_item_id: int | None,
        cheap_confidence: float,
        cheap_veto_reason: str | None,
        cheap_reasoning: str,
        strong_model_is_duplicate: bool,
        strong_final_status: Literal["accepted", "rejected", "skipped"],
        strong_to_item_id: int | None,
        strong_confidence: float,
        strong_veto_reason: str | None,
        strong_reasoning: str,
        outcome_class: Literal["tp", "fp", "fn", "tn", "conflict", "incomplete"],
        created_at: datetime,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                insert into public.judge_audit_run_items (
                    audit_run_id,
                    source_item_id,
                    source_number,
                    source_state,
                    candidate_set_id,
                    cheap_model_is_duplicate,
                    cheap_final_status,
                    cheap_to_item_id,
                    cheap_confidence,
                    cheap_veto_reason,
                    cheap_reasoning,
                    strong_model_is_duplicate,
                    strong_final_status,
                    strong_to_item_id,
                    strong_confidence,
                    strong_veto_reason,
                    strong_reasoning,
                    outcome_class,
                    created_at
                ) values (
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s
                )
                """,
                (
                    audit_run_id,
                    source_item_id,
                    source_number,
                    source_state.value,
                    candidate_set_id,
                    cheap_model_is_duplicate,
                    cheap_final_status,
                    cheap_to_item_id,
                    cheap_confidence,
                    cheap_veto_reason,
                    cheap_reasoning,
                    strong_model_is_duplicate,
                    strong_final_status,
                    strong_to_item_id,
                    strong_confidence,
                    strong_veto_reason,
                    strong_reasoning,
                    outcome_class,
                    created_at,
                ),
            )

    def complete_judge_audit_run(
        self,
        *,
        audit_run_id: int,
        status: Literal["completed", "failed"],
        sample_size_actual: int,
        compared_count: int,
        tp: int,
        fp: int,
        fn: int,
        tn: int,
        conflict: int,
        incomplete: int,
        completed_at: datetime,
    ) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                update public.judge_audit_runs
                set
                    status = %s,
                    sample_size_actual = %s,
                    compared_count = %s,
                    tp = %s,
                    fp = %s,
                    fn = %s,
                    tn = %s,
                    conflict = %s,
                    incomplete = %s,
                    completed_at = %s
                where id = %s
                """,
                (
                    status,
                    sample_size_actual,
                    compared_count,
                    tp,
                    fp,
                    fn,
                    tn,
                    conflict,
                    incomplete,
                    completed_at,
                    audit_run_id,
                ),
            )
            if cur.rowcount != 1:
                msg = f"judge_audit_run not found: {audit_run_id}"
                raise DatabaseError(msg)

    def get_judge_audit_run_report(self, *, audit_run_id: int) -> JudgeAuditRunReport | None:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select
                    j.id as audit_run_id,
                    r.org,
                    r.name,
                    j.type,
                    j.status,
                    j.sample_policy,
                    j.sample_seed,
                    j.sample_size_requested,
                    j.sample_size_actual,
                    j.candidate_set_status,
                    j.source_state_filter,
                    j.min_edge,
                    j.cheap_llm_provider,
                    j.cheap_llm_model,
                    j.strong_llm_provider,
                    j.strong_llm_model,
                    j.compared_count,
                    j.tp,
                    j.fp,
                    j.fn,
                    j.tn,
                    j.conflict,
                    j.incomplete,
                    j.created_by,
                    j.created_at,
                    j.completed_at
                from public.judge_audit_runs j
                join public.repos r
                    on r.id = j.repo_id
                where j.id = %s
                limit 1
                """,
                (audit_run_id,),
            )
            row = cur.fetchone()

        if row is None:
            return None

        status = str(row["status"])
        if status not in {"running", "completed", "failed"}:
            msg = f"invalid judge_audit_run status: {status}"
            raise DatabaseError(msg)

        return JudgeAuditRunReport(
            audit_run_id=int(row["audit_run_id"]),
            repo=f"{str(row['org'])}/{str(row['name'])}",
            type=ItemType(str(row["type"])),
            status=cast(Literal["running", "completed", "failed"], status),
            sample_policy=str(row["sample_policy"]),
            sample_seed=int(row["sample_seed"]),
            sample_size_requested=int(row["sample_size_requested"]),
            sample_size_actual=int(row["sample_size_actual"]),
            candidate_set_status=str(row["candidate_set_status"]),
            source_state_filter=str(row["source_state_filter"]),
            min_edge=float(row["min_edge"]),
            cheap_provider=str(row["cheap_llm_provider"]),
            cheap_model=str(row["cheap_llm_model"]),
            strong_provider=str(row["strong_llm_provider"]),
            strong_model=str(row["strong_llm_model"]),
            compared_count=int(row["compared_count"]),
            tp=int(row["tp"]),
            fp=int(row["fp"]),
            fn=int(row["fn"]),
            tn=int(row["tn"]),
            conflict=int(row["conflict"]),
            incomplete=int(row["incomplete"]),
            created_by=str(row["created_by"]),
            created_at=cast(datetime, row["created_at"]),
            completed_at=cast(datetime | None, row.get("completed_at")),
        )

    def list_judge_audit_simulation_rows(
        self,
        *,
        audit_run_id: int,
    ) -> list[JudgeAuditSimulationRow]:
        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select
                    j.source_number,
                    j.candidate_set_id,
                    j.cheap_final_status,
                    j.cheap_to_item_id,
                    j.strong_final_status,
                    j.strong_to_item_id,
                    j.cheap_confidence,
                    j.strong_confidence,
                    (
                        select m.rank
                        from public.candidate_set_members m
                        where
                            m.candidate_set_id = j.candidate_set_id
                            and m.candidate_item_id = j.cheap_to_item_id
                        limit 1
                    ) as cheap_target_rank,
                    (
                        select m.score
                        from public.candidate_set_members m
                        where
                            m.candidate_set_id = j.candidate_set_id
                            and m.candidate_item_id = j.cheap_to_item_id
                        limit 1
                    ) as cheap_target_score,
                    (
                        select m.score
                        from public.candidate_set_members m
                        where
                            m.candidate_set_id = j.candidate_set_id
                            and (
                                j.cheap_to_item_id is null
                                or m.candidate_item_id <> j.cheap_to_item_id
                            )
                        order by m.rank asc
                        limit 1
                    ) as cheap_best_alternative_score
                from public.judge_audit_run_items j
                where j.audit_run_id = %s
                order by j.source_number asc
                """,
                (audit_run_id,),
            )
            rows = cur.fetchall()

        result: list[JudgeAuditSimulationRow] = []
        for row in rows:
            cheap_final_status = str(row["cheap_final_status"])
            if cheap_final_status not in {"accepted", "rejected", "skipped"}:
                msg = f"invalid cheap final status: {cheap_final_status}"
                raise DatabaseError(msg)

            strong_final_status = str(row["strong_final_status"])
            if strong_final_status not in {"accepted", "rejected", "skipped"}:
                msg = f"invalid strong final status: {strong_final_status}"
                raise DatabaseError(msg)

            result.append(
                JudgeAuditSimulationRow(
                    source_number=int(row["source_number"]),
                    candidate_set_id=int(row["candidate_set_id"]),
                    cheap_final_status=cast(
                        Literal["accepted", "rejected", "skipped"],
                        cheap_final_status,
                    ),
                    cheap_to_item_id=(
                        int(row["cheap_to_item_id"])
                        if row.get("cheap_to_item_id") is not None
                        else None
                    ),
                    strong_final_status=cast(
                        Literal["accepted", "rejected", "skipped"],
                        strong_final_status,
                    ),
                    strong_to_item_id=(
                        int(row["strong_to_item_id"])
                        if row.get("strong_to_item_id") is not None
                        else None
                    ),
                    cheap_confidence=float(row["cheap_confidence"]),
                    strong_confidence=float(row["strong_confidence"]),
                    cheap_target_rank=(
                        int(row["cheap_target_rank"])
                        if row.get("cheap_target_rank") is not None
                        else None
                    ),
                    cheap_target_score=(
                        float(row["cheap_target_score"])
                        if row.get("cheap_target_score") is not None
                        else None
                    ),
                    cheap_best_alternative_score=(
                        float(row["cheap_best_alternative_score"])
                        if row.get("cheap_best_alternative_score") is not None
                        else None
                    ),
                )
            )
        return result

    def list_judge_audit_disagreements(
        self,
        *,
        audit_run_id: int,
        limit: int,
    ) -> list[JudgeAuditDisagreement]:
        if limit <= 0:
            return []

        with self._connect() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                select
                    j.outcome_class,
                    j.source_number,
                    j.cheap_final_status,
                    cheap.number as cheap_to_number,
                    j.cheap_confidence,
                    j.cheap_veto_reason,
                    j.strong_final_status,
                    strong.number as strong_to_number,
                    j.strong_confidence,
                    j.strong_veto_reason
                from public.judge_audit_run_items j
                left join public.items cheap
                    on cheap.id = j.cheap_to_item_id
                left join public.items strong
                    on strong.id = j.strong_to_item_id
                where
                    j.audit_run_id = %s
                    and j.outcome_class in ('fp', 'fn', 'conflict', 'incomplete')
                order by
                    case j.outcome_class
                        when 'conflict' then 0
                        when 'incomplete' then 1
                        when 'fp' then 2
                        when 'fn' then 3
                        else 4
                    end,
                    j.source_number asc
                limit %s
                """,
                (audit_run_id, limit),
            )
            rows = cur.fetchall()

        result: list[JudgeAuditDisagreement] = []
        for row in rows:
            outcome_class = str(row["outcome_class"])
            if outcome_class not in {"fp", "fn", "conflict", "incomplete"}:
                msg = f"invalid outcome class: {outcome_class}"
                raise DatabaseError(msg)

            cheap_final_status = str(row["cheap_final_status"])
            if cheap_final_status not in {"accepted", "rejected", "skipped"}:
                msg = f"invalid cheap final status: {cheap_final_status}"
                raise DatabaseError(msg)

            strong_final_status = str(row["strong_final_status"])
            if strong_final_status not in {"accepted", "rejected", "skipped"}:
                msg = f"invalid strong final status: {strong_final_status}"
                raise DatabaseError(msg)

            result.append(
                JudgeAuditDisagreement(
                    outcome_class=cast(
                        Literal["fp", "fn", "conflict", "incomplete"],
                        outcome_class,
                    ),
                    source_number=int(row["source_number"]),
                    cheap_final_status=cast(
                        Literal["accepted", "rejected", "skipped"],
                        cheap_final_status,
                    ),
                    cheap_to_number=(
                        int(row["cheap_to_number"])
                        if row.get("cheap_to_number") is not None
                        else None
                    ),
                    cheap_confidence=float(row["cheap_confidence"]),
                    cheap_veto_reason=(
                        str(row["cheap_veto_reason"])
                        if row.get("cheap_veto_reason") is not None
                        else None
                    ),
                    strong_final_status=cast(
                        Literal["accepted", "rejected", "skipped"],
                        strong_final_status,
                    ),
                    strong_to_number=(
                        int(row["strong_to_number"])
                        if row.get("strong_to_number") is not None
                        else None
                    ),
                    strong_confidence=float(row["strong_confidence"]),
                    strong_veto_reason=(
                        str(row["strong_veto_reason"])
                        if row.get("strong_veto_reason") is not None
                        else None
                    ),
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

    def copy_close_run_items(
        self,
        *,
        source_close_run_id: int,
        target_close_run_id: int,
        created_at: datetime,
    ) -> int:
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
                )
                select
                    %s,
                    cri.item_id,
                    cri.canonical_item_id,
                    cri.action,
                    cri.skip_reason,
                    %s
                from public.close_run_items cri
                where cri.close_run_id = %s
                """,
                (
                    target_close_run_id,
                    created_at,
                    source_close_run_id,
                ),
            )
            inserted = cur.rowcount

        return max(int(inserted), 0)

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
                    from public.judge_decisions
                    where repo_id = %s and type = %s and final_status = 'accepted'
                    union
                    select to_item_id as item_id
                    from public.judge_decisions
                    where repo_id = %s and type = %s and final_status = 'accepted'
                )
                select
                    i.id as item_id,
                    i.number,
                    i.state,
                    i.author_login,
                    i.title,
                    i.body,
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
                    title=row.get("title"),
                    body=row.get("body"),
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
