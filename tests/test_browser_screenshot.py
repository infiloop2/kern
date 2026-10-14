"""Anonymous screenshot policy, isolation, capture bounds and binary IPC."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path
import struct
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from host.runtime.browser import client
from host.runtime.browser.accounts import Accounts
from host.runtime.browser.screenshot import capture
from host.runtime.browser.service import Handler, authorized
from host.runtime.core.unix_socket_service import UnixSocketServer
from host.runtime.agent_shim.mcp_shim import UnixHTTPConnection
from host.tools.browser import BUNDLED_TOOL, MANIFEST
from host.tools.results import ActionFailed, StreamingAsset, StreamingAssetError
from browser_fakes import DispatchSessions, MemoryStore
from test_tools import FakeHostAPI


def png(width=2, height=3):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\0' + b'\xff\0\0' * width) * height)) + chunk(b'IEND', b''))


class ScreenshotTests(unittest.TestCase):
    def test_tool_guards_initial_url_only_and_returns_file(self):
        api = FakeHostAPI()
        image = client.Screenshot(png(), 2, 3, False, 'https://redirect.example/path')
        with patch.object(api.outbound, 'guard_request_parameter_string', wraps=api.outbound.guard_request_parameter_string) as guard, patch.object(client, 'screenshot', return_value=image) as request:
            result = BUNDLED_TOOL.execute('screenshot_url', {'url': 'https://example.com/page'}, api)
            self.assertIsInstance(result, StreamingAsset)
            request.assert_not_called()
            with result.open_stream() as asset:
                self.assertEqual(asset.source.read(), image.png)
                self.assertEqual(asset.media_type, 'image/png')
                self.assertIn(image.url, asset.summary)
            request.assert_called_once_with('https://example.com/page')
            self.assertTrue(guard.called)
            self.assertNotIn(image.url, [call.args[0] for call in guard.call_args_list])
        spec = next(a for a in MANIFEST.actions if a.id == 'screenshot_url')
        self.assertTrue(spec.returns_asset)
        self.assertFalse(spec.output_schema)
        self.assertEqual(api.approvals.records, {})

    def test_initial_fragment_is_guarded_after_decoding(self):
        api = FakeHostAPI()
        with patch.object(api.outbound, 'guard_request_parameter_string', wraps=api.outbound.guard_request_parameter_string) as guard:
            BUNDLED_TOOL.execute('screenshot_url', {'url': 'https://example.com/#hello%2520world'}, api)
        self.assertIn('hello world', [call.args[0] for call in guard.call_args_list])

    def test_tool_rejects_extra_inputs_and_invalid_urls(self):
        for body in ({}, {'url': 'http://example.com'}, {'url': 'https://user:pass@example.com'},
                     {'url': 'https://127.0.0.1'}, {'url': 'https://example.com:1234'},
                     {'url': 'https://example.com/' + 'a' * 201}, {'url': 'https://example.com/\n'},
                     {'url': 'https://example.com', 'account_id': 'acct_saved'},
                     {'url': 'https://example.com', 'script': 'alert(1)'}):
            with self.subTest(body=body), patch.object(client, 'screenshot') as request:
                self.assertIsInstance(BUNDLED_TOOL.execute('screenshot_url', body, FakeHostAPI()), ActionFailed)
                request.assert_not_called()

    def test_tool_stream_failure_is_safe_and_not_retried(self):
        with patch.object(client, 'screenshot', side_effect=client.BrowserActionNotStarted()) as request:
            result = BUNDLED_TOOL.execute('screenshot_url', {'url': 'https://example.com'}, FakeHostAPI())
            with self.assertRaisesRegex(StreamingAssetError, 'not started'):
                with result.open_stream():
                    self.fail('unexpected stream')
            request.assert_called_once()

    def test_service_uses_empty_profile_and_closes_without_saving(self):
        store = MemoryStore()
        browser = Mock()
        factory = Mock(return_value=browser)
        accounts = Accounts(store, factory)
        for error in (None, RuntimeError('capture failed')):
            with self.subTest(error=error), patch('host.runtime.browser.accounts.capture', side_effect=error) as run, patch.object(store, 'auth') as auth:
                if error:
                    with self.assertRaises(RuntimeError):
                        accounts.screenshot({'url': 'https://example.com'})
                else:
                    accounts.screenshot({'url': 'https://example.com'})
                self.assertEqual(factory.call_args.args, (None, 'about:blank'))
                run.assert_called_once_with(browser, 'https://example.com')
                auth.assert_not_called()
                browser.save_state.assert_not_called()
        self.assertEqual(browser.close.call_count, 2)
        self.assertEqual(accounts.profiles, {})

    def test_service_does_not_take_over_operator_profile(self):
        factory = Mock()
        accounts = Accounts(MemoryStore(), factory)
        accounts.profiles['saved'] = SimpleNamespace(browser=Mock(), lease='active', expires=float('inf'))
        with self.assertRaisesRegex(client.BrowserError, 'popup'):
            accounts.screenshot({'url': 'https://example.com'})
        factory.assert_not_called()
        accounts.profiles['saved'].browser.close.assert_not_called()
        for body in ({'url': 'about:blank'}, {'url': 'https://example.com', 'account_id': 'saved'}):
            with self.assertRaises(client.BrowserError):
                accounts.screenshot(body)

    def browser(self, width=1100, height=1800):
        browser = Mock()
        browser.page.url = 'https://final.example/'
        session = browser.context.new_cdp_session.return_value
        def send(method, args=None):
            if method == 'Page.getLayoutMetrics':
                return {'cssContentSize': {'width': width, 'height': height}}
            if method == 'Page.captureScreenshot':
                return {'data': base64.b64encode(png()).decode()}
            return {}
        session.send.side_effect = send
        return browser, session

    def test_capture_clips_before_encoding_and_closes_popups(self):
        browser, session = self.browser(width=100000, height=1000000)
        result = capture(browser, 'https://example.com')
        self.assertTrue(result.clipped)
        self.assertEqual((result.width, result.height), (1100, 16000))
        calls = [c for c in session.send.call_args_list if c.args[0] == 'Page.captureScreenshot']
        self.assertEqual(calls[0].args[1]['clip'], {'x': 0, 'y': 0, 'width': 1100, 'height': 16000, 'scale': 1})
        scrolls = [c for c in session.send.call_args_list if c.args[0] == 'Runtime.evaluate']
        self.assertLess(len(scrolls), 25)
        self.assertEqual(scrolls[-1].args[1]['expression'], 'window.scrollTo(0, 0)')
        self.assertTrue(all(c.args[1]['timeout'] == 500 for c in scrolls))
        browser.context.remove_listener.assert_called_once_with('page', browser.adopt_page)
        popup = Mock()
        browser.context.on.call_args.args[1](popup)
        popup.close.assert_called_once()
        browser.process.kill.assert_not_called()
        session.detach.assert_called_once()

    def test_capture_rejects_bad_dimensions_and_oversized_image(self):
        for width, height in [(True, 30), (float('inf'), 30), (30, -1)]:
            browser, _ = self.browser(width, height)
            with self.assertRaisesRegex(client.BrowserError, 'dimensions'):
                capture(browser, 'https://example.com')
        browser, _ = self.browser()
        with patch('host.runtime.browser.screenshot.MAX_SCREENSHOT_BYTES', 1):
            with self.assertRaisesRegex(client.BrowserError, 'byte limit'):
                capture(browser, 'https://example.com')

    def test_watchdog_interrupts_wedged_capture(self):
        browser, _ = self.browser()
        killed = threading.Event()
        browser.process.kill.side_effect = killed.set
        def hung(*args, **kwargs):
            self.assertTrue(killed.wait(2))
            raise RuntimeError('renderer disconnected')
        browser.page.goto.side_effect = hung
        with patch('host.runtime.browser.screenshot.CAPTURE_SECONDS', 0.01):
            with self.assertRaises(RuntimeError):
                capture(browser, 'https://example.com')
        browser.process.kill.assert_called_once()

    def test_binary_client_rejects_incomplete_wrong_type_and_oversized_responses(self):
        image = png()
        for content, headers in [
            (image[:-1], {}), (image, {'Content-Type': 'text/html'}),
            (image, {'Content-Length': str(client.MAX_SCREENSHOT_BYTES + 1)}),
            (b'not PNG' + image[7:], {}),
            (png(1101, 1), {}), (image, {'X-Kern-Screenshot-Clipped': 'maybe'}),
        ]:
            values = {'Content-Length': str(len(content) if content != image[:-1] else len(image)),
                      'Content-Type': 'image/png', 'X-Kern-Screenshot-Clipped': 'false', **headers}
            response = Mock(status=200, read=io.BytesIO(content).read, getheader=lambda key, default=None: values.get(key, default))
            connection = Mock(getresponse=Mock(return_value=response))
            with self.subTest(headers=headers), patch.object(client, 'Connection', return_value=connection):
                with self.assertRaises(client.BrowserError):
                    client.screenshot('https://example.com')
                connection.close.assert_called_once()

    def test_binary_socket_and_tools_only_authorization(self):
        def user(name):
            return SimpleNamespace(pw_uid=os.getuid() if name == 'kern-tools' else -1)
        with patch('host.runtime.browser.service.pwd.getpwnam', side_effect=user):
            self.assertEqual(authorized(os.getuid(), '/actions/screenshot_url'), 'screenshot_url')
            self.assertIsNone(authorized(-1, '/actions/screenshot_url'))
            self.assertIsNone(authorized(os.getuid(), '/operator/frame'))
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory) / 'browser.sock')
                server = UnixSocketServer(path, Handler)
                image = client.Screenshot(png(), 2, 3, True, 'https://final.example/?x=1')
                server.profiles = SimpleNamespace(screenshot=Mock(return_value=image))
                server.sessions = DispatchSessions(server.profiles)
                thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
                thread.start()
                try:
                    with patch.object(client, 'Connection', side_effect=lambda: UnixHTTPConnection(path)):
                        self.assertEqual(client.screenshot('https://example.com'), image)
                        server.profiles.screenshot.assert_called_once_with({'url': 'https://example.com'})
                        server.sessions.submit = Mock(side_effect=client.BrowserActionNotStarted())
                        with self.assertRaises(client.BrowserActionNotStarted):
                            client.screenshot('https://example.com')
                        self.assertEqual(server.profiles.screenshot.call_count, 1)
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
                    server.server_close()
