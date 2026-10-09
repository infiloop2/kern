"""Google Ads Search launch/end contracts. All provider calls are mocked."""
from __future__ import annotations

import copy
import io
import json
import urllib.error
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from host.tools import google_ads as ads
from host.runtime.tools.tools_host import _provider_warning_context
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared import web
from host.tools.shared.google import get_google_userinfo
from host.tools.shared.web import WebRequestError, UnmappedProviderError, ProviderWarning
from test_tools import assert_matches_output_schema, connected_google_api, google_userinfo

CUSTOMER = "1234567890"
ACCOUNT = {"id": CUSTOMER, "descriptiveName": "Example Ads", "currencyCode": "INR", "timeZone": "Asia/Kolkata", "manager": False, "testAccount": True}
CAMPAIGN = {"campaign": {"id": "77", "name": "Search", "status": "PAUSED", "advertisingChannelType": "SEARCH", "campaignBudget": f"customers/{CUSTOMER}/campaignBudgets/88", "biddingStrategyType": "MANUAL_CPC", "startDateTime": "2026-01-01 00:00:00"}, "campaignBudget": {"amountMicros": "10000000", "period": "DAILY", "explicitlyShared": False, "referenceCount": "1"}}
GROUP = {"adGroup": {"id": "99", "name": "Search group", "status": "ENABLED", "cpcBidMicros": "1000000"}, "campaign": {"id": "77"}}
AD = {"final_url": "https://example.com/pricing?source=ads", "headlines": ["Your AI Team", "Keep Work Moving", "Try Kern Today"], "descriptions": ["A permanent home for your AI team.", "Manage your agents from one command center."]}
CREATE = {"customer_id": CUSTOMER, "name": "Kern Search", "total_budget_micros": 10000000,
    "start_time": "2026-10-14T18:30:00Z", "end_time": "2026-10-22T18:29:59Z", "geo_target_ids": ["2356"],
    "keywords": [{"text": "AI agent host", "match_type": "EXACT"}], **AD}
WRITE_INPUTS = {"launch_campaign": CREATE, "end_campaign": {"customer_id": CUSTOMER, "campaign_id": "77"}}
BUDGET_RESOURCE = f"customers/{CUSTOMER}/campaignBudgets/100"
CAMPAIGN_RESOURCE = f"customers/{CUSTOMER}/campaigns/101"
GROUP_RESOURCE = f"customers/{CUSTOMER}/adGroups/102"
AD_RESOURCE = f"customers/{CUSTOMER}/adGroupAds/102~400"
KEYWORD_RESOURCE = f"customers/{CUSTOMER}/adGroupCriteria/102~304"


class GoogleAdsTests(unittest.TestCase):
    def setUp(self):
        self.diagnostics = self.enterContext(patch("host.tools.shared.ads_diagnostics.host_errors.emit_record"))
        self.api = connected_google_api("google_ads", frozenset({ads.ADS_SCOPE}))
        self.direct = [CUSTOMER]
        self.account = copy.deepcopy(ACCOUNT)
        self.campaign = copy.deepcopy(CAMPAIGN)
        self.group = copy.deepcopy(GROUP)
        self.requests = []
        self.mutations = []
        self.result_rows = []
        self.created = False
        self.read_hook = None
        self.creation_hook = None
        self.creation_error = None
        self.activation_error = None
        self.bad_result = None
        self.reset_created_state()
        self.provider = self.enterContext(patch.object(ads, "json_request", side_effect=self.request))
        self.identity = self.enterContext(patch("host.tools.shared.google.get_google_userinfo", return_value=google_userinfo()))
        self.clock = self.enterContext(patch.object(ads, "_now", return_value=datetime(2026, 10, 8, 12, tzinfo=timezone.utc)))

    def reset_created_state(self):
        self.new_campaign = {"campaign": {"id": "101", "name": "Kern Search", "status": "PAUSED", "advertisingChannelType": "SEARCH",
            "campaignBudget": BUDGET_RESOURCE, "biddingStrategyType": "TARGET_SPEND", "startDateTime": "2026-10-15 00:00:00", "endDateTime": "2026-10-22 23:59:59",
            "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING", "networkSettings": {"targetGoogleSearch": True},
            "geoTargetTypeSetting": {"positiveGeoTargetType": "PRESENCE"}},
            "campaignBudget": {"totalAmountMicros": "10000000", "period": "CUSTOM_PERIOD", "explicitlyShared": False, "referenceCount": "1", "deliveryMethod": "STANDARD"}}
        self.new_geo = [{"campaignCriterion": {"type": "LOCATION", "location": {"geoTargetConstant": "geoTargetConstants/2356"}}}]
        self.new_groups = [{"adGroup": {"resourceName": GROUP_RESOURCE, "name": "Kern Search", "status": "ENABLED", "type": "SEARCH_STANDARD"}}]
        self.new_keywords = [{"adGroupCriterion": {"resourceName": KEYWORD_RESOURCE, "type": "KEYWORD", "status": "ENABLED", "keyword": {"text": "AI agent host", "matchType": "EXACT"}}}]
        self.new_ads = [{"adGroupAd": {"resourceName": AD_RESOURCE, "status": "ENABLED", "ad": {"type": "RESPONSIVE_SEARCH_AD", "finalUrls": [AD["final_url"]],
            "responsiveSearchAd": {"headlines": [{"text": v} for v in AD["headlines"]], "descriptions": [{"text": v} for v in AD["descriptions"]]}},
            "policySummary": {"reviewStatus": "REVIEW_IN_PROGRESS"}}}]

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, copy.deepcopy(kwargs)))
        self.assertEqual(kwargs["headers"]["authorization"], "Bearer google_ads-access-token")
        self.assertNotIn("developer-token", kwargs["headers"])
        self.assertNotIn("login-customer-id", kwargs["headers"])
        if url.endswith("customers:listAccessibleCustomers"):
            self.assertEqual(method, "GET")
            return {"resourceNames": [f"customers/{cid}" for cid in self.direct]}
        body = kwargs["body"]
        if url.endswith("googleAds:mutate"):
            self.assertFalse(body["partialFailure"])
            operations = body["mutateOperations"]
            self.mutations.append(copy.deepcopy(operations))
            creating = "campaignBudgetOperation" in operations[0]
            error = self.creation_error if creating else self.activation_error
            if error:
                raise error
            if creating:
                self.created = True
                if self.creation_hook:
                    self.creation_hook()
                resources = {"campaignBudget": BUDGET_RESOURCE, "campaign": CAMPAIGN_RESOURCE, "adGroup": GROUP_RESOURCE, "adGroupAd": AD_RESOURCE}
                results = []
                for index, op in enumerate(operations):
                    kind = next(iter(op)).removesuffix("Operation")
                    resource = resources.get(kind)
                    if kind == "campaignCriterion":
                        resource = f"customers/{CUSTOMER}/campaignCriteria/101~{200 + index}"
                    if kind == "adGroupCriterion":
                        resource = f"customers/{CUSTOMER}/adGroupCriteria/102~{300 + index}"
                    results.append({kind + "Result": {"resourceName": resource}})
            else:
                resource = operations[0]["campaignOperation"]["update"]["resourceName"]
                results = [{"campaignResult": {"resourceName": resource}}]
            if self.bad_result == ("create" if creating else "activate"):
                return {}
            return {"mutateOperationResponses": results}
        query = body["query"]
        if "FROM customer LIMIT" in query:
            return {"results": [{"customer": dict(self.account)}]}
        if "FROM campaign WHERE campaign.id = 101" in query:
            if self.read_hook:
                self.read_hook()
            return {"results": [self.new_campaign]}
        if "FROM campaign_criterion" in query:
            return {"results": self.new_geo}
        if "FROM ad_group_criterion" in query:
            return {"results": self.new_keywords}
        if "FROM ad_group_ad" in query:
            return {"results": self.new_ads}
        if "FROM ad_group" in query:
            return {"results": self.new_groups if self.created else [self.group]}
        if "campaign.id = 77" in query and "LIMIT 1" in query:
            return {"results": [self.campaign]}
        if "metrics." in query:
            return {"results": self.result_rows}
        return {"results": [self.campaign]}

    def execute(self, action, values):
        return ads.BUNDLED_TOOL.execute(action, copy.deepcopy(values), self.api)

    def approve(self, action="launch_campaign", values=None):
        pending = self.execute(action, WRITE_INPUTS[action] if values is None else values)
        self.assertIsInstance(pending, ActionPendingApproval)
        return self.api.approvals.approve(pending.approval_id)

    def finish(self, approval):
        return ads.BUNDLED_TOOL.execute_approved(approval, self.api)

    def test_manifest_exposes_only_launch_end_writes_and_separate_oauth(self):
        self.assertEqual(len(ads.MANIFEST.actions), 7)
        self.assertEqual([a.id for a in ads.MANIFEST.actions if a.approval == "operator"], ["launch_campaign", "end_campaign"])
        self.assertEqual(ads.CREDENTIALS.scopes, ("openid", "email", ads.ADS_SCOPE))
        self.assertEqual([c.key for c in ads.MANIFEST.config], ["GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"])
        self.assertFalse(ads.MANIFEST.reports_cost)
        self.assertTrue(any(s.show_callback for s in ads.MANIFEST.setup_steps))
        self.assertEqual(len(ads.MANIFEST.data_summary.cards), 4)
        launch = ads.ACTIONS["launch_campaign"].input_schema
        self.assertEqual(len(launch["required"]), 9)
        self.assertNotIn("geo_target_ids", launch["required"])
        self.assertNotIn("cpc_bid_micros", launch["properties"])

    def test_country_lookup_uses_real_snapshot_without_credentials_or_provider_calls(self):
        self.api.credentials.clear()
        with patch.object(ads.CREDENTIALS, "access_token", side_effect=AssertionError("No OAuth for country lookup")):
            result = self.execute("list_locations", {})
        self.assertIsInstance(result, ActionExecuted)
        assert_matches_output_schema(self, ads.MANIFEST, "list_locations", result)
        snapshot = result.result
        self.assertEqual(snapshot["snapshot_date"], "2026-08-12")
        self.assertTrue(snapshot["source_url"].endswith("geotargets-2026-08-12.csv.zip"))
        self.assertFalse(snapshot["truncated"])
        rows = snapshot["rows"]
        self.assertEqual(len(rows), 219)
        self.assertEqual(len({row["geo_target_id"] for row in rows}), 219)
        self.assertTrue(all(row["geo_target_id"].isascii() and row["geo_target_id"].isdigit() for row in rows))
        self.assertTrue(all(row["target_type"] == "Country" and row["status"] == "Active" for row in rows))
        self.assertEqual([row["name"] for row in rows], sorted([row["name"] for row in rows], key=str.casefold))
        self.assertEqual(self.requests, [])
        self.assertEqual(self.api.approvals.records, {})

    def test_country_lookup_filters_names_codes_and_limit_with_unknowns_empty(self):
        result = self.execute("list_locations", {"query": "  UNITED KINGDOM  ", "country_code": "GB"})
        self.assertEqual(result.result["rows"], [{"geo_target_id": "2826", "name": "United Kingdom", "country_code": "GB", "target_type": "Country", "status": "Active"}])
        self.assertEqual(self.execute("list_locations", {"country_code": "US"}).result["rows"][0]["geo_target_id"], "2840")
        self.assertEqual(self.execute("list_locations", {"query": "No such country"}).result["rows"], [])
        self.assertEqual(self.execute("list_locations", {"query": "London"}).result["rows"], [])
        self.assertEqual(self.execute("list_locations", {"query": "United Kingdom", "country_code": "US"}).result["rows"], [])
        limited = self.execute("list_locations", {"limit": 1})
        self.assertEqual(len(limited.result["rows"]), 1)
        self.assertTrue(limited.result["truncated"])
        self.assertEqual(self.requests, [])

    def test_country_lookup_rejects_invalid_inputs_before_oauth_or_file_read(self):
        invalid = [{"query": "x" * 81}, {"query": "bad\nquery"}, {"query": None},
                   {"country_code": "gb"}, {"country_code": "GBR"}, {"country_code": "ＧＢ"},
                   {"limit": True}, {"limit": 0}, {"limit": 251}, {"limit": "1"},
                   {"customer_id": CUSTOMER}, {"path": "countries.json"}]
        with patch.object(ads, "list_locations", side_effect=AssertionError("Must validate before reading snapshot")):
            for values in invalid:
                with self.subTest(values=values):
                    self.assertIsInstance(self.execute("list_locations", values), ActionFailed)
        self.assertEqual(self.requests, [])

    def test_mapped_launch_and_end_errors_log_phase_resources_and_provider_detail(self):
        for stage in ("create", "activate", "stop"):
            with self.subTest(stage=stage):
                self.created = False
                self.creation_error = self.activation_error = None
                self.mutations.clear()
                approval = self.approve("end_campaign") if stage == "stop" else self.approve()
                error = WebRequestError("Google Ads request failed.", status=400,
                    body=b'{"error":{"code":400,"message":"provider-only detail","details":[{"errorCode":{"budgetError":"TOO_LOW"}}]}}')
                if stage == "create":
                    self.creation_error = error
                else:
                    self.activation_error = error
                self.diagnostics.reset_mock()
                result = self.finish(approval)
                self.assertIsInstance(result, ActionFailed)
                self.assertNotIn("provider-only detail", result.error)
                self.diagnostics.assert_called_once()
                context = self.diagnostics.call_args.args[0]["context"]
                self.assertEqual(context["http_status"], 400)
                self.assertIn("provider-only detail", context["provider_response"])
                self.assertIn("googleAds:mutate", context["operation"])
                self.assertEqual(len(self.mutations), 2 if stage == "activate" else 1)
                if stage != "create":
                    self.assertIn("campaigns/", context["confirmed_resources"])

    def test_unmapped_launch_error_retains_response_at_host_boundary(self):
        self.activation_error = WebRequestError("Google Ads request failed.", status=500,
            body=b'{"error":{"code":500,"message":"provider-only detail"},"request":{"secret":"excluded"}}')
        with self.assertRaises(ProviderWarning) as caught:
            self.finish(self.approve())
        self.assertIn("provider-only detail", caught.exception.response_body)
        self.assertNotIn("excluded", caught.exception.response_body)
        self.assertNotIn("provider-only detail", str(caught.exception))
        self.assertEqual(len(self.mutations), 2)

    def test_oauth_exchange_failure_is_diagnosed_without_logging_callback_or_tokens(self):
        redirect = "https://kern.example/tool-oauth/google_ads/callback"
        start = ads.CREDENTIALS.start_connect({"redirect_uri": redirect}, self.api)
        failure = WebRequestError("Google OAuth token exchange failed.", status=400,
            body=b'{"error":"invalid_grant","error_description":"provider-only detail"}')
        with patch("host.tools.shared.google.exchange_google_oauth_code", side_effect=failure) as exchange:
            with self.assertRaises(WebRequestError):
                ads.CREDENTIALS.complete_connect({"redirect_uri": redirect, "state": start["state"], "code": "private-code"}, self.api)
        exchange.assert_called_once()
        self.diagnostics.assert_called_once()
        context = self.diagnostics.call_args.args[0]["context"]
        self.assertEqual(context["action_id"], "oauth_complete_connect")
        self.assertEqual(context["http_status"], 400)
        self.assertIn("invalid_grant", context["provider_response"])
        self.assertNotIn("private-code", str(context))
        self.assertNotIn(redirect, str(context))

    def test_http_200_error_envelopes_do_not_become_successful_reads(self):
        for key in ("error", "partialFailureError"):
            with self.subTest(key=key), patch.object(ads, "json_request", return_value={key: {"code": 400, "message": "provider-only detail"}}) as request:
                self.diagnostics.reset_mock()
                result = self.execute("list_accounts", {})
                self.assertIsInstance(result, ActionFailed)
                self.assertNotIn("provider-only detail", result.error)
                request.assert_called_once()
                self.diagnostics.assert_called_once()
                context = self.diagnostics.call_args.args[0]["context"]
                self.assertEqual(context["http_status"], 200)
                self.assertIn("provider-only detail", context["provider_response"])

    def test_oauth_disconnect_exception_is_diagnosed_without_retry(self):
        failure = RuntimeError("Google token revocation failed.")
        with patch.object(ads, "revoke_google_token", side_effect=failure) as revoke:
            with self.assertRaises(RuntimeError):
                ads.CREDENTIALS.disconnect(self.api)
        revoke.assert_called_once()
        self.diagnostics.assert_called_once()
        self.assertEqual(self.diagnostics.call_args.args[0]["context"]["action_id"], "oauth_disconnect")
        self.assertIsNotNone(self.api.credentials.load())

    def test_oauth_disconnect_logs_rejected_revocation_and_still_clears_local_credentials(self):
        with patch.object(ads, "revoke_google_token", return_value={"success": False, "failure_type": "http", "status": 400}) as revoke:
            ads.CREDENTIALS.disconnect(self.api)
        revoke.assert_called_once()
        self.diagnostics.assert_called_once()
        context = self.diagnostics.call_args.args[0]["context"]
        self.assertEqual(context["action_id"], "oauth_disconnect")
        self.assertEqual(context["http_status"], 400)
        self.assertEqual(context["operation"], "POST OAuth revoke")
        self.assertIsNone(self.api.credentials.load())

    def test_google_nested_permission_evidence_survives_long_message_and_reconnect_chain(self):
        for status, category, code, reason in ((401, "authenticationError", "NOT_ADS_USER", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"), (403, "authorizationError", "USER_PERMISSION_DENIED", "SERVICE_DISABLED")):
            with self.subTest(status=status):
                body = json.dumps({"error": {"code": status, "status": "UNAUTHENTICATED" if status == 401 else "PERMISSION_DENIED", "message": "long provider message " * 1000, "details": [
                    {"@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure", "errors": [{"errorCode": {category: code}, "trigger": {"stringValue": "private query"}}], "requestId": "correlation-123"},
                    {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason, "metadata": {"private": "excluded"}},
                ]}}).encode()
                original = self.request
                def fail_preflight(method, url, **kwargs):
                    if "FROM customer LIMIT" in (kwargs.get("body") or {}).get("query", ""):
                        raise WebRequestError("Google Ads request failed.", status=status, body=body)
                    return original(method, url, **kwargs)
                self.diagnostics.reset_mock()
                with patch.object(ads, "json_request", side_effect=fail_preflight):
                    result = self.execute("list_campaigns", {"customer_id": CUSTOMER})
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(result.reconnect_required, status == 401)
                self.diagnostics.assert_called_once()
                record = self.diagnostics.call_args.args[0]
                context = record["context"]
                self.assertEqual(context["http_status"], status)
                self.assertIn("account preflight", context["operation"])
                self.assertEqual(context["google_error_codes"], f"{category}:{code}")
                self.assertEqual(context["google_error_reasons"], reason)
                self.assertEqual(context["google_request_id"], "correlation-123")
                self.assertTrue(context["provider_response_truncated"])
                self.assertLessEqual(len(json.dumps(context).encode()), 4096)
                self.assertNotIn("private query", str(context))
                self.assertNotIn("long provider message", result.error)
                self.assertNotIn("USER_PERMISSION_DENIED", result.error)

    def test_campaign_query_diagnostic_is_distinct_from_account_preflight(self):
        original = self.request
        def fail_campaign(method, url, **kwargs):
            query = (kwargs.get("body") or {}).get("query", "")
            if "FROM campaign " in query:
                raise WebRequestError("Google Ads request failed.", status=403, body=b'{"error":{"status":"PERMISSION_DENIED"}}')
            return original(method, url, **kwargs)
        with patch.object(ads, "json_request", side_effect=fail_campaign):
            result = self.execute("list_campaigns", {"customer_id": CUSTOMER})
        self.assertIsInstance(result, ActionFailed)
        self.diagnostics.assert_called_once()
        context = self.diagnostics.call_args.args[0]["context"]
        self.assertIn("campaign read", context["operation"])
        self.assertNotIn("SELECT", str(context))
        self.assertNotIn("account preflight", context["operation"])

    def test_unmapped_google_response_uses_chunked_context_at_host_boundary(self):
        body = json.dumps({"error": {"status": "INTERNAL", "message": "provider-only detail " * 400}}).encode()
        with patch.object(ads, "json_request", side_effect=WebRequestError("Google Ads request failed.", status=500, body=body)) as request:
            with self.assertRaises(UnmappedProviderError) as caught:
                self.execute("list_accounts", {})
        request.assert_called_once()
        self.diagnostics.assert_not_called()
        context = _provider_warning_context("google_ads", "list_accounts", caught.exception)
        from host.runtime.core import host_errors
        host_errors.report_warning("tools.provider_request", caught.exception, context=context, kind="provider_failure")
        self.diagnostics.assert_called_once()
        recorded = self.diagnostics.call_args.args[0]["context"]
        self.assertTrue(recorded["provider_response_truncated"])
        self.assertIn("provider_response_4", recorded)
        self.assertGreater(sum(len(v.encode()) for k, v in recorded.items() if k == "provider_response" or k.startswith("provider_response_") and isinstance(v, str)), 512)
        self.assertNotIn("provider-only detail", str(caught.exception))

    def test_google_oauth_requests_retain_exchange_and_userinfo_operations(self):
        self.identity.side_effect = get_google_userinfo
        redirect = "https://kern.example/tool-oauth/google_ads/callback"
        for stage, responses in (
            ("POST OAuth token exchange", [WebRequestError("Google OAuth exchange failed.", status=400, body=b'{"error":"invalid_grant"}')]),
            ("GET OAuth user-info lookup", [{"access_token": "private-token", "scope": ads.ADS_SCOPE}, WebRequestError("Google OAuth user-info failed.", status=403, body=b'{"error":{"status":"PERMISSION_DENIED"}}')]),
        ):
            with self.subTest(stage=stage):
                start = ads.CREDENTIALS.start_connect({"redirect_uri": redirect}, self.api)
                self.diagnostics.reset_mock()
                with patch("host.tools.shared.google.json_request", side_effect=responses) as request:
                    with self.assertRaises(RuntimeError):
                        ads.CREDENTIALS.complete_connect({"redirect_uri": redirect, "state": start["state"], "code": "private-code"}, self.api)
                self.assertEqual(request.call_count, len(responses))
                self.diagnostics.assert_called_once()
                context = self.diagnostics.call_args.args[0]["context"]
                self.assertEqual(context["operation"], stage)
                self.assertNotIn("private-token", str(context))
                self.assertNotIn("private-code", str(context))

    def test_oauth_warning_response_is_sanitized_before_reaching_host(self):
        from host.runtime.tools.api import OperatorError, _report_operator_provider_warning
        redirect = "https://kern.example/tool-oauth/google_ads/callback"
        bodies = [b'<html>https://google.example/oauth?client_secret=private-secret</html>', b'{"unexpected": "private-secret"}', b'{"error": {"message": "Authorization: Bearer private-secret"}}']
        for body in bodies:
            with self.subTest(body=body):
                start = ads.CREDENTIALS.start_connect({"redirect_uri": redirect}, self.api)
                source = ProviderWarning("Google", "POST OAuth token exchange", "Google OAuth failed.", status=500, body=body)
                source.diagnostic_context = {"extra_detail": "additional context"}
                with patch("host.tools.shared.google.GoogleCredentialStore.complete_connect", side_effect=source):
                    with self.assertRaises(ProviderWarning) as caught:
                        ads.CREDENTIALS.complete_connect({"redirect_uri": redirect, "state": start["state"], "code": "private-code"}, self.api)
                exc = caught.exception
                self.assertNotIn("private-secret", exc.response_body)
                context = _provider_warning_context("google_ads", "oauth_complete_connect", exc)
                self.assertEqual(context["extra_detail"], "additional context")
                self.assertNotIn("private-secret", json.dumps(context))
                self.diagnostics.reset_mock()
                with self.assertRaises(OperatorError):
                    _report_operator_provider_warning("google_ads", "oauth_complete_connect", exc)
                self.diagnostics.assert_called_once()
                self.assertNotIn("private-secret", json.dumps(self.diagnostics.call_args.args[0]))

    def test_google_oauth_server_failure_retains_bounded_details_in_diagnostics(self):
        redirect = "https://kern.example/tool-oauth/google_ads/callback"
        start = ads.CREDENTIALS.start_connect({"redirect_uri": redirect}, self.api)
        body = json.dumps({"error": "server_error", "error_description": "provider-only detail " * 300}).encode()
        with patch("host.tools.shared.google.json_request", side_effect=WebRequestError("Google OAuth exchange failed.", status=500, body=body)) as request:
            with self.assertRaises(RuntimeError):
                ads.CREDENTIALS.complete_connect({"redirect_uri": redirect, "state": start["state"], "code": "private-code"}, self.api)
        request.assert_called_once()
        self.diagnostics.assert_called_once()
        context = self.diagnostics.call_args.args[0]["context"]
        self.assertEqual(context["operation"], "POST OAuth token exchange")
        self.assertEqual(context["http_status"], 500)
        self.assertTrue(context["provider_response_truncated"])
        self.assertIn("provider_response_4", context)
        self.assertNotIn("private-code", str(context))

    def test_initial_bulk_mutate_rejection_retains_typed_codes_location_and_request_id(self):
        error_body = json.dumps({"error": {
            "code": 400, "status": "INVALID_ARGUMENT", "message": "provider-only detail " * 500, "details": [{
                "@type": "type.googleapis.com/google.ads.googleads.v25.errors.GoogleAdsFailure", "requestId": "bulk-correlation-123",
                "errors": [{"errorCode": {"budgetError": "INVALID_BUDGET_AMOUNT"}, "message": "Rejected bulk field", "trigger": {"stringValue": "private request value"}, "location": {"fieldPathElements": [
                    {"fieldName": "mutate_operations", "index": 0}, {"fieldName": "campaign_budget_operation"}, {"fieldName": "create"}, {"fieldName": "total_amount_micros"},
                ]}}],
            }],
        }}).encode()
        self.assertGreater(len(error_body), 4096)
        approval = self.approve()
        self.diagnostics.reset_mock()
        fixture = self.request
        def route(method, url, **kwargs):
            if url.endswith("googleAds:mutate"):
                self.mutations.append(kwargs.get("body"))
                return web.json_request(method, url, **kwargs)
            return fixture(method, url, **kwargs)
        response = urllib.error.HTTPError("https://google.example/mutate", 400, "Bad Request", {}, io.BytesIO(error_body))
        with patch.object(ads, "json_request", side_effect=route), patch.object(web._OPENER, "open", side_effect=response) as opening:
            result = self.finish(approval)
        opening.assert_called_once()
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("none returned", result.error)
        self.assertEqual(len(self.mutations), 1)
        self.diagnostics.assert_called_once()
        context = self.diagnostics.call_args.args[0]["context"]
        self.assertEqual(context["http_status"], 400)
        self.assertEqual(context["phase"], "creating the paused campaign")
        self.assertIn("googleAds:mutate", context["operation"])
        self.assertIn("provider-only detail", context["provider_response"])
        self.assertEqual(context["google_error_codes"], "budgetError:INVALID_BUDGET_AMOUNT")
        self.assertEqual(context["google_request_id"], "bulk-correlation-123")
        self.assertEqual(context["google_error_paths"], "mutate_operations[0].campaign_budget_operation.create.total_amount_micros")
        self.assertFalse(context["google_error_paths_truncated"])
        self.assertTrue(context["provider_response_truncated"])
        self.assertNotIn("private request value", str(context))

    def test_direct_accounts_are_ids_only_and_capped(self):
        self.direct = [str(1000000000 + i) for i in range(51)]
        result = self.execute("list_accounts", {})
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(len(result.result["customer_ids"]), 50)
        self.assertTrue(result.result["truncated"])
        self.assertEqual(len(self.requests), 1)
        assert_matches_output_schema(self, ads.MANIFEST, "list_accounts", result)

    def test_removed_manager_action_and_fields_fail_before_provider_requests(self):
        result = self.execute("list_client_accounts", {"manager_customer_id": "9876543210"})
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(self.requests, [])
        inputs = {
            "list_accounts": {},
            "list_campaigns": {"customer_id": CUSTOMER},
            "list_ad_groups": {"customer_id": CUSTOMER, "campaign_id": "77"},
            "report": {"customer_id": CUSTOMER, "report_type": "campaigns", "start_date": "2026-10-01", "end_date": "2026-10-08"},
            **WRITE_INPUTS,
        }
        for action, values in inputs.items():
            for field in ("login_customer_id", "manager_customer_id"):
                with self.subTest(action=action, field=field):
                    result = self.execute(action, {**values, field: "9876543210"})
                    self.assertIsInstance(result, ActionFailed)
                    self.assertEqual(self.requests, [])
                    self.assertNotIn(field, ads.ACTIONS[action].input_schema["properties"])

    def test_fixed_nonpolitical_declaration_is_sent_and_cannot_be_overridden_by_old_approval(self):
        self.assertNotIn("contains_eu_political_advertising", ads.ACTIONS["launch_campaign"].input_schema["properties"])
        approval = self.approve()
        self.assertNotIn("contains_eu_political_advertising", approval.payload["input"])
        for value in (True, False):
            payload = copy.deepcopy(approval.payload)
            payload["input"]["contains_eu_political_advertising"] = value
            self.requests.clear()
            self.assertIsInstance(self.finish(replace(approval, payload=payload)), ActionFailed)
            self.assertEqual(self.requests, [])
            self.assertEqual(self.mutations, [])
        self.assertIsInstance(self.finish(approval), ApprovalExecuted)
        campaign = self.mutations[0][1]["campaignOperation"]["create"]
        self.assertEqual(campaign["containsEuPoliticalAdvertising"], "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING")

    def test_old_manager_approval_inputs_are_rejected_before_provider_requests(self):
        for action in WRITE_INPUTS:
            approval = self.approve(action)
            payload = copy.deepcopy(approval.payload)
            payload["input"]["login_customer_id"] = "9876543210"
            self.requests.clear()
            result = self.finish(replace(approval, payload=payload))
            self.assertIsInstance(result, ActionFailed)
            self.assertEqual(self.requests, [])
        self.assertEqual(self.mutations, [])

    def test_unowned_or_indirect_account_stops_before_target(self):
        result = self.execute("list_campaigns", {"customer_id": "1111111111"})
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("direct access", result.error)
        self.assertFalse(any("FROM campaign " in (r[2].get("body") or {}).get("query", "") for r in self.requests))

    def test_manager_cannot_be_target_of_campaign_operations(self):
        self.account["manager"] = True
        result = self.execute("list_campaigns", {"customer_id": CUSTOMER})
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("not a manager", result.error)

    def test_all_direct_result_schemas_and_account_currency(self):
        for action, values in (
            ("list_campaigns", {"customer_id": CUSTOMER}),
            ("list_ad_groups", {"customer_id": CUSTOMER, "campaign_id": "77"}),
            ("report", {"customer_id": CUSTOMER, "report_type": "campaigns", "start_date": "2026-10-01", "end_date": "2026-10-08"}),
        ):
            with self.subTest(action=action):
                result = self.execute(action, values)
                self.assertIsInstance(result, ActionExecuted)
                self.assertEqual(result.result["account"]["currency_code"], "INR")
                assert_matches_output_schema(self, ads.MANIFEST, action, result)

    def test_report_templates_exact_micros_fractional_conversions_and_truncation(self):
        self.result_rows = [{"campaign": {"id": "77", "name": "Search"}, "segments": {"date": "2026-10-08"},
            "adGroup": {"id": "99"}, "adGroupCriterion": {"criterionId": "123", "keyword": {"text": "agent host", "matchType": "EXACT"}},
            "searchTermView": {"searchTerm": "ai hosting"}, "metrics": {"impressions": "9007199254740993", "clicks": "3", "costMicros": "1234567", "conversions": 0.5, "conversionsValue": 2.5}}] * 2
        for kind, source in (("campaigns", "campaign"), ("keywords", "keyword_view"), ("search_terms", "search_term_view")):
            result = self.execute("report", {"customer_id": CUSTOMER, "report_type": kind, "start_date": "2026-10-01", "end_date": "2026-10-08", "campaign_id": "77", "limit": 1})
            self.assertIsInstance(result, ActionExecuted)
            self.assertTrue(result.result["truncated"])
            self.assertEqual(len(result.result["rows"]), 1)
            row = result.result["rows"][0]
            self.assertEqual(row["impressions"], "9007199254740993")
            self.assertEqual(row["cost_micros"], "1234567")
            self.assertEqual(row["conversions"], 0.5)
            query = self.requests[-1][2]["body"]["query"]
            self.assertIn(f"FROM {source} WHERE", query)
            self.assertIn("campaign.advertising_channel_type = 'SEARCH'", query)
            self.assertIn("campaign.id = 77", query)
            self.assertIn("LIMIT 2", query)
            assert_matches_output_schema(self, ads.MANIFEST, "report", result)

    def test_provider_errors_are_curated_and_401_requires_reconnect(self):
        for status, fragment in ((401, "Reconnect"), (403, "production API access"), (429, "quota"), (400, "rejected")):
            with self.subTest(status=status):
                self.provider.side_effect = WebRequestError("secret provider message", status=status, body=b"private data")
                result = self.execute("list_accounts", {})
                self.assertIsInstance(result, ActionFailed)
                self.assertIn(fragment, result.error)
                self.assertNotIn("secret", result.error)
                self.assertEqual(result.reconnect_required, status == 401)
        self.provider.side_effect = WebRequestError("secret provider message", status=500)
        with self.assertRaises(UnmappedProviderError):
            self.execute("list_accounts", {})

    def test_disconnected_and_missing_scope_require_reconnect(self):
        self.api.credentials.clear()
        result = self.execute("list_accounts", {})
        self.assertIsInstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertEqual(self.requests, [])

    def test_missing_ads_scope_does_not_borrow_another_google_connection(self):
        self.api = connected_google_api("google_ads", frozenset({"https://www.googleapis.com/auth/webmasters"}))
        result = self.execute("list_accounts", {})
        self.assertIsInstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertEqual(self.requests, [])

    def test_invalid_provider_rows_and_nonfinite_metrics_fail_without_partial_results(self):
        self.result_rows = [{"campaign": {"id": "77", "name": "Search"}, "segments": {"date": "2026-10-08"}, "metrics": {"conversions": float("inf")}}]
        result = self.execute("report", {"customer_id": CUSTOMER, "report_type": "campaigns", "start_date": "2026-10-01", "end_date": "2026-10-08"})
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("invalid conversions", result.error)
        self.provider.side_effect = lambda *a, **kw: {"resourceNames": ["customers/invalid"]}
        result = self.execute("list_accounts", {})
        self.assertIsInstance(result, ActionFailed)

    def test_invalid_inputs_fail_before_authentication_or_egress(self):
        bad = [
            ("list_accounts", {"extra": 1}), ("list_campaigns", {"customer_id": "123-456-7890"}),
            ("list_campaigns", {"customer_id": "１２３４５６７８９０"}), ("list_campaigns", {"customer_id": CUSTOMER, "limit": True}),
            ("end_campaign", {"customer_id": CUSTOMER, "campaign_id": "77 OR 1=1"}),
            ("report", {"customer_id": CUSTOMER, "report_type": "campaigns", "start_date": "2026-02-30", "end_date": "2026-10-08"}),
            ("report", {"customer_id": CUSTOMER, "report_type": "campaigns", "start_date": "2025-01-01", "end_date": "2026-10-08"}),
        ]
        changes = [
            {"name": "é" * 128}, {"name": "界" * 86},
            {"total_budget_micros": 0}, {"total_budget_micros": True}, {"total_budget_micros": 1.5}, {"total_budget_micros": 1000000000001},
            {"headlines": ["Same", "Same", "Third"]}, {"headlines": ["界" * 31, "Second", "Third"]},
            {"language_ids": ["1000"]}, {"cpc_bid_micros": 1}, {"daily_budget_micros": 1}, {"objective": "CONVERSIONS"},
            {"contains_eu_political_advertising": False}, {"contains_eu_political_advertising": True}, {"contains_eu_political_advertising": None}, {"keywords": [{"text": "word", "match_type": "EXACT", "extra": "no"}]},
            {"final_url": "https://user:secret@example.com/"}, {"final_url": "http://example.com/"},
            {"geo_target_ids": ["2356", "2356"]}, {"geo_target_ids": None},
            {"start_time": "2026-02-30T09:00:00Z"}, {"start_time": "2026-10-15T09:00:00"},
            {"start_time": "2026-10-15T09:00:00.123Z"}, {"end_time": CREATE["start_time"]},
            {"end_time": "2027-10-22T09:00:00Z"},
        ]
        bad += [("launch_campaign", {**CREATE, **change}) for change in changes]
        bad += [(old, {}) for old in ("create_search_campaign", "set_campaign_status", "set_campaign_budget", "add_keywords", "create_responsive_search_ad")]
        for action, values in bad:
            with self.subTest(action=action, values=values):
                self.assertIsInstance(self.execute(action, values), ActionFailed)
        self.assertEqual(self.requests, [])
        self.identity.assert_not_called()
        self.assertEqual(self.api.approvals.records, {})

    def test_multibyte_name_at_budget_byte_limit_launches_verbatim(self):
        name = "é" * 127 + "a"
        self.assertEqual(len(name.encode("utf-8")), 255)
        approval = self.approve(values={**CREATE, "name": name})
        self.new_campaign["campaign"]["name"] = name
        self.new_groups[0]["adGroup"]["name"] = name
        self.assertIsInstance(self.finish(approval), ApprovalExecuted)
        operations = self.mutations[0]
        for index, kind in ((0, "campaignBudget"), (1, "campaign"), (3, "adGroup")):
            self.assertEqual(operations[index][kind + "Operation"]["create"]["name"], name)

    def test_double_width_ad_copy_limits_before_approval(self):
        for changes in ({"headlines": ["界" * 16, "Second", "Third"]},
                        {"headlines": ["Ａ" * 15 + "a", "Second", "Third"]},
                        {"descriptions": ["界" * 46, "Second"]}):
            with self.subTest(changes=changes):
                self.assertIsInstance(self.execute("launch_campaign", {**CREATE, **changes}), ActionFailed)
        self.assertEqual(self.requests, [])
        self.identity.assert_not_called()
        self.assertEqual(self.api.approvals.records, {})
        copy = {"headlines": ["界" * 15, "カ" * 14 + "ab", "가" * 15],
                "descriptions": ["界" * 45, "界" * 44 + "ab"]}
        approval = self.approve(values={**CREATE, **copy})
        rsa = self.new_ads[0]["adGroupAd"]["ad"]["responsiveSearchAd"]
        rsa.update({key: [{"text": text} for text in texts] for key, texts in copy.items()})
        self.assertIsInstance(self.finish(approval), ApprovalExecuted)
        self.assertEqual(self.mutations[0][-1]["adGroupAdOperation"]["create"]["ad"]["responsiveSearchAd"], rsa)

    def test_omitted_accessible_accounts_returns_empty_list(self):
        self.provider.side_effect = lambda *args, **kwargs: {}
        result = self.execute("list_accounts", {})
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(result.result, {"customer_ids": [], "truncated": False})
        assert_matches_output_schema(self, ads.MANIFEST, "list_accounts", result)

    def test_every_write_queues_exact_input_with_no_mutation(self):
        for action, values in WRITE_INPUTS.items():
            pending = self.execute(action, values)
            self.assertIsInstance(pending, ActionPendingApproval)
            payload = self.api.approvals.get(pending.approval_id).payload
            self.assertEqual(payload["google_account"]["id"], "google-sub-1")
            self.assertEqual(payload["account"]["currency_code"], "INR")
            self.assertEqual(payload["input"], values)
            self.assertNotIn("google_ads-access-token", str(payload))
            if action == "launch_campaign":
                self.assertEqual(payload["flight"]["start_date_time"], "2026-10-15 00:00:00")
        self.assertEqual(self.mutations, [])

    def test_launch_builds_paused_atomically_verifies_then_enables_once(self):
        approval = self.approve()
        result = self.finish(approval)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertIn("configured ENABLED", result.message)
        self.assertIn("REVIEW_IN_PROGRESS", result.message)
        self.assertIn("not confirmation", result.message)
        self.assertEqual(len(self.api.approvals.records), 1)
        self.assertEqual(len(self.mutations), 2)
        create, enable = self.mutations
        self.assertEqual(len(create), 6)
        budget = create[0]["campaignBudgetOperation"]["create"]
        self.assertEqual(budget["period"], "CUSTOM_PERIOD")
        self.assertEqual(budget["totalAmountMicros"], "10000000")
        self.assertNotIn("amountMicros", budget)
        self.assertFalse(budget["explicitlyShared"])
        campaign = create[1]["campaignOperation"]["create"]
        self.assertEqual(campaign["status"], "PAUSED")
        self.assertEqual(campaign["targetSpend"], {})
        self.assertNotIn("manualCpc", campaign)
        self.assertEqual(campaign["startDateTime"], "2026-10-15 00:00:00")
        self.assertEqual(campaign["endDateTime"], "2026-10-22 23:59:59")
        self.assertFalse(campaign["networkSettings"]["targetSearchNetwork"])
        self.assertFalse(campaign["networkSettings"]["targetContentNetwork"])
        self.assertNotIn("cpcBidMicros", create[3]["adGroupOperation"]["create"])
        self.assertFalse(any("language" in op.get("campaignCriterionOperation", {}).get("create", {}) for op in create))
        self.assertEqual(enable, [{"campaignOperation": {"updateMask": "status", "update": {"resourceName": CAMPAIGN_RESOURCE, "status": "ENABLED"}}}])
        urls = [request[1] for request in self.requests]
        self.assertGreater(urls.index(next(url for url in urls if url.endswith(":mutate"))), 0)
        self.assertGreater(self.identity.call_count, 2)

    def test_omitted_and_empty_locations_launch_worldwide_without_geo_operations(self):
        for empty in (False, True):
            with self.subTest(explicit_empty=empty):
                self.created = False
                self.mutations.clear()
                self.new_geo = []
                self.new_keywords[0]["adGroupCriterion"]["resourceName"] = f"customers/{CUSTOMER}/adGroupCriteria/102~303"
                values = dict(CREATE)
                if empty:
                    values["geo_target_ids"] = []
                else:
                    values.pop("geo_target_ids")
                approval = self.approve(values=values)
                self.assertEqual(approval.payload["input"]["geo_target_ids"], [])
                self.assertIsInstance(self.finish(approval), ApprovalExecuted)
                self.assertFalse(any("campaignCriterionOperation" in op for op in self.mutations[0]))

    def test_offset_times_normalize_to_utc_and_bind_account_local_flight(self):
        approval = self.approve(values={**CREATE, "start_time": "2026-10-15T00:00:00+05:30", "end_time": "2026-10-22T23:59:59+05:30"})
        self.assertEqual(approval.payload["input"]["start_time"], CREATE["start_time"])
        self.assertEqual(approval.payload["flight"]["time_zone"], "Asia/Kolkata")
        self.assertIsInstance(self.finish(approval), ApprovalExecuted)

    def test_search_flight_boundaries_and_inclusive_duration_checked_before_approval(self):
        for changes in (
            {"start_time": "2026-10-15T09:00:00+05:30"},
            {"end_time": "2026-10-22T09:00:00+05:30"},
            {"end_time": "2026-10-16T23:59:59+05:30"},
            {"end_time": "2027-01-13T23:59:59+05:30"},
        ):
            with self.subTest(changes=changes):
                result = self.execute("launch_campaign", {**CREATE, **changes})
                self.assertIsInstance(result, ActionFailed)
        self.assertEqual(self.api.approvals.records, {})
        self.assertEqual(self.mutations, [])
        pending = self.execute("launch_campaign", {**CREATE, "end_time": "2026-10-17T23:59:59+05:30"})
        self.assertIsInstance(pending, ActionPendingApproval)
        self.account["timeZone"] = "America/New_York"
        pending = self.execute("launch_campaign", {**CREATE, "start_time": "2026-10-15T00:00:00-04:00", "end_time": "2027-01-12T23:59:59-05:00"})
        self.assertIsInstance(pending, ActionPendingApproval)

    def test_edit_during_access_recheck_is_caught_by_final_readback(self):
        approval = self.approve()
        def identity_during_access(*args, **kwargs):
            if self.created:
                self.new_campaign["campaignBudget"]["totalAmountMicros"] = "999999"
            return google_userinfo()
        self.identity.side_effect = identity_during_access
        result = self.finish(approval)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("total budget differs", result.error)
        self.assertEqual(len(self.mutations), 1)

    def test_expired_or_past_start_date_fails_at_proposal_and_before_creation(self):
        self.assertIsInstance(self.execute("launch_campaign", {**CREATE, "start_time": "2026-10-01T09:00:00Z", "end_time": "2026-10-02T09:00:00Z"}), ActionFailed)
        approval = self.approve()
        self.clock.return_value = datetime(2026, 10, 23, tzinfo=timezone.utc)
        self.assertIsInstance(self.finish(approval), ActionFailed)
        self.assertEqual(self.mutations, [])

    def test_unknown_zone_and_ambiguous_dst_time_refuse_proposal(self):
        for zone, start, end in (("Unknown/Zone", CREATE["start_time"], CREATE["end_time"]), ("America/New_York", "2026-11-01T05:30:00Z", "2026-11-08T05:30:00Z")):
            self.account["timeZone"] = zone
            self.assertIsInstance(self.execute("launch_campaign", {**CREATE, "start_time": start, "end_time": end}), ActionFailed)
        self.assertEqual(self.mutations, [])

    def test_launch_account_currency_time_zone_and_identity_changes_block_creation(self):
        approval = self.approve()
        for key, value in (("currencyCode", "USD"), ("timeZone", "UTC")):
            self.account = copy.deepcopy(ACCOUNT)
            self.account[key] = value
            self.assertIsInstance(self.finish(approval), ActionFailed)
        self.account = copy.deepcopy(ACCOUNT)
        self.identity.return_value = google_userinfo(sub="another-user")
        result = self.finish(approval)
        self.assertIsInstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertEqual(self.mutations, [])

    def test_each_configuration_drift_blocks_activation_and_returns_created_ids(self):
        cases = [
            lambda: self.new_campaign["campaignBudget"].update(totalAmountMicros="999999"),
            lambda: self.new_campaign["campaignBudget"].update(explicitlyShared=True),
            lambda: self.new_campaign["campaign"].update(endDateTime="2026-12-22 14:30:00"),
            lambda: self.new_campaign["campaign"].update(biddingStrategyType="MANUAL_CPC"),
            lambda: self.new_campaign["campaign"].update(campaignBudget=f"customers/1111111111/campaignBudgets/100"),
            lambda: self.new_campaign["campaign"].update(targetSpend={"cpcBidCeilingMicros": "1000000"}),
            lambda: self.new_campaign["campaign"]["networkSettings"].update(targetContentNetwork=True),
            lambda: self.new_campaign["campaign"].update(containsEuPoliticalAdvertising="CONTAINS_EU_POLITICAL_ADVERTISING"),
            lambda: self.new_campaign["campaign"].pop("containsEuPoliticalAdvertising"),
            lambda: self.new_geo.clear(),
            lambda: self.new_groups.append(copy.deepcopy(self.new_groups[0])),
            lambda: self.new_keywords[0]["adGroupCriterion"]["keyword"].update(matchType="BROAD"),
            lambda: self.new_keywords[0]["adGroupCriterion"].update(status="PAUSED"),
            lambda: self.new_ads[0]["adGroupAd"]["ad"].update(finalUrls=["https://other.example/"]),
            lambda: self.new_ads[0]["adGroupAd"]["ad"]["responsiveSearchAd"]["headlines"][0].update(text="Changed"),
            lambda: self.new_ads.append(copy.deepcopy(self.new_ads[0])),
        ]
        for index, alter in enumerate(cases):
            with self.subTest(case=index):
                self.created = False
                self.reset_created_state()
                self.mutations.clear()
                approval = self.approve()
                alter()
                result = self.finish(approval)
                self.assertIsInstance(result, ActionFailed)
                self.assertIn(CAMPAIGN_RESOURCE, result.error)
                self.assertEqual(len(self.mutations), 1)

    def test_expired_flight_during_readback_stops_before_enable(self):
        approval = self.approve()
        self.read_hook = lambda: setattr(self.clock, "return_value", datetime(2026, 10, 23, tzinfo=timezone.utc))
        self.assertIsInstance(self.finish(approval), ActionFailed)
        self.assertEqual(len(self.mutations), 1)

    def test_account_membership_and_identity_rechecked_before_activation(self):
        approval = self.approve()
        self.creation_hook = lambda: self.direct.clear()
        result = self.finish(approval)
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(len(self.mutations), 1)

    def test_identity_changes_during_creation_require_reconnect_before_enable(self):
        approval = self.approve()
        self.creation_hook = lambda: setattr(self.identity, "return_value", google_userinfo(sub="another-user"))
        result = self.finish(approval)
        self.assertIsInstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertEqual(len(self.mutations), 1)

    def test_review_pending_unknown_or_approved_is_distinct_from_delivery(self):
        for policy in ({}, {"reviewStatus": "REVIEW_IN_PROGRESS"}, {"reviewStatus": "ELIGIBLE_MAY_SERVE"}, {"reviewStatus": "REVIEWED", "approvalStatus": "APPROVED_LIMITED"}):
            self.created = False
            self.mutations.clear()
            self.new_ads[0]["adGroupAd"]["policySummary"] = policy
            result = self.finish(self.approve())
            self.assertIsInstance(result, ApprovalExecuted)
            self.assertIn("not confirmation of approval or actual delivery", result.message)
            self.assertIn("during or after review", result.message)
            self.assertEqual(len(self.mutations), 2)
        self.created = False
        self.mutations.clear()
        self.new_ads[0]["adGroupAd"]["policySummary"] = {"reviewStatus": "REVIEWED", "approvalStatus": "DISAPPROVED"}
        result = self.finish(self.approve())
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("disapproved", result.error)
        self.assertEqual(len(self.mutations), 1)

    def test_malformed_creation_or_activation_outcome_never_retries(self):
        for phase, expected_calls in (("create", 1), ("activate", 2)):
            self.created = False
            self.mutations.clear()
            self.bad_result = phase
            result = self.finish(self.approve())
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("outcome is unclear", result.error)
            self.assertIn("Inspect Google Ads", result.error)
            self.assertEqual(len(self.mutations), expected_calls)
            if phase == "activate":
                self.assertIn(CAMPAIGN_RESOURCE, result.error)

    def test_activation_transport_failure_preserves_diagnostics_and_confirmed_ids(self):
        approval = self.approve()
        self.activation_error = WebRequestError("secret provider body", status=500)
        with self.assertRaises(ProviderWarning) as caught:
            self.finish(approval)
        self.assertIn("enabling", str(caught.exception))
        self.assertIn(CAMPAIGN_RESOURCE, str(caught.exception))
        self.assertIn("may already have started paid delivery", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(caught.exception.status, 500)
        self.assertEqual(len(self.mutations), 2)

    def test_creation_transport_failure_never_attempts_enable_or_retry(self):
        self.creation_error = WebRequestError("timeout", status=0)
        with self.assertRaises(ProviderWarning) as caught:
            self.finish(self.approve())
        self.assertIn("none returned", str(caught.exception))
        self.assertEqual(len(self.mutations), 1)

    def test_mutation_foreign_type_account_or_update_target_is_not_success(self):
        for wrong in ("customers/1111111111/campaigns/101", f"customers/{CUSTOMER}/adGroups/101", f"customers/{CUSTOMER}/campaigns/999"):
            with self.subTest(resource=wrong):
                self.created = False
                self.mutations.clear()
                def replace_update(method, url, **kwargs):
                    result = self.request(method, url, **kwargs)
                    if url.endswith(":mutate") and "update" in kwargs["body"]["mutateOperations"][0].get("campaignOperation", {}):
                        result["mutateOperationResponses"][0]["campaignResult"]["resourceName"] = wrong
                    return result
                self.provider.side_effect = replace_update
                result = self.finish(self.approve())
                self.assertIsInstance(result, ActionFailed)
                self.assertIn(CAMPAIGN_RESOURCE, result.error)
                self.assertIn("enabling", result.error)
                self.assertEqual(len(self.mutations), 2)

    def test_end_ambiguous_response_explains_inspection_and_never_retries(self):
        self.bad_result = "activate"
        result = self.finish(self.approve("end_campaign"))
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("campaigns/77", result.error)
        self.assertIn("Inspect Google Ads", result.error)
        self.assertEqual(len(self.mutations), 1)

    def test_end_allows_mutable_campaign_drift_and_only_sets_paused(self):
        approval = self.approve("end_campaign")
        self.campaign["campaign"].update(name="Renamed", status="ENABLED", endDateTime="2027-01-01 23:59:59")
        self.campaign["campaignBudget"].update(amountMicros="500000000", explicitlyShared=True)
        self.account["descriptiveName"] = "Renamed Account"
        result = self.finish(approval)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertIn("no resume", result.message)
        self.assertEqual(self.mutations, [[{"campaignOperation": {"updateMask": "status", "update": {"resourceName": f"customers/{CUSTOMER}/campaigns/77", "status": "PAUSED"}}}]])

    def test_end_never_targets_non_search_or_different_campaign(self):
        approval = self.approve("end_campaign")
        for field, value in (("advertisingChannelType", "DISPLAY"), ("id", "88")):
            self.campaign = copy.deepcopy(CAMPAIGN)
            self.campaign["campaign"][field] = value
            self.assertIsInstance(self.finish(approval), ActionFailed)
        self.assertEqual(self.mutations, [])

    def test_revoked_direct_access_blocks_both_writes(self):
        for action, values in WRITE_INPUTS.items():
            self.direct = [CUSTOMER]
            approval = self.approve(action, values)
            self.direct = []
            self.assertIsInstance(self.finish(approval), ActionFailed)
        self.assertEqual(self.mutations, [])

    def test_approval_action_and_flight_mismatch_blocks_before_mutation(self):
        approval = self.approve()
        self.requests.clear()
        self.assertIsInstance(self.finish(replace(approval, action_id="end_campaign")), ActionFailed)
        self.assertEqual(self.requests, [])
        changed = copy.deepcopy(approval.payload)
        changed["flight"]["end_date_time"] = "2027-01-01 00:00:00"
        self.assertIsInstance(self.finish(replace(approval, payload=changed)), ActionFailed)
        self.assertEqual(self.mutations, [])

    def test_summary_has_human_name_total_currency_and_click_objective(self):
        pending = self.execute("launch_campaign", CREATE)
        self.assertIsInstance(pending, ActionPendingApproval)
        for text in ("Kern Search", "Example Ads", "10 INR", "Maximize Clicks", "One approval"):
            self.assertIn(text, pending.summary)
        self.assertLessEqual(len(pending.summary.encode("utf-8")), 500)
        pending = self.execute("end_campaign", WRITE_INPUTS["end_campaign"])
        self.assertIn("Search (77)", pending.summary)
        self.assertIn("no Kern resume", pending.summary)
