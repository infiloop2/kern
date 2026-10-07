"""Database-backed Instagram approvals bind selection, decisions and receipts.

CI runs these with PostgreSQL; local production-host runs deliberately skip.
"""
from copy import deepcopy
from unittest.mock import patch

from host.runtime.core import state
from host.runtime.tools import tools_host
from host.tools import instagram
from host.tools.shared.web import WebRequestError
from test_tools_host import ToolsHostTestCase
from test_tools_instagram import connected_api
from test_tools_instagram_interactions import ACCOUNT, CUSTOMER, CONVERSATION, CLOCK, MetaFixture


class InstagramApprovalHostTests(ToolsHostTestCase):
    def setUp(self):
        super().setUp()
        with state.mutation() as cur:
            state.set_tool_enabled(cur, 'instagram', True)
            state.save_tool_config_value(cur, 'instagram', 'INSTAGRAM_APP_ID', 'ig-app')
            state.save_tool_config_value(cur, 'instagram', 'INSTAGRAM_APP_SECRET', 'ig-secret')
        for connection_id, account_id, token in (
            ('ig-first', '800', 'token-first'), ('ig-selected', ACCOUNT, 'token-selected'),
        ):
            credential = deepcopy(connected_api().credentials.record)
            credential['account']['id'] = account_id
            credential['secret']['access_token'] = token
            state.put_tool_credential('instagram', credential, connection_id)
        self.meta = MetaFixture()
        self.enterContext(patch.object(instagram, 'json_request', self.meta))
        self.enterContext(patch.object(instagram, 'now', return_value=CLOCK))

    def queue(self):
        return tools_host.execute_action('instagram', 'reply_to_conversation',
            {'conversation_id': CONVERSATION, 'text': 'Exact approved text'},
            connection_id='ig-selected', origin_thread_id=None)['approval_id']

    def test_selection_binds_read_approval_execution_and_receipt_and_send_runs_once(self):
        with self.assertRaisesRegex(tools_host.ToolCallError, 'multiple connected accounts'):
            tools_host.execute_action('instagram', 'get_conversations', {}, origin_thread_id=None)
        result = tools_host.execute_action('instagram', 'get_conversations', {},
                                          connection_id='ig-selected', origin_thread_id=None)
        self.assertEqual(result['status'], 'executed')
        self.assertEqual(state.page_tool_events_before(None)[0]['connection_id'], 'ig-selected')
        approval_id = self.queue()
        record = state.tool_approval(approval_id)
        self.assertEqual(record['connection_id'], 'ig-selected')
        self.assertEqual(record['account_id'], ACCOUNT)
        self.assertEqual(record['payload']['proposal']['recipient_id'], CUSTOMER)
        self.assertFalse(self.meta.sends)
        decision = tools_host.decide_approval(approval_id, 'approve', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'executed')
        self.assertEqual(self.meta.sends[0][2]['headers']['Authorization'], 'Bearer token-selected')
        receipt = state.page_tool_events_before(None)[0]
        self.assertEqual(receipt['connection_id'], 'ig-selected')
        self.assertEqual(receipt['account_id'], ACCOUNT)
        self.assertEqual(receipt['outcome'], 'executed')
        with self.assertRaisesRegex(tools_host.ToolCallError, 'not pending'):
            tools_host.decide_approval(approval_id, 'approve', public_hostname=None)
        self.assertEqual(len(self.meta.sends), 1)

    def test_replaced_connection_cannot_execute_its_old_approval(self):
        approval_id = self.queue()
        replacement = deepcopy(connected_api().credentials.record)
        replacement['account']['id'] = '801'
        state.delete_tool_credential('instagram', 'ig-selected')
        state.put_tool_credential('instagram', replacement, 'ig-selected')
        decision = tools_host.decide_approval(approval_id, 'approve', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'failed')
        self.assertIn('no longer connected', decision['result']['error'])
        self.assertFalse(self.meta.sends)

    def test_denial_is_terminal_with_no_send(self):
        approval_id = self.queue()
        decision = tools_host.decide_approval(approval_id, 'deny', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'denied')
        self.assertEqual(state.page_tool_events_before(None)[0]['connection_id'], 'ig-selected')
        with self.assertRaisesRegex(tools_host.ToolCallError, 'not pending'):
            tools_host.decide_approval(approval_id, 'approve', public_hostname=None)
        self.assertFalse(self.meta.sends)

    def test_ambiguous_send_records_terminal_failure_without_replay(self):
        approval_id = self.queue()
        self.meta.send_error = WebRequestError('raw secret')
        decision = tools_host.decide_approval(approval_id, 'approve', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'failed')
        self.assertIn('Do not automatically retry', decision['result']['error'])
        self.assertNotIn('raw secret', decision['approval']['result'])
        self.assertEqual(state.page_tool_events_before(None)[0]['outcome'], 'failed')
        with self.assertRaisesRegex(tools_host.ToolCallError, 'not pending'):
            tools_host.decide_approval(approval_id, 'approve', public_hostname=None)
        self.assertEqual(len(self.meta.sends), 1)
