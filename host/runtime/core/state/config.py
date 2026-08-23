"""Host configuration and operator passkey state."""

from __future__ import annotations

import hmac
import json
import secrets
from typing import Any

from host.runtime.core import db, pgclient, secretbox
from host.runtime.core.state._base import ADMIN_PASSKEY_LIMIT, _encrypt_secret, mutation

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
