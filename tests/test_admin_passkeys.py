from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pg_harness

from host.runtime.admin_api import admin_passkeys
from host.runtime.core import state


RP_ID = "admin.example.com"
ORIGIN = f"https://{RP_ID}"


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def cbor(value) -> bytes:
    if isinstance(value, bool):
        return bytes([0xF5 if value else 0xF4])
    if isinstance(value, int):
        if value >= 0:
            return cbor_head(0, value)
        return cbor_head(1, -1 - value)
    if isinstance(value, bytes):
        return cbor_head(2, len(value)) + value
    if isinstance(value, str):
        encoded = value.encode()
        return cbor_head(3, len(encoded)) + encoded
    if isinstance(value, list):
        return cbor_head(4, len(value)) + b"".join(cbor(item) for item in value)
    if isinstance(value, dict):
        return cbor_head(5, len(value)) + b"".join(
            cbor(key) + cbor(item) for key, item in value.items()
        )
    raise TypeError(type(value))


def cbor_head(major: int, length: int) -> bytes:
    if length < 24:
        return bytes([(major << 5) | length])
    if length < 256:
        return bytes([(major << 5) | 24, length])
    if length < 65536:
        return bytes([(major << 5) | 25]) + length.to_bytes(2, "big")
    return bytes([(major << 5) | 26]) + length.to_bytes(4, "big")


class AdminPasskeyTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        # Pending ceremonies are process-global and single-use; clear them on
        # the way out as well as in, so an abandoned ceremony from these tests
        # cannot survive into another module (test_admin_auth.py:13 uses the
        # same symmetric pattern for its session/failure maps).
        admin_passkeys._login_ceremonies.clear()
        admin_passkeys._registration_ceremonies.clear()
        self.addCleanup(admin_passkeys._login_ceremonies.clear)
        self.addCleanup(admin_passkeys._registration_ceremonies.clear)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.private_key = Path(self.temporary.name) / "private.pem"
        self.public_key = Path(self.temporary.name) / "public.der"
        subprocess.run(
            [
                "/usr/bin/openssl", "ecparam", "-name", "prime256v1",
                "-genkey", "-noout", "-out", str(self.private_key),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "/usr/bin/openssl", "pkey", "-in", str(self.private_key),
                "-pubout", "-outform", "DER", "-out", str(self.public_key),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        spki = self.public_key.read_bytes()
        self.assertEqual(spki[: len(admin_passkeys._P256_SPKI_PREFIX)], admin_passkeys._P256_SPKI_PREFIX)
        coordinates = spki[len(admin_passkeys._P256_SPKI_PREFIX):]
        self.assertEqual(len(coordinates), 64)
        self.x, self.y = coordinates[:32], coordinates[32:]
        self.credential_id = b"credential-id"

    def register(self) -> None:
        options = admin_passkeys.begin_registration(
            "session-hash",
            rp_id=RP_ID,
            origin=ORIGIN,
            agent_name="Kern",
        )
        client_data = json.dumps({
            "type": "webauthn.create",
            "challenge": options["challenge"],
            "origin": ORIGIN,
            "crossOrigin": False,
        }, separators=(",", ":")).encode()
        cose = cbor({
            1: admin_passkeys.EC2_KEY_TYPE,
            3: admin_passkeys.ES256_ALGORITHM,
            -1: admin_passkeys.P256_CURVE,
            -2: self.x,
            -3: self.y,
        })
        auth_data = (
            hashlib.sha256(RP_ID.encode()).digest()
            + bytes([0x4D])
            + (0).to_bytes(4, "big")
            + (b"\0" * 16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + cose
        )
        response = {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "response": {
                "clientDataJSON": b64(client_data),
                "attestationObject": b64(cbor({
                    "fmt": "none",
                    "attStmt": {},
                    "authData": auth_data,
                })),
                "transports": ["internal", "hybrid"],
            },
        }
        result = admin_passkeys.finish_registration("session-hash", response)
        self.assertTrue(result["configured"])

    def assertion(self, options: dict, *, origin: str = ORIGIN, sign_count: int = 0) -> dict:
        client_data = json.dumps({
            "type": "webauthn.get",
            "challenge": options["challenge"],
            "origin": origin,
            "crossOrigin": False,
        }, separators=(",", ":")).encode()
        auth_data = (
            hashlib.sha256(RP_ID.encode()).digest()
            + bytes([0x0D])
            + sign_count.to_bytes(4, "big")
        )
        signed_path = Path(self.temporary.name) / "signed.bin"
        signature_path = Path(self.temporary.name) / "signature.der"
        signed_path.write_bytes(auth_data + hashlib.sha256(client_data).digest())
        subprocess.run(
            [
                "/usr/bin/openssl", "dgst", "-sha256",
                "-sign", str(self.private_key),
                "-out", str(signature_path), str(signed_path),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        user_handle = state.admin_passkey_config()["user_handle"]
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "authenticatorAttachment": "platform",
            "response": {
                "clientDataJSON": b64(client_data),
                "authenticatorData": b64(auth_data),
                "signature": b64(signature_path.read_bytes()),
                "userHandle": user_handle,
            },
        }

    def test_real_es256_registration_and_login(self) -> None:
        self.register()
        token, options = admin_passkeys.begin_login(
            rp_id=RP_ID, origin=ORIGIN, client_key="cf4:203.0.113.1"
        )
        admin_passkeys.finish_login(
            token,
            self.assertion(options),
            client_key="cf4:203.0.113.1",
        )
        stored = state.admin_passkeys(RP_ID)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["transports"], ["hybrid", "internal"])
        self.assertIsNotNone(stored[0]["last_used_at"])

    def test_second_admin_passkey_registration_is_rejected(self) -> None:
        self.register()
        with self.assertRaisesRegex(
            admin_passkeys.PasskeyError,
            "already has an admin passkey",
        ):
            admin_passkeys.begin_registration(
                "second-session",
                rp_id=RP_ID,
                origin=ORIGIN,
                agent_name="Kern",
            )

        with self.assertRaises(state.AdminPasskeyLimitError):
            state.save_admin_passkey(
                user_handle=state.admin_passkey_config()["user_handle"],
                credential_id="another-credential",
                rp_id=RP_ID,
                public_key_spki=b64(self.public_key.read_bytes()),
                sign_count=0,
                transports=[],
                backed_up=False,
                created_at=state.utc_now(),
            )

    def test_signature_counter_cannot_regress_from_nonzero_to_zero(self) -> None:
        self.register()
        token, options = admin_passkeys.begin_login(
            rp_id=RP_ID, origin=ORIGIN, client_key="source"
        )
        admin_passkeys.finish_login(
            token,
            self.assertion(options, sign_count=5),
            client_key="source",
        )

        token, options = admin_passkeys.begin_login(
            rp_id=RP_ID, origin=ORIGIN, client_key="source"
        )
        with self.assertRaisesRegex(
            admin_passkeys.PasskeyError,
            "signature counter did not advance",
        ):
            admin_passkeys.finish_login(
                token,
                self.assertion(options, sign_count=0),
                client_key="source",
            )

        self.assertEqual(state.admin_passkeys(RP_ID)[0]["sign_count"], 5)

    def test_openssl_operational_failures_fail_closed(self) -> None:
        failures = (
            FileNotFoundError("openssl is unavailable"),
            subprocess.TimeoutExpired(cmd=admin_passkeys.OPENSSL_BIN, timeout=5),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(
                    admin_passkeys.subprocess,
                    "run",
                    side_effect=failure,
                ):
                    self.assertFalse(admin_passkeys._valid_public_key(b"public"))
                    self.assertFalse(
                        admin_passkeys._verify_es256(
                            b"public",
                            b"signature",
                            b"signed",
                        )
                    )

    def test_login_ceremony_is_single_use_and_bound_to_source(self) -> None:
        self.register()
        token, options = admin_passkeys.begin_login(
            rp_id=RP_ID, origin=ORIGIN, client_key="source-a"
        )
        response = self.assertion(options)
        with self.assertRaisesRegex(admin_passkeys.PasskeyError, "expired"):
            admin_passkeys.finish_login(token, response, client_key="source-b")
        with self.assertRaisesRegex(admin_passkeys.PasskeyError, "expired"):
            admin_passkeys.finish_login(token, response, client_key="source-a")

    def test_wrong_origin_is_rejected(self) -> None:
        self.register()
        token, options = admin_passkeys.begin_login(
            rp_id=RP_ID, origin=ORIGIN, client_key="source"
        )
        with self.assertRaisesRegex(admin_passkeys.PasskeyError, "client data"):
            admin_passkeys.finish_login(
                token,
                self.assertion(options, origin="https://phishing.example"),
                client_key="source",
            )

    def test_expired_registration_is_rejected(self) -> None:
        with patch.object(admin_passkeys, "_now", return_value=0):
            admin_passkeys.begin_registration(
                "session", rp_id=RP_ID, origin=ORIGIN, agent_name="Kern"
            )
        with patch.object(
            admin_passkeys,
            "_now",
            return_value=admin_passkeys.CEREMONY_TIMEOUT_SECONDS + 1,
        ):
            with self.assertRaisesRegex(admin_passkeys.PasskeyError, "expired"):
                admin_passkeys.finish_registration("session", {})

    def test_reconfigure_reset_removes_only_passkeys(self) -> None:
        self.register()
        state.save_config({
            "agent_name": "keep",
            "admin_password_sha256": "a" * 64,
            "operator_connections": [{
                "mode": "ssh",
                "ssh_public_key": "ssh-ed25519 AAAATEST operator@example",
            }],
        })
        self.assertEqual(state.reset_admin_passkeys(), 1)
        self.assertEqual(state.admin_passkeys(), [])
        self.assertIsNone(state.admin_passkey_config())
        self.assertEqual(state.load_config()["admin_password_sha256"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
