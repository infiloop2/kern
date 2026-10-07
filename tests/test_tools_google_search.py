"""Unit tests for general Google web search (all provider calls mocked)."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from host.tools import google_search
from host.tools.google_search import GoogleSearchTool
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI, assert_matches_output_schema


def configured_api() -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["SERPERAPI_API_KEY"] = "serper-key"
    return api


class GoogleSearchTests(unittest.TestCase):
    def test_general_query_locale_paging_and_provider_positions(self) -> None:
        api = configured_api()
        response = {"organic": [
            {"link": "https://EXAMPLE.com/product?plan=team#pricing", "position": 11, "title": "Competitor", "snippet": "x" * 2000},
            {"link": "https://example.com/product?plan=team#other", "position": 12},
            {"link": "http://another.org/", "position": 14},
            {"link": "https://third.net/"},
            {"link": "https://fourth.net/", "position": True},
            {"link": "https://fifth.net/", "position": "5"},
            {"link": "https://sixth.net/", "position": 0},
            {"link": "https://seventh.net/", "position": -1},
        ]}
        with patch.object(google_search, "json_request", return_value=response) as request:
            result = GoogleSearchTool().execute("search", {
                "query": 'site:example.com "pricing" OR competitor', "country": "GB",
                "language": "ZH-CN", "page": "2", "limit": "7",
            }, api)
        assert_matches_output_schema(self, google_search.MANIFEST, "search", result)
        assert isinstance(result, ActionExecuted)
        request.assert_called_once()
        self.assertEqual(request.call_args.args, ("POST", google_search.SERPER_SEARCH_URL))
        self.assertEqual(request.call_args.kwargs["headers"], {"X-API-KEY": "serper-key"})
        self.assertEqual(request.call_args.kwargs["body"], {
            "q": 'site:example.com "pricing" OR competitor', "gl": "gb", "hl": "zh-cn",
            "page": 2, "num": 7, "autocorrect": False,
        })
        rows = result.result["results"]
        self.assertEqual([row["position"] for row in rows], [11, 14, None, None, None, None, None])
        self.assertEqual(rows[0]["url"], "https://example.com/product?plan=team")
        self.assertEqual(len(rows[0]["snippet"]), 1500)
        self.assertEqual((result.result["country"], result.result["language"], result.result["page"], result.result["limit"]), ("gb", "zh-cn", 2, 7))
        self.assertIsNotNone(datetime.fromisoformat(result.result["retrieved_at"]).tzinfo)
        self.assertIn("unverified", result.result["position_semantics"])
        self.assertEqual(api.costs.calls, [("0.001000000", "")])
        self.assertNotIn("serper-key", str(result.result))

    def test_defaults_empty_results_and_one_charge(self) -> None:
        api = configured_api()
        with patch.object(google_search, "json_request", return_value={}) as request:
            result = GoogleSearchTool().execute("search", {"query": "unusual topic"}, api)
        assert_matches_output_schema(self, google_search.MANIFEST, "search", result)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(request.call_args.kwargs["body"], {"q": "unusual topic", "gl": "us", "hl": "en", "num": 10, "page": 1, "autocorrect": False})
        self.assertEqual(result.result["results"], [])
        self.assertEqual(result.result["people_also_ask"], [])
        self.assertEqual(result.result["related_searches"], [])
        self.assertEqual(api.costs.calls, [("0.001000000", "")])

    def test_closed_bounded_search_features(self) -> None:
        response = {
            "peopleAlsoAsk": [{"question": "q" * 600, "title": "t" * 600, "snippet": "s" * 1800, "link": "javascript:alert(1)", "extra": "omit"}] * 15,
            "relatedSearches": [{"query": "x" * 400, "extra": "omit"}] * 15,
            "knowledgeGraph": {"arbitrary": "omit"},
        }
        with patch.object(google_search, "json_request", return_value=response):
            result = GoogleSearchTool().execute("search", {"query": "topic"}, configured_api())
        assert_matches_output_schema(self, google_search.MANIFEST, "search", result)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(result.result["people_also_ask"]), 10)
        self.assertEqual(result.result["people_also_ask"][0], {"question": "q" * 500, "title": "t" * 500, "snippet": "s" * 1500, "url": ""})
        self.assertEqual(result.result["related_searches"], ["x" * 300] * 10)
        self.assertNotIn("knowledgeGraph", result.result)

    def test_malformed_features_and_organic_are_ignored(self) -> None:
        for response in (
            {"organic": {}, "peopleAlsoAsk": {}, "relatedSearches": "bad"},
            {"organic": [None, "bad", {}], "peopleAlsoAsk": [None, "bad", {}], "relatedSearches": [None, "bad", {}]},
        ):
            with self.subTest(response=response), patch.object(google_search, "json_request", return_value=response):
                result = GoogleSearchTool().execute("search", {"query": "topic"}, configured_api())
                assert_matches_output_schema(self, google_search.MANIFEST, "search", result)
                assert isinstance(result, ActionExecuted)
                self.assertEqual(result.result["results"], [])
                self.assertEqual(result.result["people_also_ask"], [])
                self.assertEqual(result.result["related_searches"], [])

    def test_unsafe_urls_removed_without_fetching(self) -> None:
        links = ["javascript:alert(1)", "https://user:pass@example.com/a", "https://example.com:8443/a", "https://localhost/a", "https://127.0.0.1/a", "https://127.1/a", "https://[::1]/a", "https://box.internal/a", "https://box.local/a", "https://a.example/space here", "https://a.example/" + "x" * 2048, "https://exa\tmple.com/"]
        response = {"organic": [{"link": link, "position": i + 1} for i, link in enumerate(links)] + [{"link": "https://safe.org/a", "position": 22}]}
        with patch.object(google_search, "json_request", return_value=response) as request:
            result = GoogleSearchTool().execute("search", {"query": "topic"}, configured_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual([r["url"] for r in result.result["results"]], ["https://safe.org/a"])
        self.assertEqual(result.result["results"][0]["position"], 22)
        request.assert_called_once()

    def test_bad_inputs_do_not_call_provider_or_report_cost(self) -> None:
        inputs = [{}, {"query": " "}, {"query": "x" * 301}, {"query": "topic", "country": "USA"}, {"query": "topic", "country": "üS"}, {"query": "topic", "language": "english"}, {"query": "topic", "language": "en&key=secret"}, {"query": "topic", "language": True}, {"query": "topic", "page": "11"}, {"query": "topic", "page": "0"}, {"query": "topic", "limit": "11"}, {"query": "topic", "limit": "0"}, {"query": "topic", "limit": True}, {"query": "topic", "limit": "²"}]
        for tool_input in inputs:
            api = configured_api()
            with self.subTest(tool_input=tool_input), patch.object(google_search, "json_request") as request:
                self.assertIsInstance(GoogleSearchTool().execute("search", tool_input, api), ActionFailed)
                request.assert_not_called()
                self.assertEqual(api.costs.calls, [])

    def test_guard_blocks_general_query_before_egress(self) -> None:
        api = configured_api()
        with patch.object(google_search, "json_request") as request:
            result = GoogleSearchTool().execute("search", {"query": "posts by alice.smith@acme.com"}, api)
        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()
        self.assertEqual(api.costs.calls, [])

    def test_errors_are_curated_and_do_not_report_success_charge(self) -> None:
        for status, expected in ((401, "API key"), (403, "API key"), (429, "credits"), (500, "HTTP 500")):
            api = configured_api()
            with self.subTest(status=status), patch.object(google_search, "json_request", side_effect=WebRequestError("raw", status=status, body=b"secret details")):
                result = GoogleSearchTool().execute("search", {"query": "topic"}, api)
            assert isinstance(result, ActionFailed)
            self.assertIn(expected, result.error)
            self.assertNotIn("secret", result.error)
            self.assertEqual(api.costs.calls, [])
        result = GoogleSearchTool().execute("search", {"query": "topic"}, FakeHostAPI())
        assert isinstance(result, ActionFailed)
        self.assertIn("SERPERAPI_API_KEY", result.error)

    def test_linkedin_is_an_ordinary_site_query_and_old_action_is_unavailable(self) -> None:
        with patch.object(google_search, "json_request", return_value={}) as request:
            result = GoogleSearchTool().execute("search", {"query": "site:linkedin.com/posts AI agents"}, configured_api())
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(request.call_args.kwargs["body"]["q"], "site:linkedin.com/posts AI agents")
        with patch.object(google_search, "json_request") as request:
            self.assertIsInstance(GoogleSearchTool().execute("search_posts", {"query": "AI agents"}, configured_api()), ActionFailed)
            request.assert_not_called()

    def test_manifest_discloses_general_scope_and_new_identity(self) -> None:
        manifest = google_search.MANIFEST
        self.assertEqual(manifest.tool_id, "google_search")
        self.assertEqual(manifest.display_name, "Google Search (Serper)")
        self.assertEqual([c.key for c in manifest.config], ["SERPERAPI_API_KEY"])
        self.assertTrue(manifest.reports_cost)
        search = manifest.actions[0]
        self.assertEqual([action.id for action in manifest.actions], ["search"])
        self.assertEqual(search.id, "search")
        self.assertEqual(search.approval, "direct")
        self.assertIn("Serper and Google", search.data_policy)
        self.assertIn("general-query", manifest.setup_steps[-1].description)
        self.assertIn("Queries may cover any public website", manifest.data_summary.cards[0].description)


if __name__ == "__main__":
    unittest.main()
