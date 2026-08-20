"""Unit tests for the X (Twitter) tool package (all third-party calls mocked)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from host.tools.json_types import JSONObject
from host.tools.results import ActionExecuted, ActionFailed
from host.tools import twitter
from host.tools.twitter import XTool
from host.tools.shared.web import ProviderWarning, WebRequestError
from host.runtime.tools import tools_host

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
                "scopes": ["tweet.read", "users.read", "offline.access"],
            },
            "secret": {
                "access_token": "x-access",
                "expires_at": expires_at,
                "refresh_token": "x-refresh-1",
                "scope": "tweet.read users.read offline.access",
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
                "lookup_user",
            ],
        )
        # The operator card says what the tool does, not what it cannot do.
        self.assertIn("open, review, and send yourself", tool.manifest.description)
        self.assertNotIn("not available", tool.manifest.description)
        # One agent-facing field per tool carries all three link forms, and the
        # operator payload carries none of them.
        self.assertIn("x.com/intent/tweet?text=", tool.manifest.agent_notes)
        self.assertIn("in_reply_to=", tool.manifest.agent_notes)
        self.assertIn("x.com/messages/compose", tool.manifest.agent_notes)
        self.assertIn("lookup_user", tool.manifest.agent_notes)
        self.assertNotIn("x.com/", tool.manifest.description)
        self.assertNotIn("tweet.write", twitter.X_OAUTH_SCOPES)

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

        with (
            patch.object(twitter, "json_request", fake_json_request),
            self.assertRaises(ProviderWarning) as caught,
        ):
            XTool().execute("get_trends", {}, connected_api())
        self.assertIn("X_BEARER_TOKEN", str(caught.exception))

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

    def test_missing_scope_requires_reconnect(self) -> None:
        api = connected_api()
        assert api.credentials.record is not None
        api.credentials.record["account"]["scopes"] = ["tweet.read", "offline.access"]
        result = XTool().execute("search_tweets", {"query": "x"}, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIsNone(api.credentials.load())

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
                        "scope": "tweet.read users.read offline.access",
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
                "scope": "tweet.read offline.access",
                "token_type": "bearer",
            }

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("search_tweets", {"query": "x"}, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIsNone(api.credentials.load())


class XToolUserLookupTests(unittest.TestCase):
    """lookup_user exists so an agent can turn a handle into the numeric id a
    direct-message link needs. It reads; it never sends."""

    def test_profile_lookup_data_summary_discloses_public_counts(self) -> None:
        first_card = twitter.MANIFEST.data_summary.cards[0]
        profile_point = next(
            point for point in first_card.points if point.label == "Profile lookups"
        )
        self.assertIn("follower", profile_point.text)
        self.assertIn("following", profile_point.text)
        self.assertIn("post counts", profile_point.text)

    def test_lookup_resolves_a_handle_to_the_id_a_dm_link_needs(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append((method, url))
            return {"data": {"id": "222", "name": "Recipient", "username": "recipient"}}

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("lookup_user", {"username": "@recipient"}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["user"], {"id": "222", "username": "recipient", "name": "Recipient"})
        self.assertEqual([method for method, _ in calls], ["GET"])
        self.assertIn("/users/by/username/recipient", calls[0][1])

    def test_lookup_returns_public_counts_when_the_provider_sends_them(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            self.assertIn("public_metrics", url)
            return {
                "data": {
                    "id": "222",
                    "name": "Recipient",
                    "username": "recipient",
                    "public_metrics": {
                        "followers_count": 57,
                        "following_count": 61,
                        "tweet_count": 340,
                        "listed_count": "not-an-int",
                    },
                }
            }

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("lookup_user", {"username": "recipient"}, connected_api())
        assert isinstance(result, ActionExecuted)
        user = result.result["user"]
        self.assertEqual(user["followers_count"], 57)
        self.assertEqual(user["following_count"], 61)
        self.assertEqual(user["tweet_count"], 340)
        self.assertNotIn("listed_count", user)

    def test_lookup_by_id_verifies_the_provider_echoed_the_same_id(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            return {"data": {"id": "999", "name": "Other", "username": "other"}}

        with patch.object(twitter, "json_request", fake_json_request):
            result = XTool().execute("lookup_user", {"user_id": "222"}, connected_api())
        self.assertIsInstance(result, ActionFailed)

    def test_lookup_rejects_bad_selectors_before_calling_the_provider(self) -> None:
        tool = XTool()
        for tool_input in (
            {},
            {"username": "a", "user_id": "1"},
            {"username": "bad handle"},
            {"user_id": "../users"},
            {"username": "recipient", "text": "hello"},
        ):
            with self.subTest(tool_input=tool_input), patch.object(twitter, "json_request") as request:
                self.assertIsInstance(tool.execute("lookup_user", tool_input, connected_api()), ActionFailed)
                request.assert_not_called()

    def test_every_action_result_satisfies_its_declared_output_schema(self) -> None:
        # tools_host validates ActionExecuted.result against output_schema and
        # turns a mismatch into a failure, so a result that omits the envelope
        # never reaches the agent. Calling XTool().execute() directly skips that
        # check, which is why this asserts it with the host's own validator.
        provider: dict[str, Any] = {
            "data": {"id": "222", "name": "Recipient", "username": "recipient"},
            "trends": [{"trend_name": "Kern"}],
        }
        cases = (
            ("search_tweets", {"query": "x"}),
            ("read_tweet", {"tweet_id": "9001"}),
            ("user_tweets", {"user_id": "222"}),
            ("get_trends", {}),
            ("get_personalized_trends", {}),
            ("lookup_user", {"username": "recipient"}),
        )
        for action_id, tool_input in cases:
            with self.subTest(action=action_id):
                with patch.object(twitter, "json_request", return_value=provider):
                    result = XTool().execute(action_id, tool_input, connected_api())
                assert isinstance(result, ActionExecuted)
                spec = XTool().manifest.action(action_id)
                assert spec is not None
                self.assertEqual(
                    tools_host.validate_against_schema(result.result, spec.output_schema, path="result"),
                    "",
                )

    def test_the_tool_no_longer_writes_anything(self) -> None:
        # Posting and DMs are prepared as intent links the operator opens, so
        # no action queues an approval and no scope permits a write.
        self.assertTrue(all(spec.approval == "direct" for spec in XTool().manifest.actions))
        self.assertNotIn("send_dm", [spec.id for spec in XTool().manifest.actions])
        for scope in twitter.X_OAUTH_SCOPES:
            self.assertNotIn("write", scope)
        self.assertNotIn("dm.read", twitter.X_OAUTH_SCOPES)
        with patch.object(twitter, "json_request") as request:
            result = XTool().execute("send_dm", {"text": "hi", "recipient_user_id": "222"}, connected_api())
            request.assert_not_called()
        assert isinstance(result, ActionFailed)
        self.assertIn("Unsupported X action", result.error)


class XCredentialFlowTests(unittest.TestCase):
    def test_start_connect_builds_pkce_authorization_url(self) -> None:
        api = connected_api()
        result = XTool().credentials.start_connect({"redirect_uri": "https://host.example/cb"}, api)
        self.assertTrue(result["authorization_url"].startswith("https://x.com/i/oauth2/authorize?"))
        self.assertIn("code_challenge_method=S256", result["authorization_url"])
        self.assertIn("users.read", result["authorization_url"])
        self.assertNotIn("dm.", result["authorization_url"])
        self.assertNotIn("tweet.write", result["authorization_url"])
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
                        "scope": "tweet.read users.read offline.access",
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
                    "scope": "tweet.read users.read offline.access",
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
