"""Interrupted Browser responses close without attempting a second response."""
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from browser_fakes import DispatchSessions
from host.runtime.browser.client import BrowserError, Screenshot
from host.runtime.browser.service import Handler
from host.runtime.core.unix_socket_service import UnixSocketServer


class BrowserResponseTests(unittest.TestCase):
    def test_partial_headers_and_bodies_close_for_json_and_png(self):
        for operation in ('list', 'screenshot_url'):
            for failed_write in (1, 2):
                with self.subTest(operation=operation, failed_write=failed_write), tempfile.TemporaryDirectory() as directory:
                    writes = []

                    class InterruptedHandler(Handler):
                        def do_POST(self):
                            send = self.wfile.write

                            def interrupted(data):
                                writes.append(data)
                                if len(writes) == failed_write:
                                    send(data[:len(data) // 2])
                                    raise ConnectionResetError('fixture interrupted write')
                                return send(data)

                            with patch.object(self.wfile, 'write', side_effect=interrupted):
                                super().do_POST()

                    path = str(Path(directory) / 'browser.sock')
                    server = UnixSocketServer(path, InterruptedHandler)
                    server.profiles = Mock()
                    server.profiles.dispatch.return_value = {'accounts': []}
                    server.profiles.screenshot.return_value = Screenshot(b'fixture image', 1, 1, False, 'https://example.com/')
                    server.sessions = DispatchSessions(server.profiles)
                    server.handle_error = Mock()
                    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
                    thread.start()
                    try:
                        with patch('host.runtime.browser.service.authorized', return_value=operation), patch('host.runtime.browser.service.host_errors.report_unexpected') as diagnostic:
                            with socket.socket(socket.AF_UNIX) as connection:
                                connection.settimeout(3)
                                connection.connect(path)
                                connection.sendall(f'POST /actions/{operation} HTTP/1.0\r\nContent-Length: 2\r\n\r\n{{}}'.encode())
                                received = b''
                                while chunk := connection.recv(4096):
                                    received += chunk
                            self.assertEqual(len(writes), failed_write, 'a second response was attempted')
                            self.assertEqual(received.count(b'HTTP/1.0 '), 1)
                            self.assertNotIn(b'503', received)
                            server.handle_error.assert_called_once()
                            diagnostic.assert_not_called()
                            self.assertEqual(len(server.profiles.method_calls), 1)
                    finally:
                        server.shutdown()
                        thread.join(timeout=5)
                        server.server_close()

    def test_operation_errors_still_return_json(self):
        for operation, error, status in (
            ('list', BrowserError('Browser is busy.'), 409),
            ('list', RuntimeError('private page text'), 503),
            ('screenshot_url', RuntimeError('private page text'), 503),
        ):
            with self.subTest(operation=operation, status=status):
                handler = Handler.__new__(Handler)
                handler.path = '/actions/' + operation
                handler._peer = Mock(return_value=(1, 2))
                handler.bounded_content_length = Mock(return_value=2)
                handler.read_json_object_body = Mock(return_value={})
                handler._send_json = Mock()
                sessions = Mock()
                sessions.submit.return_value.result.side_effect = error
                handler.server = SimpleNamespace(sessions=sessions)
                with patch('host.runtime.browser.service.authorized', return_value=operation), patch('host.runtime.browser.service.host_errors.report_unexpected'), patch('host.runtime.browser.service.host_metrics.service_resource_snapshot', return_value={}):
                    handler.do_POST()
                handler._send_json.assert_called_once()
                self.assertEqual(handler._send_json.call_args.args[0], status)
                self.assertNotIn('private page text', str(handler._send_json.call_args))
