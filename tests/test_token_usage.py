from __future__ import annotations

from datetime import datetime, timezone
import json
import sys
import unittest
from unittest.mock import patch

from host.runtime.agent_runtime import token_usage, codex_app_server, grok_agent, hermes_agent
from host.runtime.core import db, state
import pg_harness


class UsageTests(unittest.TestCase):
    def test_analytics_is_operator_only_and_has_no_date_parameters(self):
        from host.runtime.admin_api import service
        from host.runtime.admin_api.errors import ApiError
        operator = service.OperatorPrincipal("test-session")
        with patch.object(state, "usage_report", return_value={"groups": []}) as report:
            self.assertEqual(service.route("GET", "/v1/analytics", {}, None, principal=operator), {"groups": []})
            report.assert_called_once()
        with self.assertRaises(ApiError) as error:
            service.route("GET", "/v1/analytics", {"since": ["2020"]}, None, principal=operator)
        self.assertEqual(error.exception.status.value, 400)
        with self.assertRaises(ApiError) as error:
            service.route("GET", "/v1/analytics", {}, None, principal=service.WorkspacePrincipal())
        self.assertEqual(error.exception.status.value, 403)

    def test_normalizes_disjoint_buckets_without_adding_reasoning_twice(self):
        for provider, raw in [
            ('codex', dict(inputTokens=100, cachedInputTokens=60, cacheWriteInputTokens=10, outputTokens=20, reasoningOutputTokens=8)),
            ('grok', dict(inputTokens=100, cachedReadTokens=60, cacheCreationTokens=10, outputTokens=20, reasoningTokens=8)),
            ('claude', dict(input_tokens=30, cache_read_input_tokens=60, cache_creation_input_tokens=10, output_tokens=20)),
            ('hermes', dict(input_tokens=30, cache_read_tokens=60, cache_write_tokens=10, output_tokens=20)),
        ]:
            with self.subTest(provider=provider):
                self.assertEqual(token_usage.record('a', raw, provider)['usage'], dict(zip(token_usage.FIELDS, [30, 60, 10, 20])))

    def test_duplicate_and_corrected_responses_and_missing_counts(self):
        accumulator = token_usage.TurnUsage()
        a = token_usage.record('a', dict(input_tokens=2, cache_read_input_tokens=60, cache_creation_input_tokens=10, output_tokens=20), 'claude')
        self.assertEqual(accumulator.add(a)['output_tokens'], 20)
        self.assertIsNone(accumulator.add(a))
        a['usage']['output_tokens'] = 25
        self.assertEqual(accumulator.add(a)['output_tokens'], 25)
        b = token_usage.record('b', dict(input_tokens=3, output_tokens=10), 'claude')
        totals = accumulator.add(b)
        self.assertEqual(totals['output_tokens'], 35)
        self.assertIsNone(totals['cached_input_tokens'])
        self.assertIsNone(token_usage.record('bad', {'output_tokens': -1}, 'claude'))
        self.assertIsNone(token_usage.record('bad', {'output_tokens': True}, 'claude'))

    def test_codex_resumed_session_counts_only_new_usage_and_ignores_other_turns(self):
        script = r'''
import json, sys
for line in sys.stdin:
 m=json.loads(line); method=m.get('method')
 result = {} if method=='initialize' else {'thread':{'id':'session'}} if method=='thread/resume' else {'turn':{'id':'new-turn'}}
 if 'id' in m: print(json.dumps({'id':m['id'],'result':result}),flush=True)
 if method=='turn/start':
  for turn_id in ['old-turn','new-turn','new-turn']:
   print(json.dumps({'method':'thread/tokenUsage/updated','params':{'threadId':'session','turnId':turn_id,'tokenUsage':{'total':{'totalTokens':1000000},'last':{'inputTokens':100,'cachedInputTokens':60,'outputTokens':20}}}}),flush=True)
  print(json.dumps({'method':'turn/completed','params':{'turn':{'status':'completed'}}}),flush=True)
'''
        events = []
        with codex_app_server.CodexAppServer([sys.executable, '-u', '-c', script]) as server:
            codex_app_server.run_turn(server, 'hello', 'session', 'gpt-5.6-sol', 'high', events.append)
        usage = token_usage.TurnUsage()
        totals = None
        for event in events:
            if isinstance(event, dict):
                totals = usage.add(event) or totals
        self.assertEqual(totals, dict(zip(token_usage.FIELDS, [40, 60, 0, 20])))
        self.assertEqual(len(usage.samples), 1)

    def test_grok_completed_turn_and_replay(self):
        event = {'method':'_x.ai/session/update','params':{'sessionId':'s','update':{'sessionUpdate':'turn_completed','prompt_id':'p','usage':{'inputTokens':100,'cachedReadTokens':60,'cacheCreationTokens':0,'outputTokens':20}}}}
        emitted = []
        grok_agent._consume_turn_notification(event, 's', [], [], {}, emitted.append)
        self.assertEqual(emitted[0]['usage']['input_tokens'], 40)
        event['params']['_meta'] = {'isReplay': True}
        grok_agent._consume_turn_notification(event, 's', [], [], {}, emitted.append)
        self.assertEqual(len(emitted), 1)

    def test_hermes_hook_framing_reaches_usage_parser(self):
        from test_hermes_stdin_activity import hermes_stdin, _emitted
        emitted = _emitted(hermes_stdin._on_post_api_request, api_request_id='turn:api:1', response={'usage':dict(input_tokens=30,cache_read_tokens=60,cache_write_tokens=10,output_tokens=20)})
        marker = '\x1ekern-activity test '
        result = hermes_agent._activity_from_line(marker + json.dumps(emitted[0]), marker)
        self.assertEqual(result['usage'], dict(zip(token_usage.FIELDS, [30, 60, 10, 20])))


class UsageStorageTests(unittest.TestCase):
    def setUp(self):
        pg_harness.reset_database()

    def test_report_resolves_all_three_workspace_kinds(self):
        from host.runtime.workspace import schedules
        schedule = schedules.create_schedule({
            "name": "Daily research", "message": "Research", "cadence": "daily",
            "daily_time": "12:00", "agent_runtime": "codex", "model": "gpt-5.6-sol", "effort": "high",
        }, actor="user")
        with state.mutation() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id, name) VALUES ('thread-1', 'My chat')")
            cur.execute(
                "INSERT INTO web_apps (app_id, name, revision, created_at, updated_at, agent_runtime, agent_model, agent_effort) "
                "VALUES ('app-1', 'My app', 0, '2026-09-12T00:00:00Z', '2026-09-12T00:00:00Z', 'codex', 'gpt-5.6-sol', 'high')")
            for thread in ['thread-1', 'app-1', schedule['thread_id']]:
                state.start_turn_usage(cur, thread, 1, 'codex', 'gpt-5.6-sol')
        groups = state.usage_report()['groups']
        self.assertEqual({(row['name'], row['kind']) for row in groups}, {
            ('My chat', 'chats'), ('My app', 'apps'), ('Daily research', 'schedules')})
        self.assertTrue(all(row['active'] for row in groups))
        with state.mutation() as cur:
            cur.execute("UPDATE chat_threads SET archived = TRUE WHERE thread_id = 'thread-1'")
            cur.execute("UPDATE web_apps SET archived = TRUE WHERE app_id = 'app-1'")
        schedules.delete_schedule(schedule['id'], {'expected_revision': [str(schedule['revision'])]}, actor='user')
        groups = state.usage_report()['groups']
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(not row['active'] for row in groups))
        with state.mutation() as cur:
            cur.execute("UPDATE chat_threads SET archived = FALSE WHERE thread_id = 'thread-1'")
            cur.execute("UPDATE web_apps SET archived = FALSE WHERE app_id = 'app-1'")
            cur.execute("UPDATE schedules SET deleted_at = '2000-01-01T00:00:00Z'")
        self.assertEqual(schedules.prune_deleted(), 1)
        groups = state.usage_report()['groups']
        self.assertEqual({row['kind']: row['active'] for row in groups}, {
            'chats': True, 'apps': True, 'schedules': False,
        })

    def test_unnamed_chat_is_not_labeled_deleted(self):
        with state.mutation() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id) VALUES ('thread-1')")
            state.start_turn_usage(cur, 'thread-1', 1, 'codex', 'gpt-5.6-sol')
        self.assertEqual(state.usage_report()['groups'][0]['name'], 'Untitled chat')
        with state.mutation() as cur:
            cur.execute("DELETE FROM chat_threads WHERE thread_id = 'thread-1'")
        self.assertEqual(state.usage_report()['groups'][0]['name'], 'Deleted thread')
        self.assertFalse(state.usage_report()['groups'][0]['active'])

    def test_lifetime_totals_count_deltas_and_survive_retention(self):
        def counts(*values):
            return dict(zip(token_usage.FIELDS, values))

        self.assertEqual(state.lifetime_token_usage(), counts(0, 0, 0, 0))
        with state.mutation() as cur:
            state.start_turn_usage(cur, 'thread-1', 1, 'codex', 'gpt-5.6-sol')
            state.save_turn_usage(cur, 'thread-1', 1, counts(10, 20, 0, 5))
            state.save_turn_usage(cur, 'thread-1', 1, counts(10, 20, 0, 5))
            state.save_turn_usage(cur, 'thread-1', 1, counts(8, 30, 0, 10))
            state.start_turn_usage(cur, 'app-1', 1, 'claude_code', 'claude-fable-5-1')
            state.save_turn_usage(cur, 'app-1', 1, counts(2, None, 4, 3))
        self.assertEqual(state.lifetime_token_usage(), counts(10, 30, 4, 13))
        # Unknown replaces a previously reported bucket, matching Analytics' known totals.
        with state.mutation() as cur:
            state.save_turn_usage(cur, 'thread-1', 1, counts(8, None, 0, 10))
            cur.execute("UPDATE turn_usage SET measured_at = '2000-01-01T00:00:00Z'")
            state.prune_turn_usage(cur, '2020-01-01T00:00:00Z')
        self.assertEqual(state.usage_report()['groups'], [])
        self.assertEqual(state.lifetime_token_usage(), counts(10, 0, 4, 13))
        with state.mutation() as cur:
            state.start_turn_usage(cur, 'thread-1', 2, 'codex', 'gpt-5.6-sol')
            state.save_turn_usage(cur, 'thread-1', 2, counts(100, 200, 0, 50))
            state.save_turn_usage(cur, 'missing', 1, counts(999, 999, 999, 999))
        self.assertEqual(state.lifetime_token_usage(), counts(110, 200, 4, 63))
        # Counters and the detailed measurement roll back together.
        with self.assertRaises(RuntimeError):
            with state.mutation() as cur:
                state.save_turn_usage(cur, 'thread-1', 2, counts(500, 500, 500, 500))
                raise RuntimeError('abort transaction')
        self.assertEqual(state.lifetime_token_usage(), counts(110, 200, 4, 63))

    def test_one_row_per_turn_seven_days_unknown_and_name_join(self):
        with state.mutation() as cur:
            cur.execute("INSERT INTO chat_threads (thread_id, name) VALUES ('thread-1', 'My chat')")
            for run in [1, 2, 3, 4]:
                state.start_turn_usage(cur, 'thread-1', run, 'codex', 'gpt-5.6-sol')
            state.save_turn_usage(cur, 'thread-1', 1, dict(zip(token_usage.FIELDS, [10, 20, 0, 5])))
            state.save_turn_usage(cur, 'thread-1', 1, dict(zip(token_usage.FIELDS, [30, 40, 0, 10])))
            cur.execute("UPDATE turn_usage SET measured_at = '2026-09-12T10:00:00Z' WHERE run_number IN (1, 2)")
            cur.execute("UPDATE turn_usage SET measured_at = '2026-09-05T23:59:59Z' WHERE run_number = 3")
            cur.execute("UPDATE turn_usage SET measured_at = '2026-09-13T00:00:00Z' WHERE run_number = 4")
        result = state.usage_report(datetime(2026,9,12,12,tzinfo=timezone.utc))
        self.assertEqual(result['days'], [f'2026-09-{day:02}' for day in range(6,13)])
        self.assertEqual(len(result['groups']), 1)
        group = result['groups'][0]
        self.assertEqual((group['name'], group['turns']), ('My chat', 2))
        self.assertEqual(group['tokens']['input_tokens'], 30)
        self.assertEqual(group['measured_turns']['input_tokens'], 1)
        with db.transaction() as cur:
            cur.execute('SELECT COUNT(*) FROM turn_usage')
            self.assertEqual(cur.fetchone()[0], 4)
