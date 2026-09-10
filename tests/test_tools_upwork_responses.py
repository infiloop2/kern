"""Structured-output contracts and lossless handling of provider shape changes."""

import json
import unittest

from host.runtime.tools.tools_host import validate_against_schema
from host.tools.upwork.actions import GET_ACCOUNT, OPERATIONS
from host.tools.upwork.responses import structured_result, MAX_BYTES
from test_tools_upwork import connected_api


class UpworkResponseTests(unittest.TestCase):
    def result(self, data, action):
        value = structured_result(json.dumps(data), action, connected_api().credentials.load())
        self.assertEqual(validate_against_schema(value, OPERATIONS[action].spec.output_schema), "")
        return value

    def test_complete_job_description_and_eligibility_are_typed(self):
        description = "Full job requirements. " * 3000
        data = {"data": {"marketplaceJobPosting": {"id": "123", "content": {"title": "Automation engineer", "description": description}}},
                "can_apply": False, "connects_cost": 21, "connects_balance": 12}
        self.assertEqual(self.result(data, "get_job"), data)

    def test_search_page_preserves_typed_cursor_and_client(self):
        data = {"jobs": [{"id": "123", "title": "Engineer", "client": {"rating": 4.7, "country": "UK"}}],
                "hasMore": True, "next_cursor": "next-page"}
        self.assertEqual(self.result(data, "search_jobs"), data)

    def test_integer_outputs_normalize_integral_floats_only(self):
        for value in (10, 10.0, 10.5, True):
            with self.subTest(value=value):
                result = self.result({"hasMore": False, "message_count": value}, "read_messages")
                if type(value) is not bool and value == 10:
                    self.assertIs(type(result["message_count"]), int)
                    self.assertEqual(result["message_count"], 10)
                else:
                    self.assertNotIn("message_count", result)
                    self.assertEqual(json.loads(result["provider_details_json"])["message_count"], value)

    def test_unknown_nested_fields_preserve_complete_provider_json(self):
        data = {"jobs": [{"id": "123", "new_requirements": {"text": "x" * 20000}}], "hasMore": False}
        result = self.result(data, "search_jobs")
        self.assertEqual(result["jobs"], [{"id": "123"}])
        self.assertEqual(json.loads(result["provider_details_json"]), data)

    def test_changed_types_are_not_coerced_or_lost(self):
        data = {"data": {"marketplaceJobPosting": {"id": "123", "content": {"title": "Engineer", "description": "Project"}}}, "connects_cost": "unknown", "can_apply": None, "connects_balance": 12}
        result = self.result(data, "get_job")
        self.assertNotIn("can_apply", result)
        self.assertNotIn("connects_cost", result)
        self.assertEqual(result["connects_balance"], 12)
        self.assertEqual(json.loads(result["provider_details_json"]), data)

    def test_unknown_section_shape_is_preserved_in_account_contract(self):
        data = {"new_dashboard_section": {"unread": 3}}
        result = self.result(data, "get_dashboard")
        self.assertEqual(json.loads(result["provider_details_json"]), data)
        self.assertEqual(validate_against_schema({"dashboard": result}, GET_ACCOUNT.output_schema), "")

    def test_empty_pages_include_required_provider_fallback(self):
        for action, data in (("get_connects_balance", {"hasMore": False}),
                             ("read_messages", {"hasMore": False, "message_count": 0}),
                             ("list_conversations", {"hasMore": False})):
            with self.subTest(action=action):
                result = self.result(data, action)
                self.assertEqual(json.loads(result["provider_details_json"]), data)
                self.assertFalse(result["hasMore"])

    def test_documented_highlights_have_typed_identifiers(self):
        data = {"certificates": [{"id": "cert-one", "name": "Certificate"}],
                "portfolio_projects": [{"id": "project-one", "title": "Project"}]}
        self.assertEqual(self.result(data, "get_profile_highlights"), data)

    def test_fallback_redacts_escaped_credentials_in_keys_and_values(self):
        raw = '{"new": {"\\u0050RIVATE_ACCESS_TOKEN": "\\u0050RIVATE_REFRESH_TOKEN"}}'
        result = structured_result(raw, "get_profile", connected_api().credentials.load())
        self.assertEqual(json.loads(result["provider_details_json"]), {"new": {"[redacted]": "[redacted]"}})

    def test_oversized_malformed_duplicate_and_nonfinite_json_are_rejected(self):
        for raw in ('{"body":"' + 'x' * MAX_BYTES + '"}', '{"x": 1, "x": 2}', '{"x": NaN}', '{"x": 1e999}', '[]', 'Not JSON'):
            with self.subTest(prefix=raw[:20]), self.assertRaises(ValueError):
                structured_result(raw, "get_profile", connected_api().credentials.load())

    def test_unknown_data_obeys_nesting_and_node_limits(self):
        nested = 1
        for _ in range(33):
            nested = [nested]
        for data in ({"unknown": [[] for _ in range(20001)]}, {"unknown": nested}):
            with self.assertRaises(ValueError):
                self.result(data, "get_profile")

    def test_redaction_cannot_merge_distinct_object_keys(self):
        with self.assertRaises(ValueError):
            self.result({"PRIVATE_ACCESS_TOKEN": 1, "PRIVATE_REFRESH_TOKEN": 2}, "get_profile")

    def test_required_results_reject_missing_or_invalid_core_fields(self):
        for action, data in (("list_accounts", {}), ("list_accounts", {"accounts": None}),
                             ("list_accounts", {"accounts": [{}]}), ("list_accounts", {"accounts": [{"org_uid": ""}]}),
                             ("list_accounts", {"accounts": [{"org_uid": 123}]}),
                             ("search_jobs", {"status": "ok"}), ("get_job", {"data": {}}),
                             ("get_dashboard", {"trace_id": "trace", "status": "ok"})):
            with self.subTest(action=action, data=data), self.assertRaises(ValueError):
                self.result(data, action)
