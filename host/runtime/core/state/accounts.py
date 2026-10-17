"""OAuth login, provider-account, and connected-credential state."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from host import session_options
from host.runtime.core import db, secretbox
from host.runtime.core.state._base import _read, mutation


def runtime_account_enabled(runtime_type: str, cur: Any = None) -> bool:
    """Primary accounts follow their provider; additional accounts opt in."""
    if runtime_type not in session_options.OPTIONAL_ACCOUNT_RUNTIMES:
        return True
    provider = (openai_provider_key(runtime_type) if runtime_type in OPENAI_PROVIDER_KEYS
                else xai_provider_key(runtime_type))
    with _read(cur) as cur:
        cur.execute("SELECT extra_account_enabled FROM provider_accounts WHERE provider = %s", (provider,))
        row = cur.fetchone()
        return bool(row and row[0])


def set_runtime_account_enabled(cur: Any, runtime_type: str, enabled: bool) -> None:
    if runtime_type not in session_options.OPTIONAL_ACCOUNT_RUNTIMES:
        raise ValueError("only additional runtime accounts have an enable switch")
    provider = (openai_provider_key(runtime_type) if runtime_type in OPENAI_PROVIDER_KEYS
                else xai_provider_key(runtime_type))
    cur.execute(
        "INSERT INTO provider_accounts (provider, extra_account_enabled) VALUES (%s, %s)"
        " ON CONFLICT (provider) DO UPDATE SET extra_account_enabled = EXCLUDED.extra_account_enabled",
        (provider, enabled),
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

OPENAI_PROVIDER_KEYS = {"codex": "openai", "codex-2": "openai-2", "codex-3": "openai-3"}
XAI_PROVIDER_KEYS = {"grok": "xai", "grok-2": "xai-2"}


def openai_provider_key(runtime_type: str) -> str:
    try:
        return OPENAI_PROVIDER_KEYS[runtime_type]
    except KeyError as exc:
        raise ValueError(f"unsupported Codex runtime: {runtime_type}") from exc


def xai_provider_key(runtime_type: str) -> str:
    try:
        return XAI_PROVIDER_KEYS[runtime_type]
    except KeyError as exc:
        raise ValueError(f"unsupported Grok runtime: {runtime_type}") from exc


def save_openai_account(
    account: dict[str, Any] | None,
    cur: Any = None,
    *,
    runtime_type: str = "codex",
) -> None:
    _save_provider_account(
        openai_provider_key(runtime_type),
        account if account is not None else {"account_id": None},
        cur,
    )


def read_openai_account(
    cur: Any = None,
    *,
    runtime_type: str = "codex",
) -> dict[str, Any]:
    value = _read_provider_account(openai_provider_key(runtime_type), cur)
    return value if isinstance(value, dict) else {}


def save_claude_account(account: dict[str, Any] | None, cur: Any = None) -> None:
    _save_provider_account("claude", account or {}, cur)


def read_claude_account(cur: Any = None) -> dict[str, Any]:
    value = _read_provider_account("claude", cur)
    return value if isinstance(value, dict) else {}


def save_xai_account(
    account: dict[str, Any] | None,
    cur: Any = None,
    *,
    runtime_type: str = "grok",
) -> None:
    _save_provider_account(xai_provider_key(runtime_type), account or {}, cur)


def read_xai_account(
    cur: Any = None, *, runtime_type: str = "grok"
) -> dict[str, Any]:
    value = _read_provider_account(xai_provider_key(runtime_type), cur)
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
        " metadata = EXCLUDED.metadata,"
        " training_allowed = CASE WHEN provider_accounts.account_id = EXCLUDED.account_id"
        " THEN provider_accounts.training_allowed END,"
        " training_checked_at = CASE WHEN provider_accounts.account_id = EXCLUDED.account_id"
        " THEN provider_accounts.training_checked_at END,"
        " training_attempted_at = CASE WHEN provider_accounts.account_id = EXCLUDED.account_id"
        " THEN provider_accounts.training_attempted_at END",
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


TRAINING_PROVIDERS = {"claude_code": "claude", "grok": "xai", "grok-2": "xai-2"}


def read_training_status(runtime_type: str, cur: Any = None) -> dict[str, Any]:
    with _read(cur) as cur:
        cur.execute(
            # pgclient returns timestamps as PostgreSQL text (for example,
            # a +00 offset), which Python 3.10's fromisoformat rejects.
            # Decode numeric epochs instead, independent of server formatting.
            "SELECT training_allowed,"
            " EXTRACT(EPOCH FROM training_checked_at)::double precision,"
            " EXTRACT(EPOCH FROM training_attempted_at)::double precision"
            " FROM provider_accounts WHERE provider = %s",
            (TRAINING_PROVIDERS[runtime_type],),
        )
        row = cur.fetchone()
    return {
        "allowed": row[0] if row else None,
        "checked_at": datetime.fromtimestamp(row[1], timezone.utc).isoformat() if row and row[1] is not None else None,
        "attempted_at": datetime.fromtimestamp(row[2], timezone.utc).isoformat() if row and row[2] is not None else None,
    }


def save_training_status(cur: Any, runtime_type: str, account: dict[str, Any],
                         allowed: bool | None, attempted_at: float) -> None:
    # A reset/relogin during the provider request must not attach its result to
    # the replacement account. Failed reads preserve the last successful result.
    timestamp = datetime.fromtimestamp(attempted_at, timezone.utc).isoformat()
    cur.execute(
        "UPDATE provider_accounts SET training_attempted_at = %s,"
        " training_allowed = COALESCE(%s, training_allowed),"
        " training_checked_at = CASE WHEN %s::boolean IS NOT NULL THEN %s ELSE training_checked_at END"
        " WHERE provider = %s AND account_id = %s"
        " AND metadata->>'access_token_sha256' = %s",
        (timestamp, allowed, allowed, timestamp, TRAINING_PROVIDERS[runtime_type],
         account.get("account_id"), account.get("access_token_sha256")),
    )
