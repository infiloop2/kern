"""Upwork's published MCP OAuth flow, through Kern's existing connect UI."""

from __future__ import annotations

import secrets
import ipaddress
import json
import re
from contextlib import contextmanager
import threading
import urllib.parse
import urllib.error
from typing import cast

from host.tools.host_api import ConnectionAccount, HostAPI, StoredCredential
from host.tools.json_types import JSONObject
from host.tools.shared.oauth2 import (
    OAuth2CredentialStore, IntegrationReconnectRequired, access_token_is_fresh,
    clear_if_still_loaded, now, pkce_verifier_and_challenge, save_if_still_connected,
)
from host.tools.shared.web import json_request, ProviderWarning, WebRequestError
from host.tools.tool import OAuthStartConnectParams, OAuthStartConnectResult, OAuthCompleteConnectParams, OAuthCompleteConnectResult

# Verified via Upwork's protected-resource and authorization-server metadata,
# 2026-09-09. Do not follow provider-selected endpoints or HTTP redirects.
ENDPOINT = "https://mcp.upwork.com/mcp"
AUTHORIZE = "https://www.upwork.com/ab/account-security/oauth2/authorize"
REGISTER = "https://www.upwork.com/register"
TOKEN = "https://www.upwork.com/api/v3/oauth2/token"
REVOKE = "https://www.upwork.com/api/v3/oauth2/token/revoke"
USER_AGENT = "Kern/v1"
RECONNECT = "Upwork is no longer connected. Disconnect this connection and click Connect again."
# Leave enough lifetime for the complete approved proposal request sequence.
TOKEN_WINDOW_SECONDS = 300
# Refresh is rare. One Upwork lock avoids a per-connection lock registry and
# prevents concurrent exchanges of a single-use rotating refresh token.
_REFRESH_LOCK = threading.Lock()
_LOGIN_LOCK = threading.RLock()


def _authorization_warning(operation: str, exc: RuntimeError, *, redirect_uri: str = "") -> ProviderWarning:
    # OAuth responses can echo credentials, including in descriptions. Retain
    # only standard error codes and structural facts, never raw response text.
    details: JSONObject = {"failure": "invalid_response", "user_agent": USER_AGENT}
    status = 0
    if isinstance(exc, WebRequestError):
        status = exc.status
        details["failure"] = "http_error" if status else "transport_error"
        details["response_bytes_retained"] = len(exc.body)
        try:
            response = json.loads(exc.body)
        except (ValueError, UnicodeError):
            details["response_format"] = "non_json" if exc.body else "empty"
        else:
            details["response_format"] = "json"
            code = response.get("error") if isinstance(response, dict) else None
            known_codes = {
                "invalid_redirect_uri", "invalid_client_metadata", "invalid_request",
                "invalid_client", "unauthorized_client", "access_denied", "invalid_grant",
                "unsupported_grant_type", "unsupported_response_type", "invalid_scope",
                "server_error", "temporarily_unavailable", "invalid_token",
            }
            if isinstance(code, str):
                details["error"] = code if code in known_codes else "unrecognized"
        if exc.__cause__ is not None:
            details["cause_type"] = type(exc.__cause__).__name__
        if isinstance(exc.__cause__, urllib.error.HTTPError):
            headers = exc.__cause__.headers
            ray = headers.get("CF-Ray", "")
            if re.fullmatch(r"[0-9a-f]{16}(?:-[A-Z]{3})?", ray):
                details["cloudflare_ray_id"] = ray
            media_type = headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if media_type in {"application/json", "text/html", "text/plain"}:
                details["content_type"] = media_type
    if redirect_uri:
        details["redirect_uri"] = redirect_uri
    return ProviderWarning(
        "Upwork", operation, "Upwork could not complete the connection request. Please try again later.",
        status=status, body=json.dumps(details).encode("utf-8"),
    )


def _request(url: str, *, diagnostic_redirect_uri: str = "", **kwargs) -> JSONObject:
    operation = {REGISTER: "OAuth client registration", TOKEN: "OAuth token exchange", REVOKE: "OAuth token revocation"}[url]
    try:
        return json_request("POST", url, headers={"User-Agent": USER_AGENT}, failure_message="Upwork authorization request failed.",
                            invalid_response_message="Upwork returned an invalid authorization response.",
                            max_bytes=65536, **kwargs)
    except RuntimeError as exc:
        raise _authorization_warning(operation, exc, redirect_uri=diagnostic_redirect_uri) from None


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 16384 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise RuntimeError(f"Upwork returned an invalid {field}.")
    return value


def _tokens(response: JSONObject, *, client_id: str, previous: JSONObject | None = None) -> JSONObject:
    if str(response.get("token_type", "")).lower() != "bearer":
        raise RuntimeError("Upwork returned an unsupported token type.")
    expires_in = response.get("expires_in")
    if not isinstance(expires_in, int) or isinstance(expires_in, bool) or not TOKEN_WINDOW_SECONDS < expires_in <= 365 * 86400:
        raise RuntimeError("Upwork returned an invalid token lifetime.")
    refresh = response.get("refresh_token") or (previous or {}).get("refresh_token")
    return {
        "client_id": client_id,
        "access_token": _text(response.get("access_token"), "access token"),
        "refresh_token": _text(refresh, "refresh token"),
        "expires_at": now() + expires_in,
    }


def _revoke(secret: JSONObject) -> None:
    # Prefer revoking the refresh grant; a malformed token response may only
    # give us an access token that can be cleaned up.
    for hint in ("refresh_token", "access_token"):
        try:
            token = _text(secret.get(hint), hint)
        except RuntimeError:
            continue
        _request(REVOKE, form={"client_id": cast(str, secret["client_id"]),
                              "token": token, "token_type_hint": hint})
        return
    raise RuntimeError("Upwork returned no usable token to revoke.")


@contextmanager
def _issued_tokens(response: JSONObject, client_id: str, api: HostAPI, previous: StoredCredential | None = None):
    """Revoke issued authority if validation or persistence cannot finish."""
    issued = None
    try:
        issued = _tokens(response, client_id=client_id, previous=previous["secret"] if previous else None)
        yield issued
    except Exception:
        cleanup_failed = False
        refresh: str | None
        try:
            refresh = _text(response.get("refresh_token"), "refresh token")
        except RuntimeError:
            refresh = cast(str | None, previous["secret"].get("refresh_token")) if previous else None
        try:
            _revoke({"client_id": client_id,
                     "refresh_token": refresh,
                     "access_token": response.get("access_token")})
        except RuntimeError:
            cleanup_failed = True
        with _LOGIN_LOCK:
            try:
                current = api.credentials.load()
            except ValueError:
                # The host-selected account changed; preserve its replacement.
                current = None
            if current is not None and (current == previous or issued is not None and current["secret"] == issued):
                api.credentials.clear()
        if cleanup_failed:
            raise IntegrationReconnectRequired("Upwork could not finish connecting or refreshing. Remote revocation failed; remove Kern in Upwork Account Settings > Connected Apps and connect again.") from None
        if previous is not None:
            raise IntegrationReconnectRequired(RECONNECT) from None
        raise


class UpworkOAuth(OAuth2CredentialStore):
    reconnect_message = RECONNECT
    required_scopes = frozenset()

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        if api.credentials.load() is not None:
            raise ValueError("Disconnect the existing Upwork connection before connecting again.")
        redirect = params["redirect_uri"]
        parsed = urllib.parse.urlsplit(redirect)
        try:
            loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
        except ValueError:
            loopback = False
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != "/oauth/callback" or not (
            parsed.scheme == "https" and bool(parsed.hostname)
            or parsed.scheme == "http" and loopback
        ):
            raise ValueError("Upwork requires Kern's HTTPS or localhost OAuth callback.")
        registration = _request(REGISTER, diagnostic_redirect_uri=redirect, body={
            "client_name": "Kern", "redirect_uris": [redirect],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"], "token_endpoint_auth_method": "none",
        })
        try:
            client_id = _text(registration.get("client_id"), "client identifier")
            if registration.get("token_endpoint_auth_method", "none") != "none":
                raise RuntimeError("Upwork did not register the requested public OAuth client.")
        except RuntimeError as exc:
            raise _authorization_warning("OAuth client registration", exc, redirect_uri=redirect) from None
        verifier, challenge = pkce_verifier_and_challenge()
        state = secrets.token_urlsafe(32)
        query = urllib.parse.urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "resource": ENDPOINT,
        })
        with _LOGIN_LOCK:
            if api.credentials.load() is not None:
                raise ValueError("Disconnect the existing Upwork connection before connecting again.")
            api.secrets.save({"state": state, "client_id": client_id, "verifier": verifier, "redirect_uri": redirect, "expires_at": now() + 900})
        return {"authorization_url": f"{AUTHORIZE}?{query}", "state": state}

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        marker: JSONObject = {"state": params["state"]}
        with _LOGIN_LOCK:
            pending = api.secrets.load()
            if pending is None or type(pending.get("expires_at")) is not int or cast(int, pending["expires_at"]) <= now() or not isinstance(pending.get("state"), str) or not secrets.compare_digest(cast(str, pending["state"]).encode(), params["state"].encode()) or pending.get("redirect_uri") != params["redirect_uri"]:
                raise ValueError("Upwork sign-in expired, was cancelled, or was already used. Click Connect again.")
            client_id = _text(pending.get("client_id"), "client identifier")
            verifier = _text(pending.get("verifier"), "PKCE verifier")
            # Remove the verifier before network I/O. A marker lets disconnect
            # or a newer Connect cancel the eventual save without holding a
            # lock across the token exchange.
            api.secrets.save(marker)
        token = _request(TOKEN, form={
            "grant_type": "authorization_code", "code": params["code"],
            "redirect_uri": params["redirect_uri"], "client_id": client_id,
            "code_verifier": verifier, "resource": ENDPOINT,
        })
        with _issued_tokens(token, client_id, api) as secret:
            scope = token.get("scope", "")
            if not isinstance(scope, str) or len(scope) > 8192:
                raise RuntimeError("Upwork returned invalid granted scopes.")
            # No published user-info endpoint: bind approvals to a fresh grant.
            grant_id = "grant-" + secrets.token_hex(16)
            account: ConnectionAccount = {"id": grant_id, "label": "Upwork MCP " + grant_id[-6:], "scopes": scope.split()}
            credential: StoredCredential = {"account": account, "secret": secret, "metadata": {"created_at": now()}}
            with _LOGIN_LOCK:
                if api.secrets.load() != marker:
                    raise ValueError("Upwork sign-in expired or was cancelled. Click Connect again.")
                api.credentials.save(credential)
                api.secrets.clear()
            return {"account": account}

    def connected(self, api: HostAPI) -> StoredCredential:
        loaded = self.load_connected(api)
        if access_token_is_fresh(loaded["secret"], now(), skew_seconds=TOKEN_WINDOW_SECONDS):
            return loaded
        with _REFRESH_LOCK:
            loaded = self.load_connected(api)
            if access_token_is_fresh(loaded["secret"], now(), skew_seconds=TOKEN_WINDOW_SECONDS):
                return loaded
            old = loaded["secret"]
            try:
                # Status is needed to distinguish an invalid grant from an outage.
                response = json_request("POST", TOKEN, headers={"User-Agent": USER_AGENT}, form={
                    "grant_type": "refresh_token", "client_id": _text(old.get("client_id"), "client identifier"),
                    "refresh_token": _text(old.get("refresh_token"), "refresh token"), "resource": ENDPOINT,
                }, failure_message="Upwork token refresh failed.", invalid_response_message="Upwork returned an invalid token response.", max_bytes=65536)
            except WebRequestError as exc:
                if exc.status in (400, 401):
                    self.invalidate(api, loaded)
                    raise IntegrationReconnectRequired(RECONNECT) from None
                raise _authorization_warning("OAuth token refresh", exc) from None
            except RuntimeError as exc:
                raise _authorization_warning("OAuth token refresh", exc) from None
            with _issued_tokens(response, cast(str, old["client_id"]), api, loaded) as secret:
                account: ConnectionAccount = {**loaded["account"]}
                if "scope" in response:
                    scope = response["scope"]
                    if not isinstance(scope, str) or len(scope) > 8192:
                        raise RuntimeError("Upwork returned invalid granted scopes.")
                    account["scopes"] = scope.split()
                updated: StoredCredential = {"account": account, "secret": secret, "metadata": loaded["metadata"]}
                with _LOGIN_LOCK:
                    save_if_still_connected(api, loaded, updated, reconnect_message=RECONNECT)
                return updated

    def invalidate(self, api: HostAPI, loaded: StoredCredential) -> None:
        with _LOGIN_LOCK:
            try:
                clear_if_still_loaded(api, loaded)
            except ValueError:
                # A replacement account is outside this call's host scope.
                pass
        try:
            _revoke(loaded["secret"])
        except RuntimeError:
            raise IntegrationReconnectRequired(RECONNECT + " Remote revocation failed; remove Kern in Upwork Account Settings > Connected Apps.") from None

    def disconnect(self, api: HostAPI) -> None:
        # Cancel callbacks and remove local authority before the network call.
        with _LOGIN_LOCK:
            loaded = api.credentials.load()
            api.secrets.clear()
            api.credentials.clear()
        if loaded is not None:
            secret = loaded["secret"]
            try:
                _revoke(secret)
            except ProviderWarning as exc:
                exc.args = ("Upwork was disconnected locally, but remote revocation failed. Remove Kern in Upwork Account Settings > Connected Apps.",)
                raise
            except RuntimeError:
                raise RuntimeError("Upwork was disconnected locally, but remote revocation failed. Remove Kern in Upwork Account Settings > Connected Apps.") from None
