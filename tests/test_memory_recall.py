"""Recall evidence must describe selection without changing it."""
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
        self.assertEqual(actual, baseline)
        embed.assert_called_once()
        counters.assert_not_called()
        trace = '\n'.join(details)
        self.assertIn('direct-guide r1 — exact rank 1, lexical rank 1, semantic rank 1 cosine 0.800', trace)
        self.assertIn('linked-guide r1 — graph rank 1', trace)

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
