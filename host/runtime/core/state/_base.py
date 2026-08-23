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


def _encrypt_secret(value: Any) -> str | None:
    """Encrypt an optional non-empty secret before database storage."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("stored secrets must be non-empty strings")
    return secretbox.encrypt(value)
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
# Set by a transaction that enqueues embedding work. The indexer waits on this
# instead of polling every five seconds. It is a plain wakeup, not a work count:
# the queue table is the durable record, and the indexer also wakes on a slower
# periodic backstop.
conversation_embedding_work = threading.Event()
# Conversation history gets a deeper retained window than the high-volume
# network and tool audit logs. Each log prunes every PRUNE_EVERY appends so
# the cost stays amortized.
AGENT_EVENT_LIMIT = 10_000_000
NETWORK_EVENT_LIMIT = 1_000_000
TOOL_EVENT_LIMIT = 1_000_000
PRUNE_EVERY = 500
# Vectors are substantially larger than their source rows and share the 16 GiB
# admin volume with all PostgreSQL state. Keep a quota over conversation
# messages themselves: high-volume activity/lifecycle events must not consume
# semantic-history capacity.
CONVERSATION_EMBEDDING_MESSAGE_LIMIT = 250_000
# Computing the source-message floor walks one bounded partial index. Amortize
# that work across embedding batches; at MAX_TEXTS=8 this permits at most 800
# newly indexed messages beyond the target before the next trim.
CONVERSATION_EMBEDDING_PRUNE_EVERY_BATCHES = 100
# A message the encoder cannot handle is abandoned after this many tries rather
# than reclaimed forever, which would stall every newer message behind it.
CONVERSATION_EMBEDDING_MAX_ATTEMPTS = 5
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
