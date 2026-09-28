"""Operator boundary, input limits and recovery-safe transcription failures."""

import base64
from http import HTTPStatus
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.admin_api.errors import ApiError
from host.runtime.transcription import client, diagnostics, service


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.report = self.enterContext(patch.object(diagnostics, "report"))

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
        self.assertIn("TRANSCRIPTION_MODEL_TAG=model-faster-whisper-base.en-1", script)
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
        args = self.report.call_args.args
        self.assertEqual(args[:2], ("admin_api.dictation", "timeout"))
        self.assertEqual(args[2]["exception_type"], "TimeoutError")
        self.assertEqual(args[2]["timeout_seconds"], 60)
        self.assertNotIn("socket timed out", str(args))
        request_id = connection.request.call_args.args[3]["X-Kern-Dictation-Id"]
        self.assertEqual(args[2]["request_id"], request_id)

    def test_slow_worker_records_wall_and_cpu_time_without_payloads(self):
        handler = object.__new__(service.Handler)
        handler.path = "/transcribe"
        handler.headers = {"X-Kern-Dictation-Id": "a" * 32}
        handler.bounded_content_length = MagicMock(return_value=40)
        handler.read_json_object_body = MagicMock(return_value={"audio": "AAA="})
        with (patch.object(service, "transcribe", return_value="private words"),
              patch.object(service.Handler, "_peer", return_value=(10, 20)),
              patch.object(service, "allowed_uid", return_value=20),
              patch.object(service, "_model_instance", object()),
              patch.object(service.time, "monotonic", side_effect=[10, 25]),
              patch.object(service.time, "process_time", side_effect=[1, 3.5]),
              patch.object(service.Handler, "_send_json") as send):
            self.report.side_effect = lambda *args: self.assertFalse(service._inference_lock.locked())
            handler.do_POST()
        send.assert_called_once_with(200, {"text": "private words"})
        component, outcome, context = self.report.call_args.args
        self.assertEqual((component, outcome), ("transcription.inference", "slow_inference"))
        self.assertEqual(context["inference_ms"], 15000)
        self.assertEqual(context["cpu_ms"], 2500)
        self.assertEqual(context["request_id"], "a" * 32)
        self.assertNotIn("private words", str(context))
        self.assertNotIn("AAA=", str(context))

    def test_worker_failure_records_only_exception_class_and_safe_request_id(self):
        handler = object.__new__(service.Handler)
        handler.path = "/transcribe"
        handler.headers = {"X-Kern-Dictation-Id": "secret-untrusted-header"}
        handler.bounded_content_length = MagicMock(return_value=40)
        handler.read_json_object_body = MagicMock(return_value={"audio": "AAA="})
        with (patch.object(service, "transcribe", side_effect=RuntimeError("private speech and /host/path")),
              patch.object(service.Handler, "_peer", return_value=(10, 20)),
              patch.object(service, "allowed_uid", return_value=20),
              patch.object(service, "_model_instance", object()),
              patch.object(service.Handler, "_send_json") as send):
            handler.do_POST()
        send.assert_called_once_with(503, {"error": "transcription failed"})
        component, outcome, context = self.report.call_args.args
        self.assertEqual((component, outcome), ("transcription.inference", "inference_failure"))
        self.assertEqual(context["exception_type"], "RuntimeError")
        self.assertEqual(context["request_id"], "")
        self.assertNotIn("private speech", str(context))
        self.assertNotIn("/host/path", str(context))

    def test_fast_success_is_not_a_diagnostic(self):
        response = MagicMock(status=200)
        response.read.return_value = b'{"text":"Hello"}'
        connection = MagicMock()
        connection.getresponse.return_value = response
        with (patch.object(client, "_Connection", return_value=connection),
              patch.object(client.time, "monotonic", side_effect=[0, 1])):
            client.transcribe({"audio": "AAA="})
        self.report.assert_not_called()

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


class DiagnosticRateLimitTests(unittest.TestCase):
    def test_slow_warnings_are_bounded_without_hiding_failures(self):
        with (patch.object(diagnostics, "_last_report", {}),
              patch.object(diagnostics.time, "monotonic", side_effect=[0, 1, 2, 60]),
              patch.object(diagnostics.threading, "Thread") as thread):
            diagnostics.report("transcription.inference", "slow_inference", {"request_id": "first"})
            diagnostics.report("transcription.inference", "slow_inference", {"request_id": "second"})
            diagnostics.report("transcription.inference", "inference_failure", {})
            diagnostics.report("transcription.inference", "slow_inference", {"request_id": "third"})
        self.assertEqual(thread.call_count, 3)
        contexts = [call.kwargs["kwargs"]["context"] for call in thread.call_args_list]
        self.assertEqual(contexts[0]["request_id"], "first")
        self.assertEqual(contexts[1]["outcome"], "inference_failure")
        self.assertEqual(contexts[2]["request_id"], "third")

    def test_blocked_logger_does_not_delay_request_completion(self):
        logging_started, release_logger, logging_finished = (threading.Event() for _ in range(3))
        request_finished = threading.Event()

        def blocked_logger(*args, **kwargs):
            logging_started.set()
            release_logger.wait(5)
            logging_finished.set()

        def request():
            diagnostics.report("admin_api.dictation", "slow_request", {})
            request_finished.set()

        with (patch.object(diagnostics, "_last_report", {}),
              patch.object(diagnostics.host_errors, "report_warning", side_effect=blocked_logger)):
            caller = threading.Thread(target=request)
            caller.start()
            try:
                self.assertTrue(logging_started.wait(1))
                self.assertTrue(request_finished.wait(1), "request waited on the blocked logger")
                self.assertFalse(logging_finished.is_set())
            finally:
                release_logger.set()
                caller.join(2)
                self.assertTrue(logging_finished.wait(2))

    def test_reporter_start_failure_does_not_fail_dictation(self):
        with (patch.object(diagnostics, "_last_report", {}),
              patch.object(diagnostics.threading, "Thread", side_effect=RuntimeError("can't start thread"))):
            diagnostics.report("admin_api.dictation", "slow_request", {})
