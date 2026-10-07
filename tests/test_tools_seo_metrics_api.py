"""Contract tests at the HTTP boundary; fixtures are synthetic, not live data."""

from copy import deepcopy
import io
import json
import unittest
import urllib.error
import urllib.parse
from unittest.mock import patch

from host.runtime.admin_api import tools_client
from host.runtime.tools import tools_host
from host.tools import seo_metrics_api as seo
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared import web
from host.tools.shared.web import UnmappedProviderError
from test_tools import FakeHostAPI, assert_matches_output_schema

KEY = "sk_live_pub_private_test_key_12345"
# The public docs deliberately do not specify data's complete keyword shape.
# This invented fixture proves preservation, not a claimed provider schema.
ENVELOPE = {
    "data": {"items": [{"phrase": "seo api", "volume": None, "difficulty": 12, "cpc": 0.4,
                       "currency": "USD", "sources": {"volume": {"provider": "example", "status": "no_data", "fetched_at": "2026-10-01T00:00:00Z"}}}],
             "future_field": {"values": [False, 0, None]}},
    "meta": {"resource": "keywords", "cost_units": 2, "cache": "miss", "age_seconds": 0, "degraded": False},
    "error": None,
}


class Response(io.BytesIO):
    headers = {"content-type": "application/json"}


class SEOMetricsAPITests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI(config={"SEO_METRICS_API_KEY": KEY})
        self.open = self.enterContext(patch.object(web._OPENER, "open"))
        self.reply(ENVELOPE)

    def reply(self, body):
        self.open.side_effect = None
        self.open.return_value = Response(json.dumps(body).encode())

    def execute(self, value=None, action="get_keyword_metrics", api=None):
        return seo.BUNDLED_TOOL.execute(action, {"keywords": ["seo api"]} if value is None else value, api or self.api)

    def test_request_encoding_response_fidelity_and_cost(self):
        phrases = ['invoice & billing', 'café "software"']
        result = self.execute({"keywords": phrases, "country": "gb"})
        assert_matches_output_schema(self, seo.MANIFEST, "get_keyword_metrics", result)
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(json.loads(result.result["provider_response_json"]), ENVELOPE)
        self.assertEqual(result.result["keywords"], phrases)
        self.assertEqual(result.result["country"], "GB")
        self.assertEqual(result.result["cost_units"], 2)
        self.assertEqual(self.api.costs.calls, [("0.002500000", "")])
        req = self.open.call_args.args[0]
        url = urllib.parse.urlsplit(req.full_url)
        self.assertEqual((url.scheme, url.netloc, url.path), ("https", "seometricsapi.com", "/v1/keywords"))
        self.assertEqual(urllib.parse.parse_qs(url.query), {"q": [','.join(phrases)], "country": ["GB"]})
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data)
        self.assertEqual(req.get_header("Authorization"), "Bearer " + KEY)
        self.assertEqual(self.open.call_args.kwargs['timeout'], 30)
        self.open.assert_called_once()
        self.assertNotIn(KEY, json.dumps(result.result))

    def test_usage_has_no_query_and_retains_account_payload(self):
        body = {"data": {"period": "October", "credits": 7, "limits": {"rpm": 30}}, "meta": {}, "error": None}
        self.reply(body)
        result = self.execute({}, "get_usage")
        assert_matches_output_schema(self, seo.MANIFEST, "get_usage", result)
        self.assertEqual(json.loads(result.result['provider_response_json']), body)
        self.assertEqual(self.open.call_args.args[0].full_url, seo.BASE_URL + '/v1/usage')
        self.assertIsNone(result.result['cost_units'])
        self.assertIsNone(result.result['degraded'])
        self.assertEqual(self.api.costs.calls, [])

    def test_explicit_zero_stale_cache_and_nulls_are_preserved(self):
        body = deepcopy(ENVELOPE)
        body['meta'].update(cost_units=0, cache='hit', age_seconds=500000, degraded=True)
        self.reply(body)
        result = self.execute()
        self.assertTrue(result.result['degraded'])
        self.assertEqual(result.result['age_seconds'], 500000)
        self.assertIsNone(json.loads(result.result['provider_response_json'])['data']['items'][0]['volume'])
        self.assertEqual(self.api.costs.calls, [('0.000000000', '')])

    def test_missing_cost_is_not_zero(self):
        body = deepcopy(ENVELOPE)
        del body['meta']['cost_units']
        self.reply(body)
        result = self.execute()
        self.assertIsNone(result.result['cost_units'])
        self.assertEqual(self.api.costs.calls, [])

    def test_keyword_limits_and_country_are_validated_before_io(self):
        bad = [['keyword phrase ' * 19 + 'x'] * 5, [], ['x'] * 6, [''], ['  '], [' x'], ['x '], ['x,y'], ['x\ny'], ['x\x00y'], [1], ['é' * 151], 'keyword']
        for keywords in bad:
            with self.subTest(keywords=keywords):
                result = self.execute({'keywords': keywords})
                self.assertIsInstance(result, ActionFailed)
        for country in ['USA', 'U1', '', 'us ', 'üS', 1, None]:
            self.assertIsInstance(self.execute({'keywords': ['seo'], 'country': country}), ActionFailed)
        self.assertIsInstance(self.execute({'keywords': ['seo'], 'url': 'https://evil.test'}), ActionFailed)
        self.assertIsInstance(self.execute({'country': 'US'}, 'get_usage'), ActionFailed)
        self.assertIsInstance(self.execute({}, 'indexing'), ActionFailed)
        self.open.assert_not_called()

    def test_five_keywords_and_utf8_boundary(self):
        result = self.execute({'keywords': ['a', 'b', 'c', 'd', 'é ' * 98 + 'ééé']})
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(result.result['country'], 'US')

    def test_secret_shaped_keyword_guard_before_http(self):
        for phrase in ['sk-proj-' + 'a' * 60, 'someone@example.com', '1234567890123456']:
            with self.subTest(phrase=phrase):
                self.assertIsInstance(self.execute({'keywords': ['invoices', phrase]}), ActionFailed)
        self.open.assert_not_called()

    def test_each_keyword_and_final_wire_parameter_are_guarded(self):
        with patch.object(type(self.api.outbound), 'guard_request_parameter_string', side_effect=lambda value: value) as guard:
            self.execute({'keywords': ['one', 'two']})
        self.assertEqual([c.args for c in guard.call_args_list], [('one',), ('two',), ('one,two',)])
        self.assertTrue(all(not c.kwargs for c in guard.call_args_list))

    def test_request_echo_cannot_expose_a_nonstandard_configured_key(self):
        result = self.execute(api=FakeHostAPI(config={"SEO_METRICS_API_KEY": "seo api"}))
        self.assertIsInstance(result, ActionExecuted)
        self.assertNotIn("seo api", json.dumps(result.result))

    def test_missing_key_does_not_call_provider(self):
        for config in [{}, {'SEO_METRICS_API_KEY': ''}, {'SEO_METRICS_API_KEY': ' '}]:
            result = self.execute(api=FakeHostAPI(config=config))
            self.assertIn('SEO_METRICS_API_KEY is not set', result.error)
        self.open.assert_not_called()

    def test_nested_credentials_and_keys_are_redacted(self):
        body = deepcopy(ENVELOPE)
        body['data']['future_field'] = {KEY: [KEY, 'Bearer topsecret', {'api_key': 'opaque', 'refresh_token': 'another'}]}
        body['meta']['cache'] = KEY
        self.reply(body)
        result = self.execute()
        self.assertIsInstance(result, ActionExecuted)
        payload = json.dumps(result.result)
        for secret in [KEY, 'topsecret', 'opaque', 'another']:
            self.assertNotIn(secret, payload)
        self.assertIn('[redacted]', payload)
        self.assertEqual(result.result['cache'], '[redacted]')

    def test_redacted_key_collision_fails_instead_of_dropping_fields(self):
        body = deepcopy(ENVELOPE)
        body['data'] = {KEY: 1, '[redacted]': 2}
        self.reply(body)
        self.assertIsInstance(self.execute(), ActionFailed)

    def test_bad_envelopes_and_meta_never_become_zero_metrics(self):
        bodies = [{}, {'data': {}, 'meta': {}}, {'data': None, 'meta': {}, 'error': None}]
        for field, value in [('cost_units', True), ('cost_units', -1), ('cost_units', '2'), ('degraded', 'false'), ('age_seconds', -1), ('cache', [])]:
            body = deepcopy(ENVELOPE)
            body['meta'][field] = value
            bodies.append(body)
        for body in bodies:
            with self.subTest(body=body):
                self.reply(body)
                self.assertIsInstance(self.execute(), ActionFailed)

    def test_empty_provider_collection_is_preserved(self):
        self.reply({'data': [], 'meta': {}, 'error': None})
        result = self.execute()
        self.assertEqual(json.loads(result.result['provider_response_json'])['data'], [])

    def test_billed_invalid_data_records_cost_before_failure(self):
        body = deepcopy(ENVELOPE)
        body['data'] = 'invalid'
        self.reply(body)
        self.assertIsInstance(self.execute(), ActionFailed)
        self.assertEqual(self.api.costs.calls, [('0.002500000', '')])

    def test_oversize_or_deep_result_fails_without_truncating(self):
        body = deepcopy(ENVELOPE)
        body['data'] = {'large': 'x' * seo.MAX_RESULT_BYTES}
        self.reply(body)
        self.assertEqual(self.execute().error, seo.INVALID_RESPONSE)
        body['data'] = {}
        item = body['data']
        for _ in range(35):
            item['nested'] = {}
            item = item['nested']
        self.reply(body)
        self.assertEqual(self.execute().error, seo.INVALID_RESPONSE)

    def test_invalid_json_nonfinite_or_http_oversize(self):
        for raw in [b'not json', b'{"data":NaN}', b'{"data":1e9999}', b'x' * (web.MAX_RESPONSE_BYTES + 1)]:
            self.open.return_value = Response(raw)
            self.assertIsInstance(self.execute(), ActionFailed)
        self.assertEqual(self.api.costs.calls, [])

    def test_http_errors_are_curated_and_not_retried(self):
        for status, code, expected in [(429, 'quota_exceeded', 'credits are exhausted'), (429, 'rate_limited', 'rate limit'),
                                       (401, 'unauthorized', 'rejected the key'), (403, 'unauthorized', 'rejected the key'),
                                       (400, 'invalid_input', 'parameters'), (503, 'provider_unavailable', 'unavailable'), (302, '', 'failed')]:
            with self.subTest(status=status, code=code):
                self.open.reset_mock()
                self.open.side_effect = urllib.error.HTTPError(seo.BASE_URL, status, KEY, {}, io.BytesIO(json.dumps({'error': {'code': code, 'message': KEY}}).encode()))
                result = self.execute()
                self.assertIn(expected, result.error)
                self.assertNotIn(KEY, result.error)
                self.open.assert_called_once()

    def test_application_error_uses_fixed_message(self):
        self.reply({'data': None, 'meta': {'cost_units': 0}, 'error': {'code': 'quota_exceeded', 'message': KEY}})
        result = self.execute()
        self.assertIn('credits are exhausted', result.error)
        self.assertNotIn(KEY, result.error)

    def test_transport_failures_keep_secrets_out(self):
        self.open.side_effect = urllib.error.URLError(KEY)
        with self.assertRaises(UnmappedProviderError) as raised:
            self.execute()
        self.assertNotIn(KEY, str(raised.exception))
        with patch.object(seo, 'json_request', side_effect=RuntimeError(KEY)):
            self.assertEqual(self.execute().error, seo.INVALID_RESPONSE)

    def test_registry_config_schema_and_brand_contract(self):
        self.assertIs(tools_host.BUNDLED_TOOLS['seo_metrics_api'], seo.BUNDLED_TOOL)
        entry = tools_client._tool_entry(seo.BUNDLED_TOOL, set(), set())
        self.assertEqual(entry['display_name'], 'SEO Metrics API')
        self.assertEqual(seo.MANIFEST.config[0].key, 'SEO_METRICS_API_KEY')
        self.assertTrue(seo.MANIFEST.reports_cost)
        for action in seo.MANIFEST.actions:
            self.assertEqual(action.approval, 'direct')
            self.assertEqual(tools_host.unsupported_schema_error(action.input_schema), '')
            self.assertEqual(tools_host.unsupported_schema_error(action.output_schema), '')

    def test_stage_probe_evaluates_one_phrase_and_reads_usage(self):
        from tests.stage.stage_tool_checks import StageToolChecks
        probe = StageToolChecks()
        with patch.object(probe, '_successful_tool_call', return_value={}) as call:
            self.assertEqual(probe._check_tool_provider('seo_metrics_api'), '2 live read(s) completed')
        self.assertEqual(call.call_args_list[0].args, ('seo_metrics_api_get_keyword_metrics', {'keywords': ['seo api'], 'country': 'US'}))
        self.assertEqual(call.call_args_list[1].args, ('seo_metrics_api_get_usage', {}))
