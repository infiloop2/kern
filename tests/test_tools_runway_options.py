"""Provider payload coverage for shared Runway video inputs; no paid calls."""
import unittest
from unittest.mock import patch

from host.tools import runway
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.runway import options
from test_tools import assert_matches_output_schema
from test_tools_runway import api_with_key


class RunwayOptionsTests(unittest.TestCase):
    def setUp(self):
        self.api = api_with_key()
        self.tool = runway.RunwayTool()


    def media(self, kind, name="source"):
        return self.api.assets.add(name, filename=name, media_type={"image": "image/png", "video": "video/mp4", "audio": "audio/mpeg"}[kind])

    def request(self, values, action="generate_video"):
        with patch.object(runway, "_upload_staged_asset", side_effect=lambda asset, *a, **k: "runway://" + asset), patch.object(runway, "json_request", return_value={"id": "task-options"}) as request:
            result = self.tool.execute(action, values, self.api)
        self.assertIsInstance(result, ActionExecuted, result)
        assert_matches_output_schema(self, runway.MANIFEST, action, result)
        return request.call_args.args[1], request.call_args.kwargs["body"]

    def test_h3_resolutions_expansion_and_first_last_frames(self):
        for resolution in ("480p", "768p"):
            for expansion in ("disabled", "balanced", "quality"):
                url, body = self.request({"model": "h3_max", "prompt": "A robot waves",
                    "resolution": resolution, "prompt_expansion_mode": expansion, "seed": "9",
                    "prompt_images": [{'asset_id': self.media('image', 'start.png'), "position": "first"},
                                      {'asset_id': self.media('image', 'end.png'), "position": "last"}]})
                self.assertEqual(url, runway.IMAGE_TO_VIDEO_ENDPOINT)
                self.assertEqual(body, {"model": "h3_max", "promptText": "A robot waves", "duration": 5,
                    "resolution": resolution, "promptExpansionMode": expansion, "seed": 9,
                    "promptImage": [{"uri": "runway://start.png", "position": "first"},
                                    {"uri": "runway://end.png", "position": "last"}]})

    def test_model_resolution_choices_on_both_routes(self):
        for model, ratios in options.MODEL_RATIOS.items():
            for ratio in ratios:
                for image in (None, "runway://start.png"):
                    values = {"model": model, "prompt": "A robot waves", "ratio": ratio}
                    if image:
                        values["image_asset_id"] = self.media("image", "start.png")
                    with self.subTest(model=model, ratio=ratio, image=image):
                        url, body = self.request(values)
                        self.assertEqual(body["ratio"], ratio)
                        self.assertNotIn("resolution", body)
                        self.assertEqual(url, runway.IMAGE_TO_VIDEO_ENDPOINT if image else runway.TEXT_TO_VIDEO_ENDPOINT)
        for model in ("gen4.5", "gen4_turbo"):
            for ratio in options.GEN4_IMAGE_RATIOS:
                self.assertEqual(self.request({"model": model, "prompt": "wave", "ratio": ratio,
                    "image_asset_id": self.media("image", "start.png")})[1]["ratio"], ratio)

    def test_seedance_reference_and_video_routes(self):
        for model in options.SEEDANCE_MODELS:
            for video in (None, "https://example.com/source.mp4"):
                values = {"model": model, "prompt": "A robot waves", "duration_seconds": "auto", "audio": False,
                    "reference_images": [{'asset_id': self.media('image', 'look.png')}],
                    "reference_videos": [{'asset_id': self.media('video', 'movement.mp4')}],
                    "reference_audio": [{'asset_id': self.media('audio', 'voice.mp3')}]}
                if video:
                    values["video_asset_id"] = self.media("video", "source.mp4")
                url, body = self.request(values)
                self.assertEqual(url, runway.VIDEO_TO_VIDEO_ENDPOINT if video else runway.TEXT_TO_VIDEO_ENDPOINT)
                self.assertEqual(body["duration"], "auto")
                self.assertIs(body["audio"], False)
                self.assertEqual(body["references"], [{"uri": "runway://look.png"}])
                self.assertEqual(body["referenceVideos"], [{"uri": "runway://movement.mp4", "type": "video"}])
                self.assertEqual(body["referenceAudio"], [{"uri": "runway://voice.mp3", "type": "audio"}])
                if video:
                    self.assertEqual(body["promptVideo"], "runway://source.mp4")
        for mode in ("reference", "extend", "edit"):
            _, body = self.request({"model": "seedance2_5", 'video_asset_id': self.media('video', 'source.mp4'),
                "mode": mode, "prompt": "Make it snow", "duration_seconds": "auto"})
            self.assertEqual(body["mode"], mode)
            self.assertEqual("ratio" in body, mode == "reference")

    def test_veo_audio_negative_prompt_and_seedance_unpositioned_images(self):
        _, body = self.request({"model": "veo3.1_fast", "prompt": "A robot waves", "audio": True,
            "negative_prompt": "No camera shake", "ratio": "1080:1920",
            "prompt_images": [{'asset_id': self.media('image', 'start.png'), "position": "first"},
                              {'asset_id': self.media('image', 'end.png'), "position": "last"}]})
        self.assertEqual(body["negativePrompt"], "No camera shake")
        self.assertIs(body["audio"], True)
        self.assertEqual(body["ratio"], "1080:1920")
        url, body = self.request({"model": "seedance2", "prompt_images": [{'asset_id': self.media('image', 'reference')}],
            "reference_audio": [{'asset_id': self.media('audio', 'voice.mp3')}]})
        self.assertEqual(url, runway.IMAGE_TO_VIDEO_ENDPOINT)
        self.assertEqual(body["promptImage"], [{"uri": "runway://reference"}])
        self.assertNotIn("promptText", body)

    def test_formats_and_aleph_keyframes(self):
        for output in options.OUTPUT_FORMATS:
            _, body = self.request({"prompt": "A robot waves", "output_format": output, "public_figure_threshold": "auto"})
            self.assertEqual(body["outputFormat"], output)
            self.assertEqual(body["contentModeration"], {"publicFigureThreshold": "auto"})
        _, body = self.request({'video_asset_id': self.media('video', 'source.mp4'), "output_format": "prores",
            "prores_profile": "422 HQ", "target_aspect_ratio": "9:16", "ratio": "720:1280",
            "keyframes": [{'asset_id': self.media('image', 'style.png'), "seconds": 2.5,
                           "range": {"start_seconds": 1, "end_seconds": 4}}]}, "edit_video")
        self.assertEqual(body["keyframes"], [{"uri": "runway://style.png", "seconds": 2.5,
                                            "range": {"start_seconds": 1, "end_seconds": 4}}])
        self.assertEqual(body["proresProfile"], "422 HQ")
        self.assertEqual(body["targetAspectRatio"], "9:16")
        self.assertNotIn("promptText", body)

    def test_workspace_media_uploads_once_and_consumes_only_on_success(self):
        for success in (True, False):
            image = self.api.assets.add("image", filename="frame.png", media_type="image/png")
            video = self.api.assets.add("video", filename="source.mp4", media_type="video/mp4")
            values = {"model": "seedance2_5", "video_asset_id": video,
                "reference_images": [{"asset_id": image}, {"asset_id": image}],
                "reference_audio": [{'asset_id': self.media('audio', 'voice.mp3')}]}
            with patch.object(runway, "_upload_staged_asset", side_effect=lambda asset, *a, **k: "runway://" + asset) as upload, \
                 patch.object(runway, "json_request", return_value={"id": "task-created"} if success else {} ) as request:
                result = self.tool.execute("generate_video", values, self.api)
            self.assertIsInstance(result, ActionExecuted if success else ActionFailed)
            self.assertEqual(upload.call_count, 3)
            body = request.call_args.kwargs["body"]
            self.assertEqual(body["references"], [{"uri": "runway://" + image}] * 2)
            self.assertEqual(body["referenceAudio"], [{"uri": "runway://voice.mp3", "type": "audio"}])
            self.assertEqual(body["promptVideo"], "runway://" + video)
            for asset in (image, video, "voice.mp3"):
                self.assertEqual(asset in self.api.assets.records, not success)

    def test_all_source_url_shapes_fail_before_upload(self):
        for source in ("https://example.com/source", "runway://prior-upload", "data:image/png;base64,AA"):
            cases = [("generate_video", {"prompt": "wave", "image_url": source}),
                     ("generate_video", {"model": "seedance2_5", "video_url": source}),
                     ("edit_video", {"video_url": source}),
                     ("upscale_image", {"image_url": source}),
                     ("upscale_video", {"video_url": source})]
            for field in ("prompt_images", "reference_images", "reference_videos", "reference_audio"):
                cases.append(("generate_video", {"model": "seedance2_5", field: [{"uri": source}]}))
            cases.append(("edit_video", {"video_asset_id": self.media("video"), "keyframes": [{"uri": source, "at": 0}]}))
            with patch.object(runway, "_upload_staged_asset") as upload, patch.object(runway, "json_request") as request:
                for action, values in cases:
                    with self.subTest(source=source, action=action, values=values):
                        self.assertIsInstance(self.tool.execute(action, values, self.api), ActionFailed)
                upload.assert_not_called()
                request.assert_not_called()
            self.assertFalse(self.api.costs.calls)

    def test_invalid_combinations_fail_before_upload_or_generation(self):
        first = {"asset_id": self.media("image", "start.png"), "position": "first"}
        last = {"asset_id": self.media("image", "end.png"), "position": "last"}
        cases = [
            {"model": "h3_max", "resolution": "720p"},
            {"model": "h3_max", "prompt_images": [last]},
            {"model": "h3_max", "prompt_images": [first, first]},
            {"model": "gen4.5", "prompt_images": [last]},
            {"model": "seedance2_5", "prompt_images": [first, {"asset_id": self.media("image", "ref.png")}]},
            {"model": "seedance2_5", "image_asset_id": first["asset_id"], "prompt_images": [first]},
            {"model": "seedance2_fast", "ratio": "1080:1920"},
            {"model": "seedance2_5", "ratio": "2160:3840"},
            {"model": "seedance2", "ratio": "480:854"},
            {"model": "gen4.5", "ratio": "960:960"},
            {"model": "h3_max", "audio": True},
            {"model": "seedance2", "audio": "false"},
            {"model": "seedance2", "negative_prompt": "shake"},
            {"model": "veo3.1", "output_format": "prores"},
            {"model": "gen4.5", "prores_profile": "422"},
            {"model": "gen4.5", "output_format": "hdr_prores", "prores_profile": "4444 XQ"},
            {"model": "seedance2_5", "video_asset_id": self.media("video", "source.mp4"), "mode": "edit"},
            {"model": "seedance2_5", "video_asset_id": self.media("video", "source.mp4"), "mode": "extend", "ratio": "1280:720"},
            {"model": "seedance2_5", "mode": "reference"},
            {"model": "seedance2", "reference_images": []},
            {"model": "seedance2", "reference_images": [{"asset_id": first["asset_id"]}] * 10},
            {"model": "veo3.1", "reference_images": [{"asset_id": first["asset_id"]}]},
            {"model": "seedance2_5", "image_asset_id": first["asset_id"], "reference_videos": [{"asset_id": self.media("video", "source.mp4")}]},
            {"model": "seedance2_5", "reference_audio": [{"uri": "http://example.com/a.mp3"}]},
        ]
        # An existing workspace reference must survive every rejected request.
        asset = self.api.assets.add(filename="voice.mp3", media_type="audio/mpeg")
        with patch.object(runway, "_upload_staged_asset") as upload, patch.object(runway, "json_request") as request:
            for values in cases:
                with self.subTest(values=values):
                    result = self.tool.execute("generate_video", {"prompt": "A robot waves", **values}, self.api)
                    self.assertIsInstance(result, ActionFailed)
            result = self.tool.execute("generate_video", {"model": "seedance2_5", "reference_audio": [{"asset_id": asset}],
                "reference_images": [{"asset_id": asset}]}, self.api)
            self.assertIsInstance(result, ActionFailed)
            upload.assert_not_called()
            request.assert_not_called()
        self.assertIn(asset, self.api.assets.records)

    def test_nested_guard_and_invalid_keyframes_precede_network(self):
        secret = "https://example.com/contact?email=person@example.com"
        cases = [
            ("generate_video", {"model": "h3_max", "prompt": "person@example.com"}),
            ("generate_video", {"model": "veo3.1", "prompt": "wave", "negative_prompt": "person@example.com"}),
            ("generate_video", {"model": "seedance2_5", "reference_images": [{"uri": secret}]}),
            ("generate_video", {"model": "seedance2_5", "reference_videos": [{"uri": secret}]}),
            ("generate_video", {"model": "seedance2_5", "reference_audio": [{"uri": secret}]}),
            ("generate_video", {"model": "seedance2_5", "video_url": secret}),
            ("generate_video", {"model": "h3_max", "prompt": "wave", "prompt_images": [{"uri": secret, "position": "first"}]}),
            ("edit_video", {"video_asset_id": self.media("video", "source.mp4"), "keyframes": [{"uri": secret, "at": 0.5}]}),
        ]
        for frame in ({"at": 2}, {"seconds": float("nan")}, {"seconds": 1, "at": 0.5},
                      {"seconds": 1, "range": {"start_seconds": 2, "end_seconds": 4}},
                      {"at": 0.5, "range": {"start_seconds": False, "end_seconds": 4}}):
            cases.append(("edit_video", {"video_asset_id": self.media("video", "source.mp4"), "keyframes": [{"asset_id": self.media("image", "frame.png"), **frame}]}))
        with patch.object(runway, "json_request") as request:
            for action, values in cases:
                with self.subTest(action=action, values=values):
                    self.assertIsInstance(self.tool.execute(action, values, self.api), ActionFailed)
            request.assert_not_called()

    def test_task_returns_every_artifact(self):
        with patch.object(runway, "json_request", return_value={"id": "task-sequence", "status": "SUCCEEDED",
            "output": ["https://example.com/frames.zip", "https://example.com/sound.wav", "https://example.com/color.json"]}):
            result = self.tool.execute("get_task", {"task_id": "task-sequence"}, self.api)
        assert_matches_output_schema(self, runway.MANIFEST, "get_task", result)
        self.assertEqual(len(result.result["output_urls"]), 3)

    def test_model_prompt_limits_and_optional_prompt(self):
        # Runway prompts opt into 5 KB; lower provider character limits still apply.
        for model, count in (("gen4.5", 1000), ("seedance2", 3500), ("seedance2_5", 5120)):
            prompt = ("a " * count)[:count - 1] + "a"
            self.assertEqual(self.request({"model": model, "prompt": prompt})[1]["promptText"], prompt)
            with patch.object(runway, "json_request") as request:
                self.assertIsInstance(self.tool.execute("generate_video", {"model": model, "prompt": prompt + "aa"}, self.api), ActionFailed)
                request.assert_not_called()
        self.assertNotIn("promptText", self.request({"model": "seedance2_5"})[1])

    def test_long_prompt_checks_tail_and_does_not_expand_url_or_negative_prompt_limits(self):
        values = [
            {"model": "seedance2_5", "prompt": "a " * 600 + "person@example.com"},
            {"model": "seedance2_5", "reference_images": [{"uri": "https://example.com/" + "a" * 1100}]},
            {"model": "veo3.1", "prompt": "hello", "negative_prompt": "a " * 600},
        ]
        with patch.object(runway, "json_request") as request:
            for value in values:
                self.assertIsInstance(self.tool.execute("generate_video", value, self.api), ActionFailed)
            request.assert_not_called()
