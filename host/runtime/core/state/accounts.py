"""OAuth login, provider-account, and connected-credential state."""

from __future__ import annotations

from typing import Any

from host.runtime.core import db, secretbox
from host.runtime.core.state._base import _read, mutation

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


def save_xai_account(account: dict[str, Any] | None, cur: Any = None) -> None:
    _save_provider_account("xai", account or {}, cur)


def read_xai_account(cur: Any = None) -> dict[str, Any]:
    value = _read_provider_account("xai", cur)
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
