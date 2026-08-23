"""Agent event history and conversation-search state."""

from __future__ import annotations

from typing import Any

from host.runtime.core import db, pgclient
from host.runtime.core.state._base import (
    AGENT_EVENT_LIMIT,
    CONVERSATION_EMBEDDING_MAX_ATTEMPTS,
    CONVERSATION_EMBEDDING_MESSAGE_LIMIT,
    CONVERSATION_EMBEDDING_PRUNE_EVERY_BATCHES,
    CONVERSATION_SEARCH_STATEMENT_TIMEOUT_MS,
    EVENT_PAGE_LIMIT,
    MAX_EVENT_MESSAGE_CHARS,
    NETWORK_EVENT_LIMIT,
    PRUNE_EVERY,
    TOOL_EVENT_LIMIT,
    _AGENT_HISTORY_COUNTERS,
    conversation_embedding_work,
    mutation,
    utc_now,
)
from host.runtime.core.state.threads import _increment_counter

# -- agent events -------------------------------------------------------------------


# The typed event payload fields; every event the runtime emits uses a subset.
_EVENT_PAYLOAD_COLUMNS = ("message", "source", "error_message", "agent_runtime", "activity")
_EVENT_FIELDS = (
    "seq, created_at, event_type, thread_id, run_number, "
    + ", ".join(_EVENT_PAYLOAD_COLUMNS)
)


def _event_dict(row: Any) -> dict[str, Any]:
    seq, created_at, event_type, thread_id, run_number = row[:5]
    payload = {
        column: value for column, value in zip(_EVENT_PAYLOAD_COLUMNS, row[5:]) if value is not None
    }
    activity = payload.get("activity")
    if (
        event_type == "thread.activity"
        and run_number is not None
        and isinstance(activity, dict)
        and activity.get("activity_id")
    ):
        payload["activity"] = {
            **activity,
            # Opaque host scoping prevents provider ids reused by a later
            # process from merging with an older activity.  The private run
            # column itself is never returned by the API.
            "activity_id": f"{run_number}:{activity['activity_id']}",
        }
    return {
        "seq": int(seq),
        "timestamp": created_at,
        "event_id": f"event_{seq}",
        "event_type": event_type,
        "thread_id": thread_id,
        "payload": payload,
    }


def _jsonb_safe(value: Any) -> Any:
    """Recursively escape NULs, which PostgreSQL JSONB cannot represent."""
    if isinstance(value, str):
        return value.replace("\x00", "\\0")
    if isinstance(value, list):
        return [_jsonb_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonb_safe(item) for item in value]
    if isinstance(value, dict):
        return {
            _jsonb_safe(key) if isinstance(key, str) else key: _jsonb_safe(item)
            for key, item in value.items()
        }
    return value


def _bounded_event_message(value: Any) -> Any:
    """Cap a single audit message so one over-large streamed message cannot grow
    durable storage past the row-count cap. Non-strings and in-bound strings
    pass through unchanged; an over-limit string is truncated with a marker that
    records the original length."""
    if isinstance(value, str) and len(value) > MAX_EVENT_MESSAGE_CHARS:
        return (
            value[:MAX_EVENT_MESSAGE_CHARS]
            + f"\n…[truncated {len(value)} chars to {MAX_EVENT_MESSAGE_CHARS}]"
        )
    return value


def append_agent_event(
    cur: Any,
    event_type: str,
    thread_id: str | None,
    payload: dict[str, Any],
    *,
    run_number: int | None = None,
) -> int:
    """Insert one event inside the caller's mutation transaction, so the event
    commits or rolls back with the state change that caused it. seq is a
    serial: unique and increasing, with harmless gaps from aborted
    transactions. Payload keys map to the typed columns; an unknown key is a
    programming error and fails loudly."""
    unknown = set(payload) - set(_EVENT_PAYLOAD_COLUMNS)
    if unknown:
        raise ValueError(f"unsupported event payload keys: {sorted(unknown)}")
    values: list[Any] = []
    for column in _EVENT_PAYLOAD_COLUMNS:
        value = payload.get(column)
        if column == "activity" and value is not None:
            values.append(pgclient.Jsonb(_jsonb_safe(value)))
        elif column in ("message", "error_message"):
            values.append(_bounded_event_message(value))
        else:
            values.append(value)
    cur.execute(
        "INSERT INTO agent_events (created_at, event_type, thread_id, run_number,"
        " message, source, error_message, agent_runtime, activity)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING seq",
        (utc_now(), event_type, thread_id, run_number, *values),
    )
    seq = int(cur.fetchone()[0])
    if event_type == "thread.message" and payload.get("message") is not None:
        # Enqueue in the same transaction that writes the event, so the indexer
        # never has to rediscover outstanding work by scanning the retention
        # window. Nothing is lost if this transaction rolls back.
        cur.execute(
            "INSERT INTO conversation_embedding_queue (event_seq) VALUES (%s)"
            " ON CONFLICT DO NOTHING",
            (seq,),
        )
        # This is only a latency hint; the queue is the durable source of truth
        # and the indexer has a 30-second backstop. Waking before commit can
        # cause one harmless empty claim, while plumbing callback state through
        # every event-writing mutation would make this optimization invasive.
        conversation_embedding_work.set()
    counter = None
    if event_type == "thread.activity":
        counter = _AGENT_HISTORY_COUNTERS["activities"]
    elif event_type == "thread.message" and payload.get("source") == "user":
        counter = _AGENT_HISTORY_COUNTERS["messages"]
    elif event_type == "thread.message" and payload.get("source") == "agent":
        counter = _AGENT_HISTORY_COUNTERS["activities"]
    if counter is not None:
        _increment_counter(cur, counter)
    if seq % PRUNE_EVERY == 0:
        prune_agent_events(cur)
    return seq


def _prune_events(cur: Any, table: str, limit: int) -> None:
    # Shared pruning mechanism for the three audit logs (agent, network, tool
    # events), with the retained depth selected by the caller.
    # seq is a serial, so newest-N retention is a primary-key range
    # delete below MAX(seq) - N: two index-endpoint lookups and the excess
    # rows, instead of scanning N index entries per prune. Seq gaps from
    # aborted transactions only make retention keep slightly fewer rows.
    # ``table`` is always a module-constant name, never external input.
    cur.execute(
        f"DELETE FROM {table} WHERE"
        f" seq <= (SELECT COALESCE(MAX(seq), 0) FROM {table}) - %s",
        (limit,),
    )


def _page_before(
    table: str,
    fields: str,
    row_fn: Any,
    before: int | None,
    limit: int,
    extra_clause: str | None = None,
    extra_params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    """One newest-first page of an audit log: rows with ``seq < before`` (all
    rows when ``before`` is None). ``table``/``fields``/``extra_clause`` are
    module constants, never external input."""
    clauses = list(() if extra_clause is None else (extra_clause,))
    params: list[Any] = list(extra_params)
    if before is not None:
        clauses.append("seq < %s")
        params.append(before)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {fields} FROM {table}{where} ORDER BY seq DESC LIMIT %s",
            tuple(params) + (limit,),
        )
        return [row_fn(row) for row in cur.fetchall()]


def prune_agent_events(cur: Any) -> None:
    _prune_events(cur, "agent_events", AGENT_EVENT_LIMIT)


def prune_event_logs(cur: Any) -> None:
    """Apply all append-only event caps independent of insert cadence."""
    for table, limit in (
        ("agent_events", AGENT_EVENT_LIMIT),
        ("network_events", NETWORK_EVENT_LIMIT),
        ("tool_events", TOOL_EVENT_LIMIT),
    ):
        _prune_events(cur, table, limit)
    prune_conversation_embeddings(cur)


def page_agent_events_before(
    before: int | None, *, limit: int = EVENT_PAGE_LIMIT
) -> list[dict[str, Any]]:
    return _page_before("agent_events", _EVENT_FIELDS, _event_dict, before, limit)


def page_thread_events(
    thread_id: str,
    since: int | None,
    limit: int,
    *,
    before: int | None = None,
    event_types: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """One chronological page of a thread's turn events.

    ``since`` pages forward for live updates. ``before`` pages backward from
    the oldest event a client already has. With neither cursor, the newest
    page is returned so opening a long thread does not scan its full history.
    Optional event-type filtering happens in PostgreSQL before ``limit``, so
    an activity-dense run cannot crowd messages out of a conversation page.
    Backward and initial pages are selected newest-first in the inner query,
    then restored to chronological order for chat rendering.
    """
    event_type_clause = ""
    event_type_params: tuple[Any, ...] = ()
    if event_types is not None:
        if not event_types:
            return []
        placeholders = ", ".join(["%s"] * len(event_types))
        event_type_clause = f" AND event_type IN ({placeholders})"
        event_type_params = event_types
    with db.transaction() as cur:
        if since is not None:
            cur.execute(
                f"SELECT {_EVENT_FIELDS} FROM agent_events"
                " WHERE thread_id = %s AND seq > %s"
                f"{event_type_clause} ORDER BY seq LIMIT %s",
                (thread_id, since, *event_type_params, limit),
            )
        else:
            before_clause = " AND seq < %s" if before is not None else ""
            before_params: tuple[Any, ...] = (before,) if before is not None else ()
            params = (
                thread_id,
                *before_params,
                *event_type_params,
                limit,
            )
            cur.execute(
                f"SELECT * FROM (SELECT {_EVENT_FIELDS} FROM agent_events"
                " WHERE thread_id = %s"
                f"{before_clause}{event_type_clause} ORDER BY seq DESC LIMIT %s) AS newest"
                " ORDER BY seq",
                params,
            )
        return [_event_dict(row) for row in cur.fetchall()]


def page_thread_events_around(
    thread_id: str,
    anchor: int,
    limit: int,
    *,
    event_types: tuple[str, ...],
) -> list[dict[str, Any]] | None:
    """One chronological page centered on an event in ``thread_id``.

    ``None`` means the anchor is not an event in this thread. Selecting each
    side independently keeps both queries on ``(thread_id, seq)`` and avoids a
    distance sort over the full retained history.
    """
    if not event_types:
        return []
    placeholders = ", ".join(["%s"] * len(event_types))
    older_target = (limit + 1) // 2
    with db.transaction() as cur:
        cur.execute(
            "SELECT 1 FROM agent_events WHERE thread_id = %s AND seq = %s"
            f" AND event_type IN ({placeholders})",
            (thread_id, anchor, *event_types),
        )
        if cur.fetchone() is None:
            return None
        cur.execute(
            f"SELECT {_EVENT_FIELDS} FROM agent_events"
            " WHERE thread_id = %s AND seq <= %s"
            f" AND event_type IN ({placeholders})"
            " ORDER BY seq DESC LIMIT %s",
            (thread_id, anchor, *event_types, limit),
        )
        older_desc = cur.fetchall()
        cur.execute(
            f"SELECT {_EVENT_FIELDS} FROM agent_events"
            " WHERE thread_id = %s AND seq > %s"
            f" AND event_type IN ({placeholders})"
            " ORDER BY seq LIMIT %s",
            (thread_id, anchor, *event_types, limit),
        )
        newer = cur.fetchall()
    selected_older = older_desc[:older_target]
    selected_newer = newer[: limit - len(selected_older)]
    remaining = limit - len(selected_older) - len(selected_newer)
    if remaining:
        selected_older.extend(
            older_desc[len(selected_older) : len(selected_older) + remaining]
        )
    rows = (*reversed(selected_older), *selected_newer)
    return [_event_dict(row) for row in rows]


def thread_event_page_bounds(
    thread_id: str,
    oldest: int,
    newest: int,
    *,
    event_types: tuple[str, ...],
) -> tuple[bool, bool]:
    """Whether matching retained events exist outside a returned page."""
    if not event_types:
        return False, False
    placeholders = ", ".join(["%s"] * len(event_types))
    with db.transaction() as cur:
        cur.execute(
            "SELECT"
            " EXISTS(SELECT 1 FROM agent_events WHERE thread_id = %s AND seq < %s"
            f" AND event_type IN ({placeholders})),"
            " EXISTS(SELECT 1 FROM agent_events WHERE thread_id = %s AND seq > %s"
            f" AND event_type IN ({placeholders}))",
            (
                thread_id,
                oldest,
                *event_types,
                thread_id,
                newest,
                *event_types,
            ),
        )
        row = cur.fetchone()
    return (bool(row[0]), bool(row[1])) if row is not None else (False, False)


def search_thread_messages(
    query_variants: tuple[str, ...],
    *,
    from_timestamp: str | None,
    to_timestamp: str | None,
    thread_id: str | None,
    sources: tuple[str, ...],
    limit: int,
    before: tuple[float, int] | tuple[str, int] | None,
    max_seq: int | None = None,
    exclude_seqs: tuple[int, ...] = (),
) -> list[dict[str, Any]]:
    """Search retained thread messages with indexed relevance or time paging.

    Natural-language variants are ORed into one ``tsquery``. The caller owns
    validation and cursor mode; this accessor keeps every filter parameterized
    and returns one extra row when asked so it never needs to count all hits.
    ``exclude_seqs`` drops messages a caller has already delivered by another
    ranking, so a bounded walk can span both without repeating a hit.
    """
    clauses = ["events.event_type = 'thread.message'", "events.message IS NOT NULL"]
    params: list[Any] = []
    if exclude_seqs:
        placeholders = ", ".join("%s" for _ in exclude_seqs)
        clauses.append(f"events.seq NOT IN ({placeholders})")
        params.extend(exclude_seqs)
    if max_seq is not None:
        clauses.append("events.seq <= %s")
        params.append(max_seq)
    if thread_id is not None:
        clauses.append("events.thread_id = %s")
        params.append(thread_id)
    if from_timestamp is not None:
        clauses.append("events.created_at >= %s")
        params.append(from_timestamp)
    if to_timestamp is not None:
        clauses.append("events.created_at < %s")
        params.append(to_timestamp)
    if sources:
        placeholders = ", ".join(["%s"] * len(sources))
        clauses.append(f"events.source IN ({placeholders})")
        params.extend(sources)
    else:
        return []
    where = " AND ".join(clauses)

    if query_variants:
        query_expression = " || ".join(
            ["websearch_to_tsquery('simple', %s)"] * len(query_variants)
        )
        cursor_clause = ""
        cursor_params: tuple[Any, ...] = ()
        if before is not None:
            rank, seq = before
            cursor_clause = (
                " WHERE search_rank < %s OR (search_rank = %s AND seq < %s)"
            )
            cursor_params = (rank, rank, seq)
        sql = (
            f"WITH search_query AS (SELECT ({query_expression}) AS value),"
            " ranked AS ("
            " SELECT events.seq, events.created_at, events.thread_id, events.source,"
            " ts_rank_cd(to_tsvector('simple', COALESCE(events.message, '')), search_query.value)::float8"
            " AS search_rank,"
            " events.message,"
            " LEFT(ts_headline('simple', events.message, search_query.value,"
            " 'MaxWords=64, MinWords=16, ShortWord=1, MaxFragments=2, StartSel=[[,'"
            " || ' StopSel=]]'), 4096) AS excerpt"
            " FROM agent_events AS events"
            " CROSS JOIN search_query"
            f" WHERE {where}"
            " AND to_tsvector('simple', COALESCE(events.message, '')) @@ search_query.value"
            ")"
            " SELECT seq, created_at, thread_id, source, search_rank, excerpt,"
            " to_tsvector('simple', message) <> to_tsvector('simple', excerpt)"
            " AS excerpt_truncated"
            f" FROM ranked{cursor_clause}"
            " ORDER BY search_rank DESC, seq DESC LIMIT %s"
        )
        execute_params = (*query_variants, *params, *cursor_params, limit)
    else:
        cursor_clause = ""
        cursor_params = ()
        if before is not None:
            timestamp, seq = before
            cursor_clause = " AND (created_at, seq) < (%s, %s)"
            cursor_params = (timestamp, seq)
        sql = (
            "SELECT events.seq, events.created_at, events.thread_id, events.source,"
            " NULL::float8 AS search_rank,"
            " LEFT(events.message, 4096) AS excerpt,"
            " events.message <> LEFT(events.message, 4096) AS excerpt_truncated"
            " FROM agent_events AS events"
            f" WHERE {where}{cursor_clause}"
            " ORDER BY created_at DESC, seq DESC LIMIT %s"
        )
        execute_params = (*params, *cursor_params, limit)

    with db.transaction() as cur:
        if query_variants:
            cur.execute(
                "SET LOCAL statement_timeout ="
                f" '{CONVERSATION_SEARCH_STATEMENT_TIMEOUT_MS}ms'"
            )
        cur.execute(sql, execute_params)
        rows = cur.fetchall()
    return [
        {
            "seq": int(seq),
            "event_id": f"event_{seq}",
            "timestamp": str(created_at),
            "thread_id": str(result_thread_id),
            "source": str(source),
            "search_rank": search_rank,
            "excerpt": str(excerpt),
            "excerpt_truncated": bool(excerpt_truncated),
        }
        for (
            seq,
            created_at,
            result_thread_id,
            source,
            search_rank,
            excerpt,
            excerpt_truncated,
        ) in rows
    ]


def unembedded_thread_messages(limit: int) -> list[tuple[int, str]]:
    """Claim the newest outstanding messages from the embedding queue.

    Reads only queued work, so a caught-up indexer scans an empty table rather
    than walking the whole retention window to prove there is nothing to do.
    One message inference keeps rejecting must not wedge every newer message
    behind it, so exhausted rows are removed from the queue.
    """
    with mutation() as cur:
        cur.execute(
            "SELECT queue.event_seq, events.message"
            " FROM conversation_embedding_queue AS queue"
            " JOIN agent_events AS events ON events.seq = queue.event_seq"
            " WHERE events.message IS NOT NULL"
            " ORDER BY queue.event_seq DESC LIMIT %s",
            (limit,),
        )
        rows = cur.fetchall()
    return [(int(seq), str(message)) for seq, message in rows]


def conversation_search_snapshot() -> tuple[int, int, int, int]:
    """Stable source/vector boundaries for one relevance-search walk."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT COALESCE(MIN(seq), 0), COALESCE(MAX(seq), 0),"
            " COALESCE((SELECT MIN(event_seq)"
            " FROM conversation_message_embeddings), 0), COALESCE(("
            " SELECT embedding_generation FROM conversation_search_state"
            " WHERE singleton = TRUE), 0)"
            " FROM agent_events"
        )
        row = cur.fetchone()
    if row is None:
        return 0, 0, 0, 0
    return int(row[0]), int(row[1]), int(row[2]), int(row[3])


def conversation_search_retention() -> tuple[int, int]:
    """Current source and vector floors used to expire positional cursors."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT COALESCE(MIN(seq), 0), COALESCE(("
            " SELECT MIN(event_seq) FROM conversation_message_embeddings), 0)"
            " FROM agent_events"
        )
        row = cur.fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def record_embedding_attempts(seqs: list[int]) -> None:
    """Charge one failed attempt and drop rows that exhaust their retry budget."""
    if not seqs:
        return
    placeholders = ", ".join("%s" for _ in seqs)
    with mutation() as cur:
        cur.execute(
            "UPDATE conversation_embedding_queue"
            " SET attempts = attempts + 1, last_attempt_at = clock_timestamp()"
            f" WHERE event_seq IN ({placeholders})",
            tuple(seqs),
        )
        cur.execute(
            "DELETE FROM conversation_embedding_queue WHERE attempts >= %s",
            (CONVERSATION_EMBEDDING_MAX_ATTEMPTS,),
        )


def store_thread_message_embeddings(
    model: str,
    rows: list[tuple[int, list[float]]],
) -> None:
    """Upsert one inference batch without keeping inference inside a DB transaction."""
    if not rows:
        return
    event_seqs = [seq for seq, _embedding in rows]
    placeholders = ", ".join("%s" for _ in event_seqs)
    with mutation() as cur:
        # The singleton-row update serializes batches. A search snapshot sees
        # only a committed generation, so a batch that commits later cannot
        # enter an in-progress fused result set.
        cur.execute(
            "INSERT INTO conversation_search_state"
            " (singleton, embedding_generation) VALUES (TRUE, 1)"
            " ON CONFLICT (singleton) DO UPDATE SET embedding_generation ="
            " conversation_search_state.embedding_generation + 1"
            " RETURNING embedding_generation"
        )
        generation_row = cur.fetchone()
        if generation_row is None:
            raise RuntimeError("conversation embedding generation was not returned")
        embedding_generation = int(generation_row[0])
        for seq, embedding in rows:
            literal = "[" + ",".join(format(value, ".9g") for value in embedding) + "]"
            cur.execute(
                "INSERT INTO conversation_message_embeddings"
                " (event_seq, model, embedding, embedding_generation)"
                " SELECT seq, %s, %s::vector, %s FROM agent_events"
                " WHERE seq = %s AND event_type = 'thread.message'"
                " AND message IS NOT NULL"
                " ON CONFLICT (event_seq) DO UPDATE SET"
                " model = EXCLUDED.model, embedding = EXCLUDED.embedding,"
                " embedding_generation = EXCLUDED.embedding_generation,"
                " embedded_at = clock_timestamp()",
                (model, literal, embedding_generation, seq),
            )
        # Drain what was just stored. A row whose event vanished mid-batch
        # inserts nothing above, and dropping it here keeps the queue from
        # retrying work that no longer has a source message.
        cur.execute(
            "DELETE FROM conversation_embedding_queue"
            f" WHERE event_seq IN ({placeholders})",
            tuple(event_seqs),
        )
        prune_conversation_embeddings(cur, embedding_generation)


def prune_conversation_embeddings(
    cur: Any,
    embedding_generation: int | None = None,
) -> int:
    """Keep vectors/queued work for only the newest source-message quota."""
    if (
        embedding_generation is not None
        and embedding_generation % CONVERSATION_EMBEDDING_PRUNE_EVERY_BATCHES != 0
    ):
        return 0
    # OFFSET is backed by agent_events_message_seq_idx. The selected row is the
    # first message outside the retained newest-N set; NULL means the source has
    # not reached the quota yet.
    floor_sql = (
        "SELECT seq FROM agent_events"
        " WHERE event_type = 'thread.message' AND message IS NOT NULL"
        " ORDER BY seq DESC OFFSET %s LIMIT 1"
    )
    cur.execute(
        "DELETE FROM conversation_message_embeddings WHERE event_seq <= ("
        + floor_sql
        + ")"
        " RETURNING event_seq",
        (CONVERSATION_EMBEDDING_MESSAGE_LIMIT,),
    )
    pruned = len(cur.fetchall())
    # Drop queued work outside the same source-message quota. Without this, a
    # long inference outage could leave obsolete backlog rows queued forever.
    cur.execute(
        "DELETE FROM conversation_embedding_queue WHERE event_seq <= ("
        + floor_sql
        + ")",
        (CONVERSATION_EMBEDDING_MESSAGE_LIMIT,),
    )
    return pruned


def thread_messages_by_seqs(
    seqs: tuple[int, ...],
    *,
    from_timestamp: str | None,
    to_timestamp: str | None,
    thread_id: str | None,
    sources: tuple[str, ...],
    max_seq: int,
) -> list[dict[str, Any]]:
    """Fetch filtered source rows in a previously frozen semantic order."""
    if not seqs or not sources:
        return []
    placeholders = ", ".join("%s" for _ in seqs)
    source_placeholders = ", ".join("%s" for _ in sources)
    clauses = [
        "event_type = 'thread.message'",
        "message IS NOT NULL",
        f"seq IN ({placeholders})",
        "seq <= %s",
        f"source IN ({source_placeholders})",
    ]
    params: list[Any] = [*seqs, max_seq, *sources]
    if thread_id is not None:
        clauses.append("thread_id = %s")
        params.append(thread_id)
    if from_timestamp is not None:
        clauses.append("created_at >= %s")
        params.append(from_timestamp)
    if to_timestamp is not None:
        clauses.append("created_at < %s")
        params.append(to_timestamp)
    with db.transaction() as cur:
        cur.execute(
            "SELECT seq, created_at, thread_id, source,"
            " LEFT(message, 4096), message <> LEFT(message, 4096)"
            f" FROM agent_events WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        rows = cur.fetchall()
    by_seq = {
        int(seq): {
            "seq": int(seq),
            "event_id": f"event_{seq}",
            "timestamp": str(created_at),
            "thread_id": str(thread_id),
            "source": str(source),
            "search_rank": 0.0,
            "excerpt": str(excerpt),
            "excerpt_truncated": bool(excerpt_truncated),
        }
        for seq, created_at, thread_id, source, excerpt, excerpt_truncated in rows
    }
    return [by_seq[seq] for seq in seqs if seq in by_seq]


def search_thread_messages_semantic(
    embedding: list[float],
    model: str,
    *,
    from_timestamp: str | None,
    to_timestamp: str | None,
    thread_id: str | None,
    sources: tuple[str, ...],
    limit: int,
    minimum_similarity: float,
    max_seq: int | None = None,
    max_embedding_generation: int | None = None,
) -> list[dict[str, Any]]:
    """Nearest indexed message vectors under the same filters as text search."""
    clauses = ["embeddings.model = %s"]
    params: list[Any] = [model]
    if max_seq is not None:
        clauses.append("events.seq <= %s")
        params.append(max_seq)
    if max_embedding_generation is not None:
        clauses.append("embeddings.embedding_generation <= %s")
        params.append(max_embedding_generation)
    if thread_id is not None:
        clauses.append("events.thread_id = %s")
        params.append(thread_id)
    if from_timestamp is not None:
        clauses.append("events.created_at >= %s")
        params.append(from_timestamp)
    if to_timestamp is not None:
        clauses.append("events.created_at < %s")
        params.append(to_timestamp)
    if not sources:
        return []
    placeholders = ", ".join(["%s"] * len(sources))
    clauses.append(f"events.source IN ({placeholders})")
    params.extend(sources)
    literal = "[" + ",".join(format(value, ".9g") for value in embedding) + "]"
    sql = (
        "WITH query_embedding AS (SELECT %s::vector AS value),"
        " nearest AS MATERIALIZED ("
        " SELECT events.seq, events.created_at, events.thread_id, events.source,"
        " 1 - (embeddings.embedding <=> query_embedding.value) AS similarity,"
        " LEFT(events.message, 4096) AS excerpt,"
        " events.message <> LEFT(events.message, 4096) AS excerpt_truncated"
        " FROM conversation_message_embeddings AS embeddings"
        " JOIN agent_events AS events ON events.seq = embeddings.event_seq"
        " CROSS JOIN query_embedding"
        f" WHERE {' AND '.join(clauses)}"
        " ORDER BY embeddings.embedding <=> query_embedding.value, events.seq DESC"
        " LIMIT %s"
        ") SELECT seq, created_at, thread_id, source, similarity, excerpt, excerpt_truncated"
        " FROM nearest WHERE similarity >= %s"
        " ORDER BY similarity DESC, seq DESC"
    )
    with db.transaction() as cur:
        cur.execute("SET LOCAL hnsw.ef_search = 100")
        cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        cur.execute(sql, (literal, *params, limit, minimum_similarity))
        rows = cur.fetchall()
    return [
        {
            "seq": int(seq),
            "event_id": f"event_{seq}",
            "timestamp": str(created_at),
            "thread_id": str(result_thread_id),
            "source": str(source),
            "search_rank": float(similarity),
            "excerpt": str(excerpt),
            "excerpt_truncated": bool(excerpt_truncated),
        }
        for (
            seq,
            created_at,
            result_thread_id,
            source,
            similarity,
            excerpt,
            excerpt_truncated,
        ) in rows
    ]


def recent_thread_handoff_events(
    cur: Any,
    thread_id: str,
    *,
    message_character_limit: int,
    activity_character_limit: int,
    activity_event_character_limit: int,
    after_seq: int = 0,
) -> list[dict[str, Any]]:
    """Newest retained events selected under independent handoff budgets.

    Messages and activities have separate newest-first windows, so tool output
    cannot consume the conversation budget. Activity uses the same per-event
    bound as the serialized prompt. Results are merged back into chronological
    order without transferring the full event history. ``after_seq`` is the
    thread's cleared-working-memory floor: events at or below it are excluded
    before the budgets are measured, so a cleared thread hands over nothing.
    """
    if message_character_limit <= 0 and activity_character_limit <= 0:
        return []
    cur.execute(
        f"SELECT {_EVENT_FIELDS} FROM ("
        f" SELECT {_EVENT_FIELDS},"
        " COALESCE(SUM(CASE"
        "   WHEN event_type = 'thread.message' THEN char_length(message)"
        "   ELSE 0"
        " END) OVER ("
        "   ORDER BY seq DESC ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING"
        " ), 0) AS newer_message_characters,"
        " COALESCE(SUM(CASE"
        "   WHEN event_type = 'thread.activity'"
        "     THEN LEAST(char_length(activity::text), %s)"
        "   ELSE 0"
        " END) OVER ("
        "   ORDER BY seq DESC ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING"
        " ), 0) AS newer_activity_characters"
        " FROM agent_events"
        " WHERE thread_id = %s"
        " AND seq > %s"
        " AND event_type IN ('thread.message', 'thread.activity')"
        ") AS history"
        " WHERE (event_type = 'thread.activity' AND newer_activity_characters < %s)"
        " OR (event_type = 'thread.message' AND newer_message_characters < %s)"
        " ORDER BY seq",
        (
            max(1, activity_event_character_limit),
            thread_id,
            max(0, after_seq),
            max(0, activity_character_limit),
            max(0, message_character_limit),
        ),
    )
    return [_event_dict(row) for row in cur.fetchall()]
