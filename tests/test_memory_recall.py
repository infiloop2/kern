"""Task-focused recall and its diagnostic evidence."""
from contextlib import ExitStack
import json
import unittest
from unittest.mock import patch

from host.runtime.admin_api import conversation_history, threads
from host.runtime.workspace import memory


def row(page_id):
    return (page_id, page_id, 'Guidance', 1, None, 'agent', 'now', 'now')


class MemoryRecallDiagnosticsTests(unittest.TestCase):
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
        with patch.object(threads.workspace_proxy, 'recall_memory', return_value={'pages': [], 'diagnostics': 'Current query: test'}) as recall:
            pages, details = threads._recalled_memory_pages('thread-1', 'test')
        recall.assert_called_once_with('thread-1', 'test')
        self.assertIn('Current query: test', details)
        self.assertNotIn('Current query: test', threads._memory_context_message('thread-1', pages))

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

    def test_history_bounds_diagnostics_and_preserves_old_notices(self):
        event = {'event_id': 'event_1', 'timestamp': 'now', 'event_type': 'thread.context_added',
                 'payload': {'message': 'Self identity and 0 memories injected.', 'memory_page_ids': []}}
        self.assertNotIn('memory_recall_details', conversation_history._conversation_event(event))
        event['payload']['memory_recall_details'] = '😀\\"' * 10000
        result = conversation_history._conversation_event(event)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(json.dumps(result['memory_recall_details'], ensure_ascii=False).encode()), 14000)


class TaskRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.judge_patch = patch.object(memory, "judge", return_value=None)
        self.judge_patch.start()
        self.addCleanup(self.judge_patch.stop)

    def test_vague_followup_uses_user_task_but_thanks_does_not_search(self):
        history = [
            {"event_type": "thread.message", "payload": {"source": "user", "message": "Fix token usage analytics"}},
            {"event_type": "thread.message", "payload": {"source": "agent", "message": "Unrelated outreach"}},
            {"event_type": "thread.message", "payload": {"source": "user", "message": "thanks"}},
        ]
        with patch.object(threads.state, "page_thread_events", return_value=history) as read:
            self.assertEqual(threads._memory_task_query("thread-1", "?"), "fix token usage analytics")
            self.assertEqual(threads._memory_task_query("thread-1", "can you do that"), "fix token usage analytics")
            self.assertEqual(threads._memory_task_query("thread-1", "ok well observe wit this thanks"), "observe wit fix token usage analytics")
            read.reset_mock()
            self.assertEqual(threads._memory_task_query("thread-1", "thanks!"), "")
            self.assertEqual(threads._memory_task_query("thread-1", "SnowBid"), "snowbid")
            read.assert_not_called()
            self.assertEqual(threads._memory_task_query("thread-1", "scheduled agents"), "scheduled agents")

    def test_continuation_does_not_cross_working_memory_clear(self):
        history = [
            {"event_type": "thread.message", "payload": {"source": "user", "message": "Old task"}},
            {"event_type": "thread.memory_cleared", "payload": {}},
        ]
        with patch.object(threads.state, "page_thread_events", return_value=history):
            self.assertEqual(threads._memory_task_query("thread-1", "continue"), "")

    def test_task_words_beat_incidental_body_matches_without_dropping_semantic_matches(self):
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
                         ["thread-1", "scheduled-agents-model", "kern-repo-dev-guidelines", "synonym-guide"])
        self.assertFalse(search.call_args.kwargs["include_graph"])
        popular.assert_not_called()

    def test_empty_task_query_keeps_self_without_search(self):
        page = {"page_id": "thread-1", "description": "Self", "content": "My notes", "revision": 1}
        with patch.object(memory, "load_page", return_value=page), \
             patch.object(memory, "_search_pages") as search, \
             patch.object(memory, "_popular_rows") as popular:
            result = memory.recall_pages({"thread_id": "thread-1", "message": "thanks"})
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
        with patch.object(threads.state, "page_thread_events", side_effect=OSError("unavailable")), \
             patch.object(threads, "_report_degraded_recall") as report, \
             patch.object(threads.workspace_proxy, "recall_memory", return_value={"pages": []}) as recall:
            threads._recalled_memory_pages("thread-1", "?")
        recall.assert_called_once_with("thread-1", "")
        report.assert_called_once()

    def test_automated_header_is_not_part_of_task_query(self):
        from host.agent_scripts import AUTOMATED_TRIGGER_PREFIX
        self.assertEqual(memory._recall_query(AUTOMATED_TRIGGER_PREFIX + "Review SnowBid inventory"),
                         "review snowbid inventory")

    def test_schedule_preamble_does_not_crowd_out_later_topic(self):
        from host.agent_scripts import AUTOMATED_TRIGGER_PREFIX
        message = ("Check UTC weekday FIRST. On Saturday or Sunday, skip all work and return "
                   "the weekend-skip outcome without reading or writing Apps.\n\n"
                   "Operate TraderHand CEO using canonical app instructions.")
        self.assertIn("traderhand", memory._recall_query(AUTOMATED_TRIGGER_PREFIX + message))

    def test_greetings_and_empty_messages_do_not_recall_an_old_task(self):
        with patch.object(threads.state, "page_thread_events") as read:
            for message in ("hello", "hi!", "HELLO", "thanks", "ok", "", "..."):
                with self.subTest(message=message):
                    self.assertEqual(threads._memory_task_query("thread-1", message), "")
            read.assert_not_called()

    def test_acronyms_and_language_names_survive_both_query_normalizations(self):
        from host.memory_recall import task_query
        for message in ("IT", "US", "OR", "CAN", "MAY", "Debug C++ templates", "Debug C# templates"):
            with self.subTest(message=message), patch.object(threads.state, "page_thread_events") as read:
                query = threads._memory_task_query("thread-1", message)
                self.assertTrue(query)
                self.assertEqual(task_query(query), query)
                self.assertEqual(memory._recall_query(query), query)
                read.assert_not_called()
        self.assertIn("C++", task_query("Debug C++ templates"))
        self.assertIn("C#", task_query("Debug C# templates"))
        self.assertNotEqual(task_query("Debug C++ templates"), task_query("Debug C# templates"))

    def test_automated_header_does_not_make_a_new_topic_refer_back(self):
        from host.agent_scripts import AUTOMATED_TRIGGER_PREFIX, LEGACY_AUTOMATED_TRIGGER_PREFIX
        with patch.object(threads.state, "page_thread_events") as read:
            for prefix in (AUTOMATED_TRIGGER_PREFIX, LEGACY_AUTOMATED_TRIGGER_PREFIX):
                self.assertEqual(threads._memory_task_query("schedule-1", prefix + "Audit invoices"),
                                 "audit invoices")
            read.assert_not_called()

    def test_followup_qualifiers_are_kept_before_the_previous_task(self):
        history = [{"event_type": "thread.message", "payload": {
            "source": "user", "message": "Fix token usage analytics",
        }}]
        with patch.object(threads.state, "page_thread_events", return_value=history):
            self.assertEqual(threads._memory_task_query("thread-1", "deploy it to production"),
                             "deploy production fix token usage analytics")
            self.assertEqual(threads._memory_task_query("thread-1", "do that in staging"),
                             "staging fix token usage analytics")
            self.assertEqual(threads._memory_task_query("thread-1", "please continue"),
                             "fix token usage analytics")

    def test_plural_and_capitalized_followups_keep_the_task(self):
        history = [{"event_type": "thread.message", "payload": {
            "source": "user", "message": "Fix token usage analytics",
        }}]
        with patch.object(threads.state, "page_thread_events", return_value=history):
            for message in ("DO THAT", "CAN YOU DEPLOY IT", "COULD YOU TEST IT", "do so", "please do so", "deploy it in staging tomorrow", "deploy them", "deploy those in staging", "fix these", "test those", "DO IT", "DEPLOY IT", "DO IT IN STAGING", "CAN YOU DO THAT", "THAT", "CONTINUE", "YES"):
                with self.subTest(message=message):
                    query = threads._memory_task_query("thread-1", message)
                    self.assertTrue({"fix", "token", "usage", "analytics"} <= set(query.casefold().split()))

    def test_chained_followups_keep_the_original_task_and_each_qualifier(self):
        history = [{"event_type": "thread.message", "payload": {
            "source": "user", "message": message,
        }} for message in ("Fix token usage analytics", "deploy it", "do that in staging")]
        with patch.object(threads.state, "page_thread_events", return_value=history):
            self.assertEqual(threads._memory_task_query("thread-1", "continue"),
                             "staging deploy fix token usage analytics")
        history.insert(1, {"event_type": "thread.memory_cleared", "payload": {}})
        with patch.object(threads.state, "page_thread_events", return_value=history):
            self.assertEqual(threads._memory_task_query("thread-1", "continue"), "staging deploy")

    def test_go_is_a_task_topic_except_in_a_bare_go_ahead(self):
        self.assertEqual(memory._recall_query("Debug Go concurrency"), "debug go concurrency")
        self.assertEqual(memory._recall_query("Migrate service from Python to Go"), "migrate service python go")
        history = [{"event_type": "thread.message", "payload": {
            "source": "user", "message": "Debug Go concurrency",
        }}]
        with patch.object(threads.state, "page_thread_events", return_value=history):
            self.assertEqual(threads._memory_task_query("thread-1", "go ahead"), "debug go concurrency")

    def test_title_cased_names_survive_repeated_normalization(self):
        from host.memory_recall import task_query
        for message, name in (("Review May sales report", "May"), ("Contact Will about invoices", "Will")):
            query = task_query(message)
            self.assertIn(name, query.split())
            self.assertEqual(task_query(query), query)

    def test_it_acronym_in_a_new_task_does_not_load_unrelated_history(self):
        with patch.object(threads.state, "page_thread_events") as read:
            self.assertEqual(threads._memory_task_query("thread-1", "Audit IT"), "audit IT")
            self.assertEqual(threads._memory_task_query("thread-1", "Fix IT systems"), "fix IT systems")
            self.assertEqual(threads._memory_task_query("thread-1", "AUDIT IT"), "AUDIT IT")
            self.assertEqual(threads._memory_task_query("thread-1", "FIX IT SYSTEMS"), "FIX IT SYSTEMS")
            read.assert_not_called()

    def test_repetition_and_filler_do_not_consume_query_budget(self):
        for preamble in ("browser " * 125, "please " * 200):
            self.assertIn("screenshot", memory._recall_query(preamble + "screenshot").split())

    def test_continuation_words_do_not_expand_a_fully_specified_task(self):
        with patch.object(threads.state, "page_thread_events") as read:
            for message in ("Continue investigating login failures", "Proceed with invoice audit", "Go ahead with login repair", "Proceed using Terraform to migrate billing", "Audit this quarter's invoices", "Review this month sales"):
                with self.subTest(message=message):
                    self.assertTrue(threads._memory_task_query("thread-1", message))
            read.assert_not_called()

    def test_sentence_initial_function_words_are_not_names(self):
        for message, expected in (("Can you fix authentication?", "fix authentication"),
                                  ("How do I deploy?", "deploy"),
                                  ("What broke billing?", "broke billing"),
                                  ("Will you fix authentication?", "fix authentication"),
                                  ("May I review billing?", "review billing")):
            self.assertEqual(memory._recall_query(message), expected)

    def test_qualified_continuations_retain_task_and_qualifier(self):
        history = [{"event_type": "thread.message", "payload": {
            "source": "user", "message": "Fix token usage analytics",
        }}]
        with patch.object(threads.state, "page_thread_events", return_value=history):
            self.assertEqual(threads._memory_task_query("thread-1", "continue in staging"), "staging fix token usage analytics")
            self.assertEqual(threads._memory_task_query("thread-1", "proceed in production"), "production fix token usage analytics")

    def test_apostrophes_do_not_create_fragment_terms(self):
        for message, expected in (("Don't deploy billing", "deploy billing"),
                                  ("Can't reproduce auth", "reproduce auth"),
                                  ("SnowBid's inventory", "snowbid inventory"),
                                  ("SnowBid’s inventory", "snowbid inventory")):
            self.assertEqual(memory._recall_query(message), expected)

    def test_query_budget_keeps_complete_terms(self):
        from host.memory_recall import task_query
        terms = [f"term{index:04d}suffix" for index in range(100)]
        query = task_query(" ".join(terms))
        self.assertLessEqual(len(query.encode()), 1000)
        self.assertEqual(query.split(), terms[:len(query.split())])
        self.assertEqual(task_query(query), query)

    def test_all_caps_requests_filter_grammar_but_preserve_it(self):
        self.assertEqual(memory._recall_query("CAN YOU FIX AUTHENTICATION"), "FIX AUTHENTICATION")
        self.assertEqual(memory._recall_query("CAN YOU FIX IT SYSTEMS"), "FIX IT SYSTEMS")

    def test_all_caps_can_bus_remains_distinct(self):
        self.assertEqual(memory._recall_query("DEBUG CAN BUS"), "DEBUG CAN BUS")

    def test_unicode_spelling_is_not_casefold_transliterated(self):
        self.assertEqual(memory._recall_query("Fix Straße routing"), "fix straße routing")
        query = memory._recall_query("Deploy İstanbul")
        self.assertEqual(query, "deploy İstanbul")
        self.assertEqual(memory._recall_query(query), query)

    def test_jev_adds_a_complete_score_set_for_all_candidates(self):
        candidates = [
            {
                "page_id": f"guide-{index}",
                "description": f"Guide {index}",
                "revision": 1,
                "content": f"private content {index}",
                "memory_relevance_score": 0.01 * (index + 1),
            }
            for index in range(6)
        ]
        probabilities = {
            "guide-0": 0.01,
            "guide-1": 0.20,
            "guide-2": 0.30,
            "guide-3": 0.40,
            "guide-4": 0.50,
            "guide-5": 0.60,
        }
        captured = {}

        def judge(state, questions, *, timeout_seconds):
            captured.update(
                state=state,
                questions=questions,
                timeout_seconds=timeout_seconds,
            )
            return {
                "model": "jev-latest",
                "answers": {
                    f"q{index}": {"type": "noul", "noul": probabilities[page["page_id"]]}
                    for index, page in enumerate(candidates)
                },
            }

        details = []
        with patch.object(memory, "judge", side_effect=judge):
            memory._add_jev_relevance_scores(
                candidates,
                query="repair authentication",
                details=details,
            )

        self.assertEqual(
            [page["jev_score"] for page in candidates],
            [0.01, 0.20, 0.30, 0.40, 0.50, 0.60],
        )
        self.assertEqual(captured["timeout_seconds"], 1.2)
        self.assertEqual(
            set(captured["state"]),
            {"task_query", "candidates"},
        )
        self.assertEqual(
            [candidate["id"] for candidate in captured["state"]["candidates"]],
            list(captured["questions"]),
        )
        self.assertNotIn("page_id", json.dumps(captured["state"]))
        self.assertNotIn("private content", json.dumps(captured["state"]))
        self.assertEqual(len(details), 7)
        self.assertEqual(details[0], "Jev response model: jev-latest.")
        self.assertIn("memory relevance 0.010000; Jev score 0.010", details[1])
        self.assertIn("memory relevance 0.060000; Jev score 0.600", details[-1])

    def test_jev_scores_select_top_five_and_invalid_results_use_local_order(self):
        candidates = [
            {
                "page_id": f"guide-{index}",
                "description": f"Guide {index}",
                "revision": 1,
                "memory_relevance_score": 0.01 * (6 - index),
            }
            for index in range(6)
        ]

        def load(page_id):
            if page_id == "thread-1":
                return {
                    "page_id": page_id,
                    "description": "Self",
                    "content": "Notes",
                    "revision": 1,
                }
            return {
                **next(page for page in candidates if page["page_id"] == page_id),
                "content": "Guidance",
            }

        answers = {
            f"q{index}": {"type": "noul", "noul": index / 10}
            for index in range(6)
        }
        with (
            patch.object(memory, "load_page", side_effect=load),
            patch.object(memory, "_search_pages", return_value={"pages": candidates}),
            patch.object(
                memory,
                "judge",
                return_value={"model": "jev-latest", "answers": answers},
            ),
        ):
            result = memory.recall_pages(
                {"thread_id": "thread-1", "message": "read guides"}
            )
        self.assertEqual(
            [page["page_id"] for page in result["pages"]],
            ["thread-1", "guide-5", "guide-4", "guide-3", "guide-2", "guide-1"],
        )
        self.assertIn("Jev selection: guide-5, guide-4, guide-3, guide-2, guide-1.", result["diagnostics"])

        fallback_candidates = [dict(page) for page in candidates]
        details = []
        with patch.object(memory, "judge", return_value={"answers": {}}):
            memory._add_jev_relevance_scores(
                fallback_candidates,
                query="read guides",
                details=details,
            )
        self.assertTrue(all("jev_score" not in page for page in fallback_candidates))
        self.assertEqual(
            details,
            ["Jev scores unavailable; existing recall order used."],
        )

    def test_metadata_reranking_retains_best_search_match_at_cutoff(self):
        candidates = [{"page_id": "login-guide", "description": "Login troubleshooting", "revision": 1}]
        candidates += [{"page_id": f"repair-{i}", "description": "Repair guide", "revision": 1} for i in range(5)]
        def load(page_id):
            if page_id == "thread-1":
                return {"page_id": page_id, "description": "Self", "content": "Notes", "revision": 1}
            return {**next(p for p in candidates if p["page_id"] == page_id), "content": "Guidance"}
        with patch.object(memory, "load_page", side_effect=load), patch.object(memory, "_search_pages", return_value={"pages": candidates}):
            result = memory.recall_pages({"thread_id": "thread-1", "message": "repair authentication"})
        ids = [p["page_id"] for p in result["pages"]]
        self.assertEqual(len(ids), 6)
        self.assertIn("login-guide", ids)
        self.assertEqual(ids[1], "repair-0")

    def test_all_caps_conjunction_keeps_websearch_operator(self):
        self.assertEqual(memory._recall_query("FIX LOGIN OR BILLING"), "FIX LOGIN OR BILLING")
        self.assertEqual(memory._recall_query("Debug OR operator"), 'debug "OR" operator')
