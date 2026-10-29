"""TrustMRR protocol/packaging regression coverage; no live key or calls."""

from datetime import datetime
from decimal import Decimal
from pathlib import Path
import io
import json
import unittest
import urllib.error
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from host.runtime.admin_api import tools_client
from host.runtime.tools import tools_host
from host.tools import trustmrr
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared import web
from host.tools.trustmrr.manifest import ENUMS, LIST_INPUT, MAX_INTEGER
from test_tools import FakeHostAPI, assert_matches_output_schema

KEY = "tmrr_fixture_private_key"


class Response(io.BytesIO):
    headers = {"X-RateLimit-Limit": "10", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1793289600"}


class TrustMRRTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI(config={"TRUSTMRR_API_KEY": KEY})
        self.open = self.enterContext(patch.object(web._OPENER, "open"))
        self.reply()

    def reply(self, value=None, *, page=1, limit=10, headers=None):
        if value is None:
            value = {"data": [{"slug": "fixture-startup", "revenue": {"mrr": None, "last30Days": 0},
                               "visitorsLast30Days": 18, "visitorSource": "google-search-console",
                               "newField": {"a": [None, False, 123.5]}}],
                     "meta": {"page": page, "limit": limit, "total": 1, "hasMore": False}}
        self.open.side_effect = None
        self.open.return_value = Response(json.dumps(value).encode())
        if headers is not None:
            self.open.return_value.headers = headers
        return value

    def execute(self, value=None, *, action="list_startups", api=None):
        return trustmrr.BUNDLED_TOOL.execute(action, {} if value is None else value, api or self.api)

    def query(self):
        return parse_qs(urlsplit(self.open.call_args.args[0].full_url).query)

    def test_default_list_request_and_lossless_single_envelope(self):
        original = self.reply()
        result = self.execute()
        assert_matches_output_schema(self, trustmrr.MANIFEST, "list_startups", result)
        self.assertEqual(json.loads(result.result["provider_response_json"]), original)
        self.assertEqual(set(result.result), {"provider_response_json", "retrieved_at", "rate_limit"})
        self.assertEqual(result.result["rate_limit"], {"limit": 10, "remaining": 0, "reset": 1793289600})
        self.assertEqual(datetime.fromisoformat(result.result["retrieved_at"].replace("Z", "+00:00")).utcoffset().total_seconds(), 0)
        self.open.assert_called_once()
        request = self.open.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(urlsplit(request.full_url).netloc, "trustmrr.com")
        self.assertEqual(urlsplit(request.full_url).path, "/api/v1/startups")
        self.assertEqual(self.query(), {"page": ["1"], "limit": ["10"], "sort": ["revenue-desc"]})
        self.assertIsNone(request.data)
        self.assertEqual(dict(request.header_items()), {"Accept": "application/json", "Authorization": "Bearer " + KEY})
        self.assertEqual(self.open.call_args.kwargs["timeout"], 30)
        self.assertNotIn(KEY, request.full_url)
        self.assertEqual(self.api.approvals.records, {})
        self.assertEqual(self.api.costs.calls, [])

    def test_all_filters_pagination_and_plus_encoding(self):
        self.reply(page=100001, limit=1)
        values = {"page": 100001, "limit": 1, "onSale": "true", "category": "ai", "teamSize": "51+",
                  "fundingStatus": "vc-funded", "xHandle": "fixture_founder", "minRevenue": 0, "maxRevenue": 30000.5,
                  "minMrr": 100, "maxMrr": 200, "minGrowth": -0.5, "maxGrowth": 0.1,
                  "minPrice": 50000, "maxPrice": 80000}
        result = self.execute(values)
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(self.query(), {**{k: [str(v)] for k, v in values.items()}, "sort": ["best-deal"]})
        self.assertIn("teamSize=51%2B", self.open.call_args.args[0].full_url)
        self.open.assert_called_once()  # no automatic pagination

    def test_enums_and_conditional_sort(self):
        for field, options in ENUMS.items():
            for option in options:
                with self.subTest(field=field, option=option):
                    self.reply()
                    self.assertIsInstance(self.execute({field: option}), ActionExecuted)
                    self.assertEqual(self.query()[field], [option])
        self.reply()
        self.execute({"onSale": "false"})
        self.assertEqual(self.query()["sort"], ["revenue-desc"])
        self.reply()
        self.execute({"onSale": "true", "sort": "price-asc"})
        self.assertEqual(self.query()["sort"], ["price-asc"])

    def test_validation_rejects_unknown_null_wrong_types_and_ranges_before_http(self):
        values = [{"unknown": 1}, {"url": "https://example.com"}]
        values += [{field: None} for field in LIST_INPUT]
        values += [{field: bad} for field in ("page", "limit") for bad in (True, "1", 1.0, 0, -1)]
        values += [{"limit": 11}, {"page": MAX_INTEGER + 1}, {"onSale": True}]
        values += [{field: "unknown"} for field in ENUMS]
        values += [{"xHandle": bad} for bad in ("@founder", "a&sort=x", "a/b", "a b", "a" * 201, "\ud800")]
        values += [{field: bad} for field in trustmrr.NUMERIC_FILTERS for bad in (True, "1", float("nan"), float("inf"), 10 ** 400)]
        values += [{field: -1} for field in trustmrr.NUMERIC_FILTERS if not field.endswith("Growth")]
        values += [{"min" + metric: 2, "max" + metric: 1} for metric in ("Revenue", "Mrr", "Growth", "Price")]
        for value in values:
            with self.subTest(value=value):
                self.assertIsInstance(self.execute(value), ActionFailed)
        self.open.assert_not_called()

    def test_get_exact_slug_and_guard_placement(self):
        original = {"data": {"slug": "Fixture_Startup-2", "description": "Full text", "cofounders": [], "newField": None}}
        self.reply(original)
        with patch.object(self.api.outbound, "guard_request_parameter_string", wraps=self.api.outbound.guard_request_parameter_string) as guard:
            result = self.execute({"slug": "Fixture_Startup-2"}, action="get_startup")
        assert_matches_output_schema(self, trustmrr.MANIFEST, "get_startup", result)
        guard.assert_called_once_with("Fixture_Startup-2")
        self.assertEqual(json.loads(result.result["provider_response_json"]), original)
        self.assertEqual(self.open.call_args.args[0].full_url, trustmrr.ENDPOINT + "/Fixture_Startup-2")
        self.assertEqual(self.api.approvals.records, {})

    def test_slug_path_injection_and_unknown_fields_fail_before_http(self):
        for slug in (None, 1, True, "", ".", "..", "a/b", "a\\b", "%2f", "a%252fadmin", "a?x=y", "a#b",
                     "https://example.com", "a\n", " a", "a ", "a" * 201, "\ud800", "é", "-a"):
            with self.subTest(slug=slug):
                self.assertIsInstance(self.execute({"slug": slug}, action="get_startup"), ActionFailed)
        for value in ({}, {"slug": "ok", "page": 1}):
            self.assertIsInstance(self.execute(value, action="get_startup"), ActionFailed)
        self.open.assert_not_called()

    def test_strict_guard_denies_secret_and_numeric_identifier_before_http(self):
        for field, action in (("slug", "get_startup"), ("xHandle", "list_startups")):
            for value in ("ghp_" + "a" * 36, "1234567890123456"):
                with self.subTest(field=field, value=value):
                    result = self.execute({field: value}, action=action)
                    self.assertIsInstance(result, ActionFailed)
                    self.assertTrue("credential" in result.error or "digits" in result.error, result.error)
                    self.assertNotIn(value, result.error)
        self.open.assert_not_called()

    def test_response_redacts_key_and_bearer_without_dropping_unknowns(self):
        self.reply({"data": [], "meta": {"page": 1, "limit": 10, "total": 0, "hasMore": False},
                    "echo": KEY, KEY: "field name", "new": {"nullable": None, "future": [1, 2], "access_token": "other-secret"},
                    "message": "Bearer private0123456789-token"})
        result = self.execute()
        self.assertIsInstance(result, ActionExecuted)
        payload = result.result["provider_response_json"]
        for secret in (KEY, "private0123456789-token", "other-secret"):
            self.assertNotIn(secret, payload)
        self.assertEqual(json.loads(payload)["new"], {"nullable": None, "future": [1, 2], "access_token": "[redacted]"})

    def test_numeric_precision_and_ordinary_bearer_prose_are_preserved(self):
        raw = (b'{"data":{"slug":"ok","decimal":0.1234567890123456789,"large":123456789012345678901234567890,'
               b'"nested":[1.2300e-100,1e999,-0.0,"0.1234567890123456789"],'
               b'"description":"Supports Bearer authentication for API calls. The bearer of good news."}}')
        self.open.return_value = Response(raw)
        result = self.execute({"slug": "ok"}, action="get_startup")
        self.assertIsInstance(result, ActionExecuted)
        payload = result.result["provider_response_json"]
        self.assertEqual(json.loads(payload, parse_float=Decimal), json.loads(raw, parse_float=Decimal))
        self.assertIn("1.2300e-100", payload)
        self.assertIn("-0.0", payload)
        self.assertIn("Supports Bearer authentication", payload)
        self.assertIn("The bearer of good news", payload)

    def test_monetary_fields_and_filter_bounds_are_never_rescaled(self):
        # Representative captured precision/magnitude; fields need independent
        # provenance, not a single assumed cents-to-dollars conversion.
        row = {"slug": "fixture-startup", "revenue": {"last30Days": 52296.84, "mrr": 40063.25, "total": 319701.48},
               "askingPrice": 800000, "previousAskingPrice": None, "revenuePerVisitor": 2.7174,
               "futureMoney": 1234.56789}
        bounds = {"minRevenue": 50000, "maxRevenue": 60000, "minMrr": 39000, "maxMrr": 41000,
                  "minPrice": 790000, "maxPrice": 810000}
        self.reply({"data": [row], "meta": {"page": 1, "limit": 10, "total": 1, "hasMore": False}})
        result = self.execute(bounds)
        assert_matches_output_schema(self, trustmrr.MANIFEST, "list_startups", result)
        self.assertEqual(json.loads(result.result["provider_response_json"])["data"], [row])
        for key, value in bounds.items():
            self.assertEqual(self.query()[key], [str(value)])
        self.reply({"data": row})
        result = self.execute({"slug": "fixture-startup"}, action="get_startup")
        assert_matches_output_schema(self, trustmrr.MANIFEST, "get_startup", result)
        self.assertEqual(json.loads(result.result["provider_response_json"])["data"], row)

    def test_second_and_millisecond_reset_headers_are_preserved_raw(self):
        for value in (1793285520, 1793285520000):
            self.reply(headers={"X-RateLimit-Reset": str(value)})
            self.assertEqual(self.execute().result["rate_limit"]["reset"], value)

    def test_duplicate_provider_keys_fail_instead_of_silently_losing_values(self):
        self.open.return_value = Response(b'{"data":{"slug":"ok","future":1,"future":2}}')
        self.assertIsInstance(self.execute({"slug": "ok"}, action="get_startup"), ActionFailed)

    def test_absent_and_invalid_rate_headers_are_unknown(self):
        self.reply(headers={"X-RateLimit-Limit": "-1", "X-RateLimit-Remaining": "bogus", "X-RateLimit-Reset": KEY})
        self.assertEqual(self.execute().result["rate_limit"], {"limit": None, "remaining": None, "reset": None})

    def test_bad_shapes_and_mismatched_identity_fail(self):
        for value in ({}, {"data": None}, {"data": [], "meta": {}},
                      {"data": [], "meta": {"page": True, "limit": 10, "total": 0, "hasMore": False}},
                      {"data": [], "meta": {"page": 2, "limit": 10, "total": 0, "hasMore": False}},
                      {"data": [None], "meta": {"page": 1, "limit": 10, "total": 1, "hasMore": False}}):
            self.reply(value)
            self.assertIsInstance(self.execute(), ActionFailed)
        self.reply({"data": {"slug": "different"}})
        self.assertIsInstance(self.execute({"slug": "fixture-startup"}, action="get_startup"), ActionFailed)

    def test_malformed_nonfinite_oversized_and_deep_responses_fail(self):
        for raw in (b"not JSON", b"null", b'{"data":NaN}', b'\xff', b" " * (trustmrr.MAX_BYTES + 1)):
            self.open.return_value = Response(raw)
            self.assertIsInstance(self.execute(), ActionFailed)
        deep = None
        for _ in range(35):
            deep = {"child": deep}
        self.reply({"data": {"slug": "ok", "deep": deep}})
        self.assertIsInstance(self.execute({"slug": "ok"}, action="get_startup"), ActionFailed)
        self.reply({"data": {"slug": "ok", "text": "é" * (trustmrr.MAX_BYTES // 4)}})
        self.assertIsInstance(self.execute({"slug": "ok"}, action="get_startup"), ActionFailed)

    def test_provider_failures_redact_and_never_retry(self):
        for code in (400, 401, 403, 404, 429, 500, 503, 302):
            with self.subTest(code=code):
                self.open.reset_mock()
                self.open.side_effect = urllib.error.HTTPError(trustmrr.ENDPOINT, code, KEY, {}, io.BytesIO(KEY.encode()))
                try:
                    result = self.execute()
                except web.UnmappedProviderError as exc:
                    self.assertIn(code, (503, 302))
                    self.assertEqual(exc.response_body, "")
                    self.assertNotIn(KEY, str(exc))
                else:
                    self.assertIsInstance(result, ActionFailed)
                    self.assertNotIn(KEY, result.error)
                self.open.assert_called_once()
        self.open.side_effect = urllib.error.URLError(KEY)
        with self.assertRaises(web.UnmappedProviderError) as raised:
            self.execute()
        self.assertNotIn(KEY, str(raised.exception))

    def test_no_redirect_following_and_missing_config(self):
        handler = next(h for h in web._OPENER.handlers if isinstance(h, web._NoRedirectHandler))
        self.assertIsNone(handler.redirect_request(None, None, 302, "redirect", {}, "https://evil.example"))
        for config in ({}, {"TRUSTMRR_API_KEY": " "}):
            for action, values in (("list_startups", {}), ("get_startup", {"slug": "shipfast"})):
                result = self.execute(values, action=action, api=FakeHostAPI(config=config))
                self.assertIn("Home > Integrations", result.error)
                self.assertIn("not set", result.error)  # fresh-host smoke contract
        self.assertIsInstance(self.execute(action="anything_else"), ActionFailed)
        self.open.assert_not_called()

    def test_catalog_setup_schemas_and_direct_action_contract(self):
        self.assertIs(tools_host.BUNDLED_TOOLS["trustmrr"], trustmrr.BUNDLED_TOOL)
        entry = tools_client._tool_entry(trustmrr.BUNDLED_TOOL, {"trustmrr"}, set())
        self.assertEqual(entry["display_name"], "TrustMRR")
        self.assertEqual(entry["connection"], "enable_only")
        self.assertEqual(trustmrr.MANIFEST.config[0].key, "TRUSTMRR_API_KEY")
        self.assertFalse(trustmrr.MANIFEST.reports_cost)
        self.assertIsNone(trustmrr.BUNDLED_TOOL.credentials)
        self.assertEqual({a.id for a in trustmrr.MANIFEST.actions}, {"list_startups", "get_startup"})
        for action in trustmrr.MANIFEST.actions:
            self.assertEqual(action.approval, "direct")
            self.assertFalse(action.returns_asset)
            self.assertEqual(tools_host.unsupported_schema_error(action.input_schema), "")
            self.assertEqual(tools_host.unsupported_schema_error(action.output_schema), "")
        self.assertTrue(any(s.show_config for s in trustmrr.MANIFEST.setup_steps))

    def test_stage_probe_is_bounded_to_two_reads(self):
        from tests.stage.stage_tool_checks import StageToolChecks
        probe = StageToolChecks()
        with patch.object(probe, "_successful_tool_call", return_value={}) as call:
            self.assertEqual(probe._check_tool_provider("trustmrr"), "2 live read(s) completed")
        self.assertEqual([c.args for c in call.call_args_list], [
            ("trustmrr_list_startups", {"limit": 1}), ("trustmrr_get_startup", {"slug": "shipfast"}),
        ])
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/kern-stage.yml").read_text()
        self.assertIn("KERN_STAGE_TRUSTMRR_API_KEY: ${{ secrets.KERN_STAGE_TRUSTMRR_API_KEY }}", workflow)
