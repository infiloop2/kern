"""The fixed, current-approval-only TypeSafe risk contract."""

from __future__ import annotations

import unittest

from host.runtime.host_inference import approval_risk


class ApprovalRiskTests(unittest.TestCase):
    def test_state_keeps_complete_payload_and_contains_no_history(self) -> None:
        payload = {"to": "person@example.com", "body": "x" * 65_000}
        state = approval_risk.approval_state(
            {
                "tool_id": "gmail",
                "action_id": "send_message",
                "summary": "Send the message.",
                "payload": payload,
                "account_label": "work@example.com",
            },
            action_description="Send a message.",
            data_policy="Sends the reviewed message after approval.",
        )
        self.assertIs(state["payload"], payload)
        self.assertNotIn("origin_purpose", state)
        self.assertNotIn("history", state)
        self.assertNotIn("recipient", str(state).lower())

    def test_questions_cover_only_current_action_risk(self) -> None:
        self.assertEqual(
            set(approval_risk.APPROVAL_QUESTIONS),
            {"commits_money_or_obligation", "sensitive_data", "summary_mismatch"},
        )

    def test_typed_result_becomes_annotation(self) -> None:
        result = {
            "model": "jev-1.13.0",
            "answers": {
                "commits_money_or_obligation": {"type": "noul", "noul": 0.91},
                "sensitive_data": {"type": "noul", "noul": 0.2},
                "summary_mismatch": {"type": "noul", "noul": 0.1},
            },
        }
        assessment = approval_risk.assessment_from_result(result)
        self.assertEqual(
            assessment,
            {
                "model": "jev-1.13.0",
                "scores": {
                    "commits_money_or_obligation": 0.91,
                    "sensitive_data": 0.2,
                    "summary_mismatch": 0.1,
                },
            },
        )

    def test_invalid_result_is_rejected(self) -> None:
        self.assertIsNone(approval_risk.assessment_from_result({"model": "jev-latest", "answers": {}}))


if __name__ == "__main__":
    unittest.main()
