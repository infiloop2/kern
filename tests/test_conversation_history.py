"""Typed, bounded history responses and independent message/notice selection."""

import json
import unittest
from http import HTTPStatus
from unittest.mock import patch

from host.agent_messages import ACTION_KINDS, CONTEXT_KINDS, INPUT_KINDS, NOTICE_KINDS
from host.runtime.admin_api import conversation_history as history
from host.runtime.agent_shim import mcp_shim


class TypedHistoryTests(unittest.TestCase):
    def test_incoming_notices_keep_their_kind_and_delivered_text(self) -> None:
        for kind in INPUT_KINDS:
            with self.subTest(kind=kind):
                event = {
                    "event_id": "event_1", "timestamp": "2026-10-04T00:00:00Z",
                    "event_type": "thread.notice",
                    "payload": {"source": "user", "message": "Exact delivered text\n  with spaces.",
                                "notice": {"kind": kind, "summary": "Compact summary"}},
                }
                result = history._conversation_event(event, False)
                self.assertEqual(result["type"], "notice")
                self.assertEqual(result["notice"], event["payload"]["notice"])
                self.assertEqual(result["content"], event["payload"]["message"])
                self.assertNotIn("role", result)

    def test_operator_pasted_preamble_remains_an_operator_message(self) -> None:
        text = "This is an automated message from Kern.\n\n---\n\nMy correction"
        result = history._conversation_event({
            "event_id": "event_1", "timestamp": "2026-10-04T00:00:00Z",
            "event_type": "thread.message",
            "payload": {"source": "user", "message": text, "notice": {"kind": "operator"}},
        }, False)
        self.assertEqual((result["type"], result["role"], result["content"]), ("message", "user", text))
        self.assertNotIn("notice", result)

    def test_context_and_action_details_are_explicit_and_bounded(self) -> None:
        huge = '😀"\\\n' * 100_000
        for kind in (*CONTEXT_KINDS, *sorted(ACTION_KINDS)):
            with self.subTest(kind=kind):
                payload = {"notice": {"kind": kind, "summary": "Compact summary"}}
                if kind in ACTION_KINDS:
                    payload["notice"]["details"] = huge
                elif kind == "history_transfer":
                    payload["historical_context"] = huge
                else:
                    payload.update(memory_page_ids=[], memory_recall_details=huge)
                event = {"event_id": "event_1", "timestamp": "2026-10-04T00:00:00Z",
                         "event_type": "thread.notice", "payload": payload}
                compact = history._conversation_event(event, False)
                self.assertEqual(compact, {
                    "event_id": "event_1", "timestamp": "2026-10-04T00:00:00Z",
                    "type": "notice", "notice": {"kind": kind, "summary": "Compact summary"}, "truncated": False,
                })
                detailed = history._conversation_event(event, True)
                self.assertTrue(detailed["truncated"])
                if kind in ACTION_KINDS:
                    value, budget = detailed["notice"]["details"], 20_000
                elif kind == "history_transfer":
                    value, budget = detailed["historical_context"], 24 * 1024
                else:
                    value, budget = detailed["memory_recall_details"], 14_000
                    self.assertEqual(detailed["memory_page_ids"], [])
                self.assertLessEqual(len(json.dumps(value).encode()), budget)

    def test_read_filters_reach_all_paging_and_bounds_queries(self) -> None:
        raw = [{"seq": 2, "event_id": "event_2", "timestamp": "2026-10-04T00:00:00Z",
                "event_type": "thread.notice", "payload": {"notice": {
                    "kind": "self_memory_saved", "summary": "Saved self memory", "details": "Private details",
                }}}]
        for cursor in ({}, {"after": "event_1"}, {"before": "event_3"}, {"around_event_id": "event_2"}):
            with (
                self.subTest(cursor=cursor),
                patch.object(history.state, "page_thread_events", return_value=raw) as page,
                patch.object(history.state, "page_thread_events_around", return_value=raw) as around,
                patch.object(history.state, "thread_event_page_bounds", return_value=(False, False)) as bounds,
            ):
                result = history.read_conversation_history({
                    "thread_id": "thread-1", "roles": [], "notice_kinds": ["self_memory_saved"], **cursor,
                })
                for call in ((around if "around_event_id" in cursor else page).call_args, bounds.call_args):
                    self.assertEqual(call.kwargs["message_sources"], ())
                    self.assertEqual(call.kwargs["notice_kinds"], ("self_memory_saved",))
                self.assertEqual(result["events"][0]["notice"], {"kind": "self_memory_saved", "summary": "Saved self memory"})
                self.assertEqual(result["instruction_authority"], "none")

    def test_search_preserves_notice_identity_and_filters_every_search_path(self) -> None:
        row = {"seq": 3, "event_id": "event_3", "timestamp": "2026-10-04T00:00:00Z",
               "thread_id": "thread-1", "event_type": "thread.notice", "source": "user",
               "notice": {"kind": "agent_message", "summary": "Message from reviewer"},
               "excerpt": "Check the release", "excerpt_truncated": False, "search_rank": 0.8}
        with (
            patch.object(history.state, "conversation_search_snapshot", return_value=(1, 10, 1, 1)),
            patch.object(history.state, "conversation_search_retention", return_value=(1, 1)),
            patch.object(history.state, "search_thread_messages", return_value=[row]) as lexical,
            patch.object(history.embedding_client, "embed_texts", return_value=[[1.0] * 384]),
            patch.object(history.state, "search_thread_messages_semantic", return_value=[row]) as semantic,
        ):
            result = history.search_conversation_history({"query": "release", "roles": [], "notice_kinds": ["agent_message"]})
        for call in (lexical.call_args, semantic.call_args):
            self.assertEqual(call.kwargs["sources"], ())
            self.assertEqual(call.kwargs["notice_kinds"], ("agent_message",))
        match = result["matches"][0]
        self.assertEqual(match["type"], "notice")
        self.assertEqual(match["notice"], row["notice"])
        self.assertNotIn("role", match)

    def test_filters_and_details_reject_unknown_values(self) -> None:
        for body in ({"roles": ["operator"]}, {"roles": ["user", "user"]}, {"roles": [None]},
                     {"notice_kinds": ["automated"]}, {"notice_kinds": ["agent_message", "agent_message"]},
                     {"notice_kinds": "agent_message"}):
            for operation, required in ((history.read_conversation_history, {"thread_id": "thread-1"}),
                                        (history.search_conversation_history, {"query": "release"})):
                with self.subTest(body=body, operation=operation.__name__), self.assertRaises(history.ApiError) as error:
                    operation({**required, **body})
                self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
        with self.assertRaises(history.ApiError):
            history.search_conversation_history({"query": "release", "notice_kinds": ["self_memory_saved"]})
        with self.assertRaises(history.ApiError):
            history.read_conversation_history({"thread_id": "thread-1", "include_details": "true"})

    def test_notice_enum_and_filter_identity_are_shared(self) -> None:
        search = mcp_shim.SEARCH_CONVERSATION_HISTORY_TOOL["inputSchema"]["properties"]
        read = mcp_shim.READ_THREAD_HISTORY_TOOL["inputSchema"]["properties"]
        self.assertEqual(set(search["notice_kinds"]["items"]["enum"]), set(INPUT_KINDS))
        self.assertEqual(set(read["notice_kinds"]["items"]["enum"]), NOTICE_KINDS)
        self.assertEqual(search["cursor"]["maxLength"], history.CONVERSATION_CURSOR_BYTES)
        self.assertEqual(
            history._conversation_search_fingerprint(["release"], None, None, None, ["user", "assistant"], INPUT_KINDS),
            history._conversation_search_fingerprint(["release"], None, None, None, ["assistant", "user"], tuple(reversed(INPUT_KINDS))),
        )
