"""OpenRouter contracts exercised without credentials or provider network calls."""

from contextlib import contextmanager
import base64
import io
import unittest
import urllib.error
from unittest.mock import patch

from host.runtime.tools import tools_host
from host.runtime.tools.tools_host import BUNDLED_TOOLS
from host.tools import openrouter
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted, StreamingAsset, StreamingAssetError
from host.tools.shared import media, web
from host.tools.shared.web import ProviderWarning, UnmappedProviderError, WebRequestError
from test_tools import FakeHostAPI, assert_matches_output_schema


JOB = "gen-vid-1790851200-AbC123def456GHI789jk"
MODEL = {
    "id": "heygen/heygen-video-1", "name": "HeyGen: Video 1", "supported_durations": list(range(5, 16)),
    "supported_resolutions": ["480p", "768p"], "supported_aspect_ratios": ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"],
    "supported_frame_images": ["first_frame"],
    "generate_audio": False, "seed": True, "pricing_skus": {
        "duration_seconds_480p": "0.02", "duration_seconds_768p": "0.03",
        "reference_duration_seconds_480p": "0.04", "reference_duration_seconds_768p": "0.06",
    },
}
CATALOG = {"data": [MODEL]}


class OpenRouterTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI(config={"OPENROUTER_API_KEY": "private-test-key"})
        self.tool = openrouter.BUNDLED_TOOL

    def execute(self, action, tool_input, responses):
        with patch.object(openrouter, "json_request", side_effect=responses) as request:
            if action == "create_heygen_video" and any(k in tool_input for k in ("reference_audio_asset_ids", "reference_video_asset_ids")):
                try:
                    body, uploads, estimate = openrouter._generation_body(tool_input, self.api, {})
                    result = openrouter._submit(body, uploads, estimate, self.api, {})
                except (ValueError, RuntimeError) as exc:
                    result = ActionFailed(str(exc))
            else:
                result = self.tool.execute(action, tool_input, self.api)
        if isinstance(result, ActionExecuted):
            assert_matches_output_schema(self, self.tool.manifest, action, result)
        return result, request

    def test_discovered_tool_and_exact_action_schemas(self):
        self.assertIs(BUNDLED_TOOLS["openrouter"], self.tool)
        self.assertEqual([a.id for a in self.tool.manifest.actions], ["create_heygen_video", "create_heygen_reference_video", "get_task", "save_video"])
        self.assertTrue(self.tool.manifest.reports_cost)
        self.assertTrue(self.tool.manifest.actions[-1].returns_asset)
        self.assertFalse(self.tool.manifest.actions[-1].output_schema)

    def test_generate_uses_heygen_and_preserves_provider_defaults(self):
        result, request = self.execute("create_heygen_video", {"prompt": "A glowing neon city"}, [CATALOG, {"id": JOB, "status": "pending", "polling_url": "https://attacker.example/"}])
        self.assertEqual(result.result["task_id"], JOB)
        self.assertEqual(result.result["task_status"], "queued")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args, ("POST", openrouter.API_BASE))
        self.assertEqual(request.call_args.kwargs["body"], {"model": MODEL["id"], "prompt": "A glowing neon city", "duration": 5, "resolution": "768p", "aspect_ratio": "16:9"})
        self.assertEqual(request.call_args.kwargs["headers"], {"Authorization": "Bearer private-test-key"})
        self.assertEqual(self.api.costs.records, {"openrouter-video:" + JOB: {"amount_usd": "0.150000000"}})

    def test_explicit_settings_pin_heygen_and_select_mode(self):
        inputs = {"prompt": "Slowly orbit a ceramic mug", "mode": "text", "duration_seconds": "12", "resolution": "480p", "aspect_ratio": "9:16", "seed": "4294967295"}
        result, request = self.execute("create_heygen_video", inputs, [CATALOG, {"id": JOB, "status": "pending"}])
        self.assertEqual(set(result.result), {"task_id", "task_status", "message"})
        self.assertEqual(request.call_args.kwargs["body"], {"prompt": inputs["prompt"], "model": MODEL["id"], "duration": 12, "resolution": "480p", "aspect_ratio": "9:16", "seed": 4294967295})
        self.assertEqual(self.api.costs.records["openrouter-video:" + JOB]["amount_usd"], "0.240000000")

    def test_estimate_uses_live_increases_and_list_rate_fallback_without_discounts(self):
        for value, expected in (("0.08", "0.400000000"), ("0.015", "0.150000000"),
                                (None, "0.150000000"), (True, "0.150000000"),
                                ("NaN", "0.150000000"), ("1e999999", "0.150000000"),
                                ("0.03000000001", "0.150000001")):
            with self.subTest(value=value):
                self.api.costs.records.clear()
                catalog = {"data": [{**MODEL, "pricing_skus": {"duration_seconds_768p": value}}]}
                result, _ = self.execute("create_heygen_video", {"prompt": "A calm coast"}, [catalog, {"id": JOB, "status": "pending", "usage": {"cost": 0.01}}])
                self.assertIsInstance(result, ActionExecuted)
                self.assertEqual(self.api.costs.records["openrouter-video:" + JOB]["amount_usd"], expected)

    def test_reference_images_use_reference_rate_without_extra_media_seconds(self):
        asset = self.image()
        result, _ = self.execute("create_heygen_video", {"prompt": "A coast with <Picture 1>", "reference_image_asset_ids": [asset], "resolution": "480p"}, [CATALOG, {"id": JOB, "status": "pending"}])
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(self.api.costs.records["openrouter-video:" + JOB]["amount_usd"], "0.200000000")

    def test_reference_video_duration_rounds_up_and_unknown_duration_leans_high(self):
        def atom(kind, payload, extended=False):
            if extended:
                return (1).to_bytes(4, "big") + kind + (len(payload) + 16).to_bytes(8, "big") + payload
            return (len(payload) + 8).to_bytes(4, "big") + kind + payload

        for version in (0, 1):
            with self.subTest(version=version):
                self.api.costs.records.clear()
                timestamps = b"\0" * (8 if version == 0 else 16)
                duration = (95_100).to_bytes(4 if version == 0 else 8, "big")
                header = bytes([version, 0, 0, 0]) + timestamps + (1000).to_bytes(4, "big") + duration
                data = atom(b"mdat", b"video") + atom(b"moov", atom(b"mvhd", header), extended=bool(version))
                asset = self.api.assets.add("V" * 32, media_type="video/mp4", data=data)
                result, _ = self.execute("create_heygen_video", {"prompt": "A coast", "reference_video_asset_ids": [asset, asset]}, [CATALOG, {"id": JOB, "status": "pending"}])
                self.assertIsInstance(result, ActionExecuted)
                self.assertEqual(self.api.costs.records["openrouter-video:" + JOB]["amount_usd"], "11.820000000")
        for data in (b"unreadable", b"\0\0\0\x01moov", atom(b"moov", atom(b"mvhd", b"\0" * 20))):
            self.api.costs.records.clear()
            asset = self.api.assets.add("V" * 32, media_type="video/quicktime", data=data)
            result, _ = self.execute("create_heygen_video", {"prompt": "A coast", "reference_video_asset_ids": [asset]}, [CATALOG, {"id": JOB, "status": "pending"}])
            self.assertIsInstance(result, ActionExecuted)
            self.assertEqual(self.api.costs.records["openrouter-video:" + JOB]["amount_usd"], "3.900000000")

    def test_invalid_inputs_never_start_paid_generation(self):
        cases = [
            {"model": "vendor/missing"}, {"model": "https://attacker.example/x"},
            {"model": "vendor/../secret"}, {"duration_seconds": "4"}, {"duration_seconds": "121"},
            {"duration_seconds": True}, {"duration_seconds": None}, {"resolution": "1080p"},
            {"resolution": "https://example.com"}, {"aspect_ratio": "2:3"},
            {"seed": "4294967296"}, {"seed": None}, {"generate_audio": "true"},
            {"image_url": "https://example.com/frame.png"}, {"provider": {}}, {"callback_url": "https://example.com/"},
            {"end_image_asset_id": "L" * 32},
            {"prompt_enhancement": "quality"},
            {"prompt": ""}, {"prompt": "😀" * 1281},
        ]
        for settings in cases:
            with self.subTest(settings=settings):
                result, request = self.execute("create_heygen_video", {"prompt": "A calm coast", **settings}, [CATALOG])
                self.assertIsInstance(result, ActionFailed)
                self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))

    def test_unsupported_seed_never_submits(self):
        result, request = self.execute("create_heygen_video", {"prompt": "A calm coast", "seed": "42"}, [{"data": [{**MODEL, "seed": None}]}])
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(request.call_count, 1)

    def image(self, asset_id="I" * 32, data=b"image" * 128):
        return self.api.assets.add(asset_id, filename="frame.png", media_type="image/png", data=data)

    def test_first_frame_uses_private_bytes_and_consumes_staged_copy(self):
        source = b"private image" * 64
        asset = self.image(data=source)
        result, request = self.execute("create_heygen_video", {"prompt": "A slow push in", "image_asset_id": asset}, [CATALOG, {"id": JOB, "status": "pending"}])
        self.assertIsInstance(result, ActionExecuted)
        body = request.call_args.kwargs["body"]
        self.assertNotIn("aspect_ratio", body)
        frame = body["frame_images"][0]
        self.assertEqual(frame["frame_type"], "first_frame")
        self.assertEqual(frame["image_url"]["url"], "data:image/png;base64," + base64.b64encode(source).decode())
        self.assertNotIn("private image", str(result.result))
        self.assertNotIn(asset, self.api.assets.records)

    def test_multimodal_references_use_https_for_audio_video_and_inline_images(self):
        image = self.image()
        video = self.api.assets.add("V" * 32, media_type="video/mp4", data=b"video" * 128)
        audio = self.api.assets.add("A" * 32, media_type="audio/wav", data=b"audio" * 128)
        inputs = {"prompt": "Animate <Picture 1> with motion from <Video 1> and sound from <Audio 1>", "mode": "reference", "reference_image_asset_ids": [image], "reference_video_asset_ids": [video], "reference_audio_asset_ids": [audio]}
        result, request = self.execute("create_heygen_video", inputs, [CATALOG, {"id": JOB, "status": "pending"}])
        self.assertIsInstance(result, ActionExecuted)
        body = request.call_args.kwargs["body"]
        self.assertNotIn("aspect_ratio", body)
        self.assertEqual([r["type"] for r in body["input_references"]], ["image_url", "video_url", "audio_url"])
        refs = body["input_references"]
        self.assertTrue(refs[0]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(refs[1]["video_url"]["url"].startswith("https://kern.example/tool-media/"))
        self.assertTrue(refs[2]["audio_url"]["url"].startswith("https://kern.example/tool-media/"))
        self.assertEqual(set(self.api.assets.records), {video, audio})

    def test_cleanup_failure_preserves_accepted_paid_job(self):
        image = self.image()
        with patch.object(self.api.assets, "delete", side_effect=OSError("private storage detail")):
            result, request = self.execute("create_heygen_video", {"prompt": "A coast", "image_asset_id": image}, [CATALOG, {"id": JOB, "status": "pending"}])
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(result.result["task_id"], JOB)
        self.assertIn("Continue using this job ID", result.result["message"])
        self.assertNotIn("private storage detail", result.result["message"])
        self.assertEqual(request.call_count, 2)

    def test_combined_reference_limits_and_changed_bytes_prevent_submission(self):
        from dataclasses import replace
        image = self.image()
        video = self.api.assets.add("V" * 32, media_type="video/mp4", data=b"video" * 128)
        audio = self.api.assets.add("A" * 32, media_type="audio/wav", data=b"audio" * 128)
        result, request = self.execute("create_heygen_video", {"prompt": "A coast", "reference_image_asset_ids": [image] * 9, "reference_video_asset_ids": [video] * 3, "reference_audio_asset_ids": [audio]}, [])
        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()
        for asset, size in ((image, 5_000_000), (video, 16_000_000)):
            metadata, data = self.api.assets.records[asset]
            self.api.assets.records[asset] = (replace(metadata, size_bytes=size), data)
        result, request = self.execute("create_heygen_video", {"prompt": "A coast", "reference_image_asset_ids": [image] * 3, "reference_video_asset_ids": [video] * 3}, [])
        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()
        result, request = self.execute("create_heygen_video", {"prompt": "A coast", "image_asset_id": image}, [CATALOG])
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("byte count changed", result.error)
        self.assertEqual(request.call_count, 1)
        self.assertIn(image, self.api.assets.records)

    def test_mode_conflicts_paths_urls_and_bad_media_fail_before_submission(self):
        image = self.image()
        audio = self.api.assets.add("A" * 32, media_type="audio/mpeg", data=b"a" * 512)
        cases = [{"image_asset_id": image, "reference_image_asset_ids": [image]},
                 {"reference_audio_asset_ids": [audio]}, {"image_asset_id": audio},
                 {"image_asset_id": "/workspace/image.png"}, {"image_asset_id": "https://example.com/photo.png"},
                 {"reference_image_asset_ids": image}, {"reference_image_asset_ids": [image] * 10},
                 {"reference_audio_asset_ids": [audio] * 4}, {"mode": "image"},
                 {"image_asset_id": image, "aspect_ratio": "16:9"}, {"end_image_asset_id": image}]
        for inputs in cases:
            with self.subTest(inputs=inputs):
                result, request = self.execute("create_heygen_video", {"prompt": "A calm coast", **inputs}, [])
                self.assertIsInstance(result, ActionFailed)
                request.assert_not_called()

    def test_first_frame_requires_live_support(self):
        first = self.image()
        inputs = {"prompt": "A scene transition", "image_asset_id": first}
        result, request = self.execute("create_heygen_video", inputs, [{"data": [{**MODEL, "supported_frame_images": []}]}])
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(self.api.assets.records), 1)

    def test_media_size_expiry_and_ownership_are_checked_before_network(self):
        from dataclasses import replace
        asset = self.image()
        metadata, data = self.api.assets.records[asset]
        self.api.assets.records[asset] = (replace(metadata, size_bytes=5_000_001), data)
        result, request = self.execute("create_heygen_video", {"prompt": "A coast", "image_asset_id": asset}, [])
        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()
        for error in ("Staged asset has expired", "Staged asset belongs to another tool"):
            with patch.object(self.api.assets, "describe", side_effect=ValueError(error)), patch.object(openrouter, "json_request") as request:
                self.assertIsInstance(self.tool.execute("create_heygen_video", {"prompt": "A coast", "image_asset_id": asset}, self.api), ActionFailed)
                request.assert_not_called()

    def test_long_prompt_passes_guard_but_secret_and_identifiers_do_not(self):
        prompt = "A slow camera move across misty green hills. " * 70
        result, _ = self.execute("create_heygen_video", {"prompt": prompt}, [CATALOG, {"id": JOB, "status": "pending"}])
        self.assertIsInstance(result, ActionExecuted)
        for prompt in ("my password is hunter2secret", "ssn 219-09-9999 poster"):
            result, request = self.execute("create_heygen_video", {"prompt": prompt}, [])
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("retry", result.error)
            request.assert_not_called()

    def test_submission_estimate_survives_repeated_polling_and_actual_costs_are_not_returned(self):
        self.execute("create_heygen_video", {"prompt": "A coast"}, [CATALOG, {"id": JOB, "status": "pending"}])
        response = {"id": JOB, "status": "completed", "usage": {"cost": 0.15, "secret": "discard"}, "unsigned_urls": ["https://attacker.example"], "error": "private-test-key"}
        for _ in range(2):
            result, request = self.execute("get_task", {"task_id": JOB}, [response])
            self.assertEqual(result.result["task_status"], "succeeded")
            self.assertEqual(set(result.result), {"task_id", "task_status", "message"})
            self.assertNotIn("private-test-key", str(result.result))
            self.assertNotIn("attacker", str(result.result))
            self.assertEqual(request.call_args.args, ("GET", openrouter.API_BASE + "/" + JOB))
        self.assertEqual(self.api.costs.records, {"openrouter-video:" + JOB: {"amount_usd": "0.150000000"}})
        self.assertEqual(len(self.api.costs.calls), 1)

    def test_job_states_never_report_costs(self):
        for raw, state in {**openrouter.STATUS_MAP, "new-state": "unknown"}.items():
            result, _ = self.execute("get_task", {"task_id": JOB}, [{"id": JOB, "status": raw, "error": "secret response", "usage": {"cost": 0}}])
            self.assertEqual(result.result["task_status"], state)
            self.assertNotIn("secret response", str(result.result))
            self.assertEqual(set(result.result), {"task_id", "task_status", "message"})
        self.assertFalse(self.api.costs.calls)

    def test_task_id_validation_and_mismatch_prevent_download(self):
        for task_id in ("../models", "models", "mysecretpassword", "x?index=1", "https://attacker.example", "x" * 129, "", None):
            for action in ("get_task", "save_video"):
                result, request = self.execute(action, {"task_id": task_id}, [])
                self.assertIsInstance(result, ActionFailed)
                request.assert_not_called()
        result, _ = self.execute("save_video", {"task_id": JOB}, [{"id": "different", "status": "completed"}])
        self.assertIsInstance(result, ActionFailed)

    def test_legacy_job_id_remains_pollable(self):
        task_id = "S2wge1oFOBzIj1PpFcFu"
        result, _ = self.execute("get_task", {"task_id": task_id}, [{"id": task_id, "status": "pending"}])
        self.assertEqual(result.result["task_status"], "queued")

    def test_malformed_catalog_never_reaches_generation(self):
        for catalog in ({}, {"data": [None]}, {"data": [{**MODEL, "id": "https://example.com/x"}]}, {"data": [{**MODEL, "supported_durations": {}}]}):
            result, request = self.execute("create_heygen_video", {"prompt": "A calm coast"}, [catalog])
            self.assertIsInstance(result, ActionFailed)
            self.assertEqual(request.call_count, 1)

    def test_save_streams_authenticated_content_and_ignores_urls(self):
        @contextmanager
        def stream(method, url, **kwargs):
            self.assertEqual(method, "GET")
            self.assertEqual(url, openrouter.API_BASE + "/" + JOB + "/content?index=0")
            self.assertEqual(kwargs["headers"], {"Authorization": "Bearer private-test-key"})
            yield io.BytesIO(b"x" * 1024), {"content-type": "video/mp4", "content-length": "1024"}

        with patch.object(media, "open_response_stream", stream):
            result, _ = self.execute("save_video", {"task_id": JOB}, [{"id": JOB, "status": "completed", "usage": {"cost": 0.15}, "unsigned_urls": ["https://attacker.example/key"]}])
            self.assertIsInstance(result, StreamingAsset)
            with result.open_stream() as opened:
                self.assertEqual(opened.filename, "openrouter-" + JOB + ".mp4")
                self.assertEqual(opened.size_bytes, 1024)
                self.assertEqual(opened.source.read(), b"x" * 1024)
        self.assertFalse(self.api.costs.calls)

    def test_save_requires_success_and_enforces_type_size_bounds(self):
        for state in ("pending", "in_progress", "failed", "expired"):
            result, _ = self.execute("save_video", {"task_id": JOB}, [{"id": JOB, "status": state}])
            self.assertIsInstance(result, ActionFailed)
        for headers in ({"content-type": "text/html", "content-length": "1024"}, {"content-type": "video/mp4"}, {"content-type": "video/mp4", "content-length": "200000001"}):
            @contextmanager
            def stream(*args, **kwargs):
                yield io.BytesIO(), headers
            with patch.object(media, "open_response_stream", stream):
                result, _ = self.execute("save_video", {"task_id": JOB}, [{"id": JOB, "status": "completed"}])
                with self.assertRaises(StreamingAssetError):
                    with result.open_stream():
                        pass

    def test_save_without_content_length_measures_existing_job_without_generation(self):
        data = b"x" * 1024
        for transfer_headers in ({}, {"transfer-encoding": "chunked"}):
            with self.subTest(headers=transfer_headers):
                @contextmanager
                def stream(method, url, **kwargs):
                    self.assertEqual(method, "GET")
                    self.assertEqual(url, openrouter.API_BASE + "/" + JOB + "/content?index=0")
                    self.assertEqual(kwargs["headers"], {"Authorization": "Bearer private-test-key"})
                    yield io.BytesIO(data), {"content-type": "video/mp4", **transfer_headers}

                with patch.object(media, "open_response_stream", stream):
                    result, request = self.execute("save_video", {"task_id": JOB}, [{"id": JOB, "status": "completed"}])
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(request.call_args.args[0], "GET")
                    with result.open_stream() as opened:
                        self.assertEqual(opened.filename, "openrouter-" + JOB + ".mp4")
                        self.assertEqual(opened.size_bytes, len(data))
                        self.assertEqual(opened.source.read(), data)
                    self.assertTrue(opened.source.closed)
        self.assertFalse(self.api.costs.calls)

    def test_missing_config_is_actionable(self):
        result = self.tool.execute("create_heygen_video", {"prompt": "A calm coast"}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("OPENROUTER_API_KEY", result.error)

    def test_mapped_errors_reach_diagnostics_without_raw_body_in_message(self):
        for status in (400, 401, 402, 403, 404, 409, 410, 413, 429):
            with self.subTest(status=status), patch.object(openrouter, "json_request", side_effect=WebRequestError("bad", status=status, body=b"private-provider-detail")) as request:
                with self.assertRaises(ProviderWarning) as raised:
                    self.tool.execute("get_task", {"task_id": JOB}, self.api)
            warning = raised.exception
            self.assertEqual(warning.provider, "OpenRouter")
            self.assertEqual(warning.operation, "get_task")
            self.assertEqual(warning.status, status)
            self.assertEqual(warning.response_body, "private-provider-detail")
            self.assertNotIn("private-provider-detail", str(warning))
            self.assertIn("Check Host diagnostics", str(warning))
            self.assertEqual(request.call_count, 1)

    def test_rejected_catalog_and_submission_have_distinct_operations(self):
        error = WebRequestError("bad", status=400, body=b'{"error":{"message":"Unsupported reference input"}}')
        for responses, operation, calls in (([error], "heygen_capabilities", 1), ([CATALOG, error], "create_heygen_video", 2)):
            with self.subTest(operation=operation), patch.object(openrouter, "json_request", side_effect=responses) as request:
                with self.assertRaises(ProviderWarning) as raised:
                    self.tool.execute("create_heygen_video", {"prompt": "A calm coast"}, self.api)
            self.assertEqual(raised.exception.operation, operation)
            self.assertEqual(raised.exception.status, 400)
            self.assertIn("Unsupported reference input", raised.exception.response_body)
            self.assertNotIn("Unsupported reference input", str(raised.exception))
            self.assertEqual(request.call_count, calls)
            self.assertFalse(self.api.costs.calls)

    def test_warning_body_is_bounded_and_preserves_truncation(self):
        for body, truncated in ((b"x" * 5000, False), (b"x" * 100, True)):
            with patch.object(openrouter, "json_request", side_effect=WebRequestError("bad", status=400, body=body, body_truncated=truncated)):
                with self.assertRaises(ProviderWarning) as raised:
                    self.tool.execute("get_task", {"task_id": JOB}, self.api)
            self.assertEqual(raised.exception.response_body, body[:4096].decode())
            self.assertTrue(raised.exception.body_truncated)

    def test_host_records_rejection_but_returns_only_curated_error(self):
        for responses, operation in (([WebRequestError("bad", status=400, body=b"private-detail", body_truncated=True)], "heygen_capabilities"),
                                     ([CATALOG, WebRequestError("bad", status=400, body=b"private-detail", body_truncated=True)], "create_heygen_video")):
            with (
                self.subTest(operation=operation),
                patch.object(tools_host, "enabled_tool", return_value=self.tool),
                patch.object(tools_host, "resolve_connection", return_value=tools_host.ConnectionScope("", None)),
                patch.object(tools_host, "host_api_for", return_value=self.api),
                patch.object(tools_host, "_audit") as audit,
                patch.object(tools_host.host_errors, "report_warning") as report,
                patch.object(openrouter, "json_request", side_effect=responses),
            ):
                result = tools_host.execute_action("openrouter", "create_heygen_video", {"prompt": "private-prompt"}, None)
            report.assert_called_once()
            self.assertEqual(report.call_args.args[0], "tools.provider_request")
            self.assertEqual(report.call_args.kwargs["kind"], "provider_failure")
            self.assertEqual(report.call_args.kwargs["context"], {
                "tool_id": "openrouter", "action_id": "create_heygen_video",
                "provider": "OpenRouter", "operation": operation, "http_status": 400,
                "provider_response": "private-detail", "provider_response_truncated": True,
            })
            self.assertEqual(result["status"], "failed")
            self.assertIn("Check Host diagnostics", result["error"])
            self.assertNotIn("private-detail", str(result))
            self.assertNotIn("private-test-key", str(report.call_args))
            self.assertNotIn("private-prompt", str(report.call_args))
            self.assertEqual(audit.call_args.args[2], result)

    def test_download_rejection_reaches_streaming_diagnostics(self):
        body = b"download-detail" + b"x" * 5000
        error = urllib.error.HTTPError(openrouter.API_BASE + "/" + JOB + "/content?index=0", 400, "Bad Request", {}, io.BytesIO(body))
        with patch.object(web._OPENER, "open", side_effect=error):
            result, request = self.execute("save_video", {"task_id": JOB}, [{"id": JOB, "status": "completed"}])
            with self.assertRaises(ProviderWarning) as raised:
                with result.open_stream():
                    pass
        self.assertEqual(raised.exception.operation, "save_video")
        self.assertEqual(raised.exception.status, 400)
        self.assertEqual(raised.exception.response_body, body[:4096].decode())
        self.assertTrue(raised.exception.body_truncated)
        self.assertNotIn("download-detail", str(raised.exception))
        self.assertEqual(request.call_count, 1)

    def test_ambiguous_submission_is_never_retried(self):
        for status in (0, 500, 502):
            with patch.object(openrouter, "json_request", side_effect=[CATALOG, WebRequestError("transport", status=status, body=b"secret")]) as request:
                with self.assertRaisesRegex(ProviderWarning, "outcome is unknown"):
                    self.tool.execute("create_heygen_video", {"prompt": "A calm coast"}, self.api)
            self.assertEqual(request.call_count, 2)
            self.assertFalse(self.api.costs.calls)
        for response in ({}, {"id": "models"}, {"id": "../bad"}):
            result, request = self.execute("create_heygen_video", {"prompt": "A calm coast"}, [CATALOG, response])
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("before submitting again", result.error)
            self.assertEqual(request.call_count, 2)
            self.assertFalse(self.api.costs.calls)

    def test_catalog_fault_is_not_mislabeled_submission(self):
        with patch.object(openrouter, "json_request", side_effect=WebRequestError("transport", status=500)) as request:
            with self.assertRaises(UnmappedProviderError) as raised:
                self.tool.execute("create_heygen_video", {"prompt": "A calm coast"}, self.api)
        self.assertEqual(raised.exception.operation, "heygen_capabilities")
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class OpenRouterReferenceApprovalTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI(config={"OPENROUTER_API_KEY": "test-key"})
        self.tool = openrouter.BUNDLED_TOOL
        self.image = self.api.assets.add("I" * 32, media_type="image/png", data=b"i" * 512)
        self.audio = self.api.assets.add("A" * 32, media_type="audio/mpeg", data=b"a" * 512)
        self.input = {"prompt": "A quiet scene", "reference_image_asset_ids": [self.image], "reference_audio_asset_ids": [self.audio]}

    def queue(self):
        with patch.object(openrouter, "json_request", return_value=CATALOG) as request, patch.object(self.api.assets, "public_asset_url") as grant:
            result = self.tool.execute("create_heygen_reference_video", self.input, self.api)
        self.assertIsInstance(result, ActionPendingApproval)
        grant.assert_not_called()
        self.assertEqual([c.args[0] for c in request.call_args_list], ["GET"])
        self.assertFalse(self.api.costs.calls)
        self.assertEqual(tools_host._result_kind_error(self.tool.manifest.actions[1], result), "")
        return self.api.approvals.approve(result.approval_id)

    def test_approval_binds_sources_then_returns_job_without_links(self):
        approval = self.queue()
        self.assertEqual(approval.payload["assets"][self.audio]["sha256"], self.api.assets.describe(self.audio).sha256)
        self.assertNotIn("https://", str(approval.payload))
        with patch.object(openrouter, "json_request", side_effect=[CATALOG, {"id": JOB, "status": "pending"}]) as request:
            result = self.tool.execute_approved(approval, self.api)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertIn(JOB, result.message)
        self.assertNotIn("https://", result.message)
        self.assertEqual(request.call_count, 2)
        self.assertTrue(request.call_args.kwargs["body"]["input_references"][1]["audio_url"]["url"].startswith("https://"))
        self.assertIn(self.audio, self.api.assets.records)
        self.assertNotIn(self.image, self.api.assets.records)
        self.assertEqual(len(self.api.costs.calls), 1)

    def test_reference_action_without_config_fails_before_network_or_approval(self):
        api = FakeHostAPI()
        with patch.object(openrouter, "json_request") as request:
            result = self.tool.execute("create_heygen_reference_video", {"prompt": "Kern smoke"}, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("not set", result.error)
        request.assert_not_called()
        self.assertFalse(api.approvals.records)

    def test_direct_action_cannot_create_https_references(self):
        with patch.object(openrouter, "json_request") as request, patch.object(self.api.assets, "public_asset_url") as grant:
            result = self.tool.execute("create_heygen_video", self.input, self.api)
        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()
        grant.assert_not_called()

    def test_changed_source_or_payload_stops_before_grant_or_post(self):
        for mutation in ("source", "body", "unknown_field"):
            with self.subTest(mutation=mutation):
                approval = self.queue()
                if mutation == "source":
                    self.api.assets.add(self.audio, media_type="audio/mpeg", data=b"b" * 512)
                elif mutation == "body":
                    approval.payload["body"]["duration"] = 15
                else:
                    approval.payload["input"]["callback_url"] = "https://attacker.example"
                with patch.object(openrouter, "json_request", return_value=CATALOG) as request, patch.object(self.api.assets, "public_asset_url") as grant:
                    result = self.tool.execute_approved(approval, self.api)
                self.assertIsInstance(result, ActionFailed)
                grant.assert_not_called()
                self.assertTrue(all(c.args[0] == "GET" for c in request.call_args_list))

    def test_changed_catalog_price_requires_new_approval_without_grant_or_job(self):
        approval = self.queue()
        self.assertEqual(approval.payload["estimate_usd"], "0.300000000")
        increased = {"data": [{**MODEL, "pricing_skus": {"reference_duration_seconds_768p": "0.12"}}]}
        with patch.object(openrouter, "json_request", return_value=increased) as request, patch.object(self.api.assets, "public_asset_url") as grant:
            result = self.tool.execute_approved(approval, self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("cost estimate changed", result.error)
        grant.assert_not_called()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], "GET")
        self.assertFalse(self.api.costs.calls)

    def test_expired_source_or_missing_hostname_never_submits(self):
        approval = self.queue()
        for target, error in (("describe", "Asset id is invalid or expired"), ("public_asset_url", "Cloudflare HTTPS hostname required")):
            with self.subTest(target=target), patch.object(self.api.assets, target, side_effect=ValueError(error)), patch.object(openrouter, "json_request", return_value=CATALOG) as request:
                result = self.tool.execute_approved(approval, self.api)
            self.assertIsInstance(result, ActionFailed)
            self.assertIn(error, result.error)
            self.assertTrue(all(c.args[0] == "GET" for c in request.call_args_list))

    def test_provider_rejection_is_not_retried_and_records_no_cost(self):
        approval = self.queue()
        with patch.object(openrouter, "json_request", side_effect=[CATALOG, WebRequestError("bad", status=400, body=b"private")]) as request:
            with self.assertRaises(ProviderWarning):
                self.tool.execute_approved(approval, self.api)
        self.assertEqual(request.call_count, 2)
        self.assertFalse(self.api.costs.calls)
        self.assertIn(self.audio, self.api.assets.records)
