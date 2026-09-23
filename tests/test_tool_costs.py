"""Cost accounting without live provider calls; storage cases also run in CI."""
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast
from unittest import TestCase
from unittest.mock import patch, MagicMock

import pg_harness
from host.runtime.core import db, state, pgclient
from host.runtime.core.state import tool_costs as cost_state
from host.runtime.tools.costs import HostCosts
from host.runtime.tools import tools_host, api as tools_api
from host.tools.host_api import ApprovalRecord
from host.tools.results import ActionExecuted, ActionFailed, ApprovalExecuted
from host.runtime.admin_api import tools_client
from host.tools import runway, twitter, twitterapi_io
from host.tools.runway import costs as runway_costs
from host.tools.twitter import costs as x_costs
from test_tools import FakeHostAPI


class CostAPITests(TestCase):
    def costs(self):
        return HostCosts("runway", "", "generate_video", True, "thread-106")

    def test_reporting_tool_requires_every_action_cost_description(self):
        actions = (replace(runway.MANIFEST.actions[0], cost_description=""), *runway.MANIFEST.actions[1:])
        with self.assertRaisesRegex(ValueError, "every action"):
            replace(runway.MANIFEST, actions=actions)
        self.assertFalse(replace(runway.MANIFEST, reports_cost=False, actions=actions).reports_cost)
        with self.assertRaisesRegex(ValueError, "must be a boolean"):
            replace(runway.MANIFEST, reports_cost=cast(Any, "yes"))

    def test_validates_money_and_scopes_every_record(self):
        api = self.costs()
        with patch.object(state, "record_tool_cost") as save:
            api.record("0.00015", charge_id="provider:123")
            args = save.call_args.kwargs
            self.assertEqual(args["amount_nano_usd"], 150000)
            self.assertEqual(args["tool_id"], "runway")
            self.assertEqual(args["action_id"], "generate_video")
            self.assertEqual(args["origin_thread_id"], "thread-106")
            for amount in ("NaN", "Infinity", "-1", "0.0000000001", "1000000000", True, 0.5, None):
                with self.subTest(amount=amount), self.assertRaises(ValueError):
                    api.record(amount)
            self.assertEqual(save.call_count, 1)

    def test_preserves_nine_decimal_dollars_in_integer_storage(self):
        with patch.object(state, "record_tool_cost") as save:
            self.costs().record("0.000000001")
            self.assertEqual(save.call_args.kwargs["amount_nano_usd"], 1)
            self.costs().record("999999999.999999999")
            self.assertEqual(save.call_args.kwargs["amount_nano_usd"], 999999999999999999)
        self.assertEqual(cost_state._usd(1), "0.000000001")
        self.assertEqual(cost_state._usd(999999999999999999), "999999999.999999999")
        self.assertEqual(cost_state._usd(0), "0")

    def test_approval_has_stable_default_charge(self):
        with patch.object(state, "record_tool_cost") as save:
            for _ in range(2):
                api = HostCosts("twitter", "c1", "post_tweet", True, "thread-1", "approval-7")
                api.record("0.015")
            self.assertEqual(save.call_args_list[0].kwargs, save.call_args_list[1].kwargs)
            self.assertEqual(save.call_args.kwargs["approval_id"], "approval-7")

    def test_undeclared_or_unscoped_usage_is_rejected(self):
        with self.assertRaises(ValueError):
            HostCosts("runway", "", "", False, None).record("1")
        with self.assertRaises(ValueError):
            self.costs().record("1", charge_id="bad id")

    def test_accounting_failure_does_not_fail_completed_action(self):
        with patch.object(state, "record_tool_cost", side_effect=RuntimeError("storage down")), patch("host.runtime.tools.costs.host_errors.report_warning") as warn:
            self.costs().record("1")
            warn.assert_called_once()

    def test_direct_failure_retains_reported_charge(self):
        def execute(action, arguments, api):
            api.costs.record("0.5")
            raise RuntimeError("failed after billing")
        with patch.object(tools_host, "enabled_tool", return_value=runway.BUNDLED_TOOL), patch.object(runway.BUNDLED_TOOL, "execute", side_effect=execute), patch.object(state, "tool_config_values", return_value={}), patch.object(state, "record_tool_cost") as save, patch.object(tools_host, "_audit"), patch.object(tools_host.host_errors, "report_warning"):
            result = tools_host.execute_action("runway", "generate_video", {"prompt": "test"}, "thread-5")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(save.call_args.kwargs["amount_nano_usd"], 500000000)
        self.assertEqual(save.call_args.kwargs["origin_thread_id"], "thread-5")

    def test_approved_execution_receives_approval_and_thread_context(self):
        def execute(approval, api):
            api.costs.record("0.5")
            return ApprovalExecuted("Done")
        record = dict(tool_id="runway", action_id="generate_video", approval_id="a1", status="approved", payload={}, summary="test", created_at=1, decided_at=2, connection_id="", account_id="", account_label="", origin_thread_id="thread-5")
        with patch.object(tools_host, "enabled_tool", return_value=runway.BUNDLED_TOOL), patch.object(runway.BUNDLED_TOOL, "execute_approved", side_effect=execute), patch.object(state, "tool_config_values", return_value={}), patch.object(state, "record_tool_cost") as save, patch.object(tools_host, "_audit"):
            result = tools_host._execute_approved(record, None)
        self.assertEqual(result["status"], "executed")
        self.assertEqual(save.call_args.kwargs["approval_id"], "a1")
        self.assertEqual(save.call_args.kwargs["origin_thread_id"], "thread-5")

    def test_state_uses_supported_database_wire_types_and_numeric_strings(self):
        cursor = MagicMock()
        def execute(sql, params):
            for value in params:
                pgclient._encode_parameter(value)
        cursor.execute.side_effect = execute
        cursor.fetchone.return_value = ("2026-09-22",)
        with patch.object(cost_state, "mutation") as mutation:
            mutation.return_value.__enter__.return_value = cursor
            state.record_tool_cost(tool_id="runway", connection_id="", charge_id="task:1",
                execution_id="1", action_id="generate_video", origin_thread_id=None,
                approval_id=None, amount_nano_usd=150000)
        self.assertEqual(cursor.execute.call_count, 2)
        cursor.fetchall.return_value = [("runway", "150000")]
        with patch.object(db, "transaction") as transaction:
            transaction.return_value.__enter__.return_value = cursor
            result = state.tool_cost_usage()
        self.assertEqual(Decimal(result["month_to_date"]), Decimal("0.00015"))

    def test_api_reports_catalog_and_tool_totals(self):
        with patch.object(state, "tool_cost_usage", return_value={"month_to_date": "1.200000003", "tools": [
                {"tool_id": "retired_tool", "month_to_date": "1"},
                {"tool_id": "runway", "month_to_date": "0.000000003"},
                {"tool_id": "twitterapi_io", "month_to_date": "0.2"}]}), patch.object(state, "enabled_tool_ids", return_value={"runway", "twitter"}):
            result = tools_client.tools_route("GET", "/v1/tools/usage", None)
        self.assertNotIn("actions", result)
        self.assertEqual(set(result), {"month_to_date", "tools"})
        self.assertNotIn("retired_tool", {tool["tool_id"] for tool in result["tools"]})
        runway_meta = next(tool for tool in result["tools"] if tool["tool_id"] == "runway")
        self.assertTrue(runway_meta["enabled"])
        self.assertEqual(runway_meta["month_to_date"], "0.000000003")
        self.assertNotIn("actions", runway_meta)
        self.assertNotIn("charges", runway_meta)
        disabled = next(tool for tool in result["tools"] if tool["tool_id"] == "twitterapi_io")
        self.assertFalse(disabled["enabled"])
        self.assertEqual(disabled["month_to_date"], "0.2")
        enabled_without_spend = next(tool for tool in result["tools"] if tool["tool_id"] == "twitter")
        self.assertEqual(enabled_without_spend["month_to_date"], "0")
        entry = tools_client._tool_entry(runway.BUNDLED_TOOL, {"runway"}, set())
        self.assertTrue(entry["reports_cost"])
        self.assertNotIn("reports_cost", entry["actions"][0])
        with patch.object(state, "enabled_tool_ids", return_value={"runway"}):
            described = tools_api._describe_tool({"tool_id": "runway"})["result"]
        self.assertTrue(described["reports_cost"])
        self.assertNotIn("reports_cost", described["actions"][0])


class ProviderCostTests(TestCase):
    def test_runway_reports_accepted_task_when_response_has_no_id(self):
        body = {"model": "gen4.5", "duration": 8}
        for response, charge_id, result_type in (({}, "", ActionFailed),
                                                  ({"id": "task-1"}, "task:task-1", ActionExecuted)):
            api = FakeHostAPI()
            with self.subTest(response=response), patch.object(runway, "json_request", return_value=response):
                result = runway.BUNDLED_TOOL._create_task("/tasks", body, {}, "gen4.5", "video", api)
            self.assertIsInstance(result, result_type)
            self.assertEqual(api.costs.calls, [("0.96", charge_id)])

    def test_x_reports_accepted_post_when_response_has_no_id(self):
        for text, expected in (("Hello", "0.015"), ("https://example.com", "0.200")):
            api = FakeHostAPI()
            approval = ApprovalRecord("approval-1", "post_tweet", "approved",
                                      {"proposal": {"text": text}, "x_account": {"id": "123", "label": "@me"}},
                                      "Post to X", 1, 2)
            with (self.subTest(text=text),
                  patch.object(twitter.X_CREDENTIALS, "access_token", return_value="token"),
                  patch.object(twitter.X_CREDENTIALS, "refresh_identity", return_value={"id": "123", "label": "@me"}),
                  patch.object(twitter, "json_request", return_value={"data": {}})):
                result = twitter.BUNDLED_TOOL.execute_approved(approval, api)
            self.assertIsInstance(result, ActionFailed)
            self.assertEqual(api.costs.calls, [(expected, "")])

    def test_twitterapi_bills_full_provider_page_and_empty_minimum(self):
        for posts, expected in (([], "0.00015"), ([{"id": str(i)} for i in range(20)], "0.00300")):
            api = FakeHostAPI()
            api.config["TWITTERAPI_IO_API_KEY"] = "key"
            with patch.object(twitterapi_io, "_search", return_value={"tweets": posts}):
                twitterapi_io.BUNDLED_TOOL.execute("search_tweets", {"query": "test", "max_results": "1"}, api)
            self.assertEqual(api.costs.calls[0][0], expected)

    def test_twitterapi_bills_minimum_on_successful_malformed_response(self):
        for response in ({}, {"tweets": None}, {"tweets": "invalid"}):
            api = FakeHostAPI()
            api.config["TWITTERAPI_IO_API_KEY"] = "key"
            with self.subTest(response=response), patch.object(twitterapi_io, "_search", return_value=response):
                twitterapi_io.BUNDLED_TOOL.execute("search_tweets", {"query": "test"}, api)
            self.assertEqual(api.costs.calls[0][0], "0.00015")

    def test_runway_reports_only_calculable_accepted_tasks(self):
        api = FakeHostAPI()
        runway_costs.submitted(api, "task-1", {"model": "gen4.5", "duration": 8})
        runway_costs.submitted(api, "task-1", {"model": "gen4.5", "duration": 8})
        self.assertEqual(api.costs.records["task:task-1"]["amount_usd"], "0.96")
        self.assertEqual(len(api.costs.records), 1)
        runway_costs.submitted(api, "task-2", {"model": "seedance2_5", "duration": "auto"})
        self.assertEqual(len(api.costs.calls), 2)

    def test_x_missing_pricing_inputs_produce_no_cost_record(self):
        api = FakeHostAPI()
        x_costs.record_response(api, {"data": [{"id": "123"}]}, "post")
        api.config["X_OAUTH_CLIENT_ID"] = "app"
        x_costs.record_response(api, {"data": [{"id": "bad"}]}, "post")
        x_costs.record_response(api, {}, "post")
        self.assertEqual(api.costs.calls, [])

    def test_runway_quotes_options_and_skips_unknown_duration(self):
        for body, expected in [
            ({"model": "seedance2_5", "duration": 2, "ratio": "854:480"}, "0.8"),
            ({"model": "seedance2_5", "duration": 5, "ratio": "2206:946"}, "3.4"),
            ({"model": "seedance2", "duration": 5, "ratio": "3840:1646"}, "7.5"),
            ({"model": "seedance2_5", "duration": 5, "ratio": "1280:720", "audio": True}, None),
            ({"model": "seedance2_5", "duration": 5, "ratio": "1280:720", "audio": False}, None),
            ({"model": "veo3.1", "duration": 8, "audio": False}, "1.6"),
            ({"model": "gen4.5", "duration": 5, "outputFormat": "prores"}, "0.85"),
            ({"model": "gpt_image_2_5_flare", "quality": "xhigh"}, "0.28"),
            ({"model": "eleven_v3", "promptText": "Hi"}, "0.01"),
            ({"model": "aleph2", "videoUri": "https://example.com/video.mp4"}, None),
            ({"model": "seedance2_5", "duration": "auto"}, None),
            ({"model": "seedance2_5", "duration": 5, "referenceVideos": [{}]}, None),
        ]:
            with self.subTest(body=body):
                self.assertEqual(runway_costs.quote_usd(body), expected)

    def test_x_deduplicates_resources_across_actions_but_not_new_utc_days(self):
        api = FakeHostAPI()
        api.config["X_OAUTH_CLIENT_ID"] = "app"
        response = {"data": [{"id": "123"}], "includes": {"users": [{"id": "456"}]}}
        for day in (22, 22, 23):
            with patch.object(x_costs, "datetime") as clock:
                clock.now.return_value = datetime(2026, 9, day, tzinfo=timezone.utc)
                x_costs.record_response(api, response, "post")
        self.assertEqual(len(api.costs.records), 4)
        self.assertEqual(sum(Decimal(row["amount_usd"]) for row in api.costs.records.values()), Decimal("0.030"))

    def test_x_post_cost_does_not_guess_reply_or_bare_domain_prices(self):
        for proposal, amount in [({"text": "Hello"}, "0.015"), ({"text": "https://example.com"}, "0.200"), ({"text": "example.photography"}, None), ({"text": "Hi", "in_reply_to_tweet_id": "1"}, None)]:
            self.assertEqual(x_costs.post_amount(proposal, {}), amount)

    def test_x_owned_discount_requires_app_owner_and_authenticated_user(self):
        api = FakeHostAPI()
        api.credentials.save({"account": {"id": "123"}})
        self.assertFalse(x_costs.owned_reads(api, "123"))
        api.config["X_APP_OWNER_USER_ID"] = "123"
        self.assertTrue(x_costs.owned_reads(api, "123"))
        self.assertFalse(x_costs.owned_reads(api, "456"))


class CostStorageTests(TestCase):
    def setUp(self):
        pg_harness.reset_database()

    def record(self, charge, amount, **overrides):
        args = dict(tool_id="runway", connection_id="", charge_id=charge,
                    execution_id="call-1", action_id="generate_video", origin_thread_id="thread-1",
                    approval_id=None, amount_nano_usd=int(Decimal(amount) * 1_000_000_000))
        args.update(overrides)
        state.record_tool_cost(**args)

    def test_duplicate_report_preserves_amount_action_and_call_attribution(self):
        self.record("task-1", "0.15")
        self.record("task-1", "0.15", action_id="get_task", execution_id="call-2")
        self.record("task-1", "0.15")
        self.record("task-1", "3")
        result = state.tool_cost_usage()
        self.assertEqual(Decimal(result["month_to_date"]), Decimal("0.15"))
        self.assertEqual(result["tools"][0]["month_to_date"], "0.15")
        with db.transaction() as cur:
            cur.execute("SELECT execution_id, origin_thread_id FROM tool_costs WHERE charge_id = 'task-1'")
            self.assertEqual(cur.fetchone(), ("call-1", "thread-1"))
            cur.execute("SELECT action_id, amount_nano_usd, charges FROM tool_cost_daily WHERE tool_id = 'runway'")
            action_id, amount, charges = cur.fetchone()
            self.assertEqual((action_id, int(amount), charges), ("generate_video", 150000000, 1))

    def test_month_boundary_and_cross_connection_deduplication(self):
        # Seed a prior-month charge and its counter, then replay the ID now.
        with db.transaction() as cur:
            cur.execute("INSERT INTO tool_costs (tool_id, connection_id, charge_id, execution_id, action_id, "
                        "created_at, amount_nano_usd) VALUES ('runway', '', 'old', 'old-call', "
                        "'generate_video', '2000-01-01T00:00:00Z', 1000000000)")
            cur.execute("INSERT INTO tool_cost_daily (day, tool_id, action_id, amount_nano_usd, charges) "
                        "VALUES ('2000-01-01', 'runway', 'generate_video', 1000000000, 1)")
        self.record("old", "2")
        self.record("current", "0.00015", connection_id="account-1")
        self.record("current", "0.00015", connection_id="account-2")
        self.assertEqual(Decimal(state.tool_cost_usage()["month_to_date"]), Decimal("0.00015"))

    def test_tools_role_permissions(self):
        with db.transaction() as cur:
            for privilege, expected in (("SELECT", True), ("INSERT", True), ("UPDATE", False), ("DELETE", False)):
                cur.execute("SELECT has_table_privilege('kern-tools', 'tool_costs', %s)", (privilege,))
                self.assertEqual(cur.fetchone()[0], expected)
            for privilege, expected in (("SELECT", True), ("INSERT", True), ("UPDATE", True), ("DELETE", False)):
                cur.execute("SELECT has_table_privilege('kern-tools', 'tool_cost_daily', %s)", (privilege,))
                self.assertEqual(cur.fetchone()[0], expected)
