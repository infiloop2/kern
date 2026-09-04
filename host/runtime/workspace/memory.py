"""Host-global, revisioned Workspace memory pages."""

from __future__ import annotations

import base64
import binascii
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http import HTTPStatus
import json
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import unquote

from host.runtime.core import db, host_errors, pgclient
from host.runtime.embeddings import client as embedding_client
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.query import one as _one


PAGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
INDIVIDUAL_PAGE_ID_RE = re.compile(r"^(?:app|thread|schedule)-")
LINK_RE = re.compile(r"\[\[([a-z0-9][a-z0-9-]{0,63})\]\]")
MAX_DESCRIPTION_CHARS = 100
MAX_CONTENT_CHARS = 2000
MAX_PAGES = 10_000
# Kept in step with the cap in migration 0042's memory_page_links backfill.
MAX_PAGE_LINKS = 100
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 100
MAX_REVISION_PAGE_LIMIT = 50
MAX_SEARCH_BYTES = 200
MAX_RECALLED_PAGES = 5
MAX_CURSOR_BYTES = 512
SEMANTIC_CANDIDATES = 200
EXACT_CANDIDATES = 50
GRAPH_SEEDS = 5
GRAPH_NEIGHBORS_PER_SEED = 10
REVALIDATION_PASSES = 3
# Page writes wake the indexer directly; this only backstops work that reached
# the table without signalling, such as a restart with pages still unembedded.
# Polling unconditionally instead would re-run an anti-join over every page in
# the quota -- now 10,000 -- forever, to find nothing.
EMBEDDING_IDLE_SECONDS = 30
_embedding_work = threading.Event()
# Hybrid result sets are recomputed on each page.  Bind their cursors to a
# process-local generation so a page/link/vector mutation invalidates the
# cursor instead of applying its old positional offset to a newly ranked set.
# The random key also invalidates cursors across a service restart and prevents
# callers from editing the generation or query binding.
_search_cursor_key = secrets.token_bytes(32)
_search_generation_lock = threading.Lock()
_search_generation = 0
WEAK_SEARCH_LIMIT = 5
FALLBACK_POPULAR_LIMIT = 5
# Weak fallback deliberately ORs query tokens to recover a page when an exact
# all-token lookup misses. PostgreSQL's ``simple`` text-search configuration
# removes no stopwords, so filter them here; otherwise an unrelated page can be
# presented as a query match solely because both texts contain "a" or "the".
WEAK_SEARCH_STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "could",
    "did", "do", "does", "for", "from", "had", "has", "have", "he", "her",
    "his", "how", "i", "if", "in", "into", "is", "it", "its", "may", "me",
    "might", "must", "my", "not", "of", "on", "or", "our", "ours", "shall",
    "she", "should", "so", "that", "the", "their", "them", "then", "they",
    "this", "to", "us", "was", "we", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "your",
})
REVISION_RETAINED = 100
DELETED_RETAIN_DAYS = 90
MAX_BIGINT = 2**63 - 1
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def route_browser(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]],
) -> dict[str, Any]:
    if path == "/memory" and method == "GET":
        return list_pages(query)
    if path == "/memory/search" and method == "GET":
        return search_pages(query)
    if path == "/memory/recall" and method == "POST":
        if query:
            raise WorkspaceError(
                HTTPStatus.BAD_REQUEST,
                "memory recall does not accept query parameters",
            )
        return recall_pages(body)
    match = re.fullmatch(r"/memory/pages/([^/]+)", path)
    if match:
        page_id = _page_id(match.group(1))
        if method == "GET":
            return {"page": load_page(page_id, include_deleted=True)}
        if method == "PUT":
            return {"page": save_page(page_id, body, actor="user")}
        if method == "DELETE":
            return delete_page(page_id, query, actor="user")
    match = re.fullmatch(r"/memory/pages/([^/]+)/revisions", path)
    if match and method == "GET":
        return list_revisions(_page_id(match.group(1)), query)
    match = re.fullmatch(r"/memory/pages/([^/]+)/revisions/([1-9][0-9]*)/restore", path)
    if match and method == "POST":
        return {
            "page": restore_revision(
                _page_id(match.group(1)), int(match.group(2)), body
            )
        }
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory route not found")


def route_agent(
    method: str,
    path: str,
    body: Any,
    query: dict[str, list[str]],
) -> dict[str, Any]:
    if path == "/agent/memory" and method == "GET":
        return list_swarm_pages(query)
    if path == "/agent/memory/search" and method == "GET":
        return search_swarm_pages(query)
    match = re.fullmatch(r"/agent/memory/pages/([^/]+)", path)
    if match:
        page_id = _page_id(match.group(1))
        _require_swarm_page(page_id)
        if method == "GET":
            return {"page": load_page(page_id)}
        if method == "PUT":
            return {"page": save_page(page_id, body, actor="agent")}
        if method == "DELETE":
            return delete_page(page_id, query, actor="agent")
    raise WorkspaceError(HTTPStatus.NOT_FOUND, "agent memory route not found")


def list_pages(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(
        query,
        {"cursor", "limit", "deleted", "scope"},
        "memory index",
    )
    return _list_pages(
        query,
        deleted=_boolean_query(query, "deleted", default=False),
        scope=_memory_scope(query),
    )


def list_swarm_pages(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"cursor", "limit"}, "memory index")
    return _list_pages(query, deleted=False, scope="swarm")


def _list_pages(
    query: dict[str, list[str]],
    *,
    deleted: bool,
    scope: str,
) -> dict[str, Any]:
    limit = _limit(query)
    after = _decode_cursor(_one(query, "cursor"))
    clause = "deleted_at IS NOT NULL" if deleted else "deleted_at IS NULL"
    clause += _scope_clause(scope)
    params: list[Any] = []
    if after is not None:
        clause += " AND page_id > %s"
        params.append(after)
    with db.transaction() as cur:
        cur.execute(
            "SELECT page_id, description, content, revision, deleted_at,"
            " updated_by, created_at, updated_at FROM memory_pages"
            f" WHERE {clause} ORDER BY page_id LIMIT %s",
            (*params, limit + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    response: dict[str, Any] = {"pages": [_page_summary(row) for row in rows]}
    if more and rows:
        response["next_cursor"] = _encode_cursor(str(rows[-1][0]))
    return response


def search_pages(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(
        query,
        {"q", "cursor", "limit", "scope"},
        "memory search",
    )
    return _search_pages(
        query,
        scope=_memory_scope(query),
        record_top_hit=False,
        semantic=False,
    )


def search_swarm_pages(query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"q", "cursor", "limit"}, "memory search")
    return _search_pages(
        query,
        scope="swarm",
        record_top_hit=True,
        semantic=True,
    )


def recall_pages(body: Any) -> dict[str, Any]:
    """Return bounded turn-start context without weak or popular fallbacks."""
    request = _object(body, "memory recall request")
    _require_keys(request, {"thread_id", "message"}, {"thread_id", "message"})
    thread_id = individual_page_id(request["thread_id"])
    message = request["message"]
    if not isinstance(message, str):
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "message must be a string",
        )

    pages: list[dict[str, Any]] = []
    try:
        pages.append({**load_page(thread_id), "scope": "self"})
    except WorkspaceError as exc:
        if exc.status != HTTPStatus.NOT_FOUND:
            raise

    if "\x00" in message:
        host_errors.report_warning(
            "workspace.memory_recall",
            "swarm recall skipped for a NUL-containing query",
            context={"thread_id": thread_id, "phase": "query"},
            kind="memory_recall_degraded",
        )
        return {"pages": pages}

    remaining = MAX_RECALLED_PAGES - len(pages)
    query = _recall_query(message)
    if not query:
        return {"pages": pages}
    try:
        matches = _search_pages(
            {"q": [query], "limit": [str(remaining)]},
            scope="swarm",
            record_top_hit=False,
            semantic=True,
        )
    except (WorkspaceError, pgclient.Error, OSError) as exc:
        host_errors.report_warning(
            "workspace.memory_recall",
            exc,
            context={"thread_id": thread_id, "phase": "swarm_search"},
            kind="memory_recall_degraded",
        )
        return {"pages": pages}
    # Weak token overlap and popularity are useful discovery fallbacks for an
    # agent that can judge them. Automatic context keeps a higher bar.
    summaries = [] if matches.get("match_mode") == "weak" else matches.get("pages", [])
    for summary in summaries:
        if not isinstance(summary, dict) or not isinstance(summary.get("page_id"), str):
            continue
        try:
            page = load_page(summary["page_id"])
        except (WorkspaceError, pgclient.Error, OSError) as exc:
            host_errors.report_warning(
                "workspace.memory_recall",
                exc,
                context={"thread_id": thread_id, "phase": "swarm_load"},
                kind="memory_recall_degraded",
            )
            return {"pages": pages}
        if page.get("revision") != summary.get("revision"):
            host_errors.report_warning(
                "workspace.memory_recall",
                "swarm page changed after recall ranking",
                context={"thread_id": thread_id, "phase": "swarm_load"},
                kind="memory_recall_degraded",
            )
            continue
        pages.append({**page, "scope": "swarm"})
    return {"pages": pages}


def _recall_query(message: str) -> str:
    encoded = message.strip().encode("utf-8", errors="ignore")
    if len(encoded) <= MAX_SEARCH_BYTES:
        return message.strip()
    return encoded[:MAX_SEARCH_BYTES].decode("utf-8", errors="ignore").strip()


def _search_pages(
    query: dict[str, list[str]],
    *,
    scope: str,
    record_top_hit: bool,
    semantic: bool,
) -> dict[str, Any]:
    needle = _one(query, "q")
    try:
        needle_bytes = needle.encode() if needle is not None else b""
    except UnicodeEncodeError as exc:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "q must be valid UTF-8") from exc
    if (
        not needle
        or not needle.strip()
        or "\x00" in needle
        or len(needle_bytes) > MAX_SEARCH_BYTES
    ):
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"q must be non-empty, contain no NUL, and be at most {MAX_SEARCH_BYTES} bytes",
        )
    limit = _limit(query, default=20)
    if not semantic:
        offset = _decode_offset_cursor(_one(query, "cursor"))
        rows = _search_pages_lexical(
            needle,
            limit + 1,
            offset,
            scope=scope,
        )
        return _memory_search_response(
            rows,
            limit,
            offset,
            needle=needle,
            scope=scope,
            record_top_hit=record_top_hit,
        )

    fingerprint = _memory_search_fingerprint(needle, scope)
    cursor_mode, offset, cursor_generation = _decode_semantic_offset_cursor(
        _one(query, "cursor"), fingerprint
    )
    current_generation = _memory_search_generation()
    if cursor_generation is None:
        cursor_generation = current_generation
    elif cursor_generation != current_generation:
        raise WorkspaceError(
            HTTPStatus.CONFLICT,
            "memory search changed; restart pagination",
        )
    # Lexical candidates have to reach the requested page: capping them at a
    # fixed window would leave every lower-ranked match unreachable, which the
    # plain lexical path never did. Semantic and graph stay bounded because
    # their contribution is a head effect, so depth only extends lexical order.
    exact_rows = _search_pages_exact(
        needle,
        EXACT_CANDIDATES,
        scope=scope,
    )
    # Fusion only ever sees a fixed lexical window, so the ranked prefix does
    # not depend on how deep the requested page reaches.
    fused_lexical = _search_pages_lexical(
        needle,
        SEMANTIC_CANDIDATES,
        0,
        scope=scope,
    )
    # Rows below that window are appended in lexical order instead: scoring them
    # would hand an already-ranked candidate an extra RRF contribution and could
    # move it into the prefix a caller has consumed, dropping it from the
    # results entirely. Only their identity and order matter, because the
    # revalidation pass below re-reads whichever rows are actually returned.
    # Selecting them as full rows would pull up to MAX_PAGES pages of content
    # into this process -- tens of megabytes for a deep cursor -- to use one
    # column of each.
    # Fetch enough ids for every bounded revalidation pass plus one continuation
    # sentinel. If a whole requested slice is deleted between ranking and the
    # final read, a one-row overfetch cannot refill the page or prove that later
    # lexical matches still exist.
    deeper_depth = min(
        max(
            0,
            offset + limit * REVALIDATION_PASSES + 1 - SEMANTIC_CANDIDATES,
        ),
        MAX_PAGES,
    )
    deeper_lexical = _lexical_page_id_tail(
        needle,
        deeper_depth,
        SEMANTIC_CANDIDATES,
        scope=scope,
    )
    search_mode = "hybrid"
    continuation_mode = "hybrid"
    if cursor_mode == "fallback":
        semantic_rows = []
        search_mode = "lexical_fallback"
        continuation_mode = "fallback"
    else:
        try:
            query_embedding = embedding_client.embed_texts([needle], kind="query")[0]
            semantic_rows = _search_pages_semantic(
                query_embedding,
                embedding_client.MODEL_NAME,
                SEMANTIC_CANDIDATES,
                embedding_client.MINIMUM_SIMILARITY,
                scope=scope,
            )
        except (embedding_client.EmbeddingError, pgclient.Error, OSError) as exc:
            if not isinstance(exc, embedding_client.EmbeddingError):
                host_errors.report_unexpected("workspace.memory_embedding_search", exc)
            if cursor_mode == "hybrid":
                raise WorkspaceError(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "semantic memory pagination is temporarily unavailable; retry",
                ) from exc
            semantic_rows = []
            search_mode = "lexical_fallback"
            continuation_mode = "fallback"

    initial = _hybrid_memory_rows(exact_rows, fused_lexical, semantic_rows, [])
    graph_rows = _search_pages_graph(
        [str(row[0]) for row in initial[:GRAPH_SEEDS]],
        scope=scope,
        limit=GRAPH_SEEDS * GRAPH_NEIGHBORS_PER_SEED,
    )
    fused = _hybrid_memory_rows(
        exact_rows,
        fused_lexical,
        semantic_rows,
        graph_rows,
    )
    # Candidates are carried as page ids from here on: the revalidation pass
    # re-reads whatever is actually returned, so the fused tuples are only
    # needed for their ranking.
    ranked = [str(row[0]) for row in fused]
    seen = set(ranked)
    ranked.extend(page_id for page_id in deeper_lexical if page_id not in seen)
    # The candidate queries each ran in their own transaction, so a page
    # updated or soft-deleted between them would otherwise be reported from a
    # stale tuple. Re-read the ones actually being returned, and pull the next
    # candidates in when some have gone, so a concurrent delete shortens the
    # page rather than emptying it. Bounded passes keep a mass deletion from
    # walking the whole candidate list one query at a time.
    page: list[tuple[Any, ...]] = []
    position = offset
    for _ in range(REVALIDATION_PASSES):
        if position >= len(ranked) or len(page) >= limit:
            break
        window = ranked[position : position + (limit - len(page))]
        page.extend(_current_page_rows(window, scope=scope))
        position += len(window)
    if _memory_search_generation() != cursor_generation:
        raise WorkspaceError(
            HTTPStatus.CONFLICT,
            "memory search changed; restart pagination",
        )
    if not ranked and offset == 0:
        fallback_response = _memory_search_fallback(needle, scope=scope)
        fallback_response["search_mode"] = search_mode
        return fallback_response
    if page and offset == 0 and record_top_hit:
        _record_memory_top_hit(str(page[0][0]))
    response: dict[str, Any] = {
        "pages": [_page_summary(row) for row in page],
        "search_mode": search_mode,
    }
    if position < len(ranked):
        response["next_cursor"] = _encode_semantic_offset_cursor(
            position,
            continuation_mode,
            fingerprint,
            cursor_generation,
        )
    return response


def _current_page_rows(page_ids: list[str], *, scope: str) -> list[tuple[Any, ...]]:
    """Re-read selected pages so a response never carries a stale or deleted row."""
    if not page_ids:
        return []
    placeholders = ",".join(["%s"] * len(page_ids))
    with db.transaction() as cur:
        cur.execute(
            "SELECT page_id, description, content, revision, deleted_at,"
            " updated_by, created_at, updated_at FROM memory_pages"
            f" WHERE page_id IN ({placeholders}) AND deleted_at IS NULL"
            + _scope_clause(scope),
            tuple(page_ids),
        )
        current = {str(row[0]): row for row in cur.fetchall()}
    return [current[page_id] for page_id in page_ids if page_id in current]


def _search_pages_exact(
    needle: str,
    limit: int,
    *,
    scope: str,
) -> list[tuple[Any, ...]]:
    normalized = needle.strip().casefold()
    with db.transaction() as cur:
        cur.execute(
            "SELECT page_id, description, content, revision, deleted_at,"
            " updated_by, created_at, updated_at,"
            " CASE WHEN page_id = %s THEN 3"
            " WHEN lower(description) = %s THEN 2 ELSE 1 END AS exact_rank"
            " FROM memory_pages WHERE deleted_at IS NULL"
            + _scope_clause(scope)
            + " AND (page_id = %s OR lower(description) = %s"
            " OR (%s AND strpos(lower(description), %s) > 0))"
            " ORDER BY exact_rank DESC, page_id LIMIT %s",
            (
                normalized,
                normalized,
                normalized,
                normalized,
                len(normalized) >= 4,
                normalized,
                limit,
            ),
        )
        return cur.fetchall()


def _search_pages_lexical(
    needle: str,
    limit: int,
    offset: int,
    *,
    scope: str,
) -> list[tuple[Any, ...]]:
    normalized = needle.strip().casefold()
    with db.transaction() as cur:
        cur.execute(
            "SELECT page_id, description, content, revision, deleted_at,"
            " updated_by, created_at, updated_at,"
            " ts_rank(to_tsvector('simple', page_id || ' ' || description || ' ' || content),"
            " websearch_to_tsquery('simple', %s)) AS rank"
            " FROM memory_pages WHERE deleted_at IS NULL"
            + _scope_clause(scope)
            + " AND (to_tsvector('simple', page_id || ' ' || description || ' ' || content)"
            " @@ websearch_to_tsquery('simple', %s)"
            # A description substring is not a full-text token, so it joins the
            # paginated channel here. Left only to the bounded exact booster, a
            # query like "auth" matching "OAuth" would stop at that channel's
            # limit and strand every further page, with no cursor to reach them.
            " OR (%s AND strpos(lower(description), %s) > 0))"
            " ORDER BY rank DESC, page_id LIMIT %s OFFSET %s",
            (needle, needle, len(normalized) >= 4, normalized, limit, offset),
        )
        return cur.fetchall()


def _lexical_page_id_tail(
    needle: str,
    limit: int,
    offset: int,
    *,
    scope: str,
) -> list[str]:
    """Page ids below the fusion window, in the same order as the full query.

    Identical predicate and ordering to ``_search_pages_lexical`` -- only the
    projection differs, because callers past the fusion window need ordering
    rather than row contents.
    """
    if limit <= 0:
        return []
    normalized = needle.strip().casefold()
    with db.transaction() as cur:
        cur.execute(
            "SELECT page_id FROM memory_pages WHERE deleted_at IS NULL"
            + _scope_clause(scope)
            + " AND (to_tsvector('simple', page_id || ' ' || description || ' ' || content)"
            " @@ websearch_to_tsquery('simple', %s)"
            " OR (%s AND strpos(lower(description), %s) > 0))"
            " ORDER BY ts_rank(to_tsvector('simple',"
            " page_id || ' ' || description || ' ' || content),"
            " websearch_to_tsquery('simple', %s)) DESC, page_id"
            " LIMIT %s OFFSET %s",
            (needle, len(normalized) >= 4, normalized, needle, limit, offset),
        )
        return [str(row[0]) for row in cur.fetchall()]


def _search_pages_semantic(
    embedding: list[float],
    model: str,
    limit: int,
    minimum_similarity: float,
    *,
    scope: str,
) -> list[tuple[Any, ...]]:
    literal = "[" + ",".join(format(value, ".9g") for value in embedding) + "]"
    sql = (
        "WITH query_embedding AS (SELECT %s::vector AS value),"
        " nearest AS MATERIALIZED ("
        " SELECT pages.page_id, pages.description, pages.content, pages.revision,"
        " pages.deleted_at, pages.updated_by, pages.created_at, pages.updated_at,"
        " 1 - (embeddings.embedding <=> query_embedding.value) AS similarity"
        " FROM memory_page_embeddings AS embeddings"
        " JOIN memory_pages AS pages ON pages.page_id = embeddings.page_id"
        " AND pages.revision = embeddings.revision"
        " CROSS JOIN query_embedding"
        " WHERE pages.deleted_at IS NULL AND embeddings.model = %s"
        + _scope_clause(scope, column="pages.page_id")
        + " ORDER BY embeddings.embedding <=> query_embedding.value, pages.page_id"
        " LIMIT %s"
        ") SELECT page_id, description, content, revision, deleted_at, updated_by,"
        " created_at, updated_at, similarity FROM nearest WHERE similarity >= %s"
        " ORDER BY similarity DESC, page_id"
    )
    with db.transaction() as cur:
        cur.execute("SET LOCAL hnsw.ef_search = 100")
        cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        cur.execute(sql, (literal, model, limit, minimum_similarity))
        return cur.fetchall()


def _search_pages_graph(
    seed_page_ids: list[str],
    *,
    scope: str,
    limit: int,
) -> list[tuple[Any, ...]]:
    if not seed_page_ids or limit <= 0:
        return []
    values = ",".join(["(%s, %s)"] * len(seed_page_ids))
    exclusions = ",".join(["%s"] * len(seed_page_ids))
    seed_params: list[Any] = []
    for rank, page_id in enumerate(seed_page_ids, start=1):
        seed_params.extend((page_id, rank))
    sql = (
        f"WITH seeds(page_id, seed_rank) AS (VALUES {values}),"
        " edges AS ("
        " SELECT seeds.page_id AS seed_id, seeds.seed_rank, links.target_page_id AS page_id"
        " FROM seeds JOIN memory_page_links AS links"
        " ON links.source_page_id = seeds.page_id"
        # UNION, not UNION ALL: two pages that link to each other appear in
        # both arms, and numbering each copy would spend two of the seed's
        # ten neighbour slots on one page.
        " UNION"
        " SELECT seeds.page_id AS seed_id, seeds.seed_rank, links.source_page_id AS page_id"
        " FROM seeds JOIN memory_page_links AS links"
        " ON links.target_page_id = seeds.page_id),"
        " bounded AS ("
        " SELECT page_id, seed_rank, row_number() OVER ("
        " PARTITION BY seed_id ORDER BY page_id) AS edge_rank FROM edges),"
        " candidates AS ("
        " SELECT page_id, MIN(seed_rank) AS graph_rank FROM bounded"
        f" WHERE edge_rank <= %s AND page_id NOT IN ({exclusions}) GROUP BY page_id)"
        " SELECT pages.page_id, pages.description, pages.content, pages.revision,"
        " pages.deleted_at, pages.updated_by, pages.created_at, pages.updated_at,"
        " candidates.graph_rank FROM candidates"
        " JOIN memory_pages AS pages ON pages.page_id = candidates.page_id"
        " WHERE pages.deleted_at IS NULL"
        + _scope_clause(scope, column="pages.page_id")
        + " ORDER BY candidates.graph_rank, pages.page_id LIMIT %s"
    )
    with db.transaction() as cur:
        cur.execute(
            sql,
            (*seed_params, GRAPH_NEIGHBORS_PER_SEED, *seed_page_ids, limit),
        )
        return cur.fetchall()


def _hybrid_memory_rows(
    exact_rows: list[tuple[Any, ...]],
    lexical_rows: list[tuple[Any, ...]],
    semantic_rows: list[tuple[Any, ...]],
    graph_rows: list[tuple[Any, ...]],
) -> list[tuple[Any, ...]]:
    by_page: dict[str, tuple[Any, ...]] = {}
    scores: dict[str, float] = {}
    for rank, row in enumerate(exact_rows, start=1):
        page_id = str(row[0])
        by_page[page_id] = row
        scores[page_id] = scores.get(page_id, 0.0) + 4.0 / (60 + rank)
    for rank, row in enumerate(lexical_rows, start=1):
        page_id = str(row[0])
        by_page[page_id] = row
        scores[page_id] = scores.get(page_id, 0.0) + 2.0 / (60 + rank)
    for rank, row in enumerate(semantic_rows, start=1):
        page_id = str(row[0])
        by_page.setdefault(page_id, row)
        scores[page_id] = scores.get(page_id, 0.0) + 1.0 / (60 + rank)
    for rank, row in enumerate(graph_rows, start=1):
        page_id = str(row[0])
        by_page.setdefault(page_id, row)
        scores[page_id] = scores.get(page_id, 0.0) + 0.5 / (60 + rank)
    return sorted(
        by_page.values(),
        key=lambda row: (-scores[str(row[0])], str(row[0])),
    )


def _memory_search_response(
    rows: list[tuple[Any, ...]],
    limit: int,
    offset: int,
    *,
    needle: str,
    scope: str,
    record_top_hit: bool,
    search_mode: str | None = None,
) -> dict[str, Any]:
    if rows and offset == 0 and record_top_hit:
        _record_memory_top_hit(str(rows[0][0]))
    if not rows and offset == 0:
        fallback_response = _memory_search_fallback(needle, scope=scope)
        if search_mode is not None:
            fallback_response["search_mode"] = search_mode
        return fallback_response
    more = len(rows) > limit
    rows = rows[:limit]
    response: dict[str, Any] = {"pages": [_page_summary(row[:8]) for row in rows]}
    if search_mode is not None:
        response["search_mode"] = search_mode
    if more:
        response["next_cursor"] = _encode_offset_cursor(offset + limit)
    return response


def _record_memory_top_hit(page_id: str) -> None:
    with db.transaction() as cur:
        cur.execute(
            "UPDATE memory_pages SET strong_top_hit_count = CASE"
            " WHEN strong_top_hit_count < %s THEN strong_top_hit_count + 1"
            " ELSE strong_top_hit_count END, last_strong_top_hit_at = %s"
            " WHERE page_id = %s",
            (MAX_BIGINT, _utc_now(), page_id),
        )


def _memory_search_fallback(needle: str, *, scope: str) -> dict[str, Any]:
    with db.transaction() as cur:
        weak_rows = _weak_search_rows(cur, needle, scope=scope)
        weak_ids = {str(row[0]) for row in weak_rows}
        popular_rows = [
            row
            for row in _popular_rows(
                cur,
                scope=scope,
                limit=FALLBACK_POPULAR_LIMIT + WEAK_SEARCH_LIMIT,
            )
            if str(row[0]) not in weak_ids
        ][:FALLBACK_POPULAR_LIMIT]
    return {
        "pages": [_page_summary(row[:8]) for row in weak_rows],
        "match_mode": "weak",
        "popular_pages": [_page_summary(row[:8]) for row in popular_rows],
    }


def _weak_search_rows(cur: Any, needle: str, *, scope: str) -> list[tuple[Any, ...]]:
    tokens = _weak_search_tokens(needle)
    if not tokens:
        return []
    # Quote every lexeme so preserved acronyms such as ``OR`` are searchable
    # terms rather than websearch_to_tsquery Boolean operators. Tokens contain
    # only Unicode letters and digits, so no quote escaping is needed here.
    weak_needle = " OR ".join(f'"{token}"' for token in tokens)
    cur.execute(
        "SELECT page_id, description, content, revision, deleted_at,"
        " updated_by, created_at, updated_at,"
        " ts_rank(to_tsvector('simple', page_id || ' ' || description || ' ' || content),"
        " websearch_to_tsquery('simple', %s)) AS rank"
        " FROM memory_pages WHERE deleted_at IS NULL"
        f"{_scope_clause(scope)}"
        " AND to_tsvector('simple', page_id || ' ' || description || ' ' || content)"
        " @@ websearch_to_tsquery('simple', %s)"
        " ORDER BY rank DESC, page_id LIMIT %s",
        (weak_needle, weak_needle, WEAK_SEARCH_LIMIT),
    )
    return cur.fetchall()


def _weak_search_tokens(needle: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for raw_token in re.findall(r"[^\W_]+", needle):
        token = raw_token.casefold()
        # Multi-letter all-caps spelling is a useful acronym signal: ``IT``
        # support and the ``US`` region must remain searchable even though the
        # same lowercase words are ordinary function words.
        is_acronym = len(raw_token) > 1 and raw_token.isupper()
        if token in seen or (token in WEAK_SEARCH_STOPWORDS and not is_acronym):
            continue
        tokens.append(token)
        seen.add(token)
    return tokens


def _popular_rows(cur: Any, *, scope: str, limit: int) -> list[tuple[Any, ...]]:
    if limit == 0:
        return []
    cur.execute(
        "SELECT page_id, description, content, revision, deleted_at,"
        " updated_by, created_at, updated_at FROM memory_pages"
        " WHERE deleted_at IS NULL AND strong_top_hit_count > 0"
        f"{_scope_clause(scope)}"
        " ORDER BY strong_top_hit_count DESC,"
        " last_strong_top_hit_at DESC NULLS LAST, updated_at DESC, page_id LIMIT %s",
        (limit,),
    )
    return cur.fetchall()


def load_page(
    page_id: str,
    *,
    include_deleted: bool = False,
) -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute(
            "SELECT page_id, description, content, revision, deleted_at,"
            " updated_by, created_at, updated_at FROM memory_pages WHERE page_id = %s",
            (page_id,),
        )
        row = cur.fetchone()
        if (
            row is not None
            and (include_deleted or row[4] is None)
            and not is_individual_page_id(page_id)
        ):
            cur.execute(
                "SELECT links.source_page_id FROM memory_page_links AS links"
                " JOIN memory_pages AS pages ON pages.page_id = links.source_page_id"
                " WHERE links.target_page_id = %s AND pages.deleted_at IS NULL"
                f"{_scope_clause('swarm', column='pages.page_id')}"
                " ORDER BY links.source_page_id LIMIT 101",
                (page_id,),
            )
            backlinks = [str(item[0]) for item in cur.fetchall()[:100]]
        else:
            backlinks = []
    if row is None or (not include_deleted and row[4] is not None):
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")
    return {**_page_summary(row), "content": row[2], "backlinks": backlinks}


def save_page(
    page_id: str,
    body: Any,
    *,
    actor: str,
) -> dict[str, Any]:
    request = _object(body, "memory page request")
    _require_keys(
        request,
        {"description", "content", "expected_revision"},
        {"description", "content", "expected_revision"},
    )
    description = _description(request["description"])
    content = _content(request["content"])
    expected = _expected_revision(request["expected_revision"])
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            "SELECT description, content, revision, deleted_at, created_at"
            " FROM memory_pages WHERE page_id = %s FOR UPDATE",
            (page_id,),
        )
        current = cur.fetchone()
        if current is None:
            # A missing-row FOR UPDATE does not lock the key. Serialize the
            # uncommon create path so two agents cannot race the global quota
            # or turn a same-id create into an unhandled unique violation.
            cur.execute("LOCK TABLE memory_pages IN SHARE ROW EXCLUSIVE MODE")
            cur.execute(
                "SELECT description, content, revision, deleted_at, created_at"
                " FROM memory_pages WHERE page_id = %s FOR UPDATE",
                (page_id,),
            )
            current = cur.fetchone()
        changed = True
        if current is None:
            if expected != 0:
                raise WorkspaceError(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
            cur.execute("SELECT COUNT(*) FROM memory_pages")
            count_row = cur.fetchone()
            assert count_row is not None
            if int(count_row[0]) >= MAX_PAGES:
                raise WorkspaceError(
                    HTTPStatus.CONFLICT,
                    f"Workspace already retains {MAX_PAGES} memory pages",
                )
            revision = 1
            # Advance before and after the transaction. A continuation already
            # in flight sees the first edge even before this commit becomes
            # visible; a first page served during the transaction is made
            # stale by the second edge after commit.
            _advance_memory_search_generation()
            cur.execute(
                "INSERT INTO memory_pages"
                " (page_id, description, content, revision, deleted_at, created_by,"
                " updated_by, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, NULL, %s, %s, %s, %s)",
                (page_id, description, content, revision, actor, actor, now, now),
            )
        else:
            if current[3] is not None:
                raise WorkspaceError(
                    HTTPStatus.CONFLICT,
                    "memory page is deleted; restore it from history first",
                )
            if int(current[2]) != expected:
                raise WorkspaceError(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
            if current[0] == description and current[1] == content:
                changed = False
            else:
                revision = expected + 1
                _advance_memory_search_generation()
                cur.execute(
                    "UPDATE memory_pages SET description = %s, content = %s, revision = %s,"
                    " updated_by = %s, updated_at = %s WHERE page_id = %s",
                    (description, content, revision, actor, now, page_id),
                )
        if changed:
            _insert_revision(cur, page_id, revision, description, content, False, actor, now)
            _prune_revisions(cur, page_id)
            _replace_page_links(cur, page_id, content)
    # Signal after the transaction commits: waking the indexer while the new
    # revision is still uncommitted spends the wakeup on a snapshot that cannot
    # see it, leaving the page to wait out the backstop instead.
    if changed:
        _advance_memory_search_generation()
        _embedding_work.set()
    return load_page(page_id, include_deleted=True)


def delete_page(
    page_id: str,
    query: dict[str, list[str]],
    *,
    actor: str,
) -> dict[str, Any]:
    _reject_query_keys(query, {"expected_revision"}, "memory delete")
    expected = _required_query_revision(query)
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            "SELECT description, content, revision, deleted_at"
            " FROM memory_pages WHERE page_id = %s FOR UPDATE",
            (page_id,),
        )
        row = cur.fetchone()
        if row is None or row[3] is not None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")
        if int(row[2]) != expected:
            raise WorkspaceError(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
        revision = expected + 1
        _advance_memory_search_generation()
        cur.execute(
            "UPDATE memory_pages SET revision = %s, deleted_at = %s, updated_by = %s,"
            " updated_at = %s WHERE page_id = %s",
            (revision, now, actor, now, page_id),
        )
        _insert_revision(cur, page_id, revision, row[0], row[1], True, actor, now)
        _prune_revisions(cur, page_id)
        _replace_page_links(cur, page_id, "")
        cur.execute("DELETE FROM memory_page_embeddings WHERE page_id = %s", (page_id,))
    _advance_memory_search_generation()
    return {"ok": True, "revision": revision}


def list_revisions(page_id: str, query: dict[str, list[str]]) -> dict[str, Any]:
    _reject_query_keys(query, {"before", "limit"}, "memory history")
    limit = _limit(query, default=40, maximum=MAX_REVISION_PAGE_LIMIT)
    before = _optional_positive_int(query, "before")
    clause = " AND id < %s" if before is not None else ""
    params: list[Any] = [page_id]
    if before is not None:
        params.append(before)
    with db.transaction() as cur:
        cur.execute("SELECT 1 FROM memory_pages WHERE page_id = %s", (page_id,))
        if cur.fetchone() is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")
        cur.execute(
            "SELECT id, revision, description, content, deleted, actor, created_at"
            f" FROM memory_page_revisions WHERE page_id = %s{clause}"
            " ORDER BY id DESC LIMIT %s",
            (*params, limit + 1),
        )
        rows = cur.fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    response: dict[str, Any] = {
        "revisions": [
            {
                "id": row[0],
                "revision": row[1],
                "description": row[2],
                "content": row[3],
                "deleted": row[4],
                "actor": row[5],
                "created_at": row[6],
            }
            for row in rows
        ]
    }
    if more and rows:
        response["next_before"] = rows[-1][0]
    return response


def restore_revision(page_id: str, revision: int, body: Any) -> dict[str, Any]:
    request = _object(body, "memory restore request")
    _require_keys(request, {"expected_revision"}, {"expected_revision"})
    expected = _expected_revision(request["expected_revision"])
    now = _utc_now()
    with db.transaction() as cur:
        cur.execute(
            "SELECT revision FROM memory_pages WHERE page_id = %s FOR UPDATE",
            (page_id,),
        )
        current = cur.fetchone()
        if current is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")
        if int(current[0]) != expected:
            raise WorkspaceError(HTTPStatus.CONFLICT, "memory page changed; reload and retry")
        cur.execute(
            "SELECT description, content, deleted FROM memory_page_revisions"
            " WHERE page_id = %s AND revision = %s",
            (page_id, revision),
        )
        source = cur.fetchone()
        if source is None:
            raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory revision not found")
        new_revision = expected + 1
        deleted_at = now if source[2] else None
        _advance_memory_search_generation()
        cur.execute(
            "UPDATE memory_pages SET description = %s, content = %s, revision = %s,"
            " deleted_at = %s, updated_by = 'user', updated_at = %s WHERE page_id = %s",
            (source[0], source[1], new_revision, deleted_at, now, page_id),
        )
        _insert_revision(
            cur, page_id, new_revision, source[0], source[1], bool(source[2]), "user", now
        )
        _prune_revisions(cur, page_id)
        _replace_page_links(cur, page_id, "" if source[2] else str(source[1]))
        if source[2]:
            # Restoring to a deleted revision leaves the page deleted, and the
            # index loop skips deleted pages, so nothing else would drop this
            # vector before the 90-day hard delete. Match delete_page().
            cur.execute(
                "DELETE FROM memory_page_embeddings WHERE page_id = %s", (page_id,)
            )
    # A restore to a live revision needs a new vector; a restore to a deleted
    # one just dropped its vector above and has nothing to index.
    _advance_memory_search_generation()
    if not source[2]:
        _embedding_work.set()
    return load_page(page_id, include_deleted=True)


def prune_deleted(now: datetime | None = None) -> int:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=DELETED_RETAIN_DAYS)
    with db.transaction() as cur:
        cur.execute(
            "DELETE FROM memory_pages WHERE deleted_at IS NOT NULL AND deleted_at < %s"
            " RETURNING page_id",
            (_format_ts(cutoff),),
        )
        pruned = len(cur.fetchall())
    return pruned


def embedding_index_loop() -> None:
    """Incrementally encode current memory pages outside write transactions."""
    while True:
        try:
            # Clear before claiming, so a page saved between the claim and the
            # wait still wakes this thread rather than waiting out the backstop.
            _embedding_work.clear()
            pending = _unembedded_memory_pages(
                embedding_client.MODEL_NAME,
                embedding_client.MAX_TEXTS,
            )
            if not pending:
                _embedding_work.wait(EMBEDDING_IDLE_SECONDS)
                continue
            texts = [
                f"{page_id}\n{description}\n{content}"
                for page_id, _revision, description, content in pending
            ]
            vectors = embedding_client.embed_texts(texts, kind="passage")
            _store_memory_page_embeddings(
                embedding_client.MODEL_NAME,
                [
                    (pending[index][0], pending[index][1], vector)
                    for index, vector in enumerate(vectors)
                ],
            )
            time.sleep(0.25)
        except Exception as exc:
            host_errors.report_unexpected("workspace.memory_embedding_index", exc)
            time.sleep(30)


def _unembedded_memory_pages(
    model: str, limit: int
) -> list[tuple[str, int, str, str]]:
    with db.transaction() as cur:
        cur.execute(
            "SELECT pages.page_id, pages.revision, pages.description, pages.content"
            " FROM memory_pages AS pages"
            " LEFT JOIN memory_page_embeddings AS embeddings"
            " ON embeddings.page_id = pages.page_id AND embeddings.model = %s"
            " AND embeddings.revision = pages.revision"
            " WHERE pages.deleted_at IS NULL AND embeddings.page_id IS NULL"
            " ORDER BY pages.updated_at DESC, pages.page_id LIMIT %s",
            (model, limit),
        )
        rows = cur.fetchall()
    return [
        (str(page_id), int(revision), str(description), str(content))
        for page_id, revision, description, content in rows
    ]


def _store_memory_page_embeddings(
    model: str,
    rows: list[tuple[str, int, list[float]]],
) -> None:
    if not rows:
        return
    # Mark the candidate set unstable before inference results enter their
    # write transaction, then advance again after commit. This closes the
    # commit-to-counter race for searches already between their two checks.
    _advance_memory_search_generation()
    with db.transaction() as cur:
        for page_id, revision, embedding in rows:
            literal = "[" + ",".join(format(value, ".9g") for value in embedding) + "]"
            cur.execute(
                "INSERT INTO memory_page_embeddings"
                " (page_id, revision, model, embedding)"
                " SELECT page_id, revision, %s, %s::vector FROM memory_pages"
                " WHERE page_id = %s AND revision = %s AND deleted_at IS NULL"
                " ON CONFLICT (page_id, model) DO UPDATE SET"
                " revision = EXCLUDED.revision, embedding = EXCLUDED.embedding,"
                " embedded_at = clock_timestamp()",
                (model, literal, page_id, revision),
            )
    # Even a single new vector can change approximate HNSW traversal and the
    # fused prefix, so continuations must restart after an indexing batch.
    _advance_memory_search_generation()


def _page_summary(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "page_id": row[0],
        "description": row[1],
        "revision": row[3],
        "deleted": row[4] is not None,
        "links": _page_links(str(row[0]), str(row[2])),
        "updated_by": row[5],
        "created_at": row[6],
        "updated_at": row[7],
    }


def _page_links(page_id: str, content: str) -> list[str]:
    if is_individual_page_id(page_id):
        return []
    return [
        target
        for target in dict.fromkeys(LINK_RE.findall(content))
        if not is_individual_page_id(target)
    ][:MAX_PAGE_LINKS]


def _insert_revision(
    cur: Any,
    page_id: str,
    revision: int,
    description: str,
    content: str,
    deleted: bool,
    actor: str,
    now: str,
) -> None:
    cur.execute(
        "INSERT INTO memory_page_revisions"
        " (page_id, revision, description, content, deleted, actor, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (page_id, revision, description, content, deleted, actor, now),
    )


def _prune_revisions(cur: Any, page_id: str) -> None:
    cur.execute(
        "DELETE FROM memory_page_revisions WHERE id IN ("
        " SELECT id FROM memory_page_revisions WHERE page_id = %s"
        " ORDER BY id DESC OFFSET %s) RETURNING id",
        (page_id, REVISION_RETAINED),
    )
    cur.fetchall()


def _replace_page_links(cur: Any, page_id: str, content: str) -> None:
    cur.execute("DELETE FROM memory_page_links WHERE source_page_id = %s", (page_id,))
    if is_individual_page_id(page_id):
        return
    for target_page_id in _page_links(page_id, content):
        cur.execute(
            "INSERT INTO memory_page_links (source_page_id, target_page_id)"
            " VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (page_id, target_page_id),
        )


def _page_id(value: str) -> str:
    decoded = unquote(value)
    if PAGE_ID_RE.fullmatch(decoded) is None:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "page_id must be a lowercase slug of at most 64 characters",
        )
    return decoded


def is_individual_page_id(page_id: str) -> bool:
    return INDIVIDUAL_PAGE_ID_RE.match(page_id) is not None


def individual_page_id(value: str) -> str:
    page_id = _page_id(value)
    if not is_individual_page_id(page_id):
        raise WorkspaceError(
            HTTPStatus.CONFLICT,
            "self-memory is unavailable for this thread identity",
        )
    return page_id


def _require_swarm_page(page_id: str) -> None:
    if is_individual_page_id(page_id):
        # Do not reveal whether an identity-owned page exists through the
        # ordinary shared-memory API.
        raise WorkspaceError(HTTPStatus.NOT_FOUND, "memory page not found")


def _memory_scope(query: dict[str, list[str]]) -> str:
    scope = _one(query, "scope") or "swarm"
    if scope not in {"swarm", "individual"}:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            "scope must be swarm or individual",
        )
    return scope


def _scope_clause(scope: str | None, *, column: str = "page_id") -> str:
    if scope is None:
        return ""
    prefixes = (
        f"({column} LIKE 'app-%' OR {column} LIKE 'thread-%'"
        f" OR {column} LIKE 'schedule-%')"
    )
    return f" AND {prefixes}" if scope == "individual" else f" AND NOT {prefixes}"


def _description(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_DESCRIPTION_CHARS:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"description must be between 1 and {MAX_DESCRIPTION_CHARS} characters",
        )
    if "\n" in value or "\r" in value:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "description must be one line")
    return value


def _content(value: Any) -> str:
    if not isinstance(value, str) or len(value) > MAX_CONTENT_CHARS:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST,
            f"content must be at most {MAX_CONTENT_CHARS} characters",
        )
    return value


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{label} must be an object")
    return value


def _require_keys(value: dict[str, Any], allowed: set[str], required: set[str]) -> None:
    extra = sorted(set(value) - allowed)
    missing = sorted(required - set(value))
    if extra:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"unsupported field: {extra[0]}")
    if missing:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"missing field: {missing[0]}")


def _expected_revision(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "expected_revision must be non-negative")
    return value


def _required_query_revision(query: dict[str, list[str]]) -> int:
    value = _one(query, "expected_revision")
    if value is None or not value.isdigit():
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST, "expected_revision must be a non-negative integer"
        )
    return int(value)


def _limit(
    query: dict[str, list[str]],
    *,
    default: int = DEFAULT_PAGE_LIMIT,
    maximum: int = MAX_PAGE_LIMIT,
) -> int:
    raw = _one(query, "limit")
    if raw is None:
        return default
    if not raw.isdigit() or not 1 <= int(raw) <= maximum:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST, f"limit must be between 1 and {maximum}"
        )
    return int(raw)


def _boolean_query(query: dict[str, list[str]], key: str, *, default: bool) -> bool:
    raw = _one(query, key)
    if raw is None:
        return default
    if raw not in {"true", "false"}:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{key} must be true or false")
    return raw == "true"


def _optional_positive_int(query: dict[str, list[str]], key: str) -> int | None:
    raw = _one(query, key)
    if raw is None:
        return None
    if not raw.isdigit() or not 1 <= int(raw) <= MAX_BIGINT:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, f"{key} must be a positive integer")
    return int(raw)


def _reject_query_keys(
    query: dict[str, list[str]], allowed: set[str], label: str
) -> None:
    extra = sorted(set(query) - allowed)
    if extra:
        raise WorkspaceError(
            HTTPStatus.BAD_REQUEST, f"unexpected {label} query field: {extra[0]}"
        )


def _encode_cursor(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _decode_cursor_value(value: str) -> str:
    try:
        encoded = value.encode("ascii")
        if not encoded or len(encoded) > MAX_CURSOR_BYTES:
            raise ValueError
        padded = encoded + b"=" * (-len(encoded) % 4)
        return base64.b64decode(padded, altchars=b"-_", validate=True).decode()
    except (ValueError, UnicodeEncodeError, UnicodeDecodeError, binascii.Error) as exc:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid") from exc


def _decode_cursor(value: str | None) -> str | None:
    if value is None:
        return None
    decoded = _decode_cursor_value(value)
    if PAGE_ID_RE.fullmatch(decoded) is None:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid")
    return decoded


def _encode_offset_cursor(offset: int) -> str:
    return _encode_cursor(str(offset))


def _decode_offset_cursor(value: str | None) -> int:
    if value is None:
        return 0
    decoded = _decode_cursor_value(value)
    if not decoded.isdigit() or int(decoded) > MAX_PAGES:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid")
    return int(decoded)


def _memory_search_fingerprint(needle: str, scope: str) -> str:
    return hashlib.sha256(
        json.dumps([needle, scope], separators=(",", ":")).encode()
    ).hexdigest()[:24]


def _memory_search_generation() -> int:
    with _search_generation_lock:
        return _search_generation


def _advance_memory_search_generation() -> None:
    global _search_generation
    with _search_generation_lock:
        _search_generation += 1


def _encode_semantic_offset_cursor(
    offset: int,
    mode: str,
    fingerprint: str,
    generation: int,
) -> str:
    if mode not in {"hybrid", "fallback"}:
        raise ValueError("semantic cursor mode is invalid")
    fields: list[Any] = [mode, offset, generation, fingerprint]
    signature = hmac.new(
        _search_cursor_key,
        json.dumps(fields, separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    return _encode_cursor(json.dumps([*fields, signature], separators=(",", ":")))


def _decode_semantic_offset_cursor(
    value: str | None,
    fingerprint: str,
) -> tuple[str | None, int, int | None]:
    if value is None:
        return None, 0, None
    decoded = _decode_cursor_value(value)
    try:
        fields = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid") from exc
    if (
        not isinstance(fields, list)
        or len(fields) != 5
        or fields[0] not in {"hybrid", "fallback"}
        or not isinstance(fields[1], int)
        or isinstance(fields[1], bool)
        or not 0 <= fields[1] <= MAX_PAGES
        or not isinstance(fields[2], int)
        or isinstance(fields[2], bool)
        or fields[2] < 0
        or fields[3] != fingerprint
        or not isinstance(fields[4], str)
    ):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid")
    expected = hmac.new(
        _search_cursor_key,
        json.dumps(fields[:4], separators=(",", ":")).encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(fields[4], expected):
        raise WorkspaceError(HTTPStatus.BAD_REQUEST, "cursor is invalid")
    return str(fields[0]), int(fields[1]), int(fields[2])


def _utc_now() -> str:
    return _format_ts(datetime.now(timezone.utc))


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime(TIME_FORMAT)
