"""Mocked X Ads workflows and approval integrity; never live ad spend."""

from __future__ import annotations

import copy
import json
import urllib.parse
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from host.runtime.tools import tools_host
from host.tools import x_ads
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.web import WebRequestError
from host.tools.x_ads import api as transport, tool
from host.tools.x_ads.manifest import MANIFEST
from test_tools import FakeHostAPI, assert_matches_output_schema

ACCOUNT_ID = "18ce54d4x5t"
POST_ID = "1166476031668015104"
USER_ID = "756201191646691328"
GEO = "96683cc9126741d1"
START = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
END = (datetime.now(timezone.utc) + timedelta(days=5)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_api():
    return FakeHostAPI(config={key: "fake-" + key for key in transport.CONFIG_KEYS})


def plan(**changes):
    return {"account_id": ACCOUNT_ID, "funding_instrument_id": "fund1", "promotable_user_id": "prom1", "post_id": POST_ID,
            "name": "Kern promotion", "daily_budget_amount_local_micro": "10000000", "total_budget_amount_local_micro": "50000000",
            "start_time": START, "end_time": END, "targeting": [{"type": "LOCATION", "value": GEO}, {"type": "PHRASE_KEYWORD", "value": "agent tools"}], **changes}


class AdsProvider:
    """Exercise real signing/transport and workflows against provider responses."""
    def __init__(self, *, empty=False):
        self.calls = []
        self.account = {"id": ACCOUNT_ID, "name": "Kern advertiser", "timezone": "America/Los_Angeles", "approval_status": "ACCEPTED", "deleted": False}
        self.access = {"user_id": USER_ID, "permissions": ["ACCOUNT_ADMIN"]}
        self.funding = {"id": "fund1", "type": "CREDIT_CARD", "currency": "USD", "entity_status": "ACTIVE", "able_to_fund": True, "deleted": False, "cancelled": False}
        self.promoter = {"id": "prom1", "user_id": USER_ID, "promotable_user_type": "FULL", "deleted": False}
        self.post = {"tweet_id": POST_ID, "full_text": "Build with Kern", "user": {"id_str": USER_ID, "screen_name": "infiloop"}, "nullcast": False, "truncated": False,
                     "entities": {"urls": [{"expanded_url": "https://kern.example/"}]}, "extended_entities": {"media": [{"id_str": "100", "type": "photo", "media_url_https": "https://pbs.twimg.com/media/example.jpg"}]}}
        self.campaign = {"id": "camp1", "name": "Kern promotion", "funding_instrument_id": "fund1", "currency": "USD", "entity_status": "PAUSED", "budget_optimization": "LINE_ITEM", "daily_budget_amount_local_micro": 10000000, "total_budget_amount_local_micro": 50000000, "deleted": False}
        self.group = {"id": "group1", "campaign_id": "camp1", "name": "Kern promotion", "currency": "USD", "entity_status": "PAUSED", "objective": "ENGAGEMENTS", "product_type": "PROMOTED_TWEETS", "placements": ["TWITTER_TIMELINE"], "bid_strategy": "MAX", "goal": "ENGAGEMENT", "pay_by": "ENGAGEMENT", "standard_delivery": True, "start_time": START, "end_time": END, "daily_budget_amount_local_micro": 10000000, "total_budget_amount_local_micro": 50000000, "bid_amount_local_micro": 1000000, "deleted": False, "creative_source": "MANUAL"}
        self.targets = [{"id": "geo1", "line_item_id": "group1", "targeting_type": "LOCATION", "targeting_value": GEO, "name": "United States", "operator_type": "EQ", "deleted": False}]
        self.promoted = [{"id": "ad1", "line_item_id": "group1", "tweet_id": POST_ID, "entity_status": "ACTIVE", "approval_status": "ACCEPTED", "deleted": False}]
        if empty:
            self.campaign = None
            self.group = None
            self.targets = []
            self.promoted = []
        self.next_cursor = None
        self.extra_groups = []
        self.fail = None
        self.fail_after = None
        self.metrics = {"impressions": [100], "engagements": [7], "billed_charge_local_micro": [1250000], "likes": [None]}
        self.metrics_by_placement = {"SPOTLIGHT": {}, "TREND": {}}

    def json_request(self, method, url, *, headers, form=None, **kwargs):
        parsed = urllib.parse.urlsplit(url)
        assert parsed.scheme == "https" and parsed.netloc == "ads-api.x.com"
        assert parsed.path.startswith("/12/") and headers["Authorization"].startswith("OAuth ")
        params = dict(urllib.parse.parse_qsl(parsed.query)) if method == "GET" else dict(form or {})
        path = parsed.path.removeprefix("/12")
        self.calls.append((method, path, params))
        if self.fail == (method, path):
            raise WebRequestError("secret provider body", status=503, body=b"secret credential should never surface")
        response = self._respond(method, path, params)
        if self.fail_after == (method, path):
            raise WebRequestError("ambiguous timeout", status=0)
        return copy.deepcopy(response)

    def _respond(self, method, path, params):
        prefix = "/accounts/" + ACCOUNT_ID
        if method == "GET":
            if path == "/accounts":
                return {"data": [self.account], "next_cursor": self.next_cursor}
            if path == prefix:
                return {"data": self.account}
            if path == prefix + "/authenticated_user_access":
                return {"data": self.access}
            if path == prefix + "/funding_instruments":
                return {"data": [self.funding], "next_cursor": self.next_cursor}
            if path == prefix + "/funding_instruments/fund1":
                return {"data": self.funding}
            if path == prefix + "/promotable_users":
                return {"data": [self.promoter], "next_cursor": self.next_cursor}
            if path == prefix + "/promotable_users/prom1":
                return {"data": self.promoter}
            if path == prefix + "/tweets":
                return {"data": [self.post], "next_cursor": None}
            if path.startswith("/targeting_criteria/"):
                return {"data": [{"name": "United States", "targeting_type": "LOCATION", "targeting_value": GEO, "country_code": "US", "location_type": "COUNTRIES"}], "next_cursor": self.next_cursor}
            if path == prefix + "/campaigns":
                return {"data": [self.campaign] if self.campaign else [], "next_cursor": self.next_cursor}
            if path == prefix + "/campaigns/camp1":
                return {"data": self.campaign}
            if path == prefix + "/line_items":
                return {"data": ([self.group] if self.group else []) + self.extra_groups, "next_cursor": self.next_cursor}
            if path == prefix + "/line_items/group1":
                return {"data": self.group}
            if path == prefix + "/targeting_criteria":
                return {"data": self.targets, "next_cursor": self.next_cursor}
            if path == prefix + "/promoted_tweets":
                return {"data": self.promoted, "next_cursor": self.next_cursor}
            if path == "/stats/accounts/" + ACCOUNT_ID:
                metrics = self.metrics if params["placement"] == "ALL_ON_TWITTER" else self.metrics_by_placement[params["placement"]]
                return {"data": [{"id": "camp1", "id_data": [{"segment": None, "metrics": metrics}]}]}
        if method in ("PUT", "POST"):
            row = {}
            for key, value in params.items():
                row[key] = value.split(",") if key == "placements" else value == "true" if key == "standard_delivery" else int(value) if key.endswith("_local_micro") else value
            if path.endswith("/campaigns"):
                self.campaign = {**row, "id": "camp1", "currency": "USD", "deleted": False}
                return {"data": self.campaign}
            if path.endswith("/line_items"):
                self.group = {**row, "id": "group1", "currency": "USD", "pay_by": {"ENGAGEMENTS": "ENGAGEMENT", "REACH": "IMPRESSION", "WEBSITE_CLICKS": "IMPRESSION", "VIDEO_VIEWS": "VIEW"}[row["objective"]], "deleted": False}
                return {"data": self.group}
            if path.endswith("/campaigns/camp1"):
                self.campaign.update(row)
                return {"data": self.campaign}
            if path.endswith("/line_items/group1"):
                self.group.update(row)
                return {"data": self.group}
            if path.endswith("/targeting_criteria"):
                row.update({"id": f"target{len(self.targets) + 1}", "deleted": False})
                self.targets.append(row)
                return {"data": row}
            if path.endswith("/promoted_tweets"):
                row = {"id": "ad1", "line_item_id": params["line_item_id"], "tweet_id": params["tweet_ids"], "entity_status": "ACTIVE", "approval_status": "ACCEPTED", "deleted": False}
                self.promoted.append(row)
                return {"data": [row]}
        raise AssertionError((method, path, params))

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


class XAdsTest(unittest.TestCase):
    def setUp(self):
        self.api = host_api()
        self.provider = AdsProvider()
        self.enterContext(patch.object(transport, "json_request", side_effect=lambda *a, **kw: self.provider.json_request(*a, **kw)))

    def execute(self, action, values):
        return x_ads.BUNDLED_TOOL.execute(action, values, self.api)

    def propose(self, action, values):
        if action == "launch_campaign":
            self.provider.campaign = self.provider.group = None
            self.provider.targets = []
            self.provider.promoted = []
        result = self.execute(action, values)
        self.assertIsInstance(result, ActionPendingApproval, result)
        self.assertEqual(self.provider.writes, [])
        self.assertLessEqual(len(result.summary.encode("utf-8")), 500)
        return self.api.approvals.approve(result.approval_id)

    def approve(self, record):
        return x_ads.BUNDLED_TOOL.execute_approved(record, self.api)

    def test_every_read_matches_closed_output_schema(self):
        requests = {
            "list_accounts": {}, "get_account": {"account_id": ACCOUNT_ID},
            "list_funding_sources": {"account_id": ACCOUNT_ID}, "list_promotable_users": {"account_id": ACCOUNT_ID},
            "list_posts": {"account_id": ACCOUNT_ID, "promotable_user_id": "prom1"},
            "lookup_targeting": {"kind": "LOCATION", "query": "United States"},
            "list_campaigns": {"account_id": ACCOUNT_ID}, "get_campaign": {"account_id": ACCOUNT_ID, "campaign_id": "camp1"},
            "get_ad_group": {"account_id": ACCOUNT_ID, "line_item_id": "group1"},
            "get_performance": {"account_id": ACCOUNT_ID, "campaign_id": "camp1", "start_time": "2026-09-01T00:00:00Z", "end_time": "2026-09-02T00:00:00Z"},
        }
        for action, values in requests.items():
            with self.subTest(action=action):
                assert_matches_output_schema(self, MANIFEST, action, self.execute(action, values))
        self.assertEqual(self.provider.writes, [])

    def test_one_launch_approval_creates_verifies_and_activates_new_campaign(self):
        approval = self.propose("launch_campaign", plan())
        self.assertIn("USD daily 10", approval.summary)
        self.assertEqual(approval.payload["plan"]["targeting"], plan()["targeting"])
        self.assertEqual(approval.payload["references"]["post"]["text"], "Build with Kern")
        self.assertEqual(approval.payload["delivery"]["objective"], "ENGAGEMENTS")
        self.assertEqual(approval.payload["delivery"]["bid_strategy"], "AUTO")
        self.assertIsInstance(self.approve(approval), ApprovalExecuted)
        self.assertEqual(len(self.api.approvals.records), 1)
        self.assertEqual(len(self.provider.targets), 2)
        self.assertEqual(len(self.provider.promoted), 1)
        self.assertEqual(len(self.provider.writes), 7)
        self.assertEqual([params["entity_status"] for _, path, params in self.provider.writes if "entity_status" in params], ["PAUSED", "PAUSED", "ACTIVE", "ACTIVE"])
        self.assertTrue(self.provider.writes[-2][1].endswith("/line_items/group1"))
        self.assertTrue(self.provider.writes[-1][1].endswith("/campaigns/camp1"))
        for row in (self.provider.campaign, self.provider.group):
            self.assertEqual(row["entity_status"], "ACTIVE")
            self.assertEqual(row["daily_budget_amount_local_micro"], 10000000)
            self.assertEqual(row["total_budget_amount_local_micro"], 50000000)
        self.assertFalse(any("bid_amount_local_micro" in params or "pay_by" in params for _, _, params in self.provider.writes))

    def test_all_four_objectives_use_auto_and_internal_goal_billing_mapping(self):
        pairs = {"ENGAGEMENTS": ("ENGAGEMENT", "ENGAGEMENT"), "REACH": ("MAX_REACH", "IMPRESSION"), "WEBSITE_CLICKS": ("LINK_CLICKS", "IMPRESSION"), "VIDEO_VIEWS": ("VIDEO_VIEW", "VIEW")}
        for objective, (goal, pay_by) in pairs.items():
            with self.subTest(objective=objective):
                self.provider = AdsProvider(empty=True)
                if objective == "VIDEO_VIEWS":
                    self.provider.post["extended_entities"]["media"][0].update(type="video", video_info={"variants": [{"url": "https://video.twimg.com/example.mp4"}]})
                approval = self.propose("launch_campaign", plan(objective=objective))
                self.assertEqual(approval.payload["delivery"]["goal"], goal)
                self.assertEqual(approval.payload["delivery"]["expected_pay_by"], pay_by)
                self.assertIsInstance(self.approve(approval), ApprovalExecuted)
                self.assertEqual(self.provider.group["objective"], objective)
                self.assertEqual(self.provider.group["bid_strategy"], "AUTO")
                self.assertEqual(self.provider.group["goal"], goal)
                self.assertEqual(self.provider.group["pay_by"], pay_by)
                self.assertFalse(any("bid_amount_local_micro" in params for _, _, params in self.provider.writes))
                self.provider.calls.clear()

    def test_incompatible_objective_creative_rejected_before_approval_or_creation(self):
        mutations = [("WEBSITE_CLICKS", "entities", {}), ("WEBSITE_CLICKS", "entities", {"urls": [{"url": "https://t.co/short"}]}), ("WEBSITE_CLICKS", "entities", {"urls": [{"expanded_url": "javascript:alert(1)"}]}), ("WEBSITE_CLICKS", "entities", {"urls": [{"expanded_url": "https://x.com/i/status/123"}]}), ("WEBSITE_CLICKS", "entities", {"urls": [{"expanded_url": "https://user:password@example.com/"}]}), ("VIDEO_VIEWS", "extended_entities", {"media": [{"type": "photo"}]}), ("VIDEO_VIEWS", "extended_entities", {"media": [{"type": "animated_gif"}]}), ("VIDEO_VIEWS", "extended_entities", {})]
        for objective, field, value in mutations:
            with self.subTest(objective=objective, value=value):
                self.provider = AdsProvider()
                self.provider.post[field] = value
                result = self.execute("launch_campaign", plan(objective=objective))
                self.assertIsInstance(result, ActionFailed)
                self.assertIn(objective, result.error)
                self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.api.approvals.records, {})

    def test_objective_creative_revalidated_before_creation(self):
        for objective in ("WEBSITE_CLICKS", "VIDEO_VIEWS"):
            self.provider = AdsProvider()
            self.provider.post["extended_entities"]["media"][0]["type"] = "video"
            approval = self.propose("launch_campaign", plan(objective=objective))
            self.provider.post["entities"] = {}
            self.provider.post["extended_entities"] = {}
            self.assertIn(objective, self.approve(approval).error)
            self.assertEqual(self.provider.writes, [])

    def test_empty_or_no_location_targeting_is_explicit_worldwide(self):
        for targets in ([], [{"type": "LANGUAGE", "value": "en"}], [{"type": "SIMILAR_TO_FOLLOWERS_OF_USER", "value": "14230524"}]):
            with self.subTest(targets=targets):
                self.provider = AdsProvider()
                approval = self.propose("launch_campaign", plan(targeting=targets))
                self.assertIn("Worldwide, no location restriction", approval.summary)
                self.assertEqual(approval.payload["delivery"]["geographic_scope"], "Worldwide, no location restriction")
                self.assertEqual(approval.payload["plan"]["targeting"], targets)
                self.assertIsInstance(self.approve(approval), ApprovalExecuted)
                self.assertEqual(len(self.provider.targets), len(targets))
                self.assertFalse(any(row["targeting_type"] == "LOCATION" for row in self.provider.targets))
                self.provider.calls.clear()

    def test_numeric_follower_seeds_and_each_expansion_level_are_exact(self):
        targets = [{"type": "SIMILAR_TO_FOLLOWERS_OF_USER", "value": "14230524"}, {"type": "SIMILAR_TO_FOLLOWERS_OF_USER", "value": "90420314"}]
        for expansion in (None, "DEFINED", "EXPANDED", "BROAD"):
            self.provider = AdsProvider()
            values = plan(targeting=targets, **({"audience_expansion": expansion} if expansion else {}))
            approval = self.propose("launch_campaign", values)
            self.assertEqual(approval.payload["plan"]["targeting"], targets)
            self.assertEqual(approval.payload["delivery"]["audience_expansion"], expansion)
            self.assertEqual(approval.payload["delivery"]["expansion_description"], expansion or "No expansion")
            self.assertIsInstance(self.approve(approval), ApprovalExecuted)
            self.assertEqual(self.provider.group.get("audience_expansion"), expansion)
            self.assertEqual([row["targeting_value"] for row in self.provider.targets], ["14230524", "90420314"])
            group_call = next(params for method, path, params in self.provider.writes if path.endswith("/line_items"))
            self.assertEqual("audience_expansion" in group_call, expansion is not None)
            self.provider.calls.clear()

    def test_wrong_provider_billing_defaults_never_activate(self):
        for objective in ("ENGAGEMENTS", "REACH", "WEBSITE_CLICKS", "VIDEO_VIEWS"):
            self.provider = AdsProvider()
            self.provider.post["extended_entities"]["media"][0]["type"] = "video"
            approval = self.propose("launch_campaign", plan(objective=objective))
            original = self.provider._respond
            def incorrect(method, path, params):
                result = original(method, path, params)
                if method == "POST" and path.endswith("/line_items"):
                    self.provider.group["pay_by"] = "UNAPPROVED"
                return result
            self.provider._respond = incorrect
            result = self.approve(approval)
            self.assertIn("billing basis", result.error)
            self.assertEqual(len(self.provider.writes), 2)
            self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")
            self.provider.calls.clear()

    def test_funding_is_explicit_and_live_prerequisites_rechecked(self):
        for change in (lambda p: p.funding.update(able_to_fund=False), lambda p: p.funding.update(cancelled=True), lambda p: p.account.update(approval_status="PENDING")):
            self.provider = AdsProvider()
            approval = self.propose("launch_campaign", plan())
            change(self.provider)
            self.assertIsInstance(self.approve(approval), ActionFailed)
            self.assertIsInstance(self.execute("launch_campaign", plan()), ActionFailed)
            self.assertEqual(self.provider.writes, [])
        self.provider = AdsProvider()
        values = plan()
        del values["funding_instrument_id"]
        self.assertIsInstance(self.execute("launch_campaign", values), ActionFailed)
        self.assertFalse(any("/funding_instruments" in path for _, path, _ in self.provider.calls))

    def test_changed_credentials_invalidate_approval(self):
        approval = self.propose("launch_campaign", plan())
        self.api.config["X_ADS_ACCESS_TOKEN_SECRET"] = "new-secret"
        self.assertIn("credentials changed", self.approve(approval).error)
        self.assertEqual(self.provider.writes, [])
        self.assertNotIn("fake-X_ADS", json.dumps(approval.payload))

    def test_read_only_role_cannot_propose_or_execute_write(self):
        self.provider.access["permissions"] = ["CAMPAIGN_ANALYST"]
        self.assertIsInstance(self.execute("get_account", {"account_id": ACCOUNT_ID}), ActionExecuted)
        self.assertIn("ACCOUNT_ADMIN", self.execute("launch_campaign", plan()).error)
        self.provider.access["permissions"] = ["AD_MANAGER"]
        approval = self.propose("launch_campaign", plan())
        self.provider.access["permissions"] = ["CREATIVE_MANAGER"]
        self.assertIn("ACCOUNT_ADMIN", self.approve(approval).error)
        self.assertEqual(self.provider.writes, [])

    def test_content_media_destinations_and_author_changes_invalidate_approval(self):
        for field, change in (("full_text", "edited"), ("extended_entities", {"media": [{"id_str": "101"}]}), ("entities", {"urls": [{"expanded_url": "https://changed.example/"}]}), ("user", {"id_str": "123"})):
            with self.subTest(field=field):
                self.provider = AdsProvider()
                approval = self.propose("launch_campaign", plan())
                self.provider.post[field] = change
                self.assertIsInstance(self.approve(approval), ActionFailed)
                self.assertEqual(self.provider.writes, [])

    def test_post_engagement_counts_do_not_invalidate_content(self):
        approval = self.propose("launch_campaign", plan())
        self.provider.post["favorite_count"] = 100
        self.provider.post["retweet_count"] = 8
        self.assertIsInstance(self.approve(approval), ApprovalExecuted)

    def test_unsupported_posts_and_mutable_cards_rejected_at_proposal_and_execution(self):
        for key, value in (("quoted_status_id_str", "123"), ("is_quote_status", True), ("retweeted_status", {"id_str": "123"}), ("in_reply_to_status_id_str", "123"), ("nullcast", True), ("truncated", True), ("card_uri", "card://mutable1"), ("card", {"url": "https://example.com/"})):
            with self.subTest(key=key):
                self.provider = AdsProvider()
                approval = self.propose("launch_campaign", plan())
                self.provider.post[key] = value
                self.assertIsInstance(self.execute("launch_campaign", plan()), ActionFailed)
                self.assertIsInstance(self.approve(approval), ActionFailed)
                self.assertEqual(self.provider.writes, [])

    def test_flight_expires_while_approval_pending_without_creating(self):
        approval = self.propose("launch_campaign", plan())
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(timezone.utc) + timedelta(days=10)
        with patch.object(tool, "datetime", Later):
            result = self.approve(approval)
        self.assertIn("flight has ended", result.error)
        self.assertEqual(self.provider.writes, [])

    def test_flight_expires_during_final_reads_before_parent_activation(self):
        approval = self.propose("launch_campaign", plan())
        original = self.provider._respond
        final_read = False
        def delayed(method, path, params):
            nonlocal final_read
            response = original(method, path, params)
            if method == "GET" and path.endswith("/funding_instruments/fund1") and self.provider.group and self.provider.group["entity_status"] == "ACTIVE":
                final_read = True
            return response
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(timezone.utc) + timedelta(days=10 if final_read else 0)
        self.provider._respond = delayed
        with patch.object(tool, "datetime", Clock):
            result = self.approve(approval)
        self.assertIn("flight has ended", result.error)
        self.assertTrue(final_read)
        self.assertEqual(len(self.provider.writes), 6)
        self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")

    def test_each_write_failure_or_ambiguous_outcome_stops_without_retry(self):
        steps = [("POST", "campaigns"), ("POST", "line_items"), ("POST", "targeting_criteria"), ("POST", "promoted_tweets"), ("PUT", "line_items/group1"), ("PUT", "campaigns/camp1")]
        for method, suffix in steps:
            for after in (False, True):
                with self.subTest(suffix=suffix, after=after):
                    self.provider = AdsProvider()
                    approval = self.propose("launch_campaign", plan())
                    failure = (method, f"/accounts/{ACCOUNT_ID}/{suffix}")
                    if after:
                        self.provider.fail_after = failure
                    else:
                        self.provider.fail = failure
                    result = self.approve(approval)
                    self.assertIsInstance(result, ActionFailed)
                    self.assertIn("Last attempted step:", result.error)
                    self.assertIn("last request may have succeeded", result.error)
                    attempts = [call for call in self.provider.writes if call[:2] == failure]
                    self.assertEqual(len(attempts), 1)
                    if self.provider.campaign and suffix != "campaigns/camp1":
                        self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")
                    if len(self.provider.writes) > 1:
                        self.assertIn("campaign camp1", result.error)
                    if len(self.provider.writes) > 2:
                        self.assertIn("ad group group1", result.error)
                    self.provider.calls.clear()

    def test_created_association_id_retained_on_wrong_response_identity(self):
        approval = self.propose("launch_campaign", plan())
        original = self.provider._respond
        def wrong(method, path, params):
            response = original(method, path, params)
            if method == "POST" and path.endswith("/promoted_tweets"):
                self.provider.promoted[0]["tweet_id"] = "123"
            return response
        self.provider._respond = wrong
        result = self.approve(approval)
        self.assertIn("promoted post ad1", result.error)
        self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")

    def test_configured_drift_and_hidden_caps_before_each_activation_fail_closed(self):
        mutations = [lambda p: p.targets[0].update(targeting_value="3b77caf94bfc81fe"), lambda p: p.group.update(audience_expansion="BROAD"), lambda p: p.group.update(frequency_cap=5, duration_in_days=7), lambda p: p.group.update(frequency_cap=1), lambda p: p.group.update(duration_in_days=1), lambda p: p.campaign.update(total_budget_amount_local_micro=60000000), lambda p: p.group.update(start_time="2026-01-01T00:00:00Z"), lambda p: p.group.update(unknown_config="changed"), lambda p: p.promoted[0].update(approval_status="REJECTED"), lambda p: p.post.update(full_text="edited during execution"), lambda p: p.access.update(user_id="123"), lambda p: p.access.update(permissions=["ANALYST"]), lambda p: p.funding.update(able_to_fund=False)]
        for after_child in (False, True):
            for mutate in mutations:
                with self.subTest(after_child=after_child, mutate=mutate):
                    self.provider = AdsProvider()
                    approval = self.propose("launch_campaign", plan())
                    original = self.provider._respond
                    def changing(method, path, params):
                        response = original(method, path, params)
                        trigger = method == "PUT" and path.endswith("/line_items/group1") if after_child else method == "POST" and path.endswith("/promoted_tweets")
                        if trigger:
                            mutate(self.provider)
                        return response
                    self.provider._respond = changing
                    self.assertIsInstance(self.approve(approval), ActionFailed)
                    self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")
                    self.assertFalse(any(path.endswith("/campaigns/camp1") for _, path, _ in self.provider.writes))
                    if not after_child:
                        self.assertFalse(any(method == "PUT" for method, _, _ in self.provider.writes))
                    self.provider.calls.clear()

    def test_known_pending_creative_configures_active_without_claiming_delivery(self):
        approval = self.propose("launch_campaign", plan())
        self.assertIn("without another Kern approval", approval.summary)
        self.assertIn("blocked until X accepts", approval.payload["delivery"]["provider_review"])
        original = self.provider._respond
        def pending(method, path, params):
            response = original(method, path, params)
            if method == "POST" and path.endswith("/promoted_tweets"):
                self.provider.promoted[0]["approval_status"] = "PENDING"
            return response
        self.provider._respond = pending
        result = self.approve(approval)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertIn("configured campaign and ad group ACTIVE", result.message)
        self.assertIn("delivery is blocked until X accepts", result.message)
        self.assertIn("not proof of delivery", result.message)
        self.assertEqual(len(self.provider.writes), 7)

    def test_only_pending_to_accepted_review_transition_is_allowed(self):
        for initial, final in (("PENDING", "ACCEPTED"), ("ACCEPTED", "PENDING"), ("PENDING", "REJECTED"), ("PENDING", None), ("PENDING", "UNKNOWN")):
            self.provider = AdsProvider()
            approval = self.propose("launch_campaign", plan())
            original = self.provider._respond
            def review(method, path, params):
                response = original(method, path, params)
                if method == "POST" and path.endswith("/promoted_tweets"):
                    self.provider.promoted[0]["approval_status"] = initial
                if method == "PUT" and path.endswith("/line_items/group1"):
                    self.provider.promoted[0]["approval_status"] = final
                return response
            self.provider._respond = review
            result = self.approve(approval)
            if initial == "PENDING" and final == "ACCEPTED":
                self.assertIsInstance(result, ApprovalExecuted)
                self.assertIn("Last observed X review ACCEPTED", result.message)
            else:
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")
            self.provider.calls.clear()

    def test_repeated_review_after_pending_to_accepted_never_activates_parent(self):
        approval = self.propose("launch_campaign", plan())
        original = self.provider._respond
        def reviewed(method, path, params):
            response = original(method, path, params)
            if method == "POST" and path.endswith("/promoted_tweets"):
                self.provider.promoted[0]["approval_status"] = "PENDING"
            if method == "GET" and path.endswith("/promoted_tweets"):
                self.provider.promoted[0]["approval_status"] = "ACCEPTED" if self.provider.group["entity_status"] == "PAUSED" else "PENDING"
            return response
        self.provider._respond = reviewed
        self.assertIsInstance(self.approve(approval), ActionFailed)
        self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")

    def test_unknown_missing_or_rejected_initial_review_leaves_parent_paused(self):
        for status in (None, "UNKNOWN", "REJECTED"):
            self.provider = AdsProvider()
            approval = self.propose("launch_campaign", plan())
            original = self.provider._respond
            def review(method, path, params):
                response = original(method, path, params)
                if method == "POST" and path.endswith("/promoted_tweets"):
                    self.provider.promoted[0]["approval_status"] = status
                return response
            self.provider._respond = review
            result = self.approve(approval)
            self.assertIsInstance(result, ActionFailed)
            self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")
            self.assertEqual(len(self.provider.writes), 5)
            self.provider.calls.clear()

    def test_each_verification_read_failure_leaves_parent_paused(self):
        paths = ("campaigns/camp1", "line_items", "line_items/group1", "targeting_criteria", "promoted_tweets", "authenticated_user_access", "funding_instruments/fund1", "tweets")
        for path_suffix in paths:
            for after_child in (False, True):
                with self.subTest(path=path_suffix, after_child=after_child):
                    self.provider = AdsProvider()
                    approval = self.propose("launch_campaign", plan())
                    original = self.provider._respond
                    def fail_read(method, path, params):
                        response = original(method, path, params)
                        trigger = method == "PUT" and path.endswith("/line_items/group1") if after_child else method == "POST" and path.endswith("/promoted_tweets")
                        if trigger:
                            self.provider.fail = ("GET", f"/accounts/{ACCOUNT_ID}/{path_suffix}")
                        return response
                    self.provider._respond = fail_read
                    result = self.approve(approval)
                    self.assertIsInstance(result, ActionFailed)
                    self.assertIn("Confirmed:", result.error)
                    self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")
                    self.assertFalse(any(path.endswith("/campaigns/camp1") for _, path, _ in self.provider.writes))
                    self.provider.calls.clear()

    def test_later_targeting_write_failure_retains_earlier_criterion_id(self):
        approval = self.propose("launch_campaign", plan())
        original = self.provider._respond
        def first_target(method, path, params):
            response = original(method, path, params)
            if method == "POST" and path.endswith("/targeting_criteria"):
                self.provider.fail = (method, path)
            return response
        self.provider._respond = first_target
        result = self.approve(approval)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("targeting criterion target1", result.error)
        self.assertEqual(len(self.provider.targets), 1)
        self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")

    def test_incomplete_readback_or_extra_ad_groups_cannot_activate(self):
        for more in (False, True):
            self.provider = AdsProvider()
            approval = self.propose("launch_campaign", plan())
            original = self.provider._respond
            def incomplete(method, path, params):
                response = original(method, path, params)
                if method == "POST" and path.endswith("/promoted_tweets"):
                    if more:
                        self.provider.extra_groups = [{"id": "group2", "campaign_id": "camp1"}]
                    else:
                        self.provider.next_cursor = "nextpage"
                return response
            self.provider._respond = incomplete
            self.assertIsInstance(self.approve(approval), ActionFailed)
            self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")
            self.assertFalse(any(method == "PUT" for method, _, _ in self.provider.writes))
            self.provider.calls.clear()

    def test_derived_delivery_status_changes_do_not_block_exact_activation(self):
        approval = self.propose("launch_campaign", plan())
        original = self.provider._respond
        def activated(method, path, params):
            response = original(method, path, params)
            if method == "PUT" and path.endswith("/line_items/group1"):
                self.provider.group.update(effective_status="PAUSED", servable=False, reasons_not_servable=["PAUSED_CAMPAIGN"], updated_at="later")
            return response
        self.provider._respond = activated
        self.assertIsInstance(self.approve(approval), ApprovalExecuted)
        self.assertEqual(self.provider.campaign["entity_status"], "ACTIVE")

    def test_end_arbitrary_campaign_ignores_settings_and_volatile_fields(self):
        self.provider.extra_groups = [{"id": "group2"}]
        approval = self.propose("end_campaign", {"account_id": ACCOUNT_ID, "campaign_id": "camp1"})
        self.provider.campaign.update(entity_status="ACTIVE", effective_status="ACTIVE", servable=True, reasons_not_servable=[], total_budget_amount_local_micro=90000000, name="Externally renamed")
        self.provider.account["name"] = "Renamed advertiser"
        result = self.approve(approval)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertIn("reporting retained", result.message)
        self.assertIn("no Kern resume or permanent deletion", result.message)
        self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")
        self.assertEqual(len(self.provider.writes), 1)
        self.assertFalse(any(path.endswith("/line_items") for _, path, _ in self.provider.calls))

    def test_end_rechecks_role_user_campaign_account_and_credentials(self):
        for change in (lambda p: p.access.update(permissions=["ANALYST"]), lambda p: p.access.update(user_id="123"), lambda p: p.campaign.update(id="another1"), lambda p: p.account.update(deleted=True)):
            self.provider = AdsProvider()
            approval = self.propose("end_campaign", {"account_id": ACCOUNT_ID, "campaign_id": "camp1"})
            change(self.provider)
            self.assertIsInstance(self.approve(approval), ActionFailed)
            self.assertEqual(self.provider.writes, [])
        self.provider = AdsProvider()
        approval = self.propose("end_campaign", {"account_id": ACCOUNT_ID, "campaign_id": "camp1"})
        self.api.config["X_ADS_ACCESS_TOKEN"] = "replacement"
        self.assertIn("credentials changed", self.approve(approval).error)
        self.assertEqual(self.provider.writes, [])

    def test_removed_writes_and_resume_existing_inputs_are_rejected(self):
        for action in ("create_paused_campaign", "pause_campaign", "update_campaign"):
            self.assertIsInstance(self.execute(action, {}), ActionFailed)
            self.assertIsNone(MANIFEST.action(action))
        self.assertIsInstance(self.execute("launch_campaign", {"account_id": ACCOUNT_ID, "campaign_id": "camp1", "promotable_user_id": "prom1"}), ActionFailed)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(len(MANIFEST.actions), 12)
        self.assertEqual({a.id for a in MANIFEST.actions if a.approval == "operator"}, {"launch_campaign", "end_campaign"})

    def test_legacy_pending_resume_approval_cannot_activate(self):
        legacy = self.api.approvals.request(action_id="launch_campaign", summary="Legacy existing campaign launch",
            payload={"account_id": ACCOUNT_ID, "campaign_id": "camp1", "promotable_user_id": "prom1", "credential_binding": transport.Client(self.api).binding})
        result = self.approve(self.api.approvals.approve(legacy.approval_id))
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.provider.campaign["entity_status"], "PAUSED")

    def test_invalid_names_caps_dates_targeting_options_and_unknown_fields(self):
        invalid = [{"name": " "}, {"name": "a" * 256}, {"name": "line\nfeed"}, {"daily_budget_amount_local_micro": "01"}, {"daily_budget_amount_local_micro": "60000000"}, {"bid_amount_local_micro": "1000000"}, {"total_budget_amount_local_micro": "1000000000001"}, {"start_time": "2026-02-30T00:00:00Z"}, {"end_time": "2000-01-01T00:00:00Z"}, {"targeting": None}, {"targeting": [{"type": "LOCATION", "value": "invalid"}]}, {"targeting": [{"type": "LOCATION", "value": GEO, "extra": True}]}, {"targeting": [{"type": "LOCATION", "value": GEO}] * 2}, {"targeting": [{"type": "SIMILAR_TO_FOLLOWERS_OF_USER", "value": "@infiloop"}]}, {"targeting": [{"type": "SIMILAR_TO_FOLLOWERS_OF_USER", "value": "0123"}]}, {"targeting": [{"type": "SIMILAR_TO_FOLLOWERS_OF_USER", "value": "1" * 26}]}, {"targeting": [{"type": "FOLLOWERS_OF_USER", "value": "123"}]}, {"targeting": [{"type": "PHRASE_KEYWORD", "value": str(i)} for i in range(21)]}, {"objective": "WEBSITE_CONVERSIONS"}, {"audience_expansion": "NONE"}, {"audience_expansion": None}, {"extra": "secret"}]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.assertIsInstance(self.execute("launch_campaign", plan(**changes)), ActionFailed)
        self.assertEqual(self.provider.writes, [])
        self.assertEqual(self.api.approvals.records, {})

    def test_unicode_summary_and_bounded_payload(self):
        self.provider.account["name"] = "広告😀" * 50
        approval = self.propose("launch_campaign", plan(name="😀" * 255, targeting=[]))
        self.assertLessEqual(len(approval.summary.encode("utf-8")), 500)
        self.assertIn("Worldwide, no location restriction", approval.summary)
        self.assertLess(len(json.dumps(approval.payload).encode()), 65536)

    def test_read_query_and_cursor_are_guarded_before_egress(self):
        for action, values in (("lookup_targeting", {"kind": "LOCATION", "query": "call +1 415 555 2671"}), ("list_accounts", {"cursor": "alice@example.com"})):
            self.provider.calls.clear()
            result = self.execute(action, values)
            self.assertIsInstance(result, ActionFailed)
            self.assertEqual(self.provider.calls, [])
        self.assertIsInstance(self.execute("list_accounts", {"cursor": "6by4n4"}), ActionExecuted)
        self.assertEqual(self.provider.calls[-1][2]["cursor"], "6by4n4")

    def test_read_pagination_is_explicit_and_bounded(self):
        self.provider.next_cursor = "6by4n4"
        result = self.execute("list_accounts", {"count": 1})
        self.assertEqual(result.result["next_cursor"], "6by4n4")
        self.assertEqual(len(self.provider.calls), 1)
        self.assertIsInstance(self.execute("list_accounts", {"count": 51}), ActionFailed)
        self.assertIsInstance(self.execute("list_accounts", {"count": True}), ActionFailed)

    def test_performance_micros_and_missing_metrics_are_not_zero(self):
        result = self.execute("get_performance", {"account_id": ACCOUNT_ID, "campaign_id": "camp1", "start_time": "2026-09-01T07:00:00Z", "end_time": "2026-09-02T07:00:00Z"})
        self.assertEqual(result.result["metrics"]["billed_charge_local_micro"], "1250000")
        self.assertEqual(result.result["metrics"]["engagements"], 7)
        self.assertIsNone(result.result["metrics"]["likes"])
        self.assertIsNone(result.result["metrics"]["url_clicks"])
        self.assertEqual(self.provider.calls[-1][2]["granularity"], "TOTAL")
        self.assertEqual(self.api.costs.calls, [])

    def test_performance_aggregates_all_placements_and_returns_breakdown(self):
        self.provider.metrics_by_placement = {"SPOTLIGHT": {"impressions": [25], "engagements": [3], "billed_charge_local_micro": [2500000]}, "TREND": {"impressions": [15], "engagements": [2], "billed_charge_local_micro": [750000]}}
        result = self.execute("get_performance", {"account_id": ACCOUNT_ID, "campaign_id": "camp1", "start_time": "2026-09-01T00:00:00Z", "end_time": "2026-09-02T00:00:00Z"})
        self.assertEqual(result.result["metrics"]["billed_charge_local_micro"], "4500000")
        self.assertEqual(result.result["metrics"]["engagements"], 12)
        self.assertEqual(result.result["metrics"]["impressions"], 140)
        self.assertIsNone(result.result["metrics"]["likes"])
        buckets = result.result["placement_metrics"]
        self.assertEqual([row["placement"] for row in buckets], ["ALL_ON_TWITTER", "SPOTLIGHT", "TREND"])
        self.assertEqual(buckets[1]["metrics"]["billed_charge_local_micro"], "2500000")
        self.assertEqual([params["placement"] for method, path, params in self.provider.calls if path.startswith("/stats/")], ["ALL_ON_TWITTER", "SPOTLIGHT", "TREND"])
        assert_matches_output_schema(self, MANIFEST, "get_performance", result)

    def test_performance_preserves_empty_buckets_and_fails_incomplete_reads(self):
        values = {"account_id": ACCOUNT_ID, "campaign_id": "camp1", "start_time": "2026-09-01T00:00:00Z", "end_time": "2026-09-02T00:00:00Z"}
        self.provider.metrics = {}
        result = self.execute("get_performance", values)
        self.assertTrue(all(value is None for value in result.result["metrics"].values()))
        self.assertTrue(all(value is None for row in result.result["placement_metrics"] for value in row["metrics"].values()))
        original = self.provider._respond
        def failing(method, path, params):
            if path.startswith("/stats/") and params["placement"] == "TREND":
                raise WebRequestError("temporarily unavailable", status=503)
            return original(method, path, params)
        self.provider._respond = failing
        self.assertIsInstance(self.execute("get_performance", values), ActionFailed)

    def test_performance_time_window_and_whole_hour_validation(self):
        for end in ("2026-09-09T00:00:00Z", "2026-09-01T00:00:00Z", "2026-09-02T00:01:00Z"):
            result = self.execute("get_performance", {"account_id": ACCOUNT_ID, "campaign_id": "camp1", "start_time": "2026-09-01T00:00:00Z", "end_time": end})
            self.assertIsInstance(result, ActionFailed)

    def test_errors_do_not_disclose_raw_provider_bodies(self):
        self.provider.fail = ("GET", "/accounts")
        result = self.execute("list_accounts", {})
        self.assertIsInstance(result, ActionFailed)
        self.assertNotIn("secret", result.error)
        self.assertEqual(len(self.provider.calls), 1)

    def test_manifests_register_and_unknown_nested_fields_are_rejected(self):
        self.assertIs(tools_host.BUNDLED_TOOLS["x_ads"], x_ads.BUNDLED_TOOL)
        for action in MANIFEST.actions:
            self.assertEqual(tools_host.unsupported_schema_error(action.input_schema), "")
            if action.approval == "direct":
                self.assertEqual(tools_host.unsupported_schema_error(action.output_schema), "")
            else:
                self.assertEqual(action.output_schema, {})
        self.assertTrue(tools_host.validate_against_schema(plan(targeting=[{"type": "LOCATION", "value": GEO, "unreviewed": "x"}]), MANIFEST.action("launch_campaign").input_schema))


class OAuthSigningTest(unittest.TestCase):
    def test_published_oauth_photo_example_signature(self):
        header = transport.authorization_header("GET", "http://photos.example.net/photos", {"file": "vacation.jpg", "size": "original"},
            ("dpf43f3p2l4k3l03", "kd94hf93k423kf44", "nnch734d00sl2jdk", "pfkkdhi9sl3r4s00"), nonce="kllo9940pd9333jh", timestamp=1191242096)
        self.assertIn('oauth_signature="tR3%2BTy81lMeYAr%2FFid0kMTYa%2FWM%3D"', header)

    def test_encoded_sorted_unicode_form_params_have_one_exact_signature(self):
        keys = ("consumer", "consumer/secret", "access", "token&secret")
        a = transport.authorization_header("POST", transport.BASE_URL + "/accounts/test/campaigns", {"name": "広告 + &", "entity_status": "PAUSED"}, keys, nonce="n", timestamp=1)
        b = transport.authorization_header("POST", transport.BASE_URL + "/accounts/test/campaigns", {"entity_status": "PAUSED", "name": "広告 + &"}, keys, nonce="n", timestamp=1)
        self.assertEqual(a, b)
        c = transport.authorization_header("POST", transport.BASE_URL + "/accounts/test/campaigns", {"entity_status": "PAUSED", "name": "広告   &"}, keys, nonce="n", timestamp=1)
        self.assertNotEqual(a, c)
