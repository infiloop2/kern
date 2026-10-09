"""Real host decisions and receipts; mocked provider, PostgreSQL CI only."""
from copy import deepcopy
import json
from unittest.mock import patch

from host.runtime.core import state
from host.runtime.tools import tools_host
from host.tools import instagram_ads as ads
from host.tools.shared.web import WebRequestError
from test_tools_host import ToolsHostTestCase
from test_tools_instagram_ads import connected_api, launch_input, MetaFixture


class InstagramAdsApprovalHostTests(ToolsHostTestCase):
    def setUp(self):
        super().setUp()
        with state.mutation() as cur:
            state.set_tool_enabled(cur, 'instagram_ads', True)
            state.save_tool_config_value(cur, 'instagram_ads', 'INSTAGRAM_ADS_APP_ID', 'meta-app')
            state.save_tool_config_value(cur, 'instagram_ads', 'INSTAGRAM_ADS_APP_SECRET', 'meta-secret')
        for connection, account in (('meta-first', '999'), ('meta-selected', '900')):
            credential = deepcopy(connected_api().credentials.record)
            credential['account']['id'] = account
            state.put_tool_credential('instagram_ads', credential, connection)
        self.meta = MetaFixture()
        self.enterContext(patch.object(ads, 'json_request', self.meta))

    def queue(self):
        result = tools_host.execute_action('instagram_ads', 'launch_campaign', launch_input(),
                                          connection_id='meta-selected', origin_thread_id=None)
        self.assertEqual(result['status'], 'pending_approval', result)
        self.assertFalse(self.meta.writes)
        return result['approval_id']

    def test_selected_connection_single_decision_and_final_receipt(self):
        with self.assertRaisesRegex(tools_host.ToolCallError, 'multiple connected accounts'):
            tools_host.execute_action('instagram_ads', 'list_accounts', {}, origin_thread_id=None)
        approval = self.queue()
        record = state.tool_approval(approval)
        self.assertEqual(record['connection_id'], 'meta-selected')
        self.assertEqual(record['account_id'], '900')
        decision = tools_host.decide_approval(approval, 'approve', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'executed')
        self.assertEqual(len(self.meta.writes), 7)
        receipt = state.page_tool_events_before(None)[0]
        self.assertEqual(receipt['connection_id'], 'meta-selected')
        self.assertEqual(receipt['account_id'], '900')
        self.assertEqual(receipt['outcome'], 'executed')
        self.assertIn('campaign_id', decision['approval']['result'])
        with self.assertRaisesRegex(tools_host.ToolCallError, 'not pending'):
            tools_host.decide_approval(approval, 'approve', public_hostname=None)
        self.assertEqual(len(self.meta.writes), 7)

    def test_denial_is_terminal_and_creates_nothing(self):
        approval = self.queue()
        decision = tools_host.decide_approval(approval, 'deny', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'denied')
        self.assertFalse(self.meta.writes)
        with self.assertRaisesRegex(tools_host.ToolCallError, 'not pending'):
            tools_host.decide_approval(approval, 'approve', public_hostname=None)

    def test_direct_revoked_scope_reconnect_guidance_reaches_host_result_and_receipt(self):
        self.meta.permissions.remove('ads_read')
        result = tools_host.execute_action('instagram_ads', 'list_accounts', {},
                                          connection_id='meta-selected', origin_thread_id=None)
        self.assertEqual(result['status'], 'failed')
        self.assertTrue(result['reconnect_required'])
        self.assertIn('Connect again', result['error'])
        self.assertEqual([call[1] for call in self.meta.calls], ['/me', '/me/permissions'])
        self.assertFalse(self.meta.writes)
        self.assertIsNotNone(state.tool_credential('instagram_ads', 'meta-selected'))
        self.assertIsNotNone(state.tool_credential('instagram_ads', 'meta-first'))
        receipt = state.page_tool_events_before(None)[0]
        self.assertEqual((receipt['outcome'], receipt['connection_id']), ('failed', 'meta-selected'))
        self.assertIn('Connect again', receipt['detail'])

    def test_ambiguous_failure_receipt_is_terminal_and_redacted(self):
        approval = self.queue()
        self.meta.fail_write = 3
        decision = tools_host.decide_approval(approval, 'approve', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'failed')
        self.assertIn('creative creation', decision['result']['error'])
        self.assertIn('campaign_id', decision['result']['error'])
        self.assertNotIn('provider-body-secret', decision['approval']['result'])
        self.assertEqual(state.page_tool_events_before(None)[0]['outcome'], 'failed')
        with self.assertRaisesRegex(tools_host.ToolCallError, 'not pending'):
            tools_host.decide_approval(approval, 'approve', public_hostname=None)
        self.assertEqual(len(self.meta.writes), 3)

    def test_reconnected_credential_cannot_use_previous_approval(self):
        approval = self.queue()
        credential = deepcopy(connected_api().credentials.record)
        credential['metadata']['grant_id'] = 'new-grant'
        state.put_tool_credential('instagram_ads', credential, 'meta-selected')
        decision = tools_host.decide_approval(approval, 'approve', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'failed')
        self.assertFalse(self.meta.writes)

    def test_regional_rejection_persists_partial_ids_and_terminal_paused_failure(self):
        value = launch_input()
        value['audience'] = {'country_group': 'worldwide'}
        self.meta.account.update(default_dsa_beneficiary='Owned brand', default_dsa_payor='Advertiser')
        queued = tools_host.execute_action('instagram_ads', 'launch_campaign', value,
                                          connection_id='meta-selected', origin_thread_id=None)
        self.assertEqual(queued['status'], 'pending_approval', queued)
        posted = []
        def reject(method, url, **kwargs):
            if method == 'POST':
                posted.append(url)
                if url.endswith('/adsets'):
                    raise WebRequestError('provider secret', status=400,
                        body=json.dumps({'error': {'code': 100, 'error_subcode': 3858634,
                                                  'message': 'provider secret'}}).encode())
            return self.meta(method, url, **kwargs)
        with patch.object(ads, 'json_request', reject):
            decision = tools_host.decide_approval(queued['approval_id'], 'approve', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'failed')
        self.assertEqual(self.meta.resources['500']['status'], 'PAUSED')
        persisted = state.tool_approval(queued['approval_id'])['result']
        self.assertIn('campaign_id', persisted)
        self.assertIn('error_subcode=3858634', persisted)
        self.assertNotIn('provider secret', persisted)
        self.assertEqual(len(posted), 2)
        self.assertEqual(state.page_tool_events_before(None)[0]['outcome'], 'failed')
        with self.assertRaisesRegex(tools_host.ToolCallError, 'not pending'):
            tools_host.decide_approval(queued['approval_id'], 'approve', public_hostname=None)
        self.assertEqual(len(posted), 2)

    def test_removed_caller_fields_and_action_fail_at_host_boundary_before_provider_reads(self):
        for key in ('dsa_beneficiary', 'dsa_payor'):
            with self.subTest(key=key):
                value = launch_input()
                value[key] = 'Caller public name'
                with self.assertRaisesRegex(tools_host.ToolCallError, 'unsupported fields: ' + key):
                    tools_host.execute_action('instagram_ads', 'launch_campaign', value,
                        connection_id='meta-selected', origin_thread_id=None)
                self.assertFalse(self.meta.calls)
                self.assertFalse(self.meta.writes)
        value = launch_input()
        value['audience']['audience_ids'] = ['1001']
        with self.assertRaisesRegex(tools_host.ToolCallError, 'unsupported fields: audience_ids'):
            tools_host.execute_action('instagram_ads', 'launch_campaign', value,
                connection_id='meta-selected', origin_thread_id=None)
        with self.assertRaisesRegex(tools_host.ToolCallError, 'has no action list_audiences'):
            tools_host.execute_action('instagram_ads', 'list_audiences', {'account_id': '100'},
                connection_id='meta-selected', origin_thread_id=None)
        self.assertFalse(self.meta.calls)
        self.assertFalse(self.meta.writes)

    def test_saved_dsa_defaults_persist_in_approval_and_drift_is_terminal(self):
        self.meta.account.update(default_dsa_beneficiary='Public beneficiary', default_dsa_payor='Public payer')
        value = launch_input()
        value['audience'] = {'country_group': 'worldwide'}
        queued = tools_host.execute_action('instagram_ads', 'launch_campaign', value,
            connection_id='meta-selected', origin_thread_id=None)
        self.assertEqual(queued['status'], 'pending_approval', queued)
        record = state.tool_approval(queued['approval_id'])
        proposal = record['payload']['proposal']
        self.assertEqual(proposal['dsa'], {'dsa_beneficiary': 'Public beneficiary', 'dsa_payor': 'Public payer'})
        self.assertTrue(set(proposal['dsa']).isdisjoint(proposal['input']))
        self.meta.account['default_dsa_payor'] = 'Changed public payer'
        decision = tools_host.decide_approval(queued['approval_id'], 'approve', public_hostname=None)
        self.assertEqual(decision['approval']['status'], 'failed')
        self.assertIn('changed since approval', state.tool_approval(queued['approval_id'])['result'])
        self.assertFalse(self.meta.writes)
        self.assertEqual(state.page_tool_events_before(None)[0]['outcome'], 'failed')
        with self.assertRaisesRegex(tools_host.ToolCallError, 'not pending'):
            tools_host.decide_approval(queued['approval_id'], 'approve', public_hostname=None)

    def test_pause_auth_failure_persists_terminal_receipt_and_reconnect_guidance(self):
        for stage in ('pause_write', 'pause_readback'):
            with self.subTest(stage=stage):
                credential = deepcopy(connected_api().credentials.record)
                state.put_tool_credential('instagram_ads', credential, 'meta-selected')
                result = tools_host.execute_action('instagram_ads', 'end_campaign',
                    {'account_id': '100', 'campaign_id': '500'}, connection_id='meta-selected', origin_thread_id=None)
                self.assertEqual(result['status'], 'pending_approval', result)
                posted = False
                def revoked(method, url, **kwargs):
                    nonlocal posted
                    if method == 'POST':
                        posted = True
                    if (method == 'POST' and stage == 'pause_write') or (method == 'GET' and posted and stage == 'pause_readback'):
                        raise WebRequestError('raw provider secret', status=400,
                            body=b'{"error":{"code":190,"message":"raw provider secret"}}')
                    return self.meta(method, url, **kwargs)
                with patch.object(ads, 'json_request', revoked):
                    decision = tools_host.decide_approval(result['approval_id'], 'approve', public_hostname=None)
                self.assertEqual(decision['approval']['status'], 'failed')
                self.assertTrue(decision['result']['reconnect_required'])
                self.assertIn('pause is unconfirmed', state.tool_approval(result['approval_id'])['result'])
                self.assertNotIn('raw provider secret', decision['approval']['result'])
                self.assertIsNone(state.tool_credential('instagram_ads', 'meta-selected'))
                receipt = state.page_tool_events_before(None)[0]
                self.assertEqual((receipt['outcome'], receipt['connection_id']), ('failed', 'meta-selected'))
                with self.assertRaisesRegex(tools_host.ToolCallError, 'not pending'):
                    tools_host.decide_approval(result['approval_id'], 'approve', public_hostname=None)
