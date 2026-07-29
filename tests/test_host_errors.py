"""Tests for structured host-error reporting and journal ingestion."""

from __future__ import annotations

import hashlib
import json
import math
import unittest
from unittest.mock import Mock, patch

from host.runtime.core import host_errors
from host.runtime.host_errors import collector


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

    @patch("host.runtime.core.host_errors.subprocess.run")
    def test_emit_record_uses_structured_journal_field(self, run: Mock) -> None:
        host_errors.emit_record({"kind": "service_exit", "summary": "stopped"})

        run.assert_called_once()
        call = run.call_args
        self.assertEqual(call.args[0], ["/usr/bin/logger", "--journald"])
        self.assertIn("KERN_HOST_ERROR=1\n", call.kwargs["input"])
        self.assertIn("SYSLOG_IDENTIFIER=kern-host-error\n", call.kwargs["input"])
        self.assertTrue(call.kwargs["check"])

    @patch("host.runtime.core.host_errors.emit_record")
    def test_service_exit_reports_only_abnormal_results(self, emit: Mock) -> None:
        host_errors.report_service_exit("kern-tools", "success", "exited", "0")
        emit.assert_not_called()

        host_errors.report_service_exit("kern-tools", "exit-code", "exited", "1")
        record = emit.call_args.args[0]
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

    def test_journal_command_follows_only_new_trusted_unit_records(self) -> None:
        command = collector.journal_command(self.UNITS)
        self.assertIn("KERN_HOST_ERROR=1", command)
        self.assertIn("_SYSTEMD_UNIT=kern-admin-api.service", command)
        self.assertIn("--follow", command)
        self.assertIn("--lines=0", command)
        self.assertFalse(any(argument.startswith("--since=") for argument in command))
        self.assertFalse(any(argument.startswith("--after-cursor=") for argument in command))

    @patch("host.runtime.host_errors.collector.state.ingest_host_error")
    def test_invalid_payload_is_skipped_without_database_work(self, ingest: Mock) -> None:
        process = Mock()
        process.stdout = iter([journal_row({"kind": "invalid"})])
        process.wait.return_value = 0

        self.assertTrue(
            collector._consume(process, frozenset({"kern-admin-api.service"}))
        )
        ingest.assert_not_called()
        process.terminate.assert_not_called()

    @patch("host.runtime.host_errors.collector.state.ingest_host_error")
    def test_untrusted_unit_is_skipped_without_database_work(self, ingest: Mock) -> None:
        process = Mock()
        process.stdout = iter([
            journal_row(self.PAYLOAD, unit="agent-created.scope"),
        ])
        process.wait.return_value = 0

        self.assertTrue(collector._consume(process, self.UNITS))
        ingest.assert_not_called()

    @patch(
        "host.runtime.host_errors.collector.state.ingest_host_error",
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
