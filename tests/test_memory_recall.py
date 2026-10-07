"""Task-focused recall and its diagnostic evidence."""
from contextlib import ExitStack
import json
import os
import tempfile
import threading
import urllib.error
import re
import unittest
from unittest.mock import MagicMock, patch

from host.agent_messages import memory_context_message
from host.memory_recall import bound_query, conversation_query, RECALL_NOTICE_KINDS
from host.memory_recall_rules import MAX_QUERY_BYTES
from host.runtime import memory_context
from host.runtime.admin_api import conversation_history, threads
from host.runtime.host_inference import api, decisions, provider_http, providers, typesafe, client
from host.runtime.workspace import memory


TEXT_SETTINGS = {"instructions": "Return a JSON object that matches the supplied schema.",
                 "reasoning_effort": "none", "max_output_tokens": 400}
OPENAI_SETTINGS = {"model": "gpt-6-luna", **TEXT_SETTINGS}


def row(page_id):
    return (page_id, page_id, 'Guidance', 1, None, 'agent', 'now', 'now')


class MemoryRecallDiagnosticsTests(unittest.TestCase):
    def test_jev_recall_keeps_its_short_timeout_separate_from_task_titles(self):
        with patch.object(threads.workspace_proxy, "_proxy") as proxy:
            threads.workspace_proxy.recall_memory("thread-1", "Fix login")
        with patch.object(client, "_request") as inference:
            client.typesafe_jev_judgment({}, {"q0": {}}, timeout_seconds=memory.RECALL_RERANK_TIMEOUT_SECONDS)
        self.assertEqual(inference.call_args.args[2], 1.2)
        self.assertEqual(proxy.call_args.kwargs["timeout_seconds"], 3)
        with patch.object(client, "_request") as title:
            client.openai_text_completion("prompt", {}, "swarm_task", timeout_seconds=20, **OPENAI_SETTINGS)
        self.assertEqual(title.call_args.args[2], 20)

    def test_diagnostics_preserve_ranking_and_do_not_add_inference(self):
        direct, linked = row('direct-guide'), row('linked-guide')
        with ExitStack() as stack:
            for name, value in (
                ('_memory_search_generation', 'fixed'),
                ('_search_pages_exact', [direct]),
                ('_search_pages_lexical', [direct + (.5,)]),
                ('_lexical_page_id_tail', []),
                ('_search_pages_semantic', [direct + (.8,)]),
                ('_search_pages_graph', [linked + (1,)]),
                ('_current_page_rows', [direct, linked]),
            ):
                stack.enter_context(patch.object(memory, name, return_value=value))
            embed = stack.enter_context(patch.object(memory.embedding_client, 'embed_texts', return_value=[[1.0]]))
            counters = stack.enter_context(patch.object(memory, '_record_memory_top_hit'))
            baseline = memory._search_pages({'q': ['guide']}, scope='swarm', record_top_hit=False, semantic=True)
            embed.reset_mock()
            details = []
            actual = memory._search_pages({'q': ['guide']}, scope='swarm', record_top_hit=False, semantic=True, diagnostics=details)
        self.assertEqual(
            [page["page_id"] for page in actual["pages"]],
            [page["page_id"] for page in baseline["pages"]],
        )
        self.assertEqual(actual["search_mode"], baseline["search_mode"])
        embed.assert_called_once()
        counters.assert_not_called()
        trace = '\n'.join(details)
        self.assertIn('direct-guide r1; memory relevance 0.114754 — exact rank 1, lexical rank 1, semantic rank 1 cosine 0.800', trace)
        self.assertIn('linked-guide r1; memory relevance 0.008197 — graph rank 1', trace)

    def test_admission_keeps_diagnostics_out_of_search_and_model_input(self):
        with patch.object(threads.state, 'page_thread_events', return_value=[]), \
             patch.object(threads.workspace_proxy, 'recall_memory', return_value={'pages': [], 'diagnostics': 'Current query: test'}) as recall:
            pages, details = threads._recalled_memory_pages('thread-1', 'test')
        recall.assert_called_once_with('thread-1', 'test')
        self.assertIn('Current query: test', details)
        self.assertNotIn('Current query: test', memory_context_message('thread-1', pages))

    def test_empty_recall_and_categories_are_recorded(self):
        empty = memory._recall_response([], ['Relevant search unavailable.'], 0)
        self.assertIn('0 memory-content bytes', empty['diagnostics'])
        self.assertIn('unavailable', empty['diagnostics'])
        pages = [{'page_id': 'thread-1', 'revision': 2, 'content': '😀'},
                 {'page_id': 'popular-guide', 'revision': 3, 'content': 'x', 'selection': 'popular'}]
        result = memory._recall_response(pages, [], 0)
        self.assertEqual(result['pages'], pages)
        self.assertIn('5 memory-content bytes', result['diagnostics'])
        self.assertIn('Selected thread-1 r2: self', result['diagnostics'])
        self.assertIn('Selected popular-guide r3: popular', result['diagnostics'])

    def test_history_bounds_requested_memory_diagnostics(self):
        event = {'event_id': 'event_1', 'timestamp': 'now', 'event_type': 'thread.notice',
                 'payload': {'notice': {'kind': 'memory_injection', 'summary': 'Self identity and 0 memories injected.'}, 'memory_page_ids': []}}
        self.assertNotIn('memory_recall_details', conversation_history._conversation_event(event, True))
        event['payload']['memory_recall_details'] = '😀\\"' * 10000
        result = conversation_history._conversation_event(event, True)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(json.dumps(result['memory_recall_details'], ensure_ascii=False).encode()), 14000)


class TaskRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        # Retrieval tests keep hybrid order; reranker tests cover Jev outcomes.
        self.enterContext(patch.object(memory, "judge", side_effect=client.HostInferenceError("disabled", reason="provider_disabled")))
        self.enterContext(patch.object(memory, "openai_decisions", side_effect=client.HostInferenceError("disabled", reason="provider_disabled")))

    def test_admission_and_midturn_share_context_with_independent_buckets(self):
        history = [
            {"event_type": "thread.message", "payload": {"source": "user", "message": "Generate an Aira video with Grok audio"}},
            {"event_type": "thread.message", "payload": {"source": "agent", "message": "Preparing a PR"}},
            {"event_type": "thread.message", "payload": {"source": "user", "message": "Use a quieter voice"}},
        ]
        incoming = {"event_type": "thread.message", "payload": {
            "source": "user", "message": "Review it", "notice": {"kind": "operator"}}}
        with patch.object(threads.state, "recall_context_events", return_value=history):
            admitted = threads._memory_task_query("thread-1", "Review it", {"kind": "operator"})
        with patch.object(threads.state, "recall_context_events", return_value=[*history, incoming]):
            reevaluated = memory_context.load_query("thread-1")
        self.assertEqual(admitted, reevaluated)
        self.assertEqual(admitted.split("\n\n"), [
            "User: Review it", "User: Use a quieter voice", "User: Generate an Aira video with Grok audio",
            "Assistant: Preparing a PR"])

    def test_context_stops_at_memory_clear_for_both_roles(self):
        history = [
            {"event_type": "thread.message", "payload": {"source": "user", "message": "Old task"}},
            {"event_type": "thread.message", "payload": {"source": "agent", "message": "Old plan"}},
            {"event_type": "thread.memory_cleared", "payload": {}},
            {"event_type": "thread.message", "payload": {"source": "user", "message": "New task"}},
        ]
        with patch.object(threads.state, "recall_context_events", return_value=history):
            self.assertEqual(threads._memory_task_query("thread-1", "continue"), "User: continue\n\nUser: New task")
            history.append({"event_type": "thread.memory_cleared", "payload": {}})
            self.assertEqual(threads._memory_task_query("thread-1", "continue"), "User: continue")

    def test_user_and_assistant_budgets_are_independent_and_utf8_safe(self):
        history = [
            {"event_type": "thread.message", "payload": {"source": "user", "message": "Earlier direction"}},
            {"event_type": "thread.message", "payload": {"source": "agent", "message": "界" * 600}},
        ]
        with patch.object(threads.state, "recall_context_events", return_value=history):
            query = threads._memory_task_query("thread-1", "Current request")
            self.assertTrue(query.startswith("User: Current request\n\nUser: Earlier direction\n\nAssistant: 界"))
            long = threads._memory_task_query("thread-1", "a" * 1200)
        self.assertEqual(len(long.split("\n\n")[0].encode()), 1000)
        self.assertLessEqual(len(long.split("\n\n")[1].encode()), 500)
        self.assertLessEqual(len(long.encode()), MAX_QUERY_BYTES)
        self.assertNotIn("�", query + long)

    def test_empty_current_message_does_not_recall_old_work(self):
        with patch.object(threads.state, "recall_context_events") as read:
            self.assertEqual(threads._memory_task_query("thread-1", "   "), "")
            read.assert_not_called()

    def test_message_boundaries_and_negation_survive(self):
        history = [{"event_type": "thread.message", "payload": {
            "source": "user", "message": "Don't deploy billing.\n\nUse staging first."}}]
        with patch.object(threads.state, "recall_context_events", return_value=history):
            query = threads._memory_task_query("thread-1", "Review PLS\nmodel")
        self.assertEqual(query, "User: Review PLS model\n\nUser: Don't deploy billing. Use staging first.")
        self.assertEqual(bound_query(query), query)

    def test_notice_allowlist_excludes_tool_data_and_feedback(self):
        from host import agent_messages
        events = []
        for kind in agent_messages.NOTICE_KINDS:
            events.append({"event_type": "thread.notice", "payload": {
                "source": "user", "message": f"Body-{kind}",
                "notice": {"kind": kind, "summary": f"Summary-{kind}", "details": "PRIVATE TOOL JSON"},
                "memory_recall_details": "RECALLED BODY", "historical_context": "TRANSFERRED HISTORY"}})
        query = conversation_query(events)
        self.assertEqual(RECALL_NOTICE_KINDS, {'agent_message', 'scheduled_trigger', 'approval_outcome'})
        self.assertIn('Peer: Body-agent_message', query)
        self.assertIn('Scheduled request: Body-scheduled_trigger', query)
        self.assertIn('Approval outcome: Summary-approval_outcome', query)
        self.assertNotIn('Body-approval_outcome', query)
        for forbidden in ('PRIVATE', 'RECALLED', 'TRANSFERRED', 'Body-memory', 'Body-restart', 'Body-retry', 'Body-automated'):
            self.assertNotIn(forbidden, query)

    def test_provenance_and_routing_envelopes(self):
        from host import agent_messages
        peer = agent_messages.peer_message("schedule-38", "Review Aira video")
        scheduled = agent_messages.scheduled_message("Prepare the report")
        events = [{"event_type": "thread.notice", "payload": {
            "source": "user", "message": text, "notice": {"kind": kind, "summary": "Display only"}}}
            for text, kind in ((peer, 'agent_message'), (scheduled, 'scheduled_trigger'))]
        self.assertEqual(conversation_query(events), 'Scheduled request: Prepare the report\n\nPeer: Review Aira video')
        # Explicit operator provenance beats pasted text; legacy rows stay as recorded.
        events = [{"event_type": "thread.message", "payload": {
            "source": "user", "message": peer, "notice": {"kind": "operator"}}}]
        self.assertTrue(conversation_query(events).startswith('User: This is a message from another agent'))
        events[0]['payload'].pop('notice')
        self.assertTrue(conversation_query(events).startswith('User: This is a message from another agent'))

    def test_unavailable_history_preserves_incoming_request(self):
        with (patch.object(threads.state, 'recall_context_events', side_effect=OSError('unavailable')),
              patch.object(memory_context.host_errors, 'report_warning')):
            self.assertEqual(threads._memory_task_query('thread-1', 'Review login'), 'User: Review login')

    def test_recall_preserves_hybrid_order_without_a_provider(self):
        pages = [
            {"page_id": "kern-repo-dev-guidelines", "description": "Repository conventions", "revision": 1},
            {"page_id": "scheduled-agents-model", "description": "Creating or debugging schedules", "revision": 1},
            {"page_id": "synonym-guide", "description": "Recurring jobs", "revision": 1},
        ]
        def load(page_id):
            if page_id == "thread-1":
                return {"page_id": page_id, "description": "Self", "content": "My notes", "revision": 1}
            return {**next(p for p in pages if p["page_id"] == page_id), "content": "Guidance"}
        with patch.object(memory, "load_page", side_effect=load), \
             patch.object(memory, "_search_pages", return_value={"pages": pages}) as search, \
             patch.object(memory, "_popular_rows") as popular:
            result = memory.recall_pages({"thread_id": "thread-1", "message": "scheduled agents"})
        self.assertEqual([p["page_id"] for p in result["pages"]],
                         ["thread-1", "kern-repo-dev-guidelines", "scheduled-agents-model", "synonym-guide"])
        self.assertFalse(search.call_args.kwargs["include_graph"])
        popular.assert_not_called()

    def test_empty_message_keeps_self_without_search(self):
        page = {"page_id": "thread-1", "description": "Self", "content": "My notes", "revision": 1}
        with patch.object(memory, "load_page", return_value=page), \
             patch.object(memory, "_search_pages") as search, \
             patch.object(memory, "_popular_rows") as popular:
            result = memory.recall_pages({"thread_id": "thread-1", "message": "   "})
        self.assertEqual(result["pages"], [{**page, "scope": "self"}])
        search.assert_not_called()
        popular.assert_not_called()

    def test_recall_omits_graph_only_pages_but_explicit_search_keeps_them(self):
        direct, linked = row("direct-guide"), row("linked-guide")
        with ExitStack() as stack:
            for name, value in (
                ("_memory_search_generation", "fixed"),
                ("_search_pages_exact", [direct]),
                ("_search_pages_lexical", [direct + (.5,)]),
                ("_lexical_page_id_tail", []),
                ("_search_pages_semantic", [direct + (.8,)]),
                ("_search_pages_graph", [linked + (1,)]),
            ):
                stack.enter_context(patch.object(memory, name, return_value=value))
            stack.enter_context(patch.object(memory, "_current_page_rows", side_effect=lambda ids, **kw: [r for r in (direct, linked) if r[0] in ids]))
            stack.enter_context(patch.object(memory.embedding_client, "embed_texts", return_value=[[1.0]]))
            recalled = memory._search_pages({"q": ["guide"]}, scope="swarm", record_top_hit=False, semantic=True, include_graph=False)
            searched = memory._search_pages({"q": ["guide"]}, scope="swarm", record_top_hit=False, semantic=True)
        self.assertEqual([p["page_id"] for p in recalled["pages"]], ["direct-guide"])
        self.assertEqual([p["page_id"] for p in searched["pages"]], ["direct-guide", "linked-guide"])

    def test_history_failure_still_fetches_self_memory(self):
        with patch.object(threads.state, "recall_context_events", side_effect=OSError("unavailable")), \
             patch.object(memory_context.host_errors, "report_warning") as report, \
             patch.object(threads.workspace_proxy, "recall_memory", return_value={"pages": []}) as recall:
            threads._recalled_memory_pages("thread-1", threads._memory_task_query("thread-1", "?"))
        recall.assert_called_once_with("thread-1", "User: ?")
        report.assert_called_once()

    def test_topic_switch_uses_hybrid_order_without_a_provider(self):
        candidates = [
            {"page_id": "video-guide", "description": "Aira video Grok audio", "revision": 1},
            {"page_id": "billing-guide", "description": "SnowBid billing", "revision": 1},
        ]
        def load(page_id):
            if page_id == "thread-1":
                return {"page_id": page_id, "description": "Self", "content": "Notes", "revision": 1}
            return {**next(p for p in candidates if p["page_id"] == page_id), "content": "Guidance"}
        query = "Investigate SnowBid billing\n\nGenerate an Aira video with Grok audio"
        with patch.object(memory, "load_page", side_effect=load), \
             patch.object(memory, "_search_pages", return_value={"pages": candidates}) as search:
            result = memory.recall_pages({"thread_id": "thread-1", "message": query})
        self.assertEqual([p["page_id"] for p in result["pages"]],
                         ["thread-1", "video-guide", "billing-guide"])
        self.assertEqual(search.call_args.args[0], {"q": [query], "limit": ["20"]})
        self.assertTrue(search.call_args.kwargs["keyword_alternatives"])

    def test_recall_context_reaches_embedding_and_lexical_channels(self):
        guide = row("aira-guide")
        query = "User: " + "Generate an Aira video with Grok audio. " * 25 + "\n\nAssistant: " + ("Preparing a PR. " * 20).strip()
        self.assertGreater(len(query.encode()), 1000)
        self.assertLessEqual(len(query.encode()), MAX_QUERY_BYTES)
        with ExitStack() as stack:
            for name, value in (("load_page", {"page_id": "thread-1", "revision": 1, "content": "Notes"}),
                                ("_memory_search_generation", "fixed"),
                                ("_search_pages_exact", []), ("_lexical_page_id_tail", []),
                                ("_search_pages_semantic", [guide + (.8,)]),
                                ("_current_page_rows", [guide])):
                stack.enter_context(patch.object(memory, name, return_value=value))
            lexical = stack.enter_context(patch.object(memory, "_search_pages_lexical", return_value=[guide + (.5,)]))
            embed = stack.enter_context(patch.object(memory.embedding_client, "embed_texts", return_value=[[1.0]]))
            append = stack.enter_context(patch.object(memory, "_append_recalled_pages"))
            memory.recall_pages({"thread_id": "thread-1", "message": query})
        embed.assert_called_once_with([query], kind="query")
        self.assertEqual(lexical.call_args.args[0], query)
        self.assertTrue(lexical.call_args.kwargs["alternatives"])
        self.assertEqual(append.call_args.args[2], 5)

    def test_nonempty_stopwords_or_punctuation_still_reach_semantic_search(self):
        for query in ("the and you", "?", "IT", "May"):
            with self.subTest(query=query), ExitStack() as stack:
                for name, value in (("load_page", {"page_id": "thread-1", "revision": 1, "content": "Notes"}),
                                    ("_memory_search_generation", 1), ("_search_pages_exact", []),
                                    ("_search_pages_lexical", []), ("_lexical_page_id_tail", []),
                                    ("_search_pages_semantic", [row("guide") + (.8,)]),
                                    ("_current_page_rows", [row("guide")])):
                    stack.enter_context(patch.object(memory, name, return_value=value))
                embed = stack.enter_context(patch.object(memory.embedding_client, "embed_texts", return_value=[[1.0]]))
                append = stack.enter_context(patch.object(memory, "_append_recalled_pages"))
                memory.recall_pages({"thread_id": "thread-1", "message": query})
                embed.assert_called_once_with([query], kind="query")
                self.assertEqual(append.call_args.args[1][0]["page_id"], "guide")


class RecallRerankingTests(unittest.TestCase):
    def setUp(self):
        self.pages = [{"page_id": f"guide-{i}", "description": f"Task guidance {i}",
                       "revision": i + 1, "content": "PRIVATE BODY",
                       "memory_relevance_score": .1 / (i + 1)} for i in range(6)]
        self.scores = {f"q{i}": i / 10 for i in range(6)}
        self.config_read = self.enterContext(patch.object(memory.state, "host_inference_provider_is_enabled", side_effect=AssertionError("Workspace must not read provider settings")))
        self.judge = self.enterContext(patch.object(memory, "judge", return_value={
            "model": "jev-latest", "answers": {k: {"type": "noul", "noul": v} for k, v in self.scores.items()}}))
        self.decisions = self.enterContext(patch.object(memory, "openai_decisions",
            side_effect=client.HostInferenceError("disabled", reason="provider_disabled")))

    def rerank(self, pages=None, *, query="Fix login\n\nDo not deploy"):
        details = []
        memory._rerank_recall(self.pages if pages is None else pages,
                              query=query, details=details)
        self.details = details
        return json.loads(next(d.removeprefix("Rerank: ") for d in details if d.startswith("Rerank: ")))

    def shadow(self):
        return json.loads(next(d.removeprefix("Rerank shadow: ") for d in self.details if d.startswith("Rerank shadow: ")))

    def decision_result(self, scores):
        return {"model": "gpt-6-luna", "answers": [
            {"type": "predicate", "name": key, "probability": value} for key, value in scores.items()]}

    def test_luna_compares_original_hybrid_candidates_in_parallel_without_changing_jev_selection(self):
        arrived = threading.Barrier(2)
        evidence = {}
        def jev(*args, **kwargs):
            arrived.wait(timeout=2)
            evidence["jev"] = args[0]
            return self.judge.return_value
        def luna(input, questions, **kwargs):
            arrived.wait(timeout=2)
            evidence["luna"] = json.loads(input)
            evidence["questions"] = questions
            self.assertEqual(kwargs, {"model": "gpt-6-luna", "timeout_seconds": 1.2})
            return self.decision_result({key: 1 - value for key, value in self.scores.items()})
        self.judge.side_effect = jev
        self.decisions.side_effect = luna
        result = self.rerank()
        shadow = self.shadow()
        self.assertEqual(evidence["jev"], evidence["luna"])
        self.assertEqual([c["id"] for c in evidence["luna"]["candidates"]], list(self.scores))
        self.assertEqual([q["name"] for q in evidence["questions"]], list(self.scores))
        self.assertNotIn("PRIVATE BODY", json.dumps(evidence))
        self.assertNotIn("guide-", json.dumps(evidence))
        self.assertEqual([p["page_id"] for p in self.pages], [f"guide-{i}" for i in reversed(range(6))])
        self.assertEqual(result["selection"], [f"guide-{i}" for i in (5, 4, 3, 2, 1)])
        self.assertEqual(shadow["would_select"], [f"guide-{i}" for i in range(5)])
        self.assertEqual(shadow["candidates"][0]["would_rank"], 1)
        self.assertEqual(shadow["candidates"][0]["score"], 1)
        self.assertEqual(shadow["candidates"][0]["revision"], 1)
        self.assertFalse(shadow["applied"])
        self.assertEqual(shadow["api"], "decisions")
        self.assertEqual(shadow["outcome"], "success")
        self.assertIn("elapsed_ms", shadow)
        self.config_read.assert_not_called()

    def test_luna_success_never_replaces_hybrid_fallback_when_jev_is_disabled(self):
        self.judge.side_effect = client.HostInferenceError("disabled", reason="provider_disabled")
        self.decisions.side_effect = None
        self.decisions.return_value = self.decision_result(self.scores)
        result = self.rerank()
        self.assertEqual(result["outcome"], "provider_disabled")
        self.assertEqual([p["page_id"] for p in self.pages], [f"guide-{i}" for i in range(6)])
        self.assertEqual(self.shadow()["would_select"], [f"guide-{i}" for i in (5, 4, 3, 2, 1)])

    def test_luna_disabled_timeout_failure_and_refusal_preserve_jev_ranking(self):
        cases = [
            (client.HostInferenceError("disabled", reason="provider_disabled"), "provider_disabled"),
            (TimeoutError("PRIVATE PROVIDER DETAIL"), "timeout"),
            (client.HostInferenceError("PRIVATE PROVIDER DETAIL"), "provider_unavailable"),
        ]
        for failure, outcome in cases:
            with self.subTest(outcome=outcome), patch.object(memory.host_errors, "report_warning"):
                self.decisions.side_effect = failure
                self.decisions.reset_mock()
                result = self.rerank([dict(p) for p in self.pages])
                shadow = self.shadow()
                self.assertEqual(result["outcome"], "success")
                self.assertEqual(shadow["outcome"], outcome)
                self.assertTrue(all(c["score"] is None for c in shadow["candidates"]))
                self.assertNotIn("PRIVATE PROVIDER DETAIL", json.dumps(shadow))
                self.decisions.assert_called_once()
        self.decisions.side_effect = None
        refused = self.decision_result(self.scores)
        refused["answers"][2] = {"type": "refusal", "name": "q2"}
        self.decisions.return_value = refused
        self.rerank()
        self.assertEqual(self.shadow()["outcome"], "refusal")
        self.assertTrue(all(c["score"] is None for c in self.shadow()["candidates"]))

    def test_luna_requires_complete_valid_scores_and_keeps_hybrid_ties(self):
        self.decisions.side_effect = None
        good = self.decision_result(self.scores)
        invalid = [None, {}, {**good, "answers": good["answers"][:-1]},
                   {**good, "answers": list(reversed(good["answers"]))}]
        invalid += [self.decision_result({**self.scores, "q0": value})
                    for value in (True, "0.5", -1, 2, float("nan"), float("inf"))]
        for response in invalid:
            with self.subTest(response=response):
                self.decisions.return_value = response
                self.rerank([dict(p) for p in self.pages])
                self.assertNotEqual(self.shadow()["outcome"], "success")
                self.assertTrue(all(c["score"] is None for c in self.shadow()["candidates"]))
        self.decisions.return_value = self.decision_result({key: .5 for key in self.scores})
        self.rerank()
        self.assertEqual(self.shadow()["would_select"], [f"guide-{i}" for i in range(5)])

    def test_empty_candidates_skip_both_rankings_and_record_a_clear_reason(self):
        candidates = []
        details = []
        with patch.object(memory, "_jev_recall_ranking") as jev, \
             patch.object(memory, "_luna_recall_ranking") as luna:
            memory._rerank_recall(candidates, query="Fix login", details=details)
        self.assertEqual(candidates, [])
        self.assertEqual(details, ["Rerank skipped: no candidates."])
        jev.assert_not_called()
        luna.assert_not_called()
        self.judge.assert_not_called()
        self.decisions.assert_not_called()

    def test_luna_real_socket_records_success_disabled_and_timeout_alongside_jev(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = directory + "/inference.sock"
            server = api.HostInferenceServer(socket_path, frozenset({os.getuid()}))
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            worker.start()
            try:
                for outcome in ("success", "provider_disabled", "timeout"):
                    with self.subTest(outcome=outcome), \
                         patch.object(memory, "openai_decisions", client.openai_decisions), \
                         patch.object(client, "SOCKET_PATH", socket_path), \
                         patch.object(providers.state, "enabled_host_inference_provider",
                                      return_value=None if outcome == "provider_disabled" else {"api_key": "test"}) as config, \
                         patch.object(provider_http, "post", return_value=json.dumps(self.decision_result(self.scores)).encode(),
                                      side_effect=TimeoutError("PRIVATE") if outcome == "timeout" else None) as post, \
                         patch.object(providers.usage, "record_openai_decision_response"), \
                         patch.object(memory.host_errors, "report_warning"):
                        self.rerank([dict(p) for p in self.pages])
                    self.assertEqual(self.shadow()["outcome"], outcome)
                    config.assert_called_once_with("openai")
                    self.assertEqual(post.call_count, int(outcome != "provider_disabled"))
                    if post.called:
                        self.assertEqual(post.call_args.args[0], decisions.ENDPOINT)
                        self.assertEqual(post.call_args.kwargs["timeout"], 1.2)
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_jev_always_used_when_enabled_and_disabled_keeps_hybrid_order(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                self.judge.side_effect = None if enabled else client.HostInferenceError("disabled", reason="provider_disabled")
                self.judge.reset_mock()
                pages = [dict(p) for p in self.pages]
                result = self.rerank(pages)
                self.config_read.assert_not_called()
                self.judge.assert_called_once()
                self.assertNotIn("probability", result)
                self.assertEqual(result["provider"], "typesafe")
                self.assertEqual(result["model"], "jev-latest")
                self.assertEqual(result["version"], 5)
                self.assertEqual(result["outcome"], "success" if enabled else "provider_disabled")
                self.assertEqual(result["fallback"], not enabled)
                if not enabled:
                    self.assertTrue(all(c["score"] is None for c in result["candidates"]))
                expected = list(reversed(self.pages)) if enabled else self.pages
                self.assertEqual(pages, expected)
                self.assertEqual(result["selection"], [p["page_id"] for p in expected[:5]])
                self.assertEqual(result["candidates"][0]["hybrid_rank"], 1)
                self.assertEqual(result["candidates"][0]["final_rank"], 6 if enabled else 1)
                self.assertIn("elapsed_ms", result)

    def test_complete_valid_scores_required_and_no_retry_on_failure(self):
        bad_values = [None, {}, {"q0": .5}, {**self.scores, "extra": .5}]
        bad_values += [{**self.scores, "q0": v} for v in (True, "0.5", -1, 2, float("nan"), float("inf"))]
        for values in bad_values:
            with self.subTest(values=values):
                self.judge.reset_mock()
                self.judge.return_value = {"model": "jev-latest", "answers":
                    {k: {"type": "noul", "noul": v} for k, v in values.items()} if isinstance(values, dict) else values}
                result = self.rerank()
                self.assertTrue(result["fallback"])
                self.assertEqual([p["page_id"] for p in self.pages], [f"guide-{i}" for i in range(6)])
                self.assertTrue(all(c["score"] is None for c in result["candidates"]))
                self.judge.assert_called_once()

    def test_errors_and_ties(self):
        self.judge.side_effect = TimeoutError("private response")
        with patch.object(memory.host_errors, "report_warning"):
            result = self.rerank()
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["error_type"], "TimeoutError")
        self.assertNotIn("private response", json.dumps(result))
        self.judge.side_effect = None
        self.judge.return_value = {"model": "jev-latest", "answers":
            {k: {"type": "noul", "noul": .5} for k in self.scores}}
        result = self.rerank()
        self.assertEqual(result["selection"], [f"guide-{i}" for i in range(5)])

    def test_socket_timeouts_keep_hybrid_order(self):
        connection = MagicMock()
        connection.getresponse.side_effect = TimeoutError("private network detail")
        with patch.object(memory, "judge", client.typesafe_jev_judgment), \
             patch.object(client, "_HostInferenceConnection", return_value=connection) as connect, \
             patch.object(memory.host_errors, "report_warning"):
            result = self.rerank()
        connect.assert_called_once_with(1.3)
        self.assertEqual(result["provider"], "typesafe")
        self.assertEqual(result["timeout_seconds"], 1.2)
        self.assertEqual(result["outcome"], "timeout")
        self.assertTrue(result["fallback"])
        self.assertEqual(result["selection"], [f"guide-{i}" for i in range(5)])
        self.assertTrue(all(c["score"] is None for c in result["candidates"]))
        self.assertNotIn("private network detail", json.dumps(result))
        self.assertIn("elapsed_ms", result)

    def test_provider_outcomes_cross_the_real_socket_into_recall_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            socket_path = directory + "/host-inference.sock"
            server = api.HostInferenceServer(socket_path, frozenset({os.getuid()}))
            worker = threading.Thread(target=server.serve_forever,
                                      kwargs={"poll_interval": .01}, daemon=True)
            worker.start()
            try:
                for error in (None, TimeoutError("private timeout"),
                              urllib.error.URLError(TimeoutError("private timeout"))):
                    with self.subTest(error=type(error).__name__), \
                         patch.object(memory, "judge", client.typesafe_jev_judgment), \
                         patch.object(client, "SOCKET_PATH", socket_path), \
                         patch.object(providers.state, "enabled_host_inference_provider",
                                      return_value={"api_key": "test-key"} if error else None) as config, \
                         patch.object(provider_http._OPENER, "open", side_effect=error) as request, \
                         patch.object(memory.host_errors, "report_warning"):
                        result = self.rerank()
                    config.assert_called_once_with("typesafe")
                    self.assertEqual(result["outcome"], "timeout" if error else "provider_disabled")
                    self.assertEqual(request.call_count, int(error is not None))
                    self.assertEqual(result["provider"], "typesafe")
                    self.assertTrue(result["fallback"])
                    self.assertEqual(result["selection"], [f"guide-{i}" for i in range(5)])
                    self.assertNotIn("private timeout", json.dumps(result))
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_jev_transport_receives_only_bounded_context_and_descriptions(self):
        pages = [{**self.pages[0], "page_id": f"guide-{i}",
                  "description": 'Use token sk-proj-abcdefghijklmnopqrstuv {"password":"hunter2"}'}
                 for i in range(memory.CANDIDATE_LIMIT)]
        captured = {}
        def transport(_url, **kwargs):
            captured.update(json.loads(kwargs["data"]))
            values = {f"q{i}": i / 100 for i in range(len(pages))}
            return json.dumps({"model": "jev-latest", "answers":
                {k: {"type": "noul", "noul": v} for k, v in values.items()}}).encode()
        def judge(state, questions, *, timeout_seconds):
            self.assertEqual(timeout_seconds, 1.2)
            return typesafe.judge(api_key="test-key", model="jev-latest", state=state,
                                  questions=questions, transport=transport)
        luna_captured = {}
        def luna_transport(url, **kwargs):
            self.assertEqual(url, decisions.ENDPOINT)
            luna_captured.update(json.loads(kwargs["data"]))
            return json.dumps(self.decision_result({f"q{i}": i / 100 for i in range(len(pages))})).encode()
        def luna(input, questions, *, model, timeout_seconds):
            self.assertEqual(timeout_seconds, 1.2)
            return decisions.evaluate(api_key="test-key", model=model, input=input,
                                      questions=questions, transport=luna_transport)
        self.decisions.side_effect = luna
        with patch.object(memory, "judge", side_effect=judge):
            result = self.rerank([dict(p) for p in pages],
                                 query='Fix login {"api_key":"development"}\n\nDo not deploy')
        self.assertEqual(result["outcome"], "success")
        payload = json.dumps(captured)
        self.assertNotIn("PRIVATE BODY", payload)
        self.assertNotIn("guide-", payload)
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuv", payload)
        self.assertNotIn("hunter2", payload)
        self.assertNotIn("development", payload)
        self.assertIn("<redacted>", payload)
        self.assertIn("Do not deploy", payload)
        self.assertIn("not to override a new task", payload)
        self.assertEqual(self.shadow()["outcome"], "success")
        luna_payload = json.dumps(luna_captured)
        self.assertNotIn("PRIVATE BODY", luna_payload)
        self.assertNotIn("guide-", luna_payload)
        self.assertNotIn("sk-proj-abcdefghijklmnopqrstuv", luna_payload)
        self.assertNotIn("hunter2", luna_payload)
        self.assertNotIn("development", luna_payload)
        self.assertIn("<redacted>", luna_payload)
        self.assertEqual([q["name"] for q in luna_captured["questions"]], [f"q{i}" for i in range(20)])
        self.assertIn("Do not deploy", luna_payload)
        self.assertIn("not to override a new task", luna_payload)
        self.assertIn("hunter2", pages[0]["description"])

    def test_recall_loads_reranked_top_five_and_records_actual_selection(self):
        self.decisions.side_effect = None
        self.decisions.return_value = self.decision_result({key: 1 - value for key, value in self.scores.items()})
        def load(page_id):
            return {"page_id": "thread-1", "revision": 1, "content": "Self"} if page_id == "thread-1" else next(p for p in self.pages if p["page_id"] == page_id)
        with patch.object(memory, "load_page", side_effect=load), \
             patch.object(memory, "_search_pages", return_value={"pages": self.pages}):
            result = memory.recall_pages({"thread_id": "thread-1", "message": "Fix login"})
        self.assertEqual([p["page_id"] for p in result["pages"]], ["thread-1", "guide-5", "guide-4", "guide-3", "guide-2", "guide-1"])
        self.assertIn("Selected guide-5 r6", result["diagnostics"])
        shadow = json.loads(result["diagnostics"].split("Rerank shadow: ")[1])
        self.assertEqual(shadow["would_select"], [f"guide-{i}" for i in range(5)])
        self.assertNotIn("Rerank shadow", memory_context_message("thread-1", result["pages"]))


class RecallContextHistoryTests(unittest.TestCase):
    def setUp(self):
        import pg_harness
        pg_harness.reset_database()

    def test_assistant_and_excluded_notices_cannot_evict_user_history(self):
        state = threads.state
        with state.mutation() as cur:
            state.append_agent_event(cur, 'thread.message', 'thread-1', {
                'source': 'user', 'message': 'Do not deploy. Fix arrowheads.', 'notice': {'kind': 'operator'}})
            for n in range(30):
                state.append_agent_event(cur, 'thread.message', 'thread-1', {
                    'source': 'agent', 'message': f'Progress {n}'})
            state.append_agent_event(cur, 'thread.notice', 'thread-1', {
                'source': 'user', 'message': 'PRIVATE RESULT JSON',
                'notice': {'kind': 'approval_outcome', 'summary': 'Approved and completed: Open PR'}})
            for n in range(30):
                state.append_agent_event(cur, 'thread.notice', 'thread-1', {
                    'notice': {'kind': 'app_data_changed', 'summary': 'Action receipt', 'details': 'PRIVATE REQUEST'}})
        events = state.recall_context_events('thread-1')
        self.assertLessEqual(len(events), 24)
        query = conversation_query(events)
        self.assertTrue(query.startswith('User: Do not deploy. Fix arrowheads.'))
        self.assertIn('Approval outcome: Approved and completed: Open PR', query)
        self.assertIn('Assistant: Progress 29', query)
        self.assertNotIn('PRIVATE', query)
        self.assertNotIn('Action receipt', str(events))
        with state.mutation() as cur:
            state.append_agent_event(cur, 'thread.memory_cleared', 'thread-1', {})
            state.append_agent_event(cur, 'thread.message', 'thread-1', {
                'source': 'user', 'message': 'New task', 'notice': {'kind': 'operator'}})
        self.assertEqual(memory_context.load_query('thread-1'), 'User: New task')
