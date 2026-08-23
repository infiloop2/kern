"""Network policy, provider proxy pins, GitHub, and network audit state."""

from __future__ import annotations

import time
from typing import Any

from host.runtime.core import db, secretbox
from host.runtime.core.state._base import (
    BEDROCK_USAGE_RETAIN_DAYS,
    EVENT_PAGE_LIMIT,
    NETWORK_EVENT_LIMIT,
    PENDING_PUSH_HISTORY_LIMIT,
    PRUNE_EVERY,
    _encrypt_secret,
    mutation,
    utc_now,
)
from host.runtime.core.state.events import _page_before, _prune_events

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
        cur.execute(
            "SELECT require_dot_github_approval, block_direct_main_pushes FROM github_settings"
        )
        settings_row = cur.fetchone()
        if settings_row and settings_row[0] and "github" in integrations:
            integrations["github"]["require_dot_github_approval"] = True
        if settings_row and not settings_row[1] and "github" in integrations:
            integrations["github"]["block_direct_main_pushes"] = False
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
            if github.get("enabled") is True:
                cur.execute(
                    "INSERT INTO github_settings "
                    "(singleton, require_dot_github_approval, block_direct_main_pushes) "
                    "VALUES (TRUE, %s, %s)",
                    (
                        github.get("require_dot_github_approval") is True,
                        github.get("block_direct_main_pushes") is not False,
                    ),
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


def save_proxy_xai_account_id(account_id: str | None, cur: Any = None) -> None:
    _save_proxy_account_id("xai", account_id, cur)


def read_proxy_xai_account_id() -> str | None:
    value = _read_proxy_pin("xai").get("account_id")
    return value if isinstance(value, str) and value else None


def read_proxy_xai_status_probe_account_id() -> str | None:
    with db.transaction() as cur:
        cur.execute("SELECT account_id FROM xai_status_probe_pin")
        row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


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
