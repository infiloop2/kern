"""Contract fixtures from public provider docs; no live calls or credentials."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from host.tools import reddit_scrapecreators as reddit
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared.web import ProviderWarning, WebRequestError
from test_tools import FakeHostAPI, assert_matches_output_schema


def fixture(name):
    return json.loads((Path(__file__).parent / 'fixtures' / 'reddit_scrapecreators' / (name + '.json')).read_text())


def configured_api():
    api = FakeHostAPI()
    api.config['SCRAPECREATORS_API_KEY'] = 'scrape-key'
    return api


class RedditScrapeCreatorsTests(unittest.TestCase):
    def run_read(self, action, args, response):
        with patch.object(reddit, 'json_request', return_value=response) as request:
            result = reddit.BUNDLED_TOOL.execute(action, args, configured_api())
        assert_matches_output_schema(self, reddit.MANIFEST, action, result)
        self.assertIsInstance(result, ActionExecuted)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[0], 'GET')
        self.assertEqual(request.call_args.kwargs['headers'], {'x-api-key': 'scrape-key'})
        self.assertEqual(request.call_args.kwargs['max_bytes'], 4 * 1024 * 1024)
        url = urlsplit(request.call_args.args[1])
        self.assertEqual(url.scheme, 'https')
        self.assertEqual(url.netloc, 'api.scrapecreators.com')
        self.assertNotIn('scrape-key', str(result))
        return result.result, url.path, parse_qs(url.query)

    def test_documented_global_search_and_subreddit_listing(self):
        for action, name, args, expected_path in (
            ('search_posts', 'search', {'query': 'dogs', 'sort': 'comments', 'cursor': 't3_1izmcgx'}, '/v1/reddit/search'),
            ('get_subreddit_posts', 'subreddit', {'subreddit': 'AskReddit', 'sort': 'new', 'cursor': 't3_1lfogps'}, '/v1/reddit/subreddit'),
        ):
            with self.subTest(action=action):
                response = fixture(name)
                result, path, params = self.run_read(action, args, response)
                self.assertEqual(path, expected_path)
                self.assertEqual(params['after'], [args['cursor']])
                self.assertEqual(result['next_cursor'], response['after'])
                self.assertEqual(result['usage'], {'requests': 1, 'credits_charged': 1})
                self.assertEqual(result['posts'][0]['id'], response['posts'][0]['id'])
                self.assertEqual(result['posts'][0]['body'], response['posts'][0].get('selftext'))
                if action == 'search_posts':
                    self.assertEqual(params['filter'], ['posts'])
                    self.assertEqual(params['sort'], ['comment_count'])

    def test_top_day_request_shape_and_rising_is_rejected_locally(self):
        _, path, params = self.run_read(
            'get_subreddit_posts',
            {'subreddit': 'AskReddit', 'sort': 'top', 'timeframe': 'day', 'limit': '15'},
            fixture('subreddit'),
        )
        self.assertEqual(path, '/v1/reddit/subreddit')
        self.assertEqual(params, {
            'subreddit': ['AskReddit'], 'timeframe': ['day'], 'sort': ['top'],
        })
        spec = next(action for action in reddit.MANIFEST.actions if action.id == 'get_subreddit_posts')
        self.assertNotIn('rising', spec.input_schema['properties']['sort']['enum'])
        with patch.object(reddit, 'json_request') as request:
            result = reddit.BUNDLED_TOOL.execute(
                'get_subreddit_posts', {'subreddit': 'AskReddit', 'sort': 'rising'}, configured_api(),
            )
        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()

    def test_subreddit_search_uses_dedicated_endpoint_and_preserves_missing_text(self):
        response = fixture('subreddit_search')
        result, path, params = self.run_read('search_posts', {'query': 'pushups', 'subreddit': 'fitness', 'sort': 'comments', 'cursor': 'next_page-1'}, response)
        self.assertEqual(path, '/v1/reddit/subreddit/search')
        self.assertEqual(params['subreddit'], ['fitness'])
        self.assertEqual(params['sort'], ['comments'])
        self.assertEqual(params['cursor'], ['next_page-1'])
        self.assertNotIn('filter', params)
        post = result['posts'][0]
        self.assertEqual(post['id'], '8gmjrb')
        self.assertEqual(post['subreddit'], 'Fitness')
        self.assertEqual(post['score'], 1414)
        self.assertIsNone(post['body'])
        self.assertIsNone(post['author'])
        self.assertFalse(post['body_truncated'])

    def test_read_post_uses_flat_response_and_fixed_url(self):
        response = fixture('post')
        response['selftext'] = 'Full text 🐈\n' * 1000
        response['url'] = 'https://attacker.example/'
        result, path, params = self.run_read('read_post', {'post_id': 't3_1q6pxwn'}, response)
        self.assertEqual(path, '/v1/reddit/post')
        self.assertEqual(params, {'url': ['https://www.reddit.com/comments/1q6pxwn/']})
        self.assertEqual(result['post']['body'], response['selftext'])
        self.assertFalse(result['truncated'])
        self.assertNotIn('attacker.example', str(result))

    def test_documented_nested_comments_do_not_claim_complete_thread(self):
        result, path, params = self.run_read('read_comments', {'post_id': '1lfbo7u'}, fixture('comments'))
        self.assertEqual(path, '/v1/reddit/post/comments')
        self.assertEqual(params['url'], ['https://www.reddit.com/comments/1lfbo7u/'])
        self.assertEqual(result['comments'][1]['parent_id'], 't1_mymupxb')
        self.assertEqual(result['comments'][1]['depth'], 1)
        # Published example still contains comma batches, although the current
        # endpoint contract forbids them. Surface the gap instead of guessing.
        self.assertTrue(result['pagination_incomplete'])
        self.assertTrue(all(',' not in c['cursor'] for c in result['continuations']))

    def test_opaque_comment_cursors_round_trip_without_fanout(self):
        response = fixture('comments')
        response['more'] = {'has_more': True, 'cursor': 'opaque_Top+/='}
        response['comments'] = response['comments'][:1]
        response['comments'][0]['replies'] = {'items': [], 'more': {'has_more': True, 'next_cursor': 'opaque_Nested-1'}}
        result, _, _ = self.run_read('read_comments', {'post_id': '1lfbo7u'}, response)
        self.assertEqual(result['continuations'], [
            {'parent_id': 't3_1lfbo7u', 'cursor': 'opaque_Top+/='},
            {'parent_id': 't1_mymupxb', 'cursor': 'opaque_Nested-1'},
        ])
        self.assertFalse(result['pagination_incomplete'])
        _, _, params = self.run_read('read_comments', {'post_id': '1lfbo7u', 'cursor': result['continuations'][0]['cursor']}, response)
        self.assertEqual(params['cursor'], ['opaque_Top+/='])

    def test_row_and_utf8_body_limits_are_explicit_and_cost_is_per_request(self):
        response = fixture('subreddit')
        first = response['posts'][0]
        first['selftext'] = '🐈' * 60000
        second = {**first, 'id': 'abc123'}
        response['posts'] = [first, second]
        result, _, params = self.run_read('get_subreddit_posts', {'subreddit': 'AskReddit', 'limit': '1'}, response)
        self.assertEqual(result['provider_posts_returned'], 2)
        self.assertEqual(len(result['posts']), 1)
        self.assertTrue(result['truncated'])
        self.assertTrue(result['posts'][0]['body_truncated'])
        self.assertEqual(len(result['posts'][0]['body'].encode()), reddit.MAX_BODY_BYTES)
        self.assertNotIn('limit', params)
        self.assertEqual(result['usage'], {'requests': 1, 'credits_charged': 1})
        del response['credits_charged']
        result, _, _ = self.run_read('get_subreddit_posts', {'subreddit': 'AskReddit'}, response)
        self.assertIsNone(result['usage']['credits_charged'])
        self.assertEqual(result['posts'][1]['body'], '')
        self.assertTrue(result['posts'][1]['body_truncated'])

    def test_comment_limit_reports_omitted_replies(self):
        result, _, _ = self.run_read('read_comments', {'post_id': '1lfbo7u', 'limit': '1'}, fixture('comments'))
        self.assertEqual(len(result['comments']), 1)
        self.assertTrue(result['truncated'])

    def test_rejects_invalid_inputs_before_any_request(self):
        cases = [
            ('create_comment', {'text': 'hello'}),
            ('search_posts', {'query': ''}),
            ('search_posts', {'query': 'a' * 513}),
            ('search_posts', {'query': 'hi', 'subreddit': 'r/test'}),
            ('search_posts', {'query': 'hi', 'subreddit': 'a,b'}),
            ('search_posts', {'query': 'hi', 'sort': 'unbounded'}),
            ('search_posts', {'query': 'hi', 'timeframe': 'forever'}),
            ('search_posts', {'query': 'hi', 'url': 'https://attacker.example'}),
            ('get_subreddit_posts', {}),
            ('read_post', {'post_id': 'https://reddit.com/comments/abc/'}),
            ('read_post', {'post_id': '../abc'}),
            ('read_post', {'post_id': 'a' * 14}),
            ('read_comments', {'post_id': 'abc', 'cursor': 'abc,def'}),
            ('read_comments', {'post_id': 'abc', 'cursor': 'https://attacker.example/'}),
            ('read_comments', {'post_id': 'abc', 'cursor': 'a' * 1025}),
            ('read_comments', {'post_id': 'abc', 'limit': '101'}),
            ('read_comments', {'post_id': 'abc', 'limit': '0'}),
        ]
        with patch.object(reddit, 'json_request') as request:
            for action, args in cases:
                with self.subTest(action=action, args=args):
                    self.assertIsInstance(reddit.BUNDLED_TOOL.execute(action, args, configured_api()), ActionFailed)
            request.assert_not_called()

    def test_guard_denies_secrets_in_every_guarded_input(self):
        for action, base, fields in (
            ('search_posts', {'query': 'public terms'}, ['query', 'subreddit', 'cursor']),
            ('get_subreddit_posts', {'subreddit': 'selfhosted'}, ['subreddit', 'cursor']),
            ('read_comments', {'post_id': 'abc123'}, ['cursor']),
        ):
            for field in fields:
                with self.subTest(action=action, field=field), patch.object(reddit, 'json_request') as request:
                    result = reddit.BUNDLED_TOOL.execute(action, {**base, field: 'AKIAIOSFODNN7EXAMPLE'}, configured_api())
                    self.assertIsInstance(result, ActionFailed)
                    self.assertIn('provider API credential', str(result))
                    request.assert_not_called()

    def test_malformed_or_wrong_resource_is_failure_not_empty_success(self):
        cases = [
            ('search_posts', {'query': 'hi'}, {'success': False, 'error': 'provider secret'}),
            ('search_posts', {'query': 'hi'}, {'success': True}),
            ('search_posts', {'query': 'hi', 'subreddit': 'selfhosted'}, fixture('subreddit_search')),
            ('read_post', {'post_id': 'different'}, fixture('post')),
            ('read_comments', {'post_id': '1lfbo7u'}, {**fixture('comments'), 'comments': {}}),
        ]
        wrong_comment = copy.deepcopy(fixture('comments'))
        wrong_comment['comments'][0]['link_id'] = 't3_wrong'
        cases.append(('read_comments', {'post_id': '1lfbo7u'}, wrong_comment))
        for action, args, response in cases:
            with self.subTest(action=action), patch.object(reddit, 'json_request', return_value=response):
                result = reddit.BUNDLED_TOOL.execute(action, args, configured_api())
                self.assertIsInstance(result, ActionFailed)
                self.assertNotIn('provider secret', str(result))

    def test_provider_errors_keep_status_without_leaking_bodies_or_retrying(self):
        for status in (400, 401, 402, 403, 404, 429, 500):
            with self.subTest(status=status), patch.object(reddit, 'json_request', side_effect=WebRequestError('private provider detail', status=status)) as request:
                with self.assertRaises(ProviderWarning) as raised:
                    reddit.BUNDLED_TOOL.execute('search_posts', {'query': 'hi'}, configured_api())
                request.assert_called_once()
                self.assertIn(str(status), str(raised.exception))
                self.assertNotIn('private provider detail', str(raised.exception))

    def test_missing_configuration_and_read_only_manifest(self):
        with patch.object(reddit, 'json_request') as request:
            result = reddit.BUNDLED_TOOL.execute('search_posts', {'query': 'hi'}, FakeHostAPI())
            self.assertIsInstance(result, ActionFailed)
            self.assertIn('SCRAPECREATORS_API_KEY', str(result))
            request.assert_not_called()
        self.assertIsNone(reddit.BUNDLED_TOOL.credentials)
        self.assertEqual(reddit.MANIFEST.connection, 'enable_only')
        self.assertEqual({a.id for a in reddit.MANIFEST.actions}, {'search_posts', 'get_subreddit_posts', 'read_post', 'read_comments'})
        self.assertTrue(all(a.approval == 'direct' for a in reddit.MANIFEST.actions))
