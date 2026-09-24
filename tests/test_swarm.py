"""Swarm lifecycle annotations and bounded persistence."""
from __future__ import annotations

import json
import threading
import unittest
from http import HTTPStatus
from unittest.mock import MagicMock, patch

import pg_harness
from host.runtime import swarm_annotations
from host.runtime.admin_api import service as admin_api
from host.runtime.admin_api import threads as admin_threads
from host.runtime.core import state
from host.runtime.core.state import swarm
from host.runtime.host_inference import client, typesafe

class SwarmAnnotationsTests(unittest.TestCase):
    def test_task_uses_prepared_context_and_task_contract(self) -> None:
        with (patch.object(client, 'openai_text_completion', return_value={'task': ' Review release '}) as model,
              patch.object(state, 'save_swarm_task') as save):
            swarm_annotations.generate_task(
                'thread-1', 3, 'Recalled context\nReview this release', 'Review this release',
            )
        self.assertIn('Recalled context', model.call_args.args[0])
        self.assertIn('CURRENT REQUEST\nReview this release', model.call_args.args[0])
        self.assertEqual(model.call_args.kwargs, {'purpose': 'swarm_task'})
        self.assertEqual(model.call_args.args[1]['required'], ['task'])
        self.assertEqual(model.call_args.args[1]['properties']['task']['maxLength'], 50)
        save.assert_called_once_with('thread-1', 3, 'Review release')

    def test_invalid_task_never_saves(self) -> None:
        for result in ({'task': ''}, {'task': 'x' * 51}, {'task': True}):
            with (patch.object(client, 'openai_text_completion', return_value=result),
                  patch.object(state, 'save_swarm_task') as save,
                  self.assertRaises(ValueError)):
                swarm_annotations.generate_task('app-1', 1, 'request', 'request')
            save.assert_not_called()

    def test_jev_classifies_final_reply_not_approval_count(self) -> None:
        context = {'run_status': 'idle', 'messages': [
            {'source': 'user', 'text': 'Deploy'},
            {'source': 'agent', 'text': 'Which account should I use?'},
        ]}
        for probability, expected in [(0.8, True), (0.2, False), (0.5, False)]:
            def judge(state_value, questions):
                def transport(method, url, **kwargs):
                    request = json.loads(kwargs['data'])
                    self.assertEqual(request['questions'], questions)
                    return json.dumps({
                        'model': 'jev-latest',
                        'answers': {'needs_human': {'type': 'noul', 'noul': probability}},
                    }).encode()

                return typesafe.judge(
                    api_key='test', model='jev-latest', state=state_value,
                    questions=questions, transport=transport,
                )

            with (patch.object(state, 'swarm_ai_context', return_value=context),
                  patch.object(client, 'typesafe_jev_judgment', side_effect=judge) as jev,
                  patch.object(state, 'save_swarm_needs_human') as save):
                swarm_annotations.assess_needs_human('thread-1', 3)
            save.assert_called_once_with('thread-1', 3, expected)
            self.assertIn('Which account', jev.call_args.args[0]['recent_turn'])
            self.assertEqual(jev.call_args.args[1]['needs_human']['type'], 'noul')

    def test_running_or_missing_final_reply_is_unassessed(self) -> None:
        for context in (None, {'run_status': 'running', 'messages': []},
                        {'run_status': 'idle', 'messages': [{'source': 'user', 'text': 'start'}]}):
            with (patch.object(state, 'swarm_ai_context', return_value=context),
                  patch.object(client, 'typesafe_jev_judgment') as jev):
                swarm_annotations.assess_needs_human('thread-1', 2)
            jev.assert_not_called()

    def test_background_failure_and_saturation_release_capacity(self) -> None:
        slots = threading.BoundedSemaphore(1)
        with (patch.object(swarm_annotations, '_SLOTS', slots),
              patch.object(swarm_annotations.threading, 'Thread') as thread):
            swarm_annotations.enqueue_task('thread-1', 1, 'hello', 'hello')
            swarm_annotations.enqueue_task('thread-2', 1, 'hello', 'hello')
            self.assertEqual(thread.call_count, 1)
            with patch.object(client, 'openai_text_completion', side_effect=client.HostInferenceError('unavailable')):
                thread.call_args.kwargs['target']()
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()
        with (patch.object(swarm_annotations, '_SLOTS', slots),
              patch.object(swarm_annotations.threading.Thread, 'start', side_effect=RuntimeError('full')),
              patch.object(swarm_annotations.host_errors, 'report_warning')):
            swarm_annotations.enqueue_task('thread-1', 1, 'hello', 'hello')
            self.assertTrue(slots.acquire(blocking=False))
            self.assertFalse(slots.acquire(blocking=False))
            slots.release()

    def test_context_byte_budget_preserves_latest_input(self) -> None:
        text = swarm_annotations._bounded('Summarize this document: ' + '🧪' * 20000 + ' newest request')
        self.assertLessEqual(len(text.encode()), 32 * 1024)
        self.assertTrue(text.startswith('Summarize this document: '))
        self.assertTrue(text.endswith(' newest request'))

    def test_long_handoff_cannot_hide_the_current_request(self) -> None:
        request = 'Review the release checklist first. ' + '🧪' * 15000
        handoff = 'Old handoff activity ' * 15000 + request
        with (patch.object(client, 'openai_text_completion', return_value={'task': 'Review release checklist'}) as model,
              patch.object(state, 'save_swarm_task')):
            swarm_annotations.generate_task('thread-1', 3, handoff, request)
        prompt = model.call_args.args[0]
        self.assertIn('CURRENT REQUEST\nReview the release checklist first.', prompt)
        self.assertLess(len(prompt.encode()), 34 * 1024)

    def test_swarm_route_is_operator_only(self) -> None:
        route = next(route for route in admin_api._ROUTES if route.path == '/v1/swarm')
        self.assertTrue(route.operator_only)
        self.assertEqual(route.query_keys, frozenset({'q'}))
        peer_route = next(route for route in admin_api._ROUTES if route.path == '/v1/swarm/peer-messages')
        self.assertTrue(peer_route.operator_only)
        self.assertEqual(peer_route.query_keys, frozenset())
        with self.assertRaises(admin_api.ApiError) as error:
            admin_api.route('GET', '/v1/swarm/peer-messages', {'since': ['yesterday']}, None,
                            principal=admin_api.OperatorPrincipal('session'))
        self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_only_workspace_principal_can_supply_peer_sender(self) -> None:
        body = {
            'message': 'Displayed peer wrapper and message',
            'peer_sender_thread_id': 'thread-2',
        }
        with (patch.object(admin_threads, 'send_thread_message') as send,
              self.assertRaises(admin_api.ApiError) as error):
            admin_api.route('POST', '/v1/threads/thread-1/messages', {}, body,
                            principal=admin_api.OperatorPrincipal('session'))
        self.assertEqual(error.exception.status, HTTPStatus.FORBIDDEN)
        send.assert_not_called()
        with patch.object(admin_threads, 'send_thread_message', return_value={'status': 'accepted'}) as send:
            admin_api.route('POST', '/v1/threads/thread-1/messages', {}, body,
                            principal=admin_api.WorkspacePrincipal())
        send.assert_called_once_with('thread-1', body, 'thread-2')


class SwarmPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        with state.mutation() as cur:
            state.save_thread_session(cur, 'codex', 'thread-1', None, state.utc_now(), 'gpt-5.6-terra', 'high')
            cur.execute("INSERT INTO chat_threads (thread_id, archived) VALUES ('thread-1', FALSE)")
            self.run = state.start_thread_run(cur, 'thread-1')
            state.reset_swarm_ai(cur, 'thread-1', self.run)

    def agent(self) -> dict:
        return next(agent for agent in state.swarm_snapshot()['agents'] if agent['thread_id'] == 'thread-1')

    def finish(self) -> None:
        with state.mutation() as cur:
            state.finish_thread_run(cur, 'thread-1', self.run)

    def test_idle_chat_stays_visible_and_old_results_cannot_cross_turns(self) -> None:
        state.save_swarm_task('thread-1', self.run, 'Review release')
        self.assertEqual(state.page_thread_summaries(None, 1)[0]['task'], 'Review release')
        self.finish()
        state.save_swarm_needs_human('thread-1', self.run, True)
        self.assertEqual((self.agent()['state'], self.agent()['needs_human']), ('idle', True))
        previous = self.run
        with state.mutation() as cur:
            self.run = state.start_thread_run(cur, 'thread-1')
            state.reset_swarm_ai(cur, 'thread-1', self.run)
        state.save_swarm_task('thread-1', previous, 'Late old title')
        state.save_swarm_needs_human('thread-1', previous, True)
        state.save_swarm_needs_human('thread-1', self.run, True)
        self.assertEqual((self.agent()['state'], self.agent()['task'], self.agent()['needs_human']), ('busy', None, None))
        self.assertIsNone(state.page_thread_summaries(None, 1)[0]['task'])
        self.finish()
        self.assertIsNone(self.agent()['needs_human'])

    def test_failed_state_matches_latest_event_badge_and_running_wins(self) -> None:
        self.finish()
        with state.mutation() as cur:
            state.append_agent_event(cur, 'thread.error', 'thread-1', {'error_message': 'failed'}, run_number=self.run)
        self.assertEqual(self.agent()['state'], 'failed')
        self.assertEqual(state.page_thread_summaries(None, 1)[0]['latest_event_type'], 'thread.error')
        with state.mutation() as cur:
            self.run = state.start_thread_run(cur, 'thread-1')
        self.assertEqual(self.agent()['state'], 'busy')
        with state.mutation() as cur:
            state.append_agent_event(cur, 'thread.message', 'thread-1', {'source': 'user', 'message': 'Try again'}, run_number=self.run)
        self.finish()
        self.assertEqual(self.agent()['state'], 'idle')
        self.assertEqual(state.page_thread_summaries(None, 1)[0]['latest_event_type'], 'thread.message')

    def test_chat_reservation_without_session_is_not_an_agent(self) -> None:
        with state.mutation() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id, archived) VALUES ('thread-2', FALSE)")
        ids = {agent['thread_id'] for agent in state.swarm_snapshot()['agents']}
        self.assertIn('thread-1', ids)
        self.assertNotIn('thread-2', ids)

    def test_snapshot_bounds_large_catalog_and_search_finds_older_agents(self) -> None:
        with state.mutation() as cur:
            for number in range(2, 261):
                thread_id = f'thread-{number}'
                state.save_thread_session(
                    cur, 'codex', thread_id, None, state.utc_now(), 'gpt-5.6-terra', 'high',
                )
                cur.execute(
                    'INSERT INTO chat_threads (thread_id, name, archived) VALUES (%s, %s, FALSE)',
                    (thread_id, f'Archive agent {number}'),
                )
        snapshot = state.swarm_snapshot()
        self.assertEqual(len(snapshot['agents']), 250)
        self.assertTrue(snapshot['has_more'])
        matches = state.swarm_snapshot('Archive agent 259')
        self.assertEqual([agent['thread_id'] for agent in matches['agents']], ['thread-259'])
        self.assertFalse(matches['has_more'])

    def test_memory_clear_removes_ai_and_late_results(self) -> None:
        self.finish()
        with state.mutation() as cur:
            state.append_agent_event(cur, 'thread.memory_cleared', 'thread-1', {}, run_number=self.run)
        state.save_swarm_task('thread-1', self.run, 'Late title')
        state.save_swarm_needs_human('thread-1', self.run, True)
        self.assertIsNone(self.agent()['task'])
        self.assertIsNone(self.agent()['needs_human'])

    def test_context_is_limited_to_current_turn(self) -> None:
        with state.mutation() as cur:
            for i in range(30):
                message = ("Please provide the deployment credential. " + 'x' * 3000 + ' Diagnostic tail'
                           if i == 29 else str(i) + 'x' * 3000)
                state.append_agent_event(cur, 'thread.message', 'thread-1', {'source': 'agent', 'message': message}, run_number=self.run)
        context = state.swarm_ai_context('thread-1', self.run)
        self.assertEqual(len(context['messages']), 24)
        self.assertTrue(all(len(item['text']) <= 2000 for item in context['messages']))
        self.assertTrue(context['messages'][-1]['text'].startswith('Please provide the deployment credential.'))
        self.assertTrue(context['messages'][-1]['text'].endswith(' Diagnostic tail'))
        self.assertIn('[Middle omitted.]', context['messages'][-1]['text'])
        self.assertIsNone(state.swarm_ai_context('thread-1', self.run + 1))

    def test_peer_deliveries_are_capped_at_50_without_text(self) -> None:
        with state.mutation() as cur:
            for seq in range(1, 206):
                swarm.record_swarm_peer_delivery(cur, seq, 'thread-2', 'thread-1')
            swarm.record_swarm_peer_delivery(cur, 100, 'thread-3', 'thread-1')
        deliveries = state.swarm_peer_messages()['messages']
        self.assertEqual(len(deliveries), 50)
        latest = deliveries[0]
        self.assertEqual(latest['seq'], 205)
        self.assertEqual(latest['sender_thread_id'], 'thread-2')
        self.assertEqual(set(latest), {'seq', 'sender_thread_id', 'target_thread_id', 'timestamp'})
        with state.mutation() as cur:
            cur.execute('SELECT COUNT(*), MIN(event_seq) FROM swarm_peer_deliveries')
            self.assertEqual(cur.fetchone(), (50, 156))

    def test_archived_chat_is_excluded(self) -> None:
        with state.mutation() as cur:
            cur.execute("UPDATE chat_threads SET archived = TRUE WHERE thread_id = 'thread-1'")
        self.assertFalse(any(agent['thread_id'] == 'thread-1' for agent in state.swarm_snapshot()['agents']))


if __name__ == '__main__':
    unittest.main()
