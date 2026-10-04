"""Swarm identities, on-demand annotations, and weekly counters."""
from __future__ import annotations

import threading
import unittest
from http import HTTPStatus
from unittest.mock import MagicMock, patch

import pg_harness
from host.runtime import swarm_annotations
from host.runtime.admin_api import service as admin_api
from host.runtime.admin_api import threads as admin_threads
from host.runtime.core import db, state
from host.runtime.core.state import swarm
from host.runtime.host_inference import client

class SwarmAnnotationsTests(unittest.TestCase):
    def test_task_uses_shared_context_and_task_contract(self) -> None:
        with (patch.object(client, 'openai_text_completion', return_value={'task': ' Review release '}) as model,
              patch.object(state, 'save_swarm_task') as save):
            swarm_annotations.generate_task(
                'thread-1', 3, 'Review this release\n\nEarlier user request',
            )
        self.assertEqual(model.call_args.args[0].split('TASK CONTEXT\n', 1)[1],
                         'Review this release\n\nEarlier user request')
        self.assertEqual(model.call_args.kwargs, {
            'model': 'gpt-6-luna', 'reasoning_effort': 'none', 'max_output_tokens': 400,
            'instructions': 'Return a JSON object that matches the supplied schema.', 'timeout_seconds': 20.0,
        })
        self.assertEqual(model.call_args.args[1]['required'], ['task'])
        self.assertEqual(model.call_args.args[1]['properties']['task']['maxLength'], 70)
        self.assertIn('whole words and a natural ending', model.call_args.args[0])
        self.assertIn('never cut off a word', model.call_args.args[0])
        save.assert_called_once_with('thread-1', 3, 'Review release', only_if_empty=True)

    def test_invalid_task_never_saves(self) -> None:
        for result in ({'task': ''}, {'task': 'x' * 71}, {'task': True}):
            with (patch.object(client, 'openai_text_completion', return_value=result),
                  patch.object(state, 'save_swarm_task') as save,
                  self.assertRaises(ValueError)):
                swarm_annotations.generate_task('app-1', 1, 'request')
            save.assert_not_called()

    def test_background_failure_and_saturation_release_capacity(self) -> None:
        slots = threading.BoundedSemaphore(1)
        with (patch.object(state, 'is_on_demand_agent', return_value=True),
              patch.object(swarm_annotations, '_SLOTS', slots),
              patch.object(swarm_annotations.threading, 'Thread') as thread):
            swarm_annotations.enqueue_task('thread-1', 1, 'hello')
            swarm_annotations.enqueue_task('thread-2', 1, 'hello')
            self.assertEqual(thread.call_count, 1)
            with patch.object(client, 'openai_text_completion', side_effect=client.HostInferenceError('unavailable')):
                thread.call_args.kwargs['target']()
            self.assertTrue(slots.acquire(blocking=False))
            slots.release()
        with (patch.object(state, 'is_on_demand_agent', return_value=True),
              patch.object(swarm_annotations, '_SLOTS', slots),
              patch.object(swarm_annotations.threading.Thread, 'start', side_effect=RuntimeError('full')),
              patch.object(swarm_annotations.host_errors, 'report_warning')):
            swarm_annotations.enqueue_task('thread-1', 1, 'hello')
            self.assertTrue(slots.acquire(blocking=False))
            self.assertFalse(slots.acquire(blocking=False))
            slots.release()

    def test_persistent_agents_do_not_generate_task_titles(self) -> None:
        with patch.object(swarm_annotations, '_enqueue') as enqueue:
            swarm_annotations.enqueue_task('app-1', 1, 'request')
            swarm_annotations.enqueue_task('schedule-1', 1, 'request')
        enqueue.assert_not_called()
        with (patch.object(swarm_annotations, '_enqueue', side_effect=lambda job: job()),
              patch.object(state, 'is_on_demand_agent', return_value=False),
              patch.object(client, 'openai_text_completion') as model):
            swarm_annotations.enqueue_task('thread-2', 1, 'delegated task')
        model.assert_not_called()

    def test_swarm_route_is_operator_only(self) -> None:
        route = next(route for route in admin_api._ROUTES if route.path == '/v1/swarm')
        self.assertTrue(route.operator_only)
        self.assertEqual(route.query_keys, frozenset({'q'}))
        peer_route = next(route for route in admin_api._ROUTES if route.path == '/v1/swarm/interactions')
        self.assertTrue(peer_route.operator_only)
        self.assertEqual(peer_route.query_keys, frozenset())
        with self.assertRaises(admin_api.ApiError) as error:
            admin_api.route('GET', '/v1/swarm/interactions', {'since': ['yesterday']}, None,
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
        send.assert_called_once_with('thread-1', body, 'thread-2', operator_sent_message=False)


    def test_operator_provenance_is_explicit_and_automated_sends_are_excluded(self) -> None:
        cases = [
            (admin_api.OperatorPrincipal('session'), {'message': 'hello'}, True),
            (admin_api.WorkspacePrincipal(), {'message': 'hello', 'operator_sent_message': True}, True),
            (admin_api.WorkspacePrincipal(), {'message': 'scheduled wake-up'}, False),
            (admin_api.WorkspacePrincipal(), {'message': 'peer', 'peer_sender_thread_id': 'app-2', 'operator_sent_message': True}, True),
        ]
        for principal, body, expected in cases:
            with self.subTest(body=body), patch.object(admin_threads, 'send_thread_message') as send:
                admin_api.route('POST', '/v1/threads/thread-1/messages', {}, body, principal=principal)
                self.assertEqual(send.call_args.kwargs['operator_sent_message'], expected)


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
        self.assertEqual((self.agent()['state'], self.agent()['pending_approval_count']), ('idle', 0))
        previous = self.run
        with state.mutation() as cur:
            self.run = state.start_thread_run(cur, 'thread-1')
            state.reset_swarm_ai(cur, 'thread-1', self.run)
        state.save_swarm_task('thread-1', previous, 'Late old title')
        self.assertEqual((self.agent()['state'], self.agent()['task'], self.agent()['pending_approval_count']), ('busy', None, 0))
        self.assertIsNone(state.page_thread_summaries(None, 1)[0]['task'])
        self.finish()
        self.assertEqual(self.agent()['pending_approval_count'], 0)

    def test_updated_title_wins_over_late_initial_result(self) -> None:
        self.assertEqual(state.swarm_task_context('thread-1', self.run), {'task_title': None})
        state.save_swarm_task('thread-1', self.run, 'Publish release')
        state.save_swarm_task('thread-1', self.run, 'Edit arrows', only_if_empty=True)
        self.assertEqual(state.swarm_task_context('thread-1', self.run), {'task_title': 'Publish release'})
        self.assertIsNone(state.swarm_task_context('thread-1', self.run + 1))
        self.assertIsNone(state.swarm_task_context('app-1', self.run))
        with state.mutation() as cur:
            cur.execute("UPDATE chat_threads SET spawned_by_thread_id = 'app-1' WHERE thread_id = 'thread-1'")
        self.assertIsNone(state.swarm_task_context('thread-1', self.run))
        state.save_swarm_task('thread-1', self.run, 'Must not rename spawned task')
        with db.transaction() as cur:
            cur.execute("SELECT task FROM swarm_agent_ai WHERE thread_id = 'thread-1'")
            self.assertEqual(cur.fetchone()[0], 'Publish release')

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

    def test_only_pending_native_approvals_for_this_thread_are_counted(self) -> None:
        self.finish()
        with state.mutation() as cur:
            for origin, status in [('thread-1', 'pending'), ('thread-1', 'approved'),
                                   ('thread-2', 'pending'), (None, 'pending')]:
                cur.execute(
                    "INSERT INTO tool_approvals (tool_id, action_id, status, summary, payload,"
                    " check_token, created_at, origin_thread_id)"
                    " VALUES ('test', 'send', %s, 'Send', '{}'::jsonb, %s, 1, %s)",
                    (status, 'x' * 32, origin),
                )
            for push_id, origin, status in [('aabbcc', 'thread-1', 'pending'),
                                            ('ddeeff', 'thread-2', 'pending'),
                                            ('112233', 'thread-1', 'approved')]:
                cur.execute(
                    "INSERT INTO pending_pushes (id, owner, repo, ref_updates, changed_paths,"
                    " requested_at, origin_thread_id, status)"
                    " VALUES (%s, 'owner', 'repo', '[]'::jsonb, '[]'::jsonb, %s, %s, %s)",
                    (push_id, state.utc_now(), origin, status),
                )
        self.assertEqual(self.agent()['pending_approval_count'], 2)
        with state.mutation() as cur:
            cur.execute("UPDATE tool_approvals SET status = 'denied'"
                        " WHERE origin_thread_id = 'thread-1' AND status = 'pending'")
            cur.execute("UPDATE pending_pushes SET status = 'rejected' WHERE id = 'aabbcc'")
        self.assertEqual(self.agent()['pending_approval_count'], 0)

    def test_chat_reservation_without_session_is_not_an_agent(self) -> None:
        with state.mutation() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id, archived) VALUES ('thread-2', FALSE)")
        ids = {agent['thread_id'] for agent in state.swarm_snapshot()['agents']}
        self.assertIn('thread-1', ids)
        self.assertNotIn('thread-2', ids)

    def test_snapshot_includes_all_agents_and_search_finds_older_agents(self) -> None:
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
        self.assertEqual(len(snapshot['agents']), 260)
        self.assertFalse(snapshot['has_more'])
        with state.mutation() as cur:
            cur.execute(
                "INSERT INTO pending_pushes (id, owner, repo, ref_updates, changed_paths,"
                " requested_at, origin_thread_id)"
                " VALUES ('aabbcc', 'owner', 'repo', '[]'::jsonb, '[]'::jsonb, %s, 'thread-1')",
                (state.utc_now(),),
            )
        prioritized = state.swarm_snapshot()['agents']
        self.assertEqual(prioritized[0]['thread_id'], 'thread-1')
        matches = state.swarm_snapshot('Archive agent 259')
        self.assertEqual([agent['thread_id'] for agent in matches['agents']], ['thread-259'])
        self.assertFalse(matches['has_more'])

    def test_memory_clear_removes_ai_and_late_results(self) -> None:
        self.finish()
        with state.mutation() as cur:
            state.append_agent_event(cur, 'thread.memory_cleared', 'thread-1', {}, run_number=self.run)
        state.save_swarm_task('thread-1', self.run, 'Late title')
        self.assertIsNone(self.agent()['task'])
        self.assertEqual(self.agent()['pending_approval_count'], 0)

    def test_interactions_aggregate_by_direction_and_expire(self) -> None:
        with patch.object(swarm, '_interaction_window', return_value=('2026-09-26', '2026-10-02')):
            with state.mutation() as cur:
                cur.execute("INSERT INTO swarm_interaction_days VALUES ('2026-09-25','thread-2','thread-1',8)")
                cur.execute("INSERT INTO swarm_interaction_days VALUES ('2026-09-26','thread-2','thread-1',3)")
                for _ in range(6):
                    swarm.record_swarm_interaction(cur, 'thread-2', 'thread-1')
                swarm.record_swarm_interaction(cur, 'thread-1', 'thread-2')
            counts = state.swarm_interactions()['interactions']
            self.assertEqual(counts, [
                {'sender_thread_id': 'thread-2', 'target_thread_id': 'thread-1', 'count': 9},
                {'sender_thread_id': 'thread-1', 'target_thread_id': 'thread-2', 'count': 1},
            ])
            with state.mutation() as cur:
                cur.execute('SELECT MIN(day), COUNT(*) FROM swarm_interaction_days')
                self.assertEqual(cur.fetchone(), ('2026-09-26', 3))
        # Reads expire the visible counts even when no new messages arrive.
        with patch.object(swarm, '_interaction_window', return_value=('2026-10-03', '2026-10-09')):
            self.assertEqual(state.swarm_interactions()['interactions'], [])

    def test_dense_interactions_return_strongest_500_without_pruning_counts(self) -> None:
        with patch.object(swarm, '_interaction_window', return_value=('2026-09-26', '2026-10-02')):
            with state.mutation() as cur:
                cur.execute("INSERT INTO swarm_interaction_days SELECT '2026-10-02',"
                            " 'thread-' || n, 'app-1', n FROM generate_series(1,600) AS n")
                # Ranking uses the whole week's aggregate, not the largest day.
                cur.execute("INSERT INTO swarm_interaction_days VALUES ('2026-09-26','thread-1','app-1',1000)")
            counts = state.swarm_interactions()['interactions']
            self.assertEqual(len(counts), 500)
            self.assertEqual(counts[0], {'sender_thread_id': 'thread-1', 'target_thread_id': 'app-1', 'count': 1001})
            self.assertEqual(counts[-1], {'sender_thread_id': 'thread-102', 'target_thread_id': 'app-1', 'count': 102})
            with db.transaction() as cur:
                cur.execute('SELECT COUNT(*) FROM swarm_interaction_days')
                self.assertEqual(cur.fetchone(), (601,))

    def test_ranking_metrics_use_all_links_and_known_token_buckets_in_window(self) -> None:
        with patch.object(swarm, '_interaction_window', return_value=('2026-09-26', '2026-10-02')):
            with state.mutation() as cur:
                # The operator link is too weak to survive the 500-link display cap.
                cur.execute("INSERT INTO swarm_interaction_days SELECT '2026-10-02',"
                            " 'thread-' || n, 'app-1', 100 FROM generate_series(1,600) AS n")
                cur.execute("INSERT INTO swarm_interaction_days VALUES"
                            " ('2026-09-26','operator','app-1',3),"
                            " ('2026-10-02','operator','app-1',4),"
                            " ('2026-10-02','app-1','thread-1',1),"
                            " ('2026-10-02','app-1','app-1',9),"
                            " ('2026-10-02','kern-host','app-1',10000),"
                            " ('2026-10-02','kern-host','schedule-1',3),"
                            " ('2026-10-02','kern-host','thread-1',2),"
                            " ('2026-09-25','operator','app-1',900),"
                            " ('2026-10-03','operator','app-1',900)")
                for thread_id, run, measured, buckets in [
                    ('app-1', 1, '2026-09-26T00:00:00Z', (100, 200, 300, 400)),
                    ('app-1', 2, '2026-10-02T23:59:59Z', (10, None, 0, 20)),
                    ('app-1', 3, '2026-09-25T23:59:59Z', (9000, 0, 0, 0)),
                    ('app-1', 4, '2026-10-03T00:00:00Z', (9000, 0, 0, 0)),
                    ('app-2', 1, '2026-10-02T12:00:00Z', (0, 0, 0, 0)),
                    ('app-3', 1, '2026-10-02T12:00:00Z', (None, None, None, None)),
                ]:
                    cur.execute("INSERT INTO turn_usage (thread_id, run_number, agent_runtime, model,"
                                " started_at, measured_at, input_tokens, cached_input_tokens, cache_write_tokens, output_tokens)"
                                " VALUES (%s,%s,'codex','test',%s,%s,%s,%s,%s,%s)",
                                (thread_id, run, measured, measured, *buckets))
            result = state.swarm_interactions()
            self.assertEqual(len(result['interactions']), 500)
            self.assertFalse(any(edge['sender_thread_id'] == 'operator' for edge in result['interactions']))
            self.assertEqual(result['metrics']['app-1'], {
                'operator_messages': 7, 'agent_peers': 600, 'total_tokens': 1030, 'tokens_partial': True,
            })
            self.assertEqual(result['metrics']['app-2'], {
                'operator_messages': 0, 'agent_peers': 0, 'total_tokens': 0, 'tokens_partial': False,
            })
            self.assertIsNone(result['metrics']['app-3']['total_tokens'])
            self.assertTrue(result['metrics']['app-3']['tokens_partial'])
            self.assertEqual(result['metrics']['thread-1']['agent_peers'], 1)
            self.assertIsNone(result['metrics']['thread-1']['total_tokens'])
            self.assertNotIn('operator', result['metrics'])
            self.assertNotIn('kern-host', result['metrics'])
            self.assertEqual(result['metrics']['schedule-1'], {
                'operator_messages': 0, 'agent_peers': 0, 'total_tokens': None, 'tokens_partial': True,
            })
            self.assertIn({'sender_thread_id': 'kern-host', 'target_thread_id': 'app-1', 'count': 10000},
                          result['interactions'])

    def test_only_on_demand_has_a_task_and_spawned_parent_is_visible(self) -> None:
        self.assertTrue(state.is_on_demand_agent('thread-1'))
        state.save_swarm_task('thread-1', self.run, 'Old task')
        with state.mutation() as cur:
            cur.execute("UPDATE chat_threads SET spawned_by_thread_id = 'app-1' WHERE thread_id = 'thread-1'")
        self.assertFalse(state.is_on_demand_agent('thread-1'))
        self.assertFalse(state.is_on_demand_agent('app-1'))
        self.assertEqual(self.agent()['kind'], 'spawned')
        self.assertEqual(self.agent()['spawned_by_thread_id'], 'app-1')
        self.assertIsNone(self.agent()['task'])

    def test_archived_chat_is_excluded(self) -> None:
        with state.mutation() as cur:
            cur.execute("UPDATE chat_threads SET archived = TRUE WHERE thread_id = 'thread-1'")
        self.assertFalse(any(agent['thread_id'] == 'thread-1' for agent in state.swarm_snapshot()['agents']))


if __name__ == '__main__':
    unittest.main()
