from __future__ import annotations

import unittest

from host.runtime.admin_api import agent_activity


class AgentActivityTests(unittest.TestCase):
    def test_activity_text_escapes_nul_before_jsonb_persistence(self) -> None:
        value = agent_activity.activity(
            "codex",
            "command\x00one",
            "command",
            "completed",
            "Read binary\x00file",
            output="before\x00after",
        )

        self.assertEqual(value["activity_id"], r"command\0one")
        self.assertEqual(value["title"], r"Read binary\0file")
        self.assertEqual(value["output"], r"before\0after")
        self.assertNotIn("\x00", str(value))

    def test_provider_text_replaces_lone_surrogates(self) -> None:
        value = agent_activity.activity(
            "claude_code",
            "tool-\ud800",
            "tool",
            "completed",
            "Result \udfff",
            output="safe\ud800output",
        )

        self.assertEqual(value["activity_id"], "tool-?")
        self.assertEqual(value["title"], "Result ?")
        self.assertEqual(value["output"], "safe?output")

    def test_activity_record_boundary_validates_and_sanitizes_provider_data(self) -> None:
        normalized = agent_activity.normalize_record({
            "provider": "codex",
            "activity_id": "command-\ud800",
            "kind": "command",
            "phase": "started",
            "title": "Run\0tests",
            "output": "one\udfff",
            "append_output": True,
            "append_detail": True,
        })

        self.assertIsNotNone(normalized)
        self.assertEqual(normalized["activity_id"], "command-?")
        self.assertEqual(normalized["title"], r"Run\0tests")
        self.assertEqual(normalized["output"], "one?")
        self.assertIs(normalized["append_output"], True)
        self.assertIs(normalized["append_detail"], True)

    def test_activity_record_boundary_skips_malformed_provider_data(self) -> None:
        valid = {
            "provider": "codex",
            "activity_id": "activity-1",
            "kind": "tool",
            "phase": "completed",
            "title": "Tool result",
        }
        invalid = (
            None,
            [],
            {**valid, "provider": ""},
            {**valid, "activity_id": ""},
            {**valid, "kind": "provider_private_kind"},
            {**valid, "phase": "unknown"},
            {**valid, "title": ""},
        )
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(agent_activity.normalize_record(value))


if __name__ == "__main__":
    unittest.main()
