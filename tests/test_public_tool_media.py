"""Exercise both real HTTP handlers; provider calls and database are not needed."""
from contextlib import ExitStack
import io
from http import HTTPStatus
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.runtime.admin_api import service as admin_api, tools_client
from host.runtime.core.unix_socket_service import UnixSocketServer
from host.runtime.tools.api import ToolsServer
from host.runtime.tools.assets import AssetError, PUBLIC_MEDIA_TTL_SECONDS, ASSET_TTL_SECONDS
from host.runtime.tools.tools_host import HostAssets
from host.runtime.tools import tools_host


class PublicToolMediaTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.tools = ToolsServer(str(root / "tools.sock"), frozenset({999999}), frozenset({os.getuid()}))
        self.admin = UnixSocketServer(str(root / "admin.sock"), admin_api.Handler)
        for server in (self.tools, self.admin):
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.stack.enter_context(patch.object(tools_client, "TOOLS_SOCKET_PATH", str(root / "tools.sock")))
        self.stack.enter_context(patch.object(admin_api.state, "load_cloudflare_hostname", return_value="kern.example"))
        self.auth = self.stack.enter_context(patch.object(admin_api.Handler, "_authenticate", side_effect=admin_api.ApiError(HTTPStatus.UNAUTHORIZED, "authentication required")))
        self.data = b"0123456789" * 100
        self.metadata = self.tools.asset_store.stage(kind="video", tool_id="instagram", filename="hello.mp4",
            media_type="video/mp4", size_bytes=len(self.data), source=io.BytesIO(self.data))
        self.assets = HostAssets("instagram", self.tools.asset_store)
        self.stack.enter_context(self.assets._approved_execution("kern.example"))
        self.admin_path = str(root / "admin.sock")

    def request(self, path, method="GET", *, headers=None, private=False):
        connection = tools_client._ToolsSocketConnection(tools_client.TOOLS_SOCKET_PATH if private else self.admin_path)
        try:
            request_headers = {"Host": "kern.example", "X-Forwarded-Proto": "https"}
            request_headers.update(headers or {})
            connection.request(method, path, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_only_exact_live_capability_serves_video_through_both_handlers(self):
        with self.assets.public_asset_url(self.metadata.asset_id) as url:
            path = url.removeprefix("https://kern.example")
            status, headers, body = self.request(path)
            self.assertEqual((status, body), (200, self.data))
            self.assertEqual(headers["Content-Type"], "video/mp4")
            self.assertIn("no-store", headers["Cache-Control"])
            self.assertNotIn("Set-Cookie", headers)
            self.assertEqual(self.request(path, "HEAD")[2], b"")
            self.auth.assert_not_called()
            for bad in (path + "/file", path + "?path=/etc/passwd",
                        "/tool-media/../v1/tools", "/tool-media/%2e%2e/v1/tools"):
                self.assertEqual(self.request(bad)[0], 401, bad)
            for token in (self.metadata.asset_id, "B" * 43):
                self.assertEqual(self.request("/tool-media/" + token)[0], 404)
            for method in ("POST", "PUT", "DELETE"):
                self.assertEqual(self.request(path, method)[0], 401)
            status, headers, body = self.request(path, headers={"X-Forwarded-Proto": "http"})
            self.assertEqual((status, body), (301, b""))
            self.assertEqual(headers["Location"], url)
            self.assertEqual(self.request(path, "HEAD", headers={"X-Forwarded-Proto": "http"})[0], 403)
            self.assertEqual(self.request(path, headers={"Host": "wrong.example"})[0], 403)
        self.assertEqual(self.request(path)[0], 404)
        self.auth.reset_mock()
        for path in ("/v1/tools", "/v1/agent-files/content?path=/etc/passwd", "/v1/tools/instagram/approvals"):
            self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.auth.call_count, 3)

    def test_byte_ranges_and_no_arbitrary_private_tools_routes(self):
        with self.assets.public_asset_url(self.metadata.asset_id) as url:
            path = url.removeprefix("https://kern.example")
            for requested, expected in (("bytes=2-5", self.data[2:6]), ("bytes=-4", self.data[-4:]), ("bytes=998-", self.data[998:])):
                status, headers, body = self.request(path, headers={"Range": requested})
                self.assertEqual((status, body), (206, expected))
                self.assertEqual(int(headers["Content-Length"]), len(expected))
            for requested in ("bytes=-0", "bytes=1000-", "bytes=4-2", "bytes=0-1,3-4", "bytes=-", "invalid"):
                self.assertEqual(self.request(path, headers={"Range": requested})[0], 416)
            for path in ("/operator/tool-media/../assets", "/operator/tool-media/" + self.metadata.asset_id):
                self.assertEqual(self.request(path, private=True)[0], 404)
            self.tools.admin_uids = frozenset({999999})
            self.tools.agent_uids = frozenset({os.getuid()})
            self.assertEqual(self.request("/operator" + url.removeprefix("https://kern.example"), private=True)[0], 403)

    def test_grants_require_approval_and_tool_ownership(self):
        for assets in (HostAssets("instagram", self.tools.asset_store), HostAssets("runway", self.tools.asset_store)):
            with self.assertRaises((ValueError, AssetError)):
                with assets.public_asset_url(self.metadata.asset_id):
                    self.fail("unauthorized grant")

    def test_supported_images_require_approval_serve_exact_bytes_and_revoke(self):
        for media_type, suffix in (("image/jpeg", ".jpeg"), ("image/jpg", ".jpg"),
                                   ("image/png", ".png"), ("image/webp", ".webp")):
            with self.subTest(media_type=media_type):
                data = b"image bytes" * 100
                image = self.tools.asset_store.stage(kind="image", tool_id="instagram", filename="private" + suffix,
                    media_type=media_type, size_bytes=len(data), source=io.BytesIO(data))
                assets = HostAssets("instagram", self.tools.asset_store)
                with self.assertRaisesRegex(ValueError, "approved tool action"):
                    with assets.public_asset_url(image.asset_id):
                        self.fail("image exposed before approval")
                with assets._approved_execution("kern.example"):
                    context = assets.public_asset_url(image.asset_id)
                    url = context.__enter__()
                    path = url.removeprefix("https://kern.example")
                    status, headers, body = self.request(path)
                    self.assertEqual((status, body), (200, data))
                    self.assertEqual(headers["Content-Type"], media_type)
                    self.assertIn("no-store", headers["Cache-Control"])
                    self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(self.request(path, "HEAD")[2], b"")
                self.assertEqual(self.request(path)[0], 404)
                context.__exit__(None, None, None)

    def test_grants_reject_unsupported_asset_types(self):
        from dataclasses import replace
        store = self.tools.asset_store
        original = store._records[self.metadata.asset_id]
        for media_type in ("text/html", "image/svg+xml", "application/pdf", "video/webm", "image/gif"):
            with self.subTest(media_type=media_type):
                # Even a future staging type must not implicitly become public.
                store._records[self.metadata.asset_id] = replace(original,
                    metadata=replace(original.metadata, media_type=media_type))
                with self.assertRaisesRegex(AssetError, "supported image or video"):
                    with self.assets.public_asset_url(self.metadata.asset_id):
                        self.fail("unsupported public asset")
                self.assertEqual(store._public_asset_grants, {})

    def test_link_expiry_exception_cleanup_and_staged_asset_expiry(self):
        from host.runtime.tools import assets as asset_module
        now = self.metadata.expires_at - ASSET_TTL_SECONDS
        with self.assets.public_asset_url(self.metadata.asset_id) as url:
            path = url.removeprefix("https://kern.example")
            with patch.object(asset_module.time, "time", return_value=now + PUBLIC_MEDIA_TTL_SECONDS + 1):
                self.assertEqual(self.request(path)[0], 404)
        with self.assertRaisesRegex(RuntimeError, "provider failed"):
            with self.assets.public_asset_url(self.metadata.asset_id) as url:
                path = url.removeprefix("https://kern.example")
                raise RuntimeError("provider failed")
        self.assertEqual(self.request(path)[0], 404)
        with patch.object(asset_module.time, "time", return_value=now + ASSET_TTL_SECONDS):
            with self.assertRaises(AssetError):
                self.assets.describe(self.metadata.asset_id)
        self.assertEqual(list(self.tools.asset_store._root.iterdir()), [])

    def test_delete_revokes_live_link(self):
        with self.assets.public_asset_url(self.metadata.asset_id) as url:
            self.assets.delete(self.metadata.asset_id)
            self.assertEqual(self.request(url.removeprefix("https://kern.example"))[0], 404)

    def test_no_public_grant_without_cloudflare_or_with_local_hostname(self):
        for hostname in (None, "localhost", "127.0.0.1", "127.1", "2130706433", "::1", "192.168.1.5", "host.localhost",
                         "host.local", "host.internal", "http://kern.example", "kern.example:8000", "kern.example/path"):
            with self.subTest(hostname=hostname):
                self.assets._public_hostname = hostname
                with self.assertRaisesRegex(ValueError, "Cloudflare HTTPS"):
                    with self.assets.public_asset_url(self.metadata.asset_id):
                        self.fail("must not expose local address")
        self.assertEqual(self.tools.asset_store._public_asset_grants, {})

    def test_approved_instagram_http_failure_records_diagnostic_and_revokes_link(self):
        from dataclasses import asdict, replace
        from types import SimpleNamespace
        from host.tools import instagram
        from host.tools.shared.web import WebRequestError
        from host.runtime.tools import tools_host
        from test_tools_instagram import connected_api, ME_RESPONSE
        api = replace(connected_api(), assets=self.assets)
        with patch.object(instagram, "json_request", return_value=dict(ME_RESPONSE)):
            pending = instagram.InstagramTool().execute("post_reel", {"video_asset_id": self.metadata.asset_id}, api)
        record = asdict(api.approvals.approve(pending.approval_id))
        record.update(tool_id="instagram", connection_id="connection_test", account_id="17841400000000000", account_label="@clawcreates")
        with (patch.object(tools_host, "enabled_tool", return_value=instagram.InstagramTool()),
              patch.object(tools_host, "connection_scope", return_value=SimpleNamespace(account_id=record["account_id"])),
              patch.object(tools_host, "host_api_for", return_value=api) as scoped_api,
              patch.object(tools_host, "_audit"),
              patch.object(tools_host.host_errors, "report_warning") as report,
              patch.object(instagram, "json_request", side_effect=[dict(ME_RESPONSE),
                  WebRequestError("provider declined", status=400, body=b'{"error":{"code":100,"message":"secret"}}')])):
            result = tools_host._execute_approved(record, public_hostname="kern.example")
        self.assertEqual(result["status"], "failed")
        scoped_api.assert_called_once()
        self.assertFalse(self.assets._approved)
        self.assertEqual(report.call_args.args[0], "tools.provider_request_approved")
        context = report.call_args.kwargs["context"]
        self.assertEqual(context["http_status"], 400)
        self.assertEqual(context["operation"], "Reel container")
        self.assertNotIn("secret", str(context))
        self.assertEqual(self.tools.asset_store._public_asset_grants, {})

    def test_callback_exit_revokes_retained_context_and_late_authority(self):
        assets = HostAssets("instagram", self.tools.asset_store)
        with assets._approved_execution("kern.example"):
            delayed = assets.public_asset_url(self.metadata.asset_id)
            leaked = assets.public_asset_url(self.metadata.asset_id)
            url = leaked.__enter__()
            self.assertEqual(self.request(url.removeprefix("https://kern.example"))[0], 200)
        self.assertEqual(self.request(url.removeprefix("https://kern.example"))[0], 404)
        for context in (delayed, assets.public_asset_url(self.metadata.asset_id)):
            with self.assertRaisesRegex(ValueError, "approved tool action"):
                context.__enter__()
        leaked.__exit__(None, None, None)
        self.assertEqual(self.tools.asset_store._public_asset_grants, {})

    def test_admin_supplies_configured_hostname_and_ignores_caller_override(self):
        with patch.object(tools_client, "_tools_operator_request", return_value={"approval": {"origin_thread_id": None}}) as delegated:
            tools_client.tools_route("POST", "/v1/tools/instagram/approvals/approval_1/approve",
                                     {"public_hostname": "attacker.example"})
        self.assertEqual(delegated.call_args.args[1], {"public_hostname": "kern.example"})
        with patch.object(tools_host.state, "load_cloudflare_hostname", side_effect=AssertionError("tools cannot read operator_connections")):
            with self.assets.public_asset_url(self.metadata.asset_id) as url:
                self.assertTrue(url.startswith("https://kern.example/tool-media/"))

    def test_public_grant_rejects_other_tools_asset_even_during_approval(self):
        other = HostAssets("runway", self.tools.asset_store)
        with other._approved_execution("kern.example"), self.assertRaises(AssetError):
            with other.public_asset_url(self.metadata.asset_id):
                self.fail("wrong tool")
