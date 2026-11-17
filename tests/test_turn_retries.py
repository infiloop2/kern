"""Backoff and delivery behavior without a clock wait or provider process."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from contextlib import contextmanager
from http import HTTPStatus
from threading import Event
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.admin_api import threads, workspace_api
from host.runtime.admin_api.errors import ApiError
from host.runtime.agent_runtime import orchestrator, turn_retries as retries
from host.runtime.workspace import schedules
from host.runtime.workspace.chat import backend as chat
from host.runtime.workspace.web_apps import backend as apps
from host.runtime.workspace.host_api import WorkspaceError


class TurnRetryTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        self.enterContext(patch.object(retries.state, 'thread_recovery', side_effect=lambda thread, cur=None:
            deepcopy(self.saved.get(thread, {'attempts': 0, 'pending': None}))))
        self.enterContext(patch.object(retries.state, 'save_thread_recovery', side_effect=lambda cur, thread, attempts, pending:
            self.saved.update({thread: {'attempts': attempts, 'pending': deepcopy(pending)}})))
        def cancel(cur, thread):
            value = self.saved.get(thread)
            existed = bool(value and value['pending'])
            if value:
                value['pending'] = None
            return existed
        self.enterContext(patch.object(retries.state, 'cancel_thread_recovery', side_effect=cancel))
        self.enterContext(patch.object(retries.state, 'pending_thread_recoveries', side_effect=lambda:
            [(thread, deepcopy(value['pending'])) for thread, value in self.saved.items() if value['pending']]))
        @contextmanager
        def mutation(**kwargs):
            before = deepcopy(self.saved)
            try:
                yield MagicMock()
            except Exception:
                self.saved = before
                raise
            for callback in kwargs.get('after_commit', []):
                callback()
        self.enterContext(patch.object(retries.state, 'mutation', side_effect=mutation))
        self.enterContext(patch.object(retries.state, 'append_agent_event'))
        self.enterContext(patch.object(orchestrator, 'runtime_status', return_value='active'))
        self.enterContext(patch.object(orchestrator, 'live_thread_ids', return_value=set()))
        self.clock = self.enterContext(patch.object(retries.time, 'time', return_value=1000))
        self.config = {'run_number': 4, 'status': 'idle', 'agent_runtime': 'codex',
                       'model': 'gpt-6.2-astra', 'effort': 'high'}
        self.enterContext(patch.object(threads.state, 'thread_session_config', side_effect=lambda *args: self.config))
        self.send = self.enterContext(patch.object(threads, 'send_thread_message'))
        self.report = self.enterContext(patch.object(threads.host_errors, 'report_warning'))

    def schedule(self, thread='thread-1', attempts=0, error='provider at capacity'):
        self.saved[thread] = {'attempts': attempts, 'pending': None}
        check = retries.record_outcome(MagicMock(), thread, 'codex', 'gpt-6.2-astra', 'high', 4, error)
        return check or retries.current(thread)

    def due(self, attempts):
        retry = self.schedule(attempts=attempts)
        self.clock.return_value += retries.DELAYS[attempts]
        return retry

    def test_five_attempts_follow_backoff_and_keep_notice_provenance(self):
        def fail_turn(thread_id, body, sender, operator_sent_message, retry_attempt, recovery):
            self.assertFalse(operator_sent_message)
            self.assertEqual(body['kern_notice']['kind'], 'retry')
            self.assertIn(f'automatic retry {retry_attempt}/5', body['message'])
            retries.claim(MagicMock(), recovery)
            retries.record_outcome(MagicMock(), thread_id, 'codex', 'gpt-6.2-astra', 'high', 4, 'still at capacity')

        self.send.side_effect = fail_turn
        self.schedule()
        for attempt, delay in enumerate((300, 1200, 3600, 14400, 43200), 1):
            self.clock.return_value += delay - 1
            retries.deliver_due(threads.retry_failed_turn)
            self.assertEqual(self.send.call_count, attempt - 1)
            self.clock.return_value += 1
            retries.deliver_due(threads.retry_failed_turn)
            self.assertEqual(self.send.call_count, attempt)
        self.assertIsNone(retries.pending('thread-1'))
        self.clock.return_value += 999999
        retries.deliver_due(threads.retry_failed_turn)
        self.assertEqual(self.send.call_count, 5)

    def test_failed_delivery_is_logged_without_a_separate_delivery_retry(self):
        self.send.side_effect = ApiError(HTTPStatus.CONFLICT, 'runtime temporarily unavailable')
        self.due(0)
        retries.deliver_due(threads.retry_failed_turn)
        self.send.assert_called_once()
        self.report.assert_called_once()
        self.assertIsNone(retries.pending('thread-1'))

    def test_usage_worker_start_failure_stops_without_pending_retry(self):
        check = self.schedule(error='limit reached')
        with patch.object(retries.threading, 'Thread') as worker:
            worker.return_value.start.side_effect = RuntimeError('thread limit reached')
            retries.start_limit_check(check)
        self.report.assert_called_once()
        self.assertIsNone(retries.pending('thread-1'))
        notice = retries.state.append_agent_event.call_args.args[3]['notice']
        self.assertEqual(notice['summary'], 'Automatic recovery stopped')
        self.assertIn('usage check could not start', notice['details'])

    def test_success_and_cancelled_or_replaced_attempts_do_not_repeat(self):
        retry = self.due(0)
        retries.cancel('thread-1')
        threads.retry_failed_turn(retry)
        self.send.assert_not_called()
        old = self.due(0)
        current = self.due(0)
        threads.retry_failed_turn(old)
        self.send.assert_not_called()
        self.send.side_effect = lambda *args, **kwargs: retries.claim(MagicMock(), kwargs['recovery'])
        threads.retry_failed_turn(current)
        threads.retry_failed_turn(current)
        self.send.assert_called_once()
        self.assertIsNone(retries.pending('thread-1'))

    def test_stop_cancels_pending_work_and_records_stop(self):
        retry = self.due(0)
        with patch.object(orchestrator, 'stop_thread_turn', return_value=False), \
                patch.object(threads.state, 'mutation'), patch.object(threads.state, 'append_agent_event') as event:
            self.assertEqual(threads.stop_thread('thread-1'), {'status': 'accepted'})
        self.assertEqual(event.call_args.args[1:3], ('thread.stopped', 'thread-1'))
        threads.retry_failed_turn(retry)
        self.send.assert_not_called()

    def test_archive_requires_explicit_cancel_and_restore_does_not_retry(self):
        retry = self.due(0)
        cursor = MagicMock()
        cursor.fetchone.side_effect = [(1,), (1,), ('thread-1', True), (1,), ('thread-1', False)]

        def admin_call(method, path, body=None):
            return workspace_api.route_workspace_request(method, path, {}, body)

        with patch.object(chat.db, 'transaction') as transaction, \
                patch.object(chat, 'call_admin_api', side_effect=admin_call), \
                patch.object(threads.state, 'latest_thread_event_seqs', return_value=(0, 0)), \
                patch.object(orchestrator, 'live_thread_ids', return_value=set()), \
                patch.object(orchestrator, 'stop_thread_turn', return_value=False), \
                patch.object(threads.state, 'mutation'), patch.object(threads.state, 'append_agent_event'):
            transaction.return_value.__enter__.return_value = cursor
            with self.assertRaisesRegex(WorkspaceError, 'cancel the automatic retry'):
                chat.archive_chat_thread('thread-1')
            self.assertIsNotNone(retries.pending('thread-1'))
            self.assertFalse(any(call.args[0].startswith('UPDATE') for call in cursor.execute.call_args_list))
            admin_call('POST', '/v1/threads/thread-1/stop')
            self.assertIsNone(retries.pending('thread-1'))
            chat.archive_chat_thread('thread-1')
            chat.unarchive_chat_thread('thread-1')
        threads.retry_failed_turn(retry)
        self.send.assert_not_called()

    def test_apps_and_schedules_refuse_pending_retries_before_writing(self):
        schedule_row = (1, 'Research', [], 'codex', 'gpt-6.2-astra', 'high', 1,
                        None, None, None, '', '', 'schedule-1', '')
        for backend, target, row, action in [
            (apps, 'app-1', (1,), lambda: apps.set_web_app_archived('app-1', True)),
            (schedules, 'schedule-1', schedule_row,
             lambda: schedules.delete_schedule(1, {'expected_revision': ['1']}, actor='user')),
        ]:
            with self.subTest(target=target), patch.object(backend.db, 'transaction') as transaction, \
                    patch.object(backend, 'call_admin_api') as admin:
                cursor = transaction.return_value.__enter__.return_value
                cursor.fetchone.return_value = row
                self.schedule(target)
                admin.return_value = {'thread': {'status': 'idle', 'retry': retries.pending(target)}}
                with self.assertRaisesRegex(WorkspaceError, 'cancel the automatic retry'):
                    action()
                self.assertIsNotNone(retries.pending(target))
                self.assertFalse(any(call.args[0].startswith('UPDATE') for call in cursor.execute.call_args_list))
                admin.assert_called_once_with('GET', f'/v1/threads/{target}')

    def test_status_and_cancel_wait_until_dispatched_retry_is_admitted(self):
        dispatching, release = Event(), Event()
        live = set()

        def admit(*args, **kwargs):
            # Exercise re-entry into the same lock used by ordinary sends.
            with threads._thread_send_lock('thread-1'):
                dispatching.set()
                self.assertTrue(release.wait(5))
                retries.claim(MagicMock(), kwargs['recovery'])
                live.add('thread-1')

        self.send.side_effect = admit
        for operation in ('status', 'stop'):
            with self.subTest(operation=operation), \
                    patch.object(threads.state, 'latest_thread_event_seqs', return_value=(0, 0)), \
                    patch.object(orchestrator, 'live_thread_ids', side_effect=lambda: live.copy()), \
                    patch.object(orchestrator, 'stop_thread_turn', return_value=True) as stop, \
                    ThreadPoolExecutor(max_workers=2) as pool:
                retry = self.due(0)
                dispatching.clear()
                release.clear()
                live.clear()
                delivery = pool.submit(threads.retry_failed_turn, retry)
                try:
                    self.assertTrue(dispatching.wait(5))
                    started = Event()
                    def inspect():
                        started.set()
                        return threads.get_thread('thread-1') if operation == 'status' else threads.stop_thread('thread-1')
                    result = pool.submit(inspect)
                    self.assertTrue(started.wait(5))
                    self.assertFalse(result.done())
                    stop.assert_not_called()
                finally:
                    release.set()
                delivery.result(timeout=5)
                response = result.result(timeout=5)
                self.assertEqual(response['status'], 'running' if operation == 'status' else 'accepted')

    def test_counter_survives_cancellation_unknown_errors_and_success_resets_it(self):
        self.schedule(attempts=2)
        retries.cancel('thread-1')
        self.assertEqual(self.saved['thread-1']['attempts'], 2)
        retries.record_outcome(MagicMock(), 'thread-1', 'codex', '', '', 4, 'authentication failed')
        self.assertIsNone(retries.pending('thread-1'))
        self.assertEqual(self.saved['thread-1']['attempts'], 2)
        retries.record_outcome(MagicMock(), 'thread-1', 'codex', '', '', 4, None)
        self.assertEqual(self.saved['thread-1'], {'attempts': 0, 'pending': None})

    def test_unknown_and_hermes_limit_errors_stop_but_timeout_retries(self):
        for runtime, error, expected in [('codex', 'failed', False), ('hermes', 'usage limit', False),
                                         ('script', 'timed out', False), ('hermes', 'timed out', True)]:
            with self.subTest(runtime=runtime, error=error):
                retries.record_outcome(MagicMock(), 'thread-1', runtime, '', '', 4, error)
                self.assertEqual(retries.pending('thread-1') is not None, expected)

    def test_limit_resolution_does_not_reset_counter_and_newer_work_wins(self):
        retry = self.schedule(attempts=2, error='usage LIMIT reached')
        self.assertIsNone(retries.pending('thread-1'))
        with patch.object(retries.recovery_policy, 'limit_recovery', return_value=('codex-2', 1000)):
            resolved = retries.resolve(retry)
        self.assertEqual(resolved.attempt, 3)
        retries.claim(MagicMock(), resolved)
        retries.record_outcome(MagicMock(), 'thread-1', 'codex-2', '', '', 5, 'timed out')
        self.assertEqual(retries.current('thread-1').due_at, 1000 + 14400)
        retry = self.schedule(error='limit')
        def change_during_probe(*args):
            self.config['run_number'] += 1
            return ('codex-2', 1000)
        with patch.object(retries.recovery_policy, 'limit_recovery', side_effect=change_during_probe):
            self.assertIsNone(retries.resolve(retry))
        self.assertIsNone(retries.pending('thread-1'))

    def test_unknown_usage_stops_without_delivery(self):
        for failure in (None, TimeoutError('usage timed out'), ValueError('invalid usage')):
            retry = self.schedule(error='limit')
            with patch.object(retries.recovery_policy, 'limit_recovery', return_value=None, side_effect=failure):
                self.assertIsNone(retries.resolve(retry))
            retries.deliver_due(self.send)
            self.send.assert_not_called()
            self.assertIsNone(retries.pending('thread-1'))

    def test_admission_rollback_restores_pending_recovery(self):
        retry = self.due(0)
        with self.assertRaises(RuntimeError), retries.state.mutation() as cur:
            retries.claim(cur, retry)
            raise RuntimeError('admission failed')
        self.assertEqual(retries.current('thread-1'), retry)
        self.assertEqual(self.saved['thread-1']['attempts'], 0)

    def test_loading_on_boot_does_not_consume_due_recovery(self):
        retry = self.due(0)
        with patch.object(orchestrator, 'runtime_status', return_value='loading'):
            retries.deliver_due(self.send)
        self.send.assert_not_called()
        self.assertEqual(retries.current('thread-1'), retry)

    def test_cleanup_error_does_not_schedule_a_retry(self):
        turn = orchestrator._Turn('codex', 'thread-1', '', '', 4,
                                  phase=orchestrator.ExecutionPhase.FINISHING, retry_attempt=0)
        server = MagicMock()
        server.close.side_effect = RuntimeError('cleanup failed')
        with patch.object(orchestrator.state, 'mutation'), patch.object(orchestrator.state, 'append_agent_event'):
            orchestrator._close_turn(turn, server)
        self.assertIsNone(retries.pending('thread-1'))


if __name__ == '__main__':
    unittest.main()
