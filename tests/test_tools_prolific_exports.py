"""Export transport is mocked; the real host stream and file writer are exercised."""
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from host.runtime.agent_shim import mcp_shim
from host.runtime.tools import api as tools_api, tools_host
from host.tools import prolific as p
from host.tools.prolific import exports
from host.tools.results import ActionExecuted, ActionFailed, StreamingAsset, StreamingAssetError
from host.tools.shared.web import WebRequestError
from test_tools import FakeHostAPI
from test_tools_api import _MemoryResponse

ID = "645e4403bdd06d5f66d8fbde"
EXPORT = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
URL = "https://exports.example.com/archive.zip?signature=PRIVATE_SIGNED_VALUE"


class ProlificExportTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI()
        self.api.config["PROLIFIC_TOKEN"] = "PRIVATE_TOKEN"
        self.params = {"collection_id": ID, "export_id": EXPORT}
        self.status = {"status": "complete", "url": URL, "expires_at": "2026-10-14T10:00:00Z"}
        self.original_request = p.request
        self.request = self.enterContext(patch.object(p, "request", side_effect=lambda *a, **k: (self.status.copy(), {})))
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            for name, value in {"responses.jsonl": b'{"answer":"hello"}\n', "collection.json": b'{"title":"Task"}',
                                "README.md": b"Provider instructions", "files/original.bin": bytes(range(256))}.items():
                archive.writestr(name, value)
        self.data = data.getvalue()
        self.headers = {"content-length": str(len(self.data)), "content-type": "application/zip"}

        @contextmanager
        def download(*args, **kwargs):
            yield io.BytesIO(self.data), self.headers
        self.download = self.enterContext(patch.object(exports, "open_response_stream", side_effect=download))

    def call(self, action="save_collection_export"):
        return p.BUNDLED_TOOL.execute(action, self.params, self.api)

    def test_poll_preserves_status_id_and_hides_signed_url_even_in_fallback(self):
        self.status["extension"] = URL
        result = self.call("get_collection_export_status")
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(result.result["status"], "complete")
        self.assertEqual(result.result["export_id"], EXPORT)
        self.assertNotIn("url", result.result)
        self.assertNotIn(URL, json.dumps(result.result))
        self.download.assert_not_called()

    def test_pending_and_failed_exports_do_not_download_or_restart(self):
        for status in ("generating", "failed"):
            self.status = {"status": status}
            result = self.call()
            self.assertIsInstance(result, ActionFailed)
            self.assertIn(EXPORT, result.error)
            self.assertIn(status, result.error)
        self.assertTrue(all(c.args[0] == "GET" for c in self.request.call_args_list))
        self.download.assert_not_called()

    def materialize(self, asset, directory):
        headers = {}
        handler = Mock(wfile=io.BytesIO())
        handler._validated_stream_metadata = tools_api.ToolsRequestHandler._validated_stream_metadata
        handler.send_header.side_effect = lambda name, value: headers.update({name: value})
        with patch.object(tools_host, "finish_streaming_action"):
            tools_api.ToolsRequestHandler._send_streaming_action(handler, Mock(asset=asset))
        response = _MemoryResponse(handler.wfile.getvalue(), **headers)
        with patch.dict(os.environ, {"HOME": directory}):
            return mcp_shim._materialize_stream(response)

    def test_complete_export_crosses_real_service_and_workspace_file_boundary_losslessly(self):
        asset = self.call()
        self.assertIsInstance(asset, StreamingAsset)
        self.request.assert_called_once_with("GET", f"/data-collection/collections/{ID}/export/{EXPORT}", "PRIVATE_TOKEN")
        self.download.assert_not_called()  # Lazy open by tools service.
        with tempfile.TemporaryDirectory() as directory:
            result = self.materialize(asset, directory)
            self.assertRegex(result["path"], r"^/tool_assets/asset-[0-9a-f]{32}\.zip$")
            self.assertEqual(Path(result["filesystem_path"]).read_bytes(), self.data)
            self.assertEqual(result["media_type"], "application/zip")
            self.assertEqual(result["size_bytes"], len(self.data))
            self.assertIn(EXPORT, result["summary"])
            self.assertNotIn(URL, json.dumps(result))
        self.download.assert_called_once_with("GET", URL, headers={"Accept-Encoding": "identity"},
                                              failure_message="Prolific export download failed.", timeout=120)
        self.assertEqual(self.api.costs.calls, [("0", "")])

    def test_short_download_is_rejected_and_partial_file_removed(self):
        self.headers["content-length"] = str(len(self.data) + 1)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                self.materialize(self.call(), directory)
            self.assertEqual(list((Path(directory) / "tool_assets").iterdir()), [])

    def test_storage_failure_has_no_url_or_token_and_retry_only_refreshes_status(self):
        self.download.side_effect = WebRequestError(URL, status=403, body=b"PRIVATE_TOKEN")
        with self.assertRaises(StreamingAssetError) as caught:
            with self.call().open_stream():
                pass
        self.assertNotIn(URL, str(caught.exception))
        self.assertNotIn("PRIVATE_TOKEN", str(caught.exception))
        self.assertIn("same IDs", str(caught.exception))

    def test_download_metadata_bounds(self):
        for key, value in (("content-length", ""), ("content-length", "200000001"),
                           ("content-length", "0"), ("content-type", "text/html"), ("content-encoding", "gzip")):
            with self.subTest(key=key, value=value):
                with patch.dict(self.headers, {key: value}):
                    with self.assertRaises(StreamingAssetError):
                        with self.call().open_stream():
                            pass

    def test_untrusted_url_and_mismatched_export_id_fail_before_download(self):
        for url in ("http://exports.example.com/a", "https://127.0.0.1/a", "https://user:pass@example.com/a", "https://example.com:8000/a"):
            self.status["url"] = url
            self.assertIsInstance(self.call(), ActionFailed)
        self.status.update(url=URL, export_id=ID)
        self.assertIsInstance(self.call(), ActionFailed)
        self.download.assert_not_called()

    def test_demographic_csv_saved_completely_with_redaction_and_default_filters(self):
        self.request.side_effect = None
        self.request.return_value = ("participant_id,age,credential\nexample,30,PRIVATE_TOKEN\n", {})
        result = p.BUNDLED_TOOL.execute("export_demographics", {"study_id": ID}, self.api)
        self.assertIsInstance(result, StreamingAsset)
        self.request.assert_called_once_with("POST", f"/studies/{ID}/demographic-export/", "PRIVATE_TOKEN", body={"filters": []}, csv=True)
        with tempfile.TemporaryDirectory() as directory:
            saved = self.materialize(result, directory)
            data = Path(saved["filesystem_path"]).read_text()
            self.assertEqual(data, "participant_id,age,credential\nexample,30,[REDACTED]\n")
            self.assertTrue(saved["path"].endswith(".csv"))
            self.assertEqual(saved["media_type"], "text/csv")
        self.download.assert_not_called()
        self.assertEqual(self.api.costs.calls, [("0", "")])

    def test_demographic_csv_rejects_oversize_and_invalid_utf8(self):
        # Exercise the bounded API response path, not the request mock.
        self.request.side_effect = None
        with patch.object(p, "request", wraps=self.original_request):
            for data in (b"x" * (1024 * 1024 + 1), b"\xff"):
                with patch.object(p, "_request_bytes_and_headers", return_value=(data, {})):
                    result = p.BUNDLED_TOOL.execute("export_demographics", {"study_id": ID}, self.api)
                    self.assertIsInstance(result, ActionFailed)
        self.assertEqual(self.api.costs.calls, [])

    def test_export_creation_and_status_have_distinct_required_inputs(self):
        for action, params in (("export_collection", self.params), ("get_collection_export_status", {"collection_id": ID}),
                               ("get_collection_export_status", {"export_id": EXPORT})):
            with self.subTest(action=action, params=params):
                self.assertIsInstance(p.BUNDLED_TOOL.execute(action, params, self.api), ActionFailed)
        self.request.assert_not_called()

    def test_arbitrary_url_input_rejected(self):
        self.params["url"] = URL
        self.assertIsInstance(self.call(), ActionFailed)
        self.request.assert_not_called()
