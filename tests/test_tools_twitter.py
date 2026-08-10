"""Unit tests for the X (Twitter) tool package (all third-party calls mocked)."""

from __future__ import annotations

from dataclasses import replace
import unittest
from typing import Any
from unittest.mock import patch

from host.tools.json_types import JSONObject
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools import twitter
from host.tools.twitter import XTool
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI, FRESH_EXPIRES_AT


def connected_api(*, expires_at: int = FRESH_EXPIRES_AT) -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["X_OAUTH_CLIENT_ID"] = "x-client"
    api.config["X_OAUTH_CLIENT_SECRET"] = "x-secret"
    api.config["X_BEARER_TOKEN"] = "x-app-bearer"
    api.credentials.save(
        {
            "account": {
                "id": "111",
                "label": "@claw",
                "scopes": ["tweet.read", "users.read", "tweet.write", "dm.read", "dm.write", "offline.access"],
            },
            "secret": {
                "access_token": "x-access",
                "expires_at": expires_at,
                "refresh_token": "x-refresh-1",
                "scope": "tweet.read users.read tweet.write dm.read dm.write offline.access",
                "token_type": "bearer",
            },
            "metadata": {"created_at": 1, "updated_at": 1},
        }
    )
    return api


def me_response() -> JSONObject:
    return {"data": {"id": "111", "name": "Claw", "username": "claw"}}


class XToolReadTests(unittest.TestCase):
    def test_manifest_shape(self) -> None:
        tool = XTool()
        self.assertEqual(tool.manifest.connection, "oauth")
        self.assertIsNotNone(tool.credentials)
        self.assertEqual(
            [spec.id for spec in tool.manifest.actions],
            [
                "search_tweets",
                "read_tweet",
                "user_tweets",
                "get_trends",
                "get_personalized_trends",
                "post_tweet",
                "send_dm",
            ],
        )

    def test_search_tweets_maps_results_and_authors(self) -> None:
        seen: dict[str, str] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            return {
                "data": [
                    {"id": "9001", "text": "hello", "author_id": "222", "created_at": "2026-07-09T00:00:00Z",
                     "public_metrics": {"like_count": 3}},
                ],
                "includes": {"users": [{"id": "222", "username": "someone"}]},
            }

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("search_tweets", {"query": "fed rates", "max_results": "20"}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertIn("/tweets/search/recent?", seen["url"])
        self.assertIn("query=fed+rates", seen["url"])
        self.assertIn("max_results=20", seen["url"])
        tweets = result.result["tweets"]
        assert isinstance(tweets, list)
        tweet = tweets[0]
        assert isinstance(tweet, dict)
        self.assertEqual(tweet["author_username"], "someone")
        self.assertEqual(tweet["metrics"], {"like_count": 3})

    def test_search_requires_query(self) -> None:
        result = XTool().execute("search_tweets", {}, connected_api())
        self.assertIsInstance(result, ActionFailed)

    def test_search_rejects_overlong_query_and_out_of_range_limit(self) -> None:
        tool = XTool()
        for tool_input in (
            {"query": "x" * (twitter.MAX_QUERY_CHARS + 1)},
            {"query": "x", "max_results": "9"},
            {"query": "x", "max_results": "101"},
            {"query": "x", "max_results": "9" * 100},
        ):
            with self.subTest(tool_input=tool_input):
                self.assertIsInstance(tool.execute("search_tweets", tool_input, connected_api()), ActionFailed)

    def test_read_results_are_capped_even_if_provider_returns_extra_items(self) -> None:
        provider_tweets = [{"id": str(index), "text": "post"} for index in range(30)]
        with patch.object(twitter, "json_request", return_value={"data": provider_tweets}):
            result = XTool().execute(
                "search_tweets", {"query": "x", "max_results": "10"}, connected_api()
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(result.result["tweets"]), 10)

    def test_user_tweets_resolves_username(self) -> None:
        urls: list[str] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            urls.append(url)
            if "/users/by/username/" in url:
                return {"data": {"id": "333", "username": "target"}}
            return {"data": [{"id": "1", "text": "post"}]}

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("user_tweets", {"username": "@target"}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertIn("/users/by/username/target", urls[0])
        self.assertIn("/users/333/tweets?", urls[1])
        tweets = result.result["tweets"]
        assert isinstance(tweets, list)
        tweet = tweets[0]
        assert isinstance(tweet, dict)
        self.assertEqual(tweet["author_username"], "target")

    def test_user_lookup_rejects_provider_id_before_building_a_path(self) -> None:
        urls: list[str] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            urls.append(url)
            return {"data": {"id": "../tweets", "username": "target"}}

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("user_tweets", {"username": "target"}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["tweets"], [])
        self.assertEqual(len(urls), 1)

    def test_user_tweets_requires_exactly_one_selector(self) -> None:
        tool = XTool()
        for tool_input in ({}, {"username": "a", "user_id": "1"}):
            self.assertIsInstance(tool.execute("user_tweets", tool_input, connected_api()), ActionFailed)

    def test_user_tweets_enforces_requested_limit_on_provider_response(self) -> None:
        provider_tweets = [{"id": str(index), "text": "post"} for index in range(20)]

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/by/username/" in url:
                return {"data": {"id": "333", "username": "target"}}
            return {"data": provider_tweets}

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute(
                "user_tweets", {"username": "target", "max_results": "5"}, connected_api()
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(result.result["tweets"]), 5)

    def test_get_trends_uses_app_bearer_and_maps_results(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            seen["headers"] = kwargs.get("headers")
            return {"data": [{"trend_name": "#AI", "tweet_count": 250000}, {"trend_name": "Breaking News"}]}

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("get_trends", {"woeid": "23424977", "max_trends": "30"}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertIn("/trends/by/woeid/23424977?", seen["url"])
        self.assertIn("max_trends=30", seen["url"])
        self.assertEqual(seen["headers"]["authorization"], "Bearer x-app-bearer")
        trends = result.result["trends"]
        assert isinstance(trends, list)
        self.assertEqual(trends[0], {"trend_name": "#AI", "tweet_count": 250000})
        self.assertEqual(trends[1], {"trend_name": "Breaking News", "tweet_count": None})

    def test_get_trends_defaults_to_worldwide(self) -> None:
        seen: dict[str, str] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            return {"data": []}

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("get_trends", {}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertIn("/trends/by/woeid/1?", seen["url"])
        self.assertIn("worldwide", str(result.result["message"]))

    def test_trend_results_are_bounded(self) -> None:
        provider_trends = [{"trend_name": f"trend-{index}"} for index in range(100)]
        with patch.object(twitter, "json_request", return_value={"data": provider_trends}):
            trends = XTool().execute("get_trends", {"max_trends": "5"}, connected_api())
            personalized = XTool().execute("get_personalized_trends", {}, connected_api())
        assert isinstance(trends, ActionExecuted)
        assert isinstance(personalized, ActionExecuted)
        self.assertEqual(len(trends.result["trends"]), 5)
        self.assertEqual(len(personalized.result["trends"]), 50)

    def test_get_trends_rejects_non_numeric_woeid(self) -> None:
        result = XTool().execute("get_trends", {"woeid": "../users/me"}, connected_api())
        self.assertIsInstance(result, ActionFailed)

    def test_get_trends_401_names_bearer_token_not_reconnect(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=401)

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("get_trends", {}, connected_api())
        assert isinstance(result, ActionFailed)
        self.assertFalse(result.reconnect_required)
        self.assertIn("X_BEARER_TOKEN", result.error)

    def test_personalized_trends_uses_user_token_and_maps_fields(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            seen["headers"] = kwargs.get("headers")
            return {
                "data": [
                    {"trend_name": "Quantum", "category": "Technology", "post_count": 4200, "trending_since": "3 hours ago"},
                ]
            }

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("get_personalized_trends", {}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertIn("/users/personalized_trends?", seen["url"])
        self.assertEqual(seen["headers"]["authorization"], "Bearer x-access")
        trends = result.result["trends"]
        assert isinstance(trends, list)
        self.assertEqual(
            trends[0],
            {"trend_name": "Quantum", "category": "Technology", "post_count": 4200, "trending_since": "3 hours ago"},
        )

    def test_personalized_trends_reject_input_and_map_401_to_reconnect(self) -> None:
        self.assertIsInstance(
            XTool().execute("get_personalized_trends", {"woeid": "1"}, connected_api()), ActionFailed
        )

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=401)

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("get_personalized_trends", {}, connected_api())
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)

    def test_unauthorized_maps_to_reconnect(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=401)

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("search_tweets", {"query": "x"}, connected_api())
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)

    def test_not_connected_maps_to_reconnect(self) -> None:
        api = FakeHostAPI()
        api.config["X_OAUTH_CLIENT_ID"] = "x-client"
        api.config["X_OAUTH_CLIENT_SECRET"] = "x-secret"
        result = XTool().execute("search_tweets", {"query": "x"}, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)


class XToolRefreshTests(unittest.TestCase):
    def test_expired_token_refreshes_and_rotates_refresh_token(self) -> None:
        api = connected_api(expires_at=1)  # long past

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == twitter.X_TOKEN_URL:
                self.assertEqual(kwargs["form"]["grant_type"], "refresh_token")
                self.assertEqual(kwargs["form"]["refresh_token"], "x-refresh-1")
                self.assertTrue(kwargs["headers"]["authorization"].startswith("Basic "))
                return {"access_token": "x-access-2", "refresh_token": "x-refresh-2", "expires_in": 7200,
                        "scope": "tweet.read users.read tweet.write dm.read dm.write offline.access",
                        "token_type": "bearer"}
            return {"data": []}

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("search_tweets", {"query": "x"}, api)
        self.assertIsInstance(result, ActionExecuted)
        stored = api.credentials.load()
        assert stored is not None
        self.assertEqual(stored["secret"]["access_token"], "x-access-2")
        self.assertEqual(stored["secret"]["refresh_token"], "x-refresh-2")

    def test_invalid_grant_on_refresh_clears_and_requires_reconnect(self) -> None:
        api = connected_api(expires_at=1)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=400, body=b'{"error": "invalid_grant"}')

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("search_tweets", {"query": "x"}, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIsNone(api.credentials.load())

    def test_refresh_with_reduced_scopes_clears_and_requires_reconnect(self) -> None:
        api = connected_api(expires_at=1)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            return {
                "access_token": "x-access-2",
                "refresh_token": "x-refresh-2",
                "expires_in": 7200,
                "scope": "tweet.read users.read tweet.write offline.access",
                "token_type": "bearer",
            }

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("search_tweets", {"query": "x"}, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIsNone(api.credentials.load())


class XToolPostTests(unittest.TestCase):
    def test_post_tweet_queues_approval_with_account_and_text(self) -> None:
        api = connected_api()

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("post_tweet", {"text": "Hello world"}, api)
        assert isinstance(result, ActionPendingApproval)
        self.assertIn("@claw", result.summary)
        self.assertIn("Hello world", result.summary)
        record = api.approvals.get(result.approval_id)
        assert record is not None
        proposal = record.payload["proposal"]
        assert isinstance(proposal, dict)
        self.assertEqual(proposal["text"], "Hello world")
        self.assertNotIn("target_tweet", record.payload)

    def test_reply_captures_target_and_names_it_in_summary(self) -> None:
        api = connected_api()

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            if "/tweets/9001?" in url:
                return {"data": {"id": "9001", "text": "original post", "author_id": "222"},
                        "includes": {"users": [{"id": "222", "username": "someone"}]}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("post_tweet", {"text": "Nice", "in_reply_to_tweet_id": "9001"}, api)
        assert isinstance(result, ActionPendingApproval)
        self.assertIn("Reply on X", result.summary)
        self.assertIn("@someone", result.summary)
        self.assertIn("original post", result.summary)

    def test_reply_rejects_provider_response_for_a_different_target(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            if "/tweets/9001?" in url:
                return {"data": {"id": "9002", "text": "different post", "author_id": "222"}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute(
                "post_tweet", {"text": "Nice", "in_reply_to_tweet_id": "9001"}, connected_api()
            )
        assert isinstance(result, ActionFailed)
        self.assertIn("not found", result.error)

    def test_post_rejects_reply_and_quote_together(self) -> None:
        result = XTool().execute(
            "post_tweet", {"text": "x", "in_reply_to_tweet_id": "1", "quote_tweet_id": "2"}, connected_api()
        )
        self.assertIsInstance(result, ActionFailed)

    def test_execute_approved_posts_and_reports_id(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_pending):
            pending = XTool().execute("post_tweet", {"text": "Ship it"}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        posted: dict[str, Any] = {}

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            if url.endswith("/tweets") and method == "POST":
                posted["body"] = kwargs["body"]
                return {"data": {"id": "424242", "text": "Ship it"}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_execute):
            result = XTool().execute_approved(approved_record, api)
        assert isinstance(result, ApprovalExecuted)
        self.assertIn("424242", result.message)
        self.assertEqual(posted["body"], {"text": "Ship it"})

    def test_execute_approved_does_not_surface_provider_body(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_pending):
            pending = XTool().execute("post_tweet", {"text": "Ship it"}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            raise WebRequestError(
                "failed",
                status=403,
                body=(
                    b'{"title":"Forbidden","detail":"User is not permitted to create replies.",'
                    b'"type":"https://api.x.com/2/problems/client-forbidden","status":403}'
                ),
            )

        with patch.object(twitter, "json_request", fake_execute):
            result = XTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertIn("HTTP 403", result.error)
        self.assertNotIn("User is not permitted to create replies.", result.error)
        self.assertNotIn("client-forbidden", result.error)

    def test_execute_approved_rejects_non_post_action(self) -> None:
        api = connected_api()
        with patch.object(twitter, "json_request", return_value=me_response()):
            pending = XTool().execute("post_tweet", {"text": "Ship it"}, api)
        assert isinstance(pending, ActionPendingApproval)
        record = replace(api.approvals.approve(pending.approval_id), action_id="read_tweet")
        with patch.object(twitter, "json_request") as request:
            result = XTool().execute_approved(record, api)
        assert isinstance(result, ActionFailed)
        self.assertEqual(result.error, "X approval action is invalid.")
        request.assert_not_called()

    def test_numeric_inputs_reject_unicode_digits(self) -> None:
        with patch.object(twitter, "json_request", return_value=me_response()):
            result = XTool().execute(
                "search_tweets", {"query": "x", "max_results": "²"}, connected_api()
            )
        assert isinstance(result, ActionFailed)
        self.assertNotIn("invalid literal", result.error)

    def test_execute_approved_fails_when_account_changed(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_pending):
            pending = XTool().execute("post_tweet", {"text": "Ship it"}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return {"data": {"id": "999", "name": "Other", "username": "other"}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_execute):
            result = XTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)

    def test_execute_approved_fails_when_target_deleted(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            if "/tweets/9001?" in url:
                return {"data": {"id": "9001", "text": "original", "author_id": "222"},
                        "includes": {"users": [{"id": "222", "username": "someone"}]}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_pending):
            pending = XTool().execute("post_tweet", {"text": "Nice", "in_reply_to_tweet_id": "9001"}, api)
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            if "/tweets/9001?" in url:
                return {"errors": [{"title": "Not Found Error"}]}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_execute):
            result = XTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertIn("not found", result.error.lower())

    def test_execute_approved_rejects_target_binding_mismatch_without_posting(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            if "/tweets/9001?" in url:
                return {"data": {"id": "9001", "text": "original", "author_id": "222"}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_pending):
            pending = XTool().execute(
                "post_tweet", {"text": "Nice", "in_reply_to_tweet_id": "9001"}, api
            )
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)
        target = approved_record.payload["target_tweet"]
        assert isinstance(target, dict)
        target["id"] = "9002"

        calls: list[tuple[str, str]] = []

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append((method, url))
            if "/users/me" in url:
                return me_response()
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_execute):
            result = XTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertIn("target is invalid", result.error)
        self.assertFalse(any(method == "POST" and url.endswith("/tweets") for method, url in calls))

    def test_execute_never_posts_inline(self) -> None:
        api = connected_api()
        calls: list[str] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append(f"{method} {url}")
            if "/users/me" in url:
                return me_response()
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_json_request):
            XTool().execute("post_tweet", {"text": "Hello"}, api)
        self.assertFalse(any(call.startswith("POST") and call.endswith("/tweets") for call in calls))


class XToolDirectMessageTests(unittest.TestCase):
    def test_send_dm_resolves_recipient_and_queues_approval(self) -> None:
        api = connected_api()
        calls: list[tuple[str, str]] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append((method, url))
            if "/users/me" in url:
                return me_response()
            if "/users/by/username/recipient" in url:
                return {"data": {"id": "222", "name": "Recipient", "username": "recipient"}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_json_request):
            pending = XTool().execute(
                "send_dm",
                {"text": "Private hello", "recipient_username": "@recipient"},
                api,
            )
        assert isinstance(pending, ActionPendingApproval)
        self.assertIn("Direct message on X", pending.summary)
        self.assertIn("@recipient", pending.summary)
        self.assertIn("Private hello", pending.summary)
        record = api.approvals.get(pending.approval_id)
        assert record is not None
        self.assertEqual(record.payload["recipient"], {"id": "222", "name": "Recipient", "username": "recipient"})
        self.assertFalse(any(method == "POST" for method, _ in calls))

    def test_send_dm_requires_new_scopes_on_existing_connection(self) -> None:
        api = connected_api()
        stored = api.credentials.load()
        assert stored is not None
        stored["account"]["scopes"] = ["tweet.read", "users.read", "tweet.write", "offline.access"]
        api.credentials.save(stored)

        with patch.object(twitter, "json_request") as request:
            result = XTool().execute(
                "send_dm",
                {"text": "Private hello", "recipient_user_id": "222"},
                api,
            )
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIn("DM access", result.error)
        request.assert_not_called()

    def test_send_dm_validates_recipient_and_message(self) -> None:
        tool = XTool()
        for tool_input in (
            {"text": "x"},
            {"text": "x", "recipient_username": "a", "recipient_user_id": "1"},
            {"text": "x", "recipient_username": "bad handle"},
            {"text": "x", "recipient_user_id": "../users"},
            {"text": "x" * (twitter.MAX_DM_CHARS + 1), "recipient_user_id": "222"},
        ):
            with self.subTest(tool_input=tool_input):
                self.assertIsInstance(tool.execute("send_dm", tool_input, connected_api()), ActionFailed)

    def test_execute_approved_sends_dm_and_reports_event_id(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            if "/users/by/username/recipient" in url:
                return {"data": {"id": "222", "name": "Recipient", "username": "recipient"}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_pending):
            pending = XTool().execute(
                "send_dm",
                {"text": "Private hello", "recipient_username": "recipient"},
                api,
            )
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)
        posted: dict[str, Any] = {}

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            if "/users/222?" in url:
                return {"data": {"id": "222", "name": "Recipient", "username": "recipient"}}
            if "/dm_conversations/with/222/messages" in url:
                posted["method"] = method
                posted["body"] = kwargs["body"]
                return {"data": {"dm_conversation_id": "111-222", "dm_event_id": "333"}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_execute):
            result = XTool().execute_approved(approved_record, api)
        assert isinstance(result, ApprovalExecuted)
        self.assertIn("@recipient", result.message)
        self.assertIn("333", result.message)
        self.assertEqual(posted, {"method": "POST", "body": {"text": "Private hello"}})

    def test_execute_approved_rejects_recipient_handle_change_without_sending(self) -> None:
        api = connected_api()

        def fake_pending(method: str, url: str, **kwargs: Any) -> JSONObject:
            if "/users/me" in url:
                return me_response()
            return {"data": {"id": "222", "name": "Recipient", "username": "recipient"}}

        with patch.object(twitter, "json_request", fake_pending):
            pending = XTool().execute(
                "send_dm",
                {"text": "Private hello", "recipient_user_id": "222"},
                api,
            )
        assert isinstance(pending, ActionPendingApproval)
        approved_record = api.approvals.approve(pending.approval_id)
        calls: list[tuple[str, str]] = []

        def fake_execute(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append((method, url))
            if "/users/me" in url:
                return me_response()
            if "/users/222?" in url:
                return {"data": {"id": "222", "name": "Recipient", "username": "renamed"}}
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_execute):
            result = XTool().execute_approved(approved_record, api)
        assert isinstance(result, ActionFailed)
        self.assertIn("recipient changed", result.error)
        self.assertFalse(any(method == "POST" for method, _ in calls))


class XCredentialFlowTests(unittest.TestCase):
    def test_start_connect_builds_pkce_authorization_url(self) -> None:
        api = connected_api()
        result = XTool().credentials.start_connect({"redirect_uri": "https://host.example/cb"}, api)
        self.assertTrue(result["authorization_url"].startswith("https://x.com/i/oauth2/authorize?"))
        self.assertIn("code_challenge_method=S256", result["authorization_url"])
        self.assertIn("dm.read", result["authorization_url"])
        self.assertIn("dm.write", result["authorization_url"])
        self.assertIn("state=", result["authorization_url"])
        self.assertIn(".", result["state"])

    def test_complete_connect_exchanges_code_and_saves_account(self) -> None:
        api = FakeHostAPI()
        api.config["X_OAUTH_CLIENT_ID"] = "x-client"
        api.config["X_OAUTH_CLIENT_SECRET"] = "x-secret"
        flow = XTool().credentials
        start = flow.start_connect({"redirect_uri": "https://host.example/cb"}, api)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == twitter.X_TOKEN_URL:
                self.assertEqual(kwargs["form"]["grant_type"], "authorization_code")
                self.assertTrue(kwargs["form"]["code_verifier"])
                return {"access_token": "x-access", "refresh_token": "x-refresh", "expires_in": 7200,
                        "scope": "tweet.read users.read tweet.write dm.read dm.write offline.access",
                        "token_type": "bearer"}
            if "/users/me" in url:
                return me_response()
            raise AssertionError(f"unexpected call: {url}")

        with patch.object(twitter, "json_request", fake_json_request):
            result = flow.complete_connect(
                {"code": "auth-code", "state": start["state"], "redirect_uri": "https://host.example/cb"}, api
            )
        self.assertEqual(result["account"]["id"], "111")
        self.assertEqual(result["account"]["label"], "@claw")
        stored = api.credentials.load()
        assert stored is not None
        self.assertEqual(stored["secret"]["refresh_token"], "x-refresh")

    def test_complete_connect_rejects_missing_refresh_token(self) -> None:
        api = FakeHostAPI()
        api.config["X_OAUTH_CLIENT_ID"] = "x-client"
        api.config["X_OAUTH_CLIENT_SECRET"] = "x-secret"
        flow = XTool().credentials
        start = flow.start_connect({"redirect_uri": "https://host.example/cb"}, api)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            return {"access_token": "x-access", "expires_in": 7200,
                    "scope": "tweet.read users.read tweet.write dm.read dm.write offline.access",
                    "token_type": "bearer"}

        with patch.object(twitter, "json_request", fake_json_request):
            with self.assertRaisesRegex(RuntimeError, "no refresh token"):
                flow.complete_connect(
                    {"code": "auth-code", "state": start["state"], "redirect_uri": "https://host.example/cb"}, api
                )
        self.assertIsNone(api.credentials.load())

    def test_complete_connect_rejects_missing_scopes(self) -> None:
        api = FakeHostAPI()
        api.config["X_OAUTH_CLIENT_ID"] = "x-client"
        api.config["X_OAUTH_CLIENT_SECRET"] = "x-secret"
        flow = XTool().credentials
        start = flow.start_connect({"redirect_uri": "https://host.example/cb"}, api)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            return {"access_token": "x-access", "refresh_token": "r", "expires_in": 7200,
                    "scope": "tweet.read users.read", "token_type": "bearer"}

        with patch.object(twitter, "json_request", fake_json_request):
            with self.assertRaisesRegex(RuntimeError, "missing required permissions"):
                flow.complete_connect(
                    {"code": "auth-code", "state": start["state"], "redirect_uri": "https://host.example/cb"}, api
                )
        self.assertIsNone(api.credentials.load())


if __name__ == "__main__":
    unittest.main()
