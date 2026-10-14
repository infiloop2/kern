"""Admission, independent logins, fresh profiles and cleanup across Browser slots."""
import threading
import unittest
from unittest.mock import Mock, patch

from browser_fakes import MemoryStore
from test_browser_sessions import FakeBrowser
from host.constants import BROWSER_DEBUG_PORT, BROWSER_SESSION_COUNT
from host.runtime.browser.actions import ACTIONS
from host.runtime.browser.client import BrowserActionNotStarted, BrowserError
from host.runtime.browser.sessions import Sessions


class BrowserSlotTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.sessions = Sessions(self.store, FakeBrowser)
        self.release = threading.Event()
        self.addCleanup(self.sessions.close)
        self.addCleanup(self.release.set)

    def call(self, operation, body=None, *, tool=False):
        return self.sessions.submit(operation, body or {}, agent_action=tool).result(timeout=5)

    def save_account(self, key):
        self.store.save_account(key, 'x', {'provider_identifier': key, 'state': 'connected',
                                         'checked_at': '', 'usage': {}}, {'cookies': [], 'origins': []})

    def test_three_tools_and_one_login_with_no_fifth_browser(self):
        started = threading.Barrier(4)
        threads = set()
        def capture(browser, url):
            threads.add(threading.get_ident())
            started.wait(timeout=5)
            self.assertTrue(self.release.wait(5))
            return url
        with patch('host.runtime.browser.accounts.capture', side_effect=capture):
            calls = [self.sessions.submit('screenshot_url', {'url': 'https://example.com'}, agent_action=True) for _ in range(3)]
            started.wait(timeout=5)
            self.assertEqual(len(threads), 3)
            with self.assertRaises(BrowserActionNotStarted):
                self.sessions.submit('screenshot_url', {'url': 'https://example.com'}, agent_action=True)
            login = self.call('create', {'provider': 'x'})
            control = {**login, **{'lease': self.call('open', login)['lease']}}
            self.assertIn('image', self.call('frame', control))
            self.assertEqual(sum(bool(n) for n in self.sessions.busy), 3)
            # One replacement login, never another live operator Chromium.
            old = self.sessions.operator.pending[login['login_id']]
            replacement = self.call('create', {'provider': 'x'})
            self.assertIsNone(old.browser)
            self.assertEqual(len(self.sessions.operator.pending), 1)
            self.call('open', replacement)
            with self.assertRaises(BrowserError):
                self.call('cancel', control)
            self.release.set()
            self.assertEqual([f.result(timeout=5) for f in calls], ['https://example.com'] * 3)

    def test_same_account_excluded_and_next_call_loads_latest_usage_and_auth(self):
        self.save_account('first')
        self.save_account('second')
        started = threading.Event()
        def action(profile, body):
            profile.launch()
            self.assertEqual(profile.store.auth('first'), {'cookies': [], 'origins': []})
            profile.data['usage']['post'] = {'day': '2026-10-14', 'count': 7}
            started.set()
            self.assertTrue(self.release.wait(5))
            profile.close()
            return {'ok': True}
        with patch.dict(ACTIONS, {'post_tweet': action}):
            first = self.sessions.submit('post_tweet', {'account_id': 'first'}, agent_action=True)
            self.assertTrue(started.wait(5))
            for operation, tool in [('post_tweet', True), ('open', False), ('disconnect', False)]:
                with self.assertRaises(BrowserActionNotStarted):
                    self.sessions.submit(operation, {'account_id': 'first'}, agent_action=tool)
            opened = self.call('open', {'account_id': 'second'})
            self.assertIn('lease', opened)
            self.release.set()
            first.result(timeout=5)
        observed = []
        def next_action(profile, body):
            observed.append(profile.data['usage']['post']['count'])
            return {'ok': True}
        with patch.dict(ACTIONS, {'post_tweet': next_action}):
            self.call('post_tweet', {'account_id': 'first'}, tool=True)
        self.assertEqual(observed, [7])
        # The operator cached this account before the tool's write: refreshing
        # it must not restore the old usage when disconnecting another account.
        self.call('list')
        self.assertEqual(self.sessions.operator.profiles['first'].data['usage']['post']['count'], 7)
        with self.assertRaises(BrowserActionNotStarted):
            self.sessions.submit('post_tweet', {'account_id': 'second'}, agent_action=True)

    def test_expired_login_releases_account_and_failed_capture_releases_slot(self):
        self.save_account('first')
        self.call('open', {'account_id': 'first'})
        self.sessions.operator.profiles['first'].expires = 0
        self.sessions.expire()
        self.sessions.workers[0].submit(lambda: None).result(timeout=5)
        self.assertNotIn('first', self.sessions.accounts)
        with patch('host.runtime.browser.accounts.capture', side_effect=RuntimeError('capture failed')):
            for _ in range(4):
                with self.assertRaisesRegex(RuntimeError, 'capture failed'):
                    self.call('screenshot_url', {'url': 'https://example.com'}, tool=True)
        self.assertEqual(self.sessions.busy, [0] * 4)

    def test_account_and_slot_stay_reserved_until_failed_action_cleanup_finishes(self):
        self.save_account('first')
        closing = threading.Event()
        browser = Mock()
        browser.save_state.return_value = {'cookies': [], 'origins': []}
        def close():
            closing.set()
            self.assertTrue(self.release.wait(5))
        browser.close.side_effect = close
        self.sessions.factory = Mock(return_value=browser)
        def action(profile, body):
            profile.launch()
            raise RuntimeError('fixture failure')
        with patch.dict(ACTIONS, {'post_tweet': action}):
            call = self.sessions.submit('post_tweet', {'account_id': 'first'}, agent_action=True)
            self.assertTrue(closing.wait(5))
            with self.assertRaises(BrowserActionNotStarted):
                self.sessions.submit('post_tweet', {'account_id': 'first'}, agent_action=True)
            self.assertEqual(self.sessions.busy[1:], [1, 0, 0])
            self.release.set()
            with self.assertRaisesRegex(RuntimeError, 'fixture failure'):
                call.result(timeout=5)
        self.assertEqual(self.sessions.busy, [0] * 4)
        self.assertEqual(self.sessions.accounts, {})
        browser.close.assert_called_once()

    def test_slots_have_distinct_ports_and_shutdown_rejects_new_work(self):
        ports = [self.sessions.new_accounts(slot).debug_port for slot in range(BROWSER_SESSION_COUNT)]
        self.assertEqual(ports, list(range(BROWSER_DEBUG_PORT, BROWSER_DEBUG_PORT + 4)))
        self.sessions.close()
        with self.assertRaises(BrowserActionNotStarted):
            self.sessions.submit('list', {}, agent_action=True)
        # close is intentionally called by cleanup too.
