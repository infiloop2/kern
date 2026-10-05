"""Backoff and delivery behavior without a clock wait or provider process."""
from http import HTTPStatus
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.admin_api import threads
from host.runtime.admin_api.errors import ApiError
from host.runtime.agent_runtime import orchestrator, turn_retries as retries


class TurnRetryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(retries, '_pending', {}))
        self.clock = self.enterContext(patch.object(retries.time, 'time', return_value=1000))
        self.config = {'run_number': 4, 'status': 'idle', 'agent_runtime': 'codex',
                       'model': 'gpt-6-astra', 'effort': 'high'}
        self.enterContext(patch.object(threads.state, 'thread_session_config', side_effect=lambda *args: self.config))
        self.send = self.enterContext(patch.object(threads, 'send_thread_message'))
        self.report = self.enterContext(patch.object(threads.host_errors, 'report_warning'))

    def due(self, attempts):
        retries.schedule('thread-1', attempts, 'provider unavailable')
        self.clock.return_value += retries.DELAYS[attempts]
        return retries._pending['thread-1']

    def test_five_attempts_follow_backoff_and_keep_notice_provenance(self):
        def fail_turn(thread_id, body, sender, operator_sent_message, retry_attempt):
            self.assertFalse(operator_sent_message)
            self.assertEqual(body['kern_notice']['kind'], 'retry')
            self.assertIn(f'automatic retry {retry_attempt}/5', body['message'])
            retries.schedule(thread_id, retry_attempt, 'still unavailable')

        self.send.side_effect = fail_turn
        retries.schedule('thread-1', 0, 'provider unavailable')
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

    def test_success_and_cancelled_or_replaced_attempts_do_not_repeat(self):
        retry = self.due(0)
        retries.cancel('thread-1')
        threads.retry_failed_turn(retry)
        self.send.assert_not_called()
        old = self.due(0)
        current = self.due(0)
        threads.retry_failed_turn(old)
        self.send.assert_not_called()
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

    def test_failed_completion_schedules_only_after_commit(self):
        for runtime, error, expected in [('codex', 'failed', True), ('codex', None, False), ('script', 'failed', False)]:
            with self.subTest(runtime=runtime, error=error), \
                    patch.object(orchestrator.state, 'touch_thread_session'), \
                    patch.object(orchestrator.state, 'finish_thread_run'), \
                    patch.object(orchestrator.state, 'append_agent_event'), \
                    patch.object(orchestrator.memory_monitor, 'finish'):
                retries.cancel('thread-1')
                turn = orchestrator._Turn(runtime, 'thread-1', '', '', 4, retry_attempt=2)
                callbacks = []
                orchestrator._record_turn_finished(MagicMock(), callbacks, turn, error_message=error)
                self.assertIsNone(retries.pending('thread-1'))
                for callback in callbacks:
                    callback()
                self.assertEqual(retries.pending('thread-1') is not None, expected)
                if expected:
                    self.assertEqual(retries.pending('thread-1')['attempt'], 3)

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
