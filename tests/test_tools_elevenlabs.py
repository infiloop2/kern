"""ElevenLabs provider contracts; external requests are mocked, not paid."""
from contextlib import contextmanager
import io
import json
import unittest
from unittest.mock import patch

from host.tools import elevenlabs as el
from host.tools.results import ActionExecuted, ActionFailed, StreamingAsset
from host.tools.shared.web import WebRequestError
from test_tools import FakeHostAPI, assert_matches_output_schema

MP3 = b"ID3" + b"a" * 512


class ElevenLabsTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI()
        self.api.config["ELEVENLABS_API_KEY"] = "test-elevenlabs-secret"
        self.tool = el.ElevenLabsTool()
        self.calls = []

    @contextmanager
    def response(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        yield io.BytesIO(MP3), {"content-type": "audio/mpeg"}

    def test_speech_generates_once_preserves_tags_and_saves_bytes(self):
        with patch.object(el, "open_response_stream", self.response):
            result = self.tool.execute("generate_speech", {"text": "[whispers] Please... don't erase me.", "voice_id": "voice_123", "stability": 0, "style": 0.4, "speed": 0.9}, self.api)
        self.assertIsInstance(result, StreamingAsset)
        with result.open_stream() as asset:
            self.assertEqual(asset.source.read(), MP3)
            self.assertEqual(asset.size_bytes, len(MP3))
            self.assertEqual(asset.media_type, "audio/mpeg")
            self.assertRegex(asset.filename, r"^elevenlabs-[a-f0-9]{16}\.mp3$")
            self.assertNotIn("test-elevenlabs-secret", asset.summary)
        self.assertEqual(len(self.calls), 1)
        method, url, args = self.calls[0]
        self.assertEqual((method, url), ("POST", el.API_ROOT + "/v1/text-to-speech/voice_123?output_format=mp3_44100_128"))
        self.assertEqual(json.loads(args["data"]), {"text": "[whispers] Please... don't erase me.", "model_id": "eleven_v3", "voice_settings": {"stability": 0, "style": 0.4, "speed": 0.9}})

    def test_music_prompt_and_sfx_payloads(self):
        with patch.object(el, "open_response_stream", self.response):
            self.assertIsInstance(self.tool.execute("generate_music", {"prompt": "Tender piano motif", "duration_ms": 59000}, self.api), StreamingAsset)
            self.assertIsInstance(self.tool.execute("generate_sound_effect", {"text": "Soft room tone", "duration_seconds": 4.5, "loop": True, "prompt_influence": 0.7}, self.api), StreamingAsset)
        self.assertEqual(json.loads(self.calls[0][2]["data"]), {"model_id": "music_v2_5", "store_for_inpainting": False, "prompt": "Tender piano motif", "music_length_ms": 59000, "force_instrumental": True})
        self.assertEqual(json.loads(self.calls[1][2]["data"]), {"model_id": "eleven_text_to_sound_v2", "text": "Soft room tone", "duration_seconds": 4.5, "loop": True, "prompt_influence": 0.7})

    def test_supported_speech_speed_boundaries_reach_provider(self):
        with patch.object(el, "open_response_stream", self.response):
            for model in ("eleven_v3", "eleven_multilingual_v2"):
                for speed in (0.7, 1.2):
                    with self.subTest(model=model, speed=speed):
                        result = self.tool.execute("generate_speech", {"text": "hello", "voice_id": "voice", "model": model, "speed": speed}, self.api)
                        self.assertIsInstance(result, StreamingAsset)
                        self.assertEqual(json.loads(self.calls[-1][2]["data"])["voice_settings"]["speed"], speed)

    def test_speech_stability_respects_model_modes(self):
        with patch.object(el, "open_response_stream", self.response):
            for model, stability in (("eleven_v3", 0), ("eleven_v3", 0.5), ("eleven_v3", 1), ("eleven_multilingual_v2", 0.3)):
                with self.subTest(model=model, stability=stability):
                    result = self.tool.execute("generate_speech", {"text": "hello", "voice_id": "voice", "model": model, "stability": stability}, self.api)
                    self.assertIsInstance(result, StreamingAsset)
                    self.assertEqual(json.loads(self.calls[-1][2]["data"])["voice_settings"]["stability"], stability)

    def test_design_preview_and_save_voice_flow(self):
        description = "A warm storyteller with a soft, expressive voice."
        preview = {"previews": [{"generated_voice_id": "preview_1", "audio_base_64": "ignored", "preview_url": "https://untrusted.test"}], "text": "An audition."}
        with patch.object(el, "json_request", return_value=preview) as request:
            result = self.tool.execute("design_voice", {"voice_description": description}, self.api)
        self.assertEqual(result.result, {"generated_voice_ids": ["preview_1"], "text": "An audition."})
        assert_matches_output_schema(self, el.MANIFEST, "design_voice", result)
        self.assertEqual(request.call_args.args, ("POST", el.API_ROOT + "/v1/text-to-voice/design?output_format=mp3_44100_128"))
        self.assertEqual(request.call_args.kwargs["body"], {"voice_description": description, "model_id": "eleven_ttv_v3", "auto_generate_text": True, "stream_previews": True})
        with patch.object(el, "json_request", return_value=preview) as request:
            self.tool.execute("design_voice", {"voice_description": description, "text": "An expressive audition. " * 8, "should_enhance": True}, self.api)
        self.assertFalse(request.call_args.kwargs["body"]["auto_generate_text"])
        self.assertTrue(request.call_args.kwargs["body"]["should_enhance"])
        with patch.object(el, "open_response_stream", self.response):
            result = self.tool.execute("preview_voice", {"generated_voice_id": "preview_1"}, self.api)
        self.assertIsInstance(result, StreamingAsset)
        self.assertEqual(self.calls[-1][:2], ("GET", el.API_ROOT + "/v1/text-to-voice/preview_1/stream"))
        self.assertIsNone(self.calls[-1][2]["data"])
        with result.open_stream() as asset:
            self.assertEqual(asset.source.read(), MP3)
        with patch.object(el, "json_request", return_value={"voice_id": "saved_voice", "preview_url": "https://untrusted.test"}) as request:
            result = self.tool.execute("save_voice", {"generated_voice_id": "preview_1", "name": "Storyteller", "voice_description": description}, self.api)
        self.assertEqual(result.result, {"voice_id": "saved_voice"})
        assert_matches_output_schema(self, el.MANIFEST, "save_voice", result)
        self.assertEqual(request.call_args.args, ("POST", el.API_ROOT + "/v1/text-to-voice"))
        self.assertEqual(request.call_args.kwargs["body"], {"generated_voice_id": "preview_1", "voice_name": "Storyteller", "voice_description": description})

    def test_music_sections_preserve_order_timing_and_directions(self):
        sections = [
            {"text": "", "duration_ms": 9000, "positive_styles": ["instrumental", "hesitant piano"]},
            {"text": "", "duration_ms": 5000, "positive_styles": ["instrumental", "suspenseful strings"], "negative_styles": ["drums"]},
        ]
        with patch.object(el, "open_response_stream", self.response):
            result = self.tool.execute("generate_music", {"sections": sections}, self.api)
        self.assertIsInstance(result, StreamingAsset)
        body = json.loads(self.calls[0][2]["data"])
        self.assertEqual(body, {"model_id": "music_v2_5", "store_for_inpainting": False, "composition_plan": {"chunks": sections}})

    def test_invalid_requests_make_no_paid_call(self):
        cases = [
            ("generate_speech", {"text": "hello", "voice_id": "https://evil.test"}),
            ("generate_speech", {"text": "hello", "voice_id": "voice", "stability": 1.2}),
            ("generate_speech", {"text": "hello", "voice_id": "voice", "stability": 0.3}),
            ("generate_speech", {"text": "hello", "voice_id": "voice", "speed": True}),
            ("generate_speech", {"text": "hello", "voice_id": "voice", "speed": 0.25}),
            ("generate_speech", {"text": "hello", "voice_id": "voice", "speed": 0.699}),
            ("generate_speech", {"text": "hello", "voice_id": "voice", "speed": 1.201}),
            ("generate_speech", {"text": "hello", "voice_id": "voice", "speed": 4}),
            ("generate_speech", {"text": "hello", "voice_id": "voice", "style": float("nan")}),
            ("generate_music", {"prompt": "piano", "duration_ms": 300001}),
            ("generate_music", {"prompt": "piano", "duration_ms": 5000, "model": "music_v2"}),
            ("generate_music", {"prompt": "piano", "sections": [], "duration_ms": 4000}),
            ("generate_music", {"prompt": "piano", "duration_ms": 4000.0}),
            ("generate_music", {"sections": [{"text": "", "duration_ms": 5000, "positive_styles": ["piano"], "hidden": "secret"}]}),
            ("generate_music", {"sections": [{"reference": {"song_id": "song", "start_ms": 5, "end_ms": 4}}]}),
            ("generate_music", {"sections": [{"text": "", "duration_ms": 120000, "positive_styles": []}] * 3}),
            ("generate_sound_effect", {"text": "wind", "duration_seconds": 31}),
            ("generate_sound_effect", {"text": "wind", "duration_seconds": 10**400}),
            ("generate_sound_effect", {"text": "wind", "duration_seconds": 3, "loop": "yes"}),
            ("design_voice", {"voice_description": "short"}),
            ("design_voice", {"voice_description": "A warm and expressive storyteller.", "text": "short"}),
            ("design_voice", {"voice_description": "A warm and expressive storyteller.", "model": "unsupported"}),
            ("design_voice", {"voice_description": "A warm and expressive storyteller.", "should_enhance": "yes"}),
            ("preview_voice", {"generated_voice_id": "../../outside"}),
            ("save_voice", {"generated_voice_id": "preview", "name": "", "voice_description": "A warm and expressive storyteller."}),
            ("save_voice", {"generated_voice_id": "preview", "name": "Narrator", "voice_description": "short"}),
        ]
        with patch.object(el, "open_response_stream") as request, patch.object(el, "json_request") as query:
            for action, values in cases:
                with self.subTest(action=action, values=values):
                    self.assertIsInstance(self.tool.execute(action, values, self.api), ActionFailed)
            request.assert_not_called()
            query.assert_not_called()

    def test_guard_covers_scripts_prompts_search_and_nested_section_text(self):
        secret = "contact person@example.com"
        cases = [
            ("generate_speech", {"text": secret, "voice_id": "voice"}),
            ("generate_sound_effect", {"text": secret, "duration_seconds": 5}),
            ("generate_music", {"prompt": secret, "duration_ms": 5000}),
            ("generate_music", {"sections": [{"text": secret, "duration_ms": 5000, "positive_styles": []}]}),
            ("generate_music", {"sections": [{"text": "", "duration_ms": 5000, "positive_styles": [secret]}]}),
            ("generate_music", {"sections": [{"text": "", "duration_ms": 5000, "positive_styles": [], "negative_styles": [secret]}]}),
            ("list_voices", {"search": secret}),
            ("list_voices", {"next_page_token": "sk-proj-" + "a" * 60}),
            ("design_voice", {"voice_description": secret}),
            ("design_voice", {"voice_description": "A warm and expressive storyteller.", "text": secret * 5}),
            ("save_voice", {"generated_voice_id": "preview", "name": secret, "voice_description": "A warm and expressive storyteller."}),
            ("save_voice", {"generated_voice_id": "preview", "name": "Narrator", "voice_description": secret}),
        ]
        with patch.object(el, "open_response_stream") as request, patch.object(el, "json_request") as query:
            for action, values in cases:
                with self.subTest(action=action, values=values):
                    self.assertIsInstance(self.tool.execute(action, values, self.api), ActionFailed)
            request.assert_not_called()
            query.assert_not_called()

    def test_chunked_audio_invalid_types_and_size_bound(self):
        for raw, media in ((b"<html>error</html>", "audio/mpeg"), (MP3, "text/html"), (MP3, "audio/mpeg")):
            @contextmanager
            def response(*args, **kwargs):
                yield io.BytesIO(raw), {"content-type": media}
            with patch.object(el, "open_response_stream", response), patch.object(el, "MAX_AUDIO_BYTES", 32):
                result = self.tool.execute("generate_music", {"prompt": "piano", "duration_ms": 5000}, self.api)
            self.assertIsInstance(result, ActionFailed)

    def test_provider_errors_are_redacted_and_not_retried(self):
        for status in (400, 401, 402, 403, 404, 422, 429, 500):
            with patch.object(el, "open_response_stream", side_effect=WebRequestError("raw secret", status=status, body=b'private script and token')) as request:
                result = self.tool.execute("generate_sound_effect", {"text": "wind", "duration_seconds": 4}, self.api)
            self.assertIsInstance(result, ActionFailed)
            self.assertNotIn("private", result.error)
            self.assertNotIn("secret", result.error)
            self.assertEqual(request.call_count, 1)

    def test_voice_lookup_is_paginated_normalized_and_schema_valid(self):
        response = {"voices": [{"voice_id": "voice_1", "name": "Narrator", "description": "warm", "category": "premade", "preview_url": "https://untrusted.test/"}], "has_more": True, "next_page_token": "next-token"}
        with patch.object(el, "json_request", return_value=response) as request:
            result = self.tool.execute("list_voices", {"search": "warm", "page_size": 10, "next_page_token": "prior-token"}, self.api)
        self.assertIsInstance(result, ActionExecuted)
        assert_matches_output_schema(self, el.MANIFEST, "list_voices", result)
        self.assertNotIn("preview_url", result.result["voices"][0])
        self.assertIn("next_page_token=prior-token", request.call_args.args[1])
        self.assertEqual(result.result["next_page_token"], "next-token")
