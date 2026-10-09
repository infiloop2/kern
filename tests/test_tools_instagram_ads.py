"""Marketing API contracts are mocked. These tests never create live ads."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from host.tools import instagram_ads as ads
from host.runtime.tools import tools_host
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.web import WebRequestError
from test_tools import FakeHostAPI, FRESH_EXPIRES_AT, assert_matches_output_schema

ACCOUNT, PAGE, INSTAGRAM, MEDIA = '100', '200', '300', '400'
CAMPAIGN, ADSET, CREATIVE, AD = '500', '600', '700', '800'


def connected_api():
    api = FakeHostAPI()
    api.config.update(INSTAGRAM_ADS_APP_ID='meta-app', INSTAGRAM_ADS_APP_SECRET='meta-secret')
    api.credentials.save({
        'account': {'id': '900', 'label': 'Advertiser', 'scopes': list(ads.SCOPES)},
        'secret': {'access_token': 'meta-token', 'expires_at': FRESH_EXPIRES_AT},
        'metadata': {'created_at': 1, 'updated_at': 1, 'grant_id': 'test-grant'},
    })
    return api


def launch_input(objective='ENGAGEMENTS'):
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    result = {'account_id': ACCOUNT, 'page_id': PAGE, 'instagram_user_id': INSTAGRAM,
              'media_id': MEDIA, 'name': 'Approved Reel', 'objective': objective,
              'special_ad_category': 'NONE', 'lifetime_budget': '2500',
              'start_time': start.isoformat(), 'end_time': (start + timedelta(days=2)).isoformat(),
              'audience': {'countries': ['US']}}
    if objective == 'WEBSITE_CLICKS':
        result['destination_url'] = 'https://example.com/exact?campaign=reel'
    return result


class MetaFixture:
    """Exercise real request encoding, edges, status transitions and readback."""
    def __init__(self):
        self.calls = []
        self.writes = []
        self.account = {'id': 'act_' + ACCOUNT, 'account_id': ACCOUNT, 'name': 'Advertiser',
                        'currency': 'USD', 'timezone_name': 'America/New_York',
                        'account_status': 1, 'disable_reason': 0, 'user_tasks': ['ADVERTISE'],
                        'funding_source': 'billing-id', 'min_daily_budget': '50'}
        self.page = {'id': PAGE, 'name': 'Real Page', 'instagram_business_account': {
            'id': INSTAGRAM, 'username': 'owned_profile', 'account_type': 'BUSINESS'}}
        self.media = {'id': MEDIA, 'owner': {'id': INSTAGRAM}, 'caption': 'The exact original caption',
                      'media_type': 'VIDEO', 'media_product_type': 'REELS',
                      'media_url': 'https://cdninstagram.com/source.mp4?signature=first',
                      'permalink': 'https://www.instagram.com/reel/owned/', 'timestamp': '2026-01-01T00:00:00+0000',
                      'boost_eligibility_info': {'eligible_to_boost': True}}
        self.resources = {CAMPAIGN: {'id': CAMPAIGN, 'account_id': ACCOUNT, 'name': 'Existing',
                                    'status': 'ACTIVE', 'effective_status': 'ACTIVE', 'objective': 'OUTCOME_ENGAGEMENT'}}
        self.fail_write = None
        self.write_hook = None
        self.read_hook = None
        self.page_cursor = None
        self.minimum = 50
        self.permissions = list(ads.SCOPES)
        self.insights = [{'date_start': '2026-01-01', 'date_stop': '2026-01-01',
                          'account_currency': 'USD', 'spend': '0', 'actions': []}]

    def __call__(self, method, url, **kwargs):
        parsed = urlsplit(url)
        assert parsed.scheme == 'https' and parsed.netloc == 'graph.facebook.com'
        path = parsed.path.removeprefix('/v25.0')
        fields = {key: rows[0] for key, rows in parse_qs(parsed.query, keep_blank_values=True).items()}
        if method == 'POST':
            fields = kwargs['form']
        self.calls.append((method, path, deepcopy(fields), deepcopy(kwargs)))
        if path == '/oauth/access_token':
            return {'access_token': 'long-token' if 'fb_exchange_token' in fields else 'short-token',
                    'expires_in': 5184000}
        token = kwargs['headers']['Authorization'].removeprefix('Bearer ')
        assert fields['appsecret_proof'] == hmac.new(b'meta-secret', token.encode(), hashlib.sha256).hexdigest()
        assert kwargs['timeout'] <= 15
        if method == 'POST':
            decoded = {}
            for key, value in fields.items():
                try:
                    decoded[key] = json.loads(value)
                except ValueError:
                    decoded[key] = value
            # Numeric ids and budgets must stay provider numeric strings.
            for key in ('campaign_id', 'adset_id', 'lifetime_budget', 'object_id', 'instagram_user_id', 'source_instagram_media_id'):
                if key in fields:
                    decoded[key] = fields[key]
            self.writes.append((path, decoded))
            if self.fail_write == len(self.writes):
                raise WebRequestError('provider-body-secret meta-token')
            edges = {'campaigns': CAMPAIGN, 'adsets': ADSET, 'adcreatives': CREATIVE, 'ads': AD}
            if path.startswith('/act_' + ACCOUNT + '/'):
                identifier = edges[path.rsplit('/', 1)[1]]
                row = {**decoded, 'id': identifier, 'account_id': ACCOUNT, 'effective_status': 'PENDING_REVIEW'}
                row.pop('appsecret_proof', None)
                if identifier == AD:
                    row['campaign_id'] = CAMPAIGN
                    row['creative'] = {'id': CREATIVE}
                if identifier == CREATIVE:
                    row['destination_spec'] = None
                self.resources[identifier] = row
                result = {'id': identifier}
            else:
                self.resources[path[1:]]['status'] = decoded['status']
                result = {'success': True}
            if self.write_hook:
                self.write_hook(path, decoded)
            return result
        if self.read_hook:
            self.read_hook(path)
        if path == '/me':
            return {'id': '900', 'name': 'Advertiser'}
        if path == '/me/permissions':
            return {'data': [{'permission': scope, 'status': 'granted'} for scope in self.permissions]}
        if path == '/act_' + ACCOUNT:
            return deepcopy(self.account)
        if path == '/me/adaccounts':
            result = {'data': [deepcopy(self.account)]}
        elif path.endswith('/promote_pages'):
            result = {'data': [deepcopy(self.page)]}
        elif path.endswith('/connected_instagram_accounts'):
            result = {'data': [{'id': INSTAGRAM, 'username': 'owned_profile'}]}
        elif path == '/' + MEDIA:
            return deepcopy(self.media)
        elif path == '/' + INSTAGRAM + '/media':
            result = {'data': [deepcopy(self.media)]}
        elif path == '/search':
            if fields['type'] == 'adinterestvalid':
                assert 'interest_fbid_list' in fields and 'interest_list' not in fields
                result = {'data': [{'id': 1000, 'name': 'Provider interest', 'valid': True}]}
            elif fields['type'] == 'adinterest':
                result = {'data': [{'id': 1000, 'name': 'Provider interest'}]}
            elif fields['location_types'] == '["country_group"]':
                result = {'data': [{'key': 'worldwide', 'name': 'Worldwide', 'type': 'country_group'}]}
            else:
                result = {'data': [{'key': code, 'name': code, 'type': 'country'} for code in ('US', 'DE', 'JP', 'BH', 'GF')[:int(fields['limit'])]]}
        elif path.endswith('/minimum_budgets'):
            return {'data': [{'currency': self.account['currency'], 'min_daily_budget_imp': self.minimum}]}
        elif path == '/act_' + ACCOUNT + '/campaigns':
            result = {'data': [deepcopy(self.resources[CAMPAIGN])]}
        elif path == '/' + CAMPAIGN + '/adsets':
            row = deepcopy(self.resources[ADSET])
            if 'ads.limit(10)' in fields['fields']:
                row['ads'] = {'data': [deepcopy(self.resources[AD])]}
                result = {'data': [row]}
            else:
                result = {'data': [{'id': ADSET}]}
        elif path == '/' + ADSET + '/ads':
            return {'data': [{'id': AD}]}
        elif path.endswith('/insights'):
            result = {'data': deepcopy(self.insights)}
        elif path[1:] in self.resources:
            row = deepcopy(self.resources[path[1:]])
            if path[1:] in (ADSET, CAMPAIGN, CREATIVE):
                # A missing projection must hide the field as the real API does.
                requested = set(fields['fields'].split(','))
                row = {key: value for key, value in row.items() if key in requested}
            return row
        else:
            raise AssertionError('Unexpected provider path: ' + path)
        if self.page_cursor:
            result['paging'] = {'next': 'https://untrusted.example/never-follow', 'cursors': {'after': self.page_cursor}}
        return result


class InstagramAdsTests(unittest.TestCase):
    def setUp(self):
        self.diagnostics = self.enterContext(patch("host.tools.shared.ads_diagnostics.host_errors.emit_record"))
        self.api = connected_api()
        self.meta = MetaFixture()
        self.enterContext(patch.object(ads, 'json_request', self.meta))

    def test_local_launch_semantics_do_not_emit_provider_failure(self):
        value = launch_input()
        value["lifetime_budget"] = "0"
        result = ads.BUNDLED_TOOL.execute("launch_campaign", value, self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("lifetime_budget", result.error)
        self.assertEqual(self.meta.calls, [])
        self.diagnostics.assert_not_called()

    def test_local_report_dates_are_validated_before_provider_reads_and_diagnostics(self):
        cases = (("bad", "2026-01-02"), ("2026-02-30", "2026-03-01"), ("2026-01-02", "2026-01-01"), ("2026-01-01", "2026-05-01"))
        for start, end in cases:
            with self.subTest(start=start, end=end):
                result = ads.BUNDLED_TOOL.execute("get_performance", {"account_id": ACCOUNT, "campaign_id": CAMPAIGN, "start_date": start, "end_date": end}, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(self.meta.calls, [])
                self.diagnostics.assert_not_called()

    def queue(self, value=None):
        result = ads.BUNDLED_TOOL.execute('launch_campaign', value or launch_input(), self.api)
        self.assertIsInstance(result, ActionPendingApproval, result)
        self.assertFalse(self.meta.writes)
        return self.api.approvals.approve(result.approval_id)

    def diagnose(self, **value):
        result = ads.BUNDLED_TOOL.execute('diagnose_account', {'account_id': ACCOUNT, **value}, self.api)
        self.assertIsInstance(result, ActionExecuted, result)
        assert_matches_output_schema(self, ads.MANIFEST, 'diagnose_account', result)
        self.assertFalse(self.meta.writes)
        self.assertTrue(all(call[0] == 'GET' for call in self.meta.calls))
        return result.result

    def test_diagnostic_preserves_task_shape_and_existing_launch_guard(self):
        cases = [('missing', None), ('null', None), ('empty', []), ('invalid', 'MANAGE'),
                 ('invalid', ['MANAGE', 1]), ('present', ['ANALYZE']), ('present', ['ADVERTISE'])]
        for state, tasks in cases:
            with self.subTest(state=state, tasks=tasks):
                self.meta.account.pop('user_tasks', None)
                if state != 'missing':
                    self.meta.account['user_tasks'] = tasks
                result = self.diagnose()
                self.assertEqual(result['user_tasks_state'], state)
                passes = bool({'ADVERTISE', 'MANAGE'}.intersection(ads._strings(tasks)))
                self.assertEqual(result['launch_task_check_passes'], passes)
                self.assertEqual(result['account']['user_tasks'], ads._strings(tasks))
                self.assertEqual(result['connection']['facebook_user_id'], '900')
                self.assertEqual(result['connection']['scopes'], sorted(ads.SCOPES))
                launch = ads.BUNDLED_TOOL.execute('launch_campaign', launch_input(), self.api)
                self.assertIsInstance(launch, ActionPendingApproval if passes else ActionFailed)
                self.assertFalse(self.meta.writes)
                self.assertNotIn('fingerprint', json.dumps(result))
                self.assertNotIn('billing-id', json.dumps(result))

    def test_diagnostic_unfiltered_pages_explain_identity_filtering(self):
        cases = [('linked_identity_missing', {}), ('linked_identity_null', {'instagram_business_account': None}),
                 ('linked_identity_invalid', {'instagram_business_account': []}),
                 ('account_type_missing', {'instagram_business_account': {'id': INSTAGRAM}}),
                 ('account_type_null', {'instagram_business_account': {'account_type': None}}),
                 ('account_type_invalid', {'instagram_business_account': {'account_type': 1}}),
                 ('account_type_invalid', {'instagram_business_account': {'account_type': ''}}),
                 ('account_type_unsupported', {'instagram_business_account': {'account_type': 'PERSONAL'}}),
                 ('invalid_id', {'instagram_business_account': {'account_type': 'BUSINESS', 'id': 'bad'}}),
                 ('none', self.meta.page)]
        rows = [{'id': PAGE, 'name': 'Page', **row} for _, row in cases]
        def response(method, url, **kwargs):
            if '/promote_pages?' in url:
                return {'data': deepcopy(rows)}
            return self.meta(method, url, **kwargs)
        with patch.object(ads, 'json_request', response):
            result = self.diagnose()
        self.assertEqual([row['identity_filter_reason'] for row in result['pages']['items']], [reason for reason, _ in cases])
        self.assertEqual(result['pages']['items'][0]['linked_identity_state'], 'missing')
        self.assertEqual(result['pages']['items'][3]['account_type_state'], 'missing')
        self.assertEqual(result['instagram_accounts']['items'][0]['instagram_user_id'], INSTAGRAM)

    def test_diagnostic_empty_pages_are_distinct_from_filtered_pages(self):
        self.meta.page.pop('instagram_business_account')
        filtered = self.diagnose()
        normal = ads.BUNDLED_TOOL.execute('list_identities', {'account_id': ACCOUNT}, self.api)
        self.assertEqual(normal.result['items'], [])
        self.assertEqual(len(filtered['pages']['items']), 1)
        def response(method, url, **kwargs):
            if '/promote_pages?' in url:
                return {'data': []}
            return self.meta(method, url, **kwargs)
        with patch.object(ads, 'json_request', response):
            empty = self.diagnose()
        self.assertEqual(empty['pages']['status'], 'ok')
        self.assertEqual(empty['pages']['items'], [])

    def test_diagnostic_edge_errors_are_numeric_partial_and_never_retried(self):
        for edge in ('promote_pages', 'connected_instagram_accounts'):
            for envelope in (True, False):
                with self.subTest(edge=edge, envelope=envelope):
                    rejected = []
                    def response(method, url, **kwargs):
                        if '/' + edge + '?' in url:
                            rejected.append(url)
                            body = {'error': {'code': 100, 'error_subcode': 33, 'message': 'meta-token secret URL'},
                                    'data': [{'access_token': 'secret'}]}
                            if envelope:
                                return body
                            raise WebRequestError('secret transport text', status=400, body=json.dumps(body).encode())
                        return self.meta(method, url, **kwargs)
                    self.diagnostics.reset_mock()
                    with patch.object(ads, 'json_request', response):
                        result = self.diagnose()
                    failed_key = 'pages' if edge == 'promote_pages' else 'instagram_accounts'
                    failed = result[failed_key]
                    self.assertEqual(failed, {'status': 'failed', 'items': [], 'next_cursor': None,
                                              'http_status': 200 if envelope else 400, 'error_code': 100, 'error_subcode': 33})
                    self.assertEqual(result['instagram_accounts' if failed_key == 'pages' else 'pages']['status'], 'ok')
                    self.assertEqual(len(rejected), 1)
                    self.assertNotIn('secret', json.dumps(result))
                    self.assertNotIn('meta-token', json.dumps(result))
                    self.diagnostics.assert_called_once()

    def test_diagnostic_malformed_and_oversized_edges_are_unavailable(self):
        for listing in ({}, {'data': [None]}, {'data': [{}] * 21},
                        {'data': [], 'paging': {'next': 'https://provider.example/?token=secret'}}):
            with self.subTest(listing=listing):
                def response(method, url, **kwargs):
                    if '/promote_pages?' in url:
                        return listing
                    return self.meta(method, url, **kwargs)
                with patch.object(ads, 'json_request', response):
                    result = self.diagnose()
                self.assertEqual(result['pages']['status'], 'failed')
                self.assertEqual(result['instagram_accounts']['status'], 'ok')
                self.assertIsNone(result['pages']['error_code'])

    def test_diagnostic_cursors_are_independent_and_guarded_before_reads(self):
        self.meta.page_cursor = 'next_cursor'
        result = self.diagnose(limit=1, pages_after='page_cursor', instagram_after='ig_cursor')
        calls = {path: fields for method, path, fields, _ in self.meta.calls}
        self.assertEqual(calls['/act_100/promote_pages']['after'], 'page_cursor')
        self.assertEqual(calls['/act_100/connected_instagram_accounts']['after'], 'ig_cursor')
        self.assertEqual(calls['/act_100/promote_pages']['limit'], '1')
        self.assertEqual(result['pages']['next_cursor'], 'next_cursor')
        self.assertEqual(result['instagram_accounts']['next_cursor'], 'next_cursor')
        self.meta.calls.clear()
        self.diagnose(pages_after='page_cursor')
        calls = {path: fields for method, path, fields, _ in self.meta.calls}
        self.assertEqual(len(self.meta.calls), 5)
        self.assertNotIn('after', calls['/act_100/connected_instagram_accounts'])
        for cursor in ('pages_after', 'instagram_after'):
            self.meta.calls.clear()
            with patch.object(self.api.outbound, 'guard_request_parameter_string', side_effect=RuntimeError('guard denied')) as guard:
                denied = ads.BUNDLED_TOOL.execute('diagnose_account', {'account_id': ACCOUNT, cursor: 'cursor'}, self.api)
            self.assertIsInstance(denied, ActionFailed)
            guard.assert_called_once_with('cursor', allow_machine_tokens=True)
            self.assertEqual(self.meta.calls, [])
            for bad in ('https://example.com/', '', 1):
                denied = ads.BUNDLED_TOOL.execute('diagnose_account', {'account_id': ACCOUNT, cursor: bad}, self.api)
                self.assertIsInstance(denied, ActionFailed)
                self.assertEqual(self.meta.calls, [])
        for value in ({'limit': 21}, {'limit': True}, {'after': 'cursor'}, {'account_id': 'act_100'}, {'path': '/me'}):
            denied = ads.BUNDLED_TOOL.execute('diagnose_account', {'account_id': ACCOUNT, **value}, self.api)
            self.assertIsInstance(denied, ActionFailed)
            self.assertEqual(self.meta.calls, [])

    def test_diagnostic_auth_revocation_aborts_instead_of_partial_success(self):
        def response(method, url, **kwargs):
            if '/promote_pages?' in url:
                return {'error': {'code': 190, 'message': 'private'}}
            return self.meta(method, url, **kwargs)
        with patch.object(ads, 'json_request', response):
            result = ads.BUNDLED_TOOL.execute('diagnose_account', {'account_id': ACCOUNT}, self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIsNone(self.api.credentials.record)
        self.assertFalse(any('/connected_instagram_accounts' in call[1] for call in self.meta.calls))
        self.assertFalse(self.meta.writes)

    def test_http_json_and_reconnect_errors_have_operator_only_diagnostics(self):
        for status, code in ((400, 100), (400, 190), (403, 200), (429, 4), (500, 2), (200, 100), (200, 190)):
            with self.subTest(status=status, code=code):
                self.api = connected_api()
                error = {'error': {'code': code, 'message': 'provider-only detail'}}
                def reject(*args, **kwargs):
                    if status == 200:
                        return error
                    raise WebRequestError('Meta Marketing API request failed.', status=status, body=json.dumps(error).encode())
                self.diagnostics.reset_mock()
                with patch.object(ads, 'json_request', side_effect=reject) as request:
                    result = ads.BUNDLED_TOOL.execute('list_accounts', {}, self.api)
                request.assert_called_once()
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(result.reconnect_required, code == 190)
                self.assertNotIn('provider-only detail', result.error)
                self.diagnostics.assert_called_once()
                context = self.diagnostics.call_args.args[0]['context']
                self.assertEqual(context['http_status'], status)
                self.assertEqual(context['operation'], 'GET /me')
                self.assertIn('provider-only detail', context['provider_response'])

    def test_end_failure_logs_exact_phase_and_keeps_uncertain_result(self):
        pending = ads.BUNDLED_TOOL.execute('end_campaign', {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN}, self.api)
        approval = self.api.approvals.approve(pending.approval_id)
        def reject(method, url, **kwargs):
            if method == 'POST':
                raise WebRequestError('Meta Marketing API request failed.', status=400,
                    body=b'{"error":{"code":100,"message":"provider-only detail"}}')
            return self.meta(method, url, **kwargs)
        self.diagnostics.reset_mock()
        with patch.object(ads, 'json_request', reject):
            result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn('pause is unconfirmed', result.error)
        self.assertNotIn('provider-only detail', result.error)
        self.diagnostics.assert_called_once()
        context = self.diagnostics.call_args.args[0]['context']
        self.assertEqual(context['phase'], 'ending campaign')
        self.assertIn(CAMPAIGN, context['confirmed_resources'])
        self.assertIn('provider-only detail', context['provider_response'])

    def test_oauth_exchange_failure_records_only_provider_error(self):
        redirect = 'https://kern.example/tool-oauth/instagram_ads/callback'
        start = ads.CREDENTIALS.start_connect({'redirect_uri': redirect}, self.api)
        with patch.object(ads, 'json_request', side_effect=WebRequestError('Meta OAuth exchange failed.', status=400,
                body=b'{"error":{"code":100,"message":"provider-only detail"}}')) as exchange:
            with self.assertRaises(RuntimeError) as caught:
                ads.CREDENTIALS.complete_connect({'redirect_uri': redirect, 'state': start['state'], 'code': 'private-code'}, self.api)
        exchange.assert_called_once()
        self.diagnostics.assert_called_once()
        context = self.diagnostics.call_args.args[0]['context']
        self.assertEqual(context['action_id'], 'oauth_complete_connect')
        self.assertEqual(context['operation'], 'GET oauth/access_token')
        self.assertIn('provider-only detail', context['provider_response'])
        self.assertNotIn('provider-only detail', str(caught.exception))
        for private in ('private-code', redirect, 'meta-secret', 'meta-token'):
            self.assertNotIn(private, str(context))

    def test_real_host_approval_accepts_bounded_summaries_and_preserves_long_names(self):
        def insert(tool_id, action_id, summary, payload, created_at, **kwargs):
            return {'approval_id': 'host-approval', 'action_id': action_id,
                    'status': 'pending', 'summary': summary, 'payload': deepcopy(payload),
                    'created_at': created_at, 'decided_at': None}
        api = replace(self.api, approvals=tools_host.HostApprovals(ads.MANIFEST, tools_host.NO_CONNECTION, None))
        with patch.object(tools_host.state, 'insert_tool_approval', side_effect=insert) as persisted, \
             patch.object(tools_host.approval_assessment, 'schedule') as scheduled:
            value = launch_input('WEBSITE_CLICKS')
            value.update(name='\U0001f642' * 120, lifetime_budget=str(2**63 - 1))
            result = ads.BUNDLED_TOOL.execute('launch_campaign', value, api)
            self.assertIsInstance(result, ActionPendingApproval, result)
            proposal = persisted.call_args.args[3]['proposal']
            self.assertEqual(proposal['input']['name'], value['name'])
            self.assertEqual(proposal['source_reel']['caption'], self.meta.media['caption'])
            self.assertEqual(proposal['call_to_action']['value']['link'], value['destination_url'])
            # Exercise the host's actual byte guard at the maximum numeric sizes.
            proposal['input'].update(account_id='9' * 30, media_id='9' * 30)
            api.approvals.request(action_id='launch_campaign', summary=ads._summary(proposal),
                                  payload={'tool_id': 'instagram_ads', 'proposal': proposal})
            self.meta.resources[CAMPAIGN]['name'] = '\U0001f642' * 1000
            result = ads.BUNDLED_TOOL.execute('end_campaign', {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN}, api)
            self.assertIsInstance(result, ActionPendingApproval, result)
            self.assertEqual(persisted.call_args.args[3]['campaign_name'], self.meta.resources[CAMPAIGN]['name'])
            self.assertEqual(scheduled.call_count, 3)
            self.assertFalse(self.meta.writes)

    def test_all_four_routes_preserve_source_and_activate_parent_last(self):
        for objective, expected in ads.OBJECTIVES.items():
            with self.subTest(objective=objective):
                self.meta.writes.clear()
                value = launch_input(objective)
                value['audience']['interest_ids'] = ['1000']
                approval = self.queue(value)
                self.assertIn('impressions', approval.summary)
                proposal = approval.payload['proposal']
                self.assertEqual(proposal['source_reel']['caption'], self.meta.media['caption'])
                self.assertEqual(proposal['minimum_lifetime_budget'], '100')
                def children_stay_under_paused_parent(path, fields):
                    if path in ('/' + AD, '/' + ADSET):
                        self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                self.meta.write_hook = children_stay_under_paused_parent
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                self.assertIsInstance(result, ApprovalExecuted, result)
                self.assertIn('does not prove', result.message)
                paths = [path for path, _ in self.meta.writes]
                self.assertEqual(paths, ['/act_100/campaigns', '/act_100/adsets', '/act_100/adcreatives', '/act_100/ads', '/800', '/600', '/500'])
                campaign, adset, creative, ad = [row for _, row in self.meta.writes[:4]]
                self.assertEqual((campaign['objective'], adset['optimization_goal'], adset['destination_type']), expected)
                self.assertEqual(adset['billing_event'], 'IMPRESSIONS')
                self.assertEqual(adset['bid_strategy'], 'LOWEST_COST_WITHOUT_CAP')
                if objective in ('PROFILE_VISITS', 'ENGAGEMENTS'):
                    self.assertEqual(adset['promoted_object'], {'page_id': PAGE})
                self.assertEqual(adset['targeting']['publisher_platforms'], ['instagram'])
                self.assertEqual(adset['targeting']['instagram_positions'], ['reels'])
                self.assertEqual(adset['targeting']['geo_locations']['location_types'], ['home', 'recent'])
                self.assertEqual(adset['targeting']['age_max'], 65)
                self.assertEqual(adset['targeting']['age_range'], [18, 65])
                self.assertEqual(adset['targeting']['targeting_automation'], {'advantage_audience': 1})
                self.assertEqual([row['status'] for row in (campaign, adset, ad)], ['PAUSED'] * 3)
                self.assertEqual(creative['source_instagram_media_id'], MEDIA)
                self.assertEqual(creative['instagram_user_id'], INSTAGRAM)
                self.assertEqual(creative['object_id'], PAGE)
                self.assertEqual(adset['campaign_id'], CAMPAIGN)
                self.assertEqual(ad['adset_id'], ADSET)
                self.assertEqual(ad['creative'], {'creative_id': CREATIVE})
                self.assertNotIn('body', creative)
                if objective in ('WEBSITE_CLICKS', 'PROFILE_VISITS'):
                    self.assertEqual(creative['call_to_action'], proposal['call_to_action'])
                else:
                    self.assertNotIn('call_to_action', creative)
                self.assertEqual(self.api.costs.calls, [])

    def test_no_write_on_ownership_access_or_eligibility_failure(self):
        mutations = (
            lambda: self.meta.media['owner'].update(id='999'),
            lambda: self.meta.media.update(media_type='IMAGE'),
            lambda: self.meta.media['boost_eligibility_info'].update(eligible_to_boost=False),
            lambda: self.meta.page['instagram_business_account'].update(id='999'),
            lambda: self.meta.account.update(user_tasks=['ANALYZE']),
            lambda: self.meta.account.update(funding_source='0'),
            lambda: self.meta.account.update(account_status=2),
        )
        for mutate in mutations:
            self.meta = MetaFixture()
            with self.subTest(mutation=mutate), patch.object(ads, 'json_request', self.meta):
                mutate()
                value = launch_input()
                result = ads.BUNDLED_TOOL.execute('launch_campaign', value, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertFalse(self.meta.writes)
        self.assertEqual(self.api.approvals.counter, 0)

    def test_each_new_campaign_uses_fresh_targeting_without_saved_audience_calls(self):
        self.meta.account.update(default_dsa_beneficiary='Public beneficiary', default_dsa_payor='Public payer')
        choices = ({'countries': ['US'], 'interest_ids': ['1000']},
                   {'country_group': 'worldwide', 'advantage_plus': False})
        proposals = []
        for audience in choices:
            with self.subTest(audience=audience):
                self.meta.writes.clear()
                value = launch_input()
                value['audience'] = deepcopy(audience)
                approval = self.queue(value)
                proposals.append(deepcopy(approval.payload['proposal']))
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                self.assertIsInstance(result, ApprovalExecuted, result)
                target = self.meta.resources[ADSET]['targeting']
                expected_geo = {'countries': ['US']} if 'countries' in audience else {'country_groups': ['worldwide']}
                self.assertEqual(target['geo_locations'], {**expected_geo, 'location_types': ['home', 'recent']})
                self.assertNotIn('custom_audiences', target)
                self.assertNotIn('audiences', approval.payload['proposal']['audience'])
                if 'interest_ids' in audience:
                    self.assertEqual(target['flexible_spec'], [{'interests': [{'id': '1000'}]}])
                else:
                    self.assertNotIn('flexible_spec', target)
                    self.assertEqual(target['targeting_automation'], {'advantage_audience': 0})
        self.assertNotEqual(proposals[0]['audience']['targeting'], proposals[1]['audience']['targeting'])
        for edge in ('campaigns', 'adsets', 'adcreatives', 'ads'):
            self.assertEqual(sum(method == 'POST' and path == '/act_100/' + edge for method, path, _, _ in self.meta.calls), 2)
        self.assertFalse(any('/customaudiences' in path for _, path, _, _ in self.meta.calls))

    def test_removed_saved_audience_action_and_input_fail_before_provider_reads(self):
        self.assertIsNone(ads.MANIFEST.action('list_audiences'))
        self.assertEqual(len(ads.MANIFEST.actions), 11)
        schema = ads.MANIFEST.action('launch_campaign').input_schema
        self.assertEqual(len(schema['properties']), 12)
        self.assertEqual(set(schema['properties']['audience']['properties']),
                         {'countries', 'country_group', 'interest_ids', 'advantage_plus'})
        result = ads.BUNDLED_TOOL.execute('list_audiences', {'account_id': ACCOUNT}, self.api)
        self.assertIsInstance(result, ActionFailed)
        value = launch_input()
        value['audience']['audience_ids'] = ['1001']
        result = ads.BUNDLED_TOOL.execute('launch_campaign', value, self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn('Unknown audience fields', result.error)
        self.assertFalse(self.meta.calls)
        self.assertFalse(self.meta.writes)
        self.assertEqual(self.api.approvals.counter, 0)

    def test_approval_binds_credential_access_caption_destination_and_budget(self):
        for kind in ('caption', 'scope', 'funding', 'token', 'grant', 'app', 'username', 'budget', 'dsa_default', 'access'):
            self.api, self.meta = connected_api(), MetaFixture()
            with self.subTest(kind=kind), patch.object(ads, 'json_request', self.meta):
                value = launch_input('WEBSITE_CLICKS')
                if kind == 'dsa_default':
                    value['audience'] = {'countries': ['DE']}
                    self.meta.account.update(default_dsa_beneficiary='Original', default_dsa_payor='Payer')
                approval = self.queue(value)
                if kind == 'caption': self.meta.media['caption'] = 'Changed'
                elif kind == 'scope': self.meta.permissions.remove('ads_management')
                elif kind == 'funding': self.meta.account['funding_source'] = 'other-billing'
                elif kind == 'token': self.api.credentials.record['secret']['access_token'] = 'new-token'
                elif kind == 'grant': self.api.credentials.record['metadata']['grant_id'] = 'reconnected'
                elif kind == 'app': self.api.config['INSTAGRAM_ADS_APP_SECRET'] = 'changed'
                elif kind == 'dsa_default': self.meta.account['default_dsa_beneficiary'] = 'Changed'
                elif kind == 'access': self.meta.account['user_tasks'] = ['ANALYZE']
                elif kind == 'username': self.meta.page['instagram_business_account']['username'] = 'changed'
                else: self.meta.minimum = 60
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertFalse(self.meta.writes)

    def test_rotating_source_signature_is_not_content_drift(self):
        approval = self.queue()
        self.meta.media['media_url'] = 'https://cdninstagram.com/source.mp4?signature=second'
        result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
        self.assertIsInstance(result, ApprovalExecuted, result)

    def test_each_ambiguous_write_is_terminal_with_confirmed_ids(self):
        labels = ('campaign creation', 'ad-set creation', 'creative creation', 'ad creation',
                  'ad activation', 'ad-set activation', 'parent activation')
        for index, label in enumerate(labels, 1):
            self.meta = MetaFixture()
            with self.subTest(stage=label), patch.object(ads, 'json_request', self.meta):
                approval = self.queue()
                self.meta.fail_write = index
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertIn(label, result.error)
                self.assertIn('Confirmed ids:', result.error)
                self.assertIn('No automatic retry', result.error)
                self.assertNotIn('meta-token', result.error)
                self.assertNotIn('provider-body-secret', result.error)
                self.assertEqual(len(self.meta.writes), index)
                if index > 1: self.assertIn('campaign_id', result.error)
                if index <= 4: self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED' if index > 1 else 'ACTIVE')

    def test_unconfirmed_creation_ids_never_activate_partial_hierarchy(self):
        for index, edge in enumerate(('campaigns', 'adsets', 'adcreatives', 'ads'), 1):
            self.api, self.meta = connected_api(), MetaFixture()
            def missing_id(method, url, **kwargs):
                result = self.meta(method, url, **kwargs)
                return {} if method == 'POST' and urlsplit(url).path.endswith('/' + edge) else result
            with self.subTest(edge=edge), patch.object(ads, 'json_request', missing_id):
                approval = self.queue()
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                self.assertIsInstance(result, ActionFailed, result)
                self.assertEqual(len(self.meta.writes), index)
                self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                self.assertFalse(any(path == '/' + CAMPAIGN for path, _ in self.meta.writes))

    def test_incomplete_associations_and_required_fields_stop_both_barriers(self):
        for phase in ('paused', 'active_children'):
            for resource, field in ((CAMPAIGN, 'objective'), (ADSET, 'campaign_id'),
                                    (CREATIVE, 'source_instagram_media_id'), (AD, 'creative')):
                self.api, self.meta = connected_api(), MetaFixture()
                with self.subTest(phase=phase, resource=resource), patch.object(ads, 'json_request', self.meta):
                    approval = self.queue()
                    def hook(path, fields):
                        if path == ('/act_100/ads' if phase == 'paused' else '/' + ADSET):
                            self.meta.resources[resource].pop(field)
                    self.meta.write_hook = hook
                    self.diagnostics.reset_mock()
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                    self.diagnostics.assert_called_once()
                    context = self.diagnostics.call_args.args[0]['context']
                    self.assertNotIn('http_status', context)
                    self.assertIn(CAMPAIGN, context['confirmed_resources'])
                    self.assertIn(context['phase'], result.error)
                    self.assertIsInstance(result, ActionFailed, result)
                    self.assertEqual(len(self.meta.writes), 4 if phase == 'paused' else 6)
                    self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                    self.assertFalse(any(path == '/' + CAMPAIGN for path, _ in self.meta.writes))

    def test_unavailable_verification_reads_never_activate_parent(self):
        paths = ('/' + CAMPAIGN, '/' + ADSET, '/' + CREATIVE, '/' + AD,
                 '/' + CAMPAIGN + '/adsets', '/' + ADSET + '/ads')
        for phase in ('paused', 'active_children'):
            for unavailable in paths:
                self.api, self.meta = connected_api(), MetaFixture()
                with self.subTest(phase=phase, unavailable=unavailable), patch.object(ads, 'json_request', self.meta):
                    approval = self.queue()
                    def read_hook(path):
                        ad = self.meta.resources.get(AD)
                        if path == unavailable and ad and ad['status'] == ('PAUSED' if phase == 'paused' else 'ACTIVE'):
                            raise WebRequestError('unavailable provider secret', status=503)
                    self.meta.read_hook = read_hook
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                    self.assertIsInstance(result, ActionFailed, result)
                    self.assertEqual(len(self.meta.writes), 4 if phase == 'paused' else 6)
                    self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                    self.assertFalse(any(path == '/' + CAMPAIGN for path, _ in self.meta.writes))
                    self.assertNotIn('unavailable provider secret', result.error)

    def test_regional_api_rejection_retains_geography_and_paused_parent(self):
        for audience in ({'countries': ['DE']}, {'country_group': 'worldwide'}):
            for transport in ('http_error', 'json_error'):
                self.api, self.meta = connected_api(), MetaFixture()
                posted = []
                submitted_geo = []
                error = {'code': 100, 'error_subcode': 3858634, 'message': 'provider secret meta-token'}
                def reject(method, url, **kwargs):
                    path = urlsplit(url).path.removeprefix('/v25.0')
                    if method == 'POST':
                        posted.append(path)
                        if path == '/act_100/adsets':
                            submitted_geo.append(json.loads(kwargs['form']['targeting'])['geo_locations'])
                            if transport == 'http_error':
                                raise WebRequestError('provider secret', status=400,
                                    body=json.dumps({'error': error}).encode())
                            return {'error': error}
                    return self.meta(method, url, **kwargs)
                with self.subTest(audience=audience, transport=transport), patch.object(ads, 'json_request', reject):
                    value = launch_input()
                    value['audience'] = deepcopy(audience)
                    self.meta.account.update(default_dsa_beneficiary='Owned brand', default_dsa_payor='Advertiser')
                    approval = self.queue(value)
                    approved_geo = approval.payload['proposal']['audience']['targeting']['geo_locations']
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                    self.assertIsInstance(result, ActionFailed, result)
                    self.assertIn('ad-set creation', result.error)
                    self.assertIn('campaign_id', result.error)
                    self.assertIn('code=100', result.error)
                    self.assertIn('error_subcode=3858634', result.error)
                    self.assertIn('per-request declarations', result.error)
                    self.assertNotIn('provider secret', result.error)
                    self.assertNotIn('meta-token', result.error)
                    self.assertEqual(posted, ['/act_100/campaigns', '/act_100/adsets'])
                    self.assertEqual(submitted_geo, [approved_geo])
                    self.assertNotIn('excluded_geo_locations', approved_geo)
                    self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')

    def test_post_creation_drift_stops_before_parent_activation(self):
        for drift in ('placement', 'budget', 'cta', 'enhancement', 'caption', 'new_child', 'resource', 'credential', 'geography', 'advantage', 'dynamic'):
            self.api, self.meta = connected_api(), MetaFixture()
            with self.subTest(drift=drift), patch.object(ads, 'json_request', self.meta):
                approval = self.queue(launch_input('WEBSITE_CLICKS'))
                def hook(path, fields):
                    if path != '/act_100/ads': return
                    if drift == 'placement': self.meta.resources[ADSET]['targeting']['instagram_positions'].append('stream')
                    elif drift == 'geography': self.meta.resources[ADSET]['targeting']['geo_locations']['countries'] = ['DE']
                    elif drift == 'advantage': self.meta.resources[ADSET]['targeting']['targeting_automation']['advantage_audience'] = 0
                    elif drift == 'dynamic': self.meta.resources[ADSET]['is_dynamic_creative'] = True
                    elif drift == 'budget': self.meta.resources[ADSET]['lifetime_budget'] = '9999'
                    elif drift == 'cta': self.meta.resources[CREATIVE]['call_to_action']['value']['link'] = 'https://changed.example/'
                    elif drift == 'enhancement': self.meta.resources[CREATIVE]['degrees_of_freedom_spec'] = {'creative_features_spec': {'text_optimizations': {'enroll_status': 'OPT_IN'}}}
                    elif drift == 'caption': self.meta.resources[CREATIVE]['body'] = 'Changed'
                    elif drift == 'resource': self.meta.media['caption'] = 'Changed after creation'
                    elif drift == 'credential': self.api.credentials.record['metadata']['grant_id'] = 'new'
                    else: self.meta.page_cursor = 'more'
                self.meta.write_hook = hook
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                self.assertFalse(any(path == '/500' for path, _ in self.meta.writes))

    def test_added_targeting_fields_stop_paused_and_active_child_readback(self):
        changes = (
            lambda targeting: targeting.update(custom_audiences=[{'id': '1001'}]),
            lambda targeting: targeting.update(excluded_geo_locations={'countries': ['DE']}),
            lambda targeting: targeting.update(genders=[1]),
            lambda targeting: targeting.update(device_platforms=['desktop']),
            lambda targeting: targeting.update(age_max=50),
            lambda targeting: targeting.update(age_range=[25, 35]),
            lambda targeting: targeting.update(age_range=[65, 18]),
            lambda targeting: targeting['geo_locations'].update(location_types=['home']),
            lambda targeting: targeting['targeting_automation'].update(lookalike=1),
            lambda targeting: targeting['flexible_spec'][0].update(behaviors=[{'id': '999'}]),
        )
        for phase in ('paused', 'active_children'):
            for change in changes:
                self.api, self.meta = connected_api(), MetaFixture()
                with self.subTest(phase=phase, change=change), patch.object(ads, 'json_request', self.meta):
                    value = launch_input()
                    value['audience']['interest_ids'] = ['1000']
                    approval = self.queue(value)
                    def hook(path, fields):
                        if path == ('/act_100/ads' if phase == 'paused' else '/600'):
                            change(self.meta.resources[ADSET]['targeting'])
                    self.meta.write_hook = hook
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                    self.assertIsInstance(result, ActionFailed, result)
                    self.assertIn('targeting', result.error)
                    self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                    self.assertFalse(any(path == '/500' for path, _ in self.meta.writes))

    def test_targeting_reference_names_do_not_change_approved_ids(self):
        value = launch_input()
        value['audience']['interest_ids'] = ['1000']
        approval = self.queue(value)
        def hook(path, fields):
            if path == '/act_100/ads':
                targeting = self.meta.resources[ADSET]['targeting']
                targeting['flexible_spec'][0]['interests'][0]['name'] = 'Provider label'
        self.meta.write_hook = hook
        self.assertIsInstance(ads.BUNDLED_TOOL.execute_approved(approval, self.api), ApprovalExecuted)

    def test_engagement_page_promoted_object_drift_stops_parent_activation(self):
        for phase in ('paused', 'active_children'):
            self.api, self.meta = connected_api(), MetaFixture()
            with self.subTest(phase=phase), patch.object(ads, 'json_request', self.meta):
                approval = self.queue(launch_input('ENGAGEMENTS'))
                def hook(path, fields):
                    if path == ('/act_100/ads' if phase == 'paused' else '/600'):
                        self.meta.resources[ADSET]['promoted_object']['page_id'] = '999'
                self.meta.write_hook = hook
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                self.assertIsInstance(result, ActionFailed, result)
                self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                self.assertFalse(any(path == '/500' for path, _ in self.meta.writes))

    def test_approval_delay_requires_token_lifetime_for_complete_launch(self):
        for remaining in (40, 61, 180, 240, 241):
            self.api, self.meta = connected_api(), MetaFixture()
            self.api.credentials.record['secret']['expires_at'] = 1600
            with self.subTest(remaining=remaining), patch.object(ads, 'json_request', self.meta):
                with patch.object(ads, 'now', return_value=1000):
                    approval = self.queue()
                before = len(self.meta.calls)
                with patch.object(ads, 'now', return_value=1600 - remaining):
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                if remaining > ads.INTERACTIVE_BUDGET_SECONDS:
                    self.assertIsInstance(result, ApprovalExecuted, result)
                else:
                    self.assertIsInstance(result, ActionFailed, result)
                    self.assertTrue(result.reconnect_required)
                    self.assertEqual(len(self.meta.calls), before)
                    self.assertFalse(self.meta.writes)

    def test_provider_token_expiry_during_launch_stops_with_ids_and_reconnect(self):
        approval = self.queue()
        def expired(method, url, **kwargs):
            if len(self.meta.writes) == 4:
                raise WebRequestError('raw expired token', status=400,
                    body=b'{"error":{"code":190,"error_subcode":463,"message":"raw expired token"}}')
            return self.meta(method, url, **kwargs)
        with patch.object(ads, 'json_request', expired):
            result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
        self.assertIsInstance(result, ActionFailed, result)
        self.assertTrue(result.reconnect_required)
        self.assertIn('paused configuration verification', result.error)
        for key in ('campaign_id', 'adset_id', 'creative_id', 'ad_id'):
            self.assertIn(key, result.error)
        self.assertNotIn('raw expired token', result.error)
        self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
        self.assertEqual(len(self.meta.writes), 4)
        self.assertIsNone(self.api.credentials.record)

    def test_launch_time_margin_survives_approval_delay_and_precreation_reads(self):
        initial = datetime.now(timezone.utc).replace(microsecond=0)
        for remaining in (0, 1, 239, 240, 241):
            self.api, self.meta = connected_api(), MetaFixture()
            value = launch_input()
            value['start_time'] = (initial + timedelta(seconds=600)).isoformat()
            with self.subTest(remaining=remaining), patch.object(ads, 'json_request', self.meta), \
                 patch.object(ads, 'datetime', wraps=datetime) as clock:
                clock.now.return_value = initial
                approval = self.queue(value)
                before = len(self.meta.calls)
                clock.now.return_value = initial + timedelta(seconds=600 - remaining)
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                if remaining > ads.INTERACTIVE_BUDGET_SECONDS:
                    self.assertIsInstance(result, ApprovalExecuted, result)
                else:
                    self.assertIsInstance(result, ActionFailed, result)
                    self.assertIn('Flight start', result.error)
                    self.assertEqual(len(self.meta.calls), before)
                    self.assertFalse(self.meta.writes)
        self.api, self.meta = connected_api(), MetaFixture()
        value['start_time'] = (initial + timedelta(seconds=300)).isoformat()
        with patch.object(ads, 'json_request', self.meta), patch.object(ads, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = initial
            approval = self.queue(value)
            def reads_use_time(path):
                if path == '/' + MEDIA:
                    clock.now.return_value = initial + timedelta(seconds=70)
            self.meta.read_hook = reads_use_time
            result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
            self.assertIsInstance(result, ActionFailed, result)
            self.assertIn('Flight start', result.error)
            self.assertFalse(self.meta.writes)

    def test_parent_activation_rechecks_flight_and_remaining_readback_budget(self):
        initial = datetime.now(timezone.utc).replace(microsecond=0)
        for exhausted in ('flight', 'interactive_budget'):
            self.api, self.meta = connected_api(), MetaFixture()
            value = launch_input()
            value['start_time'] = (initial + timedelta(seconds=300)).isoformat()
            with self.subTest(exhausted=exhausted), patch.object(ads, 'json_request', self.meta), \
                 patch.object(ads, 'datetime', wraps=datetime) as clock, \
                 patch.object(ads.time, 'monotonic', return_value=0) as elapsed:
                clock.now.return_value = initial
                approval = self.queue(value)
                def hook(path, fields):
                    if path == '/600':
                        if exhausted == 'flight':
                            clock.now.return_value = initial + timedelta(seconds=70)
                        else:
                            elapsed.return_value = 211
                self.meta.write_hook = hook
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                self.assertIsInstance(result, ActionFailed, result)
                self.assertIn('activation time verification', result.error)
                self.assertEqual(len(self.meta.writes), 6)
                self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')

    def test_pause_requires_full_token_budget_before_any_provider_request(self):
        for remaining in (40, 61, 90, 240, 241):
            self.api, self.meta = connected_api(), MetaFixture()
            self.api.credentials.record['secret']['expires_at'] = 1600
            with self.subTest(remaining=remaining), patch.object(ads, 'json_request', self.meta):
                with patch.object(ads, 'now', return_value=1000):
                    result = ads.BUNDLED_TOOL.execute('end_campaign', {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN}, self.api)
                    approval = self.api.approvals.approve(result.approval_id)
                before = len(self.meta.calls)
                with patch.object(ads, 'now', return_value=1600 - remaining):
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                if remaining > ads.INTERACTIVE_BUDGET_SECONDS:
                    self.assertIsInstance(result, ApprovalExecuted, result)
                else:
                    self.assertIsInstance(result, ActionFailed, result)
                    self.assertTrue(result.reconnect_required)
                    self.assertEqual(len(self.meta.calls), before)
                    self.assertFalse(self.meta.writes)

    def test_mutable_delivery_controls_are_projected_and_stop_parent_activation(self):
        changes = {
            'adset_schedule': [{'start_minute': 600, 'end_minute': 660, 'days': [1]}],
            'pacing_type': ['day_parting'],
            'frequency_control_specs': [{'event': 'IMPRESSIONS', 'interval_days': 7, 'max_frequency': 1}],
            'bid_amount': 500,
            'bid_constraints': {'roas_average_floor': 2},
            'optimization_sub_event': 'VIDEO_SOUND_ON',
            'is_budget_schedule_enabled': True,
            'daily_spend_cap': '500', 'lifetime_spend_cap': '500',
            'daily_min_spend_target': '500', 'lifetime_min_spend_target': '500',
            'spend_cap': '500',
        }
        for phase in ('paused', 'active_children'):
            for key, changed in changes.items():
                self.api, self.meta = connected_api(), MetaFixture()
                with self.subTest(phase=phase, field=key), patch.object(ads, 'json_request', self.meta):
                    approval = self.queue(launch_input('VIDEO_VIEWS'))
                    def hook(path, fields):
                        if path == ('/act_100/ads' if phase == 'paused' else '/600'):
                            resource = CAMPAIGN if key == 'spend_cap' else ADSET
                            self.meta.resources[resource][key] = deepcopy(changed)
                    self.meta.write_hook = hook
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                    self.assertIsInstance(result, ActionFailed, result)
                    self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                    self.assertFalse(any(path == '/500' for path, _ in self.meta.writes))

    def test_documented_empty_and_removed_delivery_controls_remain_usable(self):
        approval = self.queue()
        def hook(path, fields):
            if path == '/act_100/ads':
                self.meta.resources[ADSET].update(adset_schedule=[], pacing_type=['standard'],
                    frequency_control_specs=[], bid_amount=0, bid_constraints={},
                    optimization_sub_event='NONE',
                    daily_spend_cap='922337203685478', lifetime_spend_cap='922337203685478',
                    daily_min_spend_target='0', lifetime_min_spend_target='0')
                self.meta.resources[CAMPAIGN]['spend_cap'] = '0'
        self.meta.write_hook = hook
        self.assertIsInstance(ads.BUNDLED_TOOL.execute_approved(approval, self.api), ApprovalExecuted)

    def test_budget_scheduling_requires_explicit_disabled_readback_at_both_barriers(self):
        for phase in ('paused', 'active_children'):
            for returned in ('missing', None, False, True):
                self.api, self.meta = connected_api(), MetaFixture()
                with self.subTest(phase=phase, returned=returned), patch.object(ads, 'json_request', self.meta):
                    approval = self.queue()
                    def hook(path, fields):
                        if path == ('/act_100/ads' if phase == 'paused' else '/600'):
                            if returned == 'missing':
                                self.meta.resources[ADSET].pop('is_budget_schedule_enabled')
                            else:
                                self.meta.resources[ADSET]['is_budget_schedule_enabled'] = returned
                    self.meta.write_hook = hook
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                    posted = next(value for path, value in self.meta.writes if path == '/act_100/adsets')
                    self.assertIs(posted['is_budget_schedule_enabled'], False)
                    projections = [call[2]['fields'].split(',') for call in self.meta.calls if call[0] == 'GET' and call[1] == '/600']
                    self.assertEqual(len(projections), 2 if phase == 'active_children' or returned is False else 1)
                    self.assertTrue(all('is_budget_schedule_enabled' in fields for fields in projections))
                    if returned is False:
                        self.assertIsInstance(result, ApprovalExecuted, result)
                        self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'ACTIVE')
                    else:
                        self.assertIsInstance(result, ActionFailed, result)
                        self.assertIn('budget scheduling disabled', result.error)
                        self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                        self.assertFalse(any(path == '/500' for path, _ in self.meta.writes))

    def test_every_returned_cta_destination_and_identity_must_agree(self):
        for objective in ('WEBSITE_CLICKS', 'PROFILE_VISITS'):
            for phase in ('paused', 'active_children'):
                for drift in ('link_data', 'video_data', 'link', 'link_url', 'object_url', 'cta_type', 'extra_cta', 'page', 'instagram'):
                    self.api, self.meta = connected_api(), MetaFixture()
                    with self.subTest(objective=objective, phase=phase, drift=drift), patch.object(ads, 'json_request', self.meta):
                        approval = self.queue(launch_input(objective))
                        def hook(path, fields):
                            if path != ('/act_100/ads' if phase == 'paused' else '/600'):
                                return
                            creative = self.meta.resources[CREATIVE]
                            cta = deepcopy(creative['call_to_action'])
                            if drift == 'cta_type':
                                creative['call_to_action_type'] = 'SHOP_NOW'
                            elif drift in ('link_url', 'object_url'):
                                creative[drift] = 'https://unapproved.example/'
                            elif drift == 'extra_cta':
                                cta['value']['app_destination'] = 'INSTAGRAM_DIRECT'
                                creative['object_story_spec'] = {'video_data': {'call_to_action': cta}}
                            elif drift in ('page', 'instagram'):
                                creative['object_story_spec'] = {'page_id' if drift == 'page' else 'instagram_user_id': '999'}
                            else:
                                cta['value']['link'] = 'https://unapproved.example/'
                                creative['object_story_spec'] = ({'link_data': {'link': 'https://unapproved.example/'}}
                                    if drift == 'link' else {drift: {'call_to_action': cta}})
                        self.meta.write_hook = hook
                        result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                        self.assertIsInstance(result, ActionFailed, result)
                        self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                        self.assertFalse(any(path == '/500' for path, _ in self.meta.writes))

    def test_matching_top_and_nested_ctas_do_not_require_fallback(self):
        approval = self.queue(launch_input('WEBSITE_CLICKS'))
        def hook(path, fields):
            if path == '/act_100/ads':
                creative = self.meta.resources[CREATIVE]
                cta = creative['call_to_action']
                creative['call_to_action_type'] = cta['type']
                creative['link_url'] = cta['value']['link']
                creative['object_url'] = cta['value']['link']
                creative['object_story_spec'] = {'page_id': PAGE, 'instagram_user_id': INSTAGRAM,
                    'link_data': {'link': cta['value']['link'], 'call_to_action': deepcopy(cta)},
                    'video_data': {'call_to_action': deepcopy(cta)}}
        self.meta.write_hook = hook
        self.assertIsInstance(ads.BUNDLED_TOOL.execute_approved(approval, self.api), ApprovalExecuted)

    def test_additional_destination_spec_is_read_and_stops_parent_activation(self):
        for phase in ('paused', 'active_children'):
            for spec in (None, {}, 'UNRETURNED', [], False, {'website': {'url': 'https://unapproved.example/'}}):
                self.api, self.meta = connected_api(), MetaFixture()
                with self.subTest(phase=phase, spec=spec), patch.object(ads, 'json_request', self.meta):
                    approval = self.queue(launch_input('WEBSITE_CLICKS'))
                    def hook(path, fields):
                        if path == ('/act_100/ads' if phase == 'paused' else '/600'):
                            if spec == 'UNRETURNED':
                                self.meta.resources[CREATIVE].pop('destination_spec', None)
                            else:
                                self.meta.resources[CREATIVE]['destination_spec'] = deepcopy(spec)
                    self.meta.write_hook = hook
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                    if spec is not None and spec != {}:
                        self.assertIsInstance(result, ActionFailed, result)
                        self.assertIn('destination configuration', result.error)
                        self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                        self.assertFalse(any(path == '/500' for path, _ in self.meta.writes))
                    else:
                        self.assertIsInstance(result, ApprovalExecuted, result)

    def test_required_dsa_geography_uses_both_saved_defaults_outside_caller_input(self):
        defaults = {'default_dsa_beneficiary': 'Real beneficiary', 'default_dsa_payor': 'Real payer'}
        resolved = {'dsa_beneficiary': 'Real beneficiary', 'dsa_payor': 'Real payer'}
        for audience in ({'country_group': 'worldwide'}, {'countries': ['DE']}, {'countries': ['GF']}):
            for missing in defaults:
                for name in (None, '', ' ', 'x' * 513):
                    self.api, self.meta = connected_api(), MetaFixture()
                    self.meta.account.update(defaults)
                    self.meta.account[missing] = name
                    with self.subTest(audience=audience, missing=missing, name=name), patch.object(ads, 'json_request', self.meta):
                        value = launch_input()
                        value['audience'] = deepcopy(audience)
                        result = ads.BUNDLED_TOOL.execute('launch_campaign', value, self.api)
                        self.assertIsInstance(result, ActionFailed)
                        self.assertIn(missing, result.error)
                        self.assertIn('both saved public DSA names', result.error)
                        self.assertIn('Meta Ads Manager > Advertising settings', result.error)
                        self.assertEqual(self.api.approvals.counter, 0)
                        self.assertFalse(self.meta.writes)
            self.api, self.meta = connected_api(), MetaFixture()
            self.meta.account.update(defaults)
            with self.subTest(audience=audience, valid=True), patch.object(ads, 'json_request', self.meta):
                value = launch_input()
                value['audience'] = deepcopy(audience)
                approval = self.queue(value)
                proposal = approval.payload['proposal']
                self.assertEqual(proposal['dsa'], resolved)
                self.assertTrue(set(resolved).isdisjoint(proposal['input']))
                self.assertEqual(tools_host.validate_against_schema(proposal['input'], ads.MANIFEST.action('launch_campaign').input_schema), '')
                result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                self.assertIsInstance(result, ApprovalExecuted, result)
                adset = self.meta.resources[ADSET]
                self.assertEqual({key: adset[key] for key in resolved}, resolved)
                for call in self.meta.calls:
                    if call[0] == 'GET' and call[1] == '/act_100':
                        self.assertTrue(set(defaults).issubset(call[2]['fields'].split(',')))
        self.api, self.meta = connected_api(), MetaFixture()
        self.meta.account.update(defaults)
        with patch.object(ads, 'json_request', self.meta):
            approval = self.queue()
            self.assertEqual(approval.payload['proposal']['dsa'], {})
            self.assertIsInstance(ads.BUNDLED_TOOL.execute_approved(approval, self.api), ApprovalExecuted)
            self.assertTrue(set(resolved).isdisjoint(self.meta.resources[ADSET]))

    def test_saved_dsa_default_drift_stops_at_both_launch_barriers(self):
        for barrier in ('before_creation', 'before_parent'):
            for key in ('default_dsa_beneficiary', 'default_dsa_payor'):
                for change in ('replace', 'remove'):
                    self.api, self.meta = connected_api(), MetaFixture()
                    self.meta.account.update(default_dsa_beneficiary='Approved beneficiary', default_dsa_payor='Approved payer')
                    with self.subTest(barrier=barrier, key=key, change=change), patch.object(ads, 'json_request', self.meta):
                        value = launch_input()
                        value['audience'] = {'country_group': 'worldwide'}
                        approval = self.queue(value)
                        def mutate():
                            if change == 'replace':
                                self.meta.account[key] = 'Changed public name'
                            else:
                                self.meta.account.pop(key)
                        if barrier == 'before_creation':
                            mutate()
                        else:
                            def child_hook(path, fields):
                                if path == '/' + ADSET:
                                    mutate()
                            self.meta.write_hook = child_hook
                        result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                        self.assertIsInstance(result, ActionFailed)
                        self.assertFalse(any(path == '/' + CAMPAIGN for path, _ in self.meta.writes))
                        if barrier == 'before_creation':
                            self.assertFalse(self.meta.writes)
                        else:
                            self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                            self.assertIn('resource revalidation', result.error)

    def test_resolved_dsa_readback_must_match_at_both_paused_barriers(self):
        for children_active in (False, True):
            for key in ('dsa_beneficiary', 'dsa_payor'):
                self.api, self.meta = connected_api(), MetaFixture()
                self.meta.account.update(default_dsa_beneficiary='Approved beneficiary', default_dsa_payor='Approved payer')
                with self.subTest(children_active=children_active, key=key), patch.object(ads, 'json_request', self.meta):
                    value = launch_input()
                    value['audience'] = {'country_group': 'worldwide'}
                    approval = self.queue(value)
                    def corrupt(path, fields):
                        if path == ('/' + ADSET if children_active else '/act_100/ads'):
                            self.meta.resources[ADSET][key] = 'Unapproved public name'
                    self.meta.write_hook = corrupt
                    result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                    self.assertIsInstance(result, ActionFailed)
                    self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                    self.assertIn('configuration verification', result.error)
                    self.assertFalse(any(path == '/' + CAMPAIGN for path, _ in self.meta.writes))

    def test_nonregulated_dsa_readback_rejects_unapproved_public_names(self):
        for children_active in (False, True):
            for key in ('dsa_beneficiary', 'dsa_payor'):
                for name in ('Unapproved public name', '', None):
                    self.api, self.meta = connected_api(), MetaFixture()
                    with self.subTest(children_active=children_active, key=key, name=name), patch.object(ads, 'json_request', self.meta):
                        approval = self.queue()
                        self.assertEqual(approval.payload['proposal']['dsa'], {})
                        def change(path, fields):
                            if path == ('/' + ADSET if children_active else '/act_100/ads'):
                                self.meta.resources[ADSET][key] = name
                        self.meta.write_hook = change
                        result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
                        if name:
                            self.assertIsInstance(result, ActionFailed)
                            self.assertIn('public DSA names were not approved', result.error)
                            self.assertEqual(self.meta.resources[CAMPAIGN]['status'], 'PAUSED')
                            self.assertFalse(any(path == '/' + CAMPAIGN for path, _ in self.meta.writes))
                        else:
                            self.assertIsInstance(result, ApprovalExecuted, result)

    def test_currency_units_and_minimum_are_provider_based(self):
        for currency, offset in (('USD', 100), ('JPY', 1), ('BHD', 100)):
            self.meta.account['currency'] = currency
            approval = self.queue()
            self.assertEqual(approval.payload['proposal']['account']['currency_offset'], offset)
        too_small = launch_input()
        too_small['lifetime_budget'] = '99'
        result = ads.BUNDLED_TOOL.execute('launch_campaign', too_small, self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn('100 Meta budget units', result.error)
        self.meta.account['currency'] = 'UNKNOWN'
        self.assertIsInstance(ads.BUNDLED_TOOL.execute('launch_campaign', launch_input(), self.api), ActionFailed)

    def test_inputs_reject_unknown_fields_unbounded_and_unsupported_routes(self):
        changes = ({'bid_amount': '5'}, {'dsa_beneficiary': 'Caller name'}, {'dsa_payor': 'Caller name'},
                   {'special_ad_category': 'HOUSING'}, {'lifetime_budget': '1.25'},
                   {'lifetime_budget': str(2**63)}, {'audience': {'countries': []}},
                   {'audience': {'countries': ['US'], 'country_group': 'worldwide'}},
                   {'audience': {'countries': ['US'], 'competitor_handle': 'target'}},
                   {'audience': {'countries': ['US'], 'audience_ids': ['1001']}},
                   {'audience': {'countries': ['US'], 'interest_ids': ['1000'] * 11}},
                   {'destination_url': 'https://example.com/'}, {'start_time': '2020-01-01T00:00:00Z'})
        for change in changes:
            with self.subTest(change=change):
                value = launch_input()
                value.update(change)
                result = ads.BUNDLED_TOOL.execute('launch_campaign', value, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertFalse(self.meta.writes)
        website = launch_input('WEBSITE_CLICKS')
        for url in ('http://example.com/', 'https://user:password@example.com/', 'https://instagram.com/profile'):
            website['destination_url'] = url
            self.assertIsInstance(ads.BUNDLED_TOOL.execute('launch_campaign', website, self.api), ActionFailed)

    def test_pause_retains_history_and_works_for_billing_disabled_account(self):
        self.meta.account.update(account_status=2, funding_source='0')
        result = ads.BUNDLED_TOOL.execute('end_campaign', {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN}, self.api)
        self.assertIsInstance(result, ActionPendingApproval)
        self.assertFalse(self.meta.writes)
        self.meta.resources[CAMPAIGN]['status'] = 'PAUSED'
        approval = self.api.approvals.approve(result.approval_id)
        result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertIn('past delivery remains billable', result.message)
        self.assertEqual([path for path, _ in self.meta.writes], ['/500'])

    def test_pause_ownership_and_transport_failure_do_not_claim_stopped_delivery(self):
        self.meta.resources[CAMPAIGN]['account_id'] = '999'
        result = ads.BUNDLED_TOOL.execute('end_campaign', {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN}, self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertFalse(self.meta.writes)
        self.meta.resources[CAMPAIGN]['account_id'] = ACCOUNT
        pending = ads.BUNDLED_TOOL.execute('end_campaign', {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN}, self.api)
        self.meta.fail_write = 1
        result = ads.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn('pause is unconfirmed', result.error)
        self.assertEqual(len(self.meta.writes), 1)

    def test_every_read_matches_closed_result_schema_and_missing_metrics_are_null(self):
        approval = self.queue()
        self.assertIsInstance(ads.BUNDLED_TOOL.execute_approved(approval, self.api), ApprovalExecuted)
        inputs = {'list_accounts': {}, 'get_account': {'account_id': ACCOUNT},
                  'diagnose_account': {'account_id': ACCOUNT},
                  'list_identities': {'account_id': ACCOUNT},
                  'list_posts': {'account_id': ACCOUNT, 'page_id': PAGE, 'instagram_user_id': INSTAGRAM},
                  'lookup_targeting': {'account_id': ACCOUNT, 'type': 'INTEREST', 'query': 'video'},
                  'list_campaigns': {'account_id': ACCOUNT},
                  'get_campaign': {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN},
                  'get_performance': {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN, 'start_date': '2026-01-01', 'end_date': '2026-01-01'}}
        for action, value in inputs.items():
            with self.subTest(action=action):
                result = ads.BUNDLED_TOOL.execute(action, value, self.api)
                self.assertIsInstance(result, ActionExecuted, result)
                assert_matches_output_schema(self, ads.MANIFEST, action, result)
                if action == 'get_performance':
                    row = result.result['items'][0]
                    self.assertEqual(row['spend'], '0')
                    self.assertIsNone(row['inline_link_clicks'])
                    self.assertIsNone(row['video_thruplay'])
        self.meta.page_cursor = 'provider_cursor'
        result = ads.BUNDLED_TOOL.execute('list_accounts', {'limit': 1}, self.api)
        self.assertEqual(result.result['next_cursor'], 'provider_cursor')
        self.assertFalse(any('untrusted.example' in call[1] for call in self.meta.calls))

    def test_direct_queries_and_all_cursors_are_guarded_before_provider(self):
        for spec in ads.MANIFEST.actions:
            if spec.approval != 'direct' or 'after' not in spec.input_schema['properties']: continue
            value = {key: '1' for key in spec.input_schema.get('required', [])}
            value['after'] = '123456789012'
            with self.subTest(action=spec.id):
                before = len(self.meta.calls)
                result = ads.BUNDLED_TOOL.execute(spec.id, value, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertIn('digits', result.error)
                self.assertEqual(len(self.meta.calls), before)
        result = ads.BUNDLED_TOOL.execute('lookup_targeting', {'account_id': ACCOUNT, 'type': 'INTEREST', 'query': '123456789012'}, self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn('digits', result.error)
        for limit in (0, 21, '1', True):
            self.assertIsInstance(ads.BUNDLED_TOOL.execute('list_accounts', {'limit': limit}, self.api), ActionFailed)

    def test_oauth_uses_actual_lifetime_scopes_signed_callback_and_separate_config(self):
        params = {'redirect_uri': 'https://kern.example/tool-oauth/instagram_ads/callback'}
        start = ads.CREDENTIALS.start_connect(params, self.api)
        query = parse_qs(urlsplit(start['authorization_url']).query)
        self.assertEqual(set(query['scope'][0].split(',')), set(ads.SCOPES))
        self.assertNotIn('business_management', query['scope'][0])
        self.assertNotIn('meta-secret', start['authorization_url'])
        result = ads.CREDENTIALS.complete_connect({**params, 'state': start['state'], 'code': 'code'}, self.api)
        self.assertEqual(result['account']['id'], '900')
        self.assertEqual(self.api.credentials.record['secret']['access_token'], 'long-token')
        self.assertGreater(self.api.credentials.record['secret']['expires_at'], int(datetime.now().timestamp()) + 5183900)
        self.assertEqual([call[2].get('grant_type') for call in self.meta.calls[:2]], [None, 'fb_exchange_token'])
        with self.assertRaisesRegex(RuntimeError, 'callback changed'):
            ads.CREDENTIALS.complete_connect({'redirect_uri': 'https://other.example/', 'state': start['state'], 'code': 'code'}, self.api)
        with self.assertRaises(ValueError):
            ads.CREDENTIALS.complete_connect({**params, 'state': start['state'] + 'x', 'code': 'code'}, self.api)
        ads.CREDENTIALS.disconnect(self.api)
        self.assertIsNone(self.api.credentials.record)
        self.assertEqual(ads.MANIFEST.tool_id, 'instagram_ads')

    def test_expired_or_insufficient_grant_requires_reconnect_without_requests(self):
        for kind in ('expiry', 'scopes'):
            self.api = connected_api()
            if kind == 'expiry': self.api.credentials.record['secret']['expires_at'] = 1
            else: self.api.credentials.record['account']['scopes'] = ['ads_read']
            before = len(self.meta.calls)
            result = ads.BUNDLED_TOOL.execute('list_accounts', {}, self.api)
            self.assertIsInstance(result, ActionFailed)
            self.assertTrue(result.reconnect_required)
            self.assertEqual(len(self.meta.calls), before)
            self.assertIsNone(self.api.credentials.record)

    def test_direct_reads_recheck_remotely_revoked_scopes_before_account_data(self):
        values = {
            'list_accounts': {}, 'get_account': {'account_id': ACCOUNT},
            'diagnose_account': {'account_id': ACCOUNT},
            'list_identities': {'account_id': ACCOUNT},
            'list_posts': {'account_id': ACCOUNT, 'page_id': PAGE, 'instagram_user_id': INSTAGRAM},
            'lookup_targeting': {'account_id': ACCOUNT, 'type': 'INTEREST', 'query': 'Music'},
            'list_campaigns': {'account_id': ACCOUNT},
            'get_campaign': {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN},
            'get_performance': {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN,
                                'start_date': '2026-01-01', 'end_date': '2026-01-02'},
        }
        self.assertEqual(set(values), {spec.id for spec in ads.MANIFEST.actions if spec.approval == 'direct'})
        self.assertEqual(len(values), 9)
        for action, value in values.items():
            with self.subTest(action=action):
                self.api, self.meta = connected_api(), MetaFixture()
                self.meta.permissions.remove('ads_read')
                self.assertIn('ads_read', self.api.credentials.record['account']['scopes'])
                with patch.object(ads, 'json_request', self.meta):
                    result = ads.BUNDLED_TOOL.execute(action, value, self.api)
                self.assertIsInstance(result, ActionFailed, result)
                self.assertTrue(result.reconnect_required)
                self.assertIn('Connect again', result.error)
                self.assertEqual([call[1] for call in self.meta.calls], ['/me', '/me/permissions'])
                self.assertFalse(self.meta.writes)

    def test_creators_remain_usable_for_other_outcomes_and_profile_is_explicitly_limited(self):
        self.meta.page['instagram_business_account']['account_type'] = 'MEDIA_CREATOR'
        for objective in ('WEBSITE_CLICKS', 'ENGAGEMENTS', 'VIDEO_VIEWS'):
            approval = self.queue(launch_input(objective))
            self.assertIsInstance(ads.BUNDLED_TOOL.execute_approved(approval, self.api), ApprovalExecuted)
            self.meta.writes.clear()
        result = ads.BUNDLED_TOOL.execute('launch_campaign', launch_input('PROFILE_VISITS'), self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn('Business', result.error)
        self.assertFalse(self.meta.writes)

    def test_unknown_eligibility_is_disclosed_and_not_claimed_as_provider_acceptance(self):
        self.meta.media.pop('boost_eligibility_info')
        approval = self.queue()
        self.assertIsNone(approval.payload['proposal']['source_reel']['eligible_to_boost'])
        result = ads.BUNDLED_TOOL.execute_approved(approval, self.api)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertIn('does not prove accepted review', result.message)

    def test_missing_action_metrics_are_unavailable_and_numeric_zero_is_preserved(self):
        self.meta.insights[0].pop('actions')
        values = {'account_id': ACCOUNT, 'campaign_id': CAMPAIGN, 'start_date': '2026-01-01', 'end_date': '2026-01-01'}
        result = ads.BUNDLED_TOOL.execute('get_performance', values, self.api)
        self.assertIsNone(result.result['items'][0]['actions'])
        self.meta.insights[0]['actions'] = [{'action_type': 'post_engagement', 'value': 0}]
        result = ads.BUNDLED_TOOL.execute('get_performance', values, self.api)
        self.assertEqual(result.result['items'][0]['actions'][0]['value'], '0')
        values['start_date'] = '20260101'
        self.assertIsInstance(ads.BUNDLED_TOOL.execute('get_performance', values, self.api), ActionFailed)

    def test_revoked_provider_token_requires_reconnect_without_erasing_a_new_grant(self):
        for reconnected in (False, "token", "lifetime"):
            self.api = connected_api()
            def revoked(method, url, **kwargs):
                if reconnected:
                    if reconnected == "token":
                        self.api.credentials.record['secret']['access_token'] = 'replacement-token'
                    else:
                        self.api.credentials.record['secret']['expires_at'] = FRESH_EXPIRES_AT + 100
                raise WebRequestError('raw provider secret', status=400,
                                      body=b'{"error":{"code":190,"message":"raw provider secret"}}')
            with patch.object(ads, 'json_request', revoked):
                result = ads.BUNDLED_TOOL.execute('list_accounts', {}, self.api)
            self.assertIsInstance(result, ActionFailed)
            self.assertTrue(result.reconnect_required)
            self.assertNotIn('raw provider secret', result.error)
            if reconnected:
                self.assertIsNotNone(self.api.credentials.record)
            else:
                self.assertIsNone(self.api.credentials.record)
