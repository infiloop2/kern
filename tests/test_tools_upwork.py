"""Upwork boundary tests using protocol fixtures; live compatibility is a stage gate."""

import copy
import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
import urllib.parse
from unittest.mock import Mock, patch

from host.tools import upwork
from host.tools.upwork import oauth, validation
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.oauth2 import IntegrationReconnectRequired
from host.tools.shared.web import ProviderWarning, WebRequestError
from test_tools import FakeHostAPI

READ = {"name": "example_read", "description": "Read account data.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 100}}, "required": ["query"]}}


def connected_api():
    api = FakeHostAPI()
    api.credentials.save({"account": {"id": "grant-one", "label": "Upwork MCP one", "scopes": []},
        "secret": {"client_id": "client-one", "access_token": "PRIVATE_ACCESS_TOKEN", "refresh_token": "PRIVATE_REFRESH_TOKEN", "expires_at": int(time.time()) + 3600}, "metadata": {}})
    return api


class UpworkTests(unittest.TestCase):
    def setUp(self):
        self.api = connected_api()
        self.client = Mock()
        self.client.request.return_value = {"tools": [copy.deepcopy(READ)]}
        self.client.call.return_value = {"content": [{"type": "text", "text": '{"jobs":[]}'}]}
        patcher = patch.object(upwork, "MCPConnection", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        revocation = patch.object(oauth, "_request", return_value={})
        self.revoke = revocation.start()
        self.addCleanup(revocation.stop)

    def propose(self):
        return upwork.BUNDLED_TOOL.execute("send_message", {"org_uid": "org-one", "room_id": "room-one", "message": "Hello"}, self.api)

    def test_named_reads_dispatch_fixed_names_and_bounded_pagination(self):
        result = upwork.BUNDLED_TOOL.execute("search_jobs", {"org_uid": "org-one", "query": "automation"}, self.api)
        self.assertIsInstance(result, ActionExecuted)
        self.client.call.assert_called_once_with("upwork__find_jobs", {"action": "search", "org_uid": "org-one", "params": {"query": "automation", "limit": 10}})
        self.assertEqual(self.api.approvals.records, {})
        self.assertNotIn("list_tools", upwork.OPERATIONS)
        self.assertNotIn("execute_tool", upwork.OPERATIONS)
        self.assertEqual(len(upwork.MANIFEST.actions), 12)
        self.assertNotIn("prepare_proposal", upwork.OPERATIONS)

    def test_write_queues_without_network_then_executes_exactly_once(self):
        result = self.propose()
        self.assertIsInstance(result, ActionPendingApproval)
        self.client.initialize.assert_not_called()
        self.client.call.assert_not_called()
        executed = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(result.approval_id), self.api)
        self.assertIsInstance(executed, ApprovalExecuted)
        self.client.call.assert_called_once_with("upwork__send_message", {"action": "send", "org_uid": "org-one", "params": {"room_id": "room-one", "message": "Hello"}})

    def test_parameter_guard_covers_encoded_nested_values_before_network(self):
        for value in ("AKIAIOSFODNN7EXAMPLE", "alice@example.com", "https://x.test/?q=alice%2540example.com", "https%3A%2F%2Fx.test%2F%3Fq%3Dalice%2540example.com", "alice%2540example.com"):
            with self.subTest(value=value):
                self.client.reset_mock()
                result = upwork.BUNDLED_TOOL.execute("search_jobs", {"org_uid": "org-one", "query": value}, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.client.initialize.assert_not_called()
                self.assertEqual(self.api.approvals.records, {})

    def test_nul_text_is_rejected_before_approval_or_provider_calls(self):
        for action, params in (
            ("send_message", {"room_id": "room-one", "message": "Hello\x00world"}),
            ("submit_proposal", {"job_reference": "123", "cover_letter": "Hello\x00world", "charged_amount": 50}),
            ("submit_proposal", {"job_reference": "123", "cover_letter": "Hello", "charged_amount": 50,
                                 "answers": [{"question": "Example?", "answer": "Hello\x00world"}]}),
        ):
            with self.subTest(action=action, params=params):
                result = upwork.BUNDLED_TOOL.execute(action, {"org_uid": "org-one", **params}, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertIn("NUL", result.error)
        self.assertEqual(self.api.approvals.records, {})
        self.client.initialize.assert_not_called()
        self.client.call.assert_not_called()

    def test_every_free_input_is_guarded_before_network(self):
        for action in upwork.MANIFEST.actions:
            if action.approval == "operator":
                continue
            for field in action.input_schema["properties"]:
                if action.id == "get_account" and field == "section":
                    continue  # Closed enum, tested separately below.
                with self.subTest(action=action.id, field=field):
                    params = {"section": "profile"} if action.id == "get_account" else {}
                    params[field] = "AKIAIOSFODNN7EXAMPLE"
                    result = upwork.BUNDLED_TOOL.execute(action.id, params, self.api)
                    self.assertIsInstance(result, ActionFailed)
                    self.assertIn("credential", result.error)
        self.client.initialize.assert_not_called()
        self.client.call.assert_not_called()

    def test_account_sections_dispatch_only_the_selected_read(self):
        for section, remote_tool, remote_action, params in (
            ("profile", "upwork__get_profile", "get", {}),
            ("dashboard", "upwork__get_freelancer_dashboard", "check", {}),
            ("highlights", "upwork__get_profile", "list_highlights", {}),
            ("connects", "upwork__get_profile", "connects_balance", {"limit": 10}),
        ):
            with self.subTest(section=section):
                self.client.call.reset_mock()
                data = {"certificates": [], "portfolio_projects": []} if section == "highlights" else {"account_section": {"name": "Example"}}
                self.client.call.return_value = {"content": [{"type": "text", "text": json.dumps(data)}]}
                result = upwork.BUNDLED_TOOL.execute("get_account", {"org_uid": "org-one", "section": section}, self.api)
                self.assertIsInstance(result, ActionExecuted)
                self.client.call.assert_called_once_with(remote_tool, {"action": remote_action, "org_uid": "org-one", "params": params})
        self.assertEqual(self.api.approvals.records, {})

    def test_account_sections_remain_closed_and_cannot_choose_writes(self):
        for params in ({}, {"section": "save_job"}, {"section": "submit_proposal"}, {"section": []},
                       {"section": "profile", "limit": 1}, {"section": "dashboard", "profile_key": "other"},
                       {"section": "connects", "limit": 11}, {"section": "highlights", "extra": True}):
            with self.subTest(params=params):
                self.assertIsInstance(upwork.BUNDLED_TOOL.execute("get_account", {"org_uid": "org-one", **params}, self.api), ActionFailed)
        for action in (*upwork.ACCOUNT_READS.values(), "list_contracts", "get_contract", "save_job", "unsave_job"):
            self.assertIsInstance(upwork.BUNDLED_TOOL.execute(action, {"org_uid": "org-one"}, self.api), ActionFailed)
        self.client.initialize.assert_not_called()
        self.client.call.assert_not_called()

    def test_provider_ids_and_cursors_allow_opaque_values_but_not_secrets(self):
        cursor = "eyJpZCI6IjIwOTc3ODk3NTkzNDQyMjY5MDMifQ=="
        params = {"org_uid": "1234567890123", "cursor": cursor}
        self.client.call.return_value = {"content": [{"type": "text", "text": '{"data":{"vendorProposals":{"edges":[]}}}'}]}
        result = upwork.BUNDLED_TOOL.execute("list_proposals", params, self.api)
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(self.client.call.call_args.args[1]["params"]["cursor"], cursor)
        for field in validation.OPAQUE_FIELDS:
            with self.subTest(field=field), self.assertRaises(ValueError):
                validation.arguments(json.dumps({field: "AKIAIOSFODNN7EXAMPLE"}), self.api)
        for value in ("alice@example.com", "value with spaces", "x" * 1025):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validation.arguments(json.dumps({"cursor": value}), self.api)

    def test_closed_inputs_and_provider_constraints(self):
        for action, params in (
            ("search_jobs", {"extra": "x"}), ("search_jobs", {"limit": 11}),
            ("search_jobs", {"query": "x", "title": "x"}),
            ("search_jobs", {"skills": ["x"] * 6}),
            ("search_jobs", {"job_type": "hourly", "rate_min": -1}),
            ("get_recommended_jobs", {"days_posted": 1}),
            ("send_message", {"room_id": "room", "message": "x" * 10241}),
            ("submit_proposal", {"job_reference": "~02123", "cover_letter": "Hi", "charged_amount": 50}),
            ("submit_proposal", {"job_reference": "123", "cover_letter": "Hi", "charged_amount": 50, "answers": [{"question": "q", "answer": "a", "injected": True}]}),
        ):
            with self.subTest(action=action, params=params):
                self.assertIsInstance(upwork.BUNDLED_TOOL.execute(action, {"org_uid": "org-one", **params}, self.api), ActionFailed)
        self.client.initialize.assert_not_called()

    def preview(self, **changes):
        # Documented fields in a synthetic envelope: no live draft was created.
        data = {"preview_id": "preview-one", "preview": {"job_reference": "123", "cover_letter": "A specific proposal", "charged_amount": 50, "connects_cost": 10, "connects_balance": 20, "can_apply": True}, "trace_id": "abcdef12345678901234567890abcdef"}
        data["preview"].update(changes)
        return {"content": [{"type": "text", "text": json.dumps(data)}]}

    def job_cost(self, **changes):
        data = {"data": {"marketplaceJobPosting": {"id": "123", "content": {"title": "Engineer", "description": "Project"}}},
                "connects_cost": 10, "connects_balance": 20, "can_apply": True}
        data.update(changes)
        return {"content": [{"type": "text", "text": json.dumps(data)}]}

    def submit(self, **changes):
        self.client.call.side_effect = None
        self.client.call.return_value = self.job_cost()
        params = {"org_uid": "org-one", "job_reference": "123", "cover_letter": "A specific proposal", "charged_amount": 50}
        params.update(changes)
        return upwork.BUNDLED_TOOL.execute("submit_proposal", params, self.api)

    def test_approved_long_form_text_uses_provider_limits_without_parameter_guard(self):
        for field, maximum in (("cover_letter", 5000), ("message", 10240)):
            for length in (1500, maximum, maximum + 1):
                with self.subTest(field=field, length=length):
                    self.client.reset_mock()
                    text = "alice@example.com " + "a" * (length - 18)
                    if field == "cover_letter":
                        result = self.submit(cover_letter=text)
                    else:
                        with patch.object(self.api.outbound, "guard_request_parameter_string", side_effect=AssertionError("approved message scanned")):
                            result = upwork.BUNDLED_TOOL.execute("send_message", {"org_uid": "org-one", "room_id": "room-one", "message": text}, self.api)
                    self.assertEqual(isinstance(result, ActionPendingApproval), length <= maximum)
                    if length <= maximum:
                        self.assertEqual(self.api.approvals.records[result.approval_id].payload["parameters"][field], text)
                    else:
                        self.client.call.assert_not_called()

    def test_proposal_text_stays_local_until_approval(self):
        pending = self.submit(cover_letter="Contact alice@example.com")
        self.assertIsInstance(pending, ActionPendingApproval)
        self.client.call.assert_called_once_with("upwork__find_jobs", {"action": "get", "org_uid": "org-one", "params": {"job_id": "123"}})
        payload = self.api.approvals.records[pending.approval_id].payload
        self.assertEqual(payload["parameters"]["cover_letter"], "Contact alice@example.com")
        self.assertEqual(payload["connects"], {"connects_cost": 10, "connects_balance": 20, "maximum_connects": 10})

    def test_unicode_message_fits_provider_and_real_host_payload_limits(self):
        from host.runtime.tools.tools_host import _ensure_json_object, PAYLOAD_MAX_BYTES
        message = "🙂" * 10240
        pending = upwork.BUNDLED_TOOL.execute("send_message", {"org_uid": "org-one", "room_id": "room-one", "message": message}, self.api)
        self.assertIsInstance(pending, ActionPendingApproval)
        payload = self.api.approvals.records[pending.approval_id].payload
        serialized = _ensure_json_object(payload, what="Approval payload", max_bytes=PAYLOAD_MAX_BYTES)
        self.assertEqual(json.loads(serialized)["parameters"]["message"], message)
        self.assertLess(len(serialized.encode("utf-8")), PAYLOAD_MAX_BYTES)
        with self.assertRaises(ValueError):
            _ensure_json_object({"message": "🙂" * 16384}, what="Approval payload", max_bytes=PAYLOAD_MAX_BYTES)
        with self.assertRaisesRegex(ValueError, "JSON-serializable values"):
            _ensure_json_object({"message": "\ud800"}, what="Approval payload", max_bytes=PAYLOAD_MAX_BYTES)
        nested = {}
        for _ in range(2000):
            nested = {"x": nested}
        with self.assertRaisesRegex(ValueError, "JSON-serializable values"):
            _ensure_json_object(nested, what="Tool secret", max_bytes=16384)

    def test_integral_json_costs_allow_approval_and_confirmation(self):
        self.client.call.return_value = self.job_cost(connects_cost=10.0, connects_balance=20.0)
        pending = upwork.BUNDLED_TOOL.execute("submit_proposal", {"org_uid": "org-one", "job_reference": "123", "cover_letter": "A specific proposal", "charged_amount": 50}, self.api)
        self.assertIsInstance(pending, ActionPendingApproval)
        self.client.call.side_effect = [self.job_cost(connects_cost=10.0, connects_balance=20.0), self.preview(), self.preview(connects_cost=10.0, connects_balance=20.0), {"content": [{"type": "text", "text": "Submitted"}]}]
        result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIsInstance(result, ApprovalExecuted)

    def test_one_approval_prepares_checks_and_confirms_the_exact_proposal(self):
        pending = self.submit()
        record = self.api.approvals.approve(pending.approval_id)
        self.client.call.reset_mock()
        self.client.call.side_effect = [self.job_cost(), self.preview(), self.preview(job_details="x" * 100000), {"content": [{"type": "text", "text": "Submitted"}]}]
        result = upwork.BUNDLED_TOOL.execute_approved(record, self.api)
        self.assertIsInstance(result, ApprovalExecuted)
        calls = self.client.call.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["upwork__find_jobs", "upwork__manage_proposals", "upwork__get_preview", "upwork__confirm_preview"])
        self.assertEqual(calls[1].args[1]["params"], {"job_reference": "123", "cover_letter": "A specific proposal", "charged_amount": 50})
        self.assertEqual(calls[-1].args[1], {"action": "confirm", "org_uid": "org-one", "params": {"type": "proposal", "preview_id": "preview-one"}})

    def test_cost_change_stops_before_private_creation(self):
        pending = self.submit()
        self.client.call.reset_mock()
        self.client.call.return_value = self.job_cost(connects_cost=11)
        result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIsInstance(result, ActionFailed)
        self.client.call.assert_called_once()
        self.assertEqual(self.client.call.call_args.args[0], "upwork__find_jobs")

    def stored_preview(self, **changes):
        # Reconstruct the params envelope and camelCase fields observed in
        # the 2026-09-14 diagnostic; this is not a complete captured response.
        params = {"jobReference": "123", "coverLetter": "A specific proposal",
                  "chargedAmount": 50, "connects_cost": 10}
        params.update(changes)
        return {"content": [{"type": "text", "text": json.dumps({
            "expires_at": "2026-09-14T16:35:50Z", "params": params,
            "preview_id": "preview-one"})}]}

    def test_stored_preview_checks_camelcase_content_and_fresh_job_eligibility(self):
        for options, preview_options in (
            ({}, {}),
            ({"answers": [{"question": "Relevant work?", "answer": "API integrations"}],
              "boost_connects": 3, "team_org_id": "team-one", "attachments": ["file-one"],
              "certificate_ids": ["cert-one"], "portfolio_project_ids": ["project-one"]},
             {"answers": [{"question": "Relevant work?", "answer": "API integrations"}],
              "boostConnects": 3, "teamOrgId": "team-one", "attachments": ["file-one"],
              "certificateIds": ["cert-one"], "portfolioProjectIds": ["project-one"],
              "screeningQuestions": ["Relevant work?"]}),
        ):
            with self.subTest(options=options):
                pending = self.submit(**options)
                self.client.reset_mock()
                self.client.call.side_effect = [self.job_cost(), self.preview(),
                    self.stored_preview(**preview_options), {"content": [{"type": "text", "text": "Submitted"}]}]
                result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
                self.assertIsInstance(result, ApprovalExecuted)
                self.assertEqual([call.args[0] for call in self.client.call.call_args_list],
                    ["upwork__find_jobs", "upwork__manage_proposals", "upwork__get_preview", "upwork__confirm_preview"])

    def test_stored_preview_changes_and_unapproved_camelcase_terms_never_confirm(self):
        for changes in ({"coverLetter": "Changed"}, {"chargedAmount": 51}, {"jobReference": "124"},
                        {"connects_cost": 11}, {"connects_cost": True}, {"connects_cost": "10"},
                        {"connectsBalance": 1}, {"connectsBalance": None}, {"canApply": False},
                        {"canApply": None}, {"boostConnects": 3}, {"teamOrgId": "team-two"},
                        {"certificateIds": ["cert-one"]}, {"portfolioProjectIds": ["project-one"]},
                        {"screeningQuestions": ["Unanswered?"]}):
            with self.subTest(changes=changes):
                pending = self.submit()
                self.client.reset_mock()
                self.client.call.side_effect = [self.job_cost(), self.preview(), self.stored_preview(**changes)]
                result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(self.client.call.call_count, 3)

    def test_stored_preview_missing_fields_and_alias_collisions_never_confirm(self):
        for field, alias in (("coverLetter", "cover_letter"), ("chargedAmount", "charged_amount"),
                             ("jobReference", "job_reference"), ("connects_cost", "connectsCost")):
            for duplicate in (False, True):
                with self.subTest(field=field, duplicate=duplicate):
                    preview = self.stored_preview()
                    data = json.loads(preview["content"][0]["text"])
                    if duplicate:
                        data["params"][alias] = data["params"][field]
                    else:
                        del data["params"][field]
                    preview["content"][0]["text"] = json.dumps(data)
                    pending = self.submit()
                    self.client.reset_mock()
                    self.client.call.side_effect = [self.job_cost(), self.preview(), preview]
                    with self.assertRaisesRegex(ProviderWarning, "repeated fields:" if duplicate else "Missing fields:"):
                        upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
                    self.assertEqual(self.client.call.call_count, 3)

    def test_fresh_job_ineligible_or_insufficient_balance_stops_before_preparation(self):
        for changes in ({"can_apply": False}, {"connects_balance": 1}):
            with self.subTest(changes=changes):
                pending = self.submit()
                self.client.reset_mock()
                self.client.call.side_effect = [self.job_cost(**changes)]
                result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
                self.assertIsInstance(result, ActionFailed)
                self.client.call.assert_called_once()
                self.assertEqual(self.client.call.call_args.args[0], "upwork__find_jobs")

    def test_changed_or_unapproved_preview_fields_never_confirm(self):
        for changes in ({"cover_letter": "Changed"}, {"charged_amount": 51}, {"job_reference": "124"},
                        {"connects_cost": 11}, {"connects_balance": 1}, {"can_apply": False},
                        {"boost_connects": 3}, {"attachments": ["unexpected-file"]}):
            with self.subTest(changes=changes):
                pending = self.submit()
                self.client.call.reset_mock()
                self.client.call.side_effect = [self.job_cost(), self.preview(), self.preview(**changes)]
                result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(self.client.call.call_count, 3)
                self.assertEqual(self.client.call.call_args.args[0], "upwork__get_preview")

    def test_optional_proposal_fields_must_match_approved_values(self):
        options = {"answers": [{"question": "Contact", "answer": "alice@example.com"}], "boost_connects": 3,
                   "attachments": ["file-one"], "certificate_ids": ["certificate-one"], "portfolio_project_ids": ["project-one"], "team_org_id": "team-one"}
        pending = self.submit(**options)
        record = self.api.approvals.approve(pending.approval_id)
        self.assertEqual(record.payload["connects"]["maximum_connects"], 13)
        self.client.call.side_effect = [self.job_cost(), self.preview(**options), self.preview(**options, screening_questions=[{"question": "Contact"}]), {"content": [{"type": "text", "text": "Submitted"}]}]
        self.assertIsInstance(upwork.BUNDLED_TOOL.execute_approved(record, self.api), ApprovalExecuted)

    def test_screening_questions_stop_before_confirmation_and_are_returned(self):
        pending = self.submit()
        self.client.call.reset_mock()
        self.client.call.side_effect = [self.job_cost(), self.preview(), self.preview(screening_questions=["What did you build?"])]
        result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("What did you build?", result.error)
        self.assertEqual(self.client.call.call_count, 3)

    def test_screening_question_errors_are_safe_to_persist_as_utf8(self):
        pending = self.submit()
        self.client.call.side_effect = [self.job_cost(), self.preview(), self.preview(screening_questions=["Question \ud800\x00?"])]
        result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIsInstance(result, ActionFailed)
        result.error.encode("utf-8")
        self.assertNotIn("\x00", result.error)
        self.assertIn("Question", result.error)
        self.assertEqual(self.client.call.call_count, 4)  # Includes the pre-approval job read.

    def test_partial_screening_answers_never_confirm(self):
        answers = [{"question": "First?", "answer": "My answer"}]
        pending = self.submit(answers=answers)
        self.client.call.reset_mock()
        self.client.call.side_effect = [self.job_cost(), self.preview(), self.preview(answers=answers, screening_questions=["First?", {"question": "Second?"}])]
        result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("Second?", result.error)
        self.assertEqual(self.client.call.call_count, 3)

    def test_present_screening_questions_must_be_a_list(self):
        for questions in ({}, "", False, None, 0, []):
            with self.subTest(questions=questions):
                pending = self.submit()
                self.client.call.reset_mock()
                self.client.call.side_effect = [self.job_cost(), self.preview(), self.preview(screening_questions=questions), {"content": [{"type": "text", "text": "Submitted"}]}]
                record = self.api.approvals.approve(pending.approval_id)
                if questions == []:
                    self.assertIsInstance(upwork.BUNDLED_TOOL.execute_approved(record, self.api), ApprovalExecuted)
                else:
                    with self.assertRaisesRegex(ProviderWarning, "screening-question format"):
                        upwork.BUNDLED_TOOL.execute_approved(record, self.api)
                self.assertEqual(self.client.call.call_count, 4 if questions == [] else 3)

    def test_invalid_or_unavailable_preview_never_confirms(self):
        for preview in ({"content": [{"type": "text", "text": "{}"}]}, self.preview(job_reference="１２３"),
                        {"content": [{"type": "text", "text": "Expired"}]}):
            pending = self.submit()
            self.client.call.reset_mock()
            self.client.call.side_effect = [self.job_cost(), self.preview(), preview]
            record = self.api.approvals.approve(pending.approval_id)
            if preview == self.preview(job_reference="１２３"):
                self.assertIsInstance(upwork.BUNDLED_TOOL.execute_approved(record, self.api), ActionFailed)
            else:
                with self.assertRaises(ProviderWarning):
                    upwork.BUNDLED_TOOL.execute_approved(record, self.api)
            self.assertEqual(self.client.call.call_count, 3)

    def test_insufficient_connects_or_invalid_job_does_not_queue(self):
        for job_reference in ("not-a-job", "~02123", "１２３"):
            self.assertIsInstance(self.submit(job_reference=job_reference), ActionFailed)
        for data in (self.job_cost(connects_balance=1), self.job_cost(can_apply=False), self.job_cost(connects_cost=True)):
            self.client.call.return_value = data
            params = {"org_uid": "org-one", "job_reference": "123", "cover_letter": "A specific proposal", "charged_amount": 50}
            self.assertIsInstance(upwork.BUNDLED_TOOL.execute("submit_proposal", params, self.api), ActionFailed)
        self.assertEqual(self.api.approvals.records, {})

    def test_preview_credential_echo_cannot_be_confirmed(self):
        pending = self.submit()
        value = self.preview()
        value["content"][0]["text"] = value["content"][0]["text"].replace("abcdef12345678901234567890abcdef", "\\u0063lient-one")
        self.client.call.reset_mock()
        self.client.call.side_effect = [self.job_cost(), self.preview(), value]
        with self.assertRaises(ProviderWarning) as caught:
            upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertNotIn("client-one", caught.exception.response_body)
        self.assertIn("[redacted]", caught.exception.response_body)
        self.assertEqual(caught.exception.operation, "get_preview")
        self.assertEqual(self.client.call.call_count, 3)

    def test_changed_connection_blocks_approved_write(self):
        pending = self.propose()
        credential = self.api.credentials.load()
        credential["account"]["id"] = "grant-two"
        self.api.credentials.save(credential)
        failed = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIn("connection changed", failed.error)
        self.client.call.assert_not_called()

    def test_structured_results_preserve_long_text_and_redact_decoded_secrets(self):
        body = "🙂" * 5000 + "PRIVATE_ACCESS_TOKEN PRIVATE_REFRESH_TOKEN"
        data = {"accounts": [{"name": body, "org_uid": "123", "role": "TALENT", "role_label": "Freelancer"}]}
        text = json.dumps(data).replace("PRIVATE_ACCESS_TOKEN", "\\u0050RIVATE_ACCESS_TOKEN")
        self.client.call.return_value = {"content": [{"type": "text", "text": text},
            {"type": "resource_link", "name": "Untrusted", "uri": "https://evil.test/secret"}]}
        result = upwork.BUNDLED_TOOL.execute("list_accounts", {}, self.api)
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(result.result["accounts"][0]["name"], "🙂" * 5000 + "[redacted] [redacted]")
        self.assertNotIn("truncated", result.result)
        self.assertNotIn("result_text", result.result)
        self.assertNotIn("provider_details_json", result.result)
        self.assertNotIn("evil.test", json.dumps(result.result))

    def test_approved_json_result_is_complete_and_redacts_decoded_credentials(self):
        pending = self.propose()
        data = {"message": "x" * 20000, "echo": "PRIVATE_ACCESS_TOKEN"}
        text = json.dumps(data).replace("PRIVATE_ACCESS_TOKEN", "\\u0050RIVATE_ACCESS_TOKEN")
        self.client.call.return_value = {"content": [{"type": "text", "text": text}]}
        result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertEqual(json.loads(result.message.split("\n", 1)[1]), {"message": "x" * 20000, "echo": "[redacted]"})

    def test_approved_result_redacts_each_json_block_before_joining(self):
        pending = self.propose()
        self.client.call.return_value = {"content": [
            {"type": "text", "text": '{"echo":"\\u0050RIVATE_ACCESS_TOKEN"}'},
            {"type": "text", "text": '{"echo":"\\u0050RIVATE_REFRESH_TOKEN"}'},
            {"type": "text", "text": 'Token: "\\u0050RIVATE_ACCESS_TOKEN"; another "\\u0050RIVATE_REFRESH_TOKEN"'},
            {"type": "text", "text": 'Token: "\\ud800PRIVATE_ACCESS_TOKEN"'},
            {"type": "text", "text": 'Token: \\u0050RIVATE_ACCESS_TOKEN'},
            {"type": "text", "text": 'Note: \\u0000'},
            {"type": "text", "text": "Done"},
        ]}
        result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertEqual(result.message.splitlines()[1:], ['{"echo":"[redacted]"}', '{"echo":"[redacted]"}', 'Token: "[redacted]"; another "[redacted]"', 'Token: "\\ud800[redacted]"', 'Token: [redacted]', 'Note: \\u0000', "Done"])
        result.message.encode("utf-8")

    def test_approved_result_redacts_credentials_split_across_blocks(self):
        for pieces in (("Token: PRIVATE_ACCESS_", "TOKEN"), ("Token: \\u005", "0RIVATE_ACCESS_TOKEN")):
            pending = self.propose()
            self.client.call.return_value = {"content": [{"type": "text", "text": piece} for piece in pieces]}
            result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
            with self.subTest(pieces=pieces):
                self.assertIsInstance(result, ApprovalExecuted)
                self.assertEqual(result.message.split("\n", 1)[1], "Token: [redacted]")

    def test_missing_content_is_failure_not_success(self):
        for invalid in ({}, {"content": [{}]}, {"content": [{"type": "unknown"}]}, {"structuredContent": {"ok": True}}, {"content": [{"type": "text"}]}, {"content": [], "isError": "false"}):
            self.client.call.return_value = invalid
            with self.assertRaises(ProviderWarning):
                upwork.BUNDLED_TOOL.execute("list_accounts", {}, self.api)

    def test_unexpected_read_reaches_host_warning_with_redacted_bounded_sample(self):
        from host.runtime.tools import tools_host
        self.client.call.return_value = {"content": [{"type": "text", "text": json.dumps({
            "unexpected_accounts": [], "echo": "\\\\\\\\u0050RIVATE_ACCESS_TOKEN", "zz_padding": "x" * 10000})}]}
        with (
            patch.object(tools_host, "enabled_tool", return_value=upwork.BUNDLED_TOOL),
            patch.object(tools_host, "resolve_connection", return_value=Mock()),
            patch.object(tools_host, "host_api_for", return_value=self.api),
            patch.object(tools_host, "_audit"),
            patch.object(tools_host.host_errors, "report_warning") as report,
        ):
            result = tools_host.execute_action("upwork", "list_accounts", {}, origin_thread_id=None)
        self.assertEqual(result["status"], "failed")
        self.assertIn("missing required", result["error"])
        self.assertNotIn("unexpected_accounts", result["error"])
        report.assert_called_once()
        context = report.call_args.kwargs["context"]
        self.assertEqual(context["operation"], "list_accounts")
        self.assertIn("unexpected_accounts", context["provider_response"])
        self.assertIn("[redacted]", context["provider_response"])
        self.assertNotIn("RIVATE_ACCESS_TOKEN", context["provider_response"])
        self.assertLessEqual(len(context["provider_response"].encode()), 8192)
        self.client.close.assert_called_once()

    def test_approved_result_redacts_repeated_escapes_in_json_and_prose(self):
        for layers in (1, 2, 4, 8, 16):
            escaped = "\\" * layers + "u0050RIVATE_ACCESS_TOKEN"
            for text in ("Token: " + escaped, json.dumps({"echo": escaped})):
                with self.subTest(layers=layers, text=text):
                    pending = self.propose()
                    self.client.call.return_value = {"content": [{"type": "text", "text": text}]}
                    result = upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
                    self.assertIsInstance(result, ApprovalExecuted)
                    self.assertIn("[redacted]", result.message)
                    self.assertNotIn("RIVATE_ACCESS_TOKEN", result.message)

    def test_unknown_preview_shape_logs_sample_and_never_confirms(self):
        pending = self.submit()
        self.client.call.reset_mock()
        self.client.call.side_effect = [self.job_cost(), self.preview(), {
            "content": [{"type": "text", "text": '{"new_preview":{"cover_letter":"A specific proposal"}}'}]}]
        with self.assertRaisesRegex(ProviderWarning, "unambiguous") as caught:
            upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertEqual(caught.exception.operation, "get_preview")
        self.assertIn("new_preview", caught.exception.response_body)
        self.assertEqual(self.client.call.call_count, 3)

    def test_transport_failures_do_not_echo_bodies_or_retry(self):
        pending = self.propose()
        self.client.call.side_effect = WebRequestError("Upwork request failed", status=500, body=b"PRIVATE_TOKEN")
        with self.assertRaises(ProviderWarning) as caught:
            upwork.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertNotIn("PRIVATE", str(caught.exception) + caught.exception.response_body)
        self.assertEqual(caught.exception.status, 500)
        self.client.call.assert_called_once()

    def test_sessions_close_on_success_and_failure(self):
        self.client.call.return_value = {"content": [{"type": "text", "text": '{"accounts":[]}'}]}
        upwork.BUNDLED_TOOL.execute("list_accounts", {}, self.api)
        self.client.close.assert_called_once()
        for operation in (self.client.initialize, self.client.call):
            self.client.reset_mock()
            operation.side_effect = RuntimeError("failure")
            with self.assertRaises(ProviderWarning):
                upwork.BUNDLED_TOOL.execute("list_accounts", {}, self.api)
            self.client.close.assert_called_once()
            operation.side_effect = None

    def test_unauthorized_clears_same_grant_but_forbidden_preserves_it(self):
        for status in (401, 403):
            self.api = connected_api()
            self.client.initialize.side_effect = WebRequestError("redacted", status=status)
            result = upwork.BUNDLED_TOOL.execute("list_accounts", {}, self.api)
            self.assertIsInstance(result, ActionFailed)
            self.assertEqual(result.reconnect_required, status == 401)
            self.assertEqual(self.api.credentials.load() is None, status == 401)
        self.revoke.assert_called_once_with(oauth.REVOKE, form={"client_id": "client-one", "token": "PRIVATE_REFRESH_TOKEN", "token_type_hint": "refresh_token"})

    def test_late_unauthorized_response_cannot_clear_new_grant(self):
        def reject_old():
            replacement = self.api.credentials.load()
            replacement["account"]["id"] = "grant-new"
            self.api.credentials.save(replacement)
            raise WebRequestError("redacted", status=401)
        self.client.initialize.side_effect = reject_old
        result = upwork.BUNDLED_TOOL.execute("list_accounts", {}, self.api)
        self.assertTrue(result.reconnect_required)
        self.assertEqual(self.api.credentials.load()["account"]["id"], "grant-new")


class UpworkValidationTests(unittest.TestCase):
    def test_safe_encoded_strings_are_inspected_without_rewriting_arguments(self):
        value = {"query": "https%3A%2F%2Fexample.com%2Fjobs%3Fq%3Dautomation", "note": "discount 10% today"}
        self.assertEqual(validation.arguments(json.dumps(value), FakeHostAPI()), value)
        for separator in ("?q=", "#", "/"):
            with self.subTest(separator=separator), self.assertRaises(ValueError):
                validation.arguments(json.dumps({"query": "https://x.test/" + separator + "a" * 180}), FakeHostAPI())

    def test_strict_json_and_schema_reject_unknown_nested_and_unsupported_fields(self):
        for raw in ('[]', '{"x":1,"x":2}', '{"x":NaN}', '{"x":1e999}', '{"x":' + '[' * 10 + '0' + ']' * 10 + '}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                validation.arguments(raw, FakeHostAPI())
        for args in ({}, {"query": 2}, {"query": "x", "extra": "y"}, {"query": "x" * 101}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                validation.validate(args, READ["inputSchema"])
        with self.assertRaises(ValueError):
            validation.validate({}, {"$ref": "https://evil.test/schema"})

    def test_enum_and_const_distinguish_booleans_from_numbers_recursively(self):
        for given, expected in ((True, 1), (False, 0), ({"x": [True]}, {"x": [1]}), ([False], [0])):
            for keyword in ("enum", "const"):
                schema = {keyword: [expected] if keyword == "enum" else expected, "properties": {"x": {"items": {}}}, "items": {}}
                with self.subTest(given=given, keyword=keyword), self.assertRaises(ValueError):
                    validation.validate(given, schema)
        for given, expected in ((1, 1.0), ({"x": [True]}, {"x": [True]})):
            self.assertTrue(validation._json_equal(given, expected))

    def test_integer_schemas_accept_integral_floats_but_not_fraction_or_boolean(self):
        for value in (1, 1.0, -2.0, 0.0):
            validation.validate(value, {"type": "integer"})
            validation.validate(value, {"type": ["integer", "null"]})
        for value in (1.5, True, False):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validation.validate(value, {"type": "integer"})
        validation.validate(1.0, {"type": "number"})

    def test_arrays_without_items_accept_values_but_keep_other_constraints(self):
        validation.validate([1, {"x": True}, ["nested"]], {"type": "array"})
        with self.assertRaises(ValueError):
            validation.validate([1, 2], {"type": "array", "maxItems": 1})
        with self.assertRaises(ValueError):
            validation.validate([1], {"type": "array", "items": {"type": "string"}})


class UpworkOAuthTests(unittest.TestCase):
    def test_refresh_and_disconnect_preserve_unexpected_provider_diagnostics(self):
        api = connected_api()
        stored = api.credentials.load()
        stored["secret"]["expires_at"] = 1
        api.credentials.save(stored)
        failure = WebRequestError("PRIVATE", status=503, body=b"PRIVATE_RESPONSE")
        with patch.object(oauth, "json_request", side_effect=failure), self.assertRaises(ProviderWarning) as refresh:
            oauth.UpworkOAuth().connected(api)
        self.assertEqual(refresh.exception.operation, "OAuth token refresh")
        self.assertEqual(refresh.exception.status, 503)
        self.assertIsNotNone(api.credentials.load())
        with patch.object(oauth, "json_request", side_effect=failure), self.assertRaises(ProviderWarning) as revoke:
            oauth.UpworkOAuth().disconnect(api)
        self.assertEqual(revoke.exception.operation, "OAuth token revocation")
        self.assertEqual(revoke.exception.status, 503)
        self.assertIn("disconnected locally", str(revoke.exception))
        self.assertNotIn("PRIVATE", str(revoke.exception) + revoke.exception.response_body)
        self.assertIsNone(api.credentials.load())

    def test_oauth_diagnostics_cover_provider_and_transport_failures_without_raw_bodies(self):
        cases = (
            (WebRequestError("PRIVATE", status=403, body=b"<html>PRIVATE_TOKEN</html>"), "http_error", "non_json"),
            (WebRequestError("PRIVATE", status=502), "http_error", "empty"),
            (WebRequestError("PRIVATE", status=401, body=b'{"error":"PRIVATE_TOKEN"}'), "http_error", "json"),
            (WebRequestError("PRIVATE", status=400, body=b'{"error":{"token":"PRIVATE_TOKEN"}}'), "http_error", "json"),
            (WebRequestError("PRIVATE"), "transport_error", "empty"),
            (RuntimeError("PRIVATE"), "invalid_response", None),
        )
        for url, operation in ((oauth.REGISTER, "OAuth client registration"), (oauth.TOKEN, "OAuth token exchange"), (oauth.REVOKE, "OAuth token revocation")):
            for failure, kind, response_format in cases:
                with self.subTest(url=url, failure=failure, kind=kind), patch.object(oauth, "json_request", side_effect=failure), self.assertRaises(ProviderWarning) as caught:
                    oauth._request(url)
                warning = caught.exception
                self.assertEqual(warning.operation, operation)
                self.assertEqual(warning.status, getattr(failure, "status", 0))
                details = json.loads(warning.response_body)
                self.assertEqual(details["failure"], kind)
                self.assertEqual(details.get("response_format"), response_format)
                self.assertNotIn("PRIVATE", str(warning) + warning.response_body)

    def test_invalid_registration_response_also_produces_diagnostic(self):
        for response in ({}, {"client_id": "PRIVATE_CLIENT", "token_endpoint_auth_method": "client_secret_basic"}):
            with self.subTest(response=response), patch.object(oauth, "json_request", return_value=response), self.assertRaises(ProviderWarning) as caught:
                oauth.UpworkOAuth().start_connect({"redirect_uri": "http://127.0.0.1:7443/oauth/callback"}, FakeHostAPI())
            self.assertEqual(caught.exception.operation, "OAuth client registration")
            self.assertEqual(json.loads(caught.exception.response_body)["failure"], "invalid_response")
            self.assertNotIn("PRIVATE_CLIENT", caught.exception.response_body)

    def test_localhost_guidance_is_specific_to_registration_redirect_rejection(self):
        for url, status, code, expected in (
            (oauth.REGISTER, 400, "invalid_redirect_uri", True),
            (oauth.REGISTER, 400, "invalid_client_metadata", False),
            (oauth.REGISTER, 503, "invalid_redirect_uri", False),
            (oauth.TOKEN, 400, "invalid_redirect_uri", False),
            (oauth.REVOKE, 400, "invalid_redirect_uri", False),
        ):
            failure = WebRequestError("PRIVATE", status=status, body=json.dumps({"error": code}).encode())
            with self.subTest(url=url, status=status, code=code), patch.object(oauth, "json_request", side_effect=failure), self.assertRaises(ProviderWarning) as caught:
                oauth._request(url)
            self.assertEqual("only available through localhost" in str(caught.exception), expected)
            self.assertEqual(caught.exception.response_status, 400 if expected else 502)

    def test_registration_pkce_uses_generic_secret_store(self):
        flow = oauth.UpworkOAuth()
        api = FakeHostAPI()
        with patch.object(oauth, "_request", return_value={"client_id": "public-client", "token_endpoint_auth_method": "none"}) as request:
            result = flow.start_connect({"redirect_uri": "https://kern.test/oauth/callback"}, api)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], oauth.REGISTER)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(result["authorization_url"]).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["resource"], [oauth.ENDPOINT])
        self.assertNotIn(api.secrets.load()["verifier"], result["authorization_url"])
        self.assertEqual(len(result["state"]), 43)
        self.assertGreater(api.secrets.load()["expires_at"], oauth.now())
        self.assertIsNone(api.credentials.load())

    def test_sign_in_deadline_is_enforced_by_upwork_at_exact_boundary(self):
        api = self.pending_api()
        pending = api.secrets.load()
        deadline = pending["expires_at"]
        with patch.object(oauth, "now", return_value=deadline), patch.object(oauth, "_request") as request:
            # Storage still returns the object; the tool rejects its expired state.
            self.assertEqual(api.secrets.load(), pending)
            with self.assertRaises(ValueError):
                self.callback(api)
        request.assert_not_called()

    def test_connect_rejects_existing_account_before_registration(self):
        api = connected_api()
        with patch.object(oauth, "_request") as request, self.assertRaises(ValueError):
            oauth.UpworkOAuth().start_connect({"redirect_uri": "https://kern.test/oauth/callback"}, api)
        request.assert_not_called()

    def test_http_callbacks_accept_only_localhost_or_loopback_ips(self):
        for origin in ("http://127.0.0.9:8000", "http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"):
            with self.subTest(origin=origin), patch.object(oauth, "_request", return_value={"client_id": "public-client"}) as request:
                redirect = origin + "/oauth/callback"
                oauth.UpworkOAuth().start_connect({"redirect_uri": redirect}, FakeHostAPI())
                self.assertEqual(request.call_args.kwargs["body"]["redirect_uris"], [redirect])
        for redirect in ("http://192.168.1.10/oauth/callback", "http://169.254.169.254/oauth/callback", "http://127.0.0.9.example.test/oauth/callback", "http://user@127.0.0.9/oauth/callback", "http://127.0.0.9/oauth/callback?extra=1", "http://127.0.0.9/oauth/callback#extra"):
            with self.subTest(redirect=redirect), patch.object(oauth, "_request") as request, self.assertRaises(ValueError):
                oauth.UpworkOAuth().start_connect({"redirect_uri": redirect}, FakeHostAPI())
            request.assert_not_called()

    def pending_api(self):
        api = FakeHostAPI()
        api.secrets.save({"state": "state", "redirect_uri": "https://kern.test/oauth/callback", "client_id": "client-one", "verifier": "verifier", "expires_at": oauth.now() + 900})
        return api

    def callback(self, api):
        return oauth.UpworkOAuth().complete_connect({"state": "state", "code": "code", "redirect_uri": "https://kern.test/oauth/callback"}, api)

    def test_callback_rejects_absent_expired_wrong_state_or_redirect(self):
        for field, value in (("state", "wrong"), ("redirect_uri", "https://other.test/oauth/callback"), ("expires_at", None), ("expires_at", True), ("expires_at", "future"), ("expired", True), ("absent", True)):
            api = self.pending_api()
            if field == "absent":
                api.secrets.clear()
            elif field == "expired":
                pending = api.secrets.load()
                pending["expires_at"] = 1
                api.secrets.save(pending)
            else:
                pending = api.secrets.load()
                pending[field] = value
                api.secrets.save(pending)
            with self.subTest(field=field), patch.object(oauth, "_request") as request, self.assertRaises(ValueError):
                self.callback(api)
            request.assert_not_called()

    def test_success_keeps_tokens_private_and_callback_is_single_use(self):
        api = self.pending_api()
        response = {"token_type": "Bearer", "expires_in": 3600, "access_token": "PRIVATE_ACCESS", "refresh_token": "PRIVATE_REFRESH", "scope": "actually-granted"}
        with patch.object(oauth, "_request", return_value=response) as request:
            result = self.callback(api)
            with self.assertRaises(ValueError):
                self.callback(api)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["form"]["code_verifier"], "verifier")
        self.assertNotIn("PRIVATE", json.dumps(result))
        self.assertEqual(result["account"]["scopes"], ["actually-granted"])
        self.assertTrue(result["account"]["id"].startswith("grant-"))
        self.assertIsNone(api.secrets.load())

    def test_cancelled_or_replaced_sign_in_cannot_be_restored_by_callback(self):
        for replacement in (None, {"state": "new-login", "verifier": "new-verifier"}):
            api = self.pending_api()
            revoked = []
            def exchange(url, **kwargs):
                if url == oauth.REVOKE:
                    revoked.append(kwargs["form"])
                    return {}
                # The first callback consumes the verifier before network I/O.
                with self.assertRaises(ValueError):
                    self.callback(api)
                oauth.UpworkOAuth().disconnect(api)
                if replacement:
                    api.secrets.save(replacement)
                return {"token_type": "Bearer", "expires_in": 3600, "access_token": "new-access", "refresh_token": "new-refresh"}
            with self.subTest(replacement=replacement), patch.object(oauth, "_request", side_effect=exchange), self.assertRaises(ValueError):
                self.callback(api)
            self.assertIsNone(api.credentials.load())
            self.assertEqual(api.secrets.load(), replacement)
            self.assertEqual(revoked, [{"client_id": "client-one", "token": "new-refresh", "token_type_hint": "refresh_token"}])

    def test_cancelled_callback_reports_revocation_failure_without_restoring_grant(self):
        api = self.pending_api()
        def exchange(url, **kwargs):
            if url == oauth.REVOKE:
                raise RuntimeError("PRIVATE_PROVIDER_ERROR")
            api.secrets.clear()
            return {"token_type": "Bearer", "expires_in": 3600, "access_token": "new-access", "refresh_token": "new-refresh"}
        with patch.object(oauth, "_request", side_effect=exchange), self.assertRaisesRegex(RuntimeError, "[Rr]emote revocation failed") as error:
            self.callback(api)
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertIsNone(api.credentials.load())

    def test_rotating_refresh_is_serialized_and_waiter_uses_new_token(self):
        api = connected_api()
        saved = api.credentials.load()
        saved["secret"]["expires_at"] = 1
        api.credentials.save(saved)
        entered = threading.Event()
        release = threading.Event()
        def refresh(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"token_type": "Bearer", "expires_in": 3600, "access_token": "new-access", "refresh_token": "new-refresh"}
        with patch.object(oauth, "json_request", side_effect=refresh) as request, ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(oauth.UpworkOAuth().connected, api)
            self.assertTrue(entered.wait(5))
            second = pool.submit(oauth.UpworkOAuth().connected, api)
            release.set()
            self.assertEqual(first.result(timeout=5), second.result(timeout=5))
        request.assert_called_once()
        self.assertEqual(api.credentials.load()["secret"]["refresh_token"], "new-refresh")

    def test_refresh_does_not_restore_disconnected_credentials(self):
        api = connected_api()
        saved = api.credentials.load()
        saved["secret"]["expires_at"] = 1
        api.credentials.save(saved)

        def refresh(*args, **kwargs):
            api.credentials.clear()
            return {"token_type": "Bearer", "expires_in": 3600, "access_token": "new-access", "refresh_token": "new-refresh"}

        with patch.object(oauth, "json_request", side_effect=refresh), patch.object(oauth, "_request", return_value={}) as revoke, self.assertRaises(IntegrationReconnectRequired):
            oauth.UpworkOAuth().connected(api)
        self.assertIsNone(api.credentials.load())
        revoke.assert_called_once_with(oauth.REVOKE, form={"client_id": "client-one", "token": "new-refresh", "token_type_hint": "refresh_token"})

    def test_refresh_cleanup_preserves_replacement_and_reports_revocation_failure(self):
        for failure in (None, RuntimeError("PRIVATE_PROVIDER_ERROR")):
            api = connected_api()
            expired = api.credentials.load()
            expired["secret"]["expires_at"] = 1
            api.credentials.save(expired)
            replacement = connected_api().credentials.load()
            replacement["account"]["id"] = "grant-replacement"
            def refresh(*args, **kwargs):
                api.credentials.save(replacement)
                return {"token_type": "Bearer", "expires_in": 3600, "access_token": "discard-access", "refresh_token": "discard-refresh"}
            with patch.object(oauth, "json_request", side_effect=refresh), patch.object(oauth, "_request", side_effect=failure) as revoke:
                result = upwork.BUNDLED_TOOL.execute("list_accounts", {}, api)
            self.assertTrue(result.reconnect_required)
            self.assertNotIn("PRIVATE", result.error)
            self.assertEqual("Remote revocation failed" in result.error, failure is not None)
            self.assertEqual(api.credentials.load(), replacement)
            revoke.assert_called_once_with(oauth.REVOKE, form={"client_id": "client-one", "token": "discard-refresh", "token_type_hint": "refresh_token"})

    def test_invalid_issued_tokens_or_scopes_are_revoked_for_connect_and_refresh(self):
        for field, bad in (("scope", []), ("scope", "x" * 8193), ("expires_in", "bad"), ("expires_in", 300), ("token_type", "bad")):
            response = {"token_type": "Bearer", "expires_in": 3600, "access_token": "issued-access", "refresh_token": "issued-refresh", field: bad}
            api = self.pending_api()
            with patch.object(oauth, "_request", side_effect=[response, {}]) as request, self.assertRaises(RuntimeError):
                self.callback(api)
            self.assertEqual(request.call_args.args, (oauth.REVOKE,))
            self.assertEqual(request.call_args.kwargs["form"]["token"], "issued-refresh")
            self.assertIsNone(api.credentials.load())
            api = connected_api()
            expired = api.credentials.load()
            expired["secret"]["expires_at"] = 1
            api.credentials.save(expired)
            with patch.object(oauth, "json_request", return_value=response), patch.object(oauth, "_request", return_value={}) as revoke, self.assertRaises(IntegrationReconnectRequired):
                oauth.UpworkOAuth().connected(api)
            revoke.assert_called_once_with(oauth.REVOKE, form={"client_id": "client-one", "token": "issued-refresh", "token_type_hint": "refresh_token"})
            self.assertIsNone(api.credentials.load())

    def test_refresh_starts_before_token_can_expire_during_proposal(self):
        api = connected_api()
        saved = api.credentials.load()
        saved["secret"]["expires_at"] = 1000 + 120
        api.credentials.save(saved)
        response = {"token_type": "Bearer", "expires_in": 3600, "access_token": "new-access", "refresh_token": "new-refresh"}
        with patch.object(oauth, "now", return_value=1000), patch.object(oauth, "json_request", return_value=response) as request:
            result = oauth.UpworkOAuth().connected(api)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["headers"]["User-Agent"], "Kern/v1")
        self.assertEqual(result["secret"]["access_token"], "new-access")

    def test_malformed_refresh_replacement_revokes_previous_refresh_grant(self):
        for invalid in (42, "bad token", {"token": "bad"}):
            api = connected_api()
            saved = api.credentials.load()
            saved["secret"]["expires_at"] = 1
            api.credentials.save(saved)
            response = {"token_type": "Bearer", "expires_in": 3600, "access_token": "new-access", "refresh_token": invalid}
            with patch.object(oauth, "json_request", return_value=response), patch.object(oauth, "_request", return_value={}) as revoke, self.assertRaises(IntegrationReconnectRequired):
                oauth.UpworkOAuth().connected(api)
            revoke.assert_called_once_with(oauth.REVOKE, form={"client_id": "client-one", "token": "PRIVATE_REFRESH_TOKEN", "token_type_hint": "refresh_token"})
            self.assertIsNone(api.credentials.load())

    def test_connect_without_refresh_token_revokes_issued_access_token(self):
        api = self.pending_api()
        response = {"token_type": "Bearer", "expires_in": 3600, "access_token": "issued-access"}
        with patch.object(oauth, "_request", side_effect=[response, {}]) as request, self.assertRaises(RuntimeError):
            self.callback(api)
        self.assertEqual(request.call_args.kwargs["form"], {"client_id": "client-one", "token": "issued-access", "token_type_hint": "access_token"})
        self.assertIsNone(api.credentials.load())

    def test_revocation_failure_still_removes_local_tokens(self):
        api = connected_api()
        with patch.object(oauth, "_request", side_effect=RuntimeError("PRIVATE_PROVIDER_ERROR")), self.assertRaisesRegex(RuntimeError, "disconnected locally") as error:
            oauth.UpworkOAuth().disconnect(api)
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertIsNone(api.credentials.load())
