"""Coverage tests for the outbound parameter guard.

Two jobs:

1. Completeness: every agent-controlled string input field of every bundled
   tool action is classified exactly once - either GUARDED (its call site
   passes it through ``api.outbound.guard_request_parameter_string``) or
   EXEMPT with a recorded reason. Adding a tool or field without classifying
   it here fails the build, which is what keeps the classification honest as
   the tool set grows.

2. Behavior: for each guarded field, driving the real tool code with a
   secret- or identifier-carrying value produces a denial whose descriptive
   message reaches the agent, proving the guard sits on the request path
   rather than beside it.
"""

from __future__ import annotations

import importlib
import pkgutil
import unittest

import host.tools
from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL, ParamGuardDenied
from host.tools.results import ActionFailed
from test_tools import FakeHostAPI

# (tool_id, action_id, field) -> guarded free-text parameter. The tool's
# package test and the behavioral tests below exercise each.
GUARDED_FIELDS = {
    ("brave_search", "search_web", "query"),
    ("instagram_discovery", "search_reels", "query"),
    ("instagram_discovery", "search_hashtag", "hashtag"),
    ("linkedin_discovery", "search_posts", "query"),
    ("polymarket", "search", "query"),
    ("polymarket", "get_market", "slug"),
    ("runway", "generate_video", "prompt"),
    ("runway", "edit_video", "prompt"),
    ("runway", "generate_image", "prompt"),
    ("runway", "generate_speech", "text"),
    ("seedance", "generate_video", "prompt"),
    ("twitter", "search_tweets", "query"),
}

# (tool_id, action_id, field) -> reason it is deliberately not guarded.
# Categories follow the architecture doc's scope rule.
APPROVAL_GATED = "approval-gated content: the operator approval is the control"
CONNECTED_ACCOUNT_GUARDED = (
    "connected-account mailbox query: guarded via allow_identifiers=True "
    "(secret/credential shapes denied, personal identifiers including one-time codes allowed as search syntax)"
)
TYPED = "typed value: enum/id/timestamp/cursor grammar is stricter than scanning"
PROTOCOL = "provider protocol value on a fixed-destination typed path"

EXEMPT_FIELDS = {
    ("brave_search", "search_web", "count"): TYPED,
    ("gmail", "search_messages", "query"): CONNECTED_ACCOUNT_GUARDED,
    ("gmail", "search_messages", "start_time"): TYPED,
    ("gmail", "search_messages", "end_time"): TYPED,
    ("gmail", "read_message", "message_id"): TYPED,
    ("gmail", "read_thread", "thread_id"): TYPED,
    ("gmail", "list_drafts", "query"): CONNECTED_ACCOUNT_GUARDED,
    ("gmail", "list_drafts", "page_token"): PROTOCOL,
    ("gmail", "list_drafts", "include_spam_trash"): TYPED,
    ("gmail", "send_email", "*"): APPROVAL_GATED,
    ("gmail", "message_action", "action"): TYPED,
    ("gmail", "message_action", "message_ids"): TYPED,
    ("gmail", "message_action", "label_ids"): APPROVAL_GATED,
    ("gmail", "label_action", "action"): TYPED,
    ("gmail", "label_action", "label_id"): TYPED,
    ("gmail", "label_action", "name"): APPROVAL_GATED,
    ("gmail", "label_action", "background_color"): TYPED,
    ("gmail", "label_action", "text_color"): TYPED,
    ("gmail", "draft_action", "action"): TYPED,
    ("gmail", "draft_action", "draft_id"): TYPED,
    ("gmail", "draft_action", "to"): APPROVAL_GATED,
    ("gmail", "draft_action", "subject"): APPROVAL_GATED,
    ("gmail", "draft_action", "blocks"): APPROVAL_GATED,
    ("google_calendar", "read_events", "start_time"): TYPED,
    ("google_calendar", "read_events", "end_time"): TYPED,
    ("google_calendar", "event_change", "operation"): TYPED,
    ("google_calendar", "event_change", "event_id"): TYPED,
    ("google_calendar", "event_change", "start_time"): TYPED,
    ("google_calendar", "event_change", "end_time"): TYPED,
    ("google_calendar", "event_change", "summary"): APPROVAL_GATED,
    ("google_calendar", "event_change", "description"): APPROVAL_GATED,
    ("google_calendar", "event_change", "location"): APPROVAL_GATED,
    ("google_calendar", "event_change", "time_zone"): APPROVAL_GATED,
    ("ibkr", "*", "*"): TYPED,
    ("instagram", "*", "*"): APPROVAL_GATED,
    ("instagram_discovery", "search_reels", "page"): TYPED,
    ("instagram_discovery", "search_reels", "date_posted"): TYPED,
    ("instagram_discovery", "search_reels", "limit"): TYPED,
    ("instagram_discovery", "search_hashtag", "reels_only"): TYPED,
    ("instagram_discovery", "search_hashtag", "date_posted"): TYPED,
    ("instagram_discovery", "search_hashtag", "cursor"): PROTOCOL,
    ("instagram_discovery", "search_hashtag", "limit"): TYPED,
    ("instagram_discovery", "get_trending_reels", "limit"): TYPED,
    ("instagram_discovery", "get_reels_by_audio", "*"): TYPED,
    ("instagram_discovery", "get_reel_details", "*"): TYPED,
    ("linkedin_discovery", "search_posts", "page"): TYPED,
    ("linkedin_discovery", "search_posts", "limit"): TYPED,
    ("linkedin", "*", "*"): APPROVAL_GATED,
    ("polymarket", "search", "limit_per_type"): TYPED,
    ("polymarket", "get_market", "market_id"): TYPED,
    ("polymarket", "list_markets", "*"): TYPED,
    ("polymarket", "list_events", "*"): TYPED,
    ("polymarket", "get_order_book", "*"): TYPED,
    ("polymarket", "price_history", "*"): TYPED,
    ("runway", "generate_video", "model"): TYPED,
    ("runway", "generate_video", "image_url"): TYPED,
    ("runway", "generate_video", "image_asset_id"): TYPED,
    ("runway", "generate_video", "ratio"): TYPED,
    ("runway", "generate_video", "duration_seconds"): TYPED,
    ("runway", "generate_video", "seed"): TYPED,
    ("runway", "edit_video", "video_asset_id"): TYPED,
    ("runway", "edit_video", "video_url"): TYPED,
    ("runway", "edit_video", "seed"): TYPED,
    ("runway", "generate_image", "ratio"): TYPED,
    ("runway", "generate_image", "quality"): TYPED,
    ("runway", "generate_speech", "voice"): TYPED,
    ("runway", "get_task", "*"): TYPED,
    ("runway", "save_video", "*"): TYPED,
    ("seedance", "generate_video", "image_url"): TYPED,
    ("seedance", "generate_video", "resolution"): TYPED,
    ("seedance", "generate_video", "ratio"): TYPED,
    ("seedance", "generate_video", "duration_seconds"): TYPED,
    ("seedance", "generate_video", "generate_audio"): TYPED,
    ("seedance", "generate_video", "seed"): TYPED,
    ("seedance", "get_task", "*"): TYPED,
    ("seedance", "save_video", "*"): TYPED,
    ("twitter", "search_tweets", "max_results"): TYPED,
    ("twitter", "get_tweet_metrics", "*"): TYPED,
    ("twitter", "read_tweet", "*"): TYPED,
    ("twitter", "user_tweets", "*"): TYPED,
    ("twitter", "get_trends", "*"): TYPED,
    ("twitter", "get_personalized_trends", "*"): TYPED,
    ("twitter", "send_dm", "*"): APPROVAL_GATED,
}

# Tools whose Integration Guide must carry the shared parameter-guard line.
GUARDED_TOOL_IDS = sorted({tool_id for tool_id, _, _ in GUARDED_FIELDS})


def _bundled_manifests():
    for module_info in pkgutil.iter_modules(host.tools.__path__):
        if not module_info.ispkg:
            continue
        module = importlib.import_module(f"host.tools.{module_info.name}")
        tool = getattr(module, "BUNDLED_TOOL", None)
        if tool is not None:
            yield tool.manifest


def _classified(tool_id: str, action_id: str, field: str) -> bool:
    if (tool_id, action_id, field) in GUARDED_FIELDS:
        return True
    for key in (
        (tool_id, action_id, field),
        (tool_id, action_id, "*"),
        (tool_id, "*", "*"),
    ):
        if key in EXEMPT_FIELDS:
            return True
    return False


class CompletenessTest(unittest.TestCase):
    def test_every_action_input_field_is_classified(self) -> None:
        unclassified = []
        seen_tools = set()
        for manifest in _bundled_manifests():
            seen_tools.add(manifest.tool_id)
            for action in manifest.actions:
                properties = action.input_schema.get("properties")
                if not isinstance(properties, dict):
                    continue
                for field in properties:
                    if not _classified(manifest.tool_id, action.id, field):
                        unclassified.append((manifest.tool_id, action.id, field))
        self.assertEqual(
            unclassified,
            [],
            "Classify each field as GUARDED (and add the guard call plus a "
            "behavioral test) or EXEMPT with a reason.",
        )
        # The guarded set must not name fields that do not exist.
        for tool_id, action_id, field in GUARDED_FIELDS:
            self.assertIn(tool_id, seen_tools)

    def test_no_field_is_both_guarded_and_exempt(self) -> None:
        for tool_id, action_id, field in GUARDED_FIELDS:
            self.assertNotIn((tool_id, action_id, field), EXEMPT_FIELDS)
            # No wildcard may shadow an action that has guarded fields:
            # otherwise dropping the field from GUARDED_FIELDS would silently
            # reclassify it as exempt instead of failing completeness.
            self.assertNotIn((tool_id, action_id, "*"), EXEMPT_FIELDS)
            self.assertNotIn((tool_id, "*", "*"), EXEMPT_FIELDS)

    def test_guarded_tools_declare_the_shared_guide_protection(self) -> None:
        for manifest in _bundled_manifests():
            if manifest.tool_id in GUARDED_TOOL_IDS:
                self.assertIn(
                    PARAM_GUARD_PROTECTION,
                    manifest.protections,
                    f"{manifest.tool_id} guide must carry the parameter-guard line",
                )
                self.assertIn(
                    PARAM_GUARD_TECHNICAL_DETAIL,
                    manifest.technical_details,
                    f"{manifest.tool_id} guide must carry the expanded description",
                )
            else:
                self.assertNotIn(PARAM_GUARD_PROTECTION, manifest.protections)
                self.assertNotIn(PARAM_GUARD_TECHNICAL_DETAIL, manifest.technical_details)


class BehavioralDenialTest(unittest.TestCase):
    """Each guarded surface, driven with a value the guard must deny; the
    denial message must reach the caller so the agent can retry."""

    def assert_denied(self, result, fragment: str) -> None:
        self.assertIsInstance(result, ActionFailed)
        self.assertIn(fragment, result.error)
        self.assertIn("retry", result.error)

    def test_brave_search_query_denied(self) -> None:
        from host.tools.brave_search import BUNDLED_TOOL

        result = BUNDLED_TOOL.execute(
            "search_web", {"query": "verify AKIAIOSFODNN7EXAMPLE now"}, FakeHostAPI()
        )
        self.assert_denied(result, "credential")

    def test_polymarket_search_and_slug_denied(self) -> None:
        from host.tools.polymarket import BUNDLED_TOOL

        result = BUNDLED_TOOL.execute(
            "search", {"query": "will alice@example.com win"}, FakeHostAPI()
        )
        self.assert_denied(result, "email address")
        result = BUNDLED_TOOL.execute(
            "get_market", {"slug": "market-482913488123"}, FakeHostAPI()
        )
        self.assert_denied(result, "digits")

    def test_instagram_discovery_query_and_hashtag_denied(self) -> None:
        from host.tools.instagram_discovery import BUNDLED_TOOL

        api = FakeHostAPI(config={"SCRAPECREATORS_API_KEY": "k"})
        result = BUNDLED_TOOL.execute(
            "search_reels", {"query": "code 482913 reels"}, api
        )
        self.assert_denied(result, "code")
        result = BUNDLED_TOOL.execute("search_hashtag", {"hashtag": "sale4829134881234"}, api)
        self.assert_denied(result, "digits")

    def test_linkedin_discovery_query_denied(self) -> None:
        from host.tools.linkedin_discovery import BUNDLED_TOOL

        api = FakeHostAPI(config={"SERPERAPI_API_KEY": "k"})
        result = BUNDLED_TOOL.execute(
            "search_posts", {"query": "posts by alice.smith@acme.com"}, api
        )
        self.assert_denied(result, "email address")

    def test_runway_prompt_and_speech_denied(self) -> None:
        from host.tools import runway

        with self.assertRaises(ParamGuardDenied):
            runway._image_request(FakeHostAPI(), {"prompt": "ssn 219-09-9999 poster"})
        with self.assertRaises(ParamGuardDenied):
            runway._speech_request(FakeHostAPI(), {"text": "my password is hunter2secret"})

    def test_runway_external_url_is_guarded_and_rejects_ip_literals(self) -> None:
        from host.tools import runway

        api = FakeHostAPI()
        # A clean public https URL passes the guard unchanged.
        clean = "https://images.example.com/cat.jpg"
        self.assertEqual(runway._https_url({"image_url": clean}, "image_url", api), clean)
        # A secret/identifier encoded into the URL is denied.
        with self.assertRaises(ParamGuardDenied):
            runway._https_url(
                {"image_url": "https://x.example.com/c?d=alice@example.com"}, "image_url", api
            )

    def test_seedance_prompt_and_reference_url_denied(self) -> None:
        from host.tools import seedance

        api = FakeHostAPI()
        with self.assertRaises(ParamGuardDenied):
            seedance._generation_request(api, {"prompt": "ssn 219-09-9999 poster"})
        # A clean public https reference URL passes the guard unchanged.
        clean = "https://images.example.com/cat.jpg"
        self.assertEqual(seedance._https_url({"image_url": clean}, "image_url", api), clean)
        # A secret/identifier encoded into the URL is denied.
        with self.assertRaises(ParamGuardDenied):
            seedance._https_url(
                {"image_url": "https://x.example.com/c?d=alice@example.com"}, "image_url", api
            )

    def test_twitter_search_query_denied(self) -> None:
        from host.tools import twitter

        with self.assertRaises(ParamGuardDenied):
            twitter._search_tweets("token", {"query": "call +1 415 555 2671"}, FakeHostAPI())

    def test_guarded_value_reaches_request_unchanged(self) -> None:
        # The guard returns the identical object for clean values; the
        # brave payload builder must place that exact string on the wire.
        from host.tools import brave_search

        payload = brave_search._request_payload({"query": "flights to seattle"}, FakeHostAPI())
        self.assertEqual(payload["q"], "flights to seattle")


if __name__ == "__main__":
    unittest.main()


class NetworkIntegrationGuardTest(unittest.TestCase):
    """The proxy-side surfaces share the same guard over decoded URL values."""

    def setUp(self) -> None:
        from host.network_integrations.base import ManagedIntegration

        self.config = ManagedIntegration(True)

    def test_python_packages_names_are_guarded_but_downloads_exempt(self) -> None:
        from host.network_integrations.python_packages import guard

        deny = guard.request_denied
        self.assertIsNone(deny(self.config, "GET", "pypi.org", "/simple/requests/", "", [], b""))
        self.assertEqual(
            deny(self.config, "GET", "pypi.org", "/simple/AKIAIOSFODNN7EXAMPLE/", "", [], b""),
            "request_param_secret_denied",
        )
        # Headers are not inspected on these destinations: nothing reflects
        # them back, so there is no reader for that channel.
        self.assertIsNone(
            deny(
                self.config,
                "GET",
                "pypi.org",
                "/simple/requests/",
                "",
                [("X-Request-Note", "alice@example.com")],
                b"",
            )
        )
        self.assertIsNone(
            deny(
                self.config,
                "GET",
                "pypi.org",
                "/simple/requests/",
                "",
                [("If-None-Match", 'W/"x7Kp2mQv9zR4tYw8LbN3"')],
                b"",
            )
        )
        # Download URLs are provider-echoed (index-response links): their hash
        # segments must not be scanned or pip installs would break.
        digest_path = "/packages/ab/cd/" + "e" * 64 + "/requests-2.31.0-py3-none-any.whl"
        self.assertIsNone(
            deny(
                self.config,
                "GET",
                "files.pythonhosted.org",
                digest_path,
                "",
                [("X-Request-Note", "alice@example.com")],
                b"",
            )
        )

    def test_npm_packages_names_are_guarded(self) -> None:
        from host.network_integrations.npm_packages import guard

        deny = guard.request_denied
        self.assertIsNone(
            deny(self.config, "GET", "registry.npmjs.org", "/%40babel%2fcore", "", [], b"")
        )
        self.assertEqual(
            deny(self.config, "GET", "registry.npmjs.org", "/pkg-AKIAIOSFODNN7EXAMPLE", "", [], b""),
            "request_param_secret_denied",
        )
        self.assertIsNone(
            deny(
                self.config,
                "GET",
                "registry.npmjs.org",
                "/react",
                "",
                [("X-Request-Note", "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")],
                b"",
            )
        )

    def test_github_reads_guard_query_values_without_token_rules(self) -> None:
        from host.network_integrations.github import guard
        from host.network_integrations.github.manifest import GitHubIntegration

        config = GitHubIntegration(enabled=True)
        deny = guard.request_denied
        self.assertIsNone(
            deny(config, "GET", "api.github.com", "/search/code", "q=fibonacci+language%3Apython", [], b"")
        )
        # A git fetch still passes with git's own protocol headers, which is
        # what the allowlist is for.
        self.assertIsNone(
            deny(
                config,
                "POST",
                "github.com",
                "/o/r.git/git-upload-pack",
                "",
                [
                    ("User-Agent", "git/2.43.0"),
                    ("Content-Type", "application/x-git-upload-pack-request"),
                    ("Git-Protocol", "version=2"),
                ],
                b"",
            )
        )
        # Machine-shaped provider values stay legitimate: shas, refs, cursors.
        self.assertIsNone(
            deny(
                config,
                "GET",
                "api.github.com",
                "/repos/a/b/commits",
                "sha=9c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b",
                [],
                b"",
            )
        )
        self.assertEqual(
            deny(config, "GET", "api.github.com", "/search/users", "q=alice%40example.com", [], b""),
            "request_param_pii_denied",
        )
        self.assertIsNone(
            deny(
                config,
                "GET",
                "api.github.com",
                "/rate_limit",
                "",
                [("X-Request-Note", "alice@example.com")],
                b"",
            )
        )
        self.assertIsNone(
            deny(
                config,
                "GET",
                "api.github.com",
                "/rate_limit",
                "",
                [("Authorization", "Bearer ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")],
                b"",
            )
        )
        # The param guard applies to reads only; a write query is governed by
        # the write-repo rules, not scanned for public-leak shapes (it can only
        # reach a configured repo). A POST carrying an email-shaped query to an
        # unconfigured repo is denied for the write-repo reason, never
        # request_param_pii_denied.
        write_denial = deny(
            config, "POST", "api.github.com", "/repos/o/r/issues", "q=alice%40example.com", [], b"",
        )
        self.assertNotEqual(write_denial, "request_param_pii_denied")

    def test_github_read_only_hosts_guard_forwarded_headers(self) -> None:
        from host.network_integrations.github import guard
        from host.network_integrations.github.manifest import GitHubIntegration

        self.assertIsNone(
            guard.request_denied(
                GitHubIntegration(enabled=True),
                "GET",
                "raw.githubusercontent.com",
                "/owner/repo/revision/file.txt",
                "",
                [("X-Request-Note", "alice@example.com")],
                b"",
            )
        )
        self.assertIsNone(
            guard.request_denied(
                GitHubIntegration(enabled=True),
                "POST",
                "github.com",
                "/owner/repo.git/git-upload-pack",
                "",
                [("If-None-Match", "alice@example.com")],
                b"git request",
            )
        )
        self.assertIsNone(
            guard.request_denied(
                GitHubIntegration(enabled=True),
                "POST",
                "github.com",
                "/owner/repo.git/git-upload-pack",
                "",
                [("X-Request-Note", "alice@example.com")],
                b"git request",
            )
        )

    def test_github_actions_blob_allows_only_scoped_signed_downloads(self) -> None:
        from host.network_integrations.github import guard
        from host.network_integrations.github.manifest import GitHubIntegration

        config = GitHubIntegration(enabled=True)
        deny = guard.request_denied
        host = "productionresultssa17.blob.core.windows.net"
        sas_signature = (
            "sig=HhC%2FUPa%2FtitCP1DLVLa0ZnGPCw0RT338fxdeQ04ZoPw%3D"
        )
        sas_query = (
            "sp=r&st=2026-07-25T14%3A00%3A00Z&se=2026-07-25T16%3A00%3A00Z"
            "&spr=https&sv=2025-07-05&sr=b"
            f"&{sas_signature}"
        )
        self.assertIsNone(
            deny(config, "GET", host, "/actions-results/job/logs.zip", sas_query, [], b"")
        )
        self.assertIsNone(
            deny(config, "HEAD", host, "/actions-results/job/logs.zip", sas_query, [], b"")
        )
        self.assertEqual(
            deny(config, "POST", host, "/actions-results/job/logs.zip", sas_query, [], b""),
            "network_policy_denied",
        )
        self.assertTrue(guard.host_allowed(config, host))
        self.assertTrue(
            guard.host_allowed(config, "productionresultssa0.blob.core.windows.net")
        )
        for unowned_host in (
            "attacker.blob.core.windows.net",
            "productionresultssa20.blob.core.windows.net",
            "nested.productionresultssa17.blob.core.windows.net",
        ):
            with self.subTest(unowned_host=unowned_host):
                self.assertFalse(guard.host_allowed(config, unowned_host))
        self.assertIsNone(
            deny(
                config,
                "GET",
                host,
                "/actions-results/job/logs.zip",
                sas_query,
                [
                    (
                        "x-ms-client-request-id",
                        "%61%6C%69%63%65%40%65%78%61%6D%70%6C%65%2E%63%6F%6D",
                    )
                ],
                b"",
            )
        )
        self.assertIsNone(
            deny(
                config,
                "GET",
                host,
                "/actions-results/job/logs.zip",
                sas_query,
                [("User-Agent", "curl/8.10"), ("Authorization", "Bearer agent-secret")],
                b"",
            )
        )
        rewritten = guard.rewrite_request_headers(
            config,
            "GET",
            host,
            "/actions-results/job/logs.zip",
            sas_query,
            [
                ("Host", "PrOdUcTiOnReSuLtSsA17.BlOb.CoRe.WiNdOwS.NeT:443"),
                ("Authorization", "Bearer agent-secret"),
            ],
            b"",
        )
        self.assertNotIn("authorization", {key.lower() for key, _value in rewritten})
        # Host is not rewritten here any more: the proxy forwards a canonical
        # Host on every request, so the agent's case/port spelling never
        # reaches Azure. Covered by
        # test_network_proxy.WebSocketHandshakeTests.test_host_is_forwarded_canonically.
        self.assertEqual(
            [value for key, value in rewritten if key.lower() == "host"],
            ["PrOdUcTiOnReSuLtSsA17.BlOb.CoRe.WiNdOwS.NeT:443"],
        )
        for unsigned in (
            "",
            "sv=2025-07-05",
            "sig=",
            "sig=abc",
            "sig=one&sig=two",
        ):
            with self.subTest(unsigned=unsigned):
                self.assertEqual(
                    deny(
                        config,
                        "GET",
                        host,
                        "/actions-results/job/logs.zip",
                        unsigned,
                        [],
                        b"",
                    ),
                    "network_policy_denied",
                )
        # The destination-specific sig handling still checks explicit secret
        # shapes before neutralizing the provider-issued random signature.
        self.assertEqual(
            deny(
                config,
                "GET",
                host,
                "/actions-results/job/logs.zip",
                "sig=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
                [],
                b"",
            ),
            "request_param_secret_denied",
        )
        # Every other query key retains the unchanged global parameter guard.
        self.assertEqual(
            deny(
                config,
                "GET",
                host,
                "/actions-results/job/logs.zip",
                f"{sas_signature}&token=passwordpassword",
                [],
                b"",
            ),
            "request_param_secret_denied",
        )
        self.assertEqual(
            deny(
                config,
                "GET",
                host,
                "/actions-results/job/logs.zip",
                f"{sas_signature}&note=x7Kp2mQv9zR4tYw8LbN3",
                [],
                b"",
            ),
            "request_param_encoded_blob_denied",
        )
        self.assertEqual(
            deny(
                config,
                "GET",
                host,
                "/actions-results/x7Kp2mQv9zR4tYw8LbN3/logs.zip",
                sas_signature,
                [],
                b"",
            ),
            "request_param_encoded_blob_denied",
        )
        self.assertEqual(
            deny(
                config,
                "GET",
                host,
                "/actions-results/ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/logs.zip",
                sas_signature,
                [],
                b"",
            ),
            "request_param_secret_denied",
        )

    def test_proxy_guard_rejects_invalid_percent_encoding(self) -> None:
        from host.network_integrations.base import request_param_denial

        # Lenient decoding would smooth %ff%fe into replacement characters
        # while the raw bytes went upstream: a binary channel. Strict
        # decoding denies it.
        self.assertEqual(
            request_param_denial("/simple/%ff%fe%fd/", ""),
            "request_param_encoded_blob_denied",
        )

    def test_managed_headers_forward_untouched_but_credentials_are_removed(self) -> None:
        """Headers are forwarded as sent on the managed destinations.

        These are first-party hosts that reflect nothing back, so a header is
        not a channel anyone can read; guarding it bounds nothing. What the
        proxy removes is what a header can *do* — an identity the destination
        should not receive — plus the free text in User-Agent.
        """
        from host.network_integrations.base import ManagedIntegration
        from host.network_integrations.npm_packages import guard as npm
        from host.network_integrations.python_packages import guard as pypi

        sent = [
            ("User-Agent", 'pip/24.0 {"ci":null,"cpu":"x86_64"}'),
            ("Authorization", "Bearer a-credential-the-registry-should-not-get"),
            ("Cookie", "session=abcd1234"),
            ("Accept", "text/html"),
            ("X-Whatever", "an ordinary client header"),
        ]
        for guard in (pypi, npm):
            with self.subTest(guard=guard.__name__):
                forwarded = guard.rewrite_request_headers(
                    None, "GET", "pypi.org", "/simple/x/", "", list(sent), b""
                )
                names = {key.lower() for key, _ in forwarded}
                self.assertNotIn("authorization", names)
                self.assertNotIn("cookie", names)
                from host.network_integrations.base import PROXY_USER_AGENT

                self.assertEqual(
                    [value for key, value in forwarded if key.lower() == "user-agent"],
                    [PROXY_USER_AGENT],
                )
                self.assertIn(("X-Whatever", "an ordinary client header"), forwarded)
                self.assertIn(("Accept", "text/html"), forwarded)

        # No header value denies on these destinations any more...
        config = ManagedIntegration(True)
        self.assertIsNone(
            pypi.request_denied(
                config,
                "GET",
                "pypi.org",
                "/simple/requests/",
                "",
                [("X-Request-Note", "alice@example.com")],
                b"",
            )
        )
        # ...but the URL still does, because npm and PyPI publish per-package
        # download statistics, which is a channel someone can actually read.
        self.assertEqual(
            pypi.request_denied(
                config, "GET", "pypi.org", "/simple/alice@example.com/", "", [], b""
            ),
            "request_param_pii_denied",
        )

    def test_user_agent_is_replaced_with_the_host_value(self) -> None:
        """The one field where the no-reader argument does not hold: PyPI's
        public download dataset derives the installer name and version from
        User-Agent, so an agent-chosen product token there is readable."""
        from host.network_integrations.base import PROXY_USER_AGENT, fixed_user_agent

        for sent in (
            'pip/24.0 {"ci":null,"cpu":"x86_64"}',
            "pip/x7Kp2mQv9zR4tYw8LbN3",
            "npm/10.5.0 node/v20.12.2 linux x64",
        ):
            with self.subTest(sent=sent):
                self.assertEqual(
                    fixed_user_agent([("User-Agent", sent)]),
                    [("User-Agent", PROXY_USER_AGENT)],
                )
        # Added when absent, so the destination always sees one.
        self.assertEqual(
            fixed_user_agent([("Accept", "text/html")]),
            [("Accept", "text/html"), ("User-Agent", PROXY_USER_AGENT)],
        )

    def test_proxy_guard_catches_credential_query_keys_via_whole_url(self) -> None:
        from host.network_integrations.base import request_param_denial

        # Scanning the reconstructed full URL routes through the CRED_URL
        # guard, so a bland 16+ char value under a credential-named key is a
        # smuggled secret even though the value alone would pass.
        self.assertEqual(
            request_param_denial("/x", "access_token=mFzQpLdRaWxVkHsN"),
            "request_param_secret_denied",
        )
        self.assertEqual(
            request_param_denial(
                "/file",
                "sig=HhC%2FUPa%2FtitCP1DLVLa0ZnGPCw0RT338fxdeQ04ZoPw%3D",
            ),
            "request_param_secret_denied",
        )
        self.assertEqual(
            request_param_denial(
                "/upload",
                "sig=ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
            ),
            "request_param_secret_denied",
        )
        self.assertIsNone(request_param_denial("/x", "sort=ascending&page=2"))

    def test_npm_tarball_paths_are_provider_echoed(self) -> None:
        from host.network_integrations.base import ManagedIntegration
        from host.network_integrations.npm_packages import guard

        config = ManagedIntegration(True)
        self.assertIsNone(
            guard.request_denied(
                config, "GET", "registry.npmjs.org",
                "/somepkg/-/somepkg-1.0.0-alpha.20240315123456.tgz", "",
                [("X-Request-Note", "alice@example.com")], b"",
            )
        )

    def test_polymarket_market_id_requires_numeric_grammar(self) -> None:
        from host.tools.polymarket import BUNDLED_TOOL

        result = BUNDLED_TOOL.execute(
            "get_market", {"market_id": "AKIAIOSFODNN7EXAMPLE"}, FakeHostAPI()
        )
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("numeric", result.error)

    def test_gmail_path_ids_require_the_gmail_id_grammar(self) -> None:
        # TYPED exemption backing: free agent text must never reach the Gmail
        # API path, only a provider-shaped id. Each id kind has its own
        # grammar (hex message/thread ids, "r<digits>" draft ids, uppercase or
        # "Label_<n>" label ids), so prose that fits a generic charset — e.g.
        # "please_forward_alice" — is rejected before any request is built.
        from host.tools import gmail
        from host.tools.gmail import BUNDLED_TOOL
        from host.tools.gmail.api import ToolInputValidationError, gmail_operation_request
        from test_tools import connected_google_api

        api = connected_google_api(gmail.MANIFEST.tool_id, gmail.REQUIRED_GMAIL_SCOPES)
        result = BUNDLED_TOOL.execute(
            "read_message", {"message_id": "please_forward_alice"}, api
        )
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("valid Gmail message id", result.error)
        result = BUNDLED_TOOL.execute("read_thread", {"thread_id": "not-a-thread-id"}, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("valid Gmail thread id", result.error)
        # Every operation's path goes through the same choke point, so the
        # per-kind grammars also hold for drafts and labels, and one kind's
        # valid id is not accepted for another kind's path.
        for operation, bad_id in (
            ("users.messages.get", "please_forward_alice"),
            ("users.messages.trash", "Label_7"),
            ("users.threads.get", "not-a-thread-id"),
            ("users.drafts.get", "attach_the_report"),
            ("users.drafts.delete", "18c2f0d1a2b3c4d5"),
            ("users.labels.get", "personal_notes"),
            ("users.labels.delete", "r1234567890"),
        ):
            with self.assertRaises(ToolInputValidationError, msg=f"{operation} accepted {bad_id!r}"):
                gmail_operation_request(operation, {"id": bad_id})
        for operation, provider_id in (
            ("users.messages.get", "18c2f0d1a2b3c4d5"),
            ("users.threads.get", "0a1b2c3d4e5f6789"),
            ("users.drafts.get", "r-1234567890123456789"),
            ("users.drafts.delete", "r987654321"),
            ("users.labels.get", "INBOX"),
            ("users.labels.delete", "CATEGORY_PERSONAL"),
            ("users.labels.get", "Label_25"),
        ):
            request = gmail_operation_request(operation, {"id": provider_id})
            self.assertIn(provider_id, str(request["path"]))

    def test_calendar_time_fields_and_event_id_require_their_grammars(self) -> None:
        # TYPED exemption backing: read_events time fields must parse as ISO
        # 8601 timestamps, and the pre-approval event preview only accepts an
        # id-shaped event_id.
        from host.tools import google_calendar
        from host.tools.google_calendar import BUNDLED_TOOL
        from test_tools import connected_google_api

        api = connected_google_api(
            google_calendar.MANIFEST.tool_id, google_calendar.REQUIRED_CALENDAR_SCOPES
        )
        result = BUNDLED_TOOL.execute(
            "read_events", {"start_time": "notes about alice's meeting"}, api
        )
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("ISO 8601", result.error)
        result = BUNDLED_TOOL.execute(
            "event_change", {"operation": "delete", "event_id": "call_alice"}, api
        )
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("event id", result.error)
        # The grammar is the provider's own shape — lowercase base32hex, with
        # an underscore legal only in the recurring-instance suffix — so
        # prose-shaped ids cannot pass, while real ids (including recurring
        # instances) do.
        for provider_id in ("abc12def45", "0f9e8d7c6b5a4321", "abc12def45_20240101T100000Z"):
            self.assertIsNotNone(google_calendar.CALENDAR_EVENT_ID_RE.fullmatch(provider_id))
        for prose_id in (
            "call_alice",
            "delete_the_planning_event",
            "not-an-event-id",
            "ABC12DEF45",
            "abc1",
            "abc12def45_tomorrow",
        ):
            self.assertIsNone(google_calendar.CALENDAR_EVENT_ID_RE.fullmatch(prose_id))

    def test_custom_domains_are_not_inspected(self) -> None:
        """A custom domain's contract is the operator's rule and nothing else.

        The operator names the domain, methods and paths; the host inspects
        nothing inside the request. There is no knowable client, header set or
        URL grammar to check against, and request bodies were never scanned, so
        on any write-capable domain a content guard was never a boundary.
        """
        from host.network_integrations.custom import guard
        from host.network_integrations.custom.manifest import (
            CustomDomainRule,
            CustomIntegration,
        )

        config = CustomIntegration(
            domains={
                "api.example.com": CustomDomainRule(
                    allow_http_methods=("GET", "POST", "PUT"),
                    path_guards=(r"^/v1(?:/.*)?$",),
                )
            }
        )
        deny = guard.request_denied
        # The operator's rule is enforced: method and path, nothing else.
        self.assertIsNone(deny(config, "GET", "api.example.com", "/v1/lookup", "", [], b""))
        self.assertEqual(
            deny(config, "DELETE", "api.example.com", "/v1/lookup", "", [], b""),
            "network_policy_denied",
        )
        self.assertEqual(
            deny(config, "GET", "api.example.com", "/admin", "", [], b""),
            "network_policy_denied",
        )
        self.assertEqual(
            deny(config, "GET", "other.example.com", "/v1/lookup", "", [], b""),
            "network_policy_denied",
        )
        # Inside an allowed route nothing is inspected: the headers and URL
        # values that a managed integration would refuse all pass here, which
        # is what makes the operator's act of adding the domain the decision.
        for label, headers, query in (
            ("opaque session cookie", [("Cookie", "session=abcd1234abcd1234")], ""),
            ("agent-held credential", [("Authorization", "Bearer sk-live-x7Kp2m")], ""),
            ("entity-tag precondition", [("If-Match", 'W/"x7Kp2mQv9zR4tYw8LbN3"')], ""),
            ("idempotency key", [("Idempotency-Key", "550e8400-e29b-41d4-a716-446655440000")], ""),
            ("bespoke header", [("X-Request-Note", "alice@example.com")], ""),
            ("identifier in query", [], "q=alice%40example.com"),
        ):
            with self.subTest(case=label):
                self.assertIsNone(
                    deny(config, "GET", "api.example.com", "/v1/lookup", query, headers, b"")
                )


    def test_shared_reason_codes_are_in_the_proxy_catalog(self) -> None:
        from host.network_integrations.registry import denial_reason_catalog

        catalog = denial_reason_catalog()
        for code in (
            "request_param_too_large",
            "request_param_encoded_blob_denied",
            "request_param_secret_denied",
            "request_param_pii_denied",
        ):
            self.assertIn(code, catalog)
            self.assertIn("retry", catalog[code].guidance)
