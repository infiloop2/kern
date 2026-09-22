"""Combined approval read model validation and mixed-source pagination."""

import unittest
from unittest.mock import patch

import pg_harness
from host.runtime.admin_api import approvals, service
from host.runtime.admin_api.errors import ApiError
from host.runtime.core import state


class ApprovalQueryTests(unittest.TestCase):
    def test_invalid_views_and_pages_do_not_read_state(self):
        with patch.object(state, "page_approvals") as query:
            for params in ({"view": ["all"]}, {"view": ["pending", "history"]},
                           {"page": ["0"]}, {"page": ["-1"]}, {"page": ["1", "2"]},
                           {"page": ["1 OR 1=1"]}, {"page": ["1000000"]}):
                with self.subTest(params=params), self.assertRaises(ApiError):
                    approvals.list_approvals(params)
            query.assert_not_called()

    def test_workspace_cannot_read_the_operator_approval_list(self):
        with patch.object(state, "page_approvals") as query:
            with self.assertRaises(ApiError) as error:
                service.route("GET", "/v1/approvals", {}, None, principal=service.WorkspacePrincipal())
            self.assertEqual(error.exception.status, 403)
            query.assert_not_called()

    def test_labels_are_added_without_changing_decision_identity(self):
        data = {"items": [{"kind": "tool", "id": "approval_1.token", "tool_id": "unknown_tool"},
                          {"kind": "github_push", "id": "abc123", "owner": "org", "repo": "repo"}]}
        with patch.object(state, "page_approvals", return_value=data) as query:
            result = approvals.list_approvals({"view": ["history"], "page": ["2"]})
        query.assert_called_once_with("history", 2)
        self.assertEqual(result["items"][0]["id"], "approval_1.token")
        self.assertEqual(result["items"][0]["source"], "unknown_tool")
        self.assertEqual(result["items"][1]["source"], "GitHub")


class ApprovalStorageTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()

    def test_mixed_pages_history_and_clamping(self):
        tool_ids = []
        for index in range(12):
            row = state.insert_tool_approval("fake_notes", "write_note", f"Note {index}",
                                            {"private_payload": "not in list"}, 1700000000 + index,
                                            pending_limit=1000, connection_id=f"work-{index}",
                                            account_label="Work", origin_thread_id=None)
            tool_ids.append(row["approval_id"])
        state.enqueue_pending_push("abc123", "org", "repo", [], [".github/workflows/test.yml"], origin_thread_id=None)
        first = state.page_approvals("pending", 1)
        second = state.page_approvals("pending", 2)
        self.assertEqual((first["total"], first["pages"], len(first["items"]), len(second["items"])), (13, 2, 10, 3))
        self.assertEqual(first["items"][0]["kind"], "github_push")
        tool_item = next(item for item in first["items"] if item["kind"] == "tool")
        self.assertEqual(tool_item["connection_id"], f"work-{tool_item['summary'].split()[-1]}")
        seen = {item["id"] for item in first["items"] + second["items"]}
        self.assertEqual(seen, {*tool_ids, "abc123"})
        self.assertNotIn("private_payload", str(first))
        self.assertNotIn("check_token", str(first))
        self.assertEqual(state.page_approvals("pending", 999)["page"], 2)
        state.transition_tool_approval(tool_ids[0], "pending", "denied", 1800000000)
        state.resolve_pending_push("abc123", "rejected")
        history = state.page_approvals("history", 1)
        self.assertEqual({item["id"] for item in history["items"]}, {tool_ids[0], "abc123"})
        self.assertEqual((history["pending_count"], history["history_count"]), (11, 2))

    def test_same_second_requests_are_newest_first_across_pages(self):
        ids = [state.insert_tool_approval("fake_notes", "write_note", f"Note {index}", {},
                                         1700000000, pending_limit=1000, origin_thread_id=None)["approval_id"]
               for index in range(12)]
        first = state.page_approvals("pending", 1)
        second = state.page_approvals("pending", 2)
        self.assertEqual([item["id"] for item in first["items"] + second["items"]], list(reversed(ids)))
        for approval_id in ids:
            state.transition_tool_approval(approval_id, "pending", "denied", 1800000000)
        history = state.page_approvals("history", 1)
        self.assertEqual([item["id"] for item in history["items"]], list(reversed(ids))[:10])

    def test_risk_annotation_is_saved_only_while_pending_and_joins_the_list(self):
        approval_id = state.insert_tool_approval(
            "fake_notes", "write_note", "Write note", {"text": "hello"},
            1700000000, pending_limit=1000, origin_thread_id=None,
        )["approval_id"]
        self.assertTrue(state.save_tool_approval_risk_assessment(
            approval_id,
            model="jev-1.13.0",
            assessed_at=1700000001,
            scores={
                "commits_money_or_obligation": 0.1,
                "sensitive_data": 0.9,
                "summary_mismatch": 0.05,
            },
        ))
        item = state.page_approvals("pending", 1)["items"][0]
        self.assertEqual(item["risk_scores"]["sensitive_data"], 0.9)
        self.assertNotIn("risk_level", item)
        state.transition_tool_approval(approval_id, "pending", "denied", 1700000002)
        self.assertFalse(state.save_tool_approval_risk_assessment(
            approval_id,
            model="jev-1.13.0",
            assessed_at=1700000003,
            scores={},
        ))
