"""Bounded scheduling, shared recall context, stale-run fences and advisory delivery."""
from contextlib import contextmanager
import threading
import unittest
from unittest.mock import Mock, patch

from host.runtime import memory_monitor as mm
from host.runtime.agent_runtime import orchestrator as orch


def page(name='pr-guidance', revision=1):
    return dict(page_id=name, revision=revision, description='When publishing a PR', scope='swarm')


class MemoryMonitorTests(unittest.TestCase):
    def setUp(self):
        self.clock = self.enterContext(patch.object(mm.time, 'monotonic', return_value=0.0))
        self.monitor = mm.Monitor()
        self.key = ('thread-1', 1)
        self.deliver = Mock(return_value=True)
        self.deliver_self = Mock(return_value=True)
        self.monitor.seed(self.key, 'User: Change arrows', [page('ui')], self.deliver, self.deliver_self)
        self.self_read = self.enterContext(patch.object(mm.workspace_proxy, 'self_memory', return_value=None))
        self.query = self.enterContext(patch.object(mm.memory_context, 'load_query',
            return_value='User: Change arrows\n\nAssistant: Preparing a PR'))
        self.model = self.enterContext(patch.object(mm.swarm_annotations, 'task_title', return_value='Publish arrows'))
        self.search = self.enterContext(patch.object(mm.workspace_proxy, 'recall_memory', return_value={
            'pages': [page('ui'), page(), {**page('self'), 'scope': 'self'}]}))

    def ready(self, key=None):
        self.activity(mm.SUGGESTION_EVENTS, key)
        self.clock.return_value += mm.SUGGESTION_INTERVAL

    def activity(self, count, key=None):
        for _ in range(count):
            self.monitor.observe(key or self.key, dict(kind='tool', phase='completed'))

    def redirect(self):
        self.monitor.observe(self.key, 'Investigate only', incoming=True)

    def test_shared_context_goes_directly_to_search_without_luna(self):
        self.ready()
        self.monitor.check_once()
        self.query.assert_called_once_with('thread-1')
        self.search.assert_called_once_with('thread-1', self.query.return_value)
        self.model.assert_not_called()
        self.assertEqual(self.deliver.call_args.args[0], [('pr-guidance', 1, 'When publishing a PR')])
        self.assertIn('pr-guidance', self.monitor.turns[self.key].known)

    def test_six_relevant_pages_plus_self_keep_midturn_limit_and_dedupe(self):
        self.search.return_value = {'pages': [{**page('self'), 'scope': 'self'},
            *[page(f'guide-{i}') for i in range(6)]]}
        self.ready()
        self.monitor.check_once()
        self.assertEqual([p[0] for p in self.deliver.call_args.args[0]],
                         ['guide-0', 'guide-1', 'guide-2'])
        self.redirect()
        self.ready()
        self.monitor.next_check = 0
        self.monitor.check_once()
        self.assertEqual([p[0] for p in self.deliver.call_args.args[0]],
                         ['guide-3', 'guide-4', 'guide-5'])
        self.assertNotIn('self', self.monitor.turns[self.key].known)

    def test_two_minute_interval_is_not_bypassed_by_events_or_incoming_direction(self):
        for _ in range(30):
            self.monitor.observe(self.key, 'completed')
        self.redirect()
        self.clock.return_value = 119
        self.assertFalse(self.monitor.check_once())
        self.query.assert_not_called()
        self.clock.return_value = 120
        self.assertTrue(self.monitor.check_once())
        self.redirect()
        self.activity(9)
        self.clock.return_value = 239
        self.assertFalse(self.monitor.check_once())
        self.assertEqual(self.search.call_count, 1)
        self.clock.return_value = 240
        self.assertTrue(self.monitor.check_once())

    def test_oldest_due_turn_and_global_rate_bound(self):
        second = ('thread-2', 1)
        self.clock.return_value = 1
        self.monitor.seed(second, 'Second', [], self.deliver, self.deliver_self)
        self.activity(10)
        self.activity(10, second)
        self.clock.return_value = 121
        self.monitor.check_once()
        self.assertEqual(self.search.call_args.args[0], 'thread-1')
        self.clock.return_value = 130
        self.assertFalse(self.monitor.check_once())
        self.clock.return_value = 131
        self.monitor.check_once()
        self.assertEqual(self.search.call_args.args[0], 'thread-2')

    def test_shared_check_requires_ten_new_events_even_after_timer_expires(self):
        self.activity(9)
        self.clock.return_value = 120
        self.assertFalse(self.monitor.check_once())
        self.monitor.observe(self.key, 'Assistant progress')
        self.assertTrue(self.monitor.check_once())
        self.clock.return_value = 240
        self.assertFalse(self.monitor.check_once())
        self.activity(9)
        self.assertFalse(self.monitor.check_once())
        self.activity(1)
        self.assertTrue(self.monitor.check_once())
        # The unchanged-query skip consumes this batch without another search.
        self.search.assert_called_once()
        self.assertEqual(self.monitor.turns[self.key].suggestion_events, 0)
        self.assertEqual(self.monitor.turns[self.key].self_events, 20)

    def test_self_threshold_and_counter_reset_are_independent_of_shared_checks(self):
        self.self_read.return_value = {**page('thread-1'), 'content': 'Notes', 'scope': 'self'}
        self.ready()
        self.monitor.check_once()
        self.activity(14)
        self.clock.return_value = 3600
        self.assertTrue(self.monitor.check_once())
        self.self_read.assert_not_called()
        self.assertEqual(self.monitor.turns[self.key].self_events, 24)
        self.activity(1)
        self.clock.return_value = 3610
        self.assertTrue(self.monitor.check_once())
        self.deliver_self.assert_called_once()
        self.assertEqual(self.query.call_count, 2)
        self.assertEqual(self.monitor.turns[self.key].self_events, 0)
        self.assertEqual(self.monitor.turns[self.key].suggestion_events, 1)
        self.clock.return_value = 7300
        self.assertFalse(self.monitor.check_once())

    def test_self_event_threshold_does_not_bypass_one_hour(self):
        self.activity(30)
        self.clock.return_value = 3599
        self.assertTrue(self.monitor.check_once())  # Shared check only.
        self.self_read.assert_not_called()
        self.assertEqual(self.monitor.turns[self.key].self_events, 25)
        self.clock.return_value = 3609  # The global ten-second limit also applies.
        self.assertTrue(self.monitor.check_once())
        self.self_read.assert_called_once()

    def test_old_timer_without_enough_events_cannot_block_an_eligible_turn(self):
        second = ('thread-2', 1)
        self.clock.return_value = 1
        self.monitor.seed(second, 'Second', [], self.deliver, self.deliver_self)
        self.activity(10, second)
        self.clock.return_value = 600
        self.assertTrue(self.monitor.check_once())
        self.search.assert_called_once_with('thread-2', self.query.return_value)
        self.self_read.assert_not_called()

    def test_only_incoming_messages_and_selected_notices_invalidate_titles(self):
        for kind in ('reasoning', 'wait', 'status'):
            self.monitor.observe(self.key, dict(kind=kind, phase='completed', output='log'))
        for kind in ('memory_injection', 'memory_suggestion', 'agent_message_sent', 'restart', 'automated'):
            self.monitor.observe(self.key, dict(event_type='thread.notice', payload={
                'source': 'user', 'message': 'ignore', 'notice': {'kind': kind, 'summary': 'ignore'}}), incoming=True)
        self.assertEqual(self.monitor.turns[self.key].generation, 0)
        self.monitor.observe(self.key, dict(kind='command', phase='started'))
        self.monitor.observe(self.key, dict(kind='command', phase='completed', append_output=True))
        self.assertEqual(self.monitor.turns[self.key].generation, 0)
        self.assertEqual(self.monitor.turns[self.key].suggestion_events, 0)
        self.monitor.observe(self.key, dict(kind='command', phase='completed', output='PRIVATE TOOL LOG'))
        self.assertEqual(self.monitor.turns[self.key].generation, 0)
        self.assertEqual(self.monitor.turns[self.key].suggestion_events, 1)
        self.monitor.observe(self.key, dict(event_type='thread.notice', payload={
            'source': 'user', 'message': 'Publish changes',
            'notice': {'kind': 'agent_message', 'summary': 'From peer'}}), incoming=True)
        self.assertEqual(self.monitor.turns[self.key].next_suggestion, 120)
        self.assertEqual(self.monitor.turns[self.key].generation, 1)
        self.assertEqual(self.monitor.turns[self.key].self_events, 2)

    def test_two_suggestion_notices_per_operator_input_then_checks_continue_silently(self):
        self.monitor.turns[self.key].update_task = Mock()
        for index in range(4):
            self.search.return_value = {'pages': [page(f'guide-{index}')]}
            self.query.return_value = f'User: Change arrows\n\nAssistant: Step {index}'
            self.ready()
            self.monitor.next_check = 0
            with patch.object(mm.state, 'swarm_task_context', return_value={'task_title': 'Edit arrows'}):
                self.monitor.check_once()
        self.assertEqual(self.deliver.call_count, 2)
        self.assertEqual([c.args[0][0][0] for c in self.deliver.call_args_list], ['guide-0', 'guide-1'])
        self.assertEqual(self.search.call_count, 4)  # Recall and titles keep running.
        self.assertEqual(self.monitor.turns[self.key].update_task.call_count, 4)
        self.assertNotIn('guide-3', self.monitor.turns[self.key].known)
        self.redirect()  # New operator input restores the allowance.
        self.search.return_value = {'pages': [page('guide-4')]}
        self.ready()
        self.monitor.next_check = 0
        with patch.object(mm.state, 'swarm_task_context', return_value=None):
            self.monitor.check_once()
        self.assertEqual(self.deliver.call_count, 3)
        self.assertIn('guide-4', self.monitor.turns[self.key].known)

    def test_capacity_drops_new_turns_and_finish_releases_slot(self):
        with patch.object(mm, 'MAX_TURNS', 1):
            self.monitor.seed(('thread-2', 1), 'new', [], self.deliver, self.deliver_self)
            self.assertEqual(len(self.monitor.turns), 1)
            self.assertEqual(self.monitor.stats['capacity_skips'], 1)
            self.monitor.finish(self.key)
            self.monitor.seed(('thread-2', 1), 'new', [], self.deliver, self.deliver_self)
            self.assertIn(('thread-2', 1), self.monitor.turns)

    def test_incoming_direction_during_search_does_not_block_or_advance_timer(self):
        entered, release = threading.Event(), threading.Event()
        def search(*args):
            entered.set()
            self.assertTrue(release.wait(2))
            return {'pages': []}
        self.search.side_effect = search
        self.ready()
        worker = threading.Thread(target=self.monitor.check_once)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.redirect()
            self.assertEqual(self.monitor.turns[self.key].generation, 1)
            self.assertEqual(self.monitor.turns[self.key].next_suggestion, 240)
            self.assertEqual(self.monitor.turns[self.key].suggestion_events, 1)
            self.assertEqual(self.monitor.turns[self.key].self_events, 11)
        finally:
            release.set()
            worker.join(2)
        self.assertFalse(worker.is_alive())

    def test_incoming_direction_during_lookup_allows_suggestions_but_not_stale_titles(self):
        for stage in (self.query, self.search):
            with self.subTest(stage=stage):
                self.monitor.turns[self.key].last_query = ''
                self.monitor.turns[self.key].known = {}
                self.monitor.turns[self.key].update_task = Mock()
                self.monitor.next_check = 0
                self.deliver.reset_mock()
                def lookup(*args):
                    self.redirect()
                    return stage.return_value
                stage.side_effect = lookup
                self.ready()
                self.monitor.check_once()
                stage.side_effect = None
                self.deliver.assert_called_once_with([('ui', 1, 'When publishing a PR'),
                                                     ('pr-guidance', 1, 'When publishing a PR')])
                self.model.assert_not_called()
                self.monitor.turns[self.key].update_task.assert_not_called()
                self.assertEqual(self.monitor.turns[self.key].next_suggestion, self.clock.return_value + 120)

    def test_finished_turn_drops_late_result_and_cannot_affect_new_run(self):
        def search(*args):
            self.monitor.finish(self.key)
            self.monitor.seed(('thread-1', 2), 'New run', [], self.deliver, self.deliver_self)
            return {'pages': [page()]}
        self.search.side_effect = search
        self.ready()
        self.monitor.check_once()
        self.deliver.assert_not_called()
        self.assertEqual(self.monitor.turns[('thread-1', 2)].known, {})

    def test_operator_redirect_after_selection_still_delivers_suggestion(self):
        turn = orch._Turn('codex', 'thread-1', 'gpt-6.1-sol', 'low', 1, retry_attempt=0)
        turn.phase = orch.ExecutionPhase.RUNNING
        turn.server = Mock()
        def deliver(pages):
            self.redirect()
            return orch._deliver_memory_suggestion(turn, pages)
        self.deliver.side_effect = deliver
        self.ready()
        with (patch.object(orch.state, 'mutation'),
              patch.object(orch.state, 'append_agent_event') as append):
            self.monitor.check_once()
        turn.server.steer.assert_called_once()
        append.assert_called_once()
        self.assertIn('pr-guidance', self.monitor.turns[self.key].known)

    def test_unchanged_context_skips_search_and_titles(self):
        self.query.return_value = 'User: Change arrows'
        self.ready()
        self.monitor.check_once()
        self.search.assert_not_called()
        self.model.assert_not_called()

    def test_failure_waits_for_next_interval_without_retry(self):
        self.search.side_effect = OSError('busy')
        self.ready()
        with patch.object(mm.host_errors, 'report_warning'):
            self.monitor.check_once()
        self.monitor.next_check = 0
        self.assertFalse(self.monitor.check_once())
        self.assertEqual(self.monitor.stats['failures'], 1)
        self.search.side_effect = None
        self.ready()
        self.monitor.check_once()
        self.assertEqual(self.search.call_count, 2)
        self.deliver.assert_called_once()
        self.ready()
        self.monitor.check_once()
        self.assertEqual(self.search.call_count, 2)

    def test_failed_delivery_does_not_mark_page_known_and_revisions_can_be_suggested(self):
        self.deliver.return_value = False
        self.ready()
        self.monitor.check_once()
        self.assertNotIn('pr-guidance', self.monitor.turns[self.key].known)
        self.deliver.return_value = True
        self.search.return_value = {'pages': [page('ui', revision=2)]}
        self.query.return_value += '\n\nAssistant: Validate review'
        self.monitor.next_check = 0
        self.ready()
        self.redirect()
        self.monitor.check_once()
        self.assertEqual(self.monitor.turns[self.key].known['ui'], 2)

    def test_busy_suggestion_delivery_retries_unchanged_query_at_next_interval_only(self):
        self.deliver.return_value = False
        self.ready()
        self.monitor.check_once()
        self.assertFalse(self.monitor.check_once())
        self.deliver.assert_called_once()
        self.assertNotIn('pr-guidance', self.monitor.turns[self.key].known)
        self.deliver.return_value = True
        self.ready()
        self.monitor.check_once()
        self.assertEqual(self.deliver.call_count, 2)
        self.assertIn('pr-guidance', self.monitor.turns[self.key].known)
        self.ready()
        self.monitor.check_once()
        self.assertEqual(self.deliver.call_count, 2)

    def test_only_on_demand_titles_call_luna_with_the_same_query(self):
        for task in (None, {'task_title': 'Edit arrows'}):
            update = Mock()
            self.monitor.turns[self.key].update_task = update
            self.monitor.turns[self.key].last_query = ''
            self.monitor.next_check = 0
            self.redirect()
            self.ready()
            with patch.object(mm.state, 'swarm_task_context', return_value=task):
                self.monitor.check_once()
            if task is None:
                self.model.assert_not_called()
                update.assert_not_called()
            else:
                self.model.assert_called_once_with(self.query.return_value)
                self.assertEqual(update.call_args.args[0], 'Publish arrows')
                self.assertTrue(update.call_args.args[1]())
        self.assertEqual(self.search.call_count, 2)

    def test_disabled_luna_does_not_disable_recall(self):
        self.monitor.turns[self.key].update_task = Mock()
        self.model.side_effect = mm.client.HostInferenceError('disabled')
        self.ready()
        with patch.object(mm.state, 'swarm_task_context', return_value={'task_title': None}):
            self.monitor.check_once()
        self.deliver.assert_called_once()
        self.monitor.turns[self.key].update_task.assert_not_called()

    def test_title_update_respects_incoming_generation_at_save_time(self):
        turn = orch._Turn('codex', 'thread-1', 'gpt-6.1-sol', 'low', 1, retry_attempt=0)
        turn.phase = orch.ExecutionPhase.RUNNING
        def update(title, still_current):
            self.redirect()
            orch._update_task_title(turn, title, still_current)
        self.monitor.turns[self.key].update_task = update
        self.ready()
        with (patch.object(mm.state, 'swarm_task_context', return_value={'task_title': 'Edit arrows'}),
              patch.object(orch.state, 'save_swarm_task') as save):
            self.monitor.check_once()
        save.assert_not_called()

    def test_self_memory_is_read_every_hour_with_unchanged_query_and_revision(self):
        self.query.return_value = 'User: Change arrows'
        current = {**page('thread-1'), 'scope': 'self', 'content': 'Complete notes. ' * 1000}
        self.self_read.return_value = current
        for check in range(1, 61):
            self.ready()
            self.monitor.check_once()
            self.assertEqual(self.deliver_self.call_count, check // 30)
        self.search.assert_not_called()
        self.assertEqual(self.self_read.call_count, 2)
        self.assertEqual(self.deliver_self.call_args.args[0], current)

    def test_missing_or_failed_self_read_does_not_block_shared_suggestions(self):
        for result in (None, OSError('unavailable')):
            with self.subTest(result=result):
                self.clock.return_value += 3600
                self.activity(25)
                self.monitor.turns[self.key].known = {}
                self.monitor.turns[self.key].last_query = ''
                self.self_read.side_effect = result
                self.deliver.reset_mock()
                with patch.object(mm.host_errors, 'report_warning'):
                    self.monitor.check_once()
                self.deliver_self.assert_not_called()
                self.deliver.assert_called_once()

    def test_self_refresh_survives_shared_context_failure(self):
        self.activity(25)
        self.clock.return_value = 3600
        self.self_read.return_value = {**page('thread-1'), 'content': 'Current notes', 'scope': 'self'}
        self.query.side_effect = OSError('context unavailable')
        with patch.object(mm.host_errors, 'report_warning'):
            self.monitor.check_once()
        self.deliver_self.assert_called_once_with(self.self_read.return_value)

    def test_self_refresh_reads_latest_page_without_catchup_or_busy_retry(self):
        self.activity(25)
        self.clock.return_value = 3600
        self.self_read.return_value = {**page('thread-1', 2), 'content': 'Updated notes', 'scope': 'self'}
        self.deliver_self.return_value = False
        self.monitor.check_once()
        self.deliver_self.assert_called_once_with(self.self_read.return_value)
        self.clock.return_value = 3610
        self.assertFalse(self.monitor.check_once())
        self.assertEqual(self.monitor.turns[self.key].next_self, 7200)

    def test_finished_self_read_cannot_deliver_to_replacement_run(self):
        def read(_):
            self.monitor.finish(self.key)
            self.monitor.seed(('thread-1', 2), 'New run', [], self.deliver, self.deliver_self)
            return {**page('thread-1'), 'content': 'Old notes', 'scope': 'self'}
        self.self_read.side_effect = read
        self.activity(25)
        self.clock.return_value = 3600
        self.monitor.check_once()
        self.deliver_self.assert_not_called()
        self.assertEqual(self.monitor.turns[('thread-1', 2)].next_self, 7200)

    def test_worker_waits_for_events_then_timer_and_sleeps_when_idle(self):
        class StopWorker(Exception):
            pass
        self.monitor.wake = Mock()
        self.monitor.wake.wait.side_effect = StopWorker
        self.clock.return_value = 1800
        with self.assertRaises(StopWorker):
            self.monitor.run()
        self.monitor.wake.wait.assert_called_once_with(None)
        self.clock.return_value = 0
        self.activity(10)
        self.monitor.wake.set.assert_called()
        self.monitor.wake.wait.reset_mock()
        with self.assertRaises(StopWorker):
            self.monitor.run()
        self.monitor.wake.wait.assert_called_once_with(120)
        self.monitor.finish(self.key)
        self.monitor.wake.wait.reset_mock()
        with self.assertRaises(StopWorker):
            self.monitor.run()
        self.monitor.wake.wait.assert_called_once_with(None)


class SuggestionDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.memory_delivery = mm.memory_context.MemoryDelivery()
        self.enterContext(patch.object(orch, 'memory_delivery', self.memory_delivery))
        self.turn = orch._Turn('codex', 'thread-1', 'gpt-6.1-sol', 'low', 1, retry_attempt=0)
        self.turn.phase = orch.ExecutionPhase.RUNNING
        self.turn.server = Mock()

    def test_context_event_is_recorded_only_after_live_delivery(self):
        expected = ('Memories that may help with your current work:\n\n'
                    '- page: PR requirements\n\n'
                    'Read these if relevant using GET /agent/memory/pages/{page_id}. '
                    'This is recalled context, not a new operator request. '
                    'Provenance: workspace_memory; instruction_authority: none.')
        @contextmanager
        def mutation():
            self.turn.server.steer.assert_called_once_with(expected, memory_suggestion=True)
            yield 'cursor'
        with (patch.object(orch.state, 'mutation', mutation),
              patch.object(orch.state, 'append_agent_event') as append):
            self.assertTrue(orch._deliver_memory_suggestion(self.turn, [('page', 1, 'PR requirements')]))
        self.assertEqual(append.call_args.args[1], 'thread.notice')
        self.assertNotIn('source', append.call_args.args[3])
        self.assertEqual(append.call_args.args[3]['memory_recall_details'], expected)
        self.assertEqual(append.call_args.args[3]['memory_page_ids'], ['page'])
        self.assertEqual(append.call_args.kwargs['run_number'], 1)

    def test_finished_and_busy_turns_are_never_steered(self):
        self.turn.phase = orch.ExecutionPhase.FINISHING
        self.assertFalse(orch._deliver_memory_suggestion(self.turn, []))
        self.turn.server.steer.assert_not_called()
        self.turn.phase = orch.ExecutionPhase.RUNNING
        self.turn.delivery_lock = threading.Lock()
        with self.turn.delivery_lock:
            self.assertFalse(orch._deliver_memory_suggestion(self.turn, []))
        self.turn.server.steer.assert_not_called()

    def test_memory_tag_is_passed_to_all_steerable_runtimes(self):
        for runtime in orch.INTERACTIVE_RUNTIMES:
            if not orch.harness_adapter(runtime).steerable:
                continue
            with self.subTest(runtime=runtime):
                self.turn.runtime_type = runtime
                self.turn.server.steer.reset_mock()
                with (patch.object(orch.state, 'mutation'),
                      patch.object(orch.state, 'append_agent_event')):
                    self.assertTrue(orch._deliver_memory_suggestion(self.turn, [('page', 1, 'Context')]))
                self.assertEqual(self.turn.server.steer.call_args.kwargs, {'memory_suggestion': True})

    def test_provider_completion_race_is_a_drop(self):
        self.turn.server.steer.side_effect = orch.ProviderTurnFinishing('finished')
        with patch.object(orch.state, 'mutation') as mutate:
            self.assertFalse(orch._deliver_memory_suggestion(self.turn, []))
        mutate.assert_not_called()

    def test_full_self_refresh_is_optional_context_in_all_steerable_runtimes(self):
        import json
        notes = 'Remember current constraints. ' * 600
        current = {**page('thread-1'), 'content': notes, 'scope': 'self', 'selection': 'self'}
        for runtime in orch.INTERACTIVE_RUNTIMES:
            if not orch.harness_adapter(runtime).steerable:
                continue
            with self.subTest(runtime=runtime):
                self.memory_delivery.clear(self.turn.thread_id)
                self.turn.runtime_type = runtime
                with (patch.object(orch.state, 'mutation'),
                      patch.object(orch.state, 'append_agent_event') as append):
                    self.assertTrue(orch._deliver_self_memory(self.turn, current))
                call = self.turn.server.steer.call_args
                self.assertEqual(call.kwargs, {'memory_suggestion': True})
                self.assertIn('not a new operator request', call.args[0])
                context = json.loads(call.args[0].split('--- KERN HOST CONTEXT ---\n')[1].split('\n--- END')[0])
                self.assertEqual(context['memories'][0]['content'], notes)
                notice = append.call_args.args[3]
                self.assertEqual(notice['notice']['kind'], 'memory_injection')
                self.assertEqual(notice['memory_page_ids'], ['thread-1'])
        self.turn.phase = orch.ExecutionPhase.FINISHING
        self.assertFalse(orch._deliver_self_memory(self.turn, current))

    def test_title_saves_under_turn_lock_and_finished_turns_are_skipped(self):
        def current():
            self.assertTrue(self.turn.delivery_lock.locked())
            return True
        with patch.object(orch.state, 'save_swarm_task') as save:
            orch._update_task_title(self.turn, 'Release planning', current)
            save.assert_called_once_with('thread-1', 1, 'Release planning')
            self.turn.phase = orch.ExecutionPhase.FINISHING
            orch._update_task_title(self.turn, 'Late title', current)
            self.assertEqual(save.call_count, 1)
