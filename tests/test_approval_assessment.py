"""One-attempt tool approval assessment through the inference socket."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from host.runtime.host_inference import client
from host.runtime.tools import approval_assessment


RECORD = {
    "approval_id": "approval_7.token",
    "tool_id": "gmail",
    "action_id": "send_message",
    "status": "pending",
    "summary": "Send a message.",
    "payload": {"to": "person@example.com", "body": "Hello"},
    "account_label": "work@example.com",
    "origin_thread_id": "app-2",
}


def _result() -> dict:
    return {
        "model": "jev-1.13.0",
        "answers": {
            "commits_money_or_obligation": {"type": "noul", "noul": 0.1},
            "sensitive_data": {"type": "noul", "noul": 0.9},
            "summary_mismatch": {"type": "noul", "noul": 0.05},
        },
    }


class ApprovalAssessmentTests(unittest.TestCase):
    def test_assesses_current_pending_approval_once_and_saves(self) -> None:
        judge = MagicMock(return_value=_result())
        with (
            patch.object(approval_assessment.state, "tool_approval", return_value=RECORD) as approval,
            patch.object(
                approval_assessment.state,
                "save_tool_approval_risk_assessment",
                return_value=True,
            ) as save,
        ):
            self.assertTrue(
                approval_assessment.assess(
                    RECORD,
                    action_description="Send a message.",
                    data_policy="Sends the message.",
                    judge=judge,
                )
            )
        sent_state = judge.call_args.args[0]
        self.assertIs(sent_state["payload"], RECORD["payload"])
        self.assertNotIn("origin_purpose", sent_state)
        self.assertNotIn("history", sent_state)
        self.assertEqual(approval.call_count, 2)
        judge.assert_called_once()
        save.assert_called_once()

    def test_decided_approval_never_calls_provider(self) -> None:
        judge = MagicMock()
        with patch.object(
            approval_assessment.state,
            "tool_approval",
            return_value={**RECORD, "status": "denied"},
        ):
            self.assertFalse(
                approval_assessment.assess(
                    RECORD,
                    action_description="Send a message.",
                    data_policy="Sends the message.",
                    judge=judge,
                )
            )
        judge.assert_not_called()

    def test_decision_before_final_recheck_prevents_provider_call(self) -> None:
        judge = MagicMock()
        with patch.object(
            approval_assessment.state,
            "tool_approval",
            side_effect=[RECORD, {**RECORD, "status": "approved"}],
        ):
            self.assertFalse(
                approval_assessment.assess(
                    RECORD,
                    action_description="Send a message.",
                    data_policy="Sends the message.",
                    judge=judge,
                )
            )
        judge.assert_not_called()

    def test_socket_failure_is_not_retried_or_double_reported(self) -> None:
        judge = MagicMock(side_effect=client.HostInferenceError("failed"))
        with (
            patch.object(approval_assessment.state, "tool_approval", return_value=RECORD),
            patch.object(approval_assessment.host_errors, "report_warning") as warning,
        ):
            self.assertFalse(
                approval_assessment.assess(
                    RECORD,
                    action_description="Send a message.",
                    data_policy="Sends the message.",
                    judge=judge,
                )
            )
        judge.assert_called_once()
        warning.assert_not_called()


if __name__ == "__main__":
    unittest.main()
