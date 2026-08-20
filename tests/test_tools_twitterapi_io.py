"""Tests for the read-only TwitterAPI.io bundled tool."""

from __future__ import annotations

import urllib.parse
import unittest
from unittest.mock import patch

from host.param_guard import ParamGuardDenied
from host.tools import twitterapi_io
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared.web import UnmappedProviderError, WebRequestError
from test_tools import FakeHostAPI


def api() -> FakeHostAPI:
    return FakeHostAPI(config={"TWITTERAPI_IO_API_KEY": "secret-key"})


class TwitterApiIoToolTests(unittest.TestCase):
    def test_manifest_is_bounded_read_only_and_declares_the_guard(self) -> None:
        manifest = twitterapi_io.MANIFEST
        self.assertEqual(manifest.tool_id, "twitterapi_io")
        self.assertEqual(manifest.connection, "enable_only")
        self.assertEqual([action.id for action in manifest.actions], ["search_tweets"])
        copy = " ".join(manifest.protections)
        self.assertIn("512 characters", copy)
        self.assertIn("parameter guard", copy)
        self.assertIn("one fixed-endpoint request", copy)
        self.assertIn("provider read count", copy)
        search_copy = manifest.data_summary.cards[0].points[0].text
        self.assertIn("full default host parameter guard", search_copy)
        self.assertNotIn("recursively decoded", search_copy)

    def test_search_uses_fixed_endpoint_guarded_query_and_api_key_header(self) -> None:
        captured = {}

        def fake_json_request(method, url, **kwargs):
            captured.update(method=method, url=url, **kwargs)
            return {"tweets": []}

        with patch.object(twitterapi_io, "json_request", side_effect=fake_json_request):
            result = twitterapi_io.BUNDLED_TOOL.execute(
                "search_tweets",
                {
                    "query": "AI agents -filter:retweets",
                    "query_type": "Top",
                    "lookback_hours": 0,
                    "max_results": 7,
                },
                api(),
            )

        self.assertIsInstance(result, ActionExecuted)
        parsed = urllib.parse.urlsplit(captured["url"])
        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}",
            twitterapi_io.SEARCH_ENDPOINT,
        )
        self.assertEqual(
            urllib.parse.parse_qs(parsed.query),
            {"query": ["AI agents -filter:retweets"], "queryType": ["Top"]},
        )
        self.assertEqual(captured["method"], "GET")
        self.assertEqual(captured["headers"], {"X-API-Key": "secret-key"})
        self.assertNotIn("body", captured)

    def test_query_is_required_and_rejected_above_512_characters(self) -> None:
        with patch.object(twitterapi_io, "_search", return_value={"tweets": []}) as search:
            for tool_input in ({}, {"query": "   "}, {"query": "x" * 513}):
                with self.subTest(tool_input=tool_input):
                    result = twitterapi_io.BUNDLED_TOOL.execute(
                        "search_tweets", tool_input, api()
                    )
                    self.assertIsInstance(result, ActionFailed)
            result = twitterapi_io.BUNDLED_TOOL.execute(
                "search_tweets", {"query": ("ai agents " * 52)[:512]}, api()
            )
        self.assertNotIsInstance(result, ActionFailed)
        search.assert_called_once()

    def test_search_adds_recency_and_structured_exclusions(self) -> None:
        with patch.object(twitterapi_io, "now", return_value=1_800_000_000):
            request = twitterapi_io._search_request(
                {
                    "query": "AI agents -is:reply -is:retweet",
                    "lookback_hours": 8,
                    "exclude_usernames": ["Builder", "builder", "Other_User"],
                    "max_results": 6,
                },
                api(),
            )

        self.assertEqual(request.max_results, 6)
        self.assertTrue(request.exclude_replies)
        self.assertTrue(request.exclude_retweets)
        self.assertEqual(
            request.parameters["query"],
            (
                "AI agents -filter:replies -filter:nativeretweets "
                "-from:builder -from:other_user since_time:1799971200"
            ),
        )

    def test_search_defaults_to_seven_days_and_ten_visible_results(self) -> None:
        with patch.object(twitterapi_io, "now", return_value=1_800_000_000):
            request = twitterapi_io._search_request({"query": "Kern"}, api())

        self.assertEqual(request.max_results, 10)
        self.assertEqual(request.parameters["query"], "Kern since_time:1799395200")

    def test_search_options_are_strict(self) -> None:
        invalid_inputs = (
            {"query": "Kern", "max_results": 0},
            {"query": "Kern", "max_results": 21},
            {"query": "Kern", "lookback_hours": 721},
            {"query": "Kern", "exclude_replies": "yes"},
            {"query": "Kern", "exclude_retweets": 1},
            {"query": "Kern", "exclude_usernames": ["invalid-handle"]},
            {"query": "Kern", "exclude_usernames": ["a"] * 11},
            {"query": "Kern", "cursor": "not-supported"},
        )
        for tool_input in invalid_inputs:
            with self.subTest(tool_input=tool_input):
                result = twitterapi_io.BUNDLED_TOOL.execute(
                    "search_tweets", tool_input, api()
                )
                self.assertIsInstance(result, ActionFailed)

    def test_query_type_is_strict(self) -> None:
        for value in ("latest", "Recent", "", 7):
            with self.subTest(value=value):
                result = twitterapi_io.BUNDLED_TOOL.execute(
                    "search_tweets", {"query": "Kern", "query_type": value}, api()
                )
                self.assertIsInstance(result, ActionFailed)
                self.assertIn("Latest or Top", result.error)

    def test_query_passes_through_parameter_guard(self) -> None:
        with self.assertRaises(ParamGuardDenied):
            twitterapi_io._search_parameters(
                {"query": "verify AKIAIOSFODNN7EXAMPLE now"},
                api(),
            )

    def test_excluded_usernames_pass_through_parameter_guard(self) -> None:
        with self.assertRaises(ParamGuardDenied):
            twitterapi_io._search_parameters(
                {"query": "Kern", "exclude_usernames": ["4155552671"]},
                api(),
            )

    def test_response_is_normalized_and_bounded_to_requested_posts(self) -> None:
        provider_posts = [
            {
                "id": str(100 + index),
                "text": f"post {index}",
                "url": f"https://x.com/user/status/{100 + index}",
                "createdAt": "2026-08-18T12:00:00Z",
                "lang": "en",
                "conversationId": "88",
                "viewCount": 1000,
                "likeCount": 10,
                "replyCount": 2,
                "retweetCount": 3,
                "quoteCount": 1,
                "bookmarkCount": 4,
                "author": {"id": "42", "userName": "builder", "name": "Builder"},
            }
            for index in range(25)
        ]
        provider_posts.insert(2, {"id": "not-numeric", "text": "drop me"})
        with patch.object(
            twitterapi_io,
            "_search",
            return_value={"tweets": provider_posts, "next_cursor": "ignored"},
        ):
            result = twitterapi_io.BUNDLED_TOOL.execute(
                "search_tweets",
                {"query": "Kern", "max_results": 20, "lookback_hours": 0},
                api(),
            )

        self.assertIsInstance(result, ActionExecuted)
        assert isinstance(result, ActionExecuted)
        posts = result.result["posts"]
        self.assertIsInstance(posts, list)
        assert isinstance(posts, list)
        self.assertEqual(len(posts), 19)
        self.assertEqual(result.result["provider"], "twitterapi_io")
        self.assertEqual(result.result["provider_posts_returned"], 20)
        self.assertEqual(result.result["billable_post_reads"], 20)
        self.assertEqual(result.result["locally_filtered_posts"], 1)
        self.assertEqual(result.result["locally_truncated_posts"], 0)
        first = posts[0]
        self.assertEqual(first["author_username"], "builder")
        self.assertEqual(
            first["public_metrics"],
            {
                "impression_count": 1000,
                "like_count": 10,
                "reply_count": 2,
                "repost_count": 3,
                "quote_count": 1,
                "bookmark_count": 4,
            },
        )
        self.assertNotIn("next_cursor", result.result)

    def test_response_defensively_filters_replies_and_retweets_before_output_cap(self) -> None:
        provider_posts = [
            {"id": "1", "text": "reply", "isReply": True},
            {"id": "2", "text": "RT @builder repost", "type": "retweet"},
            {"id": "3", "text": "first"},
            {"id": "4", "text": "second"},
            {"id": "5", "text": "third"},
        ]
        with patch.object(
            twitterapi_io,
            "_search",
            return_value={"tweets": provider_posts},
        ):
            result = twitterapi_io.BUNDLED_TOOL.execute(
                "search_tweets",
                {
                    "query": "Kern",
                    "lookback_hours": 0,
                    "max_results": 2,
                    "exclude_replies": True,
                    "exclude_retweets": True,
                },
                api(),
            )

        self.assertIsInstance(result, ActionExecuted)
        assert isinstance(result, ActionExecuted)
        posts = result.result["posts"]
        assert isinstance(posts, list)
        self.assertEqual([post["id"] for post in posts], ["3", "4"])
        self.assertEqual(result.result["provider_posts_returned"], 5)
        self.assertEqual(result.result["billable_post_reads"], 5)
        self.assertEqual(result.result["locally_filtered_posts"], 2)
        self.assertEqual(result.result["locally_truncated_posts"], 1)

    def test_zero_provider_results_still_reports_minimum_billable_read(self) -> None:
        with patch.object(twitterapi_io, "_search", return_value={"tweets": []}):
            result = twitterapi_io.BUNDLED_TOOL.execute(
                "search_tweets", {"query": "Kern", "lookback_hours": 0}, api()
            )

        self.assertIsInstance(result, ActionExecuted)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["provider_posts_returned"], 0)
        self.assertEqual(result.result["billable_post_reads"], 1)

    def test_non_array_tweets_response_is_rejected(self) -> None:
        with patch.object(
            twitterapi_io,
            "_search",
            return_value={"tweets": {"id": "123"}},
        ):
            result = twitterapi_io.BUNDLED_TOOL.execute(
                "search_tweets", {"query": "Kern"}, api()
            )

        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(
            result.error,
            "TwitterAPI.io returned an invalid search response.",
        )

    def test_negative_boolean_and_string_metrics_are_dropped(self) -> None:
        post = twitterapi_io._normalized_post(
            {
                "id": "123",
                "text": "safe",
                "viewCount": -1,
                "likeCount": True,
                "replyCount": "2",
                "retweetCount": 0,
            }
        )
        assert post is not None
        self.assertEqual(post["public_metrics"], {"repost_count": 0})

    def test_provider_url_and_invalid_identifiers_are_not_forwarded(self) -> None:
        post = twitterapi_io._normalized_post(
            {
                "id": "123",
                "text": "safe",
                "url": "javascript:alert(1)",
                "conversationId": "not-an-id",
                "author": {
                    "id": "not-an-id",
                    "userName": "invalid-handle",
                    "name": "Builder",
                },
            }
        )
        assert post is not None
        self.assertEqual(post["url"], "https://x.com/i/status/123")
        self.assertEqual(post["conversation_id"], "")
        self.assertEqual(post["author_id"], "")
        self.assertEqual(post["author_username"], "")

    def test_oversized_identifiers_are_rejected_without_truncation(self) -> None:
        self.assertIsNone(
            twitterapi_io._normalized_post({"id": "1" * 26, "text": "safe"})
        )
        post = twitterapi_io._normalized_post(
            {
                "id": "123",
                "text": "safe",
                "conversationId": "2" * 26,
                "author": {
                    "id": "3" * 26,
                    "userName": "a" * 16,
                },
            }
        )
        assert post is not None
        self.assertEqual(post["url"], "https://x.com/i/status/123")
        self.assertEqual(post["conversation_id"], "")
        self.assertEqual(post["author_id"], "")
        self.assertEqual(post["author_username"], "")

    def test_provider_errors_are_sanitized(self) -> None:
        cases = (
            (400, "rejected the search query"),
            (401, "rejected the configured API key"),
            (402, "credits are exhausted"),
            (429, "rate limit"),
        )
        for status, message in cases:
            with self.subTest(status=status), patch.object(
                twitterapi_io,
                "json_request",
                side_effect=WebRequestError("raw provider body", status=status),
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    twitterapi_io._search("key", {"query": "Kern", "queryType": "Latest"})

    def test_unmapped_provider_error_reaches_host_boundary(self) -> None:
        with patch.object(
            twitterapi_io,
            "json_request",
            side_effect=WebRequestError("raw provider body", status=500),
        ):
            with self.assertRaises(UnmappedProviderError):
                twitterapi_io.BUNDLED_TOOL.execute(
                    "search_tweets", {"query": "Kern"}, api()
                )

    def test_unsupported_action_fails_without_request(self) -> None:
        with patch.object(twitterapi_io, "json_request") as request:
            result = twitterapi_io.BUNDLED_TOOL.execute("post_tweet", {"query": "x"}, api())
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(result.error, "Unsupported TwitterAPI.io action.")
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
