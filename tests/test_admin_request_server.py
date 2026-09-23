"""Exercise overload and recovery with real local connections, without a DB."""
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
import socket
import threading
import time
import unittest
from unittest.mock import patch

from host.runtime.admin_api import request_server


class RequestServerTests(unittest.TestCase):
    def setUp(self):
        self.started = threading.Event()
        self.release = threading.Event()
        started, release = self.started, self.release

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.describe_request("GET", self.path.split("?", 1)[0])
                started.set()
                release.wait(5)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):
                pass

        for port in range(8000, 8016):
            try:
                self.server = request_server.BoundedThreadingHTTPServer(("127.0.0.1", port), Handler, max_workers=1)
                break
            except OSError:
                continue
        else:
            self.fail("No free test port in 8000-8015")
        self.reported = threading.Event()
        self.records = []
        def record(*args, **kwargs):
            self.records.append((args, kwargs))
            self.reported.set()
        self.report_patch = patch.object(request_server.host_errors, "report_warning", side_effect=record)
        self.report_patch.start()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.clients = []

    def tearDown(self):
        self.release.set()
        for client in self.clients:
            client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.report_patch.stop()

    def client(self):
        client = HTTPConnection(*self.server.server_address, timeout=2)
        self.clients.append(client)
        return client

    def test_busy_is_prompt_reported_and_recovers(self):
        first = self.client()
        first.request("GET", "/v1/workspace/chat/private-thread?secret=do-not-log")
        self.assertTrue(self.started.wait(1))
        second = self.client()
        began = time.monotonic()
        second.request("GET", "/v1/health")
        response = second.getresponse()
        self.assertEqual(response.status, 503)
        self.assertEqual(response.getheader("Retry-After"), "5")
        self.assertIn(b'"host_busy"', response.read())
        self.assertLess(time.monotonic() - began, 1)
        self.assertTrue(self.reported.wait(2))
        context = self.records[0][1]["context"]
        self.assertEqual(context["capacity"], 1)
        self.assertEqual(context["rejected_connections"], 1)
        self.assertIn("GET /v1/workspace/chat", context["request_1"])
        self.assertIn("wait", context["stack_1"])
        self.assertNotIn("private-thread", str(context))
        self.assertNotIn("do-not-log", str(context))
        self.release.set()
        self.assertEqual(first.getresponse().read(), b"ok")
        deadline = time.monotonic() + 2
        while self.server._active and time.monotonic() < deadline:
            time.sleep(0.01)
        third = self.client()
        third.request("GET", "/v1/health")
        self.assertEqual(third.getresponse().status, 200)

    def test_slow_inflight_request_reported_before_completion_and_rate_limited(self):
        with patch.object(request_server, "SLOW_REQUEST_SECONDS", 0):
            first = self.client()
            first.request("GET", "/v1/agent-files/private-name?token=secret")
            self.assertTrue(self.started.wait(1))
            self.assertTrue(self.reported.wait(2))
            self.assertEqual(self.records[0][1]["context"]["slow_handlers"], 1)
            for _ in range(5):
                self.server.service_actions()
            self.assertEqual(len(self.records), 1)
        self.release.set()
        first.getresponse().read()

    def test_worker_start_failure_releases_capacity(self):
        a, b = socket.socketpair()
        try:
            with patch('socketserver.ThreadingMixIn.process_request', side_effect=RuntimeError("start failed")):
                with self.assertRaises(RuntimeError):
                    self.server.process_request(a, ("127.0.0.1", 1))
            self.assertTrue(self.server._request_slots.acquire(blocking=False))
            self.server._request_slots.release()
        finally:
            a.close()
            b.close()

    def test_report_thread_start_failure_can_retry_immediately(self):
        self.server.shutdown()
        with self.server._diagnostic_lock:
            self.server._rejected = 1
            previous_report = self.server._last_report
        with patch.object(request_server.threading.Thread, "start", side_effect=RuntimeError("start failed")):
            self.server.service_actions()
        with self.server._diagnostic_lock:
            self.assertEqual(self.server._last_report, previous_report)
            self.assertEqual(self.server._rejected, 1)
            self.assertFalse(self.server._reporting)
        self.server.service_actions()
        self.assertTrue(self.reported.wait(2))

    def test_route_groups_never_include_unrecognized_input(self):
        self.assertEqual(request_server.request_group("GET", "/tool-media/secret"), "other admin request")
        self.assertEqual(request_server.request_group("GET", "/v1/health-secret"), "other admin request")
