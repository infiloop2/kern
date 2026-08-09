"""Small, dependency-free WebAuthn boundary for the single admin account.

Kern asks browsers for an ES256 resident credential with user verification.
The host stores only the credential id and DER-encoded public key. Registration
and assertion parsing live here; signature verification is delegated to the
already-installed system OpenSSL, avoiding another privileged runtime package.

Login and registration challenges are process-local, single-use, bounded, and
short-lived. Durable credentials live in Postgres through ``state`` so upgrades
and recovery preserve them. Reconfigure can explicitly delete them.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import threading
import time
from typing import Any

from host.runtime.core import state


OPENSSL_BIN = "/usr/bin/openssl"
CEREMONY_TIMEOUT_SECONDS = 5 * 60
MAX_PENDING_CEREMONIES = 1000
MAX_BINARY_FIELD_BYTES = 16 * 1024
MAX_CLIENT_DATA_BYTES = 4096
MAX_CBOR_DEPTH = 12
MAX_CBOR_ITEMS = 128
ES256_ALGORITHM = -7
EC2_KEY_TYPE = 2
P256_CURVE = 1

_B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_RP_ID_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
_TRANSPORTS = {"ble", "hybrid", "internal", "nfc", "smart-card", "usb"}
# SubjectPublicKeyInfo prefix for an uncompressed P-256 point:
# id-ecPublicKey + prime256v1, followed by a 65-byte BIT STRING.
_P256_SPKI_PREFIX = bytes.fromhex(
    "3059301306072a8648ce3d020106082a8648ce3d03010703420004"
)


class PasskeyError(ValueError):
    """A malformed, expired, or cryptographically invalid ceremony."""


@dataclass(frozen=True)
class _LoginCeremony:
    challenge: str
    rp_id: str
    origin: str
    user_handle: str
    client_key: str
    expires_at: float


@dataclass(frozen=True)
class _RegistrationCeremony:
    challenge: str
    rp_id: str
    origin: str
    user_handle: str
    expires_at: float


_login_ceremonies: dict[str, _LoginCeremony] = {}
_registration_ceremonies: dict[str, _RegistrationCeremony] = {}
_ceremony_lock = threading.Lock()


def _now() -> float:
    return time.monotonic()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: Any, *, maximum: int = MAX_BINARY_FIELD_BYTES) -> bytes:
    if not isinstance(value, str) or not value or not _B64URL_RE.fullmatch(value):
        raise PasskeyError("passkey response contains invalid base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))
    except (ValueError, binascii.Error) as exc:
        raise PasskeyError("passkey response contains invalid base64url") from exc
    if len(decoded) > maximum or _b64encode(decoded) != value:
        raise PasskeyError("passkey response contains invalid base64url")
    return decoded


def configured() -> bool:
    return bool(state.admin_passkeys())


def status(*, public_https: bool, rp_id: str | None) -> dict[str, Any]:
    credentials = state.admin_passkeys()
    return {
        "configured": bool(credentials),
        "credential_count": len(credentials),
        "setup_available": public_https and rp_id is not None,
        "rp_id": rp_id if public_https else None,
    }


def begin_login(
    *,
    rp_id: str,
    origin: str,
    client_key: str,
) -> tuple[str, dict[str, Any]]:
    credentials = state.admin_passkeys(rp_id)
    config = state.admin_passkey_config()
    if not credentials or config is None:
        raise PasskeyError(
            "no passkey is registered for this admin hostname; "
            "use root reconfigure with --reset-admin-passkeys"
        )
    token = secrets.token_urlsafe(32)
    challenge = _b64encode(secrets.token_bytes(32))
    ceremony = _LoginCeremony(
        challenge=challenge,
        rp_id=_validated_rp_id(rp_id),
        origin=_validated_origin(origin, rp_id),
        user_handle=str(config["user_handle"]),
        client_key=client_key,
        expires_at=_now() + CEREMONY_TIMEOUT_SECONDS,
    )
    with _ceremony_lock:
        _prune_locked(_login_ceremonies)
        _login_ceremonies[_token_hash(token)] = ceremony
    return token, {
        "challenge": challenge,
        "timeout": CEREMONY_TIMEOUT_SECONDS * 1000,
        "rpId": rp_id,
        "allowCredentials": [
            {
                "type": "public-key",
                "id": credential["credential_id"],
                **(
                    {"transports": credential["transports"]}
                    if credential["transports"]
                    else {}
                ),
            }
            for credential in credentials
        ],
        "userVerification": "required",
    }


def finish_login(
    token: str,
    response: Any,
    *,
    client_key: str,
) -> None:
    with _ceremony_lock:
        ceremony = _login_ceremonies.pop(_token_hash(token), None)
    if (
        ceremony is None
        or ceremony.expires_at < _now()
        or ceremony.client_key != client_key
    ):
        raise PasskeyError("passkey login expired; enter the admin password again")
    credential_id, authenticator_data, signature, client_data, user_handle = (
        _assertion_fields(response)
    )
    credentials = {
        credential["credential_id"]: credential
        for credential in state.admin_passkeys(ceremony.rp_id)
    }
    credential = credentials.get(credential_id)
    if credential is None:
        raise PasskeyError("passkey is not registered for this admin hostname")
    _validate_client_data(
        client_data,
        ceremony.challenge,
        ceremony.origin,
        expected_type="webauthn.get",
    )
    flags, sign_count = _validate_authenticator_data(
        authenticator_data,
        ceremony.rp_id,
        require_attested_credential=False,
    )
    if user_handle is not None and user_handle != ceremony.user_handle:
        raise PasskeyError("passkey user does not match the administrator")
    public_key = _b64decode(credential["public_key_spki"])
    signed = authenticator_data + hashlib.sha256(client_data).digest()
    if not _verify_es256(public_key, signature, signed):
        raise PasskeyError("passkey signature is invalid")
    previous_count = int(credential["sign_count"])
    # Zero-to-zero means this authenticator does not support a counter. Once
    # either value is nonzero, WebAuthn treats a non-advancing value (including
    # a regression back to zero) as a clone/malfunction signal.
    if (previous_count or sign_count) and sign_count <= previous_count:
        raise PasskeyError("passkey signature counter did not advance")
    next_count = max(previous_count, sign_count)
    if not state.mark_admin_passkey_used(
        credential_id,
        previous_sign_count=previous_count,
        sign_count=next_count,
        backed_up=bool(flags & 0x10),
        used_at=state.utc_now(),
    ):
        raise PasskeyError("passkey was used concurrently; enter the admin password again")


def begin_registration(
    session_hash: str,
    *,
    rp_id: str,
    origin: str,
    agent_name: str,
) -> dict[str, Any]:
    _validated_rp_id(rp_id)
    _validated_origin(origin, rp_id)
    if len(state.admin_passkeys()) >= state.ADMIN_PASSKEY_LIMIT:
        raise PasskeyError(
            "Kern already has an admin passkey; reset it before registering another"
        )
    config = state.admin_passkey_config()
    user_handle = (
        str(config["user_handle"])
        if config is not None
        else _b64encode(secrets.token_bytes(32))
    )
    challenge = _b64encode(secrets.token_bytes(32))
    ceremony = _RegistrationCeremony(
        challenge=challenge,
        rp_id=rp_id,
        origin=origin,
        user_handle=user_handle,
        expires_at=_now() + CEREMONY_TIMEOUT_SECONDS,
    )
    with _ceremony_lock:
        _prune_locked(_registration_ceremonies)
        _registration_ceremonies[session_hash] = ceremony
    credentials = state.admin_passkeys(rp_id)
    return {
        "challenge": challenge,
        "timeout": CEREMONY_TIMEOUT_SECONDS * 1000,
        "rp": {"id": rp_id, "name": f"Kern · {agent_name}"},
        "user": {
            "id": user_handle,
            "name": "admin",
            "displayName": "Kern administrator",
        },
        "pubKeyCredParams": [{"type": "public-key", "alg": ES256_ALGORITHM}],
        "authenticatorSelection": {
            "residentKey": "required",
            "requireResidentKey": True,
            "userVerification": "required",
        },
        "excludeCredentials": [
            {
                "type": "public-key",
                "id": credential["credential_id"],
                **(
                    {"transports": credential["transports"]}
                    if credential["transports"]
                    else {}
                ),
            }
            for credential in credentials
        ],
        "attestation": "none",
    }


def finish_registration(session_hash: str, response: Any) -> dict[str, Any]:
    with _ceremony_lock:
        ceremony = _registration_ceremonies.pop(session_hash, None)
    if ceremony is None or ceremony.expires_at < _now():
        raise PasskeyError("passkey setup expired; start again")
    credential_id, client_data, attestation_object, transports = (
        _registration_fields(response)
    )
    _validate_client_data(
        client_data,
        ceremony.challenge,
        ceremony.origin,
        expected_type="webauthn.create",
    )
    attestation, end = _decode_cbor(attestation_object)
    if end != len(attestation_object) or not isinstance(attestation, dict):
        raise PasskeyError("passkey attestation object is invalid")
    if attestation.get("fmt") != "none" or attestation.get("attStmt") != {}:
        raise PasskeyError("passkey returned unexpected attestation")
    auth_data = attestation.get("authData")
    if not isinstance(auth_data, bytes):
        raise PasskeyError("passkey attestation has no authenticator data")
    flags, sign_count, registered_id, public_key = _registration_auth_data(
        auth_data, ceremony.rp_id
    )
    if registered_id != credential_id:
        raise PasskeyError("passkey credential id does not match its attestation")
    if state.admin_passkeys() and state.admin_passkey_config() is None:
        raise PasskeyError("stored passkey configuration is inconsistent")
    if len(state.admin_passkeys()) >= state.ADMIN_PASSKEY_LIMIT:
        raise PasskeyError(
            "Kern already has an admin passkey; reset it before registering another"
        )
    try:
        state.save_admin_passkey(
            user_handle=ceremony.user_handle,
            credential_id=credential_id,
            rp_id=ceremony.rp_id,
            public_key_spki=_b64encode(public_key),
            sign_count=sign_count,
            transports=transports,
            backed_up=bool(flags & 0x10),
            created_at=state.utc_now(),
        )
    except state.AdminPasskeyLimitError as exc:
        raise PasskeyError(
            "Kern already has an admin passkey; reset it before registering another"
        ) from exc
    return {
        "configured": True,
        "credential_count": len(state.admin_passkeys()),
        "rp_id": ceremony.rp_id,
    }


def _prune_locked(ceremonies: dict[str, Any]) -> None:
    now = _now()
    expired = [
        key for key, ceremony in ceremonies.items()
        if ceremony.expires_at < now
    ]
    for key in expired:
        ceremonies.pop(key, None)
    while len(ceremonies) >= MAX_PENDING_CEREMONIES:
        oldest = min(ceremonies, key=lambda key: ceremonies[key].expires_at)
        ceremonies.pop(oldest, None)


def _validated_rp_id(rp_id: str) -> str:
    if not _RP_ID_RE.fullmatch(rp_id):
        raise PasskeyError("admin hostname is not a valid passkey RP ID")
    return rp_id


def _validated_origin(origin: str, rp_id: str) -> str:
    if origin != f"https://{rp_id}":
        raise PasskeyError("admin origin does not match its passkey RP ID")
    return origin


def _object(value: Any, *, fields: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - fields:
        raise PasskeyError(f"{context} has an invalid shape")
    return value


def _credential_identity(response: Any) -> tuple[dict[str, Any], str]:
    outer = _object(
        response,
        fields={"id", "rawId", "type", "response", "authenticatorAttachment"},
        context="passkey response",
    )
    if outer.get("type") != "public-key":
        raise PasskeyError("passkey response has an invalid credential type")
    credential_id = outer.get("rawId")
    raw_id = _b64decode(credential_id, maximum=1024)
    if outer.get("id") != credential_id or not raw_id:
        raise PasskeyError("passkey credential id is invalid")
    return outer, str(credential_id)


def _assertion_fields(
    response: Any,
) -> tuple[str, bytes, bytes, bytes, str | None]:
    outer, credential_id = _credential_identity(response)
    inner = _object(
        outer.get("response"),
        fields={"authenticatorData", "clientDataJSON", "signature", "userHandle"},
        context="passkey assertion",
    )
    user_handle_value = inner.get("userHandle")
    user_handle = None
    if user_handle_value is not None:
        user_handle = _b64encode(_b64decode(user_handle_value, maximum=128))
    return (
        credential_id,
        _b64decode(inner.get("authenticatorData")),
        _b64decode(inner.get("signature")),
        _b64decode(inner.get("clientDataJSON"), maximum=MAX_CLIENT_DATA_BYTES),
        user_handle,
    )


def _registration_fields(
    response: Any,
) -> tuple[str, bytes, bytes, list[str]]:
    outer, credential_id = _credential_identity(response)
    inner = _object(
        outer.get("response"),
        fields={"attestationObject", "clientDataJSON", "transports"},
        context="passkey registration",
    )
    raw_transports = inner.get("transports", [])
    if (
        not isinstance(raw_transports, list)
        or len(raw_transports) > 10
        or any(item not in _TRANSPORTS for item in raw_transports)
        or len(raw_transports) != len(set(raw_transports))
    ):
        raise PasskeyError("passkey transports are invalid")
    return (
        credential_id,
        _b64decode(inner.get("clientDataJSON"), maximum=MAX_CLIENT_DATA_BYTES),
        _b64decode(inner.get("attestationObject")),
        sorted(raw_transports),
    )


def _validate_client_data(
    raw: bytes,
    challenge: str,
    origin: str,
    *,
    expected_type: str,
) -> None:
    try:
        client = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PasskeyError("passkey client data is invalid") from exc
    if (
        not isinstance(client, dict)
        or client.get("type") != expected_type
        or client.get("challenge") != challenge
        or client.get("origin") != origin
        or client.get("crossOrigin", False) is not False
    ):
        raise PasskeyError("passkey client data does not match this request")


def _validate_authenticator_data(
    auth_data: bytes,
    rp_id: str,
    *,
    require_attested_credential: bool,
) -> tuple[int, int]:
    if len(auth_data) < 37:
        raise PasskeyError("passkey authenticator data is truncated")
    if not secrets.compare_digest(
        auth_data[:32], hashlib.sha256(rp_id.encode()).digest()
    ):
        raise PasskeyError("passkey was created for a different admin hostname")
    flags = auth_data[32]
    # User Presence and User Verification are both mandatory. Backup State
    # without Backup Eligibility is structurally invalid.
    if not flags & 0x01 or not flags & 0x04 or (flags & 0x10 and not flags & 0x08):
        raise PasskeyError("passkey did not verify the local user")
    if require_attested_credential != bool(flags & 0x40):
        raise PasskeyError("passkey authenticator data has unexpected credential data")
    return flags, int.from_bytes(auth_data[33:37], "big")


def _registration_auth_data(
    auth_data: bytes,
    rp_id: str,
) -> tuple[int, int, str, bytes]:
    flags, sign_count = _validate_authenticator_data(
        auth_data, rp_id, require_attested_credential=True
    )
    if len(auth_data) < 55:
        raise PasskeyError("passkey registration data is truncated")
    credential_length = int.from_bytes(auth_data[53:55], "big")
    credential_end = 55 + credential_length
    if credential_length == 0 or credential_length > 1024 or credential_end >= len(auth_data):
        raise PasskeyError("passkey credential id is invalid")
    credential_id = _b64encode(auth_data[55:credential_end])
    cose_key, key_end = _decode_cbor(auth_data, credential_end)
    if flags & 0x80:
        _, key_end = _decode_cbor(auth_data, key_end)
    if key_end != len(auth_data) or not isinstance(cose_key, dict):
        raise PasskeyError("passkey public key is invalid")
    if (
        cose_key.get(1) != EC2_KEY_TYPE
        or cose_key.get(3) != ES256_ALGORITHM
        or cose_key.get(-1) != P256_CURVE
    ):
        raise PasskeyError("passkey must use ES256 on the P-256 curve")
    x, y = cose_key.get(-2), cose_key.get(-3)
    if not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
        raise PasskeyError("passkey P-256 public key is invalid")
    public_key = _P256_SPKI_PREFIX + x + y
    if not _valid_public_key(public_key):
        raise PasskeyError("passkey P-256 public key is invalid")
    return flags, sign_count, credential_id, public_key


def _valid_public_key(public_key: bytes) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="kern-passkey.") as directory:
            path = Path(directory) / "public.der"
            path.write_bytes(public_key)
            result = subprocess.run(
                [
                    OPENSSL_BIN,
                    "pkey",
                    "-pubin",
                    "-inform",
                    "DER",
                    "-in",
                    str(path),
                    "-pubcheck",
                    "-noout",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _verify_es256(public_key: bytes, signature: bytes, signed: bytes) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="kern-passkey.") as directory:
            public_path = Path(directory) / "public.der"
            signature_path = Path(directory) / "signature.der"
            public_path.write_bytes(public_key)
            signature_path.write_bytes(signature)
            result = subprocess.run(
                [
                    OPENSSL_BIN,
                    "dgst",
                    "-sha256",
                    "-verify",
                    str(public_path),
                    "-keyform",
                    "DER",
                    "-signature",
                    str(signature_path),
                ],
                input=signed,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _decode_cbor(data: bytes, offset: int = 0, depth: int = 0) -> tuple[Any, int]:
    """Decode the small definite-length CBOR subset used by WebAuthn/COSE."""
    if depth > MAX_CBOR_DEPTH or offset >= len(data):
        raise PasskeyError("passkey CBOR is invalid")
    initial = data[offset]
    offset += 1
    major, additional = initial >> 5, initial & 0x1F
    length, offset = _cbor_length(data, offset, additional)
    if major == 0:
        return length, offset
    if major == 1:
        return -1 - length, offset
    if major in (2, 3):
        end = offset + length
        if length > MAX_BINARY_FIELD_BYTES or end > len(data):
            raise PasskeyError("passkey CBOR is invalid")
        raw = data[offset:end]
        if major == 2:
            return raw, end
        try:
            return raw.decode("utf-8"), end
        except UnicodeDecodeError as exc:
            raise PasskeyError("passkey CBOR text is invalid") from exc
    if major == 4:
        if length > MAX_CBOR_ITEMS:
            raise PasskeyError("passkey CBOR array is too large")
        items = []
        for _ in range(length):
            item, offset = _decode_cbor(data, offset, depth + 1)
            items.append(item)
        return items, offset
    if major == 5:
        if length > MAX_CBOR_ITEMS:
            raise PasskeyError("passkey CBOR map is too large")
        value: dict[Any, Any] = {}
        for _ in range(length):
            key, offset = _decode_cbor(data, offset, depth + 1)
            try:
                duplicate = key in value
            except TypeError as exc:
                raise PasskeyError("passkey CBOR map key is invalid") from exc
            if duplicate:
                raise PasskeyError("passkey CBOR map has duplicate keys")
            item, offset = _decode_cbor(data, offset, depth + 1)
            value[key] = item
        return value, offset
    if major == 7:
        if additional == 20:
            return False, offset
        if additional == 21:
            return True, offset
        if additional == 22:
            return None, offset
    raise PasskeyError("passkey CBOR uses an unsupported encoding")


def _cbor_length(data: bytes, offset: int, additional: int) -> tuple[int, int]:
    if additional < 24:
        return additional, offset
    sizes = {24: 1, 25: 2, 26: 4, 27: 8}
    size = sizes.get(additional)
    if size is None or offset + size > len(data):
        raise PasskeyError("passkey CBOR uses an indefinite or invalid length")
    value = int.from_bytes(data[offset:offset + size], "big")
    # Reject non-canonical length encodings.
    minimum = {1: 24, 2: 256, 4: 65536, 8: 4294967296}[size]
    if value < minimum:
        raise PasskeyError("passkey CBOR length is not canonical")
    return value, offset + size
