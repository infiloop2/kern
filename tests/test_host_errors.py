"""Tests for structured host-error reporting and journal ingestion."""

from __future__ import annotations

import hashlib
import json
import math
import unittest
from unittest.mock import Mock, patch

from host.runtime.core import host_errors
from host.runtime.host_diagnostics_collector import collector


def journal_row(
    payload: object,
    *,
    unit: str = "kern-admin-api.service",
    realtime: int = 1_800_000_000_000_000,
) -> str:
    return json.dumps(
        {
            "__REALTIME_TIMESTAMP": str(realtime),
            "_SYSTEMD_UNIT": unit,
            "_PID": "4321",
            "_BOOT_ID": "boot-1",
            "MESSAGE": json.dumps(payload),
        }
    )


class HostErrorReporterTests(unittest.TestCase):
    @patch("host.runtime.core.host_errors.emit_record")
    def test_codex_validation_failure_reaches_the_diagnostic_collector(self, emit: Mock) -> None:
        from host.runtime.agent_runtime import orchestrator

        with patch.dict(orchestrator._RUNTIME_STATUSES, {}, clear=True):
            orchestrator._set_runtime_status("codex-2", "error", "workspace routing discovery failed; private token")
        record = emit.call_args.args[0]
        _, event = collector.parse_journal_record(journal_row(record))
        self.assertIsNotNone(event)
        self.assertEqual(event["kind"], "provider_failure")
        self.assertEqual(event["context"]["agent_runtime"], "codex-2")
        self.assertEqual(event["context"]["failure_code"], "workspace_routing_failed")
        self.assertNotIn("private token", json.dumps(event))

    def test_exception_record_is_bounded_and_omits_arbitrary_context(self) -> None:
        secret = object()
        try:
            raise RuntimeError("broken request")
        except RuntimeError as exc:
            record = host_errors._exception_record(
                "admin_api.request",
                exc,
                {
                    "method": "GET",
                    "large": "x" * 2_000,
                    "secret": secret,
                    "nested": {"header": "must not persist"},
                    "not_finite": math.nan,
                },
            )

        self.assertEqual(record["kind"], "unexpected_exception")
        self.assertEqual(record["severity"], "error")
        self.assertEqual(record["exception_type"], "RuntimeError")
        self.assertEqual(record["summary"], "broken request")
        self.assertEqual(record["context"]["method"], "GET")
        self.assertEqual(len(record["context"]["large"]), 512)
        self.assertNotIn("secret", record["context"])
        self.assertNotIn("nested", record["context"])
        self.assertNotIn("not_finite", record["context"])
        self.assertNotIn(repr(secret), json.dumps(record))
        self.assertRegex(record["fingerprint"], r"^[0-9a-f]{64}$")
        self.assertIn("test_exception_record_is_bounded", record["traceback"])

    def test_provider_response_survives_reporter_and_collector_beyond_512_bytes(self):
        from host.runtime.tools.tools_host import _provider_warning_context
        from host.tools.shared.web import ProviderWarning
        body = json.dumps({"next_step": "explanation " * 100, "payment_options": ["example"]})
        warning = ProviderWarning("Upwork", "create_proposal_preview", "Missing terms", body=body.encode())
        record = host_errors._exception_record("tools.provider_request_approved", warning,
            _provider_warning_context("upwork", "submit_proposal", warning), severity="warning", kind="provider_failure")
        _, event = collector.parse_journal_record(journal_row(record))
        self.assertEqual(event["context"]["provider_response"], body)
        self.assertNotIn("diagnostic_context_truncated", event["context"])

    def test_diagnostic_truncation_is_explicit_and_escaped_context_stays_bounded(self):
        for body in ("x" * 9000, "\\" * 9000, "界" * 9000):
            context = host_errors._safe_context({"provider_response": body, "provider_response_truncated": False})
            self.assertTrue(context["diagnostic_context_truncated"])
            self.assertTrue(context["provider_response_truncated"])
            self.assertLessEqual(len(context["provider_response"].encode()), 8192)
            self.assertLessEqual(len(json.dumps(context).encode()), host_errors.MAX_CONTEXT_BYTES)
        context = host_errors._safe_context({str(i): "x" * 512 for i in range(200)})
        self.assertTrue(context["diagnostic_context_truncated"])
        self.assertLessEqual(len(json.dumps(context).encode()), host_errors.MAX_CONTEXT_BYTES)

    def test_aggregate_eviction_preserves_provider_truncation_markers(self):
        for body, upstream_truncated in (("\\" * 9000, False), ("partial", True)):
            for provider_first in (True, False):
                with self.subTest(upstream=upstream_truncated, provider_first=provider_first):
                    provider = {"provider_response": body, "provider_response_truncated": upstream_truncated}
                    other = {str(i): "x" * 512 for i in range(200)}
                    raw = {**provider, **other} if provider_first else {**other, **provider}
                    context = host_errors._safe_context(raw)
                    self.assertTrue(context["provider_response_truncated"])
                    self.assertTrue(context["diagnostic_context_truncated"])
                    self.assertLessEqual(len(json.dumps(context).encode()), host_errors.MAX_CONTEXT_BYTES)
                    if provider_first:
                        self.assertEqual(context["provider_response"], body[:8192])

    @patch("host.runtime.core.host_errors.subprocess.run")
    def test_emit_record_uses_structured_journal_field(self, run: Mock) -> None:
        host_errors.emit_record({"kind": "service_exit", "summary": "stopped"})

        run.assert_called_once()
        call = run.call_args
        self.assertEqual(call.args[0], ["/usr/bin/logger", "--journald"])
        self.assertIn("KERN_HOST_DIAGNOSTIC=1\n", call.kwargs["input"])
        self.assertIn("SYSLOG_IDENTIFIER=kern-host-diagnostic\n", call.kwargs["input"])
        self.assertIn("PRIORITY=3\n", call.kwargs["input"])
        self.assertTrue(call.kwargs["check"])

    @patch("host.runtime.core.host_errors.subprocess.run")
    def test_emit_record_uses_warning_journal_priority(self, run: Mock) -> None:
        host_errors.emit_record(
            {"severity": "warning", "kind": "unexpected_behavior", "summary": "odd"}
        )

        self.assertIn("PRIORITY=4\n", run.call_args.kwargs["input"])

    @patch("host.runtime.core.host_errors.subprocess.run")
    def test_emit_record_uses_info_journal_priority(self, run: Mock) -> None:
        host_errors.emit_record({"severity": "info", "kind": "maintenance", "summary": "cleanup complete"})
        self.assertIn("PRIORITY=6\n", run.call_args.kwargs["input"])

    @patch("host.runtime.core.host_errors.emit_record")
    def test_maintenance_information_reaches_the_diagnostic_collector(self, emit: Mock) -> None:
        host_errors.report_info(
            "admin_api.codex_session_sweep", "cleanup completed",
            context={"runtime": "codex", "retired_roots": 3, "private_object": object()},
        )
        record = emit.call_args.args[0]
        _, event = collector.parse_journal_record(journal_row(record))
        self.assertIsNotNone(event)
        self.assertEqual(event["severity"], "info")
        self.assertEqual(event["kind"], "maintenance")
        self.assertEqual(event["context"], {"runtime": "codex", "retired_roots": 3})
        self.assertEqual(event["traceback"], "")
        self.assertEqual(event["exception_type"], "")

    @patch("host.runtime.core.host_errors.emit_record")
    def test_warning_record_is_distinct_from_service_errors(self, emit: Mock) -> None:
        error = RuntimeError("provider rejected request")
        host_errors.report_warning(
            "tools.provider_request",
            error,
            context={"provider": "X", "http_status": 403},
            kind="provider_failure",
        )

        record = emit.call_args.args[0]
        self.assertEqual(record["severity"], "warning")
        self.assertEqual(record["kind"], "provider_failure")
        self.assertEqual(record["context"], {"provider": "X", "http_status": 403})

    def test_warning_fingerprint_distinguishes_summary_and_context(self) -> None:
        first = host_errors._exception_record(
            "tools.provider_request",
            RuntimeError("forbidden"),
            {"action_id": "post", "http_status": 403},
            severity="warning",
            kind="provider_failure",
        )
        different_summary = host_errors._exception_record(
            "tools.provider_request",
            RuntimeError("rate limited"),
            {"action_id": "post", "http_status": 403},
            severity="warning",
            kind="provider_failure",
        )
        different_context = host_errors._exception_record(
            "tools.provider_request",
            RuntimeError("forbidden"),
            {"action_id": "delete", "http_status": 403},
            severity="warning",
            kind="provider_failure",
        )

        self.assertNotEqual(first["fingerprint"], different_summary["fingerprint"])
        self.assertNotEqual(first["fingerprint"], different_context["fingerprint"])

    @patch("host.runtime.core.host_errors.emit_record")
    def test_service_exit_reports_only_abnormal_results(self, emit: Mock) -> None:
        host_errors.report_service_exit("kern-tools", "success", "exited", "0")
        emit.assert_not_called()

        host_errors.report_service_exit("kern-tools", "exit-code", "exited", "1")
        record = emit.call_args.args[0]
        self.assertEqual(record["severity"], "error")
        self.assertEqual(record["kind"], "service_exit")
        self.assertEqual(record["context"]["service"], "kern-tools")
        self.assertEqual(
            record["fingerprint"],
            hashlib.sha256(
                b"service_exit\nkern-tools\nexit-code\nexited\n1"
            ).hexdigest(),
        )


class HostErrorCollectorTests(unittest.TestCase):
    UNITS = frozenset({"kern-admin-api.service"})
    PAYLOAD = {
        "component": "admin_api.request",
        "severity": "error",
        "kind": "unexpected_exception",
        "exception_type": "RuntimeError",
        "summary": "broken request",
        "traceback": 'File "host/runtime/admin_api/service.py", line 1, in handle',
        "context": {"method": "GET"},
        "fingerprint": "a" * 64,
        "host_version": "1.3.3",
    }

    def test_parse_accepts_only_an_allowed_originating_unit(self) -> None:
        realtime, event = collector.parse_journal_record(
            journal_row(self.PAYLOAD),
            units=frozenset({"kern-admin-api.service"}),
        )
        self.assertEqual(realtime, 1_800_000_000_000_000)
        assert event is not None
        self.assertEqual(event["service"], "kern-admin-api")
        self.assertEqual(event["pid"], 4321)
        self.assertEqual(event["boot_id"], "boot-1")

        _, rejected = collector.parse_journal_record(
            journal_row(self.PAYLOAD, unit="agent-created.service"),
            units=frozenset({"kern-admin-api.service"}),
        )
        self.assertIsNone(rejected)

    def test_transcription_service_exits_reach_the_error_feed(self) -> None:
        units = collector.allowed_units()
        self.assertIn("_SYSTEMD_UNIT=kern-transcription.service", collector.journal_command(units))
        _, event = collector.parse_journal_record(
            journal_row({**self.PAYLOAD, "kind": "service_exit"}, unit="kern-transcription.service"),
            units=units,
        )
        assert event is not None
        self.assertEqual(event["service"], "kern-transcription")
        self.assertEqual(event["kind"], "service_exit")

    def test_browser_service_exits_reach_the_error_feed(self) -> None:
        units = collector.allowed_units()
        self.assertIn("_SYSTEMD_UNIT=kern-browser.service", collector.journal_command(units))
        _, event = collector.parse_journal_record(
            journal_row({**self.PAYLOAD, "kind": "service_exit"}, unit="kern-browser.service"),
            units=units,
        )
        assert event is not None
        self.assertEqual(event["service"], "kern-browser")

    def test_journal_command_follows_only_new_trusted_unit_records(self) -> None:
        command = collector.journal_command(self.UNITS)
        self.assertIn("KERN_HOST_DIAGNOSTIC=1", command)
        self.assertIn("_SYSTEMD_UNIT=kern-admin-api.service", command)
        self.assertIn("--follow", command)
        self.assertIn("--lines=0", command)
        self.assertFalse(any(argument.startswith("--since=") for argument in command))
        self.assertFalse(any(argument.startswith("--after-cursor=") for argument in command))

    @patch("host.runtime.host_diagnostics_collector.collector.state.ingest_host_diagnostic")
    def test_invalid_payload_is_skipped_without_database_work(self, ingest: Mock) -> None:
        process = Mock()
        process.stdout = iter([journal_row({"kind": "invalid"})])
        process.wait.return_value = 0

        self.assertTrue(
            collector._consume(process, frozenset({"kern-admin-api.service"}))
        )
        ingest.assert_not_called()
        process.terminate.assert_not_called()

    @patch("host.runtime.host_diagnostics_collector.collector.state.ingest_host_diagnostic")
    def test_untrusted_unit_is_skipped_without_database_work(self, ingest: Mock) -> None:
        process = Mock()
        process.stdout = iter([
            journal_row(self.PAYLOAD, unit="agent-created.scope"),
        ])
        process.wait.return_value = 0

        self.assertTrue(collector._consume(process, self.UNITS))
        ingest.assert_not_called()

    @patch(
        "host.runtime.host_diagnostics_collector.collector.state.ingest_host_diagnostic",
        side_effect=RuntimeError("database unavailable"),
    )
    def test_database_failure_stops_the_stream(self, ingest: Mock) -> None:
        process = Mock()
        process.stdout = iter([journal_row(self.PAYLOAD)])
        process.wait.return_value = 0

        self.assertFalse(
            collector._consume(process, frozenset({"kern-admin-api.service"}))
        )
        ingest.assert_called_once()
        process.terminate.assert_called_once()

    def test_payload_bounds_and_fingerprint_are_validated(self) -> None:
        invalid = dict(self.PAYLOAD, fingerprint="not-a-fingerprint")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            collector.parse_journal_record(
                journal_row(invalid),
                units=frozenset({"kern-admin-api.service"}),
            )
        oversized = dict(self.PAYLOAD, summary="x" * (host_errors.MAX_SUMMARY_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "storage bound"):
            collector.parse_journal_record(
                journal_row(oversized),
                units=frozenset({"kern-admin-api.service"}),
            )
        oversized_traceback = dict(
            self.PAYLOAD,
            traceback="x" * (host_errors.MAX_TRACEBACK_BYTES + 1),
        )
        with self.assertRaisesRegex(ValueError, "storage bound"):
            collector.parse_journal_record(
                journal_row(oversized_traceback),
                units=frozenset({"kern-admin-api.service"}),
            )

    def test_nul_bytes_are_escaped_before_postgres_ingest(self) -> None:
        payload = dict(
            self.PAYLOAD,
            summary="bad\x00input",
            traceback="frame\x00source",
            context={
                "route": "/bad\x00path",
                "bad\x00key": "discarded",
            },
        )
        _, event = collector.parse_journal_record(
            journal_row(payload),
            units=frozenset({"kern-admin-api.service"}),
        )
        assert event is not None
        self.assertEqual(event["summary"], r"bad\0input")
        self.assertEqual(event["traceback"], r"frame\0source")
        self.assertEqual(event["context"], {"route": r"/bad\0path"})
        self.assertNotIn("\x00", json.dumps(event))


if __name__ == "__main__":
    unittest.main()
