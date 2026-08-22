"""Google Search Console bundled-tool tests; all provider calls are mocked."""

from __future__ import annotations

import time
import unittest
import urllib.parse
from typing import Any
from unittest.mock import patch

from host.tools import google_search_console as search_console
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from test_tools import FakeHostAPI, assert_matches_output_schema, connected_google_api, google_userinfo


SITE = "https://example.com/"
DOMAIN_SITE = "sc-domain:example.com"
PREFIX_SITE = "https://example.com/catalog/"
PORT_SITE = "https://example.com:8443/catalog/"
PROPERTY_RESPONSE = {
    "siteEntry": [
        {"siteUrl": SITE, "permissionLevel": "siteOwner"},
        {"siteUrl": DOMAIN_SITE, "permissionLevel": "siteFullUser"},
    ]
}


def connected_api() -> FakeHostAPI:
    return connected_google_api(
        search_console.MANIFEST.tool_id,
        search_console.REQUIRED_SEARCH_CONSOLE_SCOPES,
    )


class GoogleSearchConsoleToolTests(unittest.TestCase):
    def test_manifest_exposes_supported_bounded_surface_and_guide(self) -> None:
        manifest = search_console.MANIFEST
        self.assertEqual(manifest.tool_id, "google_search_console")
        self.assertEqual(manifest.connection, "oauth")
        self.assertEqual(
            [action.id for action in manifest.actions],
            [
                "list_properties",
                "query_search_analytics",
                "list_sitemaps",
                "inspect_url",
                "submit_sitemap",
            ],
        )
        self.assertEqual(manifest.actions[-1].approval, "operator")
        self.assertTrue(all(action.approval == "direct" for action in manifest.actions[:-1]))
        self.assertNotIn("request_indexing", [action.id for action in manifest.actions])
        self.assertEqual(
            [item.key for item in manifest.config],
            ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"],
        )
        self.assertIn("cannot request indexing", manifest.agent_notes)
        self.assertTrue(manifest.setup_steps[-1].show_config)
        self.assertTrue(all(not step.image_path and not step.image_alt for step in manifest.setup_steps))
        self.assertEqual(len(manifest.data_summary.cards), 4)

    def test_list_properties_is_fixed_bounded_and_curated(self) -> None:
        raw = {
            "siteEntry": [
                *[
                    {
                        "siteUrl": f"https://unverified-{index}.example/",
                        "permissionLevel": "siteUnverifiedUser",
                    }
                    for index in range(search_console.MAX_PROPERTIES)
                ],
                *PROPERTY_RESPONSE["siteEntry"],
                {"siteUrl": "x" * 3_000, "permissionLevel": "siteOwner"},
                {"siteUrl": "https://ignored.example/", "permissionLevel": "newLevel"},
                {"siteUrl": "https://unverified.example/", "permissionLevel": "siteUnverifiedUser"},
                "invalid",
            ]
        }
        seen: dict[str, Any] = {}

        def fake_request(method: str, url: str, access_token: str, **kwargs: Any) -> dict[str, Any]:
            seen.update(method=method, url=url, access_token=access_token, kwargs=kwargs)
            return raw

        with patch.object(search_console, "google_json_request", side_effect=fake_request):
            result = search_console.BUNDLED_TOOL.execute("list_properties", {}, connected_api())

        self.assertIsInstance(result, ActionExecuted)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["url"], f"{search_console.SEARCH_CONSOLE_API_BASE_URL}/sites")
        self.assertEqual(seen["access_token"], "google_search_console-access-token")
        self.assertEqual(result.result["properties"], [
            {"site_url": SITE, "permission_level": "siteOwner"},
            {"site_url": DOMAIN_SITE, "permission_level": "siteFullUser"},
        ])

    def test_every_direct_result_matches_its_declared_output_schema(self) -> None:
        by_path: dict[str, dict[str, Any]] = {
            "searchAnalytics/query": {
                "responseAggregationType": "byProperty",
                "rows": [{"keys": ["kern"], "clicks": 5, "impressions": 100, "ctr": 0.05, "position": 3.5}],
                "metadata": {"first_incomplete_date": "2026-08-19"},
            },
            "sitemaps": {
                "sitemap": [{
                    "path": "https://example.com/sitemap.xml",
                    "type": "sitemap",
                    "lastSubmitted": "2026-08-01T00:00:00Z",
                    "warnings": "2",
                    "errors": 0,
                    "contents": [{"type": "web", "submitted": "10", "indexed": "4"}],
                }]
            },
            "urlInspection": {
                "inspectionResult": {
                    "inspectionResultLink": "https://search.google.com/search-console/inspect?x=1",
                    "indexStatusResult": {"verdict": "PASS", "sitemap": ["https://example.com/sitemap.xml"]},
                    "mobileUsabilityResult": {"verdict": "PASS", "issues": [{"issueType": "SMALL_FONT", "severity": "WARNING", "message": "Text too small"}]},
                    "richResultsResult": {"verdict": "PARTIAL", "detectedItems": [{
                        "richResultType": "Product",
                        "items": [{"name": "Kern", "issues": [{"issueMessage": "Missing price", "severity": "WARNING"}]}],
                    }]},
                }
            },
        }

        def fake_request(method: str, url: str, access_token: str, **kwargs: Any) -> dict[str, Any]:
            del method, access_token, kwargs
            if url.endswith("/sites"):
                return PROPERTY_RESPONSE
            for marker, response in by_path.items():
                if marker in url:
                    return response
            raise AssertionError(url)

        cases: tuple[tuple[str, dict[str, Any]], ...] = (
            ("list_properties", {}),
            ("query_search_analytics", {"site_url": SITE, "start_date": "2026-08-01", "end_date": "2026-08-07"}),
            ("list_sitemaps", {"site_url": SITE}),
            ("inspect_url", {"site_url": SITE, "inspection_url": "https://example.com/page"}),
        )
        for action, tool_input in cases:
            with self.subTest(action=action), patch.object(
                search_console, "google_json_request", side_effect=fake_request
            ):
                result = search_console.BUNDLED_TOOL.execute(action, tool_input, connected_api())
            assert_matches_output_schema(self, search_console.MANIFEST, action, result)

    def test_analytics_rechecks_property_and_sends_only_typed_options(self) -> None:
        calls: list[tuple[str, str, dict[str, Any]]] = []

        def fake_request(method: str, url: str, access_token: str, **kwargs: Any) -> dict[str, Any]:
            del access_token
            calls.append((method, url, kwargs))
            if url.endswith("/sites"):
                return PROPERTY_RESPONSE
            return {
                "rows": [
                    {
                        "keys": ["best ai host", "https://example.com/landing"],
                        "clicks": 12.0,
                        "impressions": 140.0,
                        "ctr": 0.0857,
                        "position": 9.4,
                        "ignored": "provider extra",
                    }
                ],
                "responseAggregationType": "byPage",
                "metadata": {"first_incomplete_date": "2026-08-16", "extra": "ignored"},
            }

        with patch.object(search_console, "google_json_request", side_effect=fake_request):
            result = search_console.BUNDLED_TOOL.execute(
                "query_search_analytics",
                {
                    "site_url": SITE,
                    "start_date": "2026-08-01",
                    "end_date": "2026-08-16",
                    "dimensions": ["query", "page"],
                    "search_type": "web",
                    "aggregation_type": "byPage",
                    "data_state": "all",
                    "row_limit": "50",
                    "start_row": "100",
                },
                connected_api(),
            )

        self.assertIsInstance(result, ActionExecuted)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(calls), 2)
        method, url, kwargs = calls[1]
        self.assertEqual(method, "POST")
        self.assertIn(urllib.parse.quote(SITE, safe=""), url)
        self.assertEqual(
            kwargs["body"],
            {
                "startDate": "2026-08-01",
                "endDate": "2026-08-16",
                "type": "web",
                "aggregationType": "byPage",
                "dataState": "all",
                "rowLimit": 50,
                "startRow": 100,
                "dimensions": ["query", "page"],
            },
        )
        self.assertEqual(result.result["rows"][0]["position"], 9.4)
        self.assertNotIn("ignored", result.result["rows"][0])
        self.assertEqual(result.result["metadata"], {
            "first_incomplete_date": "2026-08-16",
            "first_incomplete_hour": "",
        })

    def test_scoped_action_can_use_a_readable_property_beyond_the_display_cap(self) -> None:
        properties = [
            {
                "siteUrl": f"https://site-{index}.example/",
                "permissionLevel": "siteOwner",
            }
            for index in range(search_console.MAX_PROPERTIES + 1)
        ]
        requested = properties[-1]["siteUrl"]

        def fake_request(
            method: str, url: str, access_token: str, **kwargs: Any
        ) -> dict[str, Any]:
            del method, access_token, kwargs
            if url.endswith("/sites"):
                return {"siteEntry": properties}
            return {}

        with patch.object(search_console, "google_json_request", side_effect=fake_request):
            listed = search_console.BUNDLED_TOOL.execute(
                "list_properties", {}, connected_api()
            )
            scoped = search_console.BUNDLED_TOOL.execute(
                "list_sitemaps", {"site_url": requested}, connected_api()
            )

        self.assertIsInstance(listed, ActionExecuted)
        assert isinstance(listed, ActionExecuted)
        listed_properties = listed.result["properties"]
        self.assertIsInstance(listed_properties, list)
        assert isinstance(listed_properties, list)
        self.assertEqual(len(listed_properties), search_console.MAX_PROPERTIES)
        self.assertNotIn(
            requested,
            [item["site_url"] for item in listed_properties if isinstance(item, dict)],
        )
        self.assertIsInstance(scoped, ActionExecuted)

    def test_unlisted_property_never_reaches_scoped_endpoint(self) -> None:
        calls: list[str] = []

        def fake_request(method: str, url: str, access_token: str, **kwargs: Any) -> dict[str, Any]:
            del method, access_token, kwargs
            calls.append(url)
            return PROPERTY_RESPONSE

        with patch.object(search_console, "google_json_request", side_effect=fake_request):
            result = search_console.BUNDLED_TOOL.execute(
                "list_sitemaps", {"site_url": "https://attacker.example/"}, connected_api()
            )
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertIn("not available", result.error)
        self.assertEqual(calls, [f"{search_console.SEARCH_CONSOLE_API_BASE_URL}/sites"])

    def test_analytics_rejects_bad_dates_dimensions_and_hourly_shape(self) -> None:
        with patch.object(search_console, "_properties", return_value=[
            {"site_url": SITE, "permission_level": "siteOwner"},
            {"site_url": DOMAIN_SITE, "permission_level": "siteFullUser"},
        ]):
            for payload, fragment in (
                ({"site_url": SITE, "start_date": "yesterday", "end_date": "2026-08-16"}, "YYYY-MM-DD"),
                ({"site_url": SITE, "start_date": "2026-08-17", "end_date": "2026-08-16"}, "on or after"),
                ({"site_url": SITE, "start_date": "2026-08-01", "end_date": "2026-08-16", "dimensions": ["secret"]}, "unsupported"),
                ({"site_url": SITE, "start_date": "2026-08-01", "end_date": "2026-08-16", "data_state": "hourly_all"}, "hour dimension"),
                ({"site_url": SITE, "start_date": "2026-08-01", "end_date": "2026-08-16", "dimensions": ["hour"]}, "requires data_state"),
                ({"site_url": SITE, "start_date": "2026-08-01", "end_date": "2026-08-16", "dimensions": ["hour"], "data_state": "all"}, "requires data_state"),
                (
                    {
                        "site_url": SITE,
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-16",
                        "dimensions": ["page"],
                        "aggregation_type": "byProperty",
                    },
                    "page dimension",
                ),
                (
                    {
                        "site_url": SITE,
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-16",
                        "search_type": "discover",
                        "aggregation_type": "byProperty",
                    },
                    "not supported",
                ),
                (
                    {
                        "site_url": SITE,
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-16",
                        "search_type": "googleNews",
                        "aggregation_type": "byProperty",
                    },
                    "not supported",
                ),
                (
                    {
                        "site_url": SITE,
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-16",
                        "dimensions": ["query"],
                        "search_type": "discover",
                    },
                    "query dimension",
                ),
                (
                    {
                        "site_url": SITE,
                        "start_date": "2026-08-01",
                        "end_date": "2026-08-16",
                        "dimensions": ["query"],
                        "search_type": "googleNews",
                    },
                    "query dimension",
                ),
            ):
                with self.subTest(fragment=fragment):
                    result = search_console.BUNDLED_TOOL.execute(
                        "query_search_analytics", payload, connected_api()
                    )
                    self.assertIsInstance(result, ActionFailed)
                    assert isinstance(result, ActionFailed)
                    self.assertIn(fragment, result.error)

    def test_sitemap_listing_is_bounded_and_drops_provider_extras(self) -> None:
        def fake_request(method: str, url: str, access_token: str, **kwargs: Any) -> dict[str, Any]:
            del method, access_token, kwargs
            if url.endswith("/sites"):
                return PROPERTY_RESPONSE
            return {
                "sitemap": [
                    {
                        "path": "https://example.com/sitemap.xml",
                        "type": "sitemap",
                        "lastSubmitted": "2026-08-17T10:00:00Z",
                        "isPending": False,
                        "warnings": "1",
                        "errors": "0",
                        "contents": [{"type": "web", "submitted": "20", "indexed": "18"}],
                        "secretExtra": "ignored",
                    }
                ]
            }

        with patch.object(search_console, "google_json_request", side_effect=fake_request):
            result = search_console.BUNDLED_TOOL.execute(
                "list_sitemaps", {"site_url": SITE}, connected_api()
            )
        self.assertIsInstance(result, ActionExecuted)
        assert isinstance(result, ActionExecuted)
        sitemap = result.result["sitemaps"][0]
        self.assertEqual(sitemap["warnings"], 1)
        self.assertEqual(sitemap["contents"][0]["indexed"], 18)
        self.assertNotIn("secretExtra", sitemap)

    def test_sitemap_listing_rejects_invalid_provider_counts(self) -> None:
        with (
            patch.object(search_console, "_properties", return_value=[
                {"site_url": SITE, "permission_level": "siteOwner"}
            ]),
            patch.object(
                search_console,
                "google_json_request",
                return_value={"sitemap": [{"path": f"{SITE}sitemap.xml", "warnings": "-1"}]},
            ),
        ):
            result = search_console.BUNDLED_TOOL.execute(
                "list_sitemaps", {"site_url": SITE}, connected_api()
            )
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertIn("invalid sitemap count", result.error)

    def test_inspection_enforces_prefix_and_domain_ownership_and_curates_result(self) -> None:
        response = {
            "inspectionResult": {
                "inspectionResultLink": "https://search.google.com/search-console/inspect?x=1",
                "indexStatusResult": {
                    "verdict": "PASS",
                    "coverageState": "Submitted and indexed",
                    "robotsTxtState": "ALLOWED",
                    "indexingState": "INDEXING_ALLOWED",
                    "lastCrawlTime": "2026-08-16T12:00:00Z",
                    "pageFetchState": "SUCCESSFUL",
                    "googleCanonical": "https://example.com/page",
                    "userCanonical": "https://example.com/page",
                    "crawledAs": "MOBILE",
                    "sitemap": ["https://example.com/sitemap.xml"],
                    "referringUrls": ["https://example.com/"],
                    "providerExtra": "ignored",
                },
                "richResultsResult": {
                    "verdict": "PARTIAL",
                    "detectedItems": [{
                        "richResultType": "Product",
                        "items": [{"name": "Kern", "issues": [{"issueMessage": "Missing price", "severity": "WARNING"}]}],
                    }],
                },
            }
        }
        calls: list[tuple[str, str, dict[str, Any]]] = []

        def fake_request(method: str, url: str, access_token: str, **kwargs: Any) -> dict[str, Any]:
            del access_token
            calls.append((method, url, kwargs))
            if url.endswith("/sites"):
                return PROPERTY_RESPONSE
            return response

        with patch.object(search_console, "google_json_request", side_effect=fake_request):
            result = search_console.BUNDLED_TOOL.execute(
                "inspect_url",
                {"site_url": DOMAIN_SITE, "inspection_url": "https://blog.example.com/page"},
                connected_api(),
            )
        self.assertIsInstance(result, ActionExecuted)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(calls[-1][1], search_console.URL_INSPECTION_ENDPOINT)
        self.assertEqual(calls[-1][2]["body"]["siteUrl"], DOMAIN_SITE)
        inspection = result.result["inspection"]
        self.assertEqual(inspection["index_status"]["verdict"], "PASS")
        self.assertNotIn("providerExtra", inspection["index_status"])
        self.assertEqual(
            inspection["rich_results"]["detected_items"][0]["items"][0]["issues"][0]["message"],
            "Missing price",
        )

        with patch.object(search_console, "_properties", return_value=[
            {"site_url": SITE, "permission_level": "siteOwner"},
            {"site_url": DOMAIN_SITE, "permission_level": "siteFullUser"},
            {"site_url": PREFIX_SITE, "permission_level": "siteOwner"},
        ]):
            for site_url, url in (
                (SITE, "https://example.com.evil.test/page"),
                (DOMAIN_SITE, "https://notexample.com/page"),
                (PREFIX_SITE, "https://example.com/catalog/../private"),
                (PREFIX_SITE, "https://example.com/catalog/%2e%2e/private"),
                (PREFIX_SITE, "https://example.com/catalog/%252e%252e/private"),
                (PREFIX_SITE, "https://example.com/catalog%2f../private"),
            ):
                denied = search_console.BUNDLED_TOOL.execute(
                    "inspect_url", {"site_url": site_url, "inspection_url": url}, connected_api()
                )
                self.assertIsInstance(denied, ActionFailed)
                assert isinstance(denied, ActionFailed)
                self.assertTrue(
                    "under the selected" in denied.error or "fully qualified" in denied.error,
                    denied.error,
                )

    def test_inspection_rejects_malformed_language_tags_locally(self) -> None:
        with (
            patch.object(search_console, "_properties", return_value=[
                {"site_url": SITE, "permission_level": "siteOwner"}
            ]),
            patch.object(search_console, "google_json_request") as request,
        ):
            for language_code in (
                "-",
                "en--US",
                "-en",
                "en-",
                "en-u",
                "en_Us",
                "en-a-foo-A-bar",
                "sl-rozaj-ROZAJ",
            ):
                with self.subTest(language_code=language_code):
                    result = search_console.BUNDLED_TOOL.execute(
                        "inspect_url",
                        {
                            "site_url": SITE,
                            "inspection_url": "https://example.com/page",
                            "language_code": language_code,
                        },
                        connected_api(),
                    )
                    self.assertIsInstance(result, ActionFailed)
                    assert isinstance(result, ActionFailed)
                    self.assertIn("supported language tag", result.error)
            request.assert_not_called()

    def test_inspection_rejects_language_tags_that_can_encode_secrets(self) -> None:
        with (
            patch.object(search_console, "_properties", return_value=[
                {"site_url": SITE, "permission_level": "siteOwner"}
            ]),
            patch.object(
                search_console,
                "google_json_request",
                side_effect=AssertionError("must not send"),
            ) as request,
        ):
            result = search_console.BUNDLED_TOOL.execute(
                "inspect_url",
                {
                    "site_url": SITE,
                    "inspection_url": "https://example.com/page",
                    "language_code": "x-AKIAIOSF-ODNN7EXA-MPLE0",
                },
                connected_api(),
            )

        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()

    def test_inspection_rejects_sensitive_nested_url_encoding(self) -> None:
        with (
            patch.object(search_console, "_properties", return_value=[
                {"site_url": SITE, "permission_level": "siteOwner"}
            ]),
            patch.object(
                search_console,
                "google_json_request",
                side_effect=AssertionError("must not send"),
            ) as request,
        ):
            result = search_console.BUNDLED_TOOL.execute(
                "inspect_url",
                {
                    "site_url": SITE,
                    "inspection_url": "https://example.com/user/alice%2540example.com",
                },
                connected_api(),
            )

        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()

    def test_language_validation_accepts_grandfathered_tags(self) -> None:
        for language_code in ("i-klingon", "en-GB-oed", "sgn-BE-FR"):
            with self.subTest(language_code=language_code):
                self.assertEqual(
                    search_console._language_code(language_code), language_code
                )

    def test_nondefault_port_property_is_preserved_and_exactly_scoped(self) -> None:
        response = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}
        with (
            patch.object(search_console, "_properties", return_value=[
                {"site_url": PORT_SITE, "permission_level": "siteOwner"}
            ]),
            patch.object(search_console, "google_json_request", return_value=response),
        ):
            accepted = search_console.BUNDLED_TOOL.execute(
                "inspect_url",
                {
                    "site_url": PORT_SITE,
                    "inspection_url": "https://example.com:8443/catalog/page",
                },
                connected_api(),
            )
            denied = search_console.BUNDLED_TOOL.execute(
                "inspect_url",
                {
                    "site_url": PORT_SITE,
                    "inspection_url": "https://example.com/catalog/page",
                },
                connected_api(),
            )

        self.assertIsInstance(accepted, ActionExecuted)
        self.assertIsInstance(denied, ActionFailed)
        self.assertEqual(search_console._property_url(PORT_SITE), PORT_SITE)

    def test_sitemap_proposal_rejects_ambiguous_prefix_path(self) -> None:
        with patch.object(search_console, "_properties", return_value=[
            {"site_url": PREFIX_SITE, "permission_level": "siteOwner"}
        ]):
            result = search_console.BUNDLED_TOOL.execute(
                "submit_sitemap",
                {
                    "site_url": PREFIX_SITE,
                    "sitemap_url": "https://example.com/catalog/../sitemap.xml",
                },
                connected_api(),
            )
        self.assertIsInstance(result, ActionFailed)

    def test_root_prefix_accepts_bare_origin_url(self) -> None:
        response = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}
        with (
            patch.object(search_console, "_properties", return_value=[
                {"site_url": SITE, "permission_level": "siteOwner"}
            ]),
            patch.object(search_console, "google_json_request", return_value=response),
        ):
            result = search_console.BUNDLED_TOOL.execute(
                "inspect_url",
                {"site_url": SITE, "inspection_url": "https://example.com"},
                connected_api(),
            )
        self.assertIsInstance(result, ActionExecuted)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["inspection"]["inspection_url"], "https://example.com")

    def test_submit_sitemap_queues_then_rechecks_account_property_and_scope(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake_request(method: str, url: str, access_token: str, **kwargs: Any) -> dict[str, Any]:
            del access_token, kwargs
            calls.append((method, url))
            if url.endswith("/sites"):
                return PROPERTY_RESPONSE
            return {}

        api = connected_api()
        with (
            patch.object(search_console, "google_json_request", side_effect=fake_request),
            patch("host.tools.shared.google.get_google_userinfo", return_value=google_userinfo()),
        ):
            proposed = search_console.BUNDLED_TOOL.execute(
                "submit_sitemap",
                {"site_url": SITE, "sitemap_url": "https://example.com/sitemap.xml"},
                api,
            )
            self.assertIsInstance(proposed, ActionPendingApproval)
            assert isinstance(proposed, ActionPendingApproval)
            self.assertEqual(len(calls), 1, "proposal may list properties but must not submit")
            record = api.approvals.approve(proposed.approval_id)
            executed = search_console.BUNDLED_TOOL.execute_approved(record, api)

        self.assertIsInstance(executed, ApprovalExecuted)
        self.assertEqual(calls[-1][0], "PUT")
        self.assertIn(urllib.parse.quote("https://example.com/sitemap.xml", safe=""), calls[-1][1])
        self.assertEqual(len([method for method, _ in calls if method == "PUT"]), 1)

    def test_submit_sitemap_requires_write_permission_at_proposal_and_execution(self) -> None:
        api = connected_api()
        with patch.object(search_console, "_properties", return_value=[
            {"site_url": SITE, "permission_level": "siteRestrictedUser"}
        ]):
            denied = search_console.BUNDLED_TOOL.execute(
                "submit_sitemap",
                {"site_url": SITE, "sitemap_url": "https://example.com/sitemap.xml"},
                api,
            )
        self.assertIsInstance(denied, ActionFailed)
        assert isinstance(denied, ActionFailed)
        self.assertIn("owner or full-user", denied.error)

        with (
            patch.object(search_console, "_properties", return_value=[
                {"site_url": SITE, "permission_level": "siteOwner"}
            ]),
            patch("host.tools.shared.google.get_google_userinfo", return_value=google_userinfo()),
        ):
            proposed = search_console.BUNDLED_TOOL.execute(
                "submit_sitemap",
                {"site_url": SITE, "sitemap_url": "https://example.com/sitemap.xml"},
                api,
            )
        assert isinstance(proposed, ActionPendingApproval)
        record = api.approvals.approve(proposed.approval_id)
        with (
            patch.object(search_console, "_properties", return_value=[
                {"site_url": SITE, "permission_level": "siteRestrictedUser"}
            ]),
            patch("host.tools.shared.google.get_google_userinfo", return_value=google_userinfo()),
            patch.object(search_console, "_submit_sitemap") as submit,
        ):
            denied_after_approval = search_console.BUNDLED_TOOL.execute_approved(record, api)
        self.assertIsInstance(denied_after_approval, ActionFailed)
        assert isinstance(denied_after_approval, ActionFailed)
        self.assertIn("owner or full-user", denied_after_approval.error)
        submit.assert_not_called()

    def test_approved_sitemap_stops_on_account_change(self) -> None:
        api = connected_api()
        with (
            patch.object(search_console, "_properties", return_value=[
                {"site_url": SITE, "permission_level": "siteOwner"},
                {"site_url": DOMAIN_SITE, "permission_level": "siteFullUser"},
            ]),
            patch("host.tools.shared.google.get_google_userinfo", return_value=google_userinfo()),
        ):
            proposed = search_console.BUNDLED_TOOL.execute(
                "submit_sitemap",
                {"site_url": SITE, "sitemap_url": "https://example.com/sitemap.xml"},
                api,
            )
        assert isinstance(proposed, ActionPendingApproval)
        record = api.approvals.approve(proposed.approval_id)
        with patch(
            "host.tools.shared.google.get_google_userinfo",
            return_value=google_userinfo(sub="other-sub", email="other@example.com"),
        ):
            result = search_console.BUNDLED_TOOL.execute_approved(record, api)
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)

    def test_missing_scope_requires_reconnect(self) -> None:
        api = connected_api()
        assert api.credentials.record is not None
        api.credentials.record["account"]["scopes"] = ["openid", "email"]
        result = search_console.BUNDLED_TOOL.execute("list_properties", {}, api)
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIsNone(api.credentials.load())


if __name__ == "__main__":
    unittest.main()
