"""Browser-only Postgres access. Secrets never cross the service API."""
from __future__ import annotations

import io
import json
from typing import Any, Literal, TypedDict

from host.runtime.browser.client import BrowserError
from host.runtime.core import db, secretbox

SAVED_STATE_LIMIT_BYTES = 1_000_000_000
# Leave space for Chromium, the decoded snapshot, serialization and OpenSSL buffers
# inside the service's 2 GB memory limit. This is not a process-memory guarantee.
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


def ciphertext_bound(size: int) -> int:
    # Base64 plus generous space for secretbox's salt/header/block padding.
    return len(secretbox.PREFIX) + 4 * ((size + 66) // 3)


class Usage(TypedDict):
    day: str
    count: int


class AccountData(TypedDict):
    provider_identifier: str
    state: Literal["connected", "needs_attention"]
    checked_at: str
    usage: dict[str, Usage]


class Store:
    def load_settings(self) -> dict[str, str]:
        with db.transaction() as cur:
            cur.execute("SELECT mode, username, password_ciphertext, location, session FROM browser_settings")
            row = cur.fetchone()
        if row is None or row[0] == "direct":
            return {"mode": "direct"}
        return {"mode": row[0], "username": row[1], "password": secretbox.decrypt(row[2]),
                "location": row[3], "session": row[4]}

    def save_settings(self, value: dict[str, str]) -> None:
        password = secretbox.encrypt(value["password"]) if value["mode"] == "decodo" else None
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO browser_settings (mode, username, password_ciphertext, location, session)"
                " VALUES (%s, %s, %s, %s, %s) ON CONFLICT (singleton) DO UPDATE SET"
                " mode=EXCLUDED.mode, username=EXCLUDED.username, password_ciphertext=EXCLUDED.password_ciphertext,"
                " location=EXCLUDED.location, session=EXCLUDED.session",
                (value["mode"], value.get("username"), password, value.get("location"), value.get("session")),
            )

    def accounts(self) -> dict[str, tuple[str, AccountData]]:
        with db.transaction() as cur:
            cur.execute("SELECT account_id, provider, provider_identifier, state, checked_at, usage_day::text, usage_count"
                        " FROM browser_accounts ORDER BY account_id")
            return {row[0]: (row[1], {"provider_identifier": row[2], "state": row[3], "checked_at": row[4],
                                     "usage": {("x_post_tweet" if row[1] == "x" else "linkedin_send_dm"): {"day": row[5], "count": row[6]}} if row[5] else {}})
                    for row in cur.fetchall()}

    def auth(self, account_id: str) -> dict[str, Any]:
        with db.transaction() as cur:
            cur.execute("SELECT CASE WHEN octet_length(auth_ciphertext)<=%s THEN auth_ciphertext END"
                        " FROM browser_accounts WHERE account_id=%s", (ciphertext_bound(MAX_SNAPSHOT_BYTES), account_id))
            row = cur.fetchone()
        if row is None:
            raise BrowserError("The Browser account no longer exists.")
        if row[0] is None:
            raise BrowserError("Saved Browser login exceeds the 64 MiB snapshot limit. Reconnect this account.")
        value = json.loads(secretbox.decrypt(row[0]))
        if not isinstance(value, dict):
            raise BrowserError("Saved Browser login state is invalid. Reconnect this account.")
        return value

    def save_account(self, account_id: str, provider: str, data: AccountData,
                     auth: dict[str, Any] | None = None) -> None:
        serialized = None
        if auth is not None:
            size = 0
            def check_strings(value: Any) -> None:
                nonlocal size
                # Count ASCII JSON escaping before iterencode allocates a whole
                # string chunk, including surrogate pairs for astral characters.
                if isinstance(value, str):
                    size += 2  # quotes
                    for char in value:
                        size += (2 if char in '\\"\b\f\n\r\t' else
                                 1 if ' ' <= char <= '~' else
                                 6 if ord(char) <= 0xffff else 12)
                        if size > MAX_SNAPSHOT_BYTES:
                            raise BrowserError("Browser login snapshot exceeds the 64 MiB limit. Previous saved login is unchanged.")
                elif isinstance(value, dict):
                    for key, child in value.items():
                        check_strings(key)
                        check_strings(child)
                elif isinstance(value, list):
                    for child in value:
                        check_strings(child)
                if size > MAX_SNAPSHOT_BYTES:
                    raise BrowserError("Browser login snapshot exceeds the 64 MiB limit. Previous saved login is unchanged.")
            check_strings(auth)
            buffer = io.BytesIO()
            size = 0
            # ensure_ascii means character count equals the encoded byte count.
            # One byte buffer avoids retaining millions of small encoder objects.
            for chunk in json.JSONEncoder(ensure_ascii=True).iterencode(auth):
                size += len(chunk)
                if size > MAX_SNAPSHOT_BYTES:
                    raise BrowserError("Browser login snapshot exceeds the 64 MiB limit. Previous saved login is unchanged.")
                buffer.write(chunk.encode("ascii"))
            serialized = buffer.getvalue().decode("ascii")
            buffer.close()
        ciphertext = None
        usage = data["usage"].get("x_post_tweet" if provider == "x" else "linkedin_send_dm")
        with db.transaction() as cur:
            # Serialize capacity checks and writes, including deletion.
            cur.execute("LOCK TABLE browser_accounts IN EXCLUSIVE MODE")
            cur.execute("SELECT count(*), COALESCE(sum(octet_length(auth_ciphertext)), 0)"
                        " FROM browser_accounts WHERE account_id<>%s", (account_id,))
            row = cur.fetchone()
            assert row is not None
            count, used = row
            if count >= 5:
                raise BrowserError("Up to 5 browser accounts can be saved. Disconnect an unused account first.")
            if serialized is not None:
                if used + ciphertext_bound(len(serialized)) > SAVED_STATE_LIMIT_BYTES:
                    raise BrowserError("Browser saved state reached its 1 GB total limit. Disconnect an unused account to free space.")
                ciphertext = secretbox.encrypt(serialized)
            if ciphertext is not None and used + len(ciphertext) > SAVED_STATE_LIMIT_BYTES:
                raise BrowserError("Browser saved state reached its 1 GB total limit. Disconnect an unused account to free space. If submitting a post, check X before approving another attempt.")
            values = (provider, data["provider_identifier"], data["state"], data["checked_at"],
                      usage["day"] if usage else None, usage["count"] if usage else 0)
            if ciphertext is None:
                cur.execute("UPDATE browser_accounts SET provider=%s, provider_identifier=%s, state=%s, checked_at=%s,"
                            " usage_day=%s, usage_count=%s WHERE account_id=%s RETURNING account_id", (*values, account_id))
                if cur.fetchone() is None:
                    raise BrowserError("The Browser account no longer exists.")
            else:
                cur.execute("INSERT INTO browser_accounts (account_id, provider, provider_identifier, state, checked_at,"
                            " usage_day, usage_count, auth_ciphertext) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)"
                            " ON CONFLICT (account_id) DO UPDATE SET provider=EXCLUDED.provider,"
                            " provider_identifier=EXCLUDED.provider_identifier, state=EXCLUDED.state, checked_at=EXCLUDED.checked_at,"
                            " usage_day=EXCLUDED.usage_day, usage_count=EXCLUDED.usage_count, auth_ciphertext=EXCLUDED.auth_ciphertext",
                            (account_id, *values, ciphertext))

    def delete_account(self, account_id: str) -> None:
        with db.transaction() as cur:
            cur.execute("DELETE FROM browser_accounts WHERE account_id=%s", (account_id,))
