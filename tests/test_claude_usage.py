from __future__ import annotations

import unittest

from host.runtime.agent_runtime.claude_usage import ClaudeUsage
from host.runtime.agent_runtime.token_usage import FIELDS, TurnUsage


def assistant(identifier, counts, parent=None):
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {
        "id": identifier, "usage": counts,
    }}


def counts(i=2, cached=50, write=10, out=1):
    return dict(input_tokens=i, cache_read_input_tokens=cached,
                cache_creation_input_tokens=write, output_tokens=out)


class ClaudeUsageTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ClaudeUsage()
        self.turn = TurnUsage()
        self.total = None

    def feed(self, message):
        measured = self.adapter.add(message)
        if measured is not None:
            updated = self.turn.add(measured)
            if updated is not None:
                self.total = updated

    def expect(self, *values):
        self.assertEqual(self.total, dict(zip(FIELDS, values)))

    def test_result_replaces_deduplicated_main_loop_samples(self):
        for message in [assistant("a", counts()), assistant("a", counts()),
                        assistant("b", counts(i=3, out=2))]:
            self.feed(message)
        self.expect(5, 100, 20, None)
        result = {"type": "result", "uuid": "r1", "usage": counts(5, 100, 20, 900),
                  "modelUsage": {"model": {"inputTokens": 999999, "outputTokens": 999999}}}
        self.feed(result)
        self.expect(5, 100, 20, 900)
        self.feed(result)
        self.expect(5, 100, 20, 900)

    def test_steered_followup_adds_new_turn_without_losing_previous_result(self):
        self.feed(assistant("a", counts()))
        self.feed({"type": "result", "uuid": "r1", "usage": counts(out=300)})
        self.feed(assistant("b", counts(i=4, cached=60, out=1)))
        self.expect(6, 110, 20, None)
        # A duplicate older result cannot finalize the followup's samples.
        self.feed({"type": "result", "uuid": "r1", "usage": counts(out=300)})
        self.feed({"type": "result", "uuid": "r2", "usage": counts(4, 60, 10, 400)})
        self.expect(6, 110, 20, 700)

    def test_error_result_counts_usage_and_zeroed_crash_preserves_inputs(self):
        self.feed(assistant("a", counts()))
        self.feed({"type": "result", "uuid": "error", "is_error": True,
                   "usage": counts(out=200)})
        self.expect(2, 50, 10, 200)
        self.feed(assistant("b", counts(i=3)))
        self.feed({"type": "result", "uuid": "crash", "is_error": True,
                   "usage": counts(0, 0, 0, 0)})
        self.expect(5, 100, 20, None)

    def test_missing_or_invalid_final_fields_do_not_zero_known_inputs(self):
        self.feed(assistant("a", counts()))
        self.feed({"type": "result", "usage": {
            "input_tokens": -1, "output_tokens": 300, "cache_read_input_tokens": True,
        }})
        self.expect(2, 50, 10, 300)

    def test_missing_result_keeps_output_unknown(self):
        self.feed(assistant("a", counts()))
        self.expect(2, 50, 10, None)
        self.feed({"type": "result", "is_error": True})
        self.expect(2, 50, 10, None)

    def test_subagent_inputs_are_separate_from_main_result(self):
        self.feed(assistant("child-a", counts(i=7, cached=80, write=0), "tool-a"))
        self.feed(assistant("child-a", counts(i=7, cached=80, write=0), "tool-a"))
        self.feed(assistant("main-a", counts()))
        self.feed({"type": "result", "usage": counts(out=300)})
        # Main result excludes children; the child's output is still unknown.
        self.expect(9, 130, 10, None)

    def test_result_without_assistant_messages_and_no_usage_result(self):
        self.feed({"type": "result", "usage": {"output_tokens": 20}})
        self.expect(None, None, None, 20)
        self.feed({"type": "result"})
        self.expect(None, None, None, 20)
