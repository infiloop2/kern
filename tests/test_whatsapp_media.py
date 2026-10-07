"""Private staging, immutable approvals, and bounded child handoff; no provider calls."""
from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.runtime.agent_shim import mcp_shim
from host.runtime.tools import api as tools_api
from host.runtime.tools.assets import AssetError, ToolAssetStore
from host.tools import whatsapp
from host.tools.host_api import AssetMetadata
from host.tools.results import ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.whatsapp.gateway import MEDIA_REQUEST_TIMEOUT_SECONDS, WhatsAppGateway, WhatsAppGatewayError
from host.tools.whatsapp.media import media_snapshot
from test_tools import FakeHostAPI
from test_tools_whatsapp import CONNECTED

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 504
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"v" * 488
JPEG = b"\xff\xd8\xff" + b"j" * 509


class WhatsAppMediaTests(unittest.TestCase):
    def queue(self, *, filename="frame.png", mime="image/png", data=PNG, text=None):
        api = FakeHostAPI()
        asset_id = api.assets.add(filename=filename, media_type=mime, data=data)
        tool_input = {"recipient": "+447700900123", "media_asset_id": asset_id}
        if text is not None:
            tool_input["text"] = text
        with patch.object(whatsapp, "gateway_request", return_value=CONNECTED) as gateway:
            result = whatsapp.WhatsAppTool().execute("send_message", tool_input, api)
        self.assertIsInstance(result, ActionPendingApproval)
        gateway.assert_called_once_with("status")
        record = api.approvals.get(result.approval_id)
        return api, record, asset_id

    def test_media_only_and_caption_queue_exact_snapshot_without_upload(self):
        for filename, mime, data, text in (("frame.png", "image/png", PNG, None),
                                          ("frame.jpg", "image/jpeg", JPEG, "Exact 📷 caption"),
                                          ("clip.mp4", "video/mp4", MP4, "Exact 🎬 caption")):
            with self.subTest(filename=filename), patch.object(whatsapp, "gateway_send_media") as send:
                api, record, asset_id = self.queue(filename=filename, mime=mime, data=data, text=text)
                self.assertEqual(record.payload["text"], text or "")
                self.assertEqual(record.payload["media_asset"], media_snapshot(api.assets.describe(asset_id)))
                self.assertIn(filename, record.summary)
                send.assert_not_called()

    def test_media_execution_passes_exact_bytes_and_caption_only_after_account_check(self):
        api, record, _ = self.queue(text="Exact caption")
        def send(params, source, metadata):
            self.assertEqual(source.read(), PNG)
            self.assertEqual(params, {"account_id": CONNECTED["account"]["id"],
                                     "recipient": "+447700900123", "text": "Exact caption"})
            self.assertEqual(metadata.sha256, hashlib.sha256(PNG).hexdigest())
            return {"message_id": "sent-1"}
        with patch.object(whatsapp, "gateway_request", return_value=CONNECTED) as gateway, \
             patch.object(whatsapp, "gateway_send_media", side_effect=send) as upload:
            result = whatsapp.WhatsAppTool().execute_approved(record, api)
        self.assertIsInstance(result, ApprovalExecuted)
        gateway.assert_called_once_with("status")
        upload.assert_called_once()

    def test_changed_metadata_expired_missing_or_wrong_scope_never_uploads(self):
        for field, value in (("sha256", "a" * 64), ("size_bytes", 513), ("filename", "changed.png"),
                             ("media_type", "image/jpeg"), ("expires_at", 1)):
            with self.subTest(field=field):
                api, record, asset_id = self.queue()
                metadata, data = api.assets.records[asset_id]
                api.assets.records[asset_id] = (replace(metadata, **{field: value}), data)
                with patch.object(whatsapp, "gateway_request", return_value=CONNECTED), \
                     patch.object(whatsapp, "gateway_send_media") as upload:
                    self.assertIsInstance(whatsapp.WhatsAppTool().execute_approved(record, api), ActionFailed)
                upload.assert_not_called()
        api, record, _ = self.queue()
        with patch.object(api.assets, "describe", side_effect=AssetError("invalid or expired")), \
             patch.object(whatsapp, "gateway_request", return_value=CONNECTED), \
             patch.object(whatsapp, "gateway_send_media") as upload:
            self.assertIsInstance(whatsapp.WhatsAppTool().execute_approved(record, api), ActionFailed)
        upload.assert_not_called()

    def test_changed_account_or_disconnect_never_uploads_media(self):
        api, record, _ = self.queue()
        for status in ({**CONNECTED, "connected": False},
                       {**CONNECTED, "account": {"id": "other", "label": "Other"}}):
            with patch.object(whatsapp, "gateway_request", return_value=status), \
                 patch.object(whatsapp, "gateway_send_media") as upload:
                self.assertIsInstance(whatsapp.WhatsAppTool().execute_approved(record, api), ActionFailed)
            upload.assert_not_called()

    def test_invalid_combinations_and_unknown_fields_fail_before_status_or_approval(self):
        for extra in ({}, {"text": ""}, {"text": 1}, {"media_asset_id": ""},
                      {"media_asset_id": None}, {"media_asset_id": ["a", "b"]},
                      {"text": "hello", "url": "https://example.test/a.mp4"},
                      {"text": "😀" * 4097}):
            with self.subTest(extra=extra), patch.object(whatsapp, "gateway_request") as gateway:
                api = FakeHostAPI()
                self.assertIsInstance(whatsapp.WhatsAppTool().execute(
                    "send_message", {"recipient": "+447700900123", **extra}, api), ActionFailed)
                gateway.assert_not_called()
                self.assertEqual(api.approvals.records, {})
        self.queue(text="😀" * 4096)

    def test_non_media_and_unsupported_formats_are_not_sendable(self):
        for mime, filename in (("image/webp", "a.webp"), ("video/quicktime", "a.mov"), ("audio/mpeg", "a.mp3")):
            api = FakeHostAPI()
            asset_id = api.assets.add(filename=filename, media_type=mime, data=PNG)
            with self.subTest(mime=mime), patch.object(whatsapp, "gateway_request") as gateway:
                self.assertIsInstance(whatsapp.WhatsAppTool().execute("send_message",
                    {"recipient": "+447700900123", "media_asset_id": asset_id}, api), ActionFailed)
                gateway.assert_not_called()

    def test_handoff_hash_checks_before_rpc_and_deletes_private_copy_on_all_outcomes(self):
        metadata = AssetMetadata("asset", "frame.png", "image/png", len(PNG),
                                 hashlib.sha256(PNG).hexdigest(), 2_000_000_000)
        for data, failure in ((PNG, None), (PNG, WhatsAppGatewayError("outcome is unknown")),
                              (PNG[:-1], None), (PNG + b"extra", None), (b"x" * 512, None)):
            with tempfile.TemporaryDirectory() as directory, self.subTest(size=len(data), failure=failure):
                gateway = WhatsAppGateway(state_dir=Path(directory))
                def request(method, params, *, timeout_seconds):
                    self.assertEqual(method, "send_message")
                    self.assertEqual(timeout_seconds, MEDIA_REQUEST_TIMEOUT_SECONDS)
                    private_path = Path(params["media"]["path"])
                    self.assertEqual(private_path.read_bytes(), PNG)
                    self.assertEqual(private_path.stat().st_mode & 0o777, 0o600)
                    self.assertNotIn("bytes", params["media"])
                    if failure:
                        raise failure
                    return {"message_id": "one"}
                with patch("host.tools.whatsapp.gateway.state.enabled_tool_ids", return_value={"whatsapp"}), \
                     patch.object(gateway, "_start_locked"), patch.object(gateway, "request", side_effect=request) as rpc:
                    if data != PNG or failure:
                        with self.assertRaises(WhatsAppGatewayError):
                            gateway.send_media({}, io.BytesIO(data), metadata)
                    else:
                        self.assertEqual(gateway.send_media({}, io.BytesIO(data), metadata), {"message_id": "one"})
                    self.assertEqual(rpc.call_count, int(data == PNG))
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_disabled_handoff_does_not_start_or_read_media(self):
        api, _, asset_id = self.queue()
        source = io.BytesIO(PNG)
        gateway = WhatsAppGateway()
        with patch("host.tools.whatsapp.gateway.state.enabled_tool_ids", return_value=set()), \
             patch.object(gateway, "_start_locked") as start:
            with self.assertRaisesRegex(WhatsAppGatewayError, "disabled"):
                gateway.send_media({}, source, api.assets.describe(asset_id))
        start.assert_not_called()
        self.assertEqual(source.tell(), 0)

    def test_disconnect_removes_orphaned_handoff_without_touching_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "send-orphan").write_bytes(PNG)
            (root / "unrelated").write_bytes(b"keep")
            gateway = WhatsAppGateway(state_dir=root)
            self.assertFalse(gateway.disconnect()["retained_data"])
            self.assertEqual([path.name for path in root.iterdir()], ["unrelated"])

    def test_shim_and_real_service_stage_whatsapp_privately_and_reject_disabled_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = tools_api.ToolsServer(str(root / "tools.sock"), frozenset({os.getuid()}), asset_root=root / "assets")
            threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
            try:
                for name, data in (("frame.png", PNG), ("clip.mp4", MP4)):
                    (root / name).write_bytes(data)
                with patch.dict(os.environ, {"HOME": directory}), \
                     patch.object(mcp_shim, "SOCKET_PATH", str(root / "tools.sock")), \
                     patch.object(tools_api.state, "enabled_tool_ids", return_value={"whatsapp"}) as enabled:
                    for kind, name, data in (("image", "frame.png", PNG), ("video", "clip.mp4", MP4)):
                        result = mcp_shim._stage_asset({"path": "/" + name, "for_tool": "whatsapp"}, kind=kind)
                        asset_id = result[kind + "_asset_id"]
                        with server.asset_store.open("whatsapp", asset_id) as source:
                            self.assertEqual(source.read(), data)
                        with self.assertRaises(AssetError):
                            server.asset_store.describe("instagram", asset_id)
                        with self.assertRaisesRegex(AssetError, "private authenticated"):
                            server.asset_store.create_asset_grant("whatsapp", asset_id)
                    enabled.return_value = set()
                    with self.assertRaisesRegex(RuntimeError, "not enabled"):
                        mcp_shim._stage_image({"path": "/frame.png", "for_tool": "whatsapp"})
            finally:
                server.shutdown()
                server.server_close()

    def test_whatsapp_staging_rejects_bad_signatures_formats_and_oversize_before_reading(self):
        with tempfile.TemporaryDirectory() as directory:
            store = ToolAssetStore(Path(directory) / "assets")
            for kind, name, mime, size, data in (
                ("image", "a.png", "image/png", 512, b"x" * 512),
                ("video", "a.mp4", "video/mp4", 512, b"x" * 512),
                ("image", "a.webp", "image/webp", 512, PNG),
                ("video", "a.mov", "video/quicktime", 512, MP4),
                ("image", "a.png", "image/png", 5_000_001, PNG),
                ("video", "a.mp4", "video/mp4", 16_000_001, MP4),
            ):
                source = io.BytesIO(data)
                with self.subTest(name=name, size=size), self.assertRaises(AssetError):
                    store.stage(kind=kind, tool_id="whatsapp", filename=name, media_type=mime,
                                size_bytes=size, source=source)
                if size > 512 or mime not in {"image/png", "video/mp4"}:
                    self.assertEqual(source.tell(), 0)
            self.assertEqual(list(store._root.iterdir()), [])
