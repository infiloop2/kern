"""Provider-mocked Topaz contracts: no paid generations in this suite."""
from contextlib import contextmanager
import io
import unittest
from unittest.mock import patch

from host.tools.fal_ai import topaz, media as fal_media
from host.tools.results import ActionExecuted, ActionFailed, StreamingAsset, StreamingAssetError
from host.tools.shared import media
from host.tools.shared.web import WebRequestError
from test_tools import FakeHostAPI, assert_matches_output_schema

REAL_UPLOAD = fal_media.upload

REQUEST_ID = "764cabcf-b745-4b3e-ae38-1200304cf45b"


class TopazTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI(config={"FAL_API_KEY": "private-key"})
        self.tool = topaz.TopazTool()
        upload = patch.object(fal_media, "upload", return_value="https://v3.fal.media/source")
        self.upload = upload.start()
        self.addCleanup(upload.stop)


    def media(self, kind, name="s" * 43):
        return self.api.assets.add(name, filename=name, media_type={"image": "image/png", "video": "video/mp4", "audio": "audio/mpeg"}[kind])

    def execute(self, action, values):
        result = self.tool.execute(action, values, self.api)
        if isinstance(result, ActionExecuted):
            assert_matches_output_schema(self, topaz.MANIFEST, action, result)
        return result

    def test_missing_config_fails_closed_without_network(self):
        with patch.object(topaz, "json_request") as request:
            result = self.tool.execute("get_task", {"task_id": "topaz_image_" + REQUEST_ID}, FakeHostAPI(config={}))
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("not set", result.error)
            request.assert_not_called()

    def test_defaults_and_route_selection(self):
        for kind, model, route in (
            ("image", None, "image/precision"),
            ("image", "CGI", "image/precision"),
            ("video", None, "video/precision"),
            ("video", "Gaia 2", "video/precision"),
            ("video", "Starlight Precise 2.6", "video/generative"),
        ):
            with self.subTest(kind=kind, model=model), patch.object(topaz, "json_request", return_value={"request_id": REQUEST_ID}) as request:
                values = {f"{kind}_asset_id": self.media(kind)}
                if model:
                    values["model"] = model
                result = self.execute(f"upscale_{kind}", values)
                self.assertIsInstance(result, ActionExecuted)
                args, kwargs = request.call_args
                self.assertEqual(args, ("POST", topaz.QUEUE_BASE + "/" + route))
                self.assertEqual(kwargs["headers"]["X-Fal-Store-IO"], "0")
                self.assertEqual(kwargs["headers"]["X-Fal-Object-Lifecycle-Preference"], topaz.LIFECYCLE)
                self.assertNotIn("target_fps", kwargs["body"])
                if kind == "image":
                    self.assertEqual(kwargs["body"]["output_format"], "png")
                    self.assertFalse(kwargs["body"]["face_enhancement"])
                else:
                    self.assertTrue(kwargs["body"]["H264_output"])
                self.assertFalse(self.api.costs.calls)

    def test_invalid_settings_never_upload_or_submit(self):
        asset = self.api.assets.add(media_type="video/mp4", data=b"video")
        invalid = [
            {"upscale_factor": 0}, {"upscale_factor": True}, {"upscale_factor": float("nan")},
            {"upscale_factor": float("inf")}, {"upscale_factor": "2"}, {"upscale_factor": 5},
            {"target_fps": 120}, {"target_fps": 30.5}, {"target_fps": True},
            {"h264_output": "true"}, {"model": "arbitrary/model"}, {"grain": .11},
            {"model": "Gaia 2", "upscale_factor": 4}, {"model": "Proteus Natural", "upscale_factor": 1},
            {"model": "Starlight HQ", "compression": .5}, {"softness": 2},
            {"model": "Starlight HQ", "softness": 3}, {"webhook_url": "https://example.com"},
            {"video_url": "https://example.com/video.mp4"},
        ]
        with patch.object(topaz, "json_request") as request, patch.object(fal_media, "stream_request_bytes") as upload:
            for settings in invalid:
                with self.subTest(settings=settings):
                    self.assertIsInstance(self.execute("upscale_video", {"video_asset_id": asset, **settings}), ActionFailed)
            request.assert_not_called()
            upload.assert_not_called()
        self.assertIn(asset, self.api.assets.records)

    def test_source_urls_are_rejected_including_encoded_values(self):
        urls = ("https://example.com/?ssn=219-09-9999", "https://example.com/?ssn=%32%31%39-09-9999",
                "http://example.com/file", "data:image/png;base64,aaaa", "https://user:pass@example.com/file")
        with patch.object(topaz, "json_request") as request:
            for kind in ("image", "video"):
                for url in urls:
                    with self.subTest(kind=kind, url=url):
                        self.assertIsInstance(self.execute(f"upscale_{kind}", {f"{kind}_url": url}), ActionFailed)
            request.assert_not_called()

    def test_staged_upload_streams_bytes_without_key_or_original_name(self):
        for kind, mime in (("image", "image/png"), ("video", "video/mp4")):
            with self.subTest(kind=kind):
                asset = self.api.assets.add(asset_id="a" * 43, filename="private-customer-name", media_type=mime, data=b"pixels")
                responses = [{"upload_url": "https://storage.googleapis.com/fal/input?signature=opaque", "file_url": "https://storage.googleapis.com/fal/input"}, {"request_id": REQUEST_ID}]
                def upload(method, url, **kwargs):
                    self.assertEqual(method, "PUT")
                    self.assertEqual(kwargs["body"].read(), b"pixels")
                    self.assertEqual(kwargs["content_length"], 6)
                    self.assertEqual(kwargs["headers"], {"Content-Type": mime})
                    return b""
                with patch.object(fal_media, "upload", REAL_UPLOAD), patch.object(fal_media, "json_request", return_value=responses[0]) as init, patch.object(topaz, "json_request", return_value=responses[1]) as request, patch.object(fal_media, "stream_request_bytes", side_effect=upload):
                    result = self.execute(f"upscale_{kind}", {f"{kind}_asset_id": asset})
                self.assertIsInstance(result, ActionExecuted)
                self.assertNotIn(asset, self.api.assets.records)
                self.assertEqual(init.call_args.args[1], fal_media.UPLOAD_INIT)
                self.assertNotIn("private-customer", str(init.call_args_list))
                self.assertEqual(request.call_args.kwargs["body"][f"{kind}_url"], responses[0]["file_url"])

    def test_bad_staged_inputs_rejected_before_network(self):
        self.api.assets.add(asset_id="a" * 43, media_type="video/mp4")
        with patch.object(topaz, "json_request") as request:
            for values in ({}, {"image_asset_id": "a" * 43}, {"image_asset_id": "../escape"}, {"image_asset_id": "b" * 43}, {"image_url": None}, {"image_url": "https://example.com/a", "model": "CGI", "fix_compression": .2}):
                self.assertIsInstance(self.execute("upscale_image", values), ActionFailed)
            request.assert_not_called()

    def test_signed_upload_destination_is_bounded(self):
        asset = self.api.assets.add(media_type="image/png")
        for bad in ("http://storage.googleapis.com/upload", "https://attacker.example/upload", "https://storage.googleapis.com.attacker.example/upload", "https://127.0.0.1/upload"):
            with self.subTest(bad=bad), patch.object(fal_media, "upload", REAL_UPLOAD), patch.object(fal_media, "json_request", return_value={"upload_url": bad, "file_url": "https://v3.fal.media/input.png"}), patch.object(fal_media, "stream_request_bytes") as upload:
                self.assertIsInstance(self.execute("upscale_image", {"image_asset_id": asset}), ActionFailed)
                upload.assert_not_called()

    def test_queue_poll_uses_app_root_and_handles_lifecycle(self):
        for raw, expected in (("IN_QUEUE", "queued"), ("IN_PROGRESS", "running"), ("new-state", "unknown"), ("COMPLETED", "succeeded")):
            with self.subTest(raw=raw), patch.object(topaz, "json_request", side_effect=[{"status": raw}, {"video": {"url": "https://v3.fal.media/result.mp4"}}]) as request:
                result = self.execute("get_task", {"task_id": "topaz_video_" + REQUEST_ID})
                self.assertEqual(result.result["task_status"], expected)
                self.assertEqual(request.call_args_list[0].args[1], topaz.QUEUE_BASE + "/requests/" + REQUEST_ID + "/status")
                if expected == "succeeded":
                    self.assertEqual(request.call_args.args[1], topaz.QUEUE_BASE + "/requests/" + REQUEST_ID)
                else:
                    self.assertEqual(request.call_count, 1)
                self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))

    def test_failures_do_not_leak_provider_body_or_resubmit(self):
        scenarios = [
            [{"status": "COMPLETED", "error": "secret-provider-body"}],
            [{"status": "COMPLETED"}, {}],
            [{"status": "COMPLETED"}, WebRequestError("secret-provider-body", status=422)],
        ]
        for responses in scenarios:
            with self.subTest(responses=responses), patch.object(topaz, "json_request", side_effect=responses) as request:
                result = self.execute("get_task", {"task_id": "topaz_image_" + REQUEST_ID})
                self.assertEqual(result.result["task_status"], "failed")
                self.assertNotIn("secret-provider-body", str(result))
                self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))
        for code in (401, 402, 403, 404, 422, 429, 500):
            with patch.object(topaz, "json_request", side_effect=WebRequestError("secret-provider-body", status=code)) as request:
                result = self.execute("upscale_video", {"video_asset_id": self.media("video")})
                self.assertIsInstance(result, ActionFailed)
                self.assertNotIn("secret-provider-body", result.error)
                self.assertEqual(request.call_count, 1)

    def test_task_id_and_media_kind_cannot_select_arbitrary_paths(self):
        with patch.object(topaz, "json_request") as request:
            for task in ("../escape", "topaz_image_" + REQUEST_ID + "/../../other", "other_" + REQUEST_ID):
                self.assertIsInstance(self.execute("get_task", {"task_id": task}), ActionFailed)
            self.assertIsInstance(self.execute("save_image", {"task_id": "topaz_video_" + REQUEST_ID}), ActionFailed)
            request.assert_not_called()

    def test_save_stream_has_no_credentials_and_checks_type_and_size(self):
        for kind, mime, content in (("image", "image/png", b"pixels"), ("video", "video/mp4", b"v" * 1024)):
            @contextmanager
            def stream(method, url, **kwargs):
                self.assertEqual(method, "GET")
                self.assertNotIn("headers", kwargs)
                yield io.BytesIO(content), {"content-length": str(len(content)), "content-type": mime}
            with self.subTest(kind=kind), patch.object(topaz, "json_request", side_effect=[{"status": "COMPLETED"}, {kind: {"url": "https://v3.fal.media/output"}}]), patch.object(media, "open_response_stream", stream):
                result = self.execute("save_" + kind, {"task_id": "topaz_" + kind + "_" + REQUEST_ID})
                self.assertIsInstance(result, StreamingAsset)
                with result.open_stream() as opened:
                    self.assertEqual(opened.source.read(), content)
                    self.assertEqual(opened.media_type, mime)
        for headers in ({"content-length": "300000000", "content-type": "image/png"}, {"content-length": "10", "content-type": "text/html"}, {"content-type": "image/png"}):
            @contextmanager
            def invalid_stream(*args, **kwargs):
                yield io.BytesIO(b"x" * 10), headers
            with patch.object(media, "open_response_stream", invalid_stream), self.assertRaises(StreamingAssetError):
                with media.open_downloaded_image("https://v3.fal.media/output", provider="Topaz", filename_stem="out", map_failure=topaz._failure):
                    pass

    def test_unknown_submission_outcome_retains_asset_and_never_retries(self):
        asset = self.api.assets.add(media_type="image/png")
        with patch.object(fal_media, "upload", return_value="https://v3.fal.media/source.png"), patch.object(topaz, "json_request", return_value={"request_id": "bad"}) as request:
            result = self.execute("upscale_image", {"image_asset_id": asset})
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("duplicate charges", result.error)
            self.assertIn(asset, self.api.assets.records)
            self.assertEqual(request.call_count, 1)

    def test_non_default_options_translate_exactly(self):
        endpoint, body, _ = topaz._request(self.api, "video", {"video_asset_id": self.media("video"), "target_fps": 30, "h264_output": False, "noise": .2, "grain": .01, "upscale_factor": 1.5})
        self.assertEqual(body["target_fps"], 30)
        self.assertFalse(body["H264_output"])
        self.assertEqual(body["noise"], .2)
        self.assertEqual(body["upscale_factor"], 1.5)
        self.assertNotIn("h264_output", body)


if __name__ == "__main__":
    unittest.main()
