"""Vercel Analytics authorization, wire contract, and data-boundary tests."""
from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from host.tools import vercel_analytics as vercel
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared.web import UnmappedProviderError, WebRequestError
from test_tools import FakeHostAPI, assert_matches_output_schema


def api(token="private-test-token"):
    return FakeHostAPI(config={"VERCEL_ACCESS_TOKEN": token})


def query(**changes):
    return {"project_id": "prj_abc", "team_id": "team_xyz", "start_date": "2026-09-01", "end_date": "2026-09-09", **changes}


class VercelAnalyticsTests(unittest.TestCase):
    def test_discovery_calls_live_api_and_strips_project_settings(self):
        cases = [
            ("list_projects", {"team_id": "team_xyz", "cursor": "ABC234="}, {"projects": [{"id": "prj_abc", "name": "traderhand", "accountId": "team_xyz", "env": ["secret"], "targets": {"secret": "deploy"}}], "pagination": {"next": "DEF567="}}, "from", "ABC234="),
            ("list_teams", {"cursor": "1788915652000"}, {"teams": [{"id": "team_xyz", "name": "Personal hobby", "billing": "secret"}], "pagination": {"next": 1788910000000}}, "until", "1788915652000"),
        ]
        for action, payload, response, cursor_key, cursor_value in cases:
            with self.subTest(action=action), patch.object(vercel, "json_request", return_value=response) as request:
                result = vercel.BUNDLED_TOOL.execute(action, payload, api())
            self.assertIsInstance(result, ActionExecuted)
            assert_matches_output_schema(self, vercel.MANIFEST, action, result)
            args, kwargs = request.call_args
            self.assertEqual(args[0], "GET")
            self.assertEqual(args[1].split("?")[0], vercel.ENDPOINTS[action])
            params = parse_qs(urlsplit(args[1]).query)
            self.assertEqual(params[cursor_key], [cursor_value])
            self.assertEqual(params["limit"], ["20"])
            self.assertEqual(kwargs["headers"], {"Authorization": "Bearer private-test-token"})
            self.assertNotIn("secret", json.dumps(result.result))
            request.assert_called_once()

    def test_provider_cursor_accepts_opaque_tokens_but_rejects_secrets(self):
        cursor = "JBSWY3DPEHPK3PXP" * 12
        params = vercel._request("list_projects", {"cursor": cursor}, api())
        self.assertEqual(params["from"], cursor)
        with patch.object(vercel, "json_request") as request:
            for value in ("https://evil.example", "a" * 1025, "ghp_" + "A" * 36, True):
                self.assertIsInstance(vercel.BUNDLED_TOOL.execute("list_projects", {"cursor": value}, api()), ActionFailed)
            for value in ("ABC234=", -1, True, "12345678901234567"):
                self.assertIsInstance(vercel.BUNDLED_TOOL.execute("list_teams", {"cursor": value}, api()), ActionFailed)
        request.assert_not_called()

    def test_discovery_handles_empty_final_page_and_team_ownership(self):
        for action, key in (("list_projects", "projects"), ("list_teams", "teams")):
            with patch.object(vercel, "json_request", return_value={key: [], "pagination": {"next": None}}):
                result = vercel.BUNDLED_TOOL.execute(action, {}, api())
            self.assertEqual(result.result, {key: [], "next_cursor": None})
        result = vercel._discovery({"projects": [{"id": "prj_abc", "name": "hobby", "accountId": "team_xyz"}], "pagination": {"next": None}}, True, None)
        self.assertEqual(result["projects"][0]["team_id"], "team_xyz")
        invalid = [{"projects": []}, {"projects": [{}] * 21, "pagination": {"next": None}}, {"projects": [], "pagination": {"next": "https://evil.example"}}, {"projects": [{"id": "prj_abc", "name": "wrong", "accountId": "team_other"}], "pagination": {"next": None}}]
        for response in invalid:
            with patch.object(vercel, "json_request", return_value=response):
                self.assertIsInstance(vercel.BUNDLED_TOOL.execute("list_projects", {"team_id": "team_xyz"}, api()), ActionFailed)

    def test_queries_use_validated_project_team_fixed_get_and_bearer_only(self):
        for action, metric in (("query_visits", "pageviews"),):
            with self.subTest(action=action):
                payload = query(path="/snowbid", group_by="requestPath", limit="5")
                response = {"data": [{"requestPath": "/snowbid", metric: 20, "visitors": 12, "clientIp": "private", "eventData": {"email": "private"}}], "token": "private"}
                with patch.object(vercel, "json_request", return_value=response) as request:
                    result = vercel.BUNDLED_TOOL.execute(action, payload, api())
                self.assertIsInstance(result, ActionExecuted)
                assert_matches_output_schema(self, vercel.MANIFEST, action, result)
                args, kwargs = request.call_args
                parsed = urlsplit(args[1])
                self.assertEqual(args[0], "GET")
                self.assertEqual("https://" + parsed.netloc + parsed.path, vercel.ENDPOINTS[action])
                self.assertEqual(kwargs["headers"], {"Authorization": "Bearer private-test-token"})
                self.assertNotIn("body", kwargs)
                params = parse_qs(parsed.query)
                self.assertEqual(params["projectId"], ["prj_abc"])
                self.assertEqual(params["teamId"], ["team_xyz"])
                self.assertEqual(params["since"], ["2026-09-01T00:00:00.000Z"])
                self.assertEqual(params["until"], ["2026-09-09T23:59:59.999Z"])
                self.assertEqual(params["by"], ["requestPath"])
                expected = "environment eq 'production' and requestPath eq '/snowbid'"
                self.assertEqual(params["filter"], [expected])
                self.assertEqual(result.result["rows"], [{"value": "/snowbid", metric: 20, "visitors": 12}])
                self.assertNotIn("private", json.dumps(result.result))
                request.assert_called_once()

    def test_personal_scope_does_not_inherit_team(self):
        payload = query(project_id="prj_def")
        del payload["team_id"]
        params = vercel._request("query_visits", payload, api())
        self.assertNotIn("teamId", params)
        self.assertEqual(params["projectId"], "prj_def")

    def test_arbitrary_endpoints_ids_scopes_filters_and_actions_never_reach_network(self):
        invalid = [query(project_id="other"), query(project_id="prj_abc?secret"), query(project_id=["prj_abc"]), query(team_id="team_xyz/../evil")]
        for field in ("url", "teamId", "projectId", "project", "filter", "environment", "cursor", "headers", "event_name"):
            invalid.append(query(**{field: "anything"}))
        with patch.object(vercel, "json_request") as request:
            for payload in invalid:
                with self.subTest(payload=payload):
                    self.assertIsInstance(vercel.BUNDLED_TOOL.execute("query_visits", payload, api()), ActionFailed)
            for action in ("query_events", "deploy", "POST", "../events/aggregate", "delete_project"):
                self.assertIsInstance(vercel.BUNDLED_TOOL.execute(action, query(), api()), ActionFailed)
            self.assertIsInstance(vercel.BUNDLED_TOOL.execute("list_projects", {"url": "anything"}, api()), ActionFailed)
        request.assert_not_called()

    def test_dates_dimensions_and_limits_are_strict_and_bounded(self):
        changes = [{"start_date": "2026-02-30"}, {"start_date": "2026-09-10"}, {"start_date": "2026-08-01"}, {"end_date": "2026-09-09T00:00:00Z"}, {"start_date": True}, {"group_by": "eventName"}, {"group_by": "hour"}, {"group_by": "utmSource"}, {"group_by": "utmCampaign"}, {"group_by": ["day", "country"]}, {"limit": True}, {"limit": 0}, {"limit": "51"}]
        with patch.object(vercel, "json_request") as request:
            for change in changes:
                with self.subTest(change=change):
                    self.assertIsInstance(vercel.BUNDLED_TOOL.execute("query_visits", query(**change), api()), ActionFailed)
        request.assert_not_called()
        vercel._request("query_visits", query(start_date="2026-08-10", end_date="2026-09-09"), api())
        vercel._request("query_visits", query(start_date="2026-09-09", end_date="2026-09-09"), api())

    def test_daily_reports_preserve_every_date_even_with_small_limit(self):
        for requested_limit in (None, "1", "10", "31", "50"):
            payload = query(start_date="2026-08-10", end_date="2026-09-09")
            if requested_limit is not None:
                payload["limit"] = requested_limit
            params = vercel._request("query_visits", payload, api())
            self.assertEqual(int(params["limit"]), max(31, int(requested_limit or "10")))
        params = vercel._request("query_visits", query(group_by="requestPath", limit="1"), api())
        self.assertEqual(params["limit"], "1")

    def test_missing_config_matches_fresh_host_contract_for_every_action(self):
        with patch.object(vercel, "json_request") as request:
            for action in vercel.ENDPOINTS:
                payload = query() if action == "query_visits" else {}
                result = vercel.BUNDLED_TOOL.execute(action, payload, api(token=""))
                self.assertIsInstance(result, ActionFailed)
                self.assertIn("not set", str(result))
        request.assert_not_called()

    def test_configuration_fails_closed_without_echoing_values(self):
        with patch.object(vercel, "json_request") as request:
            for token in ("", "secret\r\nInjected: value", "space value"):
                result = vercel.BUNDLED_TOOL.execute("query_visits", query(), api(token=token))
                self.assertIsInstance(result, ActionFailed)
                self.assertNotIn("Injected", str(result))
        request.assert_not_called()

    def test_paths_pass_real_guard_including_percent_decoding(self):
        for action, field in (("query_visits", "path"),):
            for value in ("alice@example.com", "alice%40example.com", "alice%2540example.com"):
                value = "/" + value if field == "path" else value
                with self.subTest(action=action, field=field, value=value), patch.object(vercel, "json_request") as request:
                    result = vercel.BUNDLED_TOOL.execute(action, query(**{field: value}), api())
                    self.assertIsInstance(result, ActionFailed)
                    request.assert_not_called()
        with patch.object(vercel, "json_request") as request:
            for value in ("https://evil.example", "//evil.example", "/snowbid?code=abc", "/snowbid#code", "/" + "a" * 256, "/x\nheader"):
                self.assertIsInstance(vercel.BUNDLED_TOOL.execute("query_visits", query(path=value), api()), ActionFailed)
        request.assert_not_called()

    def test_odata_quote_is_escaped_as_literal(self):
        params = vercel._request("query_visits", query(path="/owner's-page"), api())
        self.assertEqual(params["filter"], "environment eq 'production' and requestPath eq '/owner''s-page'")

    def test_empty_unknown_dimension_and_daily_rows_keep_meaning(self):
        cases = [([], []), ([{"timestamp": "2026-09-01T00:00:00Z", "pageviews": 0, "visitors": 0}], [{"value": "2026-09-01T00:00:00Z", "pageviews": 0, "visitors": 0}])]
        for raw, expected in cases:
            with patch.object(vercel, "json_request", return_value={"data": raw}):
                result = vercel.BUNDLED_TOOL.execute("query_visits", query(), api())
            self.assertEqual(result.result["rows"], expected)
        self.assertEqual(vercel._rows({"data": [{"country": None, "pageviews": 1, "visitors": 1}]}, "country")[0]["value"], None)

    def test_malformed_metrics_fail_instead_of_fabricating_zero(self):
        invalid = [{}, {"data": {}}, {"data": [None]}, {"data": [{}]}, {"data": [{"pageviews": True, "visitors": 1}]}, {"data": [{"pageviews": -1, "visitors": 1}]}, {"data": [{"pageviews": "20", "visitors": 1}]}, {"data": [{"pageviews": 1, "visitors": 2**60}]}, {"data": [{}] * 101}]
        for response in invalid:
            with self.subTest(response=response), patch.object(vercel, "json_request", return_value=response):
                result = vercel.BUNDLED_TOOL.execute("query_visits", query(), api())
                self.assertIsInstance(result, ActionFailed)

    def test_http_errors_are_sanitized_and_never_retried(self):
        for status in (400, 401, 403, 404, 429):
            with patch.object(vercel, "json_request", side_effect=WebRequestError("private-test-token", status=status, body=b"secret response")) as request:
                result = vercel.BUNDLED_TOOL.execute("query_visits", query(), api())
            self.assertIsInstance(result, ActionFailed)
            self.assertNotIn("private-test-token", str(result))
            self.assertNotIn("secret response", str(result))
            request.assert_called_once()
        for status in (302, 500):
            with patch.object(vercel, "json_request", side_effect=WebRequestError("secret", status=status)), self.assertRaises(UnmappedProviderError):
                vercel.BUNDLED_TOOL.execute("query_visits", query(), api())


if __name__ == "__main__":
    unittest.main()
