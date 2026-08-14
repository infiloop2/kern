"""Unit tests for the OpenAI Images tool package (all third-party calls mocked)."""

from __future__ import annotations

import base64
import json
import unittest
from typing import Any
from unittest.mock import patch

from host.runtime.agent_shim import mcp_shim
from host.tools.json_types import JSONObject
from host.tools.results import ActionFailed, StreamingAsset
from host.tools import openai_images
from host.tools.openai_images import OpenAIImagesTool
from host.tools.shared.web import UnmappedProviderError, WebRequestError

from test_tools import FakeHostAPI

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"p" * 512
JPEG_BYTES = b"\xff\xd8\xff" + b"j" * 512


def api_with_key() -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["OPENAI_API_KEY"] = "sk-test-key"
    return api


def image_response(raw: bytes = PNG_BYTES) -> JSONObject:
    return {
        "created": 1,
        "data": [{"b64_json": base64.b64encode(raw).decode("ascii")}],
        "usage": {"total_tokens": 100},
    }


class OpenAIImagesToolTests(unittest.TestCase):
    def test_manifest_exposes_only_image_generation(self) -> None:
        tool = OpenAIImagesTool()
        self.assertEqual(tool.manifest.tool_id, "openai_images")
        self.assertEqual(tool.manifest.connection, "enable_only")
        self.assertIsNone(tool.credentials)
        self.assertEqual([spec.id for spec in tool.manifest.actions], ["generate_image"])
        self.assertEqual([item.key for item in tool.manifest.config], ["OPENAI_API_KEY"])
        cards = tool.manifest.data_summary.cards
        self.assertEqual(
            [card.title for card in cards],
            [
                "What leaves this host",
                "Where it can go",
                "What OpenAI can do with it",
                "How long OpenAI retains it",
            ],
        )
        leaves = cards[0]
        self.assertEqual(
            [point.label for point in leaves.points], ["Generation requests", "Reference images"]
        )
        self.assertIn("not used to train", cards[2].description)
        # The no-training default is a setting an owner can flip for
        # complimentary tokens, so the guide has to name where to check it.
        self.assertIn("Data controls", cards[2].description)
        self.assertTrue(
            any("data-controls/sharing" in link.url for link in cards[2].links)
        )
        self.assertTrue(
            any("data-controls/sharing" in step.link_url for step in tool.manifest.setup_steps)
        )
        self.assertIn("30 days", cards[3].description)
        self.assertIn("/tool_assets", " ".join(tool.manifest.protections))
        # Agent-only notes: what the catalog reader must know beyond the
        # description — how references are staged and what the result is.
        self.assertIn("stage_image", tool.manifest.agent_notes)
        self.assertIn("/tool_assets", tool.manifest.agent_notes)

    def test_generate_image_posts_defaults_and_returns_a_saved_file(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["method"] = method
            seen["url"] = url
            seen["body"] = kwargs["body"]
            seen["headers"] = kwargs["headers"]
            seen["timeout"] = kwargs["timeout"]
            return image_response()

        with patch.object(openai_images, "json_request", fake_json_request):
            result = OpenAIImagesTool().execute(
                "generate_image", {"prompt": "a red fox in snow"}, api_with_key()
            )
        assert isinstance(result, StreamingAsset)
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], openai_images.GENERATIONS_ENDPOINT)
        self.assertEqual(seen["headers"]["authorization"], "Bearer sk-test-key")
        # A render returns only when it is done, so the provider timeout must
        # stay under the shim's per-call budget: whichever fires first decides
        # what the agent sees, and this package's message is the useful one.
        self.assertLess(seen["timeout"], mcp_shim.REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(
            seen["body"],
            {
                "model": "gpt-image-2",
                "prompt": "a red fox in snow",
                "size": "1024x1024",
                "quality": "low",
                "output_format": "png",
                "n": 1,
            },
        )
        with result.open_stream() as opened:
            self.assertEqual(opened.media_type, "image/png")
            self.assertTrue(opened.filename.startswith("openai-image-"))
            self.assertTrue(opened.filename.endswith(".png"))
            self.assertEqual(opened.size_bytes, len(PNG_BYTES))
            self.assertEqual(opened.source.read(), PNG_BYTES)

    def test_generate_image_passes_through_selected_options(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen["body"] = kwargs["body"]
            return image_response(JPEG_BYTES)

        with patch.object(openai_images, "json_request", fake_json_request):
            result = OpenAIImagesTool().execute(
                "generate_image",
                {
                    "prompt": "a poster",
                    "model": "gpt-image-1-mini",
                    "size": "1536x1024",
                    "quality": "medium",
                    "output_format": "jpeg",
                },
                api_with_key(),
            )
        assert isinstance(result, StreamingAsset)
        self.assertEqual(seen["body"]["model"], "gpt-image-1-mini")
        self.assertEqual(seen["body"]["size"], "1536x1024")
        self.assertEqual(seen["body"]["quality"], "medium")
        with result.open_stream() as opened:
            self.assertEqual(opened.media_type, "image/jpeg")
            self.assertTrue(opened.filename.endswith(".jpg"))

    def test_reference_images_stream_to_the_edits_endpoint_and_are_consumed(self) -> None:
        api = api_with_key()
        first = api.assets.add(
            "asset_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            filename="logo.png",
            media_type="image/png",
            data=b"first image bytes",
        )
        second = api.assets.add(
            "asset_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            filename="scene.webp",
            media_type="image/webp",
            data=b"second image bytes",
        )
        streamed: dict[str, Any] = {}

        def fake_stream(method: str, url: str, **kwargs: Any) -> bytes:
            streamed["method"] = method
            streamed["url"] = url
            streamed["headers"] = kwargs["headers"]
            streamed["body"] = b"".join(kwargs["body"])
            streamed["content_length"] = kwargs["content_length"]
            return json.dumps(image_response()).encode("utf-8")

        with (
            patch.object(openai_images, "stream_request_bytes", fake_stream),
            patch.object(
                openai_images,
                "json_request",
                lambda *args, **kwargs: self.fail("references must not use the JSON endpoint"),
            ),
        ):
            result = OpenAIImagesTool().execute(
                "generate_image",
                {"prompt": "put the logo on the scene", "image_asset_ids": [first, second]},
                api,
            )
        assert isinstance(result, StreamingAsset)
        self.assertEqual(streamed["url"], openai_images.EDITS_ENDPOINT)
        self.assertTrue(streamed["headers"]["Content-Type"].startswith("multipart/form-data; boundary=kern-"))
        body = streamed["body"]
        # Exact declared length, both files in order, and generated part names
        # rather than the agent's workspace filenames.
        self.assertEqual(streamed["content_length"], len(body))
        self.assertIn(b"first image bytes", body)
        self.assertIn(b"second image bytes", body)
        self.assertIn(b'name="image[]"; filename="reference-1.png"', body)
        self.assertIn(b'name="image[]"; filename="reference-2.webp"', body)
        self.assertNotIn(b"logo.png", body)
        self.assertNotIn(b"scene.webp", body)
        self.assertIn(b'name="prompt"', body)
        self.assertIn(b"put the logo on the scene", body)
        # The staged copies are one-shot: consumed once OpenAI returned an image.
        self.assertEqual(api.assets.records, {})

    def test_reference_images_survive_an_unusable_response_for_retry(self) -> None:
        api = api_with_key()
        asset_id = api.assets.add(
            "asset_cccccccccccccccccccccccccccccccccc",
            filename="frame.png",
            media_type="image/png",
            data=b"frame bytes",
        )
        with patch.object(
            openai_images,
            "stream_request_bytes",
            lambda *args, **kwargs: json.dumps({"created": 1, "data": []}).encode("utf-8"),
        ):
            result = OpenAIImagesTool().execute(
                "generate_image", {"prompt": "edit it", "image_asset_ids": [asset_id]}, api
            )
        assert isinstance(result, ActionFailed)
        self.assertIn("no image data", result.error)
        self.assertIn(asset_id, api.assets.records)

    def test_rejects_non_image_and_oversized_references(self) -> None:
        api = api_with_key()
        video_asset = api.assets.add("asset_dddddddddddddddddddddddddddddddddd")
        wrong_type = OpenAIImagesTool().execute(
            "generate_image", {"prompt": "edit", "image_asset_ids": [video_asset]}, api
        )
        assert isinstance(wrong_type, ActionFailed)
        self.assertIn("staged JPEG, PNG, or WebP images", wrong_type.error)

        unknown = OpenAIImagesTool().execute(
            "generate_image", {"prompt": "edit", "image_asset_ids": ["not-a-real-asset"]}, api
        )
        self.assertIsInstance(unknown, ActionFailed)

    def test_validates_input(self) -> None:
        tool = OpenAIImagesTool()
        bad_inputs = [
            {},
            {"prompt": "   "},
            {"prompt": "x" * (openai_images.MAX_PROMPT_CHARS + 1)},
            {"prompt": "x", "model": "dall-e-3"},
            {"prompt": "x", "size": "2048x2048"},
            {"prompt": "x", "quality": "ultra"},
            {"prompt": "x", "output_format": "gif"},
            {"prompt": "x", "image_asset_ids": "asset"},
            {"prompt": "x", "image_asset_ids": ["a", "a"]},
            {"prompt": "x", "image_asset_ids": ["a", "b", "c", "d", "e"]},
            {"prompt": "x", "n": "2"},
            {"prompt": "x", "unknown": True},
        ]
        for bad_input in bad_inputs:
            with self.subTest(bad_input=bad_input):
                self.assertIsInstance(
                    tool.execute("generate_image", bad_input, api_with_key()), ActionFailed
                )

    def test_rejects_provider_data_that_is_not_the_requested_image(self) -> None:
        cases = (
            ({"data": [{"b64_json": base64.b64encode(JPEG_BYTES).decode("ascii")}]}, "not a png image"),
            ({"data": [{"b64_json": "!!!not-base64!!!"}]}, "undecodable"),
            ({"data": [{"b64_json": base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")}]}, "size range"),
            ({"data": [{"url": "https://cdn.example/image.png"}]}, "no image data"),
        )
        for response, fragment in cases:
            with self.subTest(fragment=fragment), patch.object(
                openai_images, "json_request", return_value=response
            ):
                result = OpenAIImagesTool().execute(
                    "generate_image", {"prompt": "x"}, api_with_key()
                )
            assert isinstance(result, ActionFailed)
            self.assertIn(fragment, result.error)

    def test_missing_key_and_provider_failures(self) -> None:
        tool = OpenAIImagesTool()
        result = tool.execute("generate_image", {"prompt": "x"}, FakeHostAPI())
        assert isinstance(result, ActionFailed)
        self.assertIn("OPENAI_API_KEY is not set", result.error)

        cases = (
            (401, b"", "rejected the configured API key"),
            (403, b'{"error":{"code":"organization_must_be_verified"}}', "verified API organization"),
            (429, b"", "rate limit or billing quota"),
            (400, b'{"error":{"code":"moderation_blocked"}}', "content filters"),
            (500, b"", "server error"),
        )
        for status, body, fragment in cases:
            def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
                raise WebRequestError("failed", status=status, body=body)

            with self.subTest(status=status), patch.object(
                openai_images, "json_request", fake_json_request
            ):
                result = tool.execute("generate_image", {"prompt": "x"}, api_with_key())
            assert isinstance(result, ActionFailed)
            self.assertIn(fragment, result.error)

    def test_unmapped_transport_failure_becomes_a_host_warning(self) -> None:
        # A render that outran the timeout and a refused connection are the same
        # statusless failure here, so neither invents a cause for the agent: the
        # host records the warning and returns its generic message.
        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=0)

        with (
            patch.object(openai_images, "json_request", fake_json_request),
            self.assertRaises(UnmappedProviderError) as raised,
        ):
            OpenAIImagesTool().execute("generate_image", {"prompt": "x"}, api_with_key())
        self.assertEqual(raised.exception.provider, "OpenAI")
        self.assertEqual(raised.exception.operation, "images")

    def test_provider_error_text_never_reaches_the_agent(self) -> None:
        # Only the machine-shaped code escapes; OpenAI's message can echo the
        # prompt (and anything the prompt was made to contain).
        body = json.dumps(
            {"error": {"code": "moderation_blocked", "message": "Your prompt 'secret leak' was rejected"}}
        ).encode("utf-8")

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            raise WebRequestError("failed", status=400, body=body)

        with patch.object(openai_images, "json_request", fake_json_request):
            result = OpenAIImagesTool().execute(
                "generate_image", {"prompt": "x"}, api_with_key()
            )
        assert isinstance(result, ActionFailed)
        self.assertIn("moderation_blocked", result.error)
        self.assertNotIn("secret leak", result.error)
        self.assertEqual(openai_images._provider_error_code(b'{"error":{"code":"a b c"}}'), "")
        self.assertEqual(openai_images._provider_error_code(b"not json"), "")

    def test_unsupported_action_and_no_approvals(self) -> None:
        tool = OpenAIImagesTool()
        self.assertIsInstance(tool.execute("edit_image", {}, api_with_key()), ActionFailed)
        self.assertIsInstance(tool.execute_approved("approval-1", api_with_key()), ActionFailed)


if __name__ == "__main__":
    unittest.main()
