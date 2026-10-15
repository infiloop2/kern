"""10-minute full-body injection heuristic; no provider-compaction claims."""
import json
import unittest
from unittest.mock import Mock, patch

from host.agent_messages import memory_context_message, memory_notice
from host.runtime import memory_context
from host.runtime.memory_context import MemoryDelivery
from host.runtime.agent_runtime import orchestrator as runtime


def page(name='guide', revision=1, scope='swarm'):
    return dict(page_id=name, revision=revision, scope=scope,
                description='Read before implementation', content='Useful guidance. ' * 80)


class MemoryDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.cache = MemoryDelivery()
        self.clock = self.enterContext(patch.object(memory_context.time, 'monotonic', return_value=0.0))
        self.enterContext(patch.object(runtime, 'memory_delivery', self.cache))

    def select(self, pages, thread='thread-1'):
        return self.cache.select(thread, pages)[0]

    def deliver(self, pages, thread='thread-1'):
        self.cache.delivered(thread, tuple((p['page_id'], p['revision']) for p in pages))

    def test_boundary_and_suppression_does_not_renew_for_self_or_shared(self):
        pages = [page('thread-1', scope='self'), page()]
        self.deliver(pages)
        for timestamp in (0, 120, 300, 599.999):
            self.clock.return_value = timestamp
            self.assertEqual(self.select(pages), [])
        self.clock.return_value = 600
        self.assertEqual(self.select(pages), pages)
        self.deliver(pages)
        self.clock.return_value = 1199.999
        self.assertEqual(self.select(pages), [])
        self.clock.return_value = 1200
        self.assertEqual(self.select(pages), pages)
        self.assertEqual(memory_context_message('thread-1', []), 'Kern immutable thread identity: {"thread_id": "thread-1"}')

    def test_changed_revisions_and_multiple_pages_keep_independent_times(self):
        self.deliver([page('thread-1', scope='self'), page('b')])
        self.clock.return_value = 300
        selected = self.select([page('thread-1', 2, 'self'), page('b'), page('c')])
        self.assertEqual(selected, [page('thread-1', 2, 'self'), page('c')])
        self.deliver(selected)
        self.clock.return_value = 600
        self.assertEqual(self.select([page('thread-1', 2, 'self'), page('b'), page('c')]), [page('b')])
        self.clock.return_value = 900
        self.assertEqual(self.select([page('thread-1', 2, 'self'), page('c')]), [page('thread-1', 2, 'self'), page('c')])
        # Deleted/unselected pages are never refilled from delivery history.
        self.assertEqual(self.select([]), [])

    def test_thread_isolation_clear_and_restart(self):
        pages = [page('thread-1', scope='self'), page()]
        self.deliver(pages)
        self.assertEqual(self.select(pages, thread='thread-2'), pages)
        self.deliver(pages, thread='thread-2')
        self.cache.clear('thread-1')
        self.assertEqual(self.select(pages), pages)
        self.assertEqual(self.select(pages, thread='thread-2'), [])
        self.cache = MemoryDelivery()  # Process restart/cache loss.
        self.assertEqual(self.select(pages, thread='thread-2'), pages)

    def test_preparation_without_delivery_never_suppresses(self):
        for timestamp in (0, 1, 600):
            self.clock.return_value = timestamp
            self.assertEqual(self.select([page()]), [page()])
        self.deliver([page()])
        self.assertEqual(self.select([page()]), [])

    def test_capacity_and_expiry_cleanup_store_no_bodies(self):
        for n in range(130):
            self.deliver([page()], thread=f'thread-{n}')
        self.assertEqual(len(self.cache._threads), 128)
        self.assertEqual(self.select([page()], thread='thread-0'), [page()])
        self.assertNotIn('Useful guidance', repr(self.cache._threads))
        for n in range(70):
            self.deliver([page(f'p-{n}')])
        known = self.cache._threads['thread-1']
        self.assertEqual(len(known), 64)
        self.clock.return_value = 600
        self.deliver([page('new')])
        self.assertEqual(self.cache._threads['thread-1'], {'new': (1, 600)})

    def test_readiness_records_without_session_id_and_later_callbacks_do_not_renew(self):
        turn = runtime._Turn('codex', 'thread-1', 'model', 'high', 1, 0)
        turn.memory_pages = (('guide', 1),)
        self.clock.return_value = 10
        self.assertTrue(runtime._provider_ready(turn))
        self.assertIsNone(turn.provider_session_id)
        self.assertEqual(self.select([page()]), [])
        self.clock.return_value = 300
        runtime._mark_finishing(turn)
        self.clock.return_value = 600
        runtime._publish_provider_session(turn, 'session')
        self.assertEqual(self.select([page()]), [])
        self.clock.return_value = 610
        self.assertEqual(self.select([page()]), [page()])

    def test_failed_readiness_does_not_record_and_session_replacement_does_not_clear(self):
        turn = runtime._Turn('codex', 'thread-1', 'model', 'high', 1, 0)
        turn.memory_pages = (('guide', 1),)
        runtime._mark_finishing(turn, 'session')
        self.assertFalse(runtime._provider_ready(turn))
        self.assertEqual(self.select([page()]), [page()])
        self.deliver([page()])
        replacement = runtime._Turn('claude_code', 'thread-1', 'model', 'high', 2, 0)
        runtime._publish_provider_session(replacement, 'replacement-session')
        self.assertTrue(runtime._provider_ready(replacement))
        self.assertEqual(self.select([page()]), [])

    def test_injection_notice_is_recorded_once_at_readiness_with_exact_message(self):
        for pages in ([page()], []):
            with self.subTest(pages=bool(pages)):
                turn = runtime._Turn('codex', 'thread-1', 'model', 'high', 3, 0)
                message = memory_context_message(turn.thread_id, pages)
                notice = memory_notice(pages, 'Selection diagnostics', message=message)
                turn.memory_notice = notice
                turn.memory_pages = tuple((p['page_id'], p['revision']) for p in pages)
                with patch.object(runtime.state, 'mutation') as mutation, \
                        patch.object(runtime.state, 'append_agent_event') as append:
                    # Learning a session id alone is not delivery.
                    runtime._publish_provider_session(turn, 'session')
                    append.assert_not_called()
                    self.assertTrue(runtime._provider_ready(turn))
                    self.assertFalse(runtime._provider_ready(turn))
                    append.assert_called_once_with(
                        mutation.return_value.__enter__.return_value,
                        'thread.notice', 'thread-1', notice, run_number=3,
                    )
                    self.assertEqual(notice['notice']['details'], message)
                    self.assertIn('injected.', notice['message'])
                    self.assertIsNone(turn.memory_notice)

    def test_startup_failure_or_stop_never_emits_injection_notice(self):
        for phase in (runtime.ExecutionPhase.FINISHING, runtime.ExecutionPhase.CLOSED):
            with self.subTest(phase=phase):
                turn = runtime._Turn('codex', 'thread-1', 'model', 'high', 1, 0)
                turn.memory_pages = (('guide', 1),)
                turn.memory_notice = memory_notice([page()], '', message='Exact message')
                turn.phase = phase
                with patch.object(runtime.state, 'mutation') as mutation:
                    self.assertFalse(runtime._provider_ready(turn))
                    mutation.assert_not_called()
                self.assertEqual(self.select([page()]), [page()])

    def test_receipt_commit_failure_does_not_mark_delivery_or_running(self):
        turn = runtime._Turn('codex', 'thread-1', 'model', 'high', 1, 0)
        turn.memory_pages = (('guide', 1),)
        turn.memory_notice = memory_notice([page()], '', message='Exact message')
        with patch.object(runtime.state, 'mutation') as mutation:
            mutation.return_value.__exit__.side_effect = RuntimeError('commit failed')
            with patch.object(runtime.state, 'append_agent_event'), self.assertRaises(RuntimeError):
                runtime._provider_ready(turn)
        self.assertEqual(turn.phase, runtime.ExecutionPhase.STARTING)
        self.assertIsNotNone(turn.memory_notice)
        self.assertEqual(self.select([page()]), [page()])

    def test_periodic_self_delivery_and_suggestions_share_no_body_timestamps(self):
        turn = runtime._Turn('codex', 'thread-1', 'model', 'high', 1, 0)
        turn.provider_session_id = 'session'
        turn.phase = runtime.ExecutionPhase.RUNNING
        turn.server = Mock()
        current = page('thread-1', scope='self')
        self.deliver([current, page()])
        with patch.object(runtime.state, 'mutation'), patch.object(runtime.state, 'append_agent_event') as append:
            for timestamp in (120, 300, 599.999):
                self.clock.return_value = timestamp
                self.assertFalse(runtime._deliver_self_memory(turn, current))
            turn.server.steer.assert_not_called()
            append.assert_not_called()
            self.assertTrue(runtime._deliver_memory_suggestion(turn, [('guide', 1, 'Guidance'), ('new', 1, 'New')]))
            self.assertEqual(self.select([page('new')]), [page('new')])
            self.clock.return_value = 600
            self.assertEqual(self.select([page()]), [page()])  # Suggestion did not renew body TTL.
            turn.server.steer.side_effect = runtime.ProviderTurnFinishing('busy')
            self.assertFalse(runtime._deliver_self_memory(turn, current))
            self.assertEqual(self.select([current]), [current])
            turn.server.steer.side_effect = None
            self.assertTrue(runtime._deliver_self_memory(turn, current))
            self.assertEqual(self.select([current]), [])
            self.clock.return_value = 601
            changed = page('thread-1', 2, 'self')
            self.assertTrue(runtime._deliver_self_memory(turn, changed))
            self.assertEqual(append.call_args.args[3]['memory_page_ids'], ['thread-1'])

    def test_preparation_diagnostics_and_context_formatting(self):
        pages = [page('a'), page('b'), page('c')]
        self.deliver(pages)
        self.clock.return_value = 599.999
        selected, details = self.cache.select('thread-1', [page('a'), page('b', 2), page('new')])
        diagnostic = json.loads(details.partition(': ')[2])
        self.assertEqual(diagnostic['delivery_status'], 'pending_readiness')
        self.assertEqual(diagnostic['ttl_seconds'], 600)
        self.assertEqual([diagnostic[key] for key in ('selected_count', 'prepared_injection_count', 'suppressed_count')], [3, 2, 1])
        self.assertEqual([p['reason'] for p in diagnostic['pages']], ['recent', 'changed', 'new'])
        self.assertEqual([p['decision'] for p in diagnostic['pages']], ['suppressed', 'prepared', 'prepared'])
        self.assertEqual([p['seconds_since_injection'] for p in diagnostic['pages']], [599.999, 599.999, None])
        self.assertNotIn('Useful guidance', details)
        message = memory_context_message('thread-1', selected)
        self.assertIn('workspace_memory', message)
        self.assertIn('"memories":', message)
        self.assertNotIn('pending_readiness', message)
        self.clock.return_value = 600
        _, details = self.cache.select('thread-1', [page('c')])
        self.assertEqual(json.loads(details.partition(': ')[2])['pages'][0]['reason'], 'expired')

    def test_timestamped_fixture_bytes(self):
        pages = [page('thread-1', scope='self')] + [page(f'guide-{n}') for n in range(6)]
        baseline = optimized = 0
        # Twelve admissions 150 seconds apart, spanning 27.5 minutes.
        for timestamp in range(0, 1800, 150):
            self.clock.return_value = timestamp
            selected = self.select(pages)
            baseline += len(memory_context_message('thread-1', pages).encode())
            optimized += len(memory_context_message('thread-1', selected).encode())
            self.deliver(selected)
        self.assertLess(optimized, baseline // 2)
        print(f'12 admissions over 27.5 minutes: {baseline} -> {optimized} injected UTF-8 bytes')
