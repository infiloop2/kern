"""Bounded Google indexing summaries; no live provider requests."""

import unittest
from unittest.mock import patch

from host.tools import google_search_console as sc
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared import google
from host.tools.shared.web import WebRequestError, UnmappedProviderError
from test_tools import assert_matches_output_schema
from test_tools_google_search_console import SITE, DOMAIN_SITE, PREFIX_SITE, PROPERTY_RESPONSE, connected_api


def evidence(verdict):
    return {"inspectionResult": {"indexStatusResult": {
        "verdict": verdict, "coverageState": "Provider coverage evidence",
        "googleCanonical": "https://example.com/canonical", "lastCrawlTime": "2026-10-07T00:00:00Z",
        "robotsTxtState": "ALLOWED", "indexingState": "INDEXING_ALLOWED", "pageFetchState": "SUCCESSFUL",
    }}}


class SearchConsoleBatchTests(unittest.TestCase):
    urls = [f"https://example.com/page/{i}?version=2" for i in range(5)]

    def execute(self, replies, urls=None, **extra):
        with patch.object(sc, "google_json_request", side_effect=[PROPERTY_RESPONSE, *replies]) as request:
            result = sc.BUNDLED_TOOL.execute("inspect_urls", {"site_url": SITE, "urls": urls or self.urls, **extra}, connected_api())
        return result, request

    def assert_result(self, result):
        self.assertIsInstance(result, ActionExecuted)
        assert_matches_output_schema(self, sc.MANIFEST, "inspect_urls", result)
        self.assertEqual(sum(result.result["counts"].values()), len(result.result["results"]))
        return result.result

    def test_success_groups_only_verdicts_and_keeps_exact_requested_list_and_evidence(self):
        result, request = self.execute([evidence(v) for v in ("PASS", "NEUTRAL", "FAIL", "VERDICT_UNSPECIFIED", "PARTIAL")])
        data = self.assert_result(result)
        self.assertEqual(data["counts"], {"indexed": 1, "not_indexed": 2, "unknown": 2, "error": 0, "unprocessed": 0})
        self.assertEqual([row["url"] for row in data["results"]], self.urls)
        self.assertEqual(data["results"][0]["inspection"]["index_status"]["coverage_state"], "Provider coverage evidence")
        self.assertEqual(data["results"][0]["inspection"]["index_status"]["google_canonical"], "https://example.com/canonical")
        self.assertEqual(request.call_count, 6)
        for call, url in zip(request.call_args_list[1:], self.urls):
            self.assertEqual(call.args, ("POST", sc.URL_INSPECTION_ENDPOINT, "google_search_console-access-token"))
            self.assertEqual(call.kwargs["body"], {"inspectionUrl": url, "siteUrl": SITE, "languageCode": "en-US"})

    def test_entire_list_validation_and_later_member_guard_precede_any_provider_or_authentication(self):
        invalid_inputs = [
            {"site_url": SITE, "urls": []}, {"site_url": SITE, "urls": self.urls + ["https://example.com/six"]},
            {"site_url": SITE, "urls": [self.urls[0], self.urls[0]]},
            {"site_url": SITE, "urls": [self.urls[0], [self.urls[1]]]},
            {"site_url": SITE, "urls": [self.urls[0], {"url": self.urls[1]}]},
            {"site_url": " " + SITE, "urls": self.urls},
            {"site_url": SITE, "urls": self.urls, "arbitrary": True},
            {"site_url": SITE, "urls": self.urls, "language_code": "../../en"},
            {"site_url": PREFIX_SITE, "urls": ["https://example.com/catalog/ok", "https://example.com/outside"]},
        ]
        invalid_urls = [
            "https://other.example/page", "https://example.com/alice@example.com",
            "https://example.com/?email=alice%2540example.com", "https://example.com/?token=abcdefghijklmnop",
            "https://example.com/../page", "https://example.com/page#fragment",
            "https://example.com/ " , "https://example.com/" + "x" * 2048,
        ]
        invalid_inputs.extend({"site_url": SITE, "urls": [self.urls[0], value]} for value in invalid_urls)
        with patch.object(sc.SEARCH_CONSOLE_CREDENTIALS, "access_token") as auth, patch.object(sc, "google_json_request") as request:
            for tool_input in invalid_inputs:
                with self.subTest(tool_input=tool_input):
                    self.assertIsInstance(sc.BUNDLED_TOOL.execute("inspect_urls", tool_input, connected_api()), ActionFailed)
            auth.assert_not_called()
            request.assert_not_called()

    def test_exact_accessible_property_is_rechecked_for_current_account(self):
        with patch.object(sc, "google_json_request", return_value={"siteEntry": [{"siteUrl": "https://other.example/", "permissionLevel": "siteOwner"}]}) as request:
            result = sc.BUNDLED_TOOL.execute("inspect_urls", {"site_url": SITE, "urls": self.urls}, connected_api())
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("not available to the connected account", result.error)
        request.assert_called_once()

    def test_domain_property_and_language_reuse_single_inspection_rules(self):
        urls = ["https://blog.example.com/%6eew?version=2"]
        with patch.object(sc, "google_json_request", side_effect=[PROPERTY_RESPONSE, evidence("PASS")]) as request:
            result = sc.BUNDLED_TOOL.execute("inspect_urls", {"site_url": DOMAIN_SITE, "urls": urls, "language_code": "de-CH"}, connected_api())
        self.assert_result(result)
        self.assertEqual(request.call_args.kwargs["body"], {"siteUrl": DOMAIN_SITE, "inspectionUrl": urls[0], "languageCode": "de-CH"})

    def test_missing_null_and_new_verdicts_are_unknown_not_not_indexed(self):
        result, _ = self.execute([
            {"inspectionResult": {}}, {"inspectionResult": {"indexStatusResult": None}},
            evidence(None), evidence("NEW_VERDICT"), evidence(""),
        ])
        data = self.assert_result(result)
        self.assertEqual(data["counts"]["unknown"], 5)
        self.assertEqual(data["counts"]["not_indexed"], 0)

    def test_quota_and_authorization_failures_stop_and_preserve_remaining_rows(self):
        for status, reason in ((429, "quota"), (403, "authorization"), (401, "authorization"), (500, "provider_error")):
            with self.subTest(status=status), patch.object(sc, "_properties", return_value=[{"site_url": SITE, "permission_level": "siteOwner"}]), patch.object(google, "json_request", side_effect=[evidence("PASS"), WebRequestError("private provider body", status=status, body=b"private secret")]) as request:
                result = sc.BUNDLED_TOOL.execute("inspect_urls", {"site_url": SITE, "urls": self.urls}, connected_api())
            data = self.assert_result(result)
            self.assertEqual(data["counts"], {"indexed": 1, "not_indexed": 0, "unknown": 0, "error": 1, "unprocessed": 3})
            self.assertEqual(data["results"][1]["reason"], reason)
            self.assertTrue(all(row["reason"] == reason and row["inspection"] is None for row in data["results"][2:]))
            self.assertEqual(request.call_count, 2)
            self.assertNotIn("private", str(data))

    def test_invalid_response_is_error_and_remaining_urls_are_unprocessed(self):
        result, request = self.execute([{"inspectionResult": None}])
        data = self.assert_result(result)
        self.assertEqual(data["results"][0]["status"], "error")
        self.assertEqual(data["results"][0]["reason"], "invalid_response")
        self.assertEqual(data["counts"]["unprocessed"], 4)
        self.assertEqual(request.call_count, 2)

    def test_time_budget_prevents_another_call_without_losing_urls(self):
        with patch.object(sc.time, "monotonic", side_effect=[0, 0, 151]):
            result, request = self.execute([evidence("PASS")])
        data = self.assert_result(result)
        self.assertEqual(data["counts"]["indexed"], 1)
        self.assertEqual(data["counts"]["unprocessed"], 4)
        self.assertTrue(all(row["reason"] == "time_budget" for row in data["results"][1:]))
        self.assertEqual(request.call_count, 2)

    def test_unmapped_transport_failure_reaches_host_diagnostics_and_stops_calls(self):
        failure = UnmappedProviderError("Google", "API")
        with patch.object(sc, "google_json_request", side_effect=[PROPERTY_RESPONSE, evidence("PASS"), failure]) as request:
            with self.assertRaises(UnmappedProviderError) as caught:
                sc.BUNDLED_TOOL.execute("inspect_urls", {"site_url": SITE, "urls": self.urls}, connected_api())
        self.assertIs(caught.exception, failure)
        self.assertEqual(request.call_count, 3)
