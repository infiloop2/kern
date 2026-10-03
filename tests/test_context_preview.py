"""Bounded, literal previews of the text transferred to a replacement session."""

import json
import unittest
from unittest.mock import MagicMock

from host.runtime.admin_api.conversation_history import _conversation_event
from host.runtime.admin_api.threads import (
    HISTORICAL_CONTEXT_PREVIEW_BYTES,
    _historical_context_preview,
)
from host.runtime.core.state.events import _event_dict, append_agent_event


class HistoricalContextPreviewTests(unittest.TestCase):
    def test_history_reads_preserve_preview_and_bound_oversized_values(self) -> None:
        preview = _historical_context_preview("BEGIN\n" + "x" * 150_000 + "\nEND")
        for value in (None, preview, "🦀" * 150_000):
            with self.subTest(has_preview=value is not None):
                payload = {"message": "Historical context transferred."}
                if value is not None:
                    payload["historical_context"] = value
                event = _conversation_event({
                    "event_type": "thread.context_added", "event_id": "event_1",
                    "timestamp": "2026-10-02T00:00:00Z", "payload": payload,
                })
                if value is None:
                    self.assertNotIn("historical_context", event)
                elif value == preview:
                    self.assertEqual(event["historical_context"], preview)
                    self.assertFalse(event["truncated"])
                else:
                    self.assertLessEqual(len(json.dumps(event["historical_context"]).encode()), HISTORICAL_CONTEXT_PREVIEW_BYTES)
                    self.assertTrue(event["truncated"])

    def test_short_transfer_is_preserved_verbatim(self) -> None:
        text = 'Beginning\n  <literal> "quotes" 🦀\nEnd'
        self.assertEqual(_historical_context_preview(text), text)

    def test_large_transfer_keeps_both_ends_within_the_json_budget(self) -> None:
        for content in ("a", "🦀", '"\\\n'):
            with self.subTest(content=content):
                text = "BEGIN\n" + content * 150_000 + "\nEND"
                preview = _historical_context_preview(text)
                self.assertLessEqual(len(json.dumps(preview).encode()), HISTORICAL_CONTEXT_PREVIEW_BYTES)
                self.assertGreater(len(preview), 1000)
                beginning, end = preview.split("\n\n… [middle omitted from preview] …\n\n")
                self.assertTrue(text.startswith(beginning))
                self.assertTrue(text.endswith(end))
                self.assertTrue(beginning.startswith("BEGIN\n"))
                self.assertTrue(end.endswith("\nEND"))

                cur = MagicMock()
                cur.fetchone.return_value = (1,)
                append_agent_event(cur, "thread.context_added", "thread-1", {
                    "message": "Historical context transferred.", "historical_context": preview,
                })
                sql, parameters = cur.execute.call_args.args
                self.assertEqual(sql.count("%s"), len(parameters))
                self.assertEqual(_event_dict((1, *parameters))["payload"]["historical_context"], preview)
