"""Magnific contracts, validation-before-upload and durable image saving."""
import io
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from host.tools import runway
from host.tools.results import ActionExecuted, ActionFailed, StreamingAsset, StreamingAssetError
from host.tools.shared import media
from test_tools import assert_matches_output_schema
from test_tools_runway import api_with_key


class RunwayUpscaleTests(unittest.TestCase):
    def setUp(self):
        self.api = api_with_key()
        self.tool = runway.RunwayTool()
        upload = patch.object(runway, "_upload_staged_asset", return_value="runway://source")
        upload.start()
        self.addCleanup(upload.stop)


    def media(self, kind, name="source"):
        return self.api.assets.add(name, filename=name, media_type={"image": "image/png", "video": "video/mp4", "audio": "audio/mpeg"}[kind])

    def test_provider_payloads(self):
        cases = [
            ("image", {"scale_factor": 4, "flavor": "sublime", "sharpen": 10, "smart_grain": 0, "ultra_detail": 30},
             {"model": "magnific_precision_upscaler_v2", "scaleFactor": 4, "flavor": "sublime", "sharpen": 10, "smartGrain": 0, "ultraDetail": 30}),
            ("video", {"resolution": "4k", "flavor": "natural", "creativity": 5, "fps_boost": True},
             {"model": "magnific_video_upscaler_creative", "resolution": "4k", "flavor": "natural", "creativity": 5, "fpsBoost": True}),
        ]
        for kind, settings, expected in cases:
            with self.subTest(kind=kind), patch.object(runway, "json_request", return_value={"id": "enhanced"}) as request:
                result = self.tool.execute(f"upscale_{kind}", {f"{kind}_asset_id": self.media(kind), **settings}, self.api)
                self.assertIsInstance(result, ActionExecuted, result)
                self.assertEqual(request.call_args.args[:2], ("POST", f"{runway.RUNWAY_API_BASE}/v1/{kind}_upscale"))
                self.assertEqual(request.call_args.kwargs["body"], {**expected, f"{kind}Uri": "runway://source"})
                self.assertEqual(result.result["output_kind"], kind)
                assert_matches_output_schema(self, runway.MANIFEST, f"upscale_{kind}", result)

    def test_invalid_settings_do_not_upload_or_submit(self):
        asset = self.api.assets.add("frame", filename="frame.png", media_type="image/png")
        for setting in ({"scale_factor": True}, {"scale_factor": 3}, {"sharpen": 1.5}, {"smart_grain": 101}, {"flavor": "natural"}, {"fps_boost": True}, {"image_url": "https://example.com/duplicate"}):
            with self.subTest(setting=setting), patch.object(runway, "_upload_staged_asset") as upload, patch.object(runway, "json_request") as request:
                self.assertIsInstance(self.tool.execute("upscale_image", {"image_asset_id": asset, **setting}, self.api), ActionFailed)
                upload.assert_not_called()
                request.assert_not_called()

    def test_staged_inputs_consumed_only_after_success_and_no_paid_retry(self):
        for kind in ("image", "video"):
            for success in (True, False):
                asset = self.api.assets.add("source", filename="source", media_type=f"{kind}/" + ("png" if kind == "image" else "mp4"))
                with patch.object(runway, "_upload_staged_asset", return_value="runway://source") as upload, patch.object(runway, "json_request", return_value={"id": "enhanced"} if success else {}) as request:
                    result = self.tool.execute(f"upscale_{kind}", {f"{kind}_asset_id": asset}, self.api)
                    self.assertIsInstance(result, ActionExecuted if success else ActionFailed)
                    self.assertEqual(upload.call_count, 1)
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(asset in self.api.assets.records, not success)
                    self.assertEqual(request.call_args.kwargs["body"][f"{kind}Uri"], "runway://source")

    def test_published_estimates_and_conservative_unknowns(self):
        cases = [
            ("image", {}, "1.50"),
            ("image", {"source_width": 2048}, "1.50"),
            ("image", {"source_width": 2048, "source_height": 1024}, "0.25"),
            ("image", {"source_width": 2049, "source_height": 1024}, "1.50"),
            ("image", {"source_width": 512, "source_height": 512, "scale_factor": 16}, "1.50"),
            ("video", {"estimated_output_frames": 300, "resolution": "720p"}, "2.10"),
            ("video", {"estimated_output_frames": 300, "resolution": "1k"}, "2.10"),
            ("video", {"estimated_output_frames": 300}, "2.70"),
            ("video", {"estimated_output_frames": 300, "resolution": "4k", "fps_boost": True}, "3.60"),
            ("video", {"estimated_output_frames": 1, "resolution": "4k"}, "0.02"),
            ("video", {"estimated_output_frames": 1}, "0.01"),
            ("video", {}, "32.40"),
            ("video", {"resolution": "4k", "fps_boost": True}, "86.40"),
        ]
        for number, (kind, settings, amount) in enumerate(cases):
            task_id = f"price-{number}"
            with self.subTest(kind=kind, settings=settings), patch.object(runway, "json_request", return_value={"id": task_id}) as request:
                result = self.tool.execute(f"upscale_{kind}", {f"{kind}_asset_id": self.media(kind), **settings}, self.api)
                self.assertIsInstance(result, ActionExecuted, result)
                self.assertEqual(result.result["estimated_cost_usd"], amount)
                self.assertIn("not final billing", result.result["cost_estimate_basis"])
                self.assertEqual(self.api.costs.records[f"task:{task_id}"]["amount_usd"], amount)
                for field in ("source_width", "source_height", "estimated_output_frames"):
                    self.assertNotIn(field, request.call_args.kwargs["body"])
                assert_matches_output_schema(self, runway.MANIFEST, f"upscale_{kind}", result)

    def test_estimates_require_accepted_task_and_deduplicate(self):
        for response in ({}, {"id": "invalid/id"}):
            with patch.object(runway, "json_request", return_value=response):
                self.assertIsInstance(self.tool.execute("upscale_image", {"image_asset_id": self.media("image")}, self.api), ActionFailed)
                self.assertEqual(self.api.costs.records, {})
        with patch.object(runway, "json_request", return_value={"id": "same-task"}):
            for _ in range(2):
                self.tool.execute("upscale_image", {"image_asset_id": self.media("image")}, self.api)
        self.assertEqual(len(self.api.costs.records), 1)

    def test_invalid_estimate_metadata_rejected_before_upload(self):
        for kind, field, maximum in (("image", "source_width", 100000), ("image", "source_height", 100000), ("video", "estimated_output_frames", 1000000)):
            asset = self.api.assets.add("source", filename="source", media_type=f"{kind}/" + ("png" if kind == "image" else "mp4"))
            for value in (True, 1.5, 0, -1, maximum + 1, "300"):
                with self.subTest(field=field, value=value), patch.object(runway, "_upload_staged_asset") as upload, patch.object(runway, "json_request") as request:
                    self.assertIsInstance(self.tool.execute(f"upscale_{kind}", {f"{kind}_asset_id": asset, field: value}, self.api), ActionFailed)
                    upload.assert_not_called()
                    request.assert_not_called()
        self.assertEqual(self.api.costs.records, {})

    def test_save_image_uses_authoritative_url_and_bounded_image_stream(self):
        @contextmanager
        def stream(*args, **kwargs):
            self.assertEqual(args[:2], ("GET", "https://example.com/output.png"))
            yield io.BytesIO(b"image"), {"content-type": "image/png", "content-length": "5"}
        with patch.object(runway, "json_request", return_value={"status": "SUCCEEDED", "output": ["https://example.com/output.png"]}), patch.object(media, "open_response_stream", stream):
            result = self.tool.execute("save_image", {"task_id": "enhanced"}, self.api)
            self.assertIsInstance(result, StreamingAsset)
            with result.open_stream() as opened:
                self.assertEqual(opened.filename, "runway-enhanced.png")
                self.assertEqual(opened.source.read(), b"image")

    def test_image_download_rejects_non_images_and_oversize(self):
        for mime, size in (("text/html", "5"), ("image/png", "200000001")):
            @contextmanager
            def stream(*args, **kwargs):
                yield io.BytesIO(b"bad"), {"content-type": mime, "content-length": size}
            with patch.object(media, "open_response_stream", stream), self.assertRaises(StreamingAssetError):
                with media.open_downloaded_image("https://example.com/output", provider="Runway", filename_stem="image", map_failure=str):
                    pass
