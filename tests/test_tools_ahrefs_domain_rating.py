"""Ahrefs protocol, validation and catalog contract; no live API key required."""

from datetime import datetime, timezone
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from host.runtime.admin_api import tools_client
from host.runtime.tools import tools_host
from host.tools import ahrefs_domain_rating as ahrefs
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared import web
from host.tools.shared.inputs import ToolInputValidationError
from host.tools.shared.web import UnmappedProviderError
from test_tools import FakeHostAPI, assert_matches_output_schema


KEY = "private-ahrefs-test-key"


class Response(io.BytesIO):
    headers = {"content-type": "application/json"}


class AhrefsDomainRatingTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI(config={"AHREFS_API_KEY": KEY})
        # Every request below is intercepted at the shared HTTP boundary.
        self.open = self.enterContext(patch.object(web._OPENER, "open"))
        self.reply({"domain_rating": {"domain_rating": 42.5, "license": ahrefs.LICENSE_URL}})

    def reply(self, value):
        self.open.side_effect = None
        self.open.return_value = Response(json.dumps(value).encode())

    def execute(self, tool_input, *, action="get_domain_rating", api=None):
        return ahrefs.BUNDLED_TOOL.execute(action, tool_input, api or self.api)

    def test_success_default_version_and_exact_request(self):
        before = datetime.now(timezone.utc)
        result = self.execute({"domain": "WWW.Example.COM"})
        after = datetime.now(timezone.utc)
        assert_matches_output_schema(self, ahrefs.MANIFEST, "get_domain_rating", result)
        self.assertIsInstance(result, ActionExecuted)
        value = result.result
        self.assertEqual(value["domain"], "www.example.com")
        self.assertEqual(value["version"], "dr3")
        self.assertEqual(value["domain_rating"], 42.5)
        self.assertEqual(value["attribution"], {"text": "Domain Rating by Ahrefs", "url": "https://ahrefs.com/"})
        self.assertEqual(value["license_url"], ahrefs.LICENSE_URL)
        retrieved = datetime.fromisoformat(value["retrieved_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, retrieved)
        self.assertLessEqual(retrieved, after)
        self.assertEqual(retrieved.utcoffset().total_seconds(), 0)
        self.assertNotIn(KEY, json.dumps(value))
        self.open.assert_called_once()
        request = self.open.call_args.args[0]
        parsed = urllib.parse.urlsplit(request.full_url)
        self.assertEqual(urllib.parse.urlunsplit(parsed._replace(query="")), ahrefs.ENDPOINT)
        self.assertEqual(urllib.parse.parse_qs(parsed.query), {"target": ["www.example.com"], "version": ["dr3"], "output": ["json"]})
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertEqual(dict(request.header_items()), {"Accept": "application/json", "Authorization": "Bearer " + KEY})
        self.assertEqual(self.open.call_args.kwargs["timeout"], web.DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(self.api.approvals.records, {})
        self.assertEqual(self.api.costs.calls, [])

    def test_versions_and_real_boundary_scores(self):
        for version in ("dr3", "dr4"):
            for score in (0, 0.0, 100, 100.0):
                with self.subTest(version=version, score=score):
                    self.reply({"domain_rating": {"domain_rating": score, "license": ahrefs.LICENSE_URL}})
                    result = self.execute({"domain": "example.com", "version": version})
                    assert_matches_output_schema(self, ahrefs.MANIFEST, "get_domain_rating", result)
                    self.assertEqual(result.result["domain_rating"], score)
                    self.assertEqual(result.result["version"], version)
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.open.call_args.args[0].full_url).query)
                    self.assertEqual(query["version"], [version])

    def test_idns_and_case_normalize_to_same_domain(self):
        for value in ("BÜCHER.de", "bücher.de", "bu\u0308cher.de", "XN--BCHER-KVA.de"):
            with self.subTest(domain=value):
                self.reply({"domain_rating": {"domain_rating": 5}})
                result = self.execute({"domain": value})
                self.assertIsInstance(result, ActionExecuted)
                self.assertEqual(result.result["domain"], "xn--bcher-kva.de")

    def test_rejects_non_public_or_non_hostname_inputs_without_request(self):
        invalid = (
            None, 123, [], "", " example.com", "example.com ", "exam ple.com",
            "https://example.com", "example.com/path", "example.com?x=y", "example.com#x",
            "user@example.com", "user:password@example.com", "example.com:443",
            "127.0.0.1", "[::1]", "::1", "2130706433", "0x7f.0x0.0x0.0x1",
            "localhost", "foo.localhost", "foo.local", "foo.internal", "foo.lan", "foo.home",
            "foo.invalid", "foo.test", "foo.localdomain", "foo.corp", "foo.private", "foo.intranet",
            "foo.onion", "router.home.arpa", "foo.example", "foo.alt", "example.123", "example.c",
            "example.com.", ".example.com", "example..com", "-example.com", "example-.com",
            "under_score.com", "example%2ecom", "example\\com", "example\n.com",
            "a" * 64 + ".com", "example.\ud800", "xn--.com", "xn--invalid-.com",
            "faß.de", "exam\u200dple.com", "example\u3002com", "\uff45xample.com",
        )
        for domain in invalid:
            with self.subTest(domain=domain):
                result = self.execute({"domain": domain})
                self.assertIsInstance(result, ActionFailed)
                self.assertIn("bare public DNS hostname", result.error)
        self.open.assert_not_called()

    def test_normalized_hostname_length_bounds(self):
        # Syntax cap independently of the stricter random-token guard.
        at_limit = ".".join(["a" * 63] * 3 + ["a" * 57, "com"])
        self.assertEqual(len(at_limit), 253)
        self.assertEqual(ahrefs._normalized_domain(at_limit)[0], at_limit)
        with self.assertRaises(ToolInputValidationError):
            ahrefs._normalized_domain(at_limit.replace("a" * 57, "a" * 58))
        with self.assertRaises(ToolInputValidationError):
            ahrefs._normalized_domain("ü" * 60 + ".com")

    def test_guard_checks_original_decoded_and_wire_forms_without_exceptions(self):
        with patch.object(self.api.outbound, "guard_request_parameter_string", wraps=self.api.outbound.guard_request_parameter_string) as guard:
            result = self.execute({"domain": "BÜCHER.de"})
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual([call.args[0] for call in guard.call_args_list], ["BÜCHER.de", "bücher.de", "xn--bcher-kva.de"])
        self.assertTrue(all(not call.kwargs for call in guard.call_args_list))

    def test_guard_denies_secrets_and_encoded_data_before_outbound(self):
        for domain in ("AKIAIOSFODNN7EXAMPLE.com", "x7Kp2mQv9zR4tYw8LbN3.com", "482913482913.com"):
            with self.subTest(domain=domain):
                result = self.execute({"domain": domain})
                self.assertIsInstance(result, ActionFailed)
                self.assertIn("retry", result.error)
                self.assertNotIn(domain, result.error)
        self.open.assert_not_called()

    def test_rejects_unknown_fields_versions_and_actions(self):
        for value in (
            {}, {"domain": "example.com", "url": ahrefs.ENDPOINT},
            {"domain": "example.com", "headers": {}}, {"domain": "example.com", "domains": []},
            *({"domain": "example.com", "version": version} for version in (None, "", "DR3", "dr5", [], 3)),
        ):
            with self.subTest(value=value):
                self.assertIsInstance(self.execute(value), ActionFailed)
        self.assertIsInstance(self.execute({"domain": "example.com"}, action="batch"), ActionFailed)
        self.open.assert_not_called()

    def test_missing_or_blank_key_is_actionable(self):
        for api in (FakeHostAPI(), FakeHostAPI(config={}), FakeHostAPI(config={"AHREFS_API_KEY": " "})):
            result = self.execute({"domain": "example.com"}, api=api)
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("AHREFS_API_KEY", result.error)
            self.assertIn("not set", result.error)
            self.assertIn("Home > Integrations", result.error)
        self.open.assert_not_called()

    def test_missing_malformed_nonfinite_and_out_of_range_scores_never_become_zero(self):
        for value in (
            {}, {"domain_rating": None}, {"domain_rating": 1}, {"domain_rating": []},
            {"domain_rating": {}},
            *({"domain_rating": {"domain_rating": score}} for score in (None, True, False, "42.5", [], {}, -1, 101, float("nan"), float("inf"), -float("inf"), 10 ** 400)),
        ):
            with self.subTest(value=value):
                self.reply(value)
                result = self.execute({"domain": "example.com"})
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(result.error, ahrefs.INVALID_RESPONSE)

    def test_malformed_json_and_body_shapes_are_safe_errors(self):
        for body in (b"", b"not json", b"\xff", b"null", b"[]", b"1", b'{"domain_rating":{"domain_rating":1e999}}'):
            with self.subTest(body=body):
                self.open.return_value = Response(body)
                result = self.execute({"domain": "example.com"})
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(result.error, ahrefs.INVALID_RESPONSE)

    def test_untrusted_provider_links_and_extra_fields_are_not_returned(self):
        self.reply({"domain_rating": {"domain_rating": 12, "license": "https://attacker.example/" + KEY, "secret": KEY}, "error": KEY})
        result = self.execute({"domain": "example.com"})
        assert_matches_output_schema(self, ahrefs.MANIFEST, "get_domain_rating", result)
        self.assertEqual(result.result["license_url"], ahrefs.LICENSE_URL)
        self.assertNotIn(KEY, json.dumps(result.result))
        self.assertNotIn("attacker", json.dumps(result.result))

    def test_auth_rate_limit_and_other_http_errors_never_leak_bodies_or_retry(self):
        for status, expected in ((401, "Replace"), (403, "Replace"), (429, "Wait"), (400, "HTTP 400"), (500, "HTTP 500"), (302, "HTTP 302")):
            with self.subTest(status=status):
                self.open.reset_mock()
                self.open.side_effect = urllib.error.HTTPError(ahrefs.ENDPOINT, status, "secret " + KEY, {"Location": "https://attacker.example/"}, Response(KEY.encode()))
                result = self.execute({"domain": "example.com"})
                self.assertIsInstance(result, ActionFailed)
                self.assertIn(expected, result.error)
                self.assertNotIn(KEY, result.error)
                self.open.assert_called_once()

    def test_uses_shared_redirect_free_opener(self):
        handler = next(item for item in web._OPENER.handlers if isinstance(item, web._NoRedirectHandler))
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://attacker.example/"))

    def test_oversized_response_is_bounded_and_actionable(self):
        self.open.return_value = Response(b"x" * (web.MAX_RESPONSE_BYTES + 1))
        result = self.execute({"domain": "example.com"})
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(result.error, web.RESPONSE_TOO_LARGE_MESSAGE)
        self.open.assert_called_once()

    def test_transport_and_unexpected_failures_keep_secrets_out_of_results(self):
        self.open.side_effect = urllib.error.URLError(KEY)
        with self.assertRaises(UnmappedProviderError) as raised:
            self.execute({"domain": "example.com"})
        self.assertNotIn(KEY, str(raised.exception))
        self.assertEqual(raised.exception.response_body, "")
        with patch.object(ahrefs, "json_request", side_effect=RuntimeError(KEY)):
            result = self.execute({"domain": "example.com"})
        self.assertEqual(result.error, "Ahrefs Domain Rating request failed.")

    def test_manifest_auto_registration_catalog_and_config_contract(self):
        self.assertIs(tools_host.BUNDLED_TOOLS["ahrefs_domain_rating"], ahrefs.BUNDLED_TOOL)
        entry = tools_client._tool_entry(ahrefs.BUNDLED_TOOL, {"ahrefs_domain_rating"}, set())
        self.assertEqual(entry["display_name"], "Ahrefs Domain Rating")
        self.assertEqual(entry["connection"], "enable_only")
        self.assertEqual(ahrefs.MANIFEST.config[0].key, "AHREFS_API_KEY")
        self.assertFalse(ahrefs.MANIFEST.reports_cost)
        self.assertIsNone(ahrefs.BUNDLED_TOOL.credentials)
        action, = ahrefs.MANIFEST.actions
        self.assertEqual(action.id, "get_domain_rating")
        self.assertEqual(action.approval, "direct")
        self.assertIsNone(action.limit_runs_per_day)
        self.assertFalse(action.input_schema["additionalProperties"])
        self.assertFalse(action.output_schema["additionalProperties"])
        self.assertEqual(tools_host.unsupported_schema_error(action.input_schema), "")
        self.assertEqual(tools_host.unsupported_schema_error(action.output_schema), "")
        self.assertIn("adjacent", ahrefs.MANIFEST.agent_notes)
        self.assertIn("dr3 and dr4", ahrefs.MANIFEST.agent_notes)

    def test_stage_probe_uses_one_free_domain_lookup(self):
        from tests.stage.stage_tool_checks import StageToolChecks

        probe = StageToolChecks()
        with patch.object(probe, "_successful_tool_call", return_value={"domain_rating": 50}) as call:
            detail = probe._check_tool_provider("ahrefs_domain_rating")
        call.assert_called_once_with("ahrefs_domain_rating_get_domain_rating", {"domain": "ahrefs.com"})
        self.assertEqual(detail, "1 live read(s) completed")
