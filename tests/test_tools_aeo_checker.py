"""AEO Checker protocol fixtures; no live provider acceptance is implied."""
from datetime import datetime, timezone
from decimal import Decimal
import io
import json
import unittest
import urllib.error
from unittest.mock import Mock, patch

from host.runtime.admin_api import tools_client
from host.runtime.tools import tools_host
from host.tools import aeo_checker as aeo
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared import web
from test_tools import FakeHostAPI, assert_matches_output_schema

OPENAI_KEY = "private-openai-fixture-key"
ANTHROPIC_KEY = "private-anthropic-fixture-key"
QUESTION = "  Which bookkeeping tools work well for a small business?  "
URL = "https://example.org/bookkeeping"


def openai_response():
    return {"id": "resp_fixture", "model": aeo.MODELS["openai"], "status": "completed", "output": [
        {"type": "web_search_call", "id": "search_1", "status": "completed", "action": {
            "type": "search", "query": "small business bookkeeping tools", "sources": [{"type": "url", "url": URL, "title": "Bookkeeping tools"}]}},
        {"type": "message", "content": [{"type": "output_text", "text": "Some options are listed here. https://invented.example/", "annotations": [
            {"type": "url_citation", "url": URL, "title": "Bookkeeping tools", "start_index": 0, "end_index": 27}]}]},
    ], "usage": {"input_tokens": 1000, "output_tokens": 100, "input_tokens_details": {"cached_tokens": 100}}}


def anthropic_response():
    return {"id": "msg_fixture", "model": aeo.MODELS["anthropic"], "stop_reason": "end_turn", "content": [
        {"type": "text", "text": "I'll search."},
        {"type": "server_tool_use", "id": "srvtoolu_fixture", "name": "web_search", "input": {"query": "small business bookkeeping tools"}},
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_fixture", "content": [
            {"type": "web_search_result", "url": URL, "title": "Bookkeeping tools", "encrypted_content": "PRIVATE_OPAQUE_CONTENT"}]},
        {"type": "text", "text": "Here are some options.", "citations": [{"type": "web_search_result_location", "url": URL,
            "title": "Bookkeeping tools", "cited_text": "Tools for small businesses", "encrypted_index": "PRIVATE_OPAQUE_INDEX"}]},
    ], "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                 "server_tool_use": {"web_search_requests": 1}}}


class Response(io.BytesIO):
    headers = {"content-type": "application/json"}


class AEOCheckerTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI(config={"OPENAI_API_KEY": OPENAI_KEY, "ANTHROPIC_API_KEY": ANTHROPIC_KEY})
        self.open = self.enterContext(patch.object(web._OPENER, "open"))
        self.reply(openai_response())

    def reply(self, value):
        self.open.side_effect = None
        self.open.return_value = Response(json.dumps(value).encode())

    def execute(self, provider="openai", query=QUESTION, api=None, **extra):
        return aeo.BUNDLED_TOOL.execute("check_query", {"provider": provider, "query": query, **extra}, api or self.api)

    def successful(self, provider="openai", api=None):
        result = self.execute(provider=provider, api=api)
        self.assertIsInstance(result, ActionExecuted)
        assert_matches_output_schema(self, aeo.MANIFEST, "check_query", result)
        return result.result

    def test_openai_exact_isolated_neutral_request_and_evidence(self):
        before = datetime.now(timezone.utc)
        value = self.successful()
        self.assertEqual(value["query"], QUESTION)
        self.assertEqual(value["model"], aeo.MODELS["openai"])
        self.assertEqual(value["observation_status"], "complete")
        self.assertTrue(value["search_used"])
        self.assertEqual(value["search_status"], "completed")
        self.assertEqual(value["citations"][0], {"block_index": 0, "url": URL, "title": "Bookkeeping tools", "start_index": 0, "end_index": 27, "cited_text": None})
        self.assertEqual(value["answer_blocks"][0]["item_index"], 1)
        self.assertEqual(value["search_calls"][0]["sources"], [{"url": URL, "title": "Bookkeeping tools"}])
        self.assertNotIn("invented.example", json.dumps(value["citations"]))
        retrieved = datetime.fromisoformat(value["retrieved_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, retrieved)
        self.assertLessEqual(retrieved, datetime.now(timezone.utc))
        self.open.assert_called_once()
        request = self.open.call_args.args[0]
        self.assertEqual(request.full_url, aeo.ENDPOINTS["openai"])
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), {
            "model": aeo.MODELS["openai"], "input": QUESTION, "instructions": aeo.INSTRUCTION, "store": False,
            "tools": [{"type": "web_search"}], "tool_choice": "required", "max_tool_calls": 3,
            "max_output_tokens": 4096, "reasoning": {"effort": "low"}, "include": ["web_search_call.action.sources"],
        })
        self.assertEqual(request.get_header("Authorization"), "Bearer " + OPENAI_KEY)
        self.assertEqual(self.open.call_args.kwargs["timeout"], 120)
        self.assertEqual(self.api.approvals.records, {})
        self.assertNotIn(OPENAI_KEY, json.dumps(value))
        self.assertNotIn(ANTHROPIC_KEY, json.dumps(value))
        self.assertEqual(Decimal(value["cost"]["estimated_usd"]), Decimal("0.0111325"))
        self.assertEqual(self.api.costs.calls, [(value["cost"]["estimated_usd"], "")])

    def test_openai_plural_queries_and_page_actions_preserve_evidence_without_search_fee(self):
        response = openai_response()
        response["output"][0]["action"].pop("query")
        response["output"][0]["action"]["queries"] = ["query one", "query two"]
        response["output"].insert(1, {"type": "web_search_call", "id": "open_1", "status": "completed",
            "action": {"type": "open_page", "url": URL}})
        response["output"].insert(2, {"type": "web_search_call", "id": "find_1", "status": "completed",
            "action": {"type": "find_in_page", "url": URL, "pattern": "bookkeeping"}})
        self.reply(response)
        value = self.successful()
        self.assertEqual(value["search_calls"][0]["queries"], ["query one", "query two"])
        self.assertEqual(value["search_calls"][1]["url"], URL)
        self.assertEqual(value["search_calls"][2]["pattern"], "bookkeeping")
        self.assertEqual(value["usage"]["search_requests"], 1)
        self.assertEqual(Decimal(value["cost"]["estimated_usd"]), Decimal("0.0111325"))

    def test_anthropic_exact_request_and_citation_mapping(self):
        self.reply(anthropic_response())
        value = self.successful("anthropic")
        request = self.open.call_args.args[0]
        self.assertEqual(request.full_url, aeo.ENDPOINTS["anthropic"])
        self.assertEqual(request.get_header("X-api-key"), ANTHROPIC_KEY)
        self.assertEqual(json.loads(request.data), {"model": aeo.MODELS["anthropic"], "system": aeo.INSTRUCTION,
            "messages": [{"role": "user", "content": QUESTION}], "max_tokens": 4096, "thinking": {"type": "disabled"},
            "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]})
        self.assertEqual(value["answer_text"], "I'll search.\nHere are some options.")
        self.assertEqual(value["citations"][0]["block_index"], 1)
        self.assertEqual(value["answer_blocks"][1]["item_index"], 3)
        self.assertIsNone(value["citations"][0]["start_index"])
        self.assertEqual(value["citations"][0]["cited_text"], "Tools for small businesses")
        self.assertNotIn("PRIVATE_OPAQUE", json.dumps(value))
        self.assertEqual(value["observation_status"], "complete")
        self.assertEqual(Decimal(value["cost"]["estimated_usd"]), Decimal("0.01015"))

    def test_keys_are_independent_and_only_selected_missing_key_fails(self):
        for provider, fixture in (("openai", openai_response), ("anthropic", anthropic_response)):
            with self.subTest(provider=provider):
                api = FakeHostAPI(config={aeo.KEYS[provider]: "only-selected-key"})
                self.reply(fixture())
                self.successful(provider, api)
                other = "anthropic" if provider == "openai" else "openai"
                self.open.reset_mock()
                missing = self.execute(provider=other, api=api)
                self.assertIsInstance(missing, ActionFailed)
                self.assertIn(aeo.KEYS[other], missing.error)
                self.open.assert_not_called()
        for config in ({}, {"OPENAI_API_KEY": " "}):
            self.assertIsInstance(self.execute(api=FakeHostAPI(config=config)), ActionFailed)

    def test_validation_and_guard_precede_request(self):
        for query in (None, 123, [], "", " \n ", "word " * 1001, "界" * 1800, "\ud800", "AKIAIOSFODNN7EXAMPLE", "email me at private@example.org"):
            with self.subTest(query=repr(query)):
                self.assertIsInstance(self.execute(query=query), ActionFailed)
        for provider in (None, "", "OPENAI", "other", 42, []):
            self.assertIsInstance(self.execute(provider=provider), ActionFailed)
        for extra in ({"api_key": "secret"}, {"model": "anything"}, {"system": "target brand"}, {"url": "https://example.org"}):
            self.assertIsInstance(self.execute(**extra), ActionFailed)
        self.assertIsInstance(aeo.BUNDLED_TOOL.execute("check_query", {}, self.api), ActionFailed)
        self.assertIsInstance(aeo.BUNDLED_TOOL.execute("other", {}, self.api), ActionFailed)
        self.open.assert_not_called()

    def test_longer_query_only_guard_exception_and_character_boundary(self):
        query = "Find the right small business bookkeeping tool. " * 40
        self.assertGreater(len(query.encode()), 1024)
        with patch.object(self.api.outbound, "guard_request_parameter_string", wraps=self.api.outbound.guard_request_parameter_string) as guard:
            value = self.successful()
            self.reply(openai_response())
            result = self.execute(query=query)
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(guard.call_args.kwargs, {"allow_longer_text": True})
        self.assertEqual(result.result["query"], query)
        self.assertEqual(value["query"], QUESTION)

    def test_query_character_and_utf8_byte_limits_are_both_enforced(self):
        phrase = "Which bookkeeping tools work for small businesses? "
        query = (phrase * 100)[:5000].ljust(5000)
        self.assertEqual(len(query), 5000)
        for accepted in (query, "界 " * 1280):
            self.reply(openai_response())
            result = self.execute(query=accepted)
            self.assertIsInstance(result, ActionExecuted)
            self.assertEqual(result.result["query"], accepted)
            self.assertEqual(json.loads(self.open.call_args.args[0].data)["input"], accepted)
        self.open.reset_mock()
        for rejected in (query + " ", "界 " * 1280 + " ", "界" * 5000):
            result = self.execute(query=rejected)
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("5000 characters and 5120 UTF-8 bytes", result.error)
        self.open.assert_not_called()
        self.assertEqual(self.api.approvals.records, {})

    def test_search_enablement_is_not_evidence(self):
        for provider, fixture in (("openai", openai_response), ("anthropic", anthropic_response)):
            response = fixture()
            if provider == "openai":
                response["output"] = response["output"][1:]
            else:
                response["content"] = [response["content"][-1]]
                response["usage"]["server_tool_use"]["web_search_requests"] = 0
            self.reply(response)
            value = self.successful(provider)
            self.assertFalse(value["search_used"])
            self.assertEqual(value["search_status"], "not_used")
            self.assertEqual(value["observation_status"], "partial")

    def test_anthropic_search_error_empty_success_and_unresolved_call(self):
        for payload, expected in (({"type": "web_search_tool_result_error", "error_code": "unavailable"}, "failed"), ([], "completed"), (None, "unknown")):
            response = anthropic_response()
            response["content"][2]["content"] = payload
            self.reply(response)
            value = self.successful("anthropic")
            self.assertTrue(value["search_used"])
            self.assertEqual(value["search_status"], expected)
            self.assertEqual(value["search_calls"][0]["sources"], [])
            self.assertEqual(value["observation_status"], "complete" if expected == "completed" else "partial")

    def test_provider_truncation_pause_failure_refusal_and_missing_answer(self):
        cases = [("openai", "incomplete", "partial"), ("openai", "failed", "failed"),
                 ("anthropic", "max_tokens", "partial"), ("anthropic", "pause_turn", "partial"), ("anthropic", "refusal", "refused")]
        for provider, status, expected in cases:
            response = openai_response() if provider == "openai" else anthropic_response()
            response["status" if provider == "openai" else "stop_reason"] = status
            if provider == "openai" and status == "incomplete":
                response["incomplete_details"] = {"reason": "max_output_tokens"}
            self.reply(response)
            value = self.successful(provider)
            self.assertEqual(value["observation_status"], expected)
            self.assertEqual(value["output_truncated"], status in {"incomplete", "max_tokens"})
            self.assertFalse(value["evidence_truncated"])
            self.open.assert_called()
        response = openai_response()
        response["output"][-1]["content"] = []
        self.reply(response)
        self.assertEqual(self.successful()["observation_status"], "no_answer")
        response["output"][-1]["content"] = [{"type": "refusal", "refusal": "Cannot help"}]
        self.reply(response)
        self.assertEqual(self.successful()["observation_status"], "refused")

    def test_uncited_searched_answer_is_partial(self):
        response = openai_response()
        response["output"][-1]["content"][0]["annotations"] = []
        self.reply(response)
        value = self.successful()
        self.assertTrue(value["search_used"])
        self.assertEqual(value["search_status"], "completed")
        self.assertEqual(value["observation_status"], "partial")
        self.assertEqual(value["citations"], [])

    def test_local_evidence_bounds_mark_partial_without_clipping_urls(self):
        response = openai_response()
        response["output"][-1]["content"][0]["text"] = "a" * 32001
        response["output"][0]["action"]["sources"] = [{"url": "https://example.org/" + "a" * 8192}]
        self.reply(response)
        value = self.successful()
        self.assertTrue(value["evidence_truncated"])
        self.assertEqual(value["observation_status"], "partial")
        self.assertEqual(len(value["answer_text"]), 32000)
        self.assertEqual(value["search_calls"][0]["sources"], [])

    def test_cost_missing_invalid_usage_and_unknown_model_are_unknown_not_free(self):
        for change in ({"usage": {}}, {"usage": {"input_tokens": True, "output_tokens": 100}}, {"model": "new-unknown-model"}):
            response = openai_response()
            response.update(change)
            self.reply(response)
            api = FakeHostAPI(config={"OPENAI_API_KEY": OPENAI_KEY})
            value = self.successful(api=api)
            self.assertIsNone(value["cost"]["estimated_usd"])
            self.assertEqual(api.costs.calls, [])
        response = anthropic_response()
        response["usage"].pop("server_tool_use")
        self.reply(response)
        self.assertIsNone(self.successful("anthropic")["cost"]["estimated_usd"])

    def test_anthropic_tiered_cost_includes_cache_and_billed_search_usage(self):
        response = anthropic_response()
        response["usage"].update(input_tokens=100001, cache_read_input_tokens=100, cache_creation_input_tokens=10)
        self.reply(response)
        value = self.successful("anthropic")
        expected = (Decimal("100001") * Decimal("0.10") + Decimal("100") * Decimal("0.01") + Decimal("10") * Decimal("0.125") + Decimal("100") * Decimal("0.50")) * 5 / 1000000 + Decimal("0.01")
        self.assertEqual(Decimal(value["cost"]["estimated_usd"]), expected)

    def test_paid_partial_is_accounted_and_invalid_response_does_not_retry(self):
        response = openai_response()
        response["output"] = "invalid"
        self.reply(response)
        self.assertIsInstance(self.execute(), ActionFailed)
        self.open.assert_called_once()
        self.open.reset_mock()
        response = openai_response()
        response["status"] = "incomplete"
        self.reply(response)
        value = self.successful()
        self.assertEqual(value["observation_status"], "partial")
        self.assertTrue(self.api.costs.calls)
        self.open.assert_called_once()

    def test_transport_errors_timeouts_redirects_and_oversize_never_retry_or_leak(self):
        for status in (302, 400, 401, 403, 429, 500):
            self.open.reset_mock()
            self.open.side_effect = urllib.error.HTTPError(aeo.ENDPOINTS["openai"], status, "secret", {}, io.BytesIO(OPENAI_KEY.encode()))
            result = self.execute()
            self.assertIsInstance(result, ActionFailed)
            self.assertNotIn(OPENAI_KEY, result.error)
            self.open.assert_called_once()
        for error in (TimeoutError(OPENAI_KEY), urllib.error.URLError(OPENAI_KEY)):
            self.open.reset_mock()
            self.open.side_effect = error
            result = self.execute()
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("No retry", result.error)
            self.assertNotIn(OPENAI_KEY, result.error)
            self.open.assert_called_once()
        self.open.side_effect = None
        self.open.return_value = Response(b" " * (aeo.MAX_RESPONSE_BYTES + 1))
        self.assertIsInstance(self.execute(), ActionFailed)
        self.open.return_value = Response(b"not json: " + OPENAI_KEY.encode())
        self.assertIsInstance(self.execute(), ActionFailed)
        self.assertEqual(self.api.costs.calls, [])

    def test_real_registry_catalog_manifest_and_schema_contract(self):
        self.assertIs(tools_host.BUNDLED_TOOLS["aeo_checker"], aeo.BUNDLED_TOOL)
        entry = tools_client._tool_entry(aeo.BUNDLED_TOOL, {"aeo_checker"}, set())
        self.assertEqual(entry["display_name"], "AEO Checker")
        self.assertTrue(entry["enabled"])
        self.assertEqual(entry["connection"], "enable_only")
        self.assertEqual({item.key for item in aeo.MANIFEST.config}, {"OPENAI_API_KEY", "ANTHROPIC_API_KEY"})
        action, = aeo.MANIFEST.actions
        self.assertEqual(action.approval, "direct")
        self.assertTrue(aeo.MANIFEST.reports_cost)
        self.assertEqual(tools_host.unsupported_schema_error(action.input_schema), "")
        self.assertEqual(tools_host.unsupported_schema_error(action.output_schema), "")
        self.assertFalse(action.input_schema["additionalProperties"])
        self.assertEqual(action.input_protections["query"].allow_longer_text, True)
        self.assertFalse(action.input_protections["query"].allow_identifiers)
        self.assertFalse(action.input_protections["query"].allow_machine_tokens)


class AEOStageProbeTests(unittest.TestCase):
    def stage(self, providers):
        from tests.stage.stage_tool_checks import StageToolChecks
        stage = StageToolChecks()
        stage._api = Mock(return_value={"tools": [{"tool_id": "aeo_checker", "enabled": True, "config": [
            {"key": aeo.KEYS[provider], "set": provider in providers} for provider in ("openai", "anthropic")]}]})
        return stage

    def test_stage_accepts_either_key_and_runs_only_configured_provider(self):
        for providers in (("openai",), ("anthropic",), ("openai", "anthropic")):
            stage = self.stage(providers)
            self.assertEqual(stage._tool_credential_failures("aeo_checker"), [])
            stage._successful_tool_call = Mock(return_value={"observation_status": "complete", "search_status": "completed", "answer_text": "Answer", "citations": [{"url": URL}]})
            stage._check_tool_provider("aeo_checker")
            self.assertEqual([call.args[1]["provider"] for call in stage._successful_tool_call.call_args_list], list(providers))
        stage = self.stage(())
        self.assertTrue(stage._tool_credential_failures("aeo_checker"))

    def test_stage_autoconfiguration_enables_with_one_key(self):
        stage = self.stage(("anthropic",))
        with patch.dict("os.environ", {}, clear=True):
            stage.autoconfigure_tools(("aeo_checker",))
        stage._api.assert_any_call("POST", "/v1/tools/aeo_checker/enable", {})

    def test_stage_rejects_incomplete_or_uncited_observations(self):
        for result in ({"observation_status": "partial", "search_status": "completed", "answer_text": "Answer"},
                       {"observation_status": "complete", "search_status": "completed", "answer_text": "Answer", "citations": []}):
            stage = self.stage(("openai",))
            stage._successful_tool_call = Mock(return_value=result)
            with self.assertRaises(AssertionError):
                stage._check_tool_provider("aeo_checker")
