"""Fixed X Ads REST transport with OAuth 1.0a signing, without retries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
import urllib.parse
from collections.abc import Mapping

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject
from host.tools.shared.web import WebRequestError, json_request

BASE_URL = "https://ads-api.x.com/12"
CONFIG_KEYS = ("X_ADS_CONSUMER_KEY", "X_ADS_CONSUMER_SECRET", "X_ADS_ACCESS_TOKEN", "X_ADS_ACCESS_TOKEN_SECRET")


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="~")


def authorization_header(
    method: str, url: str, parameters: Mapping[str, str], keys: tuple[str, str, str, str],
    *, nonce: str, timestamp: int,
) -> str:
    """Sign the exact decoded query/form parameters per RFC 5849 section 3.4."""
    consumer_key, consumer_secret, token, token_secret = keys
    oauth = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": nonce,
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": str(timestamp),
        "oauth_token": token,
        "oauth_version": "1.0",
    }
    pairs = sorted((_quote(k), _quote(v)) for k, v in (*parameters.items(), *oauth.items()))
    normalized = "&".join(f"{k}={v}" for k, v in pairs)
    signature_base = "&".join(_quote(v) for v in (method, url, normalized))
    signing_key = f"{_quote(consumer_secret)}&{_quote(token_secret)}".encode("ascii")
    oauth["oauth_signature"] = base64.b64encode(
        hmac.new(signing_key, signature_base.encode("ascii"), hashlib.sha1).digest()
    ).decode("ascii")
    return "OAuth " + ", ".join(f'{_quote(k)}="{_quote(v)}"' for k, v in sorted(oauth.items()))


class Client:
    def __init__(self, api: HostAPI) -> None:
        self.keys = tuple(api.config[key] for key in CONFIG_KEYS)
        if any(not value or len(value) > 512 or not value.isascii() or any(c.isspace() for c in value) for value in self.keys):
            raise ValueError("Set all four X Ads credentials under Home > Integrations, without whitespace.")
        # This opaque binding prevents config replacement from executing an old
        # approval. No credential or reversible encoding enters the payload.
        self.binding = hashlib.sha256(json.dumps(self.keys, separators=(",", ":")).encode("ascii")).hexdigest()

    def request(self, method: str, path: str, parameters: Mapping[str, str] | None = None) -> JSONObject:
        params = dict(parameters or {})
        url = BASE_URL + path
        header = authorization_header(
            method, url, params, (self.keys[0], self.keys[1], self.keys[2], self.keys[3]),
            nonce=secrets.token_hex(16), timestamp=int(time.time()),
        )
        encoded = urllib.parse.urlencode(params)
        headers = {"Authorization": header, "Accept": "application/json"}
        if method == "GET":
            if encoded:
                url += "?" + encoded
        else:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            result = json_request(
                method, url, headers=headers, form=params if method != "GET" else None,
                failure_message="X Ads request failed.", invalid_response_message="X Ads returned invalid JSON.",
            )
        except WebRequestError as exc:
            messages = {
                400: "X Ads rejected these parameters. Check the supported settings and advertiser eligibility.",
                401: "X Ads authentication failed. Check all four credentials and the app's Read and Write permission.",
                403: "X Ads denied access. Verify this exact developer app has Ads campaign access and this user has the required advertiser role.",
                404: "The X Ads entity was not found or is unavailable to this user.",
                429: "X Ads rate limit reached. Wait before making a new request.",
            }
            raise RuntimeError(messages.get(exc.status, "X Ads request failed; the provider outcome may be unknown.")) from exc
        if result.get("errors") or result.get("operation_errors"):
            raise RuntimeError("X Ads rejected the request. Inspect the advertiser account before retrying.")
        return result


def object_data(response: JSONObject) -> JSONObject:
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("X Ads returned an incomplete entity response.")
    return data


def page_data(response: JSONObject, *, limit: int) -> tuple[list[JSONObject], str | None]:
    data = response.get("data")
    cursor = response.get("next_cursor")
    if not isinstance(data, list) or any(not isinstance(row, dict) for row in data) or len(data) > limit:
        raise RuntimeError("X Ads returned an incomplete or oversized list response.")
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 1024):
        raise RuntimeError("X Ads returned an invalid pagination cursor.")
    return [row for row in data if isinstance(row, dict)], cursor or None


def complete_list(client: Client, path: str, params: Mapping[str, str] | None = None) -> list[JSONObject]:
    """Approval preconditions never treat a truncated first page as complete."""
    rows, cursor = page_data(client.request("GET", path, {**(params or {}), "count": "100"}), limit=100)
    if cursor:
        raise ValueError("This promotion exceeds the initial tool's 100-row inspection limit. Manage it in X Ads.")
    return rows
