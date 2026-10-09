import json
import unittest
from unittest.mock import patch

from host.tools.shared.ads_diagnostics import AdsProviderError, ads_error_body, ads_failure_context, report_ads_failure
from host.tools.shared.web import WebRequestError, ProviderWarning
from host.runtime.host_diagnostics_collector.collector import parse_journal_record


class AdsDiagnosticsTests(unittest.TestCase):
    def test_causal_provider_error_keeps_only_error_envelope_in_operator_record(self):
        body = json.dumps({"errors": [{"code": "INVALID_PARAMETER", "message": "daily budget rejected"}],
                           "request": {"token": "must-not-be-recorded"}, "data": {"caption": "private caption"}}).encode()
        try:
            try:
                raise AdsProviderError("X Ads rejected these parameters.", "POST /accounts/a/campaigns", status=400, body=body)
            except RuntimeError as cause:
                raise RuntimeError("Creation failed; confirmed campaign c1.") from cause
        except RuntimeError as exc:
            with patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
                report_ads_failure("x_ads", "launch_campaign", exc, phase="create ad group", confirmed=("campaign c1",))
        emit.assert_called_once()
        record = emit.call_args.args[0]
        self.assertEqual(record["severity"], "warning")
        self.assertEqual(record["kind"], "provider_failure")
        context = record["context"]
        self.assertEqual(context["http_status"], 400)
        self.assertEqual(context["operation"], "POST /accounts/a/campaigns")
        self.assertEqual(context["confirmed_resources"], "campaign c1")
        self.assertIn("daily budget rejected", context["provider_response"])
        self.assertNotIn("daily budget rejected", record["summary"])
        self.assertNotIn("must-not-be-recorded", json.dumps(context))
        self.assertNotIn("private caption", json.dumps(context))
        _, event = parse_journal_record(json.dumps({
            "__REALTIME_TIMESTAMP": "1000000", "_SYSTEMD_UNIT": "kern-tools.service", "MESSAGE": json.dumps(record),
        }))
        self.assertEqual(event["kind"], "provider_failure")
        self.assertEqual(event["service"], "kern-tools")

    def test_long_unicode_provider_errors_are_bounded_without_split_characters(self):
        body = json.dumps({"error": {"code": 400, "message": "\U0001f642" * 1000}}, ensure_ascii=False).encode()
        exc = WebRequestError("Provider rejected request.", status=400, body=body)
        with patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
            report_ads_failure("google_ads", "report", exc, phase="read")
        context = emit.call_args.args[0]["context"]
        self.assertTrue(context["provider_response_truncated"])
        chunks = [value for key, value in context.items() if key == "provider_response" or key.startswith("provider_response_") and isinstance(value, str)]
        self.assertGreater(len(chunks), 0)
        self.assertLessEqual(len(json.dumps(context).encode()), 4096)
        self.assertTrue(all(len(value.encode()) <= 512 and "\ufffd" not in value for value in chunks))

    def test_transport_and_verification_failures_need_no_provider_body(self):
        for exc in (WebRequestError("Provider unavailable."), RuntimeError("Created campaign settings did not match.")):
            with self.subTest(exc=type(exc).__name__), patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
                report_ads_failure("instagram_ads", "launch_campaign", exc, phase="verify paused campaign")
                emit.assert_called_once()
                context = emit.call_args.args[0]["context"]
                self.assertEqual(context["phase"], "verify paused campaign")
                self.assertNotIn("provider_response", context)

    def test_diagnostic_formatting_failure_preserves_original_failure(self):
        exc = AdsProviderError("Provider rejected request.", "POST campaigns", status=400, body=b'{}')
        with patch("host.tools.shared.ads_diagnostics.ads_error_body", side_effect=RuntimeError("format failed")), patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
            report_ads_failure("x_ads", "launch_campaign", exc, phase="create campaign")
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0]["summary"], "Provider rejected request.")
        self.assertTrue(emit.call_args.args[0]["context"]["diagnostic_incomplete"])

    def test_google_metadata_accepts_only_known_detail_types_and_code_formats(self):
        body = json.dumps({"error": {"status": "private status text", "details": [
            {"@type": "unknown", "requestId": "private-correlation", "errors": [{"errorCode": {"authorizationError": "NOT_ADS_USER"}}]},
            {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "private arbitrary reason"},
            {"@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure", "requestId": "contains whitespace secret", "errors": [{"errorCode": {"privateField": "SECRET", "authorizationError": "arbitrary private message"}}]},
        ]}}).encode()
        exc = AdsProviderError("Google Ads request failed.", "POST search", status=403, body=body)
        with patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
            report_ads_failure("google_ads", "list_campaigns", exc, phase="read")
        context = emit.call_args.args[0]["context"]
        self.assertFalse(any(key.startswith("google_") for key in context))

    def test_json_credential_echoes_are_removed_before_logging(self):
        messages = [
            "failed fetching https://graph.example/oauth?client_secret=private-secret&code=private-code",
            "failed fetching https://graph.example/oauth?access_token=private-secret&amp;code=private-code",
            "https%3A%2F%2Fgraph.example%2Foauth%3Fclient_secret%3Dprivate-secret%26code%3Dprivate-code",
            "https%253A%252F%252Fgraph.example%252Foauth%253Fclient_secret%253Dprivate-secret",
            'Echoed {"client_secret": "private-secret"}',
            "Authorization: Bearer private-secret",
            "https://user:private-secret@graph.example/oauth",
        ]
        for message in messages:
            with self.subTest(message=message):
                body = json.dumps({"error": {"message": message, "code": 400, "access_token": "private-secret", "errors": [{"message": "Useful ordinary detail"}]}}).encode()
                context = ads_failure_context("instagram_ads", "oauth_complete_connect", AdsProviderError("Meta OAuth failed.", "GET oauth/access_token", status=400, body=body), phase="connection")
                self.assertNotIn("private-secret", json.dumps(context))
                self.assertNotIn("private-code", json.dumps(context))
                self.assertIn("credential-bearing provider text omitted", context["provider_response"])
                self.assertIn("Useful ordinary detail", context["provider_response"])
                self.assertIn('"code": 400', context["provider_response"])

    def test_additional_diagnostic_context_does_not_suppress_response_body(self):
        from host.runtime.tools.tools_host import _provider_warning_context
        from host.runtime.tools.api import OperatorError, _report_operator_provider_warning
        exc = ProviderWarning("Example", "POST request", "Provider failed.", status=500, body=b'{"error":"useful provider detail"}')
        exc.diagnostic_context = {"phase": "read", "extra_detail": "additional context"}
        context = _provider_warning_context("example", "read", exc)
        self.assertEqual(context["provider_response"], exc.response_body)
        self.assertEqual(context["extra_detail"], "additional context")
        with patch("host.runtime.tools.api.host_errors.emit_record") as emit, self.assertRaises(OperatorError):
            _report_operator_provider_warning("example", "read", exc)
        emit.assert_called_once()
        self.assertEqual(emit.call_args.args[0]["context"]["provider_response"], exc.response_body)
        self.assertEqual(emit.call_args.args[0]["context"]["extra_detail"], "additional context")

    def test_transport_truncation_is_visible_without_logging_partial_json(self):
        cause = AdsProviderError("Meta request failed.", "GET /accounts", status=400, body=b'{"error": {"message": "unfinished', body_truncated=True)
        exc = RuntimeError("Reconnect required.")
        exc.__cause__ = cause
        context = ads_failure_context("instagram_ads", "list_accounts", exc, phase="read")
        self.assertEqual(context["http_status"], 400)
        self.assertEqual(context["operation"], "GET /accounts")
        self.assertTrue(context["provider_response_truncated"])
        self.assertEqual(context["provider_response_unavailable"], "transport body limit")
        self.assertNotIn("provider_response", context)

    def test_unstructured_bodies_cannot_echo_oauth_credentials_into_diagnostics(self):
        body = b'<html>https://graph.facebook.com/oauth/access_token?client_secret=private-secret&amp;code=private-code</html>'
        exc = AdsProviderError("Meta OAuth exchange failed.", "GET oauth/access_token", status=502, body=body)
        with patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
            report_ads_failure("instagram_ads", "oauth_complete_connect", exc, phase="connection")
        record = emit.call_args.args[0]
        self.assertEqual(record["context"]["http_status"], 502)
        self.assertNotIn("provider_response", record["context"])
        self.assertNotIn("private-secret", json.dumps(record))
        self.assertNotIn("private-code", json.dumps(record))

    def test_short_google_error_removes_nested_rejected_request_values(self):
        body = json.dumps({"error": {"code": 403, "message": "Rejected field", "details": [{
            "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure",
            "requestId": "correlation-123", "errors": [{"errorCode": {"authorizationError": "USER_PERMISSION_DENIED"}, "trigger": {"stringValue": "private ad copy"}, "location": {"fieldPathElements": [{"fieldName": "name"}]}}],
        }, {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT", "metadata": {"query": "private query"}}]}}).encode()
        with patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
            report_ads_failure("google_ads", "list_campaigns", AdsProviderError("Google Ads denied access.", "POST search", status=403, body=body), phase="read")
        context = emit.call_args.args[0]["context"]
        self.assertFalse(context["provider_response_truncated"])
        self.assertNotIn("private ad copy", str(context))
        self.assertNotIn("private query", str(context))
        self.assertIn("USER_PERMISSION_DENIED", str(context))
        self.assertEqual(context["google_request_id"], "correlation-123")

    def test_operator_oauth_boundary_emits_chunked_context_once_and_keeps_result_curated(self):
        from host.runtime.tools.api import OperatorError, _report_operator_provider_warning
        body = json.dumps({"error": "server_error", "error_description": "provider-only detail " * 300}).encode()
        warning = ProviderWarning("Google Ads", "POST OAuth token exchange", "OAuth exchange failed.", status=500, body=body)
        warning.diagnostic_context = ads_failure_context("google_ads", "oauth_complete_connect", warning, phase="connection")
        with patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
            with self.assertRaises(OperatorError) as caught:
                _report_operator_provider_warning("google_ads", "oauth_complete_connect", warning)
        emit.assert_called_once()
        context = emit.call_args.args[0]["context"]
        self.assertIn("provider_response_4", context)
        self.assertTrue(context["provider_response_truncated"])
        self.assertEqual(context["phase"], "connection")
        self.assertNotIn("provider-only detail", str(caught.exception))

    def test_structural_array_and_depth_omissions_are_explicitly_truncated(self):
        deep: object = {"message": "deep error"}
        for _ in range(20):
            deep = {"error": deep}
        for value in ({"errors": [{"code": "X"}] * 40}, {"error": deep}):
            with self.subTest(value=value):
                body = json.dumps(value).encode()
                with patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
                    report_ads_failure("x_ads", "list_accounts", AdsProviderError("X Ads failed.", "GET accounts", status=400, body=body), phase="read")
                context = emit.call_args.args[0]["context"]
                self.assertTrue(context["provider_response_truncated"])
                retained = "".join(text for key, text in context.items() if key == "provider_response" or key.startswith("provider_response_") and isinstance(text, str))
                self.assertTrue(json.loads(retained)["diagnostic_truncated"])
                self.assertNotIn("provider_response_4", context)
                # Google can sanitize before wrapping its host-boundary warning.
                with patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
                    report_ads_failure("google_ads", "list_accounts", AdsProviderError("Google Ads failed.", "GET accounts", status=400, body=ads_error_body(body)), phase="read")
                self.assertTrue(emit.call_args.args[0]["context"]["provider_response_truncated"])

    def test_large_confirmed_resource_lists_have_an_explicit_omission_flag(self):
        resources = ["create campaign " + "a" * 32, "create ad group " + "b" * 32] + ["create targeting " + str(i).zfill(32) for i in range(20)]
        for body in (b"", b'{"errors":[{"code":"INVALID_PARAMETER"}]}'):
            with self.subTest(body=body), patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
                report_ads_failure("x_ads", "launch_campaign", AdsProviderError("X Ads rejected the request.", "POST promoted_tweets", status=400, body=body), phase="associate post", confirmed=resources)
            context = emit.call_args.args[0]["context"]
            self.assertTrue(context["confirmed_resources_truncated"])
            self.assertLessEqual(len(context["confirmed_resources"].encode()), 512)
            self.assertIn("create campaign", context["confirmed_resources"])

    def test_google_metadata_scans_late_retained_errors_and_marks_its_own_limits(self):
        errors = [{"message": "long earlier message " * 100} for _ in range(24)] + [{
            "errorCode": {"authorizationError": "USER_PERMISSION_DENIED"}, "location": {"fieldPathElements": [{"fieldName": "mutate_operations", "index": 24}, {"fieldName": "create"}]},
        }]
        body = json.dumps({"error": {"details": [{"@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure", "errors": errors}]}}).encode()
        with patch("host.tools.shared.ads_diagnostics.host_errors.emit_record") as emit:
            report_ads_failure("google_ads", "launch_campaign", AdsProviderError("Google Ads rejected the request.", "POST mutate", status=400, body=body), phase="creating the paused campaign")
        context = emit.call_args.args[0]["context"]
        self.assertTrue(context["provider_response_truncated"])
        self.assertEqual(context["google_error_codes"], "authorizationError:USER_PERMISSION_DENIED")
        self.assertEqual(context["google_error_paths"], "mutate_operations[24].create")
        self.assertFalse(context["google_error_codes_truncated"])
