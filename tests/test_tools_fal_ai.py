"""Single-account falAI discovery and task routing across model families."""
import unittest
from unittest.mock import patch

from host.runtime.tools.tools_host import BUNDLED_TOOLS
from host.tools import fal_ai
from host.tools.fal_ai import h3max, topaz, media
from host.tools.results import ActionExecuted, ActionFailed
from test_tools import FakeHostAPI, assert_matches_output_schema

REQUEST_ID = "764cabcf-b745-4b3e-ae38-1200304cf45b"


class FalAITests(unittest.TestCase):
    def setUp(self):
        self.tool = fal_ai.FalAITool()
        self.api = FakeHostAPI(config={"FAL_API_KEY": "shared-fal-key"})

    def test_one_catalog_entry_one_new_key(self):
        self.assertIn("fal_ai", BUNDLED_TOOLS)
        self.assertNotIn("h3max", BUNDLED_TOOLS)
        self.assertNotIn("topaz", BUNDLED_TOOLS)
        self.assertNotIn("seedance", BUNDLED_TOOLS)
        self.assertEqual(self.tool.manifest.display_name, "falAI")
        self.assertEqual([c.key for c in self.tool.manifest.config], ["FAL_API_KEY"])
        self.assertEqual([a.id for a in self.tool.manifest.actions],
                         ["generate_video", "upscale_image", "upscale_video", "get_task", "save_image", "save_video"])

    def test_generation_schemas_expose_no_source_url_fields(self):
        def inspect(schema):
            if isinstance(schema, dict):
                for key, value in schema.get("properties", {}).items():
                    self.assertNotIn(key, {"uri", "url", "image_url", "video_url", "audio_url", "end_image_url",
                                           "reference_image_urls", "reference_video_urls", "reference_audio_urls"})
                    inspect(value)
                inspect(schema.get("items"))
        for tool_id in ("runway", "fal_ai", "openai_images", "elevenlabs"):
            for action in BUNDLED_TOOLS[tool_id].manifest.actions:
                if action.id.startswith(("generate_", "upscale_", "edit_")):
                    with self.subTest(tool=tool_id, action=action.id):
                        inspect(action.input_schema)

    def test_submission_and_poll_share_key_without_image_task_collision(self):
        for action, inputs, engine, prefix, queue in (
            ("generate_video", {"prompt": "train", "image_asset_id": self.api.assets.add("i" * 43, media_type="image/png")}, h3max, "image", h3max.QUEUE_BASE),
            ("upscale_image", {"image_asset_id": self.api.assets.add("j" * 43, media_type="image/png")}, topaz, "topaz_image", topaz.QUEUE_BASE),
            ("upscale_video", {"video_asset_id": self.api.assets.add("v" * 43)}, topaz, "topaz_video", topaz.QUEUE_BASE),
        ):
            with self.subTest(action=action), patch.object(media, "upload", return_value="https://v3.fal.media/source"), patch.object(engine, "json_request", return_value={"request_id": REQUEST_ID}) as request:
                result = self.tool.execute(action, inputs, self.api)
                self.assertIsInstance(result, ActionExecuted, result)
                assert_matches_output_schema(self, fal_ai.MANIFEST, action, result)
                self.assertEqual(result.result["task_id"], prefix + "_" + REQUEST_ID)
                self.assertEqual({k.lower(): v for k, v in request.call_args.kwargs["headers"].items()}["authorization"], "Key shared-fal-key")
            with patch.object(engine, "json_request", return_value={"status": "IN_PROGRESS"}) as request:
                result = self.tool.execute("get_task", {"task_id": prefix + "_" + REQUEST_ID}, self.api)
                self.assertIsInstance(result, ActionExecuted, result)
                assert_matches_output_schema(self, fal_ai.MANIFEST, "get_task", result)
                self.assertTrue(request.call_args.args[1].startswith(queue + "/"))
                self.assertEqual({k.lower(): v for k, v in request.call_args.kwargs["headers"].items()}["authorization"], "Key shared-fal-key")

    def test_save_dispatch_keeps_models_separate(self):
        for task, action, engine in (("image", "save_video", h3max), ("topaz_video", "save_video", topaz), ("topaz_image", "save_image", topaz)):
            with self.subTest(task=task), patch.object(engine, "json_request", return_value={"status": "IN_PROGRESS"}) as request:
                result = self.tool.execute(action, {"task_id": task + "_" + REQUEST_ID}, self.api)
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(request.call_count, 1)

    def test_old_keys_and_invalid_task_ids_cannot_make_network_requests(self):
        with patch.object(h3max, "json_request") as h3request, patch.object(topaz, "json_request") as topazrequest:
            old = FakeHostAPI(config={"H3MAX_FAL_KEY": "old", "TOPAZ_FAL_KEY": "old"})
            for action, values in (("generate_video", {"prompt": "train"}), ("upscale_image", {"image_asset_id": self.api.assets.add("j" * 43, media_type="image/png")})):
                self.assertIsInstance(self.tool.execute(action, values, old), ActionFailed)
            for task in ("topaz_image_../../escape", "https://example.com/request", "topaz_text_" + REQUEST_ID):
                self.assertIsInstance(self.tool.execute("get_task", {"task_id": task}, self.api), ActionFailed)
            h3request.assert_not_called()
            topazrequest.assert_not_called()
