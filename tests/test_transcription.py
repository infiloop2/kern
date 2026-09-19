"""Operator boundary, input limits and recovery-safe transcription failures."""

import base64
from http import HTTPStatus
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.admin_api.errors import ApiError
from host.runtime.transcription import client, service


class TranscriptionTests(unittest.TestCase):
    def test_accepts_only_bounded_pcm(self):
        audio = bytes(client.MAX_AUDIO_BYTES)
        self.assertEqual(client.decode_audio({"audio": base64.b64encode(audio).decode()}), audio)
        for value in (None, {}, {"audio": "?"}, {"audio": "YQ=="}, {"audio": ""},
                      {"audio": "AAA=", "path": "/etc/passwd"},
                      {"audio": "A" * (client.MAX_AUDIO_BYTES * 4 // 3 + 1)}):
            with self.subTest(value=str(value)[:80]), self.assertRaises(ValueError):
                client.decode_audio(value)

    def test_rejects_bad_input_before_opening_a_socket(self):
        with patch.object(client, "_Connection") as connection:
            with self.assertRaises(ApiError) as raised:
                client.transcribe({"audio": "broken"})
            self.assertEqual(raised.exception.status, HTTPStatus.BAD_REQUEST)
            connection.assert_not_called()

    def test_returns_text_and_always_closes_the_socket(self):
        response = MagicMock(status=200)
        response.read.return_value = b'{"text":"Hello Kern."}'
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(client, "_Connection", return_value=connection):
            self.assertEqual(client.transcribe({"audio": "AAA="}), {"text": "Hello Kern."})
        connection.close.assert_called_once()

    def test_failure_preserves_retry_semantics_without_exposing_audio(self):
        for status, raw in ((503, b'{}'), (200, b'<html>'), (200, b'{"text":[]}'),
                            (200, b'x' * (client.MAX_RESPONSE_BYTES + 1))):
            connection = MagicMock()
            connection.getresponse.return_value = MagicMock(status=status)
            connection.getresponse.return_value.read.return_value = raw
            with patch.object(client, "_Connection", return_value=connection):
                with self.assertRaises(ApiError) as raised:
                    client.transcribe({"audio": "AAA="})
                self.assertEqual(raised.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
            connection.close.assert_called_once()

    def test_service_fails_closed_when_admin_account_is_missing(self):
        with patch.object(service.pwd, "getpwnam", side_effect=KeyError):
            self.assertEqual(service.allowed_uid(), -1)

    def test_browser_route_is_operator_only(self):
        from host.runtime.admin_api import service as admin
        route = next(route for route in admin._ROUTES if route.path == "/v1/dictation/transcribe")
        self.assertTrue(route.operator_only)
        self.assertEqual(route.query_keys, frozenset())

    def test_bootstrap_model_and_service_are_offline_and_pinned(self):
        script = (Path(__file__).parents[1] / "host/bootstrap/bootstrap.sh").read_text()
        unit = script.split("cat > /etc/systemd/system/kern-transcription.service", 1)[1].split("\nUNIT", 1)[0]
        for setting in ("PrivateNetwork=yes", "ProtectSystem=strict", "MemoryMax=2G", "CPUQuota=200%", "HF_HUB_OFFLINE=1"):
            self.assertIn(setting, unit)
        self.assertIn("TRANSCRIPTION_MODEL_TAG=model-faster-whisper-small.en-1", script)
        from host.bootstrap.render import _render_bootstrap
        rendered = _render_bootstrap()
        self.assertIn('transcription_model_base="https://github.com/infiloop2/kern/releases/download/${TRANSCRIPTION_MODEL_TAG}"', rendered)
        self.assertIn('"${transcription_model_base}/${transcription_model_file}"', rendered)
        self.assertNotIn("@GITHUB_REPOSITORY@", rendered)
        self.assertIn('"$TRANSCRIPTION_MODEL_SHA256" | sha256sum --check --status', script)
        self.assertLess(client.MAX_REQUEST_BYTES, 1024 * 1024)

    def test_transport_failure_is_recoverable(self):
        connection = MagicMock()
        connection.request.side_effect = TimeoutError('socket timed out')
        with patch.object(client, '_Connection', return_value=connection):
            with self.assertRaises(ApiError) as raised:
                client.transcribe({'audio': 'AAA='})
        self.assertEqual(raised.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        connection.close.assert_called_once()

    def test_worker_rejects_non_admin_peer_before_reading_audio(self):
        handler = object.__new__(service.Handler)
        with (patch.object(service.Handler, '_peer', return_value=(10, 20)),
              patch.object(service, 'allowed_uid', return_value=30),
              patch.object(service.Handler, '_send_json') as send,
              patch.object(service, 'transcribe') as infer):
            handler.do_POST()
        send.assert_called_once_with(401, {'error': 'unauthorized'})
        infer.assert_not_called()

    def test_readiness_uses_short_timeout_and_preserves_model_state(self):
        connection = MagicMock()
        connection.getresponse.return_value = MagicMock(status=503)
        connection.getresponse.return_value.read.return_value = b'{"error":"model_not_ready"}'
        with patch.object(client, "_Connection", return_value=connection):
            with self.assertRaises(ApiError) as raised:
                client.readiness()
        self.assertIn("model isn't loaded", str(raised.exception))
        self.assertEqual(connection.timeout, 2)
        connection.close.assert_called_once()

    def test_unloaded_worker_rejects_audio_without_loading_or_reading_it(self):
        handler = object.__new__(service.Handler)
        handler.path = "/transcribe"
        with (patch.object(service.Handler, "_peer", return_value=(10, 20)),
              patch.object(service, "allowed_uid", return_value=20),
              patch.object(service, "_model_instance", None),
              patch.object(service.Handler, "_send_json") as send,
              patch.object(service.Handler, "_transcribe_request") as read,
              patch.object(service, "load_model") as load):
            handler.do_POST()
            with self.assertRaisesRegex(RuntimeError, "model not ready"):
                service.transcribe(b"\0\0")
        send.assert_called_once_with(503, {"error": "model_not_ready"})
        read.assert_not_called()
        load.assert_not_called()

    def test_busy_worker_does_not_queue_audio_or_block_readiness(self):
        handler = object.__new__(service.Handler)
        with (patch.object(service.Handler, "_peer", return_value=(10, 20)),
              patch.object(service, "allowed_uid", return_value=20),
              patch.object(service, "_model_instance", object()),
              patch.object(service.Handler, "_send_json") as send,
              patch.object(service.Handler, "_transcribe_request") as read,
              service._inference_lock):
            handler.path = "/transcribe"
            handler.do_POST()
            send.assert_called_with(503, {"error": "busy"})
            handler.path = "/ready"
            handler.do_GET()
            send.assert_called_with(200, {"ready": True})
        read.assert_not_called()

    def test_readiness_route_is_operator_only_and_service_stays_resident(self):
        from host.runtime.admin_api import service as admin
        route = next(route for route in admin._ROUTES if route.path == "/v1/dictation/ready")
        self.assertTrue(route.operator_only)
        script = (Path(__file__).parents[1] / "host/bootstrap/bootstrap.sh").read_text()
        self.assertIn("systemctl enable --now kern-embedding.socket kern-transcription.socket kern-transcription.service", script)
        unit = script.split("cat > /etc/systemd/system/kern-transcription.service", 1)[1].split("\nUNIT", 1)[0]
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("WantedBy=multi-user.target", unit)

    def test_bootstrap_preflight_explicitly_loads_before_transcribing(self):
        script = (Path(__file__).parents[1] / "host/bootstrap/bootstrap.sh").read_text()
        block = script.split("from host.runtime.transcription.service import", 1)[1].split("PYTHON", 1)[0]
        code = "from host.runtime.transcription.service import" + block
        calls = []
        with (patch.object(service, "load_model", side_effect=lambda: calls.append("load")),
              patch.object(service, "transcribe", side_effect=lambda audio: calls.append("transcribe") or "")):
            exec(code, {})
        self.assertEqual(calls, ["load", "transcribe"])
