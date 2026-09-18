"""Captured Grok protocol replay, AWS signing vectors and storage boundaries."""
import datetime
from dataclasses import replace
import gzip
import io
import json
import unittest
from urllib.parse import urlsplit, parse_qs
from unittest.mock import patch

from host.config import parse_network_controls
from host.network_integrations import runtime
from host.network_integrations.base import ResponseRewrite
from host.network_integrations.xai import guard, video
from host.network_integrations.xai.manifest import XaiIntegration
from host.runtime.admin_api import xai_video_storage
from host.runtime.core import state
from host.runtime.network_proxy import service
from test_network_proxy import xai_bearer

CONFIG = dict(bucket="test-videos", region="us-east-1", access_key_id="AKIA" + "A" * 16, secret_access_key="a" * 40)
KEY = "grok-videos/11111111-2222-4333-8444-555555555555.mp4"
HEADERS = [("Content-Type", "application/json"), ("Authorization", xai_bearer("pinned"))]


class VideoTests(unittest.TestCase):
    def setUp(self):
        for name, value in (("read_xai_video_storage", CONFIG), ("xai_video_storage_metadata", dict(configured=True, bucket=CONFIG['bucket'], region=CONFIG['region']))):
            p = patch.object(state, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(guard, 'read_proxy_xai_account_id', return_value='pinned')
        p.start()
        self.addCleanup(p.stop)

    def denial(self, payload, path='/v1/videos/generations'):
        return guard.request_denied(XaiIntegration(enabled=True), 'POST', 'api.x.ai', path, '', HEADERS, json.dumps(payload).encode())

    def test_aws_sdk_vectors(self):
        # Generated independently with botocore 1.43.63 S3SigV4QueryAuth,
        # frozen at this UTC instant, expires=900, Content-Type=video/mp4 PUT.
        when = datetime.datetime(2026, 9, 17, 19, 4, 6, tzinfo=datetime.timezone.utc)
        for method, signature in [('PUT', 'ec56c66912a11fcb0f0dc9c4e34ba779c3c4685cc7218e569bb791ca080da149'), ('GET', 'd62495b894aee9cfbfa0797145ff83b4dffcff904ef00397b9576e2c364b8ddc')]:
            query = parse_qs(urlsplit(video.signed_url(CONFIG, method, KEY, now=when)).query)
            self.assertEqual(query['X-Amz-Signature'], [signature])

    def test_capture_protocol_gzip_chunked_response_and_download(self):
        # Same request and completed-response structures as the operator's
        # 1.0.34 capture. Inline bytes and prompt are synthetic.
        original = dict(model='grok-imagine-video-1.5', prompt='A lion says hi', image={'url':'data:image/jpeg;base64,YQ=='}, duration=6, resolution='480p', reference_audios=[{'voice_id':'eve'}])
        self.assertIsNone(self.denial(original))
        wire = gzip.compress(json.dumps(original).encode())
        controls = parse_network_controls({'network_integrations': {'xai': {'enabled': True}}})
        headers, body = runtime.prepare_request(controls, 'POST', 'api.x.ai', '/v1/videos/generations', '', HEADERS + [('Content-Encoding','gzip'), ('Content-Length',str(len(wire)))], wire)
        self.assertIsNone(runtime.prepare_response(controls, 'POST', 'api.x.ai', '/v1/videos/generations', '', headers, body))
        self.assertFalse(any(k.lower() in {'content-encoding','content-length'} for k,v in headers))
        sent = json.loads(body)
        upload = sent.pop('output')['upload_url']
        self.assertEqual(sent, original)
        key = video.verified_key(CONFIG, 'PUT', upload)
        self.assertIsNotNone(key)
        rewrite = runtime.prepare_response(controls, 'GET', 'api.x.ai', '/v1/videos/11111111-2222-4333-8444-555555555555', '', HEADERS, b'')
        complete = dict(status='done', video=dict(url=upload, duration=6, respect_moderation=True), model=original['model'], progress=100, usage={'cost_in_usd_ticks':1})
        compressed = gzip.compress(json.dumps(complete).encode())
        wire = b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Encoding: gzip\r\nTransfer-Encoding: chunked\r\nX-Zero-Data-Retention: true\r\n\r\n' + f'{len(compressed):x}\r\n'.encode() + compressed + b'\r\n0\r\n\r\n'
        out = Socket()
        service.forward_rewritten_response(Socket(wire), out, rewrite)
        head, body = out.data.split(b'\r\n\r\n',1)
        self.assertIn(b'X-Zero-Data-Retention: true', head)
        self.assertNotIn(b'Content-Encoding', head)
        result = json.loads(body)
        download = result['video']['url']
        self.assertEqual(video.verified_key(CONFIG, 'GET', download), key)
        result['video']['url'] = upload
        self.assertEqual(result, complete)
        parsed = urlsplit(download)
        controls = parse_network_controls({'network_integrations':{'xai':{'enabled':True}}})
        self.assertFalse(runtime.host_allowed(controls, parsed.hostname))
        self.assertEqual(runtime.request_denied(controls, 'GET', parsed.hostname, parsed.path, parsed.query, [], b''), 'network_policy_denied')
        # Exactly the separately configured rule documented in the guide.
        controls = parse_network_controls({'network_integrations': {'xai': {'enabled': True}, 'custom': {'domains': {parsed.hostname: {'allow_http_methods': ['GET'], 'path_guards': ['/grok-videos/.*']}}}}})
        self.assertTrue(runtime.host_allowed(controls, parsed.hostname))
        self.assertIsNone(runtime.request_denied(controls, 'GET', parsed.hostname, parsed.path, parsed.query, [], b''))
        self.assertEqual(runtime.request_denied(controls, 'PUT', parsed.hostname, parsed.path, parsed.query, [], b''), 'network_policy_denied')

    def test_preparation_hook_does_not_touch_other_integrations_or_disabled_xai(self):
        controls = parse_network_controls({'network_integrations': {
            'github': {'enabled': True}, 'openai': {'enabled': True},
            'custom': {'domains': {'example.com': {'allow_http_methods': ['GET']}}},
        }})
        with patch.object(video, 'prepare_request', side_effect=AssertionError('xAI must not run')), patch.object(video, 'prepare_response', side_effect=AssertionError('xAI must not run')):
            for host in ('api.github.com', 'api.openai.com', 'example.com', 'api.x.ai'):
                with self.subTest(host=host):
                    self.assertEqual(runtime.prepare_request(controls, 'GET', host, '/', '', HEADERS, b'unchanged'), (HEADERS, b'unchanged'))
                    self.assertIsNone(runtime.prepare_response(controls, 'GET', host, '/', '', HEADERS, b'unchanged'))

    def test_request_and_response_hooks_are_independently_optional(self):
        controls = parse_network_controls({'network_integrations': {'xai': {'enabled': True}}})
        args = (controls, 'GET', 'api.x.ai', '/v1/videos/11111111-2222-4333-8444-555555555555', '', HEADERS, b'')
        registered = runtime.GUARDS['xai']
        with patch.dict(runtime.GUARDS, xai=replace(registered, prepare_request=None)):
            self.assertEqual(runtime.prepare_request(*args), (HEADERS, b''))
            self.assertIsInstance(runtime.prepare_response(*args), ResponseRewrite)
        with patch.dict(runtime.GUARDS, xai=replace(registered, prepare_response=None)):
            headers, body = runtime.prepare_request(*args)
            self.assertIn(('Accept-Encoding', 'gzip'), headers)
            self.assertEqual(body, b'')
            self.assertIsNone(runtime.prepare_response(*args))

    def test_non_video_xai_requests_do_not_load_storage_or_rewrite_responses(self):
        controls = parse_network_controls({'network_integrations': {'xai': {'enabled': True}}})
        with patch.object(state, 'read_xai_video_storage', side_effect=AssertionError('storage is video-only')):
            for host, path in [('auth.x.ai', '/oauth/token'), ('cli-chat-proxy.grok.com', '/v1/responses'), ('api.x.ai', '/v1/images/generations'), ('api.x.ai', '/v1/images/edits')]:
                with self.subTest(host=host, path=path):
                    self.assertEqual(runtime.prepare_request(controls, 'POST', host, path, '', HEADERS, b'unchanged'), (HEADERS, b'unchanged'))
                    self.assertIsNone(runtime.prepare_response(controls, 'POST', host, path, '', HEADERS, b'unchanged'))

    def test_response_transport_uses_the_integrations_failure_code(self):
        def reject(status, headers, body):
            raise ValueError('private diagnostic that must not reach the caller')
        wire = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}'
        out = Socket()
        service.forward_rewritten_response(Socket(wire), out, ResponseRewrite(reject, 'test_response_invalid'))
        self.assertIn(b'502 Bad Gateway', out.data)
        self.assertTrue(out.data.endswith(b'test_response_invalid'))
        self.assertNotIn(b'private diagnostic', out.data)

    def test_no_storage_still_allows_images_and_blocks_video(self):
        with patch.object(state, 'xai_video_storage_metadata', return_value={'configured':False}), patch.object(state,'read_xai_video_storage',return_value=None):
            self.assertIsNone(self.denial({'prompt':'image'}, '/v1/images/generations'))
            self.assertIsNone(self.denial({'prompt':'edit', 'image': {'url': 'data:image/jpeg;base64,YQ=='}}, '/v1/images/edits'))
            self.assertEqual(self.denial({'prompt':'video'}), 'xai_video_storage_required')
            with self.assertRaisesRegex(OSError, 'storage_required'):
                video.prepare_request('POST','api.x.ai','/v1/videos/generations',HEADERS,b'{}')

    def test_rewrite_overrides_output_even_on_normalized_route(self):
        for path in ('/v1/videos/generations', '/v1/%76ideos/generations', '/v1/unused/../videos/generations'):
            payload = {'prompt':'test', 'output':{'upload_url':'https://attacker.invalid/leak'}}
            self.assertIsNone(self.denial(payload,path))
            _, body = video.prepare_request('POST','API.X.AI',path,HEADERS,json.dumps(payload).encode())
            url = json.loads(body)['output']['upload_url']
            self.assertIsNotNone(video.verified_key(CONFIG,'PUT',url))
            self.assertNotIn('attacker',url)

    def test_external_inputs_and_unknown_destination_fields_denied(self):
        for payload in ({'image':{'url':'https://attacker.invalid/a'}}, {'reference_images':[{'url':'https://attacker.invalid/a'}]}, {'reference_audios':[{'url':'https://attacker.invalid/a'}]}, {'callback_url':'https://attacker.invalid'}, {'images':[{'url':'https://attacker.invalid'}]}, {'keyframes':[{'image':{'url':'https://attacker.invalid'}}]}):
            self.assertEqual(self.denial(payload),'xai_media_input_denied',payload)
        self.assertEqual(self.denial({'output':{}},'/v1/images/generations'),'xai_media_input_denied')
        self.assertEqual(self.denial({'output':{}},'/v1/images/edits'),'xai_media_input_denied')
        self.assertEqual(self.denial({'image':{'url':'https://attacker.invalid/a'}}, '/v1/images/edits'),'xai_media_input_denied')

    def test_url_capability_rejects_tampering_expiry_wrong_method_and_credentials(self):
        url = video.signed_url(CONFIG,'GET',KEY)
        self.assertEqual(video.verified_key(CONFIG,'GET',url),KEY)
        for bad in (url.replace('test-videos','another-bucket'),url.replace('grok-videos','other'),url+'&extra=1',url+'#fragment',url.replace('https://','http://'),url.replace('s3.us-east-1','s3.us-west-2'),url[:-1]+'x'):
            self.assertIsNone(video.verified_key(CONFIG,'GET',bad),bad)
        self.assertIsNone(video.verified_key(CONFIG,'PUT',url))
        self.assertIsNone(video.verified_key(dict(CONFIG,secret_access_key='b'*40),'GET',url))
        for delta in (-901, 60):
            when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=delta)
            self.assertIsNone(video.verified_key(CONFIG,'GET',video.signed_url(CONFIG,'GET',KEY,now=when)))

    def test_storage_never_changes_custom_domain_ownership_or_rules(self):
        hosts = ('test-videos.s3.us-east-1.amazonaws.com',
                 's3.us-east-1.amazonaws.com', 'other.s3.us-east-1.amazonaws.com')
        for enabled in (False, True):
            controls = parse_network_controls({'network_integrations': {
                'xai': {'enabled': enabled}, 'custom': {'domains': {
                    host: {'allow_http_methods': ['GET', 'PUT']} for host in hosts
                }},
            }})
            for metadata in ({'configured': False}, dict(configured=True, bucket='test-videos', region='us-east-1'), dict(configured=True, bucket='other', region='us-east-1')):
                with patch.object(state, 'xai_video_storage_metadata', return_value=metadata):
                    for host in hosts:
                        with self.subTest(enabled=enabled, metadata=metadata, host=host):
                            self.assertTrue(runtime.host_allowed(controls, host))
                            for method in ('GET', 'PUT'):
                                self.assertIsNone(runtime.request_denied(controls, method, host, '/object', '', [], b''))
            self.assertFalse(runtime.host_allowed(controls, 'unlisted.s3.us-east-1.amazonaws.com'))

    def test_documented_download_rule_enforces_method_and_prefix(self):
        host = 'test-videos.s3.us-east-1.amazonaws.com'
        controls = parse_network_controls({'network_integrations': {'custom': {'domains': {
            host: {'allow_http_methods': ['GET'], 'path_guards': ['/grok-videos/.*']},
        }}}})
        # This is an ordinary operator rule. AWS authenticates the signature;
        # Kern does not impose a second Grok-specific download restriction.
        self.assertIsNone(runtime.request_denied(controls, 'GET', host, '/grok-videos/example.mp4', 'X-Amz-Signature=example', [], b''))
        for method, path in [('PUT', '/grok-videos/example.mp4'), ('GET', '/'), ('GET', '/other/example.mp4'), ('GET', '/grok-videos/../other/example.mp4')]:
            self.assertEqual(runtime.request_denied(controls, method, host, path, '', [], b''), 'network_policy_denied')

    def test_poll_pending_failed_and_malformed_completed(self):
        rewrite = video.prepare_response('GET','api.x.ai','/v1/videos/11111111-2222-4333-8444-555555555555')
        headers = [('Content-Type','application/json')]
        for status, body in [(202,b'{"status":"pending"}'),(200,b'{"status":"failed","error":"S3 returned 403"}'),(403,b'forbidden')]:
            self.assertEqual(rewrite.apply(status,headers,body),(headers,body))
        for body in [b'{', b'{"status":"done","video":{"url":"https://attacker.invalid/a"}}'] + [
            json.dumps({'status': 'done', 'video': {'url': value}}).encode()
            for value in (None, 42, True, [], {})
        ]:
            wire=b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
            out=Socket()
            service.forward_rewritten_response(Socket(wire),out,rewrite)
            self.assertIn(b'502 Bad Gateway',out.data)
            self.assertTrue(out.data.endswith(b'xai_video_response_invalid'))
            self.assertNotIn(b'attacker',out.data)

    def test_admin_validation_rejects_partial_or_unsupported_storage(self):
        with patch.object(state,'save_xai_video_storage') as save:
            for value in ({},dict(CONFIG,region='us-gov-west-1'),dict(CONFIG,endpoint='https://attacker.invalid'),dict(CONFIG,secret_access_key=''),dict(CONFIG,access_key_id='ASIA'+'A'*16)):
                with self.assertRaises(ValueError):
                    xai_video_storage.replace(value)
            save.assert_not_called()
            result=xai_video_storage.replace(CONFIG)
            save.assert_called_once_with(CONFIG)
            self.assertEqual(set(result),{'configured','bucket','region'})


class Socket:
    def __init__(self, incoming=b''):
        self.incoming=incoming
        self.data=b''
    def makefile(self,*args,**kwargs):
        return io.BytesIO(self.incoming)
    def sendall(self,data):
        self.data+=data
