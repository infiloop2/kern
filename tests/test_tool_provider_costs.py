from host.tools.fal_ai import h3max, media as fal_media
"""Reported provider charges use only observable usage and tool-owned published rates."""
from contextlib import contextmanager
import io
import unittest
from unittest.mock import patch

from host.runtime.tools.tools_host import BUNDLED_TOOLS
from host.tools import apify, brave_search, instagram_discovery, google_search, openai_images
from host.tools import elevenlabs
from host.tools.results import ActionFailed
from host.tools.shared.cost_reporting import report_priced_units
from test_tools import FakeHostAPI
from test_tools_openai_images import image_response


class ProviderCostTests(unittest.TestCase):
    def test_reporting_manifests_describe_every_action(self):
        names = ('apify', 'apify_developer', 'brave_search', 'elevenlabs', 'fal_ai',
                 'instagram_discovery', 'google_search', 'openai_images',
                 'reddit_scrapecreators')
        for name in names:
            with self.subTest(tool=name):
                manifest = BUNDLED_TOOLS[name].manifest
                self.assertTrue(manifest.reports_cost)
                self.assertTrue(all(action.cost_description.strip() for action in manifest.actions))

    def test_published_rate_rejects_bad_amounts(self):
        api = FakeHostAPI()
        for units in (None, -1, float('nan')):
            report_priced_units(api, units, '0.00188')
        for rate in ('not-a-price', '9e999999'):
            report_priced_units(api, 2, rate)
        self.assertEqual(api.costs.calls, [])
        report_priced_units(api, 2, '0.00188')
        self.assertEqual(api.costs.calls, [('0.003760000', '')])

    def test_brave_reports_one_successful_request(self):
        api = FakeHostAPI(config={'BRAVE_SEARCH_API_KEY': 'test-key'})
        with patch.object(brave_search, '_post_brave_context', return_value={}):
            brave_search.BraveSearchTool().execute('search_web', {'query': 'fox'}, api)
        self.assertEqual(api.costs.calls, [('0.005000000', '')])

    def test_instagram_uses_provider_credits_including_zero(self):
        api = FakeHostAPI()
        with patch.object(instagram_discovery, 'json_request', side_effect=[
            {'success': True, 'credits_used': 2},
            {'success': True, 'credits_used': 0},
            {'success': True, 'credits_charged': 1},
        ]):
            instagram_discovery._provider_request(api, 'key', '/test', {})
            instagram_discovery._provider_request(api, 'key', '/test', {})
            instagram_discovery._provider_request(api, 'key', '/test', {})
        self.assertEqual([call[0] for call in api.costs.calls], ['0.003760000', '0.000000000', '0.001880000'])

    def test_serper_uses_published_rate(self):
        api = FakeHostAPI(config={'SERPERAPI_API_KEY': 'key'})
        with patch.object(google_search, '_search', return_value={}):
            google_search.GoogleSearchTool().execute('search', {'query': 'agents'}, api)
        self.assertEqual(api.costs.calls, [('0.001000000', '')])

    def test_h3max_reports_simple_mode_but_skips_reference_inputs(self):
        api = FakeHostAPI(config={'FAL_API_KEY': 'key'})
        request_id = '764cabcf-b745-4b3e-ae38-1200304cf45b'
        with patch.object(fal_media, 'upload', return_value='https://v3.fal.media/source'), patch.object(h3max, 'json_request', return_value={'request_id': request_id}):
            h3max.H3MaxTool().execute('generate_video', {'prompt': 'a fox', 'image_asset_id': api.assets.add(media_type='image/png')}, api)
            h3max.H3MaxTool().execute('generate_video', {
                'prompt': 'a fox', 'reference_image_asset_ids': [api.assets.add(media_type='image/png')]}, api)
        self.assertEqual(api.costs.calls, [('0.40', f'task:image_{request_id}')])





    def test_openai_images_requires_usage_breakdown(self):
        api = FakeHostAPI(config={'OPENAI_API_KEY': 'key'})
        response = image_response()
        with patch.object(openai_images, 'json_request', return_value=response):
            openai_images.OpenAIImagesTool().execute('generate_image', {'prompt': 'fox'}, api)
        self.assertEqual(api.costs.calls, [])
        response['usage'] = {'input_tokens_details': {'image_tokens': 100, 'text_tokens': 100},
                             'output_tokens_details': {'image_tokens': 200}}
        with patch.object(openai_images, 'json_request', return_value=response):
            openai_images.OpenAIImagesTool().execute('generate_image', {'prompt': 'fox'}, api)
        self.assertEqual(api.costs.calls, [('0.0073', '')])
        response['usage']['input_tokens_details']['cached_tokens'] = 50
        with patch.object(openai_images, 'json_request', return_value=response):
            openai_images.OpenAIImagesTool().execute('generate_image', {'prompt': 'fox'}, api)
        self.assertEqual(api.costs.calls, [('0.0073', ''), ('0.0073', '')])

    def test_elevenlabs_uses_returned_credits_and_published_rate(self):
        api = FakeHostAPI(config={'ELEVENLABS_API_KEY': 'key'})
        @contextmanager
        def audio(*args, **kwargs):
            yield io.BytesIO(b'ID3' + b'a' * 128), {'content-type': 'audio/mpeg', 'character-cost': '42'}
        with patch.object(elevenlabs, 'open_response_stream', audio):
            elevenlabs.ElevenLabsTool().execute('generate_speech', {'text': 'Hello', 'voice_id': 'voice_123'}, api)
        self.assertEqual(api.costs.calls, [('0.008400000', '')])

    def test_elevenlabs_reports_header_charge_before_interrupted_stream(self):
        api = FakeHostAPI(config={'ELEVENLABS_API_KEY': 'key'})

        class BrokenStream:
            def read(self, _size):
                raise ValueError('stream interrupted')

        @contextmanager
        def audio(*args, **kwargs):
            yield BrokenStream(), {'content-type': 'audio/mpeg', 'character-cost': '42'}

        with patch.object(elevenlabs, 'open_response_stream', audio):
            result = elevenlabs.ElevenLabsTool().execute('generate_speech', {'text': 'Hello', 'voice_id': 'voice_123'}, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertEqual(api.costs.calls, [('0.008400000', '')])

    def test_apify_business_search_uses_raw_provider_count(self):
        api = FakeHostAPI(config={'APIFY_API_TOKEN': 'key'})
        items = [{'placeId': 'ChIJ' + 'a' * 23}, {'placeId': 'ChIJ' + 'b' * 23}]
        with patch.object(apify, '_run_actor', return_value=items):
            apify.ApifyTool().execute('search_businesses', {'query': 'coffee', 'location': 'Austin'}, api)
        self.assertEqual(api.costs.calls, [('0.008000000', '')])


if __name__ == '__main__':
    unittest.main()
