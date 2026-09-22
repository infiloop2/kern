"""Public HTTP contract: real routing/auth, no database or provider calls.

Keep expected public resources independent of the production dispatch table.
New exact admin routes join the denial matrix automatically; new regex routes
must have a representative path below. Normal unittest discovery runs this on
every PR, including hosts where PostgreSQL integration tests are unavailable.
"""

import ast
from contextlib import ExitStack
import hashlib
from http import HTTPStatus
import inspect
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.runtime.admin_api import admin_auth, service as admin_api
from host.runtime.core.unix_socket_service import UnixSocketServer


ROOT = Path(__file__).resolve().parents[1]
ADMIN_UI = ROOT / "host/runtime/admin_api/admin_ui"
WORKSPACE = ROOT / "host/runtime/workspace"
METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")
PUBLIC_AUTH = {
    ("POST", "/v1/login"),
    ("POST", "/v1/login/passkey"),
    ("GET", "/v1/login/status"),
}
# Explicit release-file mappings; neither route names nor targets come from
# UI_ASSETS. Packaged JS modules and PNG guides may grow within their fixed
# release directories, but adding another public resource class needs review.
STATIC_FILES = {
    "/": ADMIN_UI / "index.html",
    "/oauth/callback": ADMIN_UI / "index.html",
    "/admin_ui.css": ADMIN_UI / "admin_ui.css",
    "/manifest.webmanifest": ADMIN_UI / "manifest.webmanifest",
    "/service-worker.js": ADMIN_UI / "service-worker.js",
    "/favicon.ico": ADMIN_UI / "favicon.svg",
    "/favicon.svg": ADMIN_UI / "favicon.svg",
    **{f"/icons/{name}.png": ADMIN_UI / f"icons/{name}.png" for name in (
        "kern-180", "kern-192", "kern-512", "kern-maskable-512",
    )},
    **{f"/workspace/{route}": WORKSPACE / target for route, target in {
        "chat.html": "chat/ui/index.html",
        "chat.js": "chat/ui/agent_chat.js",
        "chat.css": "chat/ui/agent_chat.css",
        "rich_text.js": "chat/ui/rich_text.js",
        "rich_text.css": "chat/ui/rich_text.css",
        "dictation.js": "ui/dictation.js",
        "dictation-worklet.js": "ui/dictation-worklet.js",
        "composer.css": "ui/composer.css",
        "web-apps.html": "web_apps/ui/index.html",
        "web-apps.js": "web_apps/ui/personal_web_app_builder.js",
        "web-apps.css": "web_apps/ui/personal_web_app_builder.css",
        "global.html": "ui/index.html",
        "global.js": "ui/workspace.js",
        "global.css": "ui/workspace.css",
        "capability-worker-sandbox.js": "web_apps/ui/capability_worker_sandbox.js",
    }.items()},
    **{f"/admin_ui/{path.name}": path for path in ADMIN_UI.glob("*.js")},
    **{f"/guide-assets/{path.name}": path
       for path in (ROOT / "host/tools").glob("*/guide_assets/**/*.png")},
}
PATTERN_SAMPLES = (
    "/v1/workspace/getting-started", "/v1/workspace/chat/thread-1",
    "/v1/workspace/web-apps/app-1", "/v1/workspace/memory/pages/example",
    "/v1/workspace/schedules/1", "/v1/threads/thread-1/messages",
    "/v1/host-inference/providers/openai", "/v1/tools/events/1",
    "/v1/host-diagnostics/1", "/v1/tools", "/v1/tools/instagram/approvals",
    "/v1/tools/instagram/approvals/approval-1/approve",
    "/v1/network-tools/github-pending-pushes/push-1/approve",
)
SPECIAL_PROTECTED_PATHS = (
    "/v1/logout", "/v1/admin-passkeys", "/v1/admin-passkeys/register/options",
    "/v1/admin-passkeys/register", "/v1/agent-files/content?path=private.png",
    "/v1/agent-files/download?path=private.txt",
    "/v1/agent-files/upload?filename=upload.txt",
)


class _AdminTestServer(UnixSocketServer):
    def get_request(self):
        connection, _ = super().get_request()
        # Login source classification expects a TCP-shaped peer address.
        return connection, ("127.0.0.1", 0)


class AdminPublicBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.socket_path = str(root / "admin.sock")
        self.server = _AdminTestServer(self.socket_path, admin_api.Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever,
                         kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.stack.enter_context(patch.object(
            admin_api.state, "load_cloudflare_hostname", return_value="kern.example"))
        self.stack.enter_context(patch.object(admin_auth, "_sessions", {}))
        self.stack.enter_context(patch.object(admin_auth, "_client_failures", {}))
        self.stack.enter_context(patch.object(
            admin_auth, "_ADMIN_PASSWORD_HASH",
            hashlib.sha256(b"test-password").hexdigest()))
        self.stack.enter_context(patch.object(
            admin_auth.admin_passkeys, "configured", return_value=True))
        self.token = admin_auth._create_session()
        with patch.object(admin_auth, "_now", return_value=(
            admin_auth._now() - admin_auth.SESSION_ABSOLUTE_TIMEOUT_SECONDS - 1
        )):
            self.expired = admin_auth._create_session()
        # These are AFTER authentication. They must remain untouched even if
        # a regression performs work and subsequently returns an auth error.
        self.dispatch = self.stack.enter_context(patch.object(
            admin_api, "route", return_value={"sentinel": "authenticated"}))
        self.special_handlers = [self.stack.enter_context(patch.object(
            admin_api.Handler, name, autospec=True,
            side_effect=admin_api.ApiError(HTTPStatus.UNAUTHORIZED, "privileged handler reached"),
        )) for name in (
            "_handle_logout", "_handle_admin_passkeys", "_send_agent_file",
            "_send_agent_file_upload",
        )]
        self.unexpected = self.stack.enter_context(patch.object(
            admin_api.host_errors, "report_unexpected"))

    def request(self, method, path, headers=(), body=b"", *, public=True, host="kern.example"):
        # Drain the raw response, including HEAD, to avoid closing while the
        # handler writes its error body. No real TCP listener or host endpoint.
        base = [("Host", host)]
        if public:
            base += [("X-Forwarded-Proto", "https"),
                     ("CF-Connecting-IP", "203.0.113.10")]
        wire = f"{method} {path} HTTP/1.0\r\n"
        wire += "".join(f"{name}: {value}\r\n" for name, value in [*base, *headers])
        wire += f"Content-Length: {len(body)}\r\n\r\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(self.socket_path)
            connection.sendall(wire.encode() + body)
            chunks = []
            while chunk := connection.recv(65536):
                chunks.append(chunk)
        head, content = b"".join(chunks).split(b"\r\n\r\n", 1)
        return int(head.split(b" ", 2)[1]), head, content

    def assert_no_privileged_work(self):
        self.dispatch.assert_not_called()
        for handler in self.special_handlers:
            handler.assert_not_called()
        self.unexpected.assert_not_called()

    def protected_paths(self):
        paths = set(PATTERN_SAMPLES) | set(SPECIAL_PROTECTED_PATHS)
        # Also discover literal paths in the HTTP adapter: adding a new
        # pre-auth branch must not evade the matrix by omitting _ROUTES.
        public_paths = set(STATIC_FILES) | {path for _, path in PUBLIC_AUTH}
        for node in ast.walk(ast.parse(inspect.getsource(admin_api.Handler))):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and re.fullmatch(r"/[A-Za-z0-9_./-]*", node.value)
                    and node.value not in public_paths):
                paths.add(node.value)
        for entry in admin_api._ROUTES:
            if isinstance(entry.path, str):
                paths.add(entry.path)
            else:
                self.assertTrue(
                    any(entry.path.fullmatch(path) for path in PATTERN_SAMPLES),
                    f"Add a protected sample for new route pattern: {entry.path.pattern}",
                )
        return sorted(paths)

    def test_every_registered_route_requires_a_live_session_and_csrf(self):
        self.assertEqual(
            {name.removeprefix("do_") for name in dir(admin_api.Handler) if name.startswith("do_")},
            set(METHODS), "Include every supported HTTP method in the boundary matrix",
        )
        credentials = {
            "anonymous": ((), 401),
            "invented bearer": ((("Authorization", "Bearer invented"),), 401),
            "invalid session": ((("Cookie", "__Host-tc_admin_session=invalid"),
                                 ("X-Kern-Csrf", "1")), 401),
            "expired session": ((("Cookie", f"__Host-tc_admin_session={self.expired}"),
                                 ("X-Kern-Csrf", "1")), 401),
            "missing csrf": ((("Cookie", f"__Host-tc_admin_session={self.token}"),), 403),
            "duplicate cookie": ((("Cookie", f"__Host-tc_admin_session={self.token}; "
                                   f"__Host-tc_admin_session={self.token}"),
                                   ("X-Kern-Csrf", "1")), 401),
            "local cookie on public ingress": ((("Cookie", f"tc_admin_session={self.token}"),
                                                ("X-Kern-Csrf", "1")), 401),
            "preauth only": ((("Cookie", "__Host-tc_admin_passkey_login=preauth"),
                              ("X-Kern-Csrf", "1")), 401),
            "forged service identity": ((("X-Kern-Workspace", "1"),
                                         ("X-Kern-Csrf", "1")), 401),
        }
        for path in self.protected_paths():
            for method in METHODS:
                for label, (headers, expected) in credentials.items():
                    with self.subTest(path=path, method=method, credentials=label):
                        status, _, body = self.request(method, path, headers)
                        self.assertEqual(status, expected)
                        self.assertEqual(json.loads(body), {"error": {"message": (
                            "missing admin session request header" if expected == 403
                            else "missing or invalid admin session"
                        )}})
                        self.assert_no_privileged_work()

    def test_valid_sessions_reach_dispatch_on_each_transport(self):
        for public, cookie in ((True, "__Host-tc_admin_session"),
                               (False, "tc_admin_session")):
            with self.subTest(public=public):
                status, _, body = self.request("GET", "/v1/health", (
                    ("Cookie", f"{cookie}={self.token}"), ("X-Kern-Csrf", "1"),
                ), public=public)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body), {"sentinel": "authenticated"})
                self.dispatch.assert_called_once()
                self.assertIsInstance(self.dispatch.call_args.kwargs["principal"],
                                      admin_api.OperatorPrincipal)
                self.dispatch.reset_mock()
        self.assert_no_privileged_work()

    def test_public_static_files_are_only_the_expected_release_assets(self):
        self.assertEqual({route: path for route, (path, _) in admin_api.UI_ASSETS.items()},
                         STATIC_FILES)
        for path, expected_file in STATIC_FILES.items():
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(body, expected_file.read_bytes())
            for method in METHODS[1:]:
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.request(method, path)[0], 401)
        self.assert_no_privileged_work()

    def test_public_login_contract_does_not_grant_an_admin_session(self):
        status, _, body = self.request("GET", "/v1/login/status")
        self.assertEqual((status, json.loads(body)), (200, {"passkey_configured": True}))
        status, headers, _ = self.request("POST", "/v1/login", body=b'{"password":"wrong"}')
        self.assertEqual(status, 401)
        self.assertNotIn(b"Set-Cookie:", headers)
        self.assertEqual(self.request("POST", "/v1/login/passkey",
                                     (("X-Kern-Csrf", "1"),), b"{}")[0], 401)
        # The password step stays real; only the passkey storage/challenge is
        # replaced. Factor-one success on an enrolled public host is preauth.
        with patch.object(admin_auth.admin_passkeys, "begin_login",
                          return_value=("preauth", {"challenge": "test"})):
            status, headers, body = self.request(
                "POST", "/v1/login", body=b'{"password":"test-password"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"passkey_required": True,
                                           "publicKey": {"challenge": "test"}})
        self.assertIn(b"Set-Cookie: __Host-tc_admin_passkey_login=", headers)
        self.assertNotIn(b"Set-Cookie: __Host-tc_admin_session=", headers)
        for path in {path for _, path in PUBLIC_AUTH}:
            for method in METHODS:
                if (method, path) not in PUBLIC_AUTH:
                    with self.subTest(path=path, method=method):
                        self.assertEqual(self.request(method, path)[0], 401)
        self.assert_no_privileged_work()

    def test_public_exceptions_do_not_extend_to_nearby_or_encoded_paths(self):
        paths = (
            "/unknown", "/v1/new-endpoint", "/v1/login/extra", "/v1/login-status",
            "/v1/login/status/extra", "/v1/login/passkey/extra",
            "/oauth/callback/extra", "/admin_ui/not-a-release-file.js",
            "/admin_ui/../v1/tools", "/admin_ui/%2e%2e/v1/tools",
            "/admin_ui/%252e%252e/v1/tools", "/%76%31/login/status",
            "/workspace/chat.js/extra", "/workspace/../../v1/health",
            "/tool-media/../v1/tools", "/tool-media/%2e%2e/v1/tools",
            "/tool-media/" + "A" * 42, "/tool-media/" + "A" * 44,
            "/tool-media/" + "A" * 43 + "/extra",
            "/tool-media/" + "A" * 43 + "?path=/v1/tools",
            "/tool-media/" + "A" * 43 + "#fragment",
        )
        for path in paths:
            for method in METHODS:
                with self.subTest(path=path, method=method):
                    self.assertEqual(self.request(method, path)[0], 401)
        self.assert_no_privileged_work()

    def test_transport_validation_precedes_every_public_exception(self):
        public_requests = [("GET", path) for path in STATIC_FILES]
        public_requests += list(PUBLIC_AUTH)
        public_requests += [("GET", "/tool-media/" + "A" * 43)]
        # Duplicate and combined forwarding markers, or a wrong Host, cannot
        # select the local recovery path or serve a public resource.
        for method, path in public_requests:
            for headers in (
                (("Host", "wrong.example"),),
                (("X-Forwarded-Proto", "http"),),
                (("X-Forwarded-Proto", "https,http"),),
            ):
                with self.subTest(path=path, method=method, headers=headers):
                    self.assertEqual(self.request(method, path, headers)[0], 403)
        for path in ("/", "/v1/login/status", "/tool-media/" + "A" * 43):
            with self.subTest(path=path, transport="wrong host"):
                self.assertEqual(self.request("GET", path, host="wrong.example")[0], 403)
            with self.subTest(path=path, transport="single combined marker"):
                self.assertEqual(self.request("GET", path,
                    (("X-Forwarded-Proto", "https,http"),), public=False)[0], 403)
        self.assert_no_privileged_work()
