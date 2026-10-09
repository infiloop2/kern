"""Task-focused recall and its diagnostic evidence."""
from contextlib import ExitStack, contextmanager
import json
from itertools import product
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
from host.runtime.core.state import events as event_state, host_inference as provider_state
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
        self.assertEqual(append.call_args.args[2], memory.RECALL_RELEVANT_LIMIT)

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
        self.selected = list(self.pages) if pages is None else pages
        memory._rerank_recall(self.selected,
                              query=query, details=details)
        self.details = details
        return self.ranking_diagnostic()["providers"]["jev"]

    def luna_diagnostic(self):
        return self.ranking_diagnostic()["providers"]["luna"]

    def decision_result(self, scores):
        return {"model": "gpt-6-luna", "answers": [
            {"type": "predicate", "name": key, "probability": value} for key, value in scores.items()]}

    def test_luna_ranks_original_hybrid_candidates_in_parallel_and_contributes_to_selection(self):
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
        luna = self.luna_diagnostic()
        self.assertEqual(evidence["jev"], evidence["luna"])
        self.assertEqual([c["id"] for c in evidence["luna"]["candidates"]], list(self.scores))
        self.assertEqual([q["name"] for q in evidence["questions"]], list(self.scores))
        self.assertNotIn("PRIVATE BODY", json.dumps(evidence))
        self.assertNotIn("guide-", json.dumps(evidence))
        self.assertEqual([p["page_id"] for p in self.selected], [f"guide-{i}" for i in (5, 0, 4, 1, 3, 2)])
        self.assertEqual(self.proposed_selection("jev"), [f"guide-{i}" for i in (5, 4, 3, 2, 1)])
        self.assertEqual(self.proposed_selection("luna"), [f"guide-{i}" for i in range(5)])
        self.assertEqual(self.candidate_scores("luna")[0]["rank"], 1)
        self.assertEqual(self.candidate_scores("luna")[0]["score"], 1)
        self.assertEqual(self.ranking_diagnostic()["candidates"][0]["revision"], 1)
        self.assertTrue(luna["applied"])
        self.assertEqual(luna["api"], "decisions")
        self.assertEqual(luna["outcome"], "success")
        self.assertIn("elapsed_ms", luna)
        self.config_read.assert_not_called()

    def test_luna_success_selects_its_top_five_when_jev_is_disabled(self):
        self.judge.side_effect = client.HostInferenceError("disabled", reason="provider_disabled")
        self.decisions.side_effect = None
        self.decisions.return_value = self.decision_result(self.scores)
        result = self.rerank()
        self.assertEqual(result["outcome"], "provider_disabled")
        self.assertEqual([p["page_id"] for p in self.selected], [f"guide-{i}" for i in (5, 4, 3, 2, 1)])
        self.assertEqual(self.proposed_selection("luna"), [f"guide-{i}" for i in (5, 4, 3, 2, 1)])

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
                luna = self.luna_diagnostic()
                self.assertEqual(result["outcome"], "success")
                self.assertEqual(luna["outcome"], outcome)
                self.assertTrue(all(c["score"] is None for c in self.candidate_scores("luna")))
                self.assertNotIn("PRIVATE PROVIDER DETAIL", json.dumps(luna))
                self.decisions.assert_called_once()
        self.decisions.side_effect = None
        refused = self.decision_result(self.scores)
        refused["answers"][2] = {"type": "refusal", "name": "q2"}
        self.decisions.return_value = refused
        self.rerank()
        self.assertEqual(self.luna_diagnostic()["outcome"], "refusal")
        self.assertTrue(all(c["score"] is None for c in self.candidate_scores("luna")))

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
                self.assertNotEqual(self.luna_diagnostic()["outcome"], "success")
                self.assertTrue(all(c["score"] is None for c in self.candidate_scores("luna")))
        self.decisions.return_value = self.decision_result({key: .5 for key in self.scores})
        self.rerank()
        self.assertEqual(self.proposed_selection("luna"), [f"guide-{i}" for i in range(5)])

    def ranking_diagnostic(self):
        return json.loads(next(d.removeprefix("Rerank: ") for d in self.details if d.startswith("Rerank: ")))

    def selection_diagnostic(self):
        diagnostic = self.ranking_diagnostic()
        return {"version": diagnostic["version"], "strategy": diagnostic["strategy"],
                "provider_outcomes": {name: p["outcome"] for name, p in diagnostic["providers"].items()},
                "selection": diagnostic["selection"]}

    def candidate_scores(self, provider):
        return [c[provider] for c in self.ranking_diagnostic()["candidates"]]

    def proposed_selection(self, provider):
        return [c["page_id"] for c in sorted(self.ranking_diagnostic()["candidates"],
                key=lambda c: c[provider]["rank"])[:5]]

    def set_rankings(self, jev_order, luna_order):
        jev_scores = {f"q{i}": 1 - jev_order.index(i) / len(jev_order) for i in range(len(jev_order))}
        luna_scores = {f"q{i}": 1 - luna_order.index(i) / len(luna_order) for i in range(len(luna_order))}
        self.judge.side_effect = None
        self.judge.return_value = {"model": "jev-latest", "answers":
            {key: {"type": "noul", "noul": value} for key, value in jev_scores.items()}}
        self.decisions.side_effect = None
        self.decisions.return_value = self.decision_result(luna_scores)

    def test_top_three_union_orders_shared_first_then_alternates_without_refill(self):
        cases = [
            ([0, 1, 2, 3, 4, 5], [0, 1, 2, 3, 4, 5], [0, 1, 2]),
            ([0, 1, 2, 3, 4, 5], [1, 3, 4, 0, 2, 5], [1, 0, 3, 2, 4]),
            ([0, 1, 2, 3, 4, 5], [3, 0, 4, 1, 2, 5], [0, 1, 3, 2, 4]),
            ([0, 1, 2, 3, 4, 5], [2, 1, 0, 3, 4, 5], [0, 1, 2]),
            ([0, 1, 2, 3, 4, 5], [3, 4, 5, 0, 1, 2], [0, 3, 1, 4, 2, 5]),
        ]
        for jev, luna, expected in cases:
            with self.subTest(jev=jev, luna=luna):
                self.set_rankings(jev, luna)
                self.rerank()
                ids = [f"guide-{i}" for i in expected]
                self.assertEqual([p["page_id"] for p in self.selected], ids)
                self.assertEqual(self.selection_diagnostic(), {
                    "version": 7, "strategy": "top_three_union",
                    "provider_outcomes": {"jev": "success", "luna": "success"}, "selection": ids})
                self.assertTrue(self.luna_diagnostic()["applied"])

    def test_fewer_candidates_and_ties_keep_original_hybrid_order(self):
        for count in (1, 2, 3):
            with self.subTest(count=count):
                pages = self.pages[:count]
                self.set_rankings(list(range(count)), list(range(count)))
                self.rerank(list(pages))
                self.assertEqual(self.selected, pages)
        self.judge.return_value = {"model": "jev-latest", "answers":
            {key: {"type": "noul", "noul": .5} for key in self.scores}}
        self.decisions.return_value = self.decision_result({key: .5 for key in self.scores})
        self.rerank()
        self.assertEqual(self.selected, self.pages[:3])

    def test_successful_provider_alone_and_hybrid_fallback_select_five(self):
        cases = [
            ("success", "provider_disabled", "jev", [5, 4, 3, 2, 1]),
            ("provider_disabled", "success", "luna", [0, 1, 2, 3, 4]),
            ("provider_disabled", "provider_disabled", "hybrid", [0, 1, 2, 3, 4]),
            ("invalid_response", "refusal", "hybrid", [0, 1, 2, 3, 4]),
        ]
        for jev, luna, strategy, expected in cases:
            with self.subTest(jev=jev, luna=luna):
                self.set_rankings([5, 4, 3, 2, 1, 0], list(range(6)))
                if jev == "provider_disabled":
                    self.judge.side_effect = client.HostInferenceError("disabled", reason="provider_disabled")
                elif jev == "invalid_response":
                    self.judge.return_value = {"model": "jev-latest", "answers": {}}
                if luna == "provider_disabled":
                    self.decisions.side_effect = client.HostInferenceError("disabled", reason="provider_disabled")
                elif luna == "refusal":
                    self.decisions.return_value["answers"][0] = {"type": "refusal", "name": "q0"}
                self.rerank()
                diagnostic = self.selection_diagnostic()
                self.assertEqual(diagnostic["strategy"], strategy)
                self.assertEqual(diagnostic["provider_outcomes"], {"jev": jev, "luna": luna})
                self.assertEqual(self.selected, [self.pages[i] for i in expected])

    def test_page_revalidation_cannot_refill_beyond_the_chosen_set(self):
        self.set_rankings(list(range(6)), list(range(6)))
        original = {p["page_id"]: p for p in self.pages}
        for missing in (False, True):
            with self.subTest(missing=missing):
                def load(page_id):
                    if page_id == "thread-1":
                        return {"page_id": page_id, "description": "Self", "revision": 1, "content": "Notes"}
                    if page_id == "guide-1":
                        if missing:
                            raise memory.WorkspaceError(404, "deleted")
                        return {**original[page_id], "revision": 99}
                    return original[page_id]
                with patch.object(memory, "load_page", side_effect=load) as read, \
                     patch.object(memory, "_search_pages", return_value={"pages": list(original.values())}), \
                     patch.object(memory.host_errors, "report_warning"):
                    result = memory.recall_pages({"thread_id": "thread-1", "message": "Fix login"})
                expected = ["thread-1", "guide-0"] if missing else ["thread-1", "guide-0", "guide-2"]
                self.assertEqual([p["page_id"] for p in result["pages"]], expected)
                self.assertTrue(all(c.args[0] in {"thread-1", "guide-0", "guide-1", "guide-2"}
                                    for c in read.call_args_list))
                self.assertIn('"selection":["guide-0","guide-1","guide-2"]', result["diagnostics"])
                self.assertNotIn("Selected guide-1", result["diagnostics"])

    def test_worst_bounded_trace_survives_admission_notice_and_history(self):
        self.pages = [{"page_id": f"g-{i}-" + "x" * (61 - len(str(i))),
                       "description": "d" * 100, "revision": 2147483647, "content": "Notes"}
                      for i in range(20)]
        self.set_rankings(list(range(20)), list(reversed(range(20))))
        self.judge.return_value["model"] = "j" * 128
        self.decisions.return_value["model"] = "l" * 128
        rows = [(p['page_id'], p['description'], p['content'], p['revision'],
                 None, 'agent', 'now', 'now') for p in self.pages]
        self_page = {"page_id": "thread-1", "scope": "self", "description": "Self",
                     "revision": 1, "content": "Notes"}
        def load(page_id):
            return self_page if page_id == "thread-1" else next(p for p in self.pages if p["page_id"] == page_id)
        with ExitStack() as stack:
            for name, value in (("_memory_search_generation", "fixed"),
                                ("_search_pages_exact", rows),
                                ("_search_pages_lexical", [r + (.5,) for r in rows]),
                                ("_lexical_page_id_tail", []),
                                ("_search_pages_semantic", [r + (.8,) for r in rows]),
                                ("_current_page_rows", rows)):
                stack.enter_context(patch.object(memory, name, return_value=value))
            stack.enter_context(patch.object(memory.embedding_client, "embed_texts", return_value=[[1.0]]))
            stack.enter_context(patch.object(memory, "load_page", side_effect=load))
            result = memory.recall_pages({"thread_id": "thread-1", "message": '"' * MAX_QUERY_BYTES})
        with patch.object(threads.workspace_proxy, "recall_memory", return_value=result):
            admitted, details = threads._recalled_memory_pages("thread-1", "Fix login")
        self.assertEqual(len(admitted), 7)
        self.assertLess(len(result["diagnostics"]), 12000)
        self.assertIn("Task query:", details)
        self.assertIn("Candidate 20:", details)
        diagnostic = json.loads(next(d.removeprefix("Rerank: ") for d in result["diagnostics"].split("\n\n")
                                     if d.startswith("Rerank: ")))
        self.assertEqual(diagnostic["selection"], [self.pages[i]["page_id"] for i in (0, 19, 1, 18, 2, 17)])
        self.assertEqual(len(diagnostic["candidates"]), 20)
        self.assertEqual(diagnostic["providers"]["jev"]["response_model"], "j" * 128)
        self.assertEqual(diagnostic["providers"]["luna"]["response_model"], "l" * 128)
        for candidate in diagnostic["candidates"]:
            self.assertEqual(candidate["revision"], 2147483647)
            self.assertEqual(len(candidate["page_id"]), 64)
            self.assertIsNotNone(candidate["jev"]["score"])
            self.assertIsNotNone(candidate["luna"]["score"])
        for page in admitted:
            self.assertIn(f"Selected {page['page_id']} r{page['revision']}", details)
        notice = threads.message_templates.memory_notice(admitted, details)
        self.assertEqual(notice["memory_page_ids"], [p["page_id"] for p in admitted])
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)
        event_state.append_agent_event(cursor, "thread.notice", "thread-1", notice)
        stored = tuple(v.value if isinstance(v, memory.pgclient.Jsonb) else v
                       for v in cursor.execute.call_args.args[1])
        event = event_state._event_dict((1, *stored))
        self.assertEqual(event["payload"]["memory_recall_details"], details)
        projected = conversation_history._conversation_event(event, True)
        self.assertFalse(projected["truncated"])
        self.assertEqual(projected["memory_recall_details"], details)
        self.assertLessEqual(len(json.dumps(projected["memory_recall_details"], ensure_ascii=False).encode()), 14000)
        event["payload"]["memory_recall_details"] = details * 10
        self.assertTrue(conversation_history._conversation_event(event, True)["truncated"])

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

    @contextmanager
    def inference_boundary(self, configs, outcomes):
        # Exercise real config resolution, socket/client/service, adapters and recall;
        # only the database rows, decryption and HTTP transport are test doubles.
        @contextmanager
        def transaction():
            cursor = MagicMock()
            def execute(sql, params):
                config = configs[params[0]]
                cursor.fetchone.return_value = (
                    None if config in {"disabled", "missing_row"} else
                    (None, {}) if config == "unconfigured" else
                    ("" if config == "empty_key" else "test-key", {}))
            cursor.execute.side_effect = execute
            yield cursor

        def post(url, **kwargs):
            provider = "jev" if url == typesafe.ENDPOINT else "luna"
            outcome = outcomes[provider]
            self.assertEqual(kwargs["timeout"], 1.2)
            if outcome == "timeout":
                raise TimeoutError("PRIVATE TRANSPORT DETAIL")
            if outcome == "unavailable":
                raise OSError("PRIVATE TRANSPORT DETAIL")
            if outcome == "malformed":
                return b'{"model":"invalid","answers":[]}'
            if provider == "jev":
                response = self.judge.return_value
            else:
                response = self.decision_result({key: 1 - value for key, value in self.scores.items()})
                if outcome == "refusal":
                    response["answers"][0] = {"name": "q0", "type": "refusal"}
            return json.dumps(response).encode()

        with tempfile.TemporaryDirectory() as directory:
            socket_path = directory + "/inference.sock"
            server = api.HostInferenceServer(socket_path, frozenset({os.getuid()}))
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
            worker.start()
            try:
                with (patch.object(memory, "judge", client.typesafe_jev_judgment),
                      patch.object(memory, "openai_decisions", client.openai_decisions),
                      patch.object(client, "SOCKET_PATH", socket_path),
                      patch.object(provider_state.db, "transaction", side_effect=transaction),
                      patch.object(provider_state.secretbox, "decrypt", side_effect=lambda value: value),
                      patch.object(provider_http, "post", side_effect=post) as transport,
                      patch.object(providers.usage, "record_typesafe_response"),
                      patch.object(providers.usage, "record_openai_decision_response"),
                      patch.object(memory.host_errors, "report_warning") as warnings):
                    yield transport, warnings
            finally:
                server.shutdown()
                server.server_close()
                worker.join()

    def test_provider_configuration_matrix_across_real_inference_boundary(self):
        configurations = ("enabled", "disabled", "unconfigured", "missing_row", "empty_key")
        for jev_config, luna_config in product(configurations, repeat=2):
            with self.subTest(jev=jev_config, luna=luna_config):
                configs = {"typesafe": jev_config, "openai": luna_config}
                with self.inference_boundary(configs, {"jev": "success", "luna": "success"}) as (post, warnings):
                    self.rerank()
                expected_outcomes = {name: "success" if config == "enabled" else
                    "provider_unavailable" if config == "empty_key" else "provider_disabled"
                    for name, config in (("jev", jev_config), ("luna", luna_config))}
                diagnostic = self.selection_diagnostic()
                self.assertEqual(diagnostic["provider_outcomes"], expected_outcomes)
                self.assertEqual(post.call_count, sum(config == "enabled" for config in configs.values()))
                if "empty_key" not in configs.values():
                    warnings.assert_not_called()
                expected = ([5, 0, 4, 1, 3, 2] if jev_config == luna_config == "enabled" else
                            [5, 4, 3, 2, 1] if jev_config == "enabled" else [0, 1, 2, 3, 4])
                self.assertEqual(self.selected, [self.pages[i] for i in expected])

    def test_enabled_provider_failure_matrix_across_real_inference_boundary(self):
        cases = [(failure, "success") for failure in ("timeout", "unavailable", "malformed")]
        cases += [("success", failure) for failure in ("timeout", "unavailable", "malformed", "refusal")]
        cases += [("timeout", "timeout"), ("malformed", "refusal"), ("unavailable", "unavailable")]
        for jev, luna in cases:
            with self.subTest(jev=jev, luna=luna):
                with self.inference_boundary({"typesafe": "enabled", "openai": "enabled"},
                                             {"jev": jev, "luna": luna}) as (post, warnings):
                    self.rerank()
                diagnostic = self.selection_diagnostic()
                # Invalid wire responses are rejected and logged by the concrete adapter;
                # the existing socket exposes only "no usable result" to recall.
                expected_outcomes = {name: "provider_unavailable" if outcome in {"malformed", "unavailable"}
                                     else outcome for name, outcome in (("jev", jev), ("luna", luna))}
                self.assertEqual(diagnostic["provider_outcomes"], expected_outcomes)
                expected = [5, 4, 3, 2, 1] if jev == "success" else [0, 1, 2, 3, 4]
                self.assertEqual(self.selected, [self.pages[i] for i in expected])
                post.assert_called()
                self.assertEqual(post.call_count, 2)
                trace = json.dumps(self.ranking_diagnostic())
                self.assertNotIn("PRIVATE", trace)
                self.assertNotIn("test-key", trace)
                for name, outcome in (("jev", jev), ("luna", luna)):
                    provider = self.ranking_diagnostic()["providers"][name]
                    self.assertEqual(provider["applied"], outcome == "success")
                    self.assertEqual(provider["fallback"], outcome != "success")
                    if outcome == "timeout":
                        self.assertEqual(provider["error_type"], "TimeoutError")
                    if outcome != "success":
                        self.assertTrue(all(c["score"] is None for c in self.candidate_scores(name)))
                    if outcome == "malformed":
                        self.assertTrue(any(call.args[0].startswith("host_inference.") and
                                            type(call.args[1]).__name__ in {"JudgmentResponseError", "DecisionResponseError"}
                                            for call in warnings.call_args_list))

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
                self.assertEqual(self.ranking_diagnostic()["version"], 7)
                self.assertEqual(result["outcome"], "success" if enabled else "provider_disabled")
                self.assertEqual(result["fallback"], not enabled)
                if not enabled:
                    self.assertTrue(all(c["score"] is None for c in self.candidate_scores("jev")))
                expected = list(reversed(self.pages)) if enabled else self.pages
                self.assertEqual(pages, expected[:5])
                self.assertEqual(self.proposed_selection("jev"), [p["page_id"] for p in expected[:5]])
                self.assertEqual(self.ranking_diagnostic()["candidates"][0]["hybrid_rank"], 1)
                self.assertEqual(self.candidate_scores("jev")[0]["rank"], 6 if enabled else 1)
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
                self.assertEqual([p["page_id"] for p in self.selected], [f"guide-{i}" for i in range(5)])
                self.assertTrue(all(c["score"] is None for c in self.candidate_scores("jev")))
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
        self.assertEqual(self.proposed_selection("jev"), [f"guide-{i}" for i in range(5)])

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
        self.assertEqual(self.proposed_selection("jev"), [f"guide-{i}" for i in range(5)])
        self.assertTrue(all(c["score"] is None for c in self.candidate_scores("jev")))
        self.assertNotIn("private network detail", json.dumps(result))
        self.assertIn("elapsed_ms", result)

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
        self.assertEqual(self.luna_diagnostic()["outcome"], "success")
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

    def test_recall_carries_six_selected_pages_plus_self_through_admission(self):
        self.decisions.side_effect = None
        self.decisions.return_value = self.decision_result({key: 1 - value for key, value in self.scores.items()})
        def load(page_id):
            return {"page_id": "thread-1", "description": "Self guidance", "revision": 1, "content": "Self"} if page_id == "thread-1" else next(p for p in self.pages if p["page_id"] == page_id)
        with patch.object(memory, "load_page", side_effect=load), \
             patch.object(memory, "_search_pages", return_value={"pages": self.pages}):
            result = memory.recall_pages({"thread_id": "thread-1", "message": "Fix login"})
        self.assertEqual([p["page_id"] for p in result["pages"]], ["thread-1", "guide-5", "guide-0", "guide-4", "guide-1", "guide-3", "guide-2"])
        self.assertIn("Selected guide-5 r6", result["diagnostics"])
        luna = json.loads(next(d.removeprefix("Rerank: ") for d in result["diagnostics"].split("\n\n") if d.startswith("Rerank: ")))["providers"]["luna"]
        self.assertEqual(luna["outcome"], "success")
        with patch.object(threads.workspace_proxy, "recall_memory", return_value=result):
            admitted, diagnostics = threads._recalled_memory_pages("thread-1", "Fix login")
        self.assertEqual([p["page_id"] for p in admitted], [p["page_id"] for p in result["pages"]])
        context = memory_context_message("thread-1", admitted)
        self.assertIn('"page_id": "guide-2"', context)
        self.assertIn('"page_id": "thread-1"', context)
        self.assertNotIn("Rerank Luna", context)
        self.assertIn("Rerank:", diagnostics)


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
