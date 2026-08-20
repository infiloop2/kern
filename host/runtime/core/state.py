"""Normalized host-state accessors and proxy file-path helpers.

Host, network, app-migration, and tool state lives in the local
``kern_admin`` Postgres database (see ``host/migrations/`` for the schema
and ``host.runtime.core.db``/``host.runtime.core.pgclient`` for the Unix-socket client).
This module exposes per-operation queries rather than materializing the full
state. Reads use MVCC snapshots; process-local check-then-act writes use
``mutation()``, while cross-process transitions rely on database constraints
and conditional updates.

Agent runtime statuses deliberately do not live here: runtime status is
in-process memory in ``orchestrator`` (derived health, re-computed within
seconds of startup) and resets with the service.

The proxy and tools services participate under narrow database roles. A
database outage fails closed: the proxy denies every request until the
database returns. Proxy TLS material stays in proxy-owned files because
``ssl`` and OpenSSL consume paths.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import hmac
import secrets
import threading
import time
from typing import Any, Callable, Iterator

from host.runtime.core import db, pgclient, secretbox


DEFAULT_PROXY_STATE_DIR = Path("/mnt/kern-admin/proxy-state")
# Serializes every admin-state write cycle. Private on purpose: writes go only
# through mutation() below, so the locking contract is enforced by structure.
# Three things to know:
# - It is in-process only. The admin port bind permits one admin process, while
#   the proxy and tools processes reach only their granted operations. Postgres
#   transactions and conditional updates carry cross-process correctness; this
#   lock makes a whole check-then-act sequence atomic against sibling threads.
# - It is an RLock so a helper that reads state can be called from inside a
#   mutation() block without deadlocking (reads run on their own database
#   connections and see the last committed state).
# - Nesting: code inside mutation() may take the orchestrator's _LIVE_LOCK
#   (turn admission, steer delivery, stop, finish). Nothing enters mutation()
#   while holding it — keep it that way, or the lock graph grows a cycle.
_MUTATION_LOCK = threading.RLock()
# Conversation history gets a deeper retained window than the high-volume
# network and tool audit logs. Each log prunes every PRUNE_EVERY appends so
# the cost stays amortized.
AGENT_EVENT_LIMIT = 10_000_000
NETWORK_EVENT_LIMIT = 1_000_000
TOOL_EVENT_LIMIT = 1_000_000
PRUNE_EVERY = 500
# Bound each stored message, not just the row count: a pathological multi-
# megabyte streamed message would otherwise grow durable Postgres storage far
# past the apparent AGENT_EVENT_LIMIT cap. The bound sits well above any normal
# assistant message; an over-limit value is truncated with a marker rather
# than dropped so replay stays coherent.
MAX_EVENT_MESSAGE_CHARS = 128 * 1024
# The audit logs page newest-first in pages of EVENT_PAGE_LIMIT rows; the
# limit query parameter can only shrink a page.
EVENT_PAGE_LIMIT = 100
# Host diagnostics are materially larger than ordinary audit rows because
# details may carry a stack trace or provider response. Keep one smaller
# bounded log across errors and warnings.
HOST_DIAGNOSTIC_LIMIT = 10_000
HOST_DIAGNOSTIC_PRUNE_EVERY = 100
HOST_DIAGNOSTIC_COALESCE_SECONDS = 60
ADMIN_PASSKEY_LIMIT = 1
# Resolved GitHub push approvals are useful operator history, but their JSON
# payloads can be much larger than ordinary audit rows. Pending pushes have
# separate admission backpressure and are never removed by retention.
PENDING_PUSH_HISTORY_LIMIT = 100
# The admin UI reads current-month Bedrock totals. Keep a little over a year of
# daily source counters for diagnosis without accumulating one row per model
# per day for the lifetime of the host.
BEDROCK_USAGE_RETAIN_DAYS = 400
# Full-text ranking can otherwise sort up to the complete retained event log
# for a very common token. Keep each relevance query inside a fixed database
# execution budget; callers turn a cancellation into an actionable narrow-
# the-query response.
CONVERSATION_SEARCH_STATEMENT_TIMEOUT_MS = 2_000
_AGENT_HISTORY_COUNTERS = {
    "threads": "agent_history_threads",
    "messages": "agent_history_messages",
    "activities": "agent_history_activities",
}

def _proxy_state_dir() -> Path:
    return Path(os.environ.get("KERN_PROXY_STATE_DIR", str(DEFAULT_PROXY_STATE_DIR)))


@dataclass(frozen=True)
class NetworkProxyCertFiles:
    directory: Path
    cert: Path
    key: Path
    csr: Path
    ext: Path
    ca_cert: Path
    ca_key: Path


def network_proxy_cert_files(host: str) -> NetworkProxyCertFiles:
    safe_host = "".join(char if char.isalnum() or char in ".-" else "_" for char in host)
    directory = _proxy_state_dir() / "generated-certs"
    return NetworkProxyCertFiles(
        directory=directory,
        cert=directory / f"{safe_host}.crt",
        key=directory / f"{safe_host}.key",
        csr=directory / f"{safe_host}.csr",
        ext=directory / f"{safe_host}.ext",
        ca_cert=_proxy_state_dir() / "network_proxy_ca.crt",
        ca_key=_proxy_state_dir() / "network_proxy_ca.key",
    )


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# -- transactions -------------------------------------------------------------


@contextmanager
def mutation(*, after_commit: list[Callable[[], None]] | None = None) -> Iterator[Any]:
    """The single sanctioned way to write admin state: the process-wide
    mutation lock plus one database transaction, yielded as a cursor. The lock
    spans the whole check-then-act cycle so concurrent mutations cannot
    interleave between a read and its dependent write; an exception rolls the
    transaction back. Callers that must publish related in-memory state may
    pass a list and append no-fail callbacks; they run after the database
    commit but before the mutation lock is released. Do slow work (runtime
    spawns, helper subprocesses, process closes) outside this block so reads
    never stall behind it. Plain reads use the read-only accessors below — no
    lock, their own snapshot."""
    callbacks = after_commit if after_commit is not None else []
    with _MUTATION_LOCK:
        with db.transaction() as cur:
            yield cur
        for callback in callbacks:
            callback()


@contextmanager
def _read(cur: Any = None) -> Iterator[Any]:
    """Run with the given cursor (already inside a transaction) or a fresh
    read-only transaction."""
    if cur is not None:
        yield cur
        return
    with db.transaction() as fresh:
        yield fresh


# -- host config ---------------------------------------------------------------


def load_config() -> dict[str, Any]:
    """The host config as a dict: the singleton config row plus the operator
    connection rows, with absent values omitted."""
    config: dict[str, Any] = {}
    with db.transaction() as cur:
        cur.execute("SELECT agent_name, admin_password_sha256 FROM config")
        row = cur.fetchone()
        if row:
            if row[0] is not None:
                config["agent_name"] = row[0]
            if row[1] is not None:
                config["admin_password_sha256"] = row[1]
        cur.execute(
            "SELECT mode, ssh_public_key, hostname, tunnel_token"
            " FROM operator_connections ORDER BY mode"
        )
        connections = []
        for mode, ssh_public_key, hostname, tunnel_token in cur.fetchall():
            connection: dict[str, Any] = {"mode": mode}
            if ssh_public_key is not None:
                connection["ssh_public_key"] = ssh_public_key
            if hostname is not None:
                connection["hostname"] = hostname
            if tunnel_token is not None:
                connection["tunnel_token"] = secretbox.decrypt(tunnel_token)
            connections.append(connection)
        if connections:
            config["operator_connections"] = connections
    return config


def load_admin_password_hash() -> str:
    """Just the admin password hash from the config row, with no operator
    connections loaded and no tunnel-token decryption. The admin API caches this
    at startup so the login path does no per-request database work (reconfigure
    restarts the service, which reloads it)."""
    with db.transaction() as cur:
        cur.execute("SELECT admin_password_sha256 FROM config")
        row = cur.fetchone()
    return row[0] if row and row[0] is not None else ""


def load_cloudflare_hostname() -> str | None:
    """The configured public admin hostname without decrypting its tunnel
    token. WebAuthn uses this exact value as both RP ID and expected origin."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT hostname FROM operator_connections"
            " WHERE mode = 'cloudflare_tunnel'"
        )
        row = cur.fetchone()
    return str(row[0]) if row and row[0] is not None else None


def save_config(config: dict[str, Any]) -> None:
    """Replace the whole host config, the way deploy refreshes it. The table
    constraints validate field formats and per-mode shapes; write_config
    performs the friendlier completeness validation before calling this."""
    with mutation() as cur:
        cur.execute("DELETE FROM operator_connections")
        cur.execute("DELETE FROM config")
        agent_name = config.get("agent_name")
        admin_password_sha256 = config.get("admin_password_sha256")
        if agent_name is not None or admin_password_sha256 is not None:
            cur.execute(
                "INSERT INTO config (agent_name, admin_password_sha256) VALUES (%s, %s)",
                (agent_name, admin_password_sha256),
            )
        for connection in config.get("operator_connections") or []:
            cur.execute(
                "INSERT INTO operator_connections (mode, ssh_public_key, hostname, tunnel_token)"
                " VALUES (%s, %s, %s, %s)",
                (
                    connection.get("mode"),
                    connection.get("ssh_public_key"),
                    connection.get("hostname"),
                    _encrypt_secret(connection.get("tunnel_token")),
                ),
            )


# -- admin passkeys ------------------------------------------------------------


def admin_passkey_config() -> dict[str, Any] | None:
    with db.transaction() as cur:
        cur.execute(
            "SELECT user_handle, created_at FROM admin_passkey_config"
            " WHERE singleton = TRUE"
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"user_handle": row[0], "created_at": row[1]}


def admin_passkeys(rp_id: str | None = None) -> list[dict[str, Any]]:
    sql = (
        "SELECT credential_id, rp_id, public_key_spki, sign_count, transports,"
        " backed_up, created_at, last_used_at FROM admin_passkeys"
    )
    params: tuple[Any, ...] = ()
    if rp_id is not None:
        sql += " WHERE rp_id = %s"
        params = (rp_id,)
    sql += " ORDER BY created_at, credential_id"
    with db.transaction() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [
        {
            "credential_id": row[0],
            "rp_id": row[1],
            "public_key_spki": row[2],
            "sign_count": row[3],
            "transports": row[4],
            "backed_up": row[5],
            "created_at": row[6],
            "last_used_at": row[7],
        }
        for row in rows
    ]


class AdminPasskeyLimitError(ValueError):
    """The single administrator already has a durable passkey."""


def save_admin_passkey(
    *,
    user_handle: str,
    credential_id: str,
    rp_id: str,
    public_key_spki: str,
    sign_count: int,
    transports: list[str],
    backed_up: bool,
    created_at: str,
) -> None:
    """Create one credential and its singleton user handle atomically."""
    with mutation() as cur:
        cur.execute("SELECT COUNT(*) FROM admin_passkeys")
        count_row = cur.fetchone()
        if count_row is not None and int(count_row[0]) >= ADMIN_PASSKEY_LIMIT:
            raise AdminPasskeyLimitError("admin passkey limit reached")
        cur.execute(
            "INSERT INTO admin_passkey_config (singleton, user_handle, created_at)"
            " VALUES (TRUE, %s, %s)"
            " ON CONFLICT (singleton) DO NOTHING",
            (user_handle, created_at),
        )
        cur.execute(
            "SELECT user_handle FROM admin_passkey_config WHERE singleton = TRUE"
        )
        row = cur.fetchone()
        if row is None or row[0] != user_handle:
            raise ValueError("admin passkey user handle changed during registration")
        cur.execute(
            "INSERT INTO admin_passkeys"
            " (credential_id, rp_id, public_key_spki, sign_count, transports,"
            " backed_up, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                credential_id,
                rp_id,
                public_key_spki,
                sign_count,
                db.jsonb(transports),
                backed_up,
                created_at,
            ),
        )


def mark_admin_passkey_used(
    credential_id: str,
    *,
    previous_sign_count: int,
    sign_count: int,
    backed_up: bool,
    used_at: str,
) -> bool:
    """Advance one credential after an assertion. The previous counter is in
    the predicate so concurrent replay can never make both requests succeed."""
    with mutation() as cur:
        cur.execute(
            "UPDATE admin_passkeys"
            " SET sign_count = %s, backed_up = %s, last_used_at = %s"
            " WHERE credential_id = %s AND sign_count = %s"
            " RETURNING credential_id",
            (
                sign_count,
                backed_up,
                used_at,
                credential_id,
                previous_sign_count,
            ),
        )
        return cur.fetchone() is not None


def reset_admin_passkeys() -> int:
    """Root-reconfigure recovery: remove every public credential and its user
    handle. Returns the number of credentials removed."""
    with mutation() as cur:
        cur.execute("SELECT COUNT(*) FROM admin_passkeys")
        row = cur.fetchone()
        count = int(row[0]) if row else 0
        cur.execute("DELETE FROM admin_passkeys")
        cur.execute("DELETE FROM admin_passkey_config")
    return count


# -- threads --------------------------------------------------------------------


def _increment_counter(cur: Any, name: str) -> None:
    cur.execute(
        "UPDATE counters SET value = value + 1 WHERE name = %s RETURNING value",
        (name,),
    )
    if cur.fetchone() is None:
        raise RuntimeError(f"counter {name!r} is not initialized")


def agent_history_counts() -> dict[str, int]:
    """Monotonic thread, user-message, and agent-activity totals shown on Home."""
    names = tuple(_AGENT_HISTORY_COUNTERS.values())
    with db.transaction() as cur:
        cur.execute(
            "SELECT name, value FROM counters WHERE name IN (%s, %s, %s)",
            names,
        )
        stored = {str(name): int(value) for name, value in cur.fetchall()}
    if stored.keys() != set(names):
        missing = sorted(set(names) - stored.keys())
        raise RuntimeError(f"agent history counters are not initialized: {missing}")
    return {field: stored[name] for field, name in _AGENT_HISTORY_COUNTERS.items()}


def page_thread_summaries(
    before: tuple[str, str] | None,
    limit: int,
    *,
    thread_prefix: str | None = None,
) -> list[dict[str, Any]]:
    """One newest-first keyset page of canonical thread session rows.

    ``before`` is the last page's ``(last_used_at, thread_id)`` sort key. A
    caller may request a prefix filter as a query optimization; it conveys no
    ownership or authorization.
    """
    clauses: list[str] = []
    params: list[Any] = []
    if thread_prefix is not None:
        clauses.append("LEFT(thread_id, %s) = %s")
        params.extend((len(thread_prefix), thread_prefix))
    if before is not None:
        clauses.append("(COALESCE(last_used_at, ''), thread_id) < (%s, %s)")
        params.extend(before)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with db.transaction() as cur:
        cur.execute(
            "SELECT thread_id, agent_runtime, model, effort, last_used_at, run_status,"
            " COALESCE((SELECT seq FROM agent_events"
            " WHERE agent_events.thread_id = thread_sessions.thread_id"
            " ORDER BY seq DESC LIMIT 1), 0),"
            " COALESCE((SELECT seq FROM agent_events"
            " WHERE agent_events.thread_id = thread_sessions.thread_id"
            " AND event_type = 'thread.message'"
            " ORDER BY seq DESC LIMIT 1), 0)"
            f" FROM thread_sessions{where}"
            " ORDER BY COALESCE(last_used_at, '') DESC, thread_id DESC LIMIT %s",
            (*params, limit),
        )
        return [
            {
                "thread_id": str(thread_id),
                "agent_runtime": agent_runtime,
                "model": model,
                "effort": effort,
                "last_used_at": last_used_at or "",
                "status": str(run_status),
                "latest_event_seq": int(latest_event_seq),
                "latest_message_seq": int(latest_message_seq),
            }
            for (
                thread_id,
                agent_runtime,
                model,
                effort,
                last_used_at,
                run_status,
                latest_event_seq,
                latest_message_seq,
            ) in cur.fetchall()
        ]


def latest_thread_event_seqs(thread_id: str) -> tuple[int, int]:
    """Newest retained event and message sequences from one database snapshot."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT"
            " COALESCE((SELECT seq FROM agent_events WHERE thread_id = %s"
            " ORDER BY seq DESC LIMIT 1), 0),"
            " COALESCE((SELECT seq FROM agent_events WHERE thread_id = %s"
            " AND event_type = 'thread.message' ORDER BY seq DESC LIMIT 1), 0)",
            (thread_id, thread_id),
        )
        row = cur.fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def recover_interrupted_thread_runs(cur: Any) -> list[tuple[str, int]]:
    """Return stale running threads to idle and return their private run ids.

    The admin process is the sole run owner, so every persisted ``running`` row
    at its startup belongs to a process that died before it could settle.
    """
    cur.execute(
        "UPDATE thread_sessions SET run_status = 'idle'"
        " WHERE run_status = 'running'"
        " RETURNING thread_id, run_number"
    )
    return [(str(thread_id), int(run_number)) for thread_id, run_number in cur.fetchall()]


# -- thread -> provider session maps ---------------------------------------------


def save_thread_session(
    cur: Any,
    runtime: str,
    thread_id: str,
    provider_session_id: str | None,
    last_used_at: str | None,
    model: str,
    effort: str,
) -> None:
    # The admin process is the sole thread-session writer and callers hold the
    # mutation lock, so this pre-read and the insert/update below are one
    # creation decision. Count only the first durable row for a thread.
    cur.execute("SELECT 1 FROM thread_sessions WHERE thread_id = %s", (thread_id,))
    creating = cur.fetchone() is None
    cur.execute(
        "INSERT INTO thread_sessions (agent_runtime, thread_id, provider_session_id, last_used_at, model, effort)"
        " VALUES (%s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (thread_id) DO UPDATE SET"
        " provider_session_id = EXCLUDED.provider_session_id,"
        " last_used_at = EXCLUDED.last_used_at"
        " WHERE thread_sessions.agent_runtime = EXCLUDED.agent_runtime"
        " AND thread_sessions.model = EXCLUDED.model"
        " AND thread_sessions.effort = EXCLUDED.effort"
        " RETURNING 1",
        (runtime, thread_id, provider_session_id, last_used_at, model, effort),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} already has another session configuration")
    if creating:
        _increment_counter(cur, _AGENT_HISTORY_COUNTERS["threads"])


def rotate_thread_session(
    cur: Any,
    thread_id: str,
    runtime: str,
    model: str,
    effort: str,
    last_used_at: str,
) -> None:
    """Replace one idle thread's provider configuration.

    Clearing the provider session makes the next run a new provider
    conversation. The idle predicate is the durable race fence: a caller
    cannot switch configuration underneath admitted work.
    """
    cur.execute(
        "UPDATE thread_sessions SET"
        " agent_runtime = %s,"
        " provider_session_id = NULL,"
        " last_used_at = %s,"
        " model = %s,"
        " effort = %s"
        " WHERE thread_id = %s AND run_status = 'idle'"
        " RETURNING 1",
        (runtime, last_used_at, model, effort, thread_id),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} is running or does not exist")


def clear_thread_context(
    cur: Any,
    thread_id: str,
    cleared_seq: int,
    last_used_at: str,
) -> None:
    """Drop one idle thread's provider session and fence its handoff history.

    The next run opens a new provider conversation. ``cleared_seq`` becomes the
    handoff floor so that run is not handed back the events the operator just
    cleared; without it, the missing provider session would itself trigger the
    replay. Nothing is deleted: the events stay readable and only stop being
    forwarded. The idle predicate is the durable race fence, and the floor
    never moves backwards, so a stale caller cannot restore cleared context.
    """
    cur.execute(
        "UPDATE thread_sessions SET"
        " provider_session_id = NULL,"
        " context_cleared_seq = GREATEST(context_cleared_seq, %s),"
        " last_used_at = %s"
        " WHERE thread_id = %s AND run_status = 'idle'"
        " RETURNING 1",
        (cleared_seq, last_used_at, thread_id),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} is running or does not exist")


def save_thread_provider_session(
    cur: Any,
    thread_id: str,
    run_number: int,
    provider_session_id: str,
) -> None:
    """Persist a provider-confirmed non-empty session for exactly one run.

    Matching ``run_number`` prevents a late callback from an old process from
    replacing a newer run's mapping. The lifecycle may already be durably idle
    while its process is finishing, so this deliberately does not require
    ``run_status = 'running'``.
    """
    if not isinstance(provider_session_id, str) or not provider_session_id.strip():
        raise ValueError("provider session id must not be empty")
    provider_session_id = provider_session_id.strip()
    cur.execute(
        "UPDATE thread_sessions SET provider_session_id = %s"
        " WHERE thread_id = %s AND run_number = %s"
        " RETURNING 1",
        (provider_session_id, thread_id, run_number),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} run {run_number} does not exist")


def clear_thread_provider_session(
    cur: Any,
    thread_id: str,
    run_number: int,
    provider_session_id: str,
) -> None:
    """Clear one provider session after the provider confirms it is missing."""
    cur.execute(
        "UPDATE thread_sessions SET provider_session_id = NULL"
        " WHERE thread_id = %s AND run_number = %s AND provider_session_id = %s"
        " RETURNING 1",
        (thread_id, run_number, provider_session_id),
    )
    if cur.fetchone() is None:
        raise ValueError(
            f"thread {thread_id!r} run {run_number} no longer has provider session"
            f" {provider_session_id!r}"
        )


def touch_thread_session(cur: Any, thread_id: str, last_used_at: str) -> None:
    """Refresh one existing thread's recency without changing its provider
    session or current runtime configuration."""
    cur.execute(
        "UPDATE thread_sessions SET last_used_at = %s"
        " WHERE thread_id = %s RETURNING 1",
        (last_used_at, thread_id),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} has no session configuration")


def start_thread_run(cur: Any, thread_id: str) -> int:
    """Atomically mark an idle thread running and allocate its private scope."""
    cur.execute(
        "UPDATE thread_sessions"
        " SET run_status = 'running', run_number = run_number + 1"
        " WHERE thread_id = %s AND run_status = 'idle'"
        " RETURNING run_number",
        (thread_id,),
    )
    row = cur.fetchone()
    if row is None:
        raise ValueError(f"thread {thread_id!r} is already running or does not exist")
    return int(row[0])


def finish_thread_run(cur: Any, thread_id: str, run_number: int) -> None:
    """Return exactly the matching live run to idle."""
    cur.execute(
        "UPDATE thread_sessions SET run_status = 'idle'"
        " WHERE thread_id = %s AND run_status = 'running' AND run_number = %s"
        " RETURNING 1",
        (thread_id, run_number),
    )
    if cur.fetchone() is None:
        raise ValueError(f"thread {thread_id!r} run {run_number} is not running")


def thread_session_config(thread_id: str, cur: Any = None) -> dict[str, Any] | None:
    with _read(cur) as cur:
        cur.execute(
            "SELECT agent_runtime, provider_session_id, last_used_at, model, effort,"
            " run_status, run_number, context_cleared_seq"
            " FROM thread_sessions WHERE thread_id = %s",
            (thread_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "agent_runtime": str(row[0]),
        "provider_session_id": row[1],
        "last_used_at": row[2],
        "model": str(row[3]),
        "effort": str(row[4]),
        "status": str(row[5]),
        "run_number": int(row[6]),
        "context_cleared_seq": int(row[7]),
    }


def prune_thread_sessions(cur: Any, runtime: str, keep: int) -> None:
    """Drop least-recently-used unreferenced threads beyond ``keep``.

    A thread with retained events keeps its canonical row so its history stays
    listed; once event retention drops a thread's last event, the ordinary LRU
    cap applies.
    """
    cur.execute(
        "DELETE FROM thread_sessions AS candidate"
        " WHERE candidate.agent_runtime = %s"
        " AND NOT EXISTS (SELECT 1 FROM agent_events WHERE agent_events.thread_id = candidate.thread_id)"
        " AND candidate.thread_id NOT IN ("
        "  SELECT retained.thread_id FROM thread_sessions AS retained"
        "  WHERE retained.agent_runtime = %s"
        "  AND NOT EXISTS (SELECT 1 FROM agent_events WHERE agent_events.thread_id = retained.thread_id)"
        "  ORDER BY retained.last_used_at DESC NULLS LAST, retained.thread_id LIMIT %s)",
        (runtime, runtime, keep),
    )


# -- OAuth logins -------------------------------------------------------------------


_OAUTH_COLUMNS = ("status", "login_url", "expires_at", "device_code", "login_id", "access_token_sha256")


def oauth_login(key: str, cur: Any = None) -> dict[str, Any] | None:
    """The in-flight login record for ``codex`` or ``claude``, or None."""
    with _read(cur) as cur:
        cur.execute(
            f"SELECT {', '.join(_OAUTH_COLUMNS)} FROM oauth_logins WHERE runtime = %s", (key,)
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {column: value for column, value in zip(_OAUTH_COLUMNS, row) if value is not None}


def set_oauth_login(cur: Any, key: str, data: dict[str, Any] | None) -> None:
    if data is None:
        cur.execute("DELETE FROM oauth_logins WHERE runtime = %s", (key,))
        return
    unknown = set(data) - set(_OAUTH_COLUMNS)
    if unknown:
        raise ValueError(f"unsupported oauth login keys: {sorted(unknown)}")
    cur.execute(
        "INSERT INTO oauth_logins (runtime, status, login_url, expires_at, device_code, login_id, access_token_sha256)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (runtime) DO UPDATE SET status = EXCLUDED.status,"
        " login_url = EXCLUDED.login_url, expires_at = EXCLUDED.expires_at,"
        " device_code = EXCLUDED.device_code, login_id = EXCLUDED.login_id,"
        " access_token_sha256 = EXCLUDED.access_token_sha256",
        (key, *(data.get(column) for column in _OAUTH_COLUMNS)),
    )


# -- provider account records ---------------------------------------------------------


def save_openai_account(account: dict[str, Any] | None, cur: Any = None) -> None:
    _save_provider_account("openai", account if account is not None else {"account_id": None}, cur)


def read_openai_account(cur: Any = None) -> dict[str, Any]:
    value = _read_provider_account("openai", cur)
    return value if isinstance(value, dict) else {}


def save_claude_account(account: dict[str, Any] | None, cur: Any = None) -> None:
    _save_provider_account("claude", account or {}, cur)


def read_claude_account(cur: Any = None) -> dict[str, Any]:
    value = _read_provider_account("claude", cur)
    return value if isinstance(value, dict) else {}


def save_bedrock_account(account: dict[str, Any] | None, cur: Any = None) -> None:
    """Cache the AWS-attested identity for Hermes."""
    _save_provider_account("bedrock", account or {}, cur)


def read_bedrock_account(cur: Any = None) -> dict[str, Any]:
    value = _read_provider_account("bedrock", cur)
    return value if isinstance(value, dict) else {}


# -- Bedrock connected credential (one admin-written, proxy-readable row) ------------


def save_bedrock_credential(access_key_id: str, secret_access_key: str, region: str, cur: Any) -> None:
    """Store a synchronously validated AWS key pair."""
    cur.execute(
        "INSERT INTO bedrock_credentials (singleton, access_key_id, secret_access_key_encrypted, region)"
        " VALUES (TRUE, %s, %s, %s)"
        " ON CONFLICT (singleton) DO UPDATE SET access_key_id = EXCLUDED.access_key_id,"
        " secret_access_key_encrypted = EXCLUDED.secret_access_key_encrypted, region = EXCLUDED.region",
        (access_key_id, secretbox.encrypt(secret_access_key), region),
    )


def delete_bedrock_credential(cur: Any) -> None:
    cur.execute("DELETE FROM bedrock_credentials")


def read_bedrock_access_key_id(cur: Any = None) -> str | None:
    """The connected access key id (not secret), or None. Used to report
    whether a credential is connected without decrypting the secret."""
    with _read(cur) as cur:
        cur.execute("SELECT access_key_id FROM bedrock_credentials WHERE singleton = TRUE")
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def read_bedrock_credential_secret() -> tuple[str, str] | None:
    """The connected (access_key_id, secret_access_key) with the secret
    decrypted, or None. Called only in the admin service (which owns the
    secretbox key) to hand the plaintext to the root helper through its
    environment. The plaintext never touches disk."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT access_key_id, secret_access_key_encrypted FROM bedrock_credentials"
            " WHERE singleton = TRUE"
        )
        row = cur.fetchone()
    if row is None or not row[0] or not row[1]:
        return None
    return str(row[0]), secretbox.decrypt(str(row[1]))


def _save_provider_account(provider: str, data: dict[str, Any], cur: Any = None) -> None:
    # account_id is a typed column; the rest is the provider CLI's own shape,
    # cached verbatim as metadata.
    if cur is None:
        with mutation() as fresh:
            _save_provider_account(provider, data, fresh)
        return
    metadata = {key: value for key, value in data.items() if key != "account_id"}
    cur.execute(
        "INSERT INTO provider_accounts (provider, account_id, metadata) VALUES (%s, %s, %s)"
        " ON CONFLICT (provider) DO UPDATE SET account_id = EXCLUDED.account_id,"
        " metadata = EXCLUDED.metadata",
        (provider, data.get("account_id"), db.jsonb(metadata)),
    )


def _read_provider_account(provider: str, cur: Any = None) -> dict[str, Any]:
    with _read(cur) as cur:
        cur.execute(
            "SELECT account_id, metadata FROM provider_accounts WHERE provider = %s", (provider,)
        )
        row = cur.fetchone()
    if row is None:
        return {}
    account: dict[str, Any] = dict(row[1]) if isinstance(row[1], dict) else {}
    if row[0] is not None:
        account["account_id"] = row[0]
    return account


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
) -> list[dict[str, Any]]:
    """Search retained thread messages with indexed relevance or time paging.

    Natural-language variants are ORed into one ``tsquery``. The caller owns
    validation and cursor mode; this accessor keeps every filter parameterized
    and returns one extra row when asked so it never needs to count all hits.
    """
    clauses = ["events.event_type = 'thread.message'", "events.message IS NOT NULL"]
    params: list[Any] = []
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


# -- network policy and proxy account pins (admin writes, proxy reads) ---------------


def network_policy_record() -> dict[str, Any] | None:
    """The stored policy assembled back into the operator-facing shape:
    ``{"controls": ..., "updated_at": ...}``, or None when nothing was ever
    stored (the fail-closed empty default)."""
    with db.transaction() as cur:
        # One snapshot for every SELECT: under the default READ COMMITTED a
        # concurrent policy replace could commit between statements and this
        # read would recombine two policies (for example old allowed methods
        # with new missing path guards).
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cur.execute("SELECT updated_at FROM network_policy")
        row = cur.fetchone()
        if row is None:
            return None
        updated_at = row[0]
        cur.execute("SELECT integration FROM managed_integrations")
        integrations: dict[str, dict[str, Any]] = {
            str(integration): {"enabled": True} for (integration,) in cur.fetchall()
        }
        cur.execute("SELECT owner, repo FROM github_repositories ORDER BY position")
        write_repositories = [{"owner": str(owner), "repo": str(repo)} for owner, repo in cur.fetchall()]
        # Validation guarantees write repositories exist only while GitHub is
        # enabled, so a row without the enabled integration is unreachable.
        if write_repositories and "github" in integrations:
            integrations["github"]["write_repositories"] = write_repositories
        cur.execute("SELECT require_dot_github_approval FROM github_settings")
        settings_row = cur.fetchone()
        if settings_row and settings_row[0] and "github" in integrations:
            integrations["github"]["require_dot_github_approval"] = True
        cur.execute("SELECT web_search FROM claude_settings")
        claude_row = cur.fetchone()
        if claude_row and claude_row[0] and "claude" in integrations:
            integrations["claude"]["web_search"] = True
        allowed: dict[str, dict[str, Any]] = {}
        cur.execute("SELECT domain, allow_websocket FROM allowed_domains ORDER BY domain")
        for domain, allow_websocket in cur.fetchall():
            allowed[str(domain)] = {"allow_http_methods": []}
            if allow_websocket:
                allowed[str(domain)]["allow_websocket"] = True
        cur.execute("SELECT domain, method FROM domain_methods ORDER BY domain, position")
        for domain, method in cur.fetchall():
            allowed[str(domain)]["allow_http_methods"].append(method)
        cur.execute("SELECT domain, pattern FROM domain_path_guards ORDER BY domain, position")
        for domain, pattern in cur.fetchall():
            allowed[str(domain)].setdefault("path_guards", []).append(pattern)
        if allowed:
            integrations["custom"] = {"domains": allowed}
    return {
        "controls": {"network_integrations": integrations},
        "updated_at": updated_at,
    }


def read_claude_web_search() -> bool:
    """Whether the operator enabled Anthropic server-side web search for Claude
    Code. Read by the orchestrator to tell the root launcher whether to expose
    the WebSearch tool; the proxy enforces the same toggle independently."""
    with db.transaction() as cur:
        cur.execute("SELECT web_search FROM claude_settings")
        row = cur.fetchone()
        return bool(row and row[0])


# -- Bedrock live usage (proxy-written token counters) ------------------------

_BEDROCK_USAGE_COUNTERS = (
    "metered_requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


def record_bedrock_usage(
    model_id: str, usage: dict[str, int] | None, cost_usd: float
) -> None:
    """Add one allowed Bedrock invocation to its (model, UTC day)
    counter row. Runs in the proxy process under its own database role.
    ``usage`` is the token usage AWS reported in the response, or None when
    the response carried none (an AWS error, or a shape the meter could not
    parse) — the request still counts, so undercounting stays visible as
    ``requests`` without ``metered_requests``. ``cost_usd`` is the USD the
    proxy priced this response at; it is stored, not recomputed at read time,
    so a later rate edit never rewrites history. ``model_id`` is already
    normalized to the catalog (unknown models collapse into one bucket), which
    bounds the row count.

    The single-statement ``INSERT ... ON CONFLICT DO UPDATE`` is atomic: on a
    conflict Postgres takes a row lock and applies the ``col = col + EXCLUDED``
    increments under it, so concurrent proxy writes to the same row serialize
    and simply sum — no read-modify-write race in application code."""
    counters = {column: 0 for column in _BEDROCK_USAGE_COUNTERS}
    if usage is not None:
        counters["metered_requests"] = 1
        for column in _BEDROCK_USAGE_COUNTERS[1:]:
            counters[column] = int(usage.get(column, 0))
    # An unmetered response carries no priced cost regardless of the argument.
    cost = cost_usd if usage is not None else 0.0
    columns = (*_BEDROCK_USAGE_COUNTERS, "cost_usd")
    assignments = ", ".join(
        f"{column} = bedrock_usage.{column} + EXCLUDED.{column}"
        for column in ("requests", *columns)
    )
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO bedrock_usage (model_id, day, requests, "
            + ", ".join(columns)
            + ") VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (model_id, day) DO UPDATE SET " + assignments,
            (
                model_id,
                time.strftime("%Y-%m-%d", time.gmtime()),
                *(counters[column] for column in _BEDROCK_USAGE_COUNTERS),
                cost,
            ),
        )


def read_bedrock_usage(since_day: str) -> list[dict[str, Any]]:
    """Per-model counter totals for UTC days >= ``since_day``
    (an ISO date, typically the first of the current month). ``cost_usd`` is
    the summed recorded cost — the final figure, not a re-priced estimate."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT model_id, SUM(requests), "
            + ", ".join(f"SUM({column})" for column in _BEDROCK_USAGE_COUNTERS)
            + ", SUM(cost_usd)"
            + " FROM bedrock_usage WHERE day >= %s GROUP BY model_id"
            " ORDER BY model_id",
            (since_day,),
        )
        rows = cur.fetchall()
    return [
        {
            "model_id": str(row[0]),
            "requests": int(row[1]),
            **{column: int(row[2 + index]) for index, column in enumerate(_BEDROCK_USAGE_COUNTERS)},
            "cost_usd": float(row[2 + len(_BEDROCK_USAGE_COUNTERS)]),
        }
        for row in rows
    ]


def prune_bedrock_usage(cur: Any, cutoff_day: str) -> None:
    """Drop daily counters older than the configured reporting horizon."""
    cur.execute("DELETE FROM bedrock_usage WHERE day < %s", (cutoff_day,))


def read_bedrock_region() -> str | None:
    """The shared operator-configured Bedrock region, or None without a
    connected credential. Read by the orchestrator to tell each Bedrock
    launcher which regional endpoint to use; the proxy enforces the
    same region independently."""
    with db.transaction() as cur:
        cur.execute("SELECT region FROM bedrock_credentials WHERE singleton = TRUE")
        row = cur.fetchone()
        return str(row[0]) if row and row[0] else None


def save_network_policy(controls: dict[str, Any], updated_at: str) -> None:
    """Replace the active policy in one transaction (admin service only; the
    proxy role can only read these tables). ``controls`` is the already
    validated operator-facing shape from host.config."""
    with mutation() as cur:
        cur.execute("DELETE FROM domain_path_guards")
        cur.execute("DELETE FROM domain_methods")
        cur.execute("DELETE FROM allowed_domains")
        cur.execute("DELETE FROM github_repositories")
        cur.execute("DELETE FROM github_settings")
        cur.execute("DELETE FROM claude_settings")
        cur.execute("DELETE FROM managed_integrations")
        cur.execute(
            "INSERT INTO network_policy (singleton, updated_at) VALUES (TRUE, %s)"
            " ON CONFLICT (singleton) DO UPDATE SET updated_at = EXCLUDED.updated_at",
            (updated_at,),
        )
        integrations = controls.get("network_integrations") or {}
        for integration, value in integrations.items():
            if integration == "custom":
                continue  # custom's domains live in the domain tables below
            if isinstance(value, dict) and value.get("enabled") is True:
                cur.execute("INSERT INTO managed_integrations (integration) VALUES (%s)", (integration,))
        github = integrations.get("github")
        if isinstance(github, dict):
            for position, repository in enumerate(github.get("write_repositories") or []):
                cur.execute(
                    "INSERT INTO github_repositories (position, owner, repo) VALUES (%s, %s, %s)",
                    (position, repository["owner"], repository["repo"]),
                )
            if github.get("require_dot_github_approval") is True:
                cur.execute(
                    "INSERT INTO github_settings (singleton, require_dot_github_approval) VALUES (TRUE, TRUE)"
                )
        claude = integrations.get("claude")
        if isinstance(claude, dict) and claude.get("web_search") is True:
            cur.execute("INSERT INTO claude_settings (singleton, web_search) VALUES (TRUE, TRUE)")
        custom = integrations.get("custom")
        custom_domains = custom.get("domains") if isinstance(custom, dict) else {}
        for domain, rule in (custom_domains or {}).items():
            cur.execute(
                "INSERT INTO allowed_domains (domain, allow_websocket) VALUES (%s, %s)",
                (domain, rule.get("allow_websocket") is True),
            )
            for position, method in enumerate(rule.get("allow_http_methods") or []):
                cur.execute(
                    "INSERT INTO domain_methods (domain, position, method) VALUES (%s, %s, %s)",
                    (domain, position, method),
                )
            for position, pattern in enumerate(rule.get("path_guards") or []):
                cur.execute(
                    "INSERT INTO domain_path_guards (domain, position, pattern) VALUES (%s, %s, %s)",
                    (domain, position, pattern),
                )


def save_proxy_openai_account_id(account_id: str | None, cur: Any = None) -> None:
    _save_proxy_account_id("openai", account_id, cur)


def read_proxy_openai_account_id() -> str | None:
    value = _read_proxy_pin("openai").get("account_id")
    return value if isinstance(value, str) and value else None


def save_proxy_claude_account_id(account_id: str | None, cur: Any = None) -> None:
    _save_proxy_account_id("claude", account_id, cur)


def read_proxy_claude_account_id() -> str | None:
    value = _read_proxy_pin("claude").get("account_id")
    return value if isinstance(value, str) and value else None


_bedrock_proxy_credential_cache: tuple[str, str, str, str] | None = None


def read_bedrock_proxy_credential() -> tuple[str, str, str] | None:
    """Read the one shared row for the trusted network proxy.

    Enablement is already present in the parsed proxy policy. Decryption is
    cached per ciphertext so an enabled steady-state request costs one SELECT,
    like the proxy GitHub token.
    """
    with db.transaction() as cur:
        cur.execute(
            "SELECT access_key_id, secret_access_key_encrypted, region FROM bedrock_credentials"
            " WHERE singleton = TRUE"
        )
        row = cur.fetchone()
    if row is None:
        global _bedrock_proxy_credential_cache
        _bedrock_proxy_credential_cache = None
        return None
    access_key_id, ciphertext, region = str(row[0]), str(row[1]), str(row[2])
    cached = _bedrock_proxy_credential_cache
    if cached is not None and cached[0] == ciphertext:
        return cached[1], cached[2], cached[3]
    secret = secretbox.decrypt(ciphertext)
    _bedrock_proxy_credential_cache = (ciphertext, access_key_id, secret, region)
    return access_key_id, secret, region


def _save_proxy_account_id(provider: str, account_id: str | None, cur: Any = None) -> None:
    """Set or clear the account identity currently allowed through the proxy.

    This temporary authorization row is deliberately separate from the
    user-controlled account anchor in ``provider_accounts`` and from the
    candidate credential observed in the agent user's auth files.
    """
    if cur is None:
        with mutation() as fresh:
            _save_proxy_account_id(provider, account_id, fresh)
        return
    cur.execute(
        "INSERT INTO proxy_provider_pins (provider, account_id) VALUES (%s, %s)"
        " ON CONFLICT (provider) DO UPDATE SET account_id = EXCLUDED.account_id",
        (provider, account_id),
    )


def _read_proxy_pin(provider: str) -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute(
            "SELECT account_id FROM proxy_provider_pins WHERE provider = %s",
            (provider,),
        )
        row = cur.fetchone()
    if row is None:
        return {}
    return {"account_id": row[0]} if row[0] is not None else {}


# -- github credential (admin only; the proxy role has no grant on it) ----------------


_GITHUB_CREDENTIAL_COLUMNS = (
    "mode",
    "token",
    "app_id",
    "installation_id",
    "private_key_pem",
    "updated_at",
    "validation",
)
_GITHUB_CREDENTIAL_SECRET_COLUMNS = ("token", "private_key_pem")


def _encrypt_secret(value: Any) -> str | None:
    """Encrypt a secret column value. Secrets are either absent or non-empty
    strings — anything else is a programming error, refused loudly rather
    than ever stored unencrypted."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("stored secrets must be non-empty strings")
    return secretbox.encrypt(value)


def read_github_credential() -> dict[str, Any]:
    with db.transaction() as cur:
        cur.execute(f"SELECT {', '.join(_GITHUB_CREDENTIAL_COLUMNS)} FROM github_credential")
        row = cur.fetchone()
    if row is None:
        return {}
    credential = {column: value for column, value in zip(_GITHUB_CREDENTIAL_COLUMNS, row) if value is not None}
    for column in _GITHUB_CREDENTIAL_SECRET_COLUMNS:
        if isinstance(credential.get(column), str):
            credential[column] = secretbox.decrypt(credential[column])
    return credential


def save_github_credential(credential: dict[str, Any] | None) -> None:
    """Replace or clear the single fixed GitHub credential row."""
    with mutation() as cur:
        cur.execute("DELETE FROM github_credential")
        if credential is None:
            return
        cur.execute(
            f"INSERT INTO github_credential (singleton, {', '.join(_GITHUB_CREDENTIAL_COLUMNS)})"
            f" VALUES (TRUE, {', '.join(['%s'] * len(_GITHUB_CREDENTIAL_COLUMNS))})",
            tuple(
                db.jsonb(credential.get(column) or {})
                if column == "validation"
                else _encrypt_secret(credential.get(column))
                if column in _GITHUB_CREDENTIAL_SECRET_COLUMNS
                else credential.get(column)
                for column in _GITHUB_CREDENTIAL_COLUMNS
            ),
        )


def set_github_credential_validation(validation: dict[str, Any]) -> None:
    with mutation() as cur:
        cur.execute("UPDATE github_credential SET validation = %s", (db.jsonb(validation),))


# -- proxy github token (the proxy's working copy; SELECT grant) ----------------------


def save_proxy_github_token(token: str | None, expires_at: str | None = None) -> None:
    """Replace or clear the proxy's working copy of the active GitHub token —
    the only copy: ``expires_at`` (app mode; None for a PAT) is what reconcile
    checks to re-mint in time. Stored as secretbox ciphertext like every other
    secret; the proxy role holds SELECT on this row and on secret_keys (see
    migration 0002), which together decrypt exactly this working set and
    nothing else."""
    ciphertext = _encrypt_secret(token)
    with mutation() as cur:
        cur.execute("DELETE FROM proxy_github_token")
        if ciphertext is not None:
            cur.execute(
                "INSERT INTO proxy_github_token (singleton, token, expires_at, updated_at) VALUES (TRUE, %s, %s, %s)",
                (ciphertext, expires_at, utc_now()),
            )


_proxy_github_token_cache: tuple[str, str] | None = None


def read_proxy_github_token() -> str | None:
    """The active token the proxy injects, or None while GitHub is disabled
    or no credential is stored. Runs under the proxy role (SELECT grant)."""
    record = read_proxy_github_token_record()
    return record["token"] if record else None


def read_proxy_github_token_record() -> dict[str, Any] | None:
    """The working-token row (``token`` decrypted, plus ``expires_at``), or
    None when nothing is published. Reconcile reads the expiry to decide
    whether the published app token still has margin or must be re-minted."""
    global _proxy_github_token_cache
    with db.transaction() as cur:
        cur.execute("SELECT token, expires_at FROM proxy_github_token")
        row = cur.fetchone()
    if not row:
        return None
    ciphertext, expires_at = str(row[0]), row[1]
    cached = _proxy_github_token_cache
    if cached is not None and cached[0] == ciphertext:
        token = cached[1]
    else:
        token = secretbox.decrypt(ciphertext)
        _proxy_github_token_cache = (ciphertext, token)
    return {"token": token, "expires_at": expires_at}


# -- github repository audits (admin only; no proxy grant) ----------------------------


def save_github_repo_audit(owner: str, repo: str, facts: dict[str, Any], error: str | None) -> None:
    """Upsert one repository's audit facts (or the fetch error)."""
    with mutation() as cur:
        cur.execute(
            "INSERT INTO github_repo_audit (owner, repo, fetched_at, facts, error)"
            " VALUES (%s, %s, %s, %s, %s)"
            " ON CONFLICT (owner, repo) DO UPDATE SET"
            " fetched_at = EXCLUDED.fetched_at, facts = EXCLUDED.facts, error = EXCLUDED.error",
            (owner, repo, utc_now(), db.jsonb(facts), error),
        )


def read_github_repo_audits() -> dict[tuple[str, str], dict[str, Any]]:
    """All stored audits keyed by (owner, repo):
    ``{"fetched_at": ..., "facts": {...}, "error": ...?}``."""
    with db.transaction() as cur:
        cur.execute("SELECT owner, repo, fetched_at, facts, error FROM github_repo_audit")
        rows = cur.fetchall()
    audits: dict[tuple[str, str], dict[str, Any]] = {}
    for owner, repo, fetched_at, facts, error in rows:
        audit: dict[str, Any] = {"fetched_at": fetched_at, "facts": facts if isinstance(facts, dict) else {}}
        if error is not None:
            audit["error"] = str(error)
        audits[(str(owner), str(repo))] = audit
    return audits


def prune_github_repo_audits(keep: set[tuple[str, str]]) -> None:
    """Drop audits for repositories no longer in the policy."""
    with mutation() as cur:
        if not keep:
            cur.execute("DELETE FROM github_repo_audit")
            return
        placeholders = ", ".join(["(%s, %s)"] * len(keep))
        cur.execute(
            f"DELETE FROM github_repo_audit WHERE (owner, repo) NOT IN ({placeholders})",
            [value for pair in sorted(keep) for value in pair],
        )


# -- github .github push-approval gate (pending_pushes) ------------------------
# The proxy enqueues a row when a gated push touches .github/ (INSERT grant);
# the admin service lists and resolves them.

def enqueue_pending_push(
    push_id: str,
    owner: str,
    repo: str,
    ref_updates: list[dict[str, str]],
    changed_paths: list[str],
) -> None:
    with mutation() as cur:
        cur.execute(
            "INSERT INTO pending_pushes (id, owner, repo, ref_updates, changed_paths, requested_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (push_id, owner, repo, db.jsonb(ref_updates), db.jsonb(changed_paths), utc_now()),
        )


def count_pending_pushes() -> int:
    """Number of pushes still waiting for an operator decision."""
    with db.transaction() as cur:
        cur.execute("SELECT count(*) FROM pending_pushes WHERE status = 'pending'")
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _pending_push_row(row: tuple[Any, ...]) -> dict[str, Any]:
    push_id, owner, repo, ref_updates, changed_paths, requested_at, status, resolved_at, detail = row
    value: dict[str, Any] = {
        "id": str(push_id),
        "owner": str(owner),
        "repo": str(repo),
        "ref_updates": ref_updates if isinstance(ref_updates, list) else [],
        "changed_paths": changed_paths if isinstance(changed_paths, list) else [],
        "requested_at": requested_at,
        "status": str(status),
    }
    if resolved_at is not None:
        value["resolved_at"] = resolved_at
    if detail is not None:
        value["detail"] = str(detail)
    return value


_PENDING_PUSH_COLUMNS = "id, owner, repo, ref_updates, changed_paths, requested_at, status, resolved_at, detail"


def read_pending_pushes() -> list[dict[str, Any]]:
    """Pending pushes, newest first."""
    with db.transaction() as cur:
        cur.execute(f"SELECT {_PENDING_PUSH_COLUMNS} FROM pending_pushes ORDER BY requested_at DESC")
        return [_pending_push_row(row) for row in cur.fetchall()]


def get_pending_push(push_id: str) -> dict[str, Any] | None:
    with db.transaction() as cur:
        cur.execute(f"SELECT {_PENDING_PUSH_COLUMNS} FROM pending_pushes WHERE id = %s", (push_id,))
        row = cur.fetchone()
    return _pending_push_row(row) if row else None


def resolve_pending_push(push_id: str, status: str, detail: str | None = None) -> dict[str, Any]:
    """Mark a pending push resolved (approved/rejected/failed) with an optional
    detail message, and return the resolved row. The caller
    (push_gate.pending) holds RESOLVE_LOCK and has just read the row as
    pending, so the conditional update always matches; a vanished row would be
    a programming error and fails loudly."""
    with mutation() as cur:
        cur.execute(
            "UPDATE pending_pushes SET status = %s, resolved_at = %s, detail = %s"
            " WHERE id = %s AND status = 'pending'"
            f" RETURNING {_PENDING_PUSH_COLUMNS}",
            (status, utc_now(), detail or None, push_id),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"pending push {push_id} vanished mid-resolve")
    return _pending_push_row(row)


def prune_pending_pushes(cur: Any, keep: int = PENDING_PUSH_HISTORY_LIMIT) -> None:
    """Keep every pending push and only the newest resolved history rows."""
    cur.execute(
        "DELETE FROM pending_pushes WHERE status <> 'pending' AND id IN ("
        " SELECT id FROM pending_pushes WHERE status <> 'pending'"
        " ORDER BY COALESCE(resolved_at, requested_at) DESC, id DESC OFFSET %s)",
        (keep,),
    )


def read_github_credential_metadata() -> dict[str, Any]:
    credential = read_github_credential()
    mode = credential.get("mode")
    if mode not in ("pat", "app"):
        return {"configured": False}
    value: dict[str, Any] = {"configured": True, "mode": mode}
    if isinstance(credential.get("updated_at"), str):
        value["updated_at"] = credential["updated_at"]
    if mode == "app":
        if isinstance(credential.get("app_id"), str):
            value["app_id"] = credential["app_id"]
        if isinstance(credential.get("installation_id"), str):
            value["installation_id"] = credential["installation_id"]
        published = read_proxy_github_token_record()
        if published and isinstance(published.get("expires_at"), str):
            value["app_token_expires_at"] = published["expires_at"]
    validation = credential.get("validation")
    value["validation"] = validation if isinstance(validation, dict) else {"status": "not_checked"}
    return value


def append_network_event(
    protocol: str,
    method: str,
    host: str,
    port: int,
    path: str,
    query: str,
    allowed: bool,
    reason_code: str | None = None,
) -> None:
    """Record one allow/deny decision. Runs in the proxy process under its own
    database role, whose event-table grant permits this operation. A failure
    surfaces to the caller's connection handler: a decision that cannot be
    logged fails that request, never the proxy itself — fail closed."""
    # Field caps keep the row cap a real disk bound: the agent's own request
    # stream feeds this log, and headers allow multi-kilobyte URLs.
    host = host[:512]
    path = path[:2048]
    query = query[:2048]
    if reason_code is not None:
        reason_code = reason_code[:128]
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO network_events (created_at, protocol, method, host, port, path,"
            " query, decision, reason_code) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " RETURNING seq",
            (
                utc_now(),
                protocol,
                method,
                host,
                port,
                path,
                query,
                "allowed" if allowed else "denied",
                reason_code,
            ),
        )
        row = cur.fetchone()
        assert row is not None  # INSERT ... RETURNING always yields one row
        seq = int(row[0])
        if seq % PRUNE_EVERY == 0:
            prune_network_events(cur)


def prune_network_events(cur: Any) -> None:
    """Runs on the proxy's request path (every PRUNE_EVERY appends), so it
    must never scan the whole retained log — see _prune_events."""
    _prune_events(cur, "network_events", NETWORK_EVENT_LIMIT)


def _network_event_dict(row: Any) -> dict[str, Any]:
    seq, created_at, protocol, method, host, port, path, query, decision, reason_code = row
    event: dict[str, Any] = {
        "seq": int(seq),
        "timestamp": created_at,
        "protocol": protocol,
        "method": method,
        "host": host,
        "port": int(port),
        "path": path,
        "query": query,
        "decision": decision,
    }
    if reason_code is not None:
        event["reason_code"] = reason_code
    return event


_NETWORK_EVENT_FIELDS = "seq, created_at, protocol, method, host, port, path, query, decision, reason_code"


def page_network_events_before(
    before: int | None,
    *,
    decision: str | None = None,
    limit: int = EVENT_PAGE_LIMIT,
) -> list[dict[str, Any]]:
    extra = ("decision = %s", (decision,)) if decision is not None else (None, ())
    return _page_before(
        "network_events", _NETWORK_EVENT_FIELDS, _network_event_dict, before, limit,
        extra_clause=extra[0], extra_params=extra[1],
    )


# -- tools ---------------------------------------------------------------------


# Public "approval_<number>" ids, like "task_<number>" for tasks.
_APPROVAL_ID_PREFIX = "approval_"
_TOOL_APPROVAL_FIELDS = "number, tool_id, action_id, status, summary, payload, check_token, result, created_at, decided_at"


class PendingToolApprovalLimitReached(Exception):
    """Raised when inserting another pending tool approval would exceed the cap."""


def enabled_tool_ids() -> set[str]:
    with db.transaction() as cur:
        cur.execute("SELECT tool_id FROM enabled_tools")
        return {row[0] for row in cur.fetchall()}


def set_tool_enabled(cur: Any, tool_id: str, enabled: bool) -> None:
    if enabled:
        cur.execute(
            "INSERT INTO enabled_tools (tool_id) VALUES (%s) ON CONFLICT (tool_id) DO NOTHING",
            (tool_id,),
        )
    else:
        cur.execute("DELETE FROM enabled_tools WHERE tool_id = %s", (tool_id,))


def tool_config_keys(tool_id: str) -> set[str]:
    """The configured key names for one tool; values stay in the database
    except for tool_config_values callers building a tool call's config view."""
    with db.transaction() as cur:
        cur.execute("SELECT key FROM tool_config WHERE tool_id = %s", (tool_id,))
        return {row[0] for row in cur.fetchall()}


def tool_config_values(tool_id: str, keys: list[str]) -> dict[str, str]:
    """Configured values for one tool's manifest keys. Config is scoped per
    tool, so a shared key name resolves to this tool's own value. Values are
    secretbox ciphertext at rest and decrypted here for the tool call's config
    view."""
    wanted = set(keys)
    if not wanted:
        return {}
    with db.transaction() as cur:
        cur.execute("SELECT key, value FROM tool_config WHERE tool_id = %s", (tool_id,))
        return {row[0]: secretbox.decrypt(row[1]) for row in cur.fetchall() if row[0] in wanted}


def save_tool_config_value(cur: Any, tool_id: str, key: str, value: str) -> None:
    """Set one tool's deployment config value; an empty value clears the key.
    Stored as secretbox ciphertext so config secrets never sit in the clear."""
    if value:
        cur.execute(
            "INSERT INTO tool_config (tool_id, key, value) VALUES (%s, %s, %s)"
            " ON CONFLICT (tool_id, key) DO UPDATE SET value = EXCLUDED.value",
            (tool_id, key, secretbox.encrypt(value)),
        )
    else:
        cur.execute("DELETE FROM tool_config WHERE tool_id = %s AND key = %s", (tool_id, key))


def tool_credential(tool_id: str) -> dict[str, Any] | None:
    """One tool's stored OAuth credential (the store behind HostAPI.credentials),
    reassembled into the StoredCredential shape from its columns, or None if
    the tool is not connected."""
    with db.transaction() as cur:
        cur.execute(
            "SELECT account_id, account_label, account_scopes, secret, metadata"
            " FROM tool_credentials WHERE tool_id = %s",
            (tool_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    account_id, account_label, account_scopes, secret_ciphertext, metadata = row
    secret = json.loads(secretbox.decrypt(secret_ciphertext))
    return {
        "account": {
            "id": account_id,
            "label": account_label,
            "scopes": [str(scope) for scope in account_scopes] if isinstance(account_scopes, list) else [],
        },
        "secret": secret if isinstance(secret, dict) else {},
        "metadata": metadata if isinstance(metadata, dict) else {},
    }


def put_tool_credential(tool_id: str, value: dict[str, Any]) -> None:
    """Store a StoredCredential in its columns. Only the provider token
    material is a secret: it is serialized and secretbox-encrypted; the
    connected-account fields and tool bookkeeping are non-secret by contract
    (host/tools/host_api.py) and stored as plain columns. Malformed records
    are rejected rather than stored partially."""
    account = value.get("account")
    secret = value.get("secret")
    metadata = value.get("metadata")
    if (
        not isinstance(account, dict)
        or not isinstance(account.get("id"), str)
        or not account["id"]
        or not isinstance(account.get("label"), str)
        or not isinstance(account.get("scopes"), list)
        or not isinstance(secret, dict)
        or not isinstance(metadata, dict)
    ):
        raise ValueError(f"malformed stored credential for tool {tool_id}")
    with mutation() as cur:
        cur.execute(
            "INSERT INTO tool_credentials (tool_id, account_id, account_label, account_scopes, secret, metadata)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tool_id) DO UPDATE SET account_id = EXCLUDED.account_id,"
            " account_label = EXCLUDED.account_label, account_scopes = EXCLUDED.account_scopes,"
            " secret = EXCLUDED.secret, metadata = EXCLUDED.metadata",
            (
                tool_id,
                account["id"],
                account["label"],
                db.jsonb([str(scope) for scope in account["scopes"]]),
                secretbox.encrypt(json.dumps(secret)),
                db.jsonb(metadata),
            ),
        )


def delete_tool_credential(tool_id: str) -> None:
    with mutation() as cur:
        cur.execute("DELETE FROM tool_credentials WHERE tool_id = %s", (tool_id,))


# -- tool audit log ------------------------------------------------------------
# The tool-side peer of the agent and network event logs: one row per tool
# event, paged newest-first with the same before-cursor model.

_TOOL_EVENT_FIELDS = "seq, created_at, tool_id, action_id, outcome, detail, arguments"


def _tool_event_dict(row: Any, *, include_arguments: bool = False) -> dict[str, Any]:
    seq, created_at, tool_id, action_id, outcome, detail, arguments = row
    event: dict[str, Any] = {
        "seq": int(seq),
        "timestamp": created_at,
        "event_id": f"tool_event_{seq}",
        "tool_id": tool_id,
        "action_id": action_id,
        "outcome": outcome,
        "detail": detail or "",
        "has_arguments": isinstance(arguments, dict),
    }
    if include_arguments:
        event["arguments"] = arguments if isinstance(arguments, dict) else None
    return event


def record_tool_event(
    tool_id: str,
    action_id: str,
    outcome: str,
    detail: str = "",
    arguments: dict[str, Any] | None = None,
) -> None:
    """Append one tool audit event in its own transaction. seq is a serial:
    unique and increasing, with harmless gaps from aborted transactions.
    Prunes to TOOL_EVENT_LIMIT amortized, like the other event logs."""
    with mutation() as cur:
        cur.execute(
            "INSERT INTO tool_events (created_at, tool_id, action_id, outcome, detail, arguments)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING seq",
            (utc_now(), tool_id, action_id, outcome, detail, db.jsonb(arguments) if arguments is not None else None),
        )
        if int(cur.fetchone()[0]) % PRUNE_EVERY == 0:
            _prune_events(cur, "tool_events", TOOL_EVENT_LIMIT)


def page_tool_events_before(
    before: int | None, *, limit: int = EVENT_PAGE_LIMIT
) -> list[dict[str, Any]]:
    return _page_before("tool_events", _TOOL_EVENT_FIELDS, _tool_event_dict, before, limit)


def tool_event(seq: int) -> dict[str, Any] | None:
    """Load one audit event with its exact arguments for an operator expansion."""
    with db.transaction() as cur:
        cur.execute(f"SELECT {_TOOL_EVENT_FIELDS} FROM tool_events WHERE seq = %s", (seq,))
        row = cur.fetchone()
    return _tool_event_dict(row, include_arguments=True) if row is not None else None


# -- host diagnostics log ------------------------------------------------------
# A journald collector is the sole writer.

_HOST_DIAGNOSTIC_FIELDS = (
    "id, seq, first_seen_at, last_seen_at, service, component, kind,"
    " severity, exception_type, summary, traceback, context, fingerprint,"
    " occurrence_count, host_version, boot_id, pid"
)


def _host_diagnostic_dict(row: Any, *, include_details: bool = False) -> dict[str, Any]:
    (
        diagnostic_id,
        seq,
        first_seen_at,
        last_seen_at,
        service,
        component,
        kind,
        severity,
        exception_type,
        summary,
        trace,
        context,
        fingerprint,
        occurrence_count,
        host_version,
        boot_id,
        pid,
    ) = row
    diagnostic: dict[str, Any] = {
        "id": int(diagnostic_id),
        "seq": int(seq),
        "diagnostic_id": f"host_diagnostic_{diagnostic_id}",
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
        "service": service,
        "component": component,
        "kind": kind,
        "severity": severity,
        "exception_type": exception_type,
        "summary": summary,
        "occurrence_count": int(occurrence_count),
        "host_version": host_version,
        "boot_id": boot_id,
        "pid": int(pid) if pid is not None else None,
        "has_details": bool(trace or context),
    }
    if include_details:
        diagnostic["traceback"] = trace
        diagnostic["context"] = dict(context) if isinstance(context, dict) else {}
        diagnostic["fingerprint"] = fingerprint
    return diagnostic


def ingest_host_diagnostic(
    realtime_usec: int,
    event: dict[str, Any],
) -> int:
    """Store or briefly coalesce one validated journal diagnostic."""
    from datetime import datetime, timedelta, timezone

    seen_at = (
        datetime.fromtimestamp(realtime_usec / 1_000_000, timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    cutoff = (
        datetime.fromtimestamp(realtime_usec / 1_000_000, timezone.utc)
        - timedelta(seconds=HOST_DIAGNOSTIC_COALESCE_SECONDS)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with db.transaction() as cur:
        cur.execute(
            "SELECT id FROM host_diagnostics"
            " WHERE fingerprint = %s AND service = %s AND last_seen_at >= %s"
            " ORDER BY seq DESC LIMIT 1 FOR UPDATE",
            (event["fingerprint"], event["service"], cutoff),
        )
        existing = cur.fetchone()
        if existing is not None:
            existing_id = int(existing[0])
            cur.execute(
                "UPDATE host_diagnostics SET seq = nextval('host_diagnostics_seq_seq'),"
                " last_seen_at = %s,"
                " occurrence_count = occurrence_count + 1, summary = %s,"
                " traceback = %s, context = %s, host_version = %s,"
                " boot_id = %s, pid = %s WHERE id = %s RETURNING id, seq",
                (
                    seen_at,
                    event["summary"],
                    event["traceback"],
                    db.jsonb(event["context"]),
                    event["host_version"],
                    event["boot_id"],
                    event["pid"],
                    existing_id,
                ),
            )
            updated = cur.fetchone()
            assert updated is not None
            diagnostic_id, seq = int(updated[0]), int(updated[1])
        else:
            cur.execute(
                "INSERT INTO host_diagnostics"
                " (first_seen_at, last_seen_at, service, component, kind,"
                " severity, exception_type, summary, traceback, context, fingerprint,"
                " occurrence_count, host_version, boot_id, pid)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s)"
                " RETURNING id, seq",
                (
                    seen_at,
                    seen_at,
                    event["service"],
                    event["component"],
                    event["kind"],
                    event["severity"],
                    event["exception_type"],
                    event["summary"],
                    event["traceback"],
                    db.jsonb(event["context"]),
                    event["fingerprint"],
                    event["host_version"],
                    event["boot_id"],
                    event["pid"],
                ),
            )
            inserted = cur.fetchone()
            assert inserted is not None
            diagnostic_id, seq = int(inserted[0]), int(inserted[1])
        if seq % HOST_DIAGNOSTIC_PRUNE_EVERY == 0:
            prune_host_diagnostics(cur)
    return diagnostic_id


def prune_host_diagnostics(cur: Any) -> None:
    """Keep the newest bounded host diagnostics across both severities."""
    cur.execute(
        "DELETE FROM host_diagnostics WHERE"
        " seq < COALESCE(("
        " SELECT seq FROM host_diagnostics ORDER BY seq DESC OFFSET %s LIMIT 1"
        "), 0)",
        (HOST_DIAGNOSTIC_LIMIT - 1,),
    )


def page_host_diagnostics_before(
    before: int | None,
    *,
    service: str | None = None,
    severity: str | None = None,
    limit: int = EVENT_PAGE_LIMIT,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if service is not None:
        clauses.append("service = %s")
        params.append(service)
    if severity is not None:
        clauses.append("severity = %s")
        params.append(severity)
    return _page_before(
        "host_diagnostics",
        _HOST_DIAGNOSTIC_FIELDS,
        _host_diagnostic_dict,
        before,
        limit,
        extra_clause=" AND ".join(clauses) if clauses else None,
        extra_params=tuple(params),
    )


def host_diagnostic(diagnostic_id: int) -> dict[str, Any] | None:
    with db.transaction() as cur:
        cur.execute(
            f"SELECT {_HOST_DIAGNOSTIC_FIELDS} FROM host_diagnostics WHERE id = %s",
            (diagnostic_id,),
        )
        row = cur.fetchone()
    return _host_diagnostic_dict(row, include_details=True) if row is not None else None


def _approval_id(number: int, check_token: str) -> str:
    # The public id carries the unguessable check token, so the id itself is
    # the agent's poll capability: no separate token to marry back up, and the
    # sequential number alone cannot be enumerated. token_urlsafe has no dots,
    # so the number splits off unambiguously.
    return f"{_APPROVAL_ID_PREFIX}{number}.{check_token}"


def _tool_approval_dict(row: Any) -> dict[str, Any]:
    number, tool_id, action_id, status, summary, payload, check_token, result, created_at, decided_at = row
    return {
        "approval_id": _approval_id(number, check_token),
        "tool_id": tool_id,
        "action_id": action_id,
        "status": status,
        "summary": summary,
        "payload": dict(payload) if isinstance(payload, dict) else {},
        "result": result or "",
        "created_at": int(created_at),
        "decided_at": int(decided_at),
    }


def _approval_number(approval_id: str) -> int | None:
    if not isinstance(approval_id, str) or not approval_id.startswith(_APPROVAL_ID_PREFIX):
        return None
    number_part = approval_id[len(_APPROVAL_ID_PREFIX):].split(".", 1)[0]
    return int(number_part) if number_part.isdigit() else None


def insert_tool_approval(
    tool_id: str,
    action_id: str,
    summary: str,
    payload: dict[str, Any],
    created_at: int,
    *,
    pending_limit: int,
) -> dict[str, Any]:
    with mutation() as cur:
        # All inserts run in the tools service under this process's
        # mutation lock, so the count check cannot race another insert.
        # (The admin maintenance pass may expire rows concurrently, which
        # only makes the backpressure count conservative.)
        cur.execute("SELECT COUNT(*) FROM tool_approvals WHERE status = 'pending'")
        if int(cur.fetchone()[0]) >= pending_limit:
            raise PendingToolApprovalLimitReached()
        cur.execute(
            "INSERT INTO tool_approvals (tool_id, action_id, status, summary, payload, check_token, created_at)"
            " VALUES (%s, %s, 'pending', %s, %s, %s, %s)"
            f" RETURNING {_TOOL_APPROVAL_FIELDS}",
            (tool_id, action_id, summary, db.jsonb(payload), secrets.token_urlsafe(32), created_at),
        )
        return _tool_approval_dict(cur.fetchone())


def tool_approval(approval_id: str, tool_id: str | None = None) -> dict[str, Any] | None:
    """The approval record for the full ``approval_<n>.<token>`` id, optionally
    restricted to one tool's partition. Returns None unless the id's token
    matches the stored one (constant-time), so a guessed number never
    resolves — verification for every caller lives here."""
    number = _approval_number(approval_id)
    if number is None:
        return None
    with db.transaction() as cur:
        if tool_id is None:
            cur.execute(
                f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals WHERE number = %s",
                (number,),
            )
        else:
            cur.execute(
                f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals"
                " WHERE number = %s AND tool_id = %s",
                (number, tool_id),
            )
        row = cur.fetchone()
    if row is None:
        return None
    record = _tool_approval_dict(row)
    if not hmac.compare_digest(approval_id, record["approval_id"]):
        return None
    return record


def list_tool_approvals(limit: int, tool_id: str | None = None) -> list[dict[str, Any]]:
    """Newest approvals first, pending before decided so open decisions
    surface at the top of the admin UI. Scoped to one tool when tool_id is set,
    which is how the operator UI shows approvals per tool rather than unified."""
    order = " ORDER BY (status = 'pending') DESC, number DESC LIMIT %s"
    with db.transaction() as cur:
        if tool_id is None:
            cur.execute(f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals{order}", (limit,))
        else:
            cur.execute(
                f"SELECT {_TOOL_APPROVAL_FIELDS} FROM tool_approvals WHERE tool_id = %s{order}",
                (tool_id, limit),
            )
        return [_tool_approval_dict(row) for row in cur.fetchall()]


def transition_tool_approval(
    approval_id: str,
    from_status: str,
    to_status: str,
    decided_at: int,
    result: str | None = None,
) -> bool:
    """Atomic conditional status transition; False when the record is absent
    or no longer in from_status, so concurrent decisions cannot both win.
    ``result`` is the terminal outcome text: the approved action's
    user-visible message when it executed, or the error when it failed."""
    number = _approval_number(approval_id)
    if number is None:
        return False
    with mutation() as cur:
        cur.execute(
            "UPDATE tool_approvals SET status = %s, decided_at = %s,"
            " result = COALESCE(%s, result)"
            " WHERE number = %s AND status = %s RETURNING number",
            (to_status, decided_at, result, number, from_status),
        )
        return cur.fetchone() is not None


def fail_approved_tool_approvals(decided_at: int) -> None:
    """Mark every approval stuck in ``approved`` as failed — a direct scan, so
    no record escapes through a listing horizon. Write a failure ``result``
    too, so ``check_tool_approval`` reports the interrupted execution instead
    of an empty outcome."""
    result = "The tools service restarted while executing this approved action; its outcome is unknown."
    with mutation() as cur:
        cur.execute(
            "UPDATE tool_approvals SET status = 'failed', decided_at = %s, result = %s WHERE status = 'approved'",
            (decided_at, result),
        )


def expire_tool_approvals(cutoff: int) -> None:
    """Expire pending approvals created before the cutoff (host expiry
    policy, applied by the maintenance pass)."""
    with mutation() as cur:
        cur.execute(
            "UPDATE tool_approvals SET status = 'expired', decided_at = %s"
            " WHERE status = 'pending' AND created_at < %s",
            (int(time.time()), cutoff),
        )


def prune_tool_approvals(keep: int) -> None:
    """Cap decided-approval history; pending records are never pruned."""
    with mutation() as cur:
        cur.execute(
            "DELETE FROM tool_approvals WHERE status <> 'pending' AND number <="
            " (SELECT COALESCE(MAX(number), 0) FROM tool_approvals) - %s",
            (keep,),
        )
