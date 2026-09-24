"""Exercise the Apify boundary with hostile inputs, state changes and provider data."""

import copy
import json
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from host.runtime.tools.tools_host import unsupported_schema_error
from host.tools import apify_developer as dev
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted, StreamingAsset
from host.tools.shared.web import WebRequestError
from test_tools import FakeHostAPI, assert_matches_output_schema

ACTOR = "a" * 17
ACCOUNT = "u" * 17
BUILD = "b" * 17
RUN = "r" * 17
DATASET = "d" * 17
OTHER = "x" * 17
TOKEN = "apify_api_developer_TEST_ONLY"


class Provider:
    def __init__(self):
        self.calls = []
        self.account = ACCOUNT
        self.actor = {"id": ACTOR, "userId": ACCOUNT, "name": "test-actor", "title": "Test Actor",
                      "description": "Public website data", "versions": [], "stats": {"totalRuns": 12, "totalUsers7Days": 3},
                      "isPublic": False, "actorPermissionLevel": "LIMITED_PERMISSIONS",
                      "pricingInfos": [{"pricingModel": "FREE", "apifyMarginPercentage": 0.2,
                                        "createdAt": "2025-01-01T00:00:00Z", "startedAt": "2025-01-01T00:00:00Z"}]}
        self.build = {"id": BUILD, "userId": ACCOUNT, "actId": ACTOR, "buildNumber": "0.1.7",
                      "status": "SUCCEEDED", "readme": "# Example", "stats": {"runTimeSecs": 2}}
        self.run = {"id": RUN, "userId": ACCOUNT, "actId": ACTOR, "buildId": BUILD, "status": "SUCCEEDED",
                    "defaultDatasetId": DATASET, "usageTotalUsd": 0, "chargedEventCounts": {"record": 10}}
        self.source = {"versionNumber": "0.1", "sourceType": "SOURCE_FILES", "buildTag": None,
                       "sourceFiles": [{"name": "main.js", "format": "TEXT", "content": "console.log('hello')"}]}
        self.log = b"hello world"
        self.rows = [{"title": "Test result"}]

    def __call__(self, method, url, **kwargs):
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path.removeprefix("/v2")
        params = urllib.parse.parse_qs(parsed.query)
        body = json.loads(kwargs["data"]) if kwargs.get("data") else None
        self.calls.append((method, path, params, body, kwargs))
        assert parsed.scheme == "https" and parsed.netloc == "api.apify.com"
        assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
        assert TOKEN not in url
        assert kwargs["timeout"] == 30 and kwargs["max_bytes"] == dev.MAX_RESPONSE
        data = None
        if method == "GET":
            if path == "/users/me":
                data = {"id": self.account, "proxyPassword": "never-return", "token": TOKEN}
            elif path in ("/actors", "/store"):
                data = {"items": [self.actor], "total": 1}
            elif path == "/actors/" + ACTOR:
                data = self.actor
            elif path == "/actor-builds/" + BUILD:
                data = self.build
            elif path == "/actor-runs/" + RUN:
                data = self.run
            elif path == f"/actors/{ACTOR}/versions/0.1":
                data = self.source
            elif path == f"/actors/{ACTOR}/builds":
                data = {"items": [self.build], "total": 1}
            elif path == f"/actors/{ACTOR}/runs":
                data = {"items": [self.run], "total": 1}
            elif path.startswith("/logs/"):
                return self.log
            elif path == f"/datasets/{DATASET}/items":
                return json.dumps(self.rows).encode()
            elif path == "/users/me/usage/monthly":
                data = {"usageCycle": {"startAt": "2026-09-01", "endAt": "2026-09-30"},
                        "totalUsageCreditsUsdAfterVolumeDiscount": 1.25,
                        "monthlyServiceUsage": {"ACTOR_COMPUTE_UNITS": {"quantity": 5, "amountAfterVolumeDiscountUsd": 1.25}},
                        "dailyServiceUsages": [{"date": "2026-09-14", "totalUsageCreditsUsd": 1.25,
                            "serviceUsage": {"ACTOR_COMPUTE_UNITS": {"quantity": 5, "amountAfterVolumeDiscountUsd": 1.0}}}]}
            elif path == "/users/me/limits":
                data = {"limits": {"maxMonthlyUsageUsd": 10, "dataRetentionDays": 7}, "current": {"activeActorJobCount": 1}}
        elif method == "POST":
            if path == "/actors":
                data = self.actor
            elif path.endswith("/versions"):
                data = self.source
            elif path.endswith("/builds"):
                assert len(params["tag"][0]) <= 30
                data = self.build
            elif path.endswith("/runs"):
                data = self.run
        elif method == "PUT" and path == "/actors/" + ACTOR:
            if "pricingInfos" in body:
                self.actor["pricingInfos"] = copy.deepcopy(body["pricingInfos"])
            data = self.actor
        assert data is not None, (method, path)
        return json.dumps({"data": data}).encode()


class ApifyDeveloperTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI()
        self.api.config["APIFY_API_TOKEN"] = TOKEN
        self.provider = Provider()
        self.tool = dev.ApifyDeveloperTool()
        self.mock = patch.object(dev, "request_bytes", self.provider)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def execute(self, action, values=None):
        return self.tool.execute(action, values or {}, self.api)

    def approve(self, result):
        self.assertIsInstance(result, ActionPendingApproval)
        record = self.api.approvals.approve(result.approval_id)
        return self.tool.execute_approved(record, self.api)

    def writes(self):
        return [c for c in self.provider.calls if c[0] != "GET"]

    def pricing_input(self):
        return {"actor_id": ACTOR,
                "minimum_run_budget_usd": 0.01,
                "events": [{"name": "result", "title": "Saved result", "description": "One saved row",
                            "price_usd": 0.001, "primary": True, "one_time": False}]}

    def test_pricing_approval_sends_only_the_approved_new_period(self):
        result = self.execute("set_monetization", self.pricing_input())
        self.assertEqual(self.writes(), [])
        self.assertIn("SINGLE", result.summary)
        approved = self.approve(result)
        self.assertIsInstance(approved, ApprovalExecuted)
        write = self.writes()[0]
        self.assertEqual(set(write[3]), {"pricingInfos"})
        self.assertEqual(len(write[3]["pricingInfos"]), 1)
        self.assertEqual(write[3]["pricingInfos"][0]["apifyMarginPercentage"], 0.2)
        self.assertEqual({k: v for k, v in write[3]["pricingInfos"][0].items() if k not in ("createdAt", "startedAt")},
                         self.api.approvals.get(result.approval_id).payload["pricing_entry"])
        self.assertEqual(self.provider.calls[-1][0], "GET")
        self.assertFalse(self.provider.actor["isPublic"])
        readback = self.execute("get_monetization", {"actor_id": ACTOR})
        assert_matches_output_schema(self, dev.MANIFEST, "get_monetization", readback)
        self.assertEqual(len(json.loads(readback.result["pricing_json"])), 1)

    def test_provider_history_never_reaches_approval_or_update(self):
        for extra in (
            {"pricingPerEvent": {"actorChargeEvents": {"old": {"eventTitle": TOKEN}}}},
            {"pricingPerEvent": {"actorChargeEvents": {"old": {"eventDescription": "Bearer historical-token"}}}},
            {"providerMetadata": [{"nested": "ghp_TEST_HISTORICAL_SECRET"}]},
            {TOKEN: "secret in a provider-added key"},
            {"providerMetadata": {"private_note": "historical customer@example.com"}},
        ):
            with self.subTest(extra_fields=list(extra)):
                original = Provider().actor["pricingInfos"][0]
                original.update(extra)
                self.provider.actor["pricingInfos"] = [copy.deepcopy(original)]
                result = self.execute("set_monetization", self.pricing_input())
                self.assertIsInstance(result, ActionPendingApproval)
                record = self.api.approvals.get(result.approval_id)
                self.assertEqual(set(record.payload), {"action", "account_id", "input", "pricing_entry"})
                serialized = json.dumps(record.payload)
                for marker in (TOKEN, "Bearer historical-token", "ghp_TEST_HISTORICAL_SECRET", "customer@example.com", "providerMetadata"):
                    self.assertNotIn(marker, serialized)
                self.assertNotIn("pricingInfos", serialized)
                self.assertIn("pricingPerEvent", record.payload["pricing_entry"])
                self.assertEqual(self.provider.actor["pricingInfos"], [original])
                self.assertIsInstance(self.approve(result), ApprovalExecuted)
                self.assertEqual({k: v for k, v in self.writes()[-1][3]["pricingInfos"][0].items() if k not in ("createdAt", "startedAt")},
                                 record.payload["pricing_entry"])

    def test_pricing_new_text_uses_parameter_guard_before_approval(self):
        for field in ("title", "description"):
            values = self.pricing_input()
            values["events"][0][field] = "verify AKIAIOSFODNN7EXAMPLE now"
            result = self.execute("set_monetization", values)
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("credential", result.error)
        self.assertEqual(self.api.approvals.records, {})
        self.assertEqual(self.provider.calls, [])

    def test_pricing_modified_approval_entry_cannot_change_write(self):
        result = self.execute("set_monetization", self.pricing_input())
        record = self.api.approvals.get(result.approval_id)
        record.payload["pricing_entry"]["minimalMaxTotalChargeUsd"] = 5
        self.assertIsInstance(self.approve(result), ActionFailed)
        self.assertEqual(self.writes(), [])

    def test_pricing_tiers_and_synthetic_start(self):
        values = self.pricing_input()
        result = values["events"][0]
        del result["price_usd"]
        result["tier_prices_usd"] = {t: 0.002 for t in dev.pricing.TIERS}
        values["events"].append({"name": "apify-actor-start", "title": "Start", "description": "Run start",
                                  "price_usd": 0.00005, "primary": False, "one_time": True})
        self.assertIsInstance(self.approve(self.execute("set_monetization", values)), ApprovalExecuted)
        events = self.writes()[0][3]["pricingInfos"][-1]["pricingPerEvent"]["actorChargeEvents"]
        self.assertNotIn("eventPriceUsd", events["result"])
        self.assertEqual(events["result"]["eventTieredPricingUsd"]["GOLD"], {"tieredEventPriceUsd": 0.002})

    def test_pricing_rejects_bad_inputs_before_http(self):
        mutations = [lambda v: v.update(minimum_run_budget_usd=True),
                     lambda v: v.update(effective_at="2026-09-23"),
                     lambda v: v["events"][0].update(price_usd=float("nan")),
                     lambda v: v["events"][0].update(price_usd=-1),
                     lambda v: v["events"][0].update(primary=False),
                     lambda v: v["events"].append(copy.deepcopy(v["events"][0])),
                     lambda v: v["events"][0].update(tier_prices_usd={}),
                     lambda v: v["events"][0].update(name="apify-secret"),
                     lambda v: v["events"][0].update(forceContainsSignificantPriceChange=True)]
        for mutate in mutations:
            values = self.pricing_input()
            mutate(values)
            self.assertIsInstance(self.execute("set_monetization", values), ActionFailed)
        self.assertEqual(self.provider.calls, [])

    def test_pricing_account_and_owner_rechecked(self):
        for change in ("account", "owner"):
            with self.subTest(change=change):
                self.provider = Provider()
                self.mock.stop()
                self.mock = patch.object(dev, "request_bytes", self.provider)
                self.mock.start()
                values = self.pricing_input()
                result = self.execute("set_monetization", values)
                if change == "account":
                    self.provider.account = OTHER
                elif change == "owner":
                    self.provider.actor["userId"] = OTHER
                approved = self.approve(result)
                self.assertIsInstance(approved, ActionFailed)
                self.assertEqual(self.writes(), [])
        self.mock.stop()

    def test_pricing_does_not_require_previous_records_or_margin(self):
        for mode in ("missing", "null", "empty", "margin", "large"):
            with self.subTest(mode=mode):
                self.provider.actor = Provider().actor
                if mode == "missing":
                    del self.provider.actor["pricingInfos"]
                elif mode == "null":
                    self.provider.actor["pricingInfos"] = None
                elif mode == "empty":
                    self.provider.actor["pricingInfos"] = []
                elif mode == "margin":
                    del self.provider.actor["pricingInfos"][0]["apifyMarginPercentage"]
                else:
                    self.provider.actor["pricingInfos"] *= 1000
                result = self.execute("set_monetization", self.pricing_input())
                self.assertIsInstance(result, ActionPendingApproval)
                self.assertIsInstance(self.approve(result), ApprovalExecuted)
                self.assertEqual(len(self.writes()[-1][3]["pricingInfos"]), 1)
                self.assertEqual(self.writes()[-1][3]["pricingInfos"][0]["apifyMarginPercentage"], 0.2)

    def test_pricing_starts_at_execution_even_after_delayed_approval(self):
        for public in (False, True):
            with self.subTest(public=public):
                self.provider.actor["isPublic"] = public
                pending = self.execute("set_monetization", self.pricing_input())
                entry = self.api.approvals.get(pending.approval_id).payload["pricing_entry"]
                self.assertNotIn("startedAt", entry)
                executed_at = datetime.now(timezone.utc) + timedelta(days=3)
                with patch.object(dev.pricing, "now", return_value=executed_at):
                    self.assertIsInstance(self.approve(pending), ApprovalExecuted)
                written = self.writes()[-1][3]["pricingInfos"][0]
                self.assertEqual(written["startedAt"], executed_at.isoformat(timespec="milliseconds"))
                self.assertEqual(written["createdAt"], written["startedAt"])

    def test_pricing_provider_rejection_does_not_schedule_or_retry(self):
        pending = self.execute("set_monetization", self.pricing_input())
        writes = []
        def reject(method, url, **kwargs):
            if method == "PUT":
                writes.append(json.loads(kwargs["data"]))
                raise WebRequestError("Provider rejects immediate pricing", status=400)
            return self.provider(method, url, **kwargs)
        with patch.object(dev, "request_bytes", reject):
            self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertEqual(len(writes), 1)

    def test_monetization_read_returns_current_and_next_not_history(self):
        past = Provider().actor["pricingInfos"][0]
        current = dict(past, startedAt="2025-02-01T00:00:00Z")
        future = dict(past, startedAt=(datetime.now(timezone.utc) + timedelta(days=15)).isoformat())
        # Provider order is not part of the read contract.
        self.provider.actor["pricingInfos"] = [future, past, current]
        result = self.execute("get_monetization", {"actor_id": ACTOR})
        self.assertEqual(json.loads(result.result["pricing_json"]), [current, future])
        for missing in (None, []):
            self.provider.actor["pricingInfos"] = missing
            result = self.execute("get_monetization", {"actor_id": ACTOR})
            self.assertEqual(json.loads(result.result["pricing_json"]), [])

    def test_pricing_readback_ignores_old_periods_and_platform_metadata(self):
        pending = self.execute("set_monetization", self.pricing_input())
        def with_history(method, url, **kwargs):
            response = self.provider(method, url, **kwargs)
            if method == "PUT":
                self.provider.actor["pricingInfos"][0]["apifyMarginPercentage"] = 0.2
                self.provider.actor["pricingInfos"].insert(0, Provider().actor["pricingInfos"][0])
            return response
        with patch.object(dev, "request_bytes", with_history):
            self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.assertEqual(len(self.writes()[0][3]["pricingInfos"]), 1)

    def test_pricing_readback_rejects_a_retained_future_price(self):
        pending = self.execute("set_monetization", self.pricing_input())
        def retained_future(method, url, **kwargs):
            response = self.provider(method, url, **kwargs)
            if method == "PUT":
                future = copy.deepcopy(self.provider.actor["pricingInfos"][0])
                future["startedAt"] = (datetime.now(timezone.utc) + timedelta(days=15)).isoformat()
                self.provider.actor["pricingInfos"].append(future)
            return response
        with patch.object(dev, "request_bytes", retained_future):
            result = self.approve(pending)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("do not repeat", result.error)
        self.assertEqual(len(self.writes()), 1)

    def test_pricing_readback_mismatch_is_not_success_or_retried(self):
        result = self.execute("set_monetization", self.pricing_input())
        real = self.provider
        def changed(method, url, **kwargs):
            response = real(method, url, **kwargs)
            if method == "PUT":
                real.actor["pricingInfos"][-1]["minimalMaxTotalChargeUsd"] = 5
            return response
        with patch.object(dev, "request_bytes", changed):
            approved = self.approve(result)
        self.assertIsInstance(approved, ActionFailed)
        self.assertIn("do not repeat", approved.error)
        self.assertEqual(len(self.writes()), 1)

    def test_completed_run_reports_provider_cost_once_across_polls(self):
        self.provider.run["usageTotalUsd"] = 0.25
        self.provider.run["finishedAt"] = datetime.now(timezone.utc).isoformat()
        self.execute("get_run", {"run_id": RUN})
        self.assertEqual(self.api.costs.records["run:" + RUN]["amount_usd"], "0.250000000")
        self.execute("get_run", {"run_id": RUN})
        self.assertEqual(len(self.api.costs.records), 1)

    def test_direct_json_contracts_and_metrics_population(self):
        cases = [("get_monetization", {"actor_id": ACTOR}), ("get_account_usage", {}), ("search_store", {"query": "exhibitors", "sort": "newest"}),
                 ("list_actors", {}), ("get_actor", {"actor_id": ACTOR}),
                 ("list_builds", {"actor_id": ACTOR}), ("get_build", {"build_id": BUILD}),
                 ("list_runs", {"actor_id": ACTOR}), ("get_run", {"run_id": RUN}),
                 ("read_log", {"kind": "build", "job_id": BUILD})]
        self.assertEqual({a.id for a in dev.MANIFEST.actions if a.approval == "direct" and not a.returns_asset}, {a for a, _ in cases})
        for action, values in cases:
            with self.subTest(action=action):
                result = self.execute(action, values)
                assert_matches_output_schema(self, dev.MANIFEST, action, result)
                self.assertNotIn(TOKEN, json.dumps(result.result))
        actor = self.execute("get_actor", {"actor_id": ACTOR}).result
        self.assertEqual(actor["actor"]["stats"]["total_runs"], 12)
        self.assertIsNone(actor["actor"]["stats"]["total_users"])
        self.assertIn("free", actor["metrics_scope"])
        run = self.execute("get_run", {"run_id": RUN}).result["run"]
        self.assertEqual(run["usage_usd"], 0)

    def test_daily_usage_uses_documented_daily_total(self):
        # The provider's required daily total is authoritative, not a sum of
        # optional per-service discounted amounts.
        result = self.execute("get_account_usage").result
        self.assertEqual(result["daily_usage"], [{"date": "2026-09-14", "usage_usd": 1.25}])

    def test_page_cursor_never_exceeds_accepted_offset(self):
        for offset, expected in ((9999, 10000), (10000, None)):
            with self.subTest(offset=offset):
                page = dev._page({"items": [self.provider.actor], "total": 20000},
                                 {"limit": 1, "offset": offset}, dev._actor_result, dev.SCOPE)
                self.assertEqual(page["next_offset"], expected)

    def test_public_run_counts_keep_owner_exclusion_and_missing_distinct(self):
        self.provider.actor["stats"]["publicActorRunStats30Days"] = {"TOTAL": 30, "SUCCEEDED": 29, "FAILED": 1, "ABORTED": 0}
        result = self.execute("get_actor", {"actor_id": ACTOR}).result["actor"]
        self.assertEqual(result["public_runs_30_days"], {"total": 30, "succeeded": 29, "failed": 1, "aborted": 0, "timed_out": None})
        self.assertIsInstance(self.execute("create_actor", {"name": "task-run-monitor", "title": "Task-based workflow", "description": "Task-run data"}), ActionPendingApproval)

    def test_manifest_uses_supported_closed_schemas_and_complete_disclosure(self):
        self.assertEqual(len(dev.MANIFEST.data_summary.cards), 4)
        self.assertTrue(dev.MANIFEST.setup_steps[-1].show_config)
        for action in dev.MANIFEST.actions:
            self.assertEqual(unsupported_schema_error(action.input_schema), "")
            if action.output_schema:
                self.assertEqual(unsupported_schema_error(action.output_schema), "")
        run = dev.MANIFEST.action("run_actor")
        self.assertIn("not a cumulative budget", run.data_policy)
        self.assertIn("network policy", run.data_policy)

    def test_invalid_inputs_never_reach_provider(self):
        cases = [("get_actor", {"actor_id": "../users/me"}), ("get_build", {"build_id": "user~actor"}),
                 ("list_actors", {"limit": True}), ("list_actors", {"offset": 10001}),
                 ("list_runs", {"actor_id": ACTOR, "limit": 51}),
                 ("search_store", {"sort": "unsupported"}), ("read_log", {"kind": "task", "job_id": RUN}),
                 ("get_actor", {"actor_id": ACTOR, "endpoint": "https://evil.example"}),
                 ("create_actor", {"name": "a/../b", "title": "Test", "description": "Test"}),
                 ("build_actor", {"actor_id": ACTOR, "version": "1.1/../../users"})]
        for action, values in cases:
            with self.subTest(action=action, values=values):
                self.assertIsInstance(self.execute(action, values), ActionFailed)
        self.assertEqual(self.provider.calls, [])

    def test_search_and_recursive_run_input_guard_before_any_provider_request(self):
        for secret in (TOKEN, "sk-proj-" + "A" * 80):
            self.assertIsInstance(self.execute("search_store", {"query": secret}), ActionFailed)
            for payload in ({"nested": [{"value": secret}]}, {secret: "public"},
                            {"url": "https://example.org/?q=" + urllib.parse.quote(secret, safe="")}):
                self.assertIsInstance(self.execute("run_actor", {"build_id": BUILD, "input_json": json.dumps(payload)}), ActionFailed)
        encoded = "https://example.org/?q=%2573%256b%252d%2570%2572%256f%256a%252d" + "A" * 80
        self.assertIsInstance(self.execute("run_actor", {"build_id": BUILD, "input_json": json.dumps({"url": encoded})}), ActionFailed)
        self.assertEqual(self.provider.calls, [])

    def test_run_json_limits_and_unsafe_keys(self):
        payloads = ['{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '[]', '{"x":1000000001}',
                    '{"x":"%GG"}', '{"x":"%FF"}', '{"x":"' + 'a' * 9000 + '"}',
                    json.dumps({"x": list(range(201))}), '{"a":' * 8 + '{}' + '}' * 8]
        payloads += [json.dumps({"nested": [{key: "anything"}]}) for key in ("apiToken", "cookies", "pageFunction", "headers", "webhooks", "proxyConfiguration")]
        for payload in payloads:
            with self.subTest(payload=payload[:60]):
                self.assertIsInstance(self.execute("run_actor", {"build_id": BUILD, "input_json": payload}), ActionFailed)
        self.assertEqual(self.provider.calls, [])

    def test_run_approval_pins_owned_build_exact_input_and_execution_limits(self):
        original = {"urls": ["https://example.org"], "maxItems": 10}
        result = self.execute("run_actor", {"build_id": BUILD, "input_json": json.dumps(original),
                              "timeout_seconds": 120, "memory_mb": 1024, "max_charge_usd": 0.5})
        self.assertIsInstance(result, ActionPendingApproval)
        self.assertEqual(self.writes(), [])
        record = self.api.approvals.get(result.approval_id)
        self.assertEqual(record.payload["input"]["input_json"], json.dumps(original))
        self.assertIn("$0.50", record.summary)
        self.assertIsInstance(self.approve(result), ApprovalExecuted)
        self.assertEqual(len(self.writes()), 1)
        _, path, params, body, _ = self.writes()[0]
        self.assertEqual(path, f"/actors/{ACTOR}/runs")
        self.assertEqual(body, original)
        self.assertEqual(params, {"build": ["0.1.7"], "timeout": ["120"], "memory": ["1024"],
                                 "maxTotalChargeUsd": ["0.5"], "restartOnError": ["false"],
                                 "forcePermissionLevel": ["LIMITED_PERMISSIONS"], "waitForFinish": ["0"]})

    def test_run_approval_rechecks_account_build_ownership_and_input(self):
        for record, key, value in ((self.provider.actor, "userId", OTHER),
                                   (self.provider.build, "status", "RUNNING"),
                                   (self.provider.build, "buildNumber", "0.1.8")):
            pending = self.execute("run_actor", {"build_id": BUILD, "input_json": "{}"})
            before = record[key]
            record[key] = value
            self.assertIsInstance(self.approve(pending), ActionFailed)
            record[key] = before
        pending = self.execute("run_actor", {"build_id": BUILD, "input_json": "{}"})
        self.provider.account = OTHER
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.provider.account = ACCOUNT
        pending = self.execute("run_actor", {"build_id": BUILD, "input_json": "{}"})
        record = self.api.approvals.get(pending.approval_id)
        record.payload["input"]["input_json"] = '{"token":"secret"}'
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertEqual(self.writes(), [])

    def test_run_sends_approved_json_bytes_without_normalizing_numbers(self):
        raw = '{ "value": -0, "fraction": 0.1234567890123456789, "label": "café" }'
        pending = self.execute("run_actor", {"build_id": BUILD, "input_json": raw})
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.assertEqual(self.writes()[0][4]["data"], raw.encode("utf-8"))

    def test_build_and_run_pages_describe_their_own_scope(self):
        builds = self.execute("list_builds", {"actor_id": ACTOR})
        runs = self.execute("list_runs", {"actor_id": ACTOR})
        self.assertEqual(builds.result["metrics_scope"], dev.BUILD_SCOPE)
        self.assertEqual(runs.result["metrics_scope"], dev.RUN_SCOPE)

    def test_run_rejects_overrides_foreign_jobs_and_unbuilt_code(self):
        for key, value in (("timeout_seconds", 0), ("timeout_seconds", 121), ("memory_mb", 129),
                           ("max_charge_usd", 0), ("max_charge_usd", 0.51), ("max_charge_usd", True),
                           ("max_charge_usd", float("nan")), ("max_charge_usd", float("inf"))):
            self.assertIsInstance(self.execute("run_actor", {"build_id": BUILD, "input_json": "{}", key: value}), ActionFailed)
        for record, key, value in ((self.provider.build, "userId", OTHER), (self.provider.actor, "userId", OTHER),
                                   (self.provider.build, "status", "RUNNING"), (self.provider.build, "buildNumber", "latest")):
            before = record.get(key)
            record[key] = value
            self.assertIsInstance(self.execute("run_actor", {"build_id": BUILD, "input_json": "{}"}), ActionFailed)
            record[key] = before
        self.assertEqual(self.writes(), [])

    def test_create_private_actor_queues_exact_payload_and_binds_account(self):
        values = {"name": "test-actor", "title": "Test", "description": "Public data"}
        result = self.execute("create_actor", values)
        self.assertEqual(self.writes(), [])
        values["title"] = "changed locally"
        self.assertIsInstance(self.approve(result), ApprovalExecuted)
        self.assertEqual(self.writes()[0][3], {"name": "test-actor", "title": "Test", "description": "Public data", "isPublic": False, "actorPermissionLevel": "LIMITED_PERMISSIONS"})
        pending = self.execute("create_actor", {"name": "second-actor", "title": "Other", "description": "Other"})
        self.provider.account = OTHER
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertEqual(len(self.writes()), 1)

    def source_input(self):
        return {"actor_id": ACTOR, "version": "0.1", "files": [{"path": "src/main.js", "content": "console.log('hello')"}]}

    def test_source_upload_is_create_only_reviewed_text_with_no_default_tag(self):
        result = self.execute("create_version", self.source_input())
        self.assertEqual(self.writes(), [])
        self.assertIsInstance(self.approve(result), ApprovalExecuted)
        payload = self.writes()[0][3]
        self.assertEqual(payload["sourceFiles"][0], {"name": "src/main.js", "format": "TEXT", "content": "console.log('hello')"})
        self.assertIsNone(payload["buildTag"])
        self.assertFalse(payload["applyEnvVarsToBuild"])
        pending = self.execute("create_version", self.source_input())
        self.provider.actor["versions"] = [{"versionNumber": "0.1"}]
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertEqual(len(self.writes()), 1)

    def test_source_paths_sizes_duplicates_and_secrets_are_rejected(self):
        for path in ("../main.js", "/main.js", "src//main.js", "src/./main.js", "src\\main.js", "https://source.example/a", "a" * 161):
            values = self.source_input()
            values["files"][0]["path"] = path
            self.assertIsInstance(self.execute("create_version", values), ActionFailed)
        for files in ([], [{"path": "a", "content": "x"}] * 2, [{"path": "a", "content": TOKEN}],
                      [{"path": "a", "content": "\\" * 30000}], [{"path": "a", "content": "x", "format": "BASE64"}]):
            self.assertIsInstance(self.execute("create_version", {**self.source_input(), "files": files}), ActionFailed)
        self.assertEqual(self.provider.calls, [])

    def test_build_approval_includes_code_and_rejects_changed_source(self):
        pending = self.execute("build_actor", {"actor_id": ACTOR, "version": "0.1"})
        record = self.api.approvals.get(pending.approval_id)
        self.assertEqual(record.payload["source_for_review"], self.provider.source)
        self.provider.source["sourceFiles"][0]["content"] = "changed()"
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertEqual(self.writes(), [])
        pending = self.execute("build_actor", {"actor_id": ACTOR, "version": "0.1"})
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.assertRegex(self.writes()[0][2]["tag"][0], r"^kern-candidate-[a-f0-9]{15}$")
        self.assertLessEqual(len(self.writes()[0][2]["tag"][0]), 30)
        self.assertEqual(self.writes()[0][2]["waitForFinish"], ["0"])

    def test_build_rejects_git_source_and_environment_secrets(self):
        for update in ({"sourceType": "GIT_REPO"}, {"envVars": [{"name": "SECRET", "value": "hidden"}]}, {"applyEnvVarsToBuild": True}):
            before = copy.deepcopy(self.provider.source)
            self.provider.source.update(update)
            self.assertIsInstance(self.execute("build_actor", {"actor_id": ACTOR, "version": "0.1"}), ActionFailed)
            self.provider.source = before
        self.assertEqual(self.api.approvals.records, {})

    def test_approval_fingerprints_ignore_recursive_object_key_order(self):
        def reordered(value):
            if isinstance(value, dict):
                return {k: reordered(v) for k, v in reversed(list(value.items()))}
            if isinstance(value, list):
                return [reordered(v) for v in value]
            return value
        pending = self.execute("build_actor", {"actor_id": ACTOR, "version": "0.1"})
        self.provider.source = reordered(self.provider.source)
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.provider.actor["defaultRunOptions"] = {"memoryMbytes": 512, "build": "latest"}
        self.provider.actor["taggedBuilds"] = {"stable": {"buildId": OTHER, "buildNumber": "0.0.1"}}
        pending = self.execute("publish_actor", self.release_input())
        self.provider.actor = reordered(self.provider.actor)
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)

    def release_input(self):
        return {"actor_id": ACTOR, "build_id": BUILD, "test_run_id": RUN, "title": "Test", "description": "Public data", "categories": ["DEVELOPER_TOOLS"]}

    def latest_input(self):
        return {"actor_id": ACTOR, "build_id": BUILD, "test_run_id": RUN}

    def test_private_latest_changes_only_tag_after_approval(self):
        self.provider.actor.update(taggedBuilds={"latest": {"buildId": OTHER}, "stable": {"buildId": OTHER}},
                                   defaultRunOptions={"build": "0.0.1", "maxTotalChargeUsd": 0.1})
        pending = self.execute("set_latest_build", self.latest_input())
        self.assertEqual(self.writes(), [])
        self.assertIn("keeping the Actor private", pending.summary)
        self.assertIn("Future runs selecting latest", pending.summary)
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.assertEqual([(c[0], c[1], c[3]) for c in self.writes()], [
            ("PUT", "/actors/" + ACTOR, {"taggedBuilds": {"latest": {"buildId": BUILD}}})])

    def test_private_latest_requires_owned_private_tested_release_at_both_phases(self):
        cases = [("actor", "isPublic", True), ("actor", "isPublic", None),
                 ("actor", "userId", OTHER), ("build", "userId", OTHER),
                 ("run", "userId", OTHER), ("build", "actId", OTHER),
                 ("run", "actId", OTHER), ("run", "buildId", OTHER),
                 ("build", "status", "FAILED"), ("run", "status", "RUNNING"),
                 ("build", "readme", "")]
        for phase in ("request", "approval"):
            for target, key, value in cases:
                with self.subTest(phase=phase, target=target, key=key, value=value):
                    pending = self.execute("set_latest_build", self.latest_input()) if phase == "approval" else None
                    obj = getattr(self.provider, target)
                    before = obj[key]
                    obj[key] = value
                    result = self.approve(pending) if pending else self.execute("set_latest_build", self.latest_input())
                    self.assertIsInstance(result, ActionFailed)
                    obj[key] = before
        self.assertEqual(self.writes(), [])

    def test_private_latest_rechecks_account_and_reviewed_settings(self):
        for target, key, value in ((self.provider, "account", OTHER),
                                  (self.provider.actor, "taggedBuilds", {"latest": {"buildId": OTHER}}),
                                  (self.provider.actor, "defaultRunOptions", {"build": "0.0.1"})):
            pending = self.execute("set_latest_build", self.latest_input())
            if isinstance(target, dict):
                target[key] = value
            else:
                setattr(target, key, value)
            self.assertIsInstance(self.approve(pending), ActionFailed)
            self.provider.account = ACCOUNT
        self.assertEqual(self.writes(), [])

    def test_actor_reports_latest_build_without_exposing_tag_metadata(self):
        for latest, expected in ((None, None), ({"buildId": BUILD, "other": TOKEN}, BUILD),
                                 ({"buildId": "malformed"}, None)):
            self.provider.actor["taggedBuilds"] = {"latest": latest}
            result = self.execute("get_actor", {"actor_id": ACTOR})
            assert_matches_output_schema(self, dev.MANIFEST, "get_actor", result)
            self.assertEqual(result.result["actor"]["latest_build_id"], expected)
            self.assertNotIn(TOKEN, json.dumps(result.result))

    def test_publication_requires_exact_test_and_rechecks_mutable_listing(self):
        self.provider.run["buildId"] = OTHER
        self.assertIsInstance(self.execute("publish_actor", self.release_input()), ActionFailed)
        self.provider.run["buildId"] = BUILD
        pending = self.execute("publish_actor", self.release_input())
        self.provider.actor["description"] = "Changed in Console"
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertEqual(self.writes(), [])
        pending = self.execute("publish_actor", self.release_input())
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.assertEqual(self.writes()[0][3]["taggedBuilds"], {"latest": {"buildId": BUILD}})
        self.assertTrue(self.writes()[0][3]["isPublic"])

    def test_publication_updates_default_build_and_detects_changed_run_defaults(self):
        self.provider.actor["defaultRunOptions"] = {
            "build": "0.0.1", "forcePermissionLevel": "FULL_PERMISSIONS",
            "timeoutSecs": 300, "memoryMbytes": 2048, "restartOnError": False,
            "maxTotalChargeUsd": 0.35,
        }
        pending = self.execute("publish_actor", self.release_input())
        self.provider.actor["defaultRunOptions"]["build"] = "0.0.2"
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertEqual(self.writes(), [])
        pending = self.execute("publish_actor", self.release_input())
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.assertEqual(self.writes()[0][3]["defaultRunOptions"], {
            "build": "latest", "forcePermissionLevel": "LIMITED_PERMISSIONS",
            "timeoutSecs": 300, "memoryMbytes": 2048, "restartOnError": False,
            "maxTotalChargeUsd": 0.35,
        })

    def test_publication_accepts_readme_from_immutable_build_snapshot(self):
        self.provider.build.pop("readme")
        self.assertIsInstance(self.execute("publish_actor", self.release_input()), ActionFailed)
        # A README in the current source is not evidence about the tested build.
        self.provider.source["sourceFiles"].append({"name": "README.md", "format": "TEXT", "content": "# New version"})
        self.assertIsInstance(self.execute("publish_actor", self.release_input()), ActionFailed)
        self.provider.build["actVersion"] = {"sourceFiles": [{"name": ".actor/README.md", "format": "TEXT", "content": "# Tested build"}]}
        pending = self.execute("publish_actor", self.release_input())
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.provider.build["actVersion"]["sourceFiles"][0]["content"] = "   "
        self.assertIsInstance(self.execute("publish_actor", self.release_input()), ActionFailed)

    def test_logs_and_exports_are_account_scoped_and_redact_token(self):
        self.provider.run["userId"] = OTHER
        for action, values in (("get_run", {"run_id": RUN}), ("read_log", {"kind": "run", "job_id": RUN}), ("export_results", {"run_id": RUN})):
            self.assertIsInstance(self.execute(action, values), ActionFailed)
        self.assertFalse(any(c[1].startswith(("/datasets/", "/logs/")) for c in self.provider.calls))
        self.provider.run["userId"] = ACCOUNT
        self.provider.log = (TOKEN + "\n" + "x" * 17000).encode()
        result = self.execute("read_log", {"kind": "run", "job_id": RUN})
        self.assertNotIn(TOKEN, result.result["text"])
        self.assertTrue(result.result["truncated"])
        self.provider.log = ("😀" * 16000).encode()
        result = self.execute("read_log", {"kind": "run", "job_id": RUN})
        self.assertTrue(result.result["truncated"])
        self.assertLess(len(json.dumps(result.result).encode()), 64 * 1024)
        self.provider.rows = [{"title": "test", "accidental_key": TOKEN}]
        result = self.execute("export_results", {"run_id": RUN})
        self.assertIsInstance(result, StreamingAsset)
        with result.open_stream() as asset:
            data = asset.source.read()
            self.assertEqual(asset.size_bytes, len(data))
            self.assertNotIn(TOKEN.encode(), data)
            self.assertEqual(json.loads(data)[0]["title"], "test")

    def test_provider_errors_are_redacted_and_never_retried(self):
        for status in (0, 400, 401, 402, 403, 408, 429, 500):
            with patch.object(dev, "request_bytes", side_effect=WebRequestError(TOKEN, status=status, body=TOKEN.encode())) as request:
                result = self.execute("search_store", {})
                self.assertIsInstance(result, ActionFailed)
                self.assertNotIn(TOKEN, result.error)
                self.assertEqual(request.call_count, 1)

    def test_escaped_dataset_credentials_are_redacted_after_json_decoding(self):
        original = self.provider
        def provider(method, url, **kwargs):
            if "/datasets/" in url:
                escaped = "".join("\\u%04x" % ord(c) for c in TOKEN)
                return ('[{"echo":"' + escaped + '"}]').encode()
            return original(method, url, **kwargs)
        with patch.object(dev, "request_bytes", provider):
            result = self.execute("export_results", {"run_id": RUN})
            with result.open_stream() as asset:
                self.assertEqual(json.loads(asset.source.read()), [{"echo": "[redacted]"}])

    def test_dataset_export_preserves_numeric_tokens_while_redacting_strings(self):
        original = self.provider
        raw = (b'[{"decimal":0.1234567890123456789,"negativeZero":-0,'
               b'"integer":123456789012345678901234567890,"exponent":1e400,'
               b'"text":"escaped \\"quote\\" and 0.125", "secret":"' + TOKEN.encode() + b'"}]')
        def provider(method, url, **kwargs):
            return raw if "/datasets/" in url else original(method, url, **kwargs)
        with patch.object(dev, "request_bytes", provider):
            result = self.execute("export_results", {"run_id": RUN})
            self.assertIsInstance(result, StreamingAsset)
            with result.open_stream() as asset:
                self.assertEqual(asset.source.read(), raw.replace(TOKEN.encode(), b"[redacted]"))

    def test_page_byte_limit_preserves_resume_offset(self):
        actor = copy.deepcopy(self.provider.actor)
        actor["description"] = "😀" * 300
        data = {"items": [actor] * 50, "total": 100}
        page = dev._page(data, {"limit": 50, "offset": 10}, dev._actor_result, dev.SCOPE)
        self.assertLess(len(json.dumps(page).encode()), 64 * 1024)
        self.assertEqual(page["next_offset"], 10 + len(page["items"]))
        self.assertLess(len(page["items"]), 50)

    def test_oversize_malformed_or_nonfinite_provider_data_fails_safely(self):
        for response in (b"x" * (dev.MAX_RESPONSE + 1), b"not json", b'{"data":[],"data":{}}',
                         b'{"data":{"items":[],"total":NaN}}', b'{"data":{"items":[],"total":1e999}}'):
            with patch.object(dev, "request_bytes", return_value=response):
                self.assertIsInstance(self.execute("search_store", {}), ActionFailed)


if __name__ == "__main__":
    unittest.main()
