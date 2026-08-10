from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping
from typing import cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.host_api import ConnectionAccount, HostAPI, StoredCredential
from host.tools.manifest import SetupStep
from host.tools.shared.oauth2 import (
    IntegrationReconnectRequired,
    access_token_is_fresh,
    clear_if_still_loaded,
    now,
    save_if_still_connected,
    signed_state,
    verify_state,
)
from host.tools.shared.web import WebRequestError, json_request, known_provider_transport_error, request_bytes, unmapped_provider_error
from host.tools.tool import (
    ConnectionStatus,
    OAuthCompleteConnectParams,
    OAuthCompleteConnectResult,
    OAuthStartConnectParams,
    OAuthStartConnectResult,
)

GOOGLE_OAUTH_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_DEFAULT_EXPIRES_IN_SECONDS = 3600
GOOGLE_UNAUTHORIZED_RECONNECT_MESSAGE = (
    "Google rejected the stored credentials. Please reconnect this tool from the admin UI."
)


class GoogleOAuthInvalidGrantError(RuntimeError):
    pass


def build_google_oauth_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    scopes: tuple[str, ...],
    state: str,
    force_consent: bool,
) -> str:
    query_params = {
        "access_type": "offline",
        "client_id": client_id,
        "include_granted_scopes": "true",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
    }
    if force_consent:
        query_params["prompt"] = "consent"
    return f"{GOOGLE_OAUTH_AUTH_URL}?{urllib.parse.urlencode(query_params)}"


def is_google_invalid_grant_payload(payload: bytes) -> bool:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(decoded, dict) and decoded.get("error") == "invalid_grant"


def normalize_email(value: str) -> str:
    return value.strip().lower()


def google_identity_from_userinfo(userinfo: Mapping[str, object]) -> JSONObject:
    sub = userinfo.get("sub")
    if (
        not isinstance(sub, str)
        or not sub
        or len(sub) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in sub)
    ):
        raise RuntimeError("Google did not return a stable account id.")
    email_value = userinfo.get("email")
    if not isinstance(email_value, str) or not email_value.strip() or len(email_value) > 500:
        raise RuntimeError("Google did not return an email address.")
    if userinfo.get("email_verified") is not True:
        raise RuntimeError("Google email address is not verified.")
    return {
        "email": normalize_email(email_value),
        "email_verified": True,
        "sub": sub,
    }


def granted_google_scopes(token_response: Mapping[str, object]) -> set[str]:
    scope = token_response.get("scope")
    return set(scope.split()) if isinstance(scope, str) else set()


def google_refresh_token_from_payload(token_payload: object) -> str:
    if not isinstance(token_payload, Mapping):
        return ""
    refresh_token = token_payload.get("refresh_token")
    return refresh_token if isinstance(refresh_token, str) else ""


def google_token_for_revoke_from_payload(token_payload: object) -> str:
    if not isinstance(token_payload, Mapping):
        return ""
    refresh_token = token_payload.get("refresh_token")
    if isinstance(refresh_token, str) and refresh_token:
        return refresh_token
    access_token = token_payload.get("access_token")
    return access_token if isinstance(access_token, str) else ""


def google_access_token_from_payload(token_payload: Mapping[str, object]) -> str:
    access_token = token_payload.get("access_token")
    return access_token if isinstance(access_token, str) and access_token else ""


def google_token_payload_from_response(
    token_response: Mapping[str, object],
    *,
    fallback_refresh_token: str,
    current_time: int,
) -> JSONObject:
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Google OAuth token response returned no access token.")
    refresh_token = token_response.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = fallback_refresh_token
    if not refresh_token:
        raise RuntimeError("Google OAuth token response returned no refresh token.")
    expires_in = token_response.get("expires_in")
    scope = token_response.get("scope")
    token_type = token_response.get("token_type")
    return {
        "access_token": access_token,
        "expires_at": current_time + (expires_in if isinstance(expires_in, int) else GOOGLE_DEFAULT_EXPIRES_IN_SECONDS),
        "refresh_token": refresh_token,
        "scope": scope if isinstance(scope, str) else "",
        "token_type": token_type if isinstance(token_type, str) else "Bearer",
    }


def google_refreshed_token_payload(
    token_payload: Mapping[str, object],
    token_response: Mapping[str, object],
    *,
    current_time: int,
) -> JSONObject:
    access_token = token_response.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise RuntimeError("Google OAuth token refresh returned no access token.")
    expires_in = token_response.get("expires_in")
    refreshed_payload: JSONObject = {
        key: value
        for key, value in token_payload.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    refreshed_payload["access_token"] = access_token
    refreshed_payload["expires_at"] = current_time + (
        expires_in if isinstance(expires_in, int) else GOOGLE_DEFAULT_EXPIRES_IN_SECONDS
    )
    return refreshed_payload


def _post_google_oauth_form(
    form: Mapping[str, str],
    *,
    failure_message: str,
    invalid_response_message: str,
    invalid_grant_is_special: bool,
) -> dict[str, object]:
    try:
        decoded = json_request(
            "POST",
            GOOGLE_OAUTH_TOKEN_URL,
            form=form,
            failure_message=failure_message,
            invalid_response_message=invalid_response_message,
        )
    except WebRequestError as exc:
        if invalid_grant_is_special and is_google_invalid_grant_payload(exc.body):
            raise GoogleOAuthInvalidGrantError("Google OAuth refresh token is invalid.") from exc
        if exc.status in {400, 401, 403}:
            raise RuntimeError(
                "Google OAuth rejected the request. Check the OAuth client credentials, callback URI, authorization code, and requested scopes."
            ) from exc
        if exc.status:
            raise RuntimeError(f"{failure_message.rstrip('.')} (HTTP {exc.status}).") from exc
        known = known_provider_transport_error(exc)
        if known:
            raise RuntimeError(known) from exc
        raise unmapped_provider_error("Google", "OAuth", exc) from None
    return cast(dict[str, object], decoded)


def exchange_google_oauth_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    failure_message: str,
    invalid_response_message: str,
) -> dict[str, object]:
    return _post_google_oauth_form(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        failure_message=failure_message,
        invalid_response_message=invalid_response_message,
        invalid_grant_is_special=False,
    )


def refresh_google_oauth_token(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    failure_message: str,
    invalid_response_message: str,
) -> dict[str, object]:
    return _post_google_oauth_form(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        failure_message=failure_message,
        invalid_response_message=invalid_response_message,
        invalid_grant_is_special=True,
    )


def get_google_userinfo(
    access_token: str,
    *,
    failure_message: str,
    invalid_response_message: str,
) -> dict[str, object]:
    try:
        decoded = json_request(
            "GET",
            GOOGLE_USERINFO_URL,
            headers={"authorization": f"Bearer {access_token}"},
            failure_message=failure_message,
            invalid_response_message=invalid_response_message,
        )
    except WebRequestError as exc:
        if exc.status == 401:
            # Same treatment as google_json_request: a rejected cached token
            # must surface the reconnect flow, and refresh_identity runs
            # before every write proposal and approved execution.
            raise IntegrationReconnectRequired(GOOGLE_UNAUTHORIZED_RECONNECT_MESSAGE) from exc
        if exc.status == 403:
            raise RuntimeError("Google denied the profile lookup. The connection may be missing a required scope.") from exc
        if exc.status == 429:
            raise RuntimeError("Google API rate limit was reached during the profile lookup.") from exc
        if exc.status:
            raise RuntimeError(f"{failure_message.rstrip('.')} (HTTP {exc.status}).") from exc
        known = known_provider_transport_error(exc)
        if known:
            raise RuntimeError(known) from exc
        raise unmapped_provider_error("Google", "profile lookup", exc) from None
    return cast(dict[str, object], decoded)


def revoke_google_token(token: str) -> JSONObject:
    body = urllib.parse.urlencode({"token": token}).encode("utf-8")
    try:
        request_bytes(
            "POST",
            GOOGLE_OAUTH_REVOKE_URL,
            headers={"content-type": "application/x-www-form-urlencoded"},
            data=body,
            failure_message="Google token revocation failed.",
        )
    except WebRequestError as exc:
        if exc.status:
            return {"success": False, "failure_type": "http", "status": exc.status}
        return {"success": False, "failure_type": "network", "error_type": "WebRequestError"}
    return {"success": True}


def google_oauth_setup_steps(
    *,
    project_step_description: str,
    enable_api_step: SetupStep,
    scopes_step: SetupStep,
    connect_step_description: str,
) -> tuple[SetupStep, ...]:
    """The shared Google Cloud OAuth setup guide: the Gmail and Google Calendar
    tools walk the operator through the same console flow, differing only in
    which API is enabled, which scopes are declared, and the two descriptions
    that name the tool."""
    return (
        SetupStep(
            title="Create or select a Google Cloud project",
            description=project_step_description,
            link_url="https://console.cloud.google.com/projectcreate",
            link_label="Open Google Cloud project creation",
        ),
        enable_api_step,
        SetupStep(
            title="Configure the OAuth consent screen",
            description="Open Google Auth Platform > Branding and choose Get Started. Enter an app name such as Kern, a support email, External audience unless you use a Workspace-internal app, and your contact email. Then publish the app to Production; an app left in Testing needs your Google account under Audience > Test users and must be reconnected every week.",
            link_url="https://developers.google.com/workspace/guides/configure-oauth-consent",
            link_label="View Google's consent-screen guide",
            image_path="/guide-assets/google-auth-app-information.png",
            image_alt="Google Auth Platform app information form with App name and User support email fields.",
        ),
        scopes_step,
        SetupStep(
            title="Create a Web application OAuth client",
            description="Open Google Auth Platform > Clients, choose Create Client, and select Web application. Give the client a recognizable name. Leave Authorized JavaScript origins empty. Under Authorized redirect URIs, choose Add URI and enter this host's callback URI shown below. Then create the client and copy the client ID and client secret for the final step. The screenshot shows where the two URI sections appear.",
            link_url="https://developers.google.com/workspace/guides/create-credentials#web-application",
            link_label="View Google's web-client instructions",
            image_path="/guide-assets/google-auth-web-client.png",
            image_alt="Google Auth Platform Web application client form with Authorized JavaScript origins and Authorized redirect URIs sections.",
            show_callback=True,
        ),
        SetupStep(
            title="Configure Kern and connect",
            description=connect_step_description,
            show_config=True,
        ),
    )


class GoogleCredentialStore:
    def __init__(
        self,
        *,
        tool_id: str,
        scopes: tuple[str, ...],
        required_scopes: frozenset[str],
        reconnect_message: str,
    ) -> None:
        self.tool_id = tool_id
        self.scopes = scopes
        self.required_scopes = required_scopes
        self.reconnect_message = reconnect_message

    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        redirect_uri = params["redirect_uri"]
        state = self._signed_state(api)
        return {
            "authorization_url": build_google_oauth_authorization_url(
                client_id=api.config["GOOGLE_OAUTH_CLIENT_ID"],
                redirect_uri=redirect_uri,
                scopes=self.scopes,
                state=state,
                force_consent=True,
            ),
            "state": state,
        }

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        code = params["code"]
        redirect_uri = params["redirect_uri"]
        self._verify_state(params["state"], api)
        token_response = exchange_google_oauth_code(
            client_id=api.config["GOOGLE_OAUTH_CLIENT_ID"],
            client_secret=api.config["GOOGLE_OAUTH_CLIENT_SECRET"],
            code=code,
            redirect_uri=redirect_uri,
            failure_message="Google OAuth token exchange failed.",
            invalid_response_message="Google OAuth token exchange returned an invalid response.",
        )
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError("Google OAuth token exchange returned no access token.")
        missing_scopes = self.required_scopes - granted_google_scopes(token_response)
        if missing_scopes:
            # The insufficient new grant was never saved, so there is nothing
            # to clean up — and an already-connected account must survive a
            # failed reconnect attempt, so nothing is cleared here.
            raise RuntimeError("Google connection is missing required permissions.")
        identity = google_identity_from_userinfo(
            get_google_userinfo(
                access_token,
                failure_message="Google OAuth user profile lookup failed.",
                invalid_response_message="Google OAuth user profile lookup returned an invalid response.",
            )
        )
        existing = api.credentials.load()
        fallback_refresh_token = ""
        if existing is not None and existing["account"]["id"] == identity["sub"]:
            fallback_refresh_token = google_refresh_token_from_payload(existing["secret"])
        token_payload = google_token_payload_from_response(
            token_response,
            fallback_refresh_token=fallback_refresh_token,
            current_time=now(),
        )
        scope_field = token_payload.get("scope")
        scope_str = scope_field if isinstance(scope_field, str) and scope_field else " ".join(self.scopes)
        scopes = scope_str.split()
        created_at = existing["metadata"].get("created_at") if existing is not None else None
        current_time = now()
        account: ConnectionAccount = {"id": str(identity["sub"]), "label": str(identity["email"]), "scopes": scopes}
        credential: StoredCredential = {
            "account": account,
            "secret": token_payload,
            "metadata": {
                "created_at": created_at if isinstance(created_at, int) else current_time,
                "email_verified": identity["email_verified"],
                "identity_checked_at": current_time,
                "updated_at": current_time,
            },
        }
        api.credentials.save(credential)
        return {"account": account}

    def disconnect(self, api: HostAPI) -> None:
        existing = api.credentials.load()
        if existing is not None:
            token = google_token_for_revoke_from_payload(existing["secret"])
            if token:
                revoke_google_token(token)
        api.credentials.clear()

    # Saves and clears after a network round trip go through shared/oauth2.py's
    # compare-before-write guards, so an operator disconnect/reconnect
    # landing in that multi-second window always wins: a stale refresh cannot
    # clobber the fresh credential and a stale failure cannot drop it.

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        existing = api.credentials.load()
        if existing is None:
            return {"connected": False}
        return {"connected": True, "account": existing["account"]}

    def access_token(self, api: HostAPI) -> str:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(f"{self.tool_id} is not connected.")
        token_payload = existing["secret"]
        missing_scopes = self.required_scopes - set(existing["account"]["scopes"])
        if missing_scopes:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(self.reconnect_message)
        payload = cast(Mapping[str, object], token_payload)
        if access_token_is_fresh(payload, now()):
            return google_access_token_from_payload(payload)
        refresh_token = google_refresh_token_from_payload(payload)
        if not refresh_token:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(self.reconnect_message)
        try:
            token_response = refresh_google_oauth_token(
                client_id=api.config["GOOGLE_OAUTH_CLIENT_ID"],
                client_secret=api.config["GOOGLE_OAUTH_CLIENT_SECRET"],
                refresh_token=refresh_token,
                failure_message="Google OAuth token refresh failed.",
                invalid_response_message="Google OAuth token refresh returned an invalid response.",
            )
        except GoogleOAuthInvalidGrantError as exc:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(self.reconnect_message) from exc
        updated_payload = google_refreshed_token_payload(payload, token_response, current_time=now())
        save_if_still_connected(
            api,
            existing,
            {
                "account": existing["account"],
                "secret": updated_payload,
                "metadata": {**existing["metadata"], "updated_at": now()},
            },
            reconnect_message=self.reconnect_message,
        )
        return google_access_token_from_payload(updated_payload)

    def refresh_identity(self, api: HostAPI, access_token: str) -> ConnectionAccount:
        existing = api.credentials.load()
        if existing is None:
            raise IntegrationReconnectRequired(self.reconnect_message)
        identity = google_identity_from_userinfo(
            get_google_userinfo(
                access_token,
                failure_message="Google OAuth user profile lookup failed.",
                invalid_response_message="Google OAuth user profile lookup returned an invalid response.",
            )
        )
        if existing["account"]["id"] != identity["sub"]:
            clear_if_still_loaded(api, existing)
            raise IntegrationReconnectRequired(self.reconnect_message)
        account: ConnectionAccount = {
            "id": str(identity["sub"]),
            "label": str(identity["email"]),
            "scopes": existing["account"]["scopes"],
        }
        save_if_still_connected(
            api,
            existing,
            {
                "account": account,
                "secret": existing["secret"],
                "metadata": {
                    **existing["metadata"],
                    "email_verified": identity["email_verified"],
                    "identity_checked_at": now(),
                    "updated_at": now(),
                },
            },
            reconnect_message=self.reconnect_message,
        )
        return account

    def _signed_state(self, api: HostAPI) -> str:
        return signed_state(secret=api.config["GOOGLE_OAUTH_CLIENT_SECRET"], tool_id=self.tool_id)

    def _verify_state(self, state: str, api: HostAPI) -> None:
        verify_state(
            state,
            secret=api.config["GOOGLE_OAUTH_CLIENT_SECRET"],
            tool_id=self.tool_id,
            invalid_message="Invalid Google OAuth state.",
            expired_message="Google OAuth state expired.",
        )


def google_json_request(
    method: str,
    url: str,
    access_token: str,
    *,
    body: JSONObject | None = None,
    failure_message: str,
    invalid_response_message: str,
) -> JSONObject:
    try:
        return json_request(
            method,
            url,
            headers={"authorization": f"Bearer {access_token}"},
            body=body,
            failure_message=failure_message,
            invalid_response_message=invalid_response_message,
        )
    except WebRequestError as exc:
        if exc.status == 401:
            # A still-cached token Google no longer accepts (for example the
            # operator revoked the app) is a connection problem, not a
            # generic API failure: surface the reconnect-required flow.
            raise IntegrationReconnectRequired(GOOGLE_UNAUTHORIZED_RECONNECT_MESSAGE) from exc
        if exc.status == 403:
            raise RuntimeError("Google denied the API request. The connection may be missing a required scope.") from exc
        if exc.status == 429:
            raise RuntimeError("Google API rate limit was reached.") from exc
        if exc.status:
            raise RuntimeError(f"{failure_message.rstrip('.')} (HTTP {exc.status}).") from exc
        known = known_provider_transport_error(exc)
        if known:
            raise RuntimeError(known) from exc
        raise unmapped_provider_error("Google", "API", exc) from None
