"""Bounded, cursor-stable retained conversation search and reads."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
import hashlib
import hmac
import json
import math
import re
import secrets
from typing import Any

from host.runtime.admin_api.errors import ApiError
from host.runtime.admin_api.request_params import clip_json_encoded_text as _clip_json_encoded_text
from host.runtime.core import host_errors, pgclient, state
from host.runtime.embeddings import client as embedding_client

PRODUCT_THREAD_ID_RE = re.compile(
    r"(?=^[a-z0-9-]{1,64}$)^(?:app|thread|schedule)-[a-z0-9-]+$"
)
UTC_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
RFC3339_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.(?P<fraction>[0-9]+))?(?:Z|[+-][0-9]{2}:[0-9]{2})$",
    re.IGNORECASE,
)
CONVERSATION_SEARCH_LIMIT = 25
CONVERSATION_SEARCH_EXCERPT_BYTES = 2 * 1024
CONVERSATION_READ_LIMIT = 50
CONVERSATION_QUERY_BYTES = 512
CONVERSATION_VARIANT_BYTES = 256
CONVERSATION_VARIANT_LIMIT = 8
CONVERSATION_CURSOR_BYTES = 8192
CONVERSATION_MESSAGE_BYTES = 16 * 1024
CONVERSATION_RESPONSE_BYTES = 256 * 1024
CONVERSATION_EVENT_TYPES = ("thread.message", "thread.activity")
CONVERSATION_SEMANTIC_CANDIDATES = 200
_CONVERSATION_CURSOR_SIGNING_KEY = secrets.token_bytes(32)
HISTORY_PROVENANCE = "retained_conversation_history"
HISTORY_TRUST = "untrusted"
HISTORY_INSTRUCTION_AUTHORITY = "none"
POSTGRES_BIGINT_MAX = 9_223_372_036_854_775_807
EVENT_ID_RE = re.compile(r"^event_([1-9][0-9]{0,18})$")
CONVERSATION_ACTIVITY_FIELDS = (
    ("activity_id", 128),
    ("provider", 128),
    ("kind", 128),
    ("phase", 128),
    ("title", 256),
    ("status", 128),
    ("detail", 768),
    ("output", 1024),
    ("error", 1024),
)

def _conversation_utf8_bytes(value: str, field: str) -> bytes:
    try:
        return value.encode()
    except UnicodeEncodeError as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"{field} must be valid UTF-8",
        ) from exc


def _optional_conversation_text(value: Any, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be a non-empty string")
    if "\x00" in value:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must not contain NUL")
    normalized = value.strip()
    if len(_conversation_utf8_bytes(normalized, field)) > maximum:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"{field} must be at most {maximum} UTF-8 bytes",
        )
    return normalized


def _optional_conversation_timestamp(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an RFC 3339 timestamp")
    if len(_conversation_utf8_bytes(value, field)) > 64:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an RFC 3339 timestamp")
    timestamp_match = RFC3339_TIMESTAMP_RE.fullmatch(value)
    if timestamp_match is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an RFC 3339 timestamp")
    try:
        fraction = timestamp_match.group("fraction")
        parse_value = value
        if fraction is not None and len(fraction) > 6:
            parse_value = (
                value[: timestamp_match.start("fraction") + 6]
                + value[timestamp_match.end("fraction") :]
            )
        parsed = datetime.fromisoformat(
            parse_value[:-1] + "+00:00"
            if parse_value.endswith(("Z", "z"))
            else parse_value
        )
        parsed = parsed.astimezone(timezone.utc)
        if fraction is not None and any(digit != "0" for digit in fraction):
            parsed = parsed.replace(microsecond=0) + timedelta(seconds=1)
    except (ValueError, OverflowError) as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"{field} must be an RFC 3339 timestamp",
        ) from exc
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ")


def _conversation_limit(value: Any, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= maximum:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"limit must be between 1 and {maximum}",
        )
    return value


def _conversation_event_seq(value: Any, field: str) -> int:
    if not isinstance(value, str) or (match := EVENT_ID_RE.fullmatch(value)) is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an event id")
    seq = int(match.group(1))
    if seq > POSTGRES_BIGINT_MAX:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must be an event id")
    return seq


def _conversation_search_fingerprint(
    queries: list[str],
    from_timestamp: str | None,
    to_timestamp: str | None,
    thread_id: str | None,
    roles: list[str],
) -> str:
    encoded = json.dumps(
        [queries, from_timestamp, to_timestamp, thread_id, roles],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _encode_conversation_search_cursor(
    fingerprint: str,
    relevance: bool,
    value: dict[str, Any],
    *,
    mode: str | None = None,
    min_seq: int | None = None,
    max_seq: int | None = None,
    embedding_min_seq: int | None = None,
    embedding_generation: int | None = None,
    semantic_seqs: tuple[int, ...] | None = None,
) -> str:
    cursor_mode = mode or ("rank" if relevance else "time")
    if relevance:
        fields = [fingerprint, cursor_mode, value.get("rank"), value.get("seq")]
        snapshot = [
            min_seq,
            max_seq,
            embedding_min_seq,
            embedding_generation,
            None if semantic_seqs is None else list(semantic_seqs),
        ]
        if all(item is not None for item in snapshot):
            fields.extend(snapshot)
            signature = hmac.new(
                _CONVERSATION_CURSOR_SIGNING_KEY,
                json.dumps(fields, separators=(",", ":")).encode(),
                hashlib.sha256,
            ).hexdigest()
            fields.append(signature)
        elif cursor_mode != "rank":
            raise ValueError("relevance cursor requires a complete snapshot")
    else:
        fields = [fingerprint, "time", value.get("timestamp"), value.get("seq")]
    raw = json.dumps(fields, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_conversation_search_cursor(
    value: Any,
    fingerprint: str,
    relevance: bool,
) -> tuple[
    str,
    float | int | str,
    int,
    int | None,
    int | None,
    int | None,
    int | None,
    tuple[int, ...] | None,
] | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or not value:
            raise ValueError
        encoded = _conversation_utf8_bytes(value, "cursor")
        if len(encoded) > CONVERSATION_CURSOR_BYTES:
            raise ValueError
        padded = encoded + b"=" * (-len(encoded) % 4)
        decoded = json.loads(
            base64.b64decode(padded, altchars=b"-_", validate=True)
        )
        if not isinstance(decoded, list) or len(decoded) not in {4, 10}:
            raise ValueError
        if len(decoded) == 10:
            signature = decoded.pop()
            if not isinstance(signature, str) or not hmac.compare_digest(
                signature,
                hmac.new(
                    _CONVERSATION_CURSOR_SIGNING_KEY,
                    json.dumps(decoded, separators=(",", ":")).encode(),
                    hashlib.sha256,
                ).hexdigest(),
            ):
                raise ValueError
        cursor_fingerprint, mode, position, seq = decoded[:4]
        valid_modes = {"rank", "hybrid", "fallback", "lexical"} if relevance else {"time"}
        if (
            cursor_fingerprint != fingerprint
            or mode not in valid_modes
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or seq < 1
            or seq > POSTGRES_BIGINT_MAX
        ):
            raise ValueError
        if relevance:
            if len(decoded) == 4:
                # Cursors issued before snapshot fields existed were plain
                # lexical rank cursors. Preserve that compatibility without
                # pretending they are stable hybrid positions.
                if mode != "rank":
                    raise ValueError
                snapshot: tuple[
                    int | None,
                    int | None,
                    int | None,
                    int | None,
                    tuple[int, ...] | None,
                ] = (
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            else:
                (
                    min_seq,
                    max_seq,
                    embedding_min_seq,
                    embedding_generation,
                    encoded_semantic_seqs,
                ) = decoded[4:]
                if (
                    not isinstance(min_seq, int)
                    or isinstance(min_seq, bool)
                    or min_seq < 0
                    or not isinstance(max_seq, int)
                    or isinstance(max_seq, bool)
                    or max_seq < min_seq
                    or max_seq > POSTGRES_BIGINT_MAX
                    or not isinstance(embedding_min_seq, int)
                    or isinstance(embedding_min_seq, bool)
                    or embedding_min_seq < 0
                    or embedding_min_seq > max_seq
                    or not isinstance(embedding_generation, int)
                    or isinstance(embedding_generation, bool)
                    or embedding_generation < 0
                    or embedding_generation > POSTGRES_BIGINT_MAX
                    or not isinstance(encoded_semantic_seqs, list)
                    or len(encoded_semantic_seqs) > CONVERSATION_SEMANTIC_CANDIDATES
                    or any(
                        not isinstance(candidate, int)
                        or isinstance(candidate, bool)
                        or candidate < 1
                        or candidate > max_seq
                        for candidate in encoded_semantic_seqs
                    )
                    or len(set(encoded_semantic_seqs)) != len(encoded_semantic_seqs)
                ):
                    raise ValueError
                snapshot = (
                    min_seq,
                    max_seq,
                    embedding_min_seq,
                    embedding_generation,
                    tuple(encoded_semantic_seqs),
                )
            if mode in {"hybrid", "fallback"}:
                if (
                    not isinstance(position, int)
                    or isinstance(position, bool)
                    or not 0 <= position <= CONVERSATION_SEMANTIC_CANDIDATES * 2
                ):
                    raise ValueError
                return (
                    mode,
                    position,
                    seq,
                    snapshot[0],
                    snapshot[1],
                    snapshot[2],
                    snapshot[3],
                    snapshot[4],
                )
            if (
                not isinstance(position, (int, float))
                or isinstance(position, bool)
            ):
                raise ValueError
            try:
                rank = float(position)
            except OverflowError as exc:
                raise ValueError from exc
            if not math.isfinite(rank) or rank < 0:
                raise ValueError
            # ``rank`` is the pre-hybrid cursor mode. It must continue as plain
            # lexical pagination: that client has not consumed a fused prefix,
            # so excluding today's semantic candidates would silently skip
            # results it has never seen.
            return (
                mode,
                rank,
                seq,
                snapshot[0],
                snapshot[1],
                snapshot[2],
                snapshot[3],
                snapshot[4],
            )
        if not isinstance(position, str) or UTC_TIMESTAMP_RE.fullmatch(position) is None:
            raise ValueError
        try:
            datetime.strptime(position, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError as exc:
            raise ValueError from exc
        return "time", position, seq, None, None, None, None, None
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "cursor is invalid or belongs to different search filters",
        ) from exc


def _require_conversation_search_snapshot(
    expected_event_min_seq: int,
    expected_embedding_min_seq: int,
) -> None:
    event_min_seq, embedding_min_seq = state.conversation_search_retention()
    source_expired = expected_event_min_seq > 0 and event_min_seq != expected_event_min_seq
    vectors_expired = expected_embedding_min_seq > 0 and (
        embedding_min_seq == 0 or embedding_min_seq > expected_embedding_min_seq
    )
    if source_expired or vectors_expired:
        raise ApiError(
            HTTPStatus.CONFLICT,
            "conversation search cursor snapshot expired; restart the search",
        )


def _frozen_conversation_semantic_rows(
    semantic_seqs: tuple[int, ...],
    *,
    from_timestamp: str | None,
    to_timestamp: str | None,
    thread_id: str | None,
    sources: tuple[str, ...],
    min_seq: int,
    max_seq: int,
    embedding_min_seq: int,
) -> list[dict[str, Any]]:
    """Resolve cursor ids only when every id still satisfies its search."""
    rows = state.thread_messages_by_seqs(
        semantic_seqs,
        from_timestamp=from_timestamp,
        to_timestamp=to_timestamp,
        thread_id=thread_id,
        sources=sources,
        max_seq=max_seq,
    )
    if len(rows) != len(semantic_seqs):
        # Prefer the specific expiry response when retention advanced during
        # this lookup; otherwise the cursor was altered or never belonged to
        # these fingerprinted filters.
        _require_conversation_search_snapshot(min_seq, embedding_min_seq)
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "cursor is invalid or belongs to different search filters",
        )
    return rows


def search_conversation_history(body: Any) -> dict[str, Any]:
    """Validate and execute one public, bounded history-search request."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "conversation search must be an object")
    allowed = {
        "query",
        "query_variants",
        "from",
        "to",
        "thread_id",
        "roles",
        "limit",
        "cursor",
    }
    unexpected = sorted(set(body) - allowed)
    if unexpected:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"unsupported conversation search field: {unexpected[0]}",
        )
    query = _optional_conversation_text(body.get("query"), "query", CONVERSATION_QUERY_BYTES)
    variants = body.get("query_variants", [])
    if not isinstance(variants, list) or len(variants) > CONVERSATION_VARIANT_LIMIT:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"query_variants must contain at most {CONVERSATION_VARIANT_LIMIT} strings",
        )
    normalized_variants: list[str] = []
    for value in variants:
        variant = _optional_conversation_text(
            value, "query variant", CONVERSATION_VARIANT_BYTES
        )
        if variant is None:
            raise ApiError(HTTPStatus.BAD_REQUEST, "query variants must be non-empty")
        if variant not in normalized_variants:
            normalized_variants.append(variant)
    if normalized_variants and query is None:
        raise ApiError(HTTPStatus.BAD_REQUEST, "query_variants require query")
    queries = ([] if query is None else [query]) + [
        variant for variant in normalized_variants if variant != query
    ]
    from_timestamp = _optional_conversation_timestamp(body.get("from"), "from")
    to_timestamp = _optional_conversation_timestamp(body.get("to"), "to")
    if (
        from_timestamp is not None
        and to_timestamp is not None
        and from_timestamp >= to_timestamp
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "from must be earlier than to")
    thread_id = body.get("thread_id")
    if thread_id is not None and (
        not isinstance(thread_id, str)
        or PRODUCT_THREAD_ID_RE.fullmatch(thread_id) is None
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "thread_id is invalid")
    if query is None and from_timestamp is None and to_timestamp is None and thread_id is None:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "provide query, from, to, or thread_id",
        )
    roles = body.get("roles", ["user", "assistant"])
    if (
        not isinstance(roles, list)
        or not roles
        or not all(isinstance(role, str) for role in roles)
        or set(roles) - {"user", "assistant"}
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "roles must contain user and/or assistant")
    roles = list(dict.fromkeys(roles))
    limit = _conversation_limit(body.get("limit", 10), CONVERSATION_SEARCH_LIMIT)
    fingerprint = _conversation_search_fingerprint(
        queries, from_timestamp, to_timestamp, thread_id, roles
    )
    decoded_cursor = _decode_conversation_search_cursor(
        body.get("cursor"), fingerprint, bool(queries)
    )
    cursor_mode = decoded_cursor[0] if decoded_cursor is not None else None
    snapshot_min_seq: int | None = None
    snapshot_max_seq: int | None = None
    snapshot_embedding_min_seq: int | None = None
    snapshot_embedding_generation: int | None = None
    snapshot_semantic_seqs: tuple[int, ...] | None = None
    hybrid_offset = 0
    before: tuple[float, int] | tuple[str, int] | None = None
    if decoded_cursor is not None:
        (
            _mode,
            position,
            seq,
            snapshot_min_seq,
            snapshot_max_seq,
            snapshot_embedding_min_seq,
            snapshot_embedding_generation,
            snapshot_semantic_seqs,
        ) = decoded_cursor
        if cursor_mode in {"hybrid", "fallback"}:
            if not isinstance(position, int) or isinstance(position, bool):
                raise ApiError(HTTPStatus.BAD_REQUEST, "cursor is invalid")
            hybrid_offset = position
        elif queries:
            if not isinstance(position, float):
                raise ApiError(HTTPStatus.BAD_REQUEST, "cursor is invalid")
            before = (position, seq)
        else:
            if not isinstance(position, str):
                raise ApiError(HTTPStatus.BAD_REQUEST, "cursor is invalid")
            before = (position, seq)
    if queries and decoded_cursor is None:
        (
            snapshot_min_seq,
            snapshot_max_seq,
            snapshot_embedding_min_seq,
            snapshot_embedding_generation,
        ) = state.conversation_search_snapshot()
    if queries and snapshot_min_seq is not None:
        assert snapshot_embedding_min_seq is not None
        _require_conversation_search_snapshot(
            snapshot_min_seq,
            snapshot_embedding_min_seq,
        )
    search_mode = "lexical" if queries else "timestamp"
    continuation_mode: str | None = None
    lexical_tail: dict[str, Any] | None = None
    lexical_tail_mode = "lexical"
    try:
        sources = tuple("user" if role == "user" else "agent" for role in roles)
        if queries and cursor_mode not in {"rank", "lexical"}:
            lexical_rows = state.search_thread_messages(
                tuple(queries),
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                thread_id=thread_id,
                sources=sources,
                limit=CONVERSATION_SEMANTIC_CANDIDATES + 1,
                before=None,
                max_seq=snapshot_max_seq,
            )
            # The extra row distinguishes a result set that ends exactly at the
            # candidate window from one with more matches below it, so a lexical
            # continuation is only advertised when a tail really exists.
            if len(lexical_rows) > CONVERSATION_SEMANTIC_CANDIDATES:
                lexical_rows = lexical_rows[:CONVERSATION_SEMANTIC_CANDIDATES]
                last_lexical = lexical_rows[-1]
                lexical_tail = {
                    "rank": last_lexical["search_rank"],
                    "seq": last_lexical["seq"],
                }
            if cursor_mode == "fallback":
                # A fallback cursor owns a lexical-only positional offset.
                # Keep that ordering stable even if inference has recovered;
                # switching it to fused ranking would skip or repeat hits.
                rows = _hybrid_conversation_rows(lexical_rows, [])[hybrid_offset:]
                search_mode = "lexical_fallback"
                continuation_mode = "fallback"
                lexical_tail_mode = "rank"
            elif cursor_mode == "hybrid":
                # The first page froze the ordered HNSW candidate ids. Fetch
                # their immutable source rows instead of rerunning an
                # approximate scan whose graph may have changed meanwhile.
                assert snapshot_semantic_seqs is not None
                assert snapshot_min_seq is not None
                assert snapshot_max_seq is not None
                assert snapshot_embedding_min_seq is not None
                semantic_rows = _frozen_conversation_semantic_rows(
                    snapshot_semantic_seqs,
                    from_timestamp=from_timestamp,
                    to_timestamp=to_timestamp,
                    thread_id=thread_id,
                    sources=sources,
                    min_seq=snapshot_min_seq,
                    max_seq=snapshot_max_seq,
                    embedding_min_seq=snapshot_embedding_min_seq,
                )
                rows = _hybrid_conversation_rows(
                    lexical_rows,
                    semantic_rows,
                )[hybrid_offset:]
                search_mode = "hybrid"
                continuation_mode = "hybrid"
            else:
                try:
                    query_embedding = embedding_client.embed_texts(
                        [query or queries[0]], kind="query"
                    )[0]
                    semantic_rows = state.search_thread_messages_semantic(
                        query_embedding,
                        embedding_client.MODEL_NAME,
                        from_timestamp=from_timestamp,
                        to_timestamp=to_timestamp,
                        thread_id=thread_id,
                        sources=sources,
                        limit=CONVERSATION_SEMANTIC_CANDIDATES,
                        minimum_similarity=embedding_client.MINIMUM_SIMILARITY,
                        max_seq=snapshot_max_seq,
                        max_embedding_generation=snapshot_embedding_generation,
                    )
                    snapshot_semantic_seqs = tuple(
                        int(row["seq"]) for row in semantic_rows
                    )
                    rows = _hybrid_conversation_rows(
                        lexical_rows,
                        semantic_rows,
                    )[hybrid_offset:]
                    search_mode = "hybrid"
                    continuation_mode = "hybrid"
                except (embedding_client.EmbeddingError, pgclient.Error, OSError) as exc:
                    if not isinstance(exc, embedding_client.EmbeddingError):
                        host_errors.report_unexpected("admin_api.embedding_search", exc)
                    snapshot_semantic_seqs = ()
                    rows = _hybrid_conversation_rows(lexical_rows, [])[hybrid_offset:]
                    search_mode = "lexical_fallback"
                    continuation_mode = "fallback"
                    # A fallback prefix contains only lexical candidates, so
                    # its tail must not exclude semantic candidates.
                    lexical_tail_mode = "rank"
        else:
            # A lexical continuation walks below the fused window, where the
            # semantic candidates already returned on the hybrid pages appear
            # again at their own lexical rank. Exclude the candidate ids frozen
            # by the first page so the walk yields each hit once without
            # rerunning inference or HNSW.
            exclude_seqs: tuple[int, ...] = ()
            if queries and cursor_mode == "lexical":
                assert snapshot_semantic_seqs is not None
                assert snapshot_min_seq is not None
                assert snapshot_max_seq is not None
                assert snapshot_embedding_min_seq is not None
                _frozen_conversation_semantic_rows(
                    snapshot_semantic_seqs,
                    from_timestamp=from_timestamp,
                    to_timestamp=to_timestamp,
                    thread_id=thread_id,
                    sources=sources,
                    min_seq=snapshot_min_seq,
                    max_seq=snapshot_max_seq,
                    embedding_min_seq=snapshot_embedding_min_seq,
                )
                exclude_seqs = snapshot_semantic_seqs
            rows = state.search_thread_messages(
                tuple(queries),
                from_timestamp=from_timestamp,
                to_timestamp=to_timestamp,
                thread_id=thread_id,
                sources=sources,
                limit=limit + 1,
                before=before,
                max_seq=snapshot_max_seq,
                exclude_seqs=exclude_seqs,
            )
            continuation_mode = (
                "rank" if cursor_mode == "rank" else "lexical"
            ) if queries else None
    except pgclient.Error as exc:
        if exc.sqlstate == "57014":
            raise ApiError(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                "conversation search exceeded its work limit; narrow the query or time range",
            ) from exc
        raise
    if queries and snapshot_min_seq is not None:
        assert snapshot_embedding_min_seq is not None
        _require_conversation_search_snapshot(
            snapshot_min_seq,
            snapshot_embedding_min_seq,
        )
    page = rows[:limit]
    matches = []
    for row in page:
        excerpt = _clip_json_encoded_text(
            row["excerpt"], CONVERSATION_SEARCH_EXCERPT_BYTES
        )
        matches.append(
            {
                "thread_id": row["thread_id"],
                "event_id": row["event_id"],
                "timestamp": row["timestamp"],
                "role": "user" if row["source"] == "user" else "assistant",
                "excerpt": excerpt,
                "excerpt_truncated": row["excerpt_truncated"] or excerpt != row["excerpt"],
            }
        )
    response: dict[str, Any] = {
        "provenance": HISTORY_PROVENANCE,
        "trust": HISTORY_TRUST,
        "instruction_authority": HISTORY_INSTRUCTION_AUTHORITY,
        "matches": matches,
        "search_mode": search_mode,
        "next_cursor": None,
    }
    if len(rows) > limit and page:
        last = page[-1]
        next_value = (
            {
                "rank": (
                    hybrid_offset + limit
                    if continuation_mode in {"hybrid", "fallback"}
                    else last["search_rank"]
                ),
                "seq": last["seq"],
            }
            if queries
            else {"timestamp": last["timestamp"], "seq": last["seq"]}
        )
        response["next_cursor"] = _encode_conversation_search_cursor(
            fingerprint,
            bool(queries),
            next_value,
            mode=continuation_mode,
            min_seq=snapshot_min_seq,
            max_seq=snapshot_max_seq,
            embedding_min_seq=snapshot_embedding_min_seq,
            embedding_generation=snapshot_embedding_generation,
            semantic_seqs=snapshot_semantic_seqs,
        )
    elif lexical_tail is not None and page:
        response["next_cursor"] = _encode_conversation_search_cursor(
            fingerprint,
            True,
            lexical_tail,
            mode=lexical_tail_mode,
            min_seq=snapshot_min_seq,
            max_seq=snapshot_max_seq,
            embedding_min_seq=snapshot_embedding_min_seq,
            embedding_generation=snapshot_embedding_generation,
            semantic_seqs=snapshot_semantic_seqs,
        )
    return response


def _hybrid_conversation_rows(
    lexical_rows: list[dict[str, Any]],
    semantic_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fuse bounded candidate sets while favoring exact lexical evidence."""
    by_seq: dict[int, dict[str, Any]] = {}
    scores: dict[int, float] = {}
    for rank, row in enumerate(lexical_rows, start=1):
        seq = int(row["seq"])
        by_seq[seq] = row
        scores[seq] = scores.get(seq, 0.0) + 2.0 / (60 + rank)
    for rank, row in enumerate(semantic_rows, start=1):
        seq = int(row["seq"])
        by_seq.setdefault(seq, row)
        scores[seq] = scores.get(seq, 0.0) + 1.0 / (60 + rank)
    ranked = [dict(row, search_rank=scores[seq]) for seq, row in by_seq.items()]
    ranked.sort(
        key=lambda row: (float(row["search_rank"]), int(row["seq"])),
        reverse=True,
    )
    return ranked


def read_conversation_history(body: Any) -> dict[str, Any]:
    """Validate and execute one public, bounded thread-history request."""
    if not isinstance(body, dict):
        raise ApiError(HTTPStatus.BAD_REQUEST, "conversation read must be an object")
    allowed = {
        "thread_id",
        "before",
        "after",
        "around_event_id",
        "include_activity",
        "limit",
    }
    unexpected = sorted(set(body) - allowed)
    if unexpected:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            f"unsupported conversation read field: {unexpected[0]}",
        )
    thread_id = body.get("thread_id")
    if (
        not isinstance(thread_id, str)
        or PRODUCT_THREAD_ID_RE.fullmatch(thread_id) is None
    ):
        raise ApiError(HTTPStatus.BAD_REQUEST, "thread_id is invalid")
    cursors: dict[str, int] = {}
    for public_name, internal_name in (
        ("before", "before"),
        ("after", "after"),
        ("around_event_id", "around"),
    ):
        value = body.get(public_name)
        if value is None:
            continue
        cursors[internal_name] = _conversation_event_seq(value, public_name)
    if len(cursors) > 1:
        raise ApiError(
            HTTPStatus.BAD_REQUEST,
            "before, after, and around_event_id cannot be combined",
        )
    include_activity = body.get("include_activity", False)
    if not isinstance(include_activity, bool):
        raise ApiError(HTTPStatus.BAD_REQUEST, "include_activity must be a boolean")
    limit = _conversation_limit(body.get("limit", 20), CONVERSATION_READ_LIMIT)
    event_types = CONVERSATION_EVENT_TYPES if include_activity else ("thread.message",)
    if "around" in cursors:
        raw_events = state.page_thread_events_around(
            thread_id,
            cursors["around"],
            limit,
            event_types=event_types,
        )
        if raw_events is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "anchor event not found in thread")
        mode = "around"
    elif "after" in cursors:
        raw_events = state.page_thread_events(
            thread_id,
            cursors["after"],
            limit,
            event_types=event_types,
        )
        mode = "after"
    else:
        raw_events = state.page_thread_events(
            thread_id,
            None,
            limit,
            before=cursors.get("before"),
            event_types=event_types,
        )
        mode = "before"

    projected = [_conversation_event(event) for event in raw_events]
    events = _bounded_conversation_events(projected, mode, cursors.get("around"))
    response: dict[str, Any] = {
        "provenance": HISTORY_PROVENANCE,
        "trust": HISTORY_TRUST,
        "instruction_authority": HISTORY_INSTRUCTION_AUTHORITY,
        "thread": {"thread_id": thread_id},
        "events": events,
        "older_cursor": None,
        "newer_cursor": None,
    }
    if events:
        oldest = _event_seq(events[0]["event_id"])
        newest = _event_seq(events[-1]["event_id"])
        has_older, has_newer = state.thread_event_page_bounds(
            thread_id,
            oldest,
            newest,
            event_types=event_types,
        )
        if has_older:
            response["older_cursor"] = f"event_{oldest}"
        if has_newer:
            response["newer_cursor"] = f"event_{newest}"
    return response


def _conversation_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if event.get("event_type") == "thread.message":
        content = payload.get("message")
        content = content if isinstance(content, str) else ""
        clipped = _clip_json_encoded_text(content, CONVERSATION_MESSAGE_BYTES)
        return {
            "event_id": event["event_id"],
            "timestamp": event["timestamp"],
            "type": "message",
            "role": "user" if payload.get("source") == "user" else "assistant",
            "content": clipped,
            "truncated": clipped != content,
        }
    activity = payload.get("activity")
    activity = activity if isinstance(activity, dict) else {}
    summary: dict[str, Any] = {}
    truncated = False
    for field, budget in CONVERSATION_ACTIVITY_FIELDS:
        value = activity.get(field)
        if not isinstance(value, str) or not value:
            continue
        clipped = _clip_json_encoded_text(value, budget)
        summary[field] = clipped
        truncated = truncated or clipped != value
    return {
        "event_id": event["event_id"],
        "timestamp": event["timestamp"],
        "type": "activity",
        "activity": summary,
        "truncated": truncated or bool(set(activity) - set(summary)),
    }


def _bounded_conversation_events(
    events: list[dict[str, Any]], mode: str, anchor: int | None
) -> list[dict[str, Any]]:
    """Keep a contiguous page within the exact encoded response budget."""
    if not events:
        return []
    if mode == "after":
        order = list(range(len(events)))
    elif mode == "around" and anchor is not None:
        anchor_index = next(
            (index for index, event in enumerate(events) if _event_seq(event["event_id"]) == anchor),
            len(events) - 1,
        )
        order = [anchor_index]
        distance = 1
        while len(order) < len(events):
            if anchor_index - distance >= 0:
                order.append(anchor_index - distance)
            if anchor_index + distance < len(events):
                order.append(anchor_index + distance)
            distance += 1
    else:
        order = list(reversed(range(len(events))))
    selected: set[int] = set()
    for index in order:
        candidate = [events[item] for item in sorted((*selected, index))]
        if len(json.dumps({"events": candidate}).encode()) > CONVERSATION_RESPONSE_BYTES - 4096:
            break
        selected.add(index)
    return [events[index] for index in sorted(selected)]


def _event_seq(event_id: str) -> int:
    return int(event_id.removeprefix("event_"))
