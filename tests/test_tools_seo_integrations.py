"""Provider-mocked tests for PageSpeed Insights and IndexNow."""

from __future__ import annotations

import json
import hashlib
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import unittest
import urllib.parse
from unittest.mock import patch

from host.tools import indexnow
from host.tools import pagespeed_insights as pagespeed
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.web import WebRequestError
from test_tools import FakeHostAPI, assert_matches_output_schema


def configured_api() -> FakeHostAPI:
    return FakeHostAPI(
        config={
            "PAGESPEED_INSIGHTS_API_KEY": "pagespeed-secret",
        }
    )


class PageSpeedInsightsTests(unittest.TestCase):
    def response(self):
        return {
            "id": "https://example.com/final",
            "analysisUTCTimestamp": "2026-09-22T10:00:00Z",
            "lighthouseResult": {
                "requestedUrl": "https://example.com/",
                "finalUrl": "https://example.com/final",
                "lighthouseVersion": "13.0.0",
                "categories": {
                    "performance": {"score": 0.72},
                    "accessibility": {"score": 0.98},
                    "best-practices": {"score": 0.91},
                    "seo": {"score": 1.0},
                },
                "audits": {
                    "first-contentful-paint": {
                        "title": "First Contentful Paint",
                        "displayValue": "1.2 s",
                        "numericValue": 1200.5,
                        "numericUnit": "millisecond",
                        "score": 0.8,
                    },
                    "render-blocking-resources": {
                        "title": "Eliminate render-blocking resources",
                        "description": "Resources are blocking first paint.",
                        "displayValue": "Potential savings of 400 ms",
                        "score": 0.25,
                        "scoreDisplayMode": "numeric",
                    },
                    "manual-audit": {"title": "Manual", "score": 0, "scoreDisplayMode": "manual"},
                },
            },
        }

    def test_manifest_and_normalized_result(self):
        self.assertEqual(pagespeed.MANIFEST.connection, "enable_only")
        self.assertEqual([a.id for a in pagespeed.MANIFEST.actions], ["analyze_page"])
        self.assertEqual(len(pagespeed.MANIFEST.data_summary.cards), 4)
        with patch.object(pagespeed, "json_request", return_value=self.response()) as request:
            result = pagespeed.BUNDLED_TOOL.execute(
                "analyze_page",
                {"url": "https://example.com/", "strategy": "mobile"},
                configured_api(),
            )
        self.assertIsInstance(result, ActionExecuted)
        assert_matches_output_schema(self, pagespeed.MANIFEST, "analyze_page", result)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["scores"]["performance"], 0.72)
        self.assertEqual(result.result["metrics"][0]["id"], "first_contentful_paint")
        self.assertEqual(result.result["priority_audits"][0]["id"], "render-blocking-resources")
        url = request.call_args.args[1]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(query["url"], ["https://example.com/"])
        self.assertEqual(query["strategy"], ["mobile"])
        self.assertEqual(query["category"], list(pagespeed.CATEGORIES))
        self.assertEqual(query["key"], ["pagespeed-secret"])
        self.assertNotIn("pagespeed-secret", json.dumps(result.result))

    def test_invalid_private_or_secret_bearing_url_fails_before_network(self):
        invalid = (
            "http://localhost/page",
            "https://127.0.0.1/page",
            "https://[::ffff:127.0.0.1]/page",
            "https://user:pass@example.com/",
            "ftp://example.com/",
            "https://example.com/#fragment",
            "https://example.com/?token=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        )
        with patch.object(pagespeed, "json_request") as request:
            for url in invalid:
                with self.subTest(url=url):
                    result = pagespeed.BUNDLED_TOOL.execute("analyze_page", {"url": url}, configured_api())
                    self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()

    def test_defaults_enums_bounds_and_nested_url_guard(self):
        api = configured_api()
        long_url = "https://example.com/" + ("hosting%20" * 202) + "articles"
        with patch.object(pagespeed, "json_request", return_value=self.response()) as request:
            result = pagespeed.BUNDLED_TOOL.execute("analyze_page", {"url": long_url, "strategy": "desktop", "categories": ["seo"]}, api)
            self.assertIsInstance(result, ActionExecuted)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[1]).query)
            self.assertEqual(query["url"], [long_url])
            self.assertEqual(query["category"], ["seo"])
            self.assertEqual(query["strategy"], ["desktop"])
        for tool_input in (
            {"url": long_url + "a"}, {"url": "https://example.com/" + "é" * 1024},
            {"url": "https://example.com/?email=alice%2540example.com"},
            {"url": "https://example.com/", "categories": []},
            {"url": "https://example.com/", "categories": ["seo", "seo"]},
            {"url": "https://example.com/", "strategy": "tablet"},
            {"url": "https://example.com/", "unknown": True},
        ):
            with self.subTest(tool_input=tool_input), patch.object(pagespeed, "json_request") as request:
                self.assertIsInstance(pagespeed.BUNDLED_TOOL.execute("analyze_page", tool_input, api), ActionFailed)
                request.assert_not_called()

    def test_lighthouse_runtime_error_is_a_curated_failure(self):
        response = self.response()
        response["lighthouseResult"]["runtimeError"] = {"code": "ERRORED_DOCUMENT_REQUEST", "message": "private provider detail"}
        with patch.object(pagespeed, "json_request", return_value=response):
            result = pagespeed.BUNDLED_TOOL.execute("analyze_page", {"url": "https://example.com/"}, configured_api())
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("could not complete", result.error)
        self.assertNotIn("private", result.error)

    def test_provider_failures_are_curated(self):
        with patch.object(pagespeed, "json_request", side_effect=WebRequestError("raw", status=403, body=b"secret body")):
            result = pagespeed.BUNDLED_TOOL.execute("analyze_page", {"url": "https://example.com"}, configured_api())
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("API key", result.error)
        self.assertNotIn("secret body", result.error)


class IndexNowTests(unittest.TestCase):
    def test_verification_file_is_generated_once_and_survives_api_recreation(self):
        api = configured_api()
        api.secrets.save({"fingerprint_salt": "a" * 64, "other": "preserved"})
        with patch.object(indexnow, "request_bytes") as request, patch.object(api.approvals, "request") as queue:
            result = indexnow.BUNDLED_TOOL.execute("get_verification_file", {}, api)
            self.assertIsInstance(result, ActionExecuted)
            assert_matches_output_schema(self, indexnow.MANIFEST, "get_verification_file", result)
            key = result.result["key"]
            self.assertEqual(result.result, {"key": key, "filename": f"{key}.txt", "content": key, "path": f"/{key}.txt"})
            recreated = replace(api, config={})
            self.assertEqual(indexnow.BUNDLED_TOOL.execute("get_verification_file", {}, recreated), result)
            self.assertEqual(api.secrets.load()["other"], "preserved")
            self.assertEqual(api.secrets.load()["fingerprint_salt"], "a" * 64)
            queue.assert_not_called()
            request.assert_not_called()
        self.assertFalse(indexnow.MANIFEST.config)
        self.assertFalse(api.costs.calls)

    def test_verification_file_rejects_inputs_and_corrupt_key_without_replacing_it(self):
        api = configured_api()
        result = indexnow.BUNDLED_TOOL.execute("get_verification_file", {"key": "caller-key"}, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIsNone(api.secrets.load())
        api.secrets.save({"key": "invalid"})
        result = indexnow.BUNDLED_TOOL.execute("get_verification_file", {}, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(api.secrets.load(), {"key": "invalid"})

    def test_approval_omits_key_and_posts_exact_batch(self):
        api = configured_api()
        urls = ["https://example.com/new", "https://example.com/changed?version=2"]
        with patch.object(indexnow, "request_bytes") as request:
            pending = indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": urls}, api)
            self.assertIsInstance(pending, ActionPendingApproval)
            assert isinstance(pending, ActionPendingApproval)
            approval = api.approvals.approve(pending.approval_id)
            self.assertNotIn(api.secrets.load()["key"], json.dumps(approval.payload))
            self.assertNotEqual(approval.payload["key_fingerprint"], hashlib.sha256(api.secrets.load()["key"].encode()).hexdigest())
            verification = indexnow.BUNDLED_TOOL.execute("get_verification_file", {}, api)
            self.assertEqual(verification.result["key"], api.secrets.load()["key"])
            self.assertNotIn(api.secrets.load()["fingerprint_salt"], json.dumps(approval.payload))
            self.assertEqual(approval.payload["urls"], urls)
            request.assert_not_called()
            self.assertEqual(approval.payload["host"], "example.com")
            executed = indexnow.BUNDLED_TOOL.execute_approved(approval, api)
        self.assertIsInstance(executed, ApprovalExecuted)
        request.assert_called_once()
        self.assertEqual(request.call_args.args, ("POST", indexnow.ENDPOINT))
        self.assertEqual(json.loads(request.call_args.kwargs["data"]), {
            "host": "example.com", "key": api.secrets.load()["key"], "urlList": urls,
        })

    def test_invalid_or_mixed_hosts_and_key_change_fail_before_post(self):
        api = configured_api()
        invalid = (
            ["https://127.0.0.1/page"],
            ["https://example.com/a", "https://other.com/b"],
            ["https://example.com/a", "https://example.com/a"],
            ["https://example.com/a#fragment"],
            ["https://example.com/a/../b"],
            ["http://example.com/a"],
        )
        with patch.object(indexnow, "request_bytes") as request:
            for urls in invalid:
                with self.subTest(urls=urls):
                    self.assertIsInstance(indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": urls}, api), ActionFailed)
            pending = indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": ["https://example.com/new"]}, api)
            assert isinstance(pending, ActionPendingApproval)
            approval = api.approvals.approve(pending.approval_id)
            api.secrets.save({**api.secrets.load(), "key": "fedcba9876543210fedcba9876543210"})
            self.assertIsInstance(indexnow.BUNDLED_TOOL.execute_approved(approval, api), ActionFailed)
        request.assert_not_called()

    def test_every_url_path_and_nested_query_are_guarded_before_queue(self):
        denied = [
            "https://example.com/people/alice@example.com",
            "https://example.com/new?email=alice%40example.com",
            "https://example.com/new?email=alice%2540example.com",
            "https://example.com/new?next=https%253A%252F%252Fexample.com%252F%253Ftoken%253Dabcdefghijklmnop",
            "https://example.com/new?password=hunter2",
            "https://example.com/ghp_" + "A" * 36,
        ]
        with patch.object(indexnow, "request_bytes") as request:
            for value in denied:
                api = configured_api()
                with self.subTest(value=value), patch.object(api.approvals, "request", wraps=api.approvals.request) as queue:
                    result = indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": ["https://example.com/safe", value]}, api)
                    self.assertIsInstance(result, ActionFailed)
                    self.assertNotIn(value, result.error)
                    queue.assert_not_called()
            request.assert_not_called()

    def test_guard_preserves_exact_values_and_existing_byte_and_batch_bounds(self):
        api = configured_api()
        prefix = "https://example.com/"
        urls = [prefix + ("hosting%20" * 202) + "articles", "https://example.com/%6eew?version=2&name=public+page"]
        pending = indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": urls}, api)
        self.assertIsInstance(pending, ActionPendingApproval)
        approval = api.approvals.approve(pending.approval_id)
        with patch.object(indexnow, "request_bytes") as request:
            self.assertIsInstance(indexnow.BUNDLED_TOOL.execute_approved(approval, api), ApprovalExecuted)
            self.assertEqual(json.loads(request.call_args.kwargs["data"])["urlList"], urls)
        hundred = [f"https://example.com/page/{i}" for i in range(100)]
        self.assertIsInstance(indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": hundred}, api), ActionPendingApproval)
        for tool_input in (
            {"urls": []}, {"urls": hundred + ["https://example.com/extra"]},
            {"urls": [urls[0] + "a"]}, {"urls": [prefix + "é" * 1024]},
            {"urls": [["https://example.com/new"]]}, {"urls": [{"url": "https://example.com/new"}]},
            {"urls": ["https://example.com/new"], "key": "caller-key"},
            {"urls": ["https://user:pass@example.com/new"]}, {"urls": ["https://example.com/%2e%2e/new"]},
            {"urls": ["https://example.com/%252e%252e/private"]},
            {"urls": ["https://example.com/%255cprivate"]},
        ):
            with self.subTest(tool_input=tool_input):
                self.assertIsInstance(indexnow.BUNDLED_TOOL.execute("submit_urls", tool_input, api), ActionFailed)

    def test_execution_rechecks_guard_host_and_private_key_binding(self):
        api = configured_api()
        pending = indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": ["https://example.com/new"]}, api)
        approval = api.approvals.approve(pending.approval_id)
        with patch.object(indexnow, "request_bytes") as request:
            for edits in (
                {"urls": ["https://example.com/new?email=alice%2540example.com"]},
                {"urls": ["https://example.com/%252e%252e/private"]},
                {"urls": ["https://example.com/%255cprivate"]},
                {"host": "other.com"}, {"key_fingerprint": "0" * 64},
            ):
                result = indexnow.BUNDLED_TOOL.execute_approved(replace(approval, payload={**approval.payload, **edits}), api)
                self.assertIsInstance(result, ActionFailed)
            api.secrets.clear()
            self.assertIsInstance(indexnow.BUNDLED_TOOL.execute_approved(approval, api), ActionFailed)
            request.assert_not_called()

    def test_concurrent_initial_batches_share_one_persisted_private_binding(self):
        api = configured_api()
        load = api.secrets.load
        start = threading.Barrier(4)
        def slow_initial_load():
            value = load()
            if value is None:
                # Force the old load-then-save race across concurrent handlers.
                time.sleep(0.01)
            return value
        def fingerprint(_):
            start.wait(timeout=5)
            key = indexnow._key(api, create=True)
            return key, indexnow._key_fingerprint(key, api, create=True)
        with patch.object(api.secrets, "load", side_effect=slow_initial_load), patch.object(api.secrets, "save", wraps=api.secrets.save) as save:
            with ThreadPoolExecutor(max_workers=4) as pool:
                bindings = list(pool.map(fingerprint, range(4)))
        self.assertEqual(save.call_count, 2)  # One key, one private binding salt.
        self.assertEqual(len(set(bindings)), 1)
        self.assertEqual(bindings[0], (api.secrets.load()["key"], indexnow._key_fingerprint(api.secrets.load()["key"], api)))

    def test_complete_approval_json_size_checked_before_queue_and_execution(self):
        api = configured_api()
        urls = []
        for i in range(32):
            prefix = f"https://example.com/{i}/"
            path = "hosting%20" * ((2044 - len(prefix) - 8) // 10) + "articles"
            urls.append(prefix + path + "/" * (2044 - len(prefix + path)))
        encoded = json.dumps({"urls": urls}, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertLessEqual(len(encoded), 64 * 1024)
        with patch.object(api.approvals, "request", wraps=api.approvals.request) as queue:
            result = indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": urls}, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("65,536", result.error)
        queue.assert_not_called()
        # The same aggregate validation is required at approved execution.
        pending = indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": urls[:31]}, api)
        self.assertIsInstance(pending, ActionPendingApproval)
        approval = api.approvals.approve(pending.approval_id)
        self.assertLessEqual(len(json.dumps(approval.payload, separators=(",", ":"), ensure_ascii=False).encode()), indexnow.MAX_APPROVAL_BYTES)
        payload = {**approval.payload, "urls": urls}
        self.assertGreater(len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()), indexnow.MAX_APPROVAL_BYTES)
        with patch.object(indexnow, "request_bytes") as request:
            self.assertIsInstance(indexnow.BUNDLED_TOOL.execute_approved(replace(approval, payload=payload), api), ActionFailed)
            request.assert_not_called()

    def test_key_file_rejection_has_curated_error(self):
        api = configured_api()
        pending = indexnow.BUNDLED_TOOL.execute("submit_urls", {"urls": ["https://example.com/new"]}, api)
        assert isinstance(pending, ActionPendingApproval)
        approval = api.approvals.approve(pending.approval_id)
        with patch.object(indexnow, "request_bytes", side_effect=WebRequestError("raw secret", status=403, body=b"secret")):
            result = indexnow.BUNDLED_TOOL.execute_approved(approval, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("key file", result.error)
        self.assertNotIn("secret", result.error)



if __name__ == "__main__":
    unittest.main()
