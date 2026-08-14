"""Unit tests for the Reddit personal-use script tool (all provider calls mocked)."""

from __future__ import annotations

import unittest
import urllib.parse
from typing import Any
from unittest.mock import patch

from host.runtime.tools import tools_host
from host.tools import reddit
from host.tools.json_types import JSONObject
from host.tools.reddit import RedditTool
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.web import ProviderWarning, WebRequestError

from test_tools import FakeHostAPI


def connected_api() -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["REDDIT_CLIENT_ID"] = "reddit-client"
    api.config["REDDIT_CLIENT_SECRET"] = "reddit-secret"
    api.config["REDDIT_USERNAME"] = "kern_dev"
    api.config["REDDIT_PASSWORD"] = "reddit-password"
    return api


def me_response() -> JSONObject:
    return {
        "id": "abc123",
        "name": "kern_dev",
        "link_karma": 12,
        "comment_karma": 34,
        "created_utc": 1_700_000_000.0,
        "is_gold": False,
        "is_mod": True,
    }


def listing_response(count: int = 2, *, after: object = "t3_next123") -> JSONObject:
    return {
        "data": {
            "after": after,
            "children": [
                {
                    "kind": "t3",
                    "data": {
                        "id": f"p{index}",
                        "name": f"t3_p{index}",
                        "title": f"Post {index}",
                        "selftext": f"Body {index}",
                        "author": "author",
                        "subreddit": "kern",
                        "score": index,
                        "num_comments": index + 1,
                        "created_utc": 1_700_000_000.0 + index,
                        "permalink": f"/r/kern/comments/p{index}/post/",
                        "url": f"https://example.com/{index}",
                        "over_18": False,
                    },
                }
                for index in range(count)
            ],
        }
    }


def subreddit_response(name: str = "kern") -> JSONObject:
    return {"data": {"display_name": name}}


def info_response(parent_id: str = "t3_p0") -> JSONObject:
    return {"data": {"children": [{"kind": parent_id[:2], "data": {"name": parent_id}}]}}


def write_response(fullname: str, url: str) -> JSONObject:
    return {"json": {"errors": [], "data": {"name": fullname, "url": url}}}


class RedditToolTestCase(unittest.TestCase):
    def setUp(self) -> None:
        token = patch.object(reddit, "_script_access_token", return_value="reddit-access")
        token.start()
        self.addCleanup(token.stop)


class RedditToolReadTests(RedditToolTestCase):
    def test_manifest_has_minimum_read_and_approval_gated_write_surface(self) -> None:
        tool = RedditTool()
        self.assertEqual(tool.manifest.connection, "enable_only")
        self.assertIsNone(tool.credentials)
        self.assertEqual(
            [spec.id for spec in tool.manifest.actions],
            [
                "get_profile",
                "get_home_feed",
                "get_subreddit_posts",
                "search_posts",
                "read_post",
                "create_post",
                "create_comment",
            ],
        )
        self.assertEqual(
            [requirement.key for requirement in tool.manifest.config],
            [
                "REDDIT_CLIENT_ID",
                "REDDIT_CLIENT_SECRET",
                "REDDIT_USERNAME",
                "REDDIT_PASSWORD",
            ],
        )
        approvals = {spec.id: spec.approval for spec in tool.manifest.actions}
        self.assertEqual(approvals["create_post"], "operator")
        self.assertEqual(approvals["create_comment"], "operator")
        self.assertTrue(all(approvals[action] == "direct" for action in approvals if action.startswith(("get_", "search_", "read_"))))
        self.assertIn("explicit approval", tool.manifest.description.lower())
        self.assertIn("not supported", tool.manifest.agent_notes)

    def test_get_profile_returns_bounded_identity(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen.update(method=method, url=url, headers=kwargs["headers"])
            return me_response()

        with patch.object(reddit, "json_request", fake_json_request):
            result = RedditTool().execute("get_profile", {}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["profile"]["username"], "kern_dev")  # type: ignore[index]
        self.assertEqual(result.result["profile"]["comment_karma"], 34)  # type: ignore[index]
        self.assertEqual(seen["url"], "https://oauth.reddit.com/api/v1/me")
        self.assertEqual(seen["headers"]["authorization"], "Bearer reddit-access")
        self.assertEqual(
            seen["headers"]["user-agent"],
            "script:kern:client (by /u/kern_dev)",
        )

    def test_home_feed_builds_listing_and_returns_cursor(self) -> None:
        seen: dict[str, str] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            return listing_response(2)

        with patch.object(reddit, "json_request", fake_json_request):
            result = RedditTool().execute(
                "get_home_feed",
                {"sort": "top", "time_filter": "week", "limit": "2", "after": "t3_old123"},
                connected_api(),
            )
        assert isinstance(result, ActionExecuted)
        parsed = urllib.parse.urlsplit(seen["url"])
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, "/top")
        self.assertEqual(params["t"], ["week"])
        self.assertEqual(params["after"], ["t3_old123"])
        self.assertEqual(len(result.result["posts"]), 2)  # type: ignore[arg-type]
        self.assertEqual(result.result["next_cursor"], "t3_next123")

    def test_subreddit_listing_validates_path_component(self) -> None:
        seen: dict[str, str] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            return listing_response(1)

        with patch.object(reddit, "json_request", fake_json_request):
            result = RedditTool().execute(
                "get_subreddit_posts", {"subreddit": "r/Python", "sort": "new"}, connected_api()
            )
        assert isinstance(result, ActionExecuted)
        self.assertIn("/r/Python/new?", seen["url"])
        self.assertEqual(result.result["subreddit"], "Python")

        for bad in ("../api", "python news", "r/", "a" * 22):
            with self.subTest(subreddit=bad), patch.object(reddit, "json_request") as request:
                result = RedditTool().execute("get_subreddit_posts", {"subreddit": bad}, connected_api())
                self.assertIsInstance(result, ActionFailed)
                request.assert_not_called()

    def test_search_uses_official_api_and_parameter_guard(self) -> None:
        seen: dict[str, str] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            return listing_response(1)

        with patch.object(reddit, "json_request", fake_json_request):
            result = RedditTool().execute(
                "search_posts",
                {"query": "oauth integrations", "subreddit": "Kern", "sort": "top", "time_filter": "month"},
                connected_api(),
            )
        assert isinstance(result, ActionExecuted)
        parsed = urllib.parse.urlsplit(seen["url"])
        params = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "oauth.reddit.com")
        self.assertEqual(parsed.path, "/r/Kern/search")
        self.assertEqual(params["q"], ["oauth integrations"])
        self.assertEqual(params["restrict_sr"], ["true"])
        self.assertEqual(params["type"], ["link"])

    def test_search_rejects_secret_shaped_query_before_provider_call(self) -> None:
        with patch.object(reddit, "json_request") as request:
            result = RedditTool().execute(
                "search_posts", {"query": "ghp_abcdefghijklmnopqrstuvwxyz1234567890"}, connected_api()
            )
        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()

    def test_listing_caps_provider_results_and_drops_bad_cursor(self) -> None:
        with patch.object(reddit, "json_request", return_value=listing_response(40, after="../../token")):
            result = RedditTool().execute("get_home_feed", {"limit": "25"}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(result.result["posts"]), 25)  # type: ignore[arg-type]
        self.assertEqual(result.result["next_cursor"], "")

    def test_read_post_maps_post_and_nested_comments(self) -> None:
        post_listing = listing_response(1, after=None)
        comment_listing: JSONObject = {
            "data": {
                "children": [
                    {
                        "kind": "t1",
                        "data": {
                            "id": "c1",
                            "parent_id": "t3_p0",
                            "author": "commenter",
                            "body": "Top level",
                            "score": 9,
                            "created_utc": 1_700_000_100.0,
                            "permalink": "/r/kern/comments/p0/post/c1/",
                            "replies": {
                                "data": {
                                    "children": [
                                        {
                                            "kind": "t1",
                                            "data": {
                                                "id": "c2",
                                                "parent_id": "t1_c1",
                                                "author": "reply",
                                                "body": "Nested",
                                                "score": 2,
                                            },
                                        }
                                    ]
                                }
                            },
                        },
                    },
                    {"kind": "more", "data": {"children": ["c3"]}},
                ]
            }
        }
        seen: dict[str, str] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["url"] = url
            return {"items": [post_listing, comment_listing]}

        with patch.object(reddit, "json_request", fake_json_request):
            result = RedditTool().execute(
                "read_post", {"post_id": "t3_p0", "comment_sort": "top", "comment_limit": "2"}, connected_api()
            )
        assert isinstance(result, ActionExecuted)
        self.assertIn("/comments/p0?", seen["url"])
        self.assertEqual(result.result["post"]["id"], "p0")  # type: ignore[index]
        comments = result.result["comments"]
        assert isinstance(comments, list)
        self.assertEqual([item["body"] for item in comments], ["Top level", "Nested"])
        self.assertEqual(comments[1]["depth"], 1)

    def test_invalid_input_never_calls_reddit(self) -> None:
        cases = (
            ("get_profile", {"extra": "x"}),
            ("get_home_feed", {"sort": "controversial"}),
            ("get_home_feed", {"sort": "new", "time_filter": "day"}),
            ("get_home_feed", {"after": "not-a-fullname"}),
            ("search_posts", {"query": " "}),
            ("read_post", {"post_id": "../api"}),
            ("read_post", {"post_id": "abc", "comment_limit": "51"}),
        )
        for action, tool_input in cases:
            with self.subTest(action=action, tool_input=tool_input), patch.object(reddit, "json_request") as request:
                result = RedditTool().execute(action, tool_input, connected_api())
                self.assertIsInstance(result, ActionFailed)
                request.assert_not_called()

    def test_every_result_satisfies_declared_output_schema(self) -> None:
        cases = (
            ("get_profile", {}, me_response()),
            ("get_home_feed", {}, listing_response()),
            ("get_subreddit_posts", {"subreddit": "kern"}, listing_response()),
            ("search_posts", {"query": "kern"}, listing_response()),
            ("read_post", {"post_id": "abc"}, {"items": [listing_response(), {"data": {"children": []}}]}),
        )
        for action, tool_input, provider_response in cases:
            with self.subTest(action=action), patch.object(reddit, "json_request", return_value=provider_response):
                result = RedditTool().execute(action, tool_input, connected_api())
            assert isinstance(result, ActionExecuted)
            spec = RedditTool().manifest.action(action)
            assert spec is not None
            self.assertEqual(tools_host.validate_against_schema(result.result, spec.output_schema, path="result"), "")

    def test_401_names_script_credentials_and_403_names_api_access(self) -> None:
        with (
            patch.object(reddit, "json_request", side_effect=WebRequestError("failed", status=401)),
            self.assertRaises(ProviderWarning) as unauthorized,
        ):
            RedditTool().execute("get_home_feed", {}, connected_api())
        self.assertIn("script credentials", str(unauthorized.exception))

        with (
            patch.object(reddit, "json_request", side_effect=WebRequestError("failed", status=403)),
            self.assertRaises(ProviderWarning) as caught,
        ):
            RedditTool().execute("get_home_feed", {}, connected_api())
        self.assertIn("approved", str(caught.exception))


class RedditToolWriteTests(RedditToolTestCase):
    def test_text_post_queues_exact_approval_then_executes(self) -> None:
        api = connected_api()
        tool = RedditTool()
        calls: list[tuple[str, str, dict[str, Any]]] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append((method, url, kwargs))
            if url.endswith("/api/v1/me"):
                return me_response()
            if url.endswith("/r/kern/about"):
                return subreddit_response()
            if url.endswith("/api/submit"):
                return write_response(
                    "t3_created1", "https://www.reddit.com/r/kern/comments/created1/post/"
                )
            raise AssertionError(f"unexpected call: {method} {url}")

        proposal = {
            "subreddit": "r/kern",
            "title": "A guarded write",
            "kind": "self",
            "text": "Nothing publishes before approval.",
        }
        with patch.object(reddit, "json_request", fake_json_request):
            pending = tool.execute("create_post", proposal, api)

        assert isinstance(pending, ActionPendingApproval)
        self.assertFalse(any(method == "POST" for method, _, _ in calls))
        self.assertFalse(any(url.endswith("/r/kern/about") for _, url, _ in calls))
        record = api.approvals.get(pending.approval_id)
        assert record is not None
        self.assertEqual(
            record.payload["proposal"],
            {
                "subreddit": "kern",
                "title": "A guarded write",
                "kind": "self",
                "text": "Nothing publishes before approval.",
            },
        )
        self.assertEqual(record.payload["reddit_account"]["id"], "abc123")  # type: ignore[index]

        calls.clear()
        with patch.object(reddit, "json_request", fake_json_request):
            executed = tool.execute_approved(api.approvals.approve(pending.approval_id), api)

        assert isinstance(executed, ApprovalExecuted)
        submit_calls = [call for call in calls if call[1].endswith("/api/submit")]
        self.assertEqual(len(submit_calls), 1)
        self.assertEqual(submit_calls[0][2]["form"]["sr"], "kern")
        self.assertEqual(submit_calls[0][2]["form"]["text"], proposal["text"])
        self.assertNotIn("url", submit_calls[0][2]["form"])
        self.assertIn("t3_created1", executed.message)

    def test_link_post_requires_public_https_url(self) -> None:
        invalid = (
            {"kind": "link", "url": "http://example.com"},
            {"kind": "link", "url": "https://127.0.0.1/private"},
            {"kind": "link", "url": "https://user:password@example.com"},
            {"kind": "link", "url": "https://example.com", "text": "ambiguous"},
        )
        for fields in invalid:
            tool_input = {"subreddit": "kern", "title": "Title", **fields}
            with self.subTest(tool_input=tool_input), patch.object(reddit, "json_request") as request:
                result = RedditTool().execute("create_post", tool_input, connected_api())
            self.assertIsInstance(result, ActionFailed)
            request.assert_not_called()

    def test_comment_checks_exact_parent_only_after_approval(self) -> None:
        api = connected_api()
        tool = RedditTool()
        posted_forms: list[dict[str, str]] = []
        parent_lookups = 0

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            nonlocal parent_lookups
            if url.endswith("/api/v1/me"):
                return me_response()
            if "/api/info?" in url:
                parent_lookups += 1
                self.assertIn("id=t1_comment1", url)
                return info_response("t1_comment1")
            if url.endswith("/api/comment"):
                posted_forms.append(kwargs["form"])
                return write_response(
                    "t1_reply1", "https://www.reddit.com/r/kern/comments/p0/post/reply1/"
                )
            raise AssertionError(f"unexpected call: {method} {url}")

        with patch.object(reddit, "json_request", fake_json_request):
            pending = tool.execute(
                "create_comment", {"parent_id": "t1_comment1", "text": "Approved reply"}, api
            )
        assert isinstance(pending, ActionPendingApproval)
        self.assertEqual(posted_forms, [])
        self.assertEqual(parent_lookups, 0)

        with patch.object(reddit, "json_request", fake_json_request):
            executed = tool.execute_approved(api.approvals.approve(pending.approval_id), api)
        assert isinstance(executed, ApprovalExecuted)
        self.assertEqual(parent_lookups, 1)
        self.assertEqual(posted_forms, [{
            "api_type": "json",
            "thing_id": "t1_comment1",
            "text": "Approved reply",
            "raw_json": "1",
        }])

    def test_approved_write_fails_closed_on_account_or_payload_change(self) -> None:
        api = connected_api()
        tool = RedditTool()

        def proposal_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url.endswith("/api/v1/me"):
                return me_response()
            return subreddit_response()

        with patch.object(reddit, "json_request", proposal_request):
            pending = tool.execute(
                "create_post",
                {"subreddit": "kern", "title": "Title", "kind": "self", "text": "Body"},
                api,
            )
        assert isinstance(pending, ActionPendingApproval)
        approved = api.approvals.approve(pending.approval_id)
        approved.payload["proposal"]["extra"] = "tampered"  # type: ignore[index]
        with patch.object(reddit, "json_request") as request:
            invalid = tool.execute_approved(approved, api)
        assert isinstance(invalid, ActionFailed)
        self.assertIn("payload is invalid", invalid.error)
        request.assert_not_called()

    def test_reddit_json_errors_are_redacted(self) -> None:
        response: JSONObject = {
            "json": {"errors": [["SOME_PRIVATE_DETAIL", "provider secret body", "text"]]}
        }
        with self.assertRaisesRegex(RuntimeError, "Check the account's eligibility") as caught:
            reddit._write_result(response, "comment submission", "t1")
        self.assertNotIn("provider secret body", str(caught.exception))

    def test_write_summary_and_nested_comment_response_are_bounded(self) -> None:
        proposal: JSONObject = {
            "subreddit": "kern",
            "title": "🧪" * 300,
            "kind": "self",
            "text": "🚀" * 10_000,
        }
        summary = reddit._write_summary("create_post", proposal, "u/kern_dev")
        self.assertLessEqual(len(summary.encode("utf-8")), reddit.SUMMARY_MAX_BYTES)
        nested: JSONObject = {
            "json": {
                "errors": [],
                "data": {
                    "things": [
                        {
                            "kind": "t1",
                            "data": {
                                "name": "t1_reply1",
                                "permalink": "/r/kern/comments/p0/post/reply1/",
                            },
                        }
                    ]
                },
            }
        }
        self.assertEqual(
            reddit._write_result(nested, "comment submission", "t1"),
            (
                "t1_reply1",
                "https://www.reddit.com/r/kern/comments/p0/post/reply1/",
            ),
        )


class RedditScriptCredentialTests(unittest.TestCase):
    def test_password_grant_sends_all_credentials_only_to_token_endpoint(self) -> None:
        api = connected_api()
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen.update(method=method, url=url, headers=kwargs["headers"], form=kwargs["form"])
            return {"access_token": "reddit-access", "expires_in": 3600, "scope": "*"}

        with patch.object(reddit, "json_request", fake_json_request):
            token = reddit._script_access_token(api)
        self.assertEqual(token, "reddit-access")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], reddit.REDDIT_TOKEN_URL)
        self.assertEqual(
            seen["form"],
            {
                "grant_type": "password",
                "username": "kern_dev",
                "password": "reddit-password",
            },
        )
        self.assertTrue(seen["headers"]["authorization"].startswith("Basic "))
        self.assertEqual(
            seen["headers"]["user-agent"],
            "script:kern:client (by /u/kern_dev)",
        )

    def test_token_is_not_persisted(self) -> None:
        api = connected_api()
        with patch.object(
            reddit,
            "json_request",
            return_value={"access_token": "reddit-access", "expires_in": 3600, "scope": "*"},
        ):
            reddit._script_access_token(api)
        self.assertIsNone(api.credentials.load())

    def test_provider_rejection_is_redacted_and_actionable(self) -> None:
        api = connected_api()
        with (
            patch.object(
                reddit,
                "json_request",
                side_effect=WebRequestError(
                    "failed", status=401, body=b'{"error":"bad secret detail"}'
                ),
            ),
            self.assertRaises(ProviderWarning) as caught,
        ):
            reddit._script_access_token(api)
        message = str(caught.exception)
        self.assertIn("personal-use script credentials", message)
        self.assertNotIn("bad secret detail", message)

    def test_json_error_and_missing_token_fail_closed(self) -> None:
        api = connected_api()
        for response, message in (
            ({"error": "invalid_grant"}, "rejected the personal-use script credentials"),
            ({"scope": "*"}, "returned no access token"),
        ):
            with self.subTest(response=response), patch.object(
                reddit, "json_request", return_value=response
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    reddit._script_access_token(api)

    def test_account_must_match_configured_username(self) -> None:
        api = connected_api()
        with patch.object(reddit, "_fetch_me", return_value={**me_response(), "name": "other"}):
            with self.assertRaisesRegex(RuntimeError, "different account"):
                reddit._script_identity("reddit-access", api)

    def test_bad_config_fails_before_any_request(self) -> None:
        cases = (
            ("REDDIT_CLIENT_ID", "", "REDDIT_CLIENT_ID"),
            ("REDDIT_CLIENT_SECRET", "", "REDDIT_CLIENT_SECRET"),
            ("REDDIT_USERNAME", "u/kern_dev", "REDDIT_USERNAME"),
            ("REDDIT_PASSWORD", "", "REDDIT_PASSWORD"),
        )
        for key, value, message in cases:
            api = connected_api()
            api.config[key] = value
            with self.subTest(key=key), patch.object(reddit, "json_request") as request:
                result = RedditTool().execute("get_home_feed", {}, api)
            assert isinstance(result, ActionFailed)
            self.assertIn(message, result.error)
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
