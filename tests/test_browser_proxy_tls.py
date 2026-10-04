"""Real offline TLS: gateway identity, encrypted auth and nested website TLS."""
import os
from pathlib import Path
import select
import socket
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from host.runtime.browser.client import BrowserError
from host.runtime.browser_network.relay import Network, TunnelServer
from host.runtime.browser_network.transport import connect_proxy


class BrowserProxyTlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.cert, cls.key = cls.root / 'cert.pem', cls.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(cls.key), '-out', str(cls.cert), '-days', '1',
                        '-subj', '/CN=gate.decodo.com', '-addext',
                        'subjectAltName=DNS:gate.decodo.com,DNS:api.ipify.org'],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.server_context.load_cert_chain(cls.cert, cls.key)
        cls.trusted = ssl.create_default_context(cafile=str(cls.cert))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.warning = self.enterContext(patch("host.runtime.browser_network.transport.host_errors.report_warning"))

    def test_proxy_certificate_rejected_before_credentials_sent(self):
        for hostname, context in [('gate.decodo.com', ssl.create_default_context()),
                                  ('wrong.example', self.trusted)]:
            with self.subTest(hostname=hostname):
                client, peer = socket.socketpair()
                application_data = []
                def serve():
                    try:
                        with self.server_context.wrap_socket(peer, server_side=True) as secure:
                            application_data.append(secure.recv(4096))
                    except ssl.SSLError:
                        pass
                worker = threading.Thread(target=serve)
                worker.start()
                try:
                    with patch('socket.create_connection', return_value=client), patch('ssl.create_default_context', return_value=context):
                        with self.assertRaises(BrowserError):
                            connect_proxy((hostname, 7000), '93.184.215.14', ('user', 'secret'))
                finally:
                    client.close()
                    worker.join(3)
                    peer.close()
                self.assertFalse(worker.is_alive())
                self.assertEqual(application_data, [])
                context = self.warning.call_args.kwargs["context"]
                self.assertEqual(context["stage"], "proxy_tls")
                self.assertEqual(context["error_type"], "SSLCertVerificationError")

    def test_https_ip_check_inside_verified_https_proxy(self):
        self.check_https_ip()

    def test_https_ip_check_recovers_ended_session_through_shared_relay(self):
        self.check_https_ip(rejection=b"HTTP/1.1 502 Bad Gateway\r\nx-error-message: Bad gateway. The session has ended.\r\n\r\n")

    def test_https_ip_check_recovers_timeout_through_shared_relay(self):
        self.check_https_ip(rejection=b"HTTP/1.1 522 Timeout\r\nx-error-message: Timeout. The request took too long to complete and has timed out. Please try sending it again.\r\n\r\n")

    def check_https_ip(self, *, rejection=None):
        client, proxy_peer = socket.socketpair()
        target_pipe, website_peer = socket.socketpair()
        for stream in (client, proxy_peer, target_pipe, website_peer):
            stream.settimeout(3)
            self.addCleanup(stream.close)
        headers, errors = [], []
        expired_client, expired_peer = socket.socketpair()
        for stream in (expired_client, expired_peer):
            stream.settimeout(3)
            self.addCleanup(stream.close)
        def expired_proxy():
            try:
                with self.server_context.wrap_socket(expired_peer, server_side=True) as secure:
                    data = b''
                    while not data.endswith(b'\r\n\r\n'):
                        data += secure.recv(1)
                    headers.append(data)
                    secure.sendall(rejection)
            except Exception as exc:
                errors.append(exc)
        def proxy():
            try:
                with self.server_context.wrap_socket(proxy_peer, server_side=True) as secure:
                    data = b''
                    while not data.endswith(b'\r\n\r\n'):
                        data += secure.recv(1)
                    headers.append(data)
                    secure.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
                    while True:
                        ready = [secure] if secure.pending() else select.select([secure, target_pipe], [], [], 3)[0]
                        if not ready:
                            return
                        for source in ready:
                            chunk = source.recv(16384)
                            if not chunk:
                                return
                            (target_pipe if source is secure else secure).sendall(chunk)
            except BrokenPipeError:
                pass  # Client closes after its bounded HTTP response, before close_notify forwarding.
            except (OSError, ssl.SSLError) as exc:
                errors.append(exc)
        def website():
            try:
                with self.server_context.wrap_socket(website_peer, server_side=True) as secure:
                    request = b''
                    while not request.endswith(b'\r\n\r\n'):
                        request += secure.recv(4096)
                    self.assertNotIn(b'Proxy-Authorization', request)
                    secure.sendall(b'HTTP/1.0 200 OK\r\nContent-Length: 14\r\n\r\n93.184.215.14\n')
            except Exception as exc:
                errors.append(exc)
        workers = [threading.Thread(target=fn) for fn in ([expired_proxy] if rejection else []) + [proxy, website]]
        for worker in workers:
            worker.start()
        from browser_fakes import MemoryStore
        network = Network(MemoryStore())
        network.dispatch('save', {'mode': 'decodo', 'username': 'fixture', 'password': 'secret',
                                 'location': 'new_york'})
        original = network.settings.proxy_username()
        try:
            with TunnelServer(network, port=8009) as relay:
                relay_worker = threading.Thread(target=relay.serve_forever)
                relay_worker.start()
                try:
                    with patch('socket.create_connection', side_effect=([expired_client] if rejection else []) + [client]), patch('ssl.create_default_context', return_value=self.trusted), patch('host.runtime.browser_network.relay.BROWSER_NETWORK_PORT', 8009), patch.dict(os.environ, {'CURL_CA_BUNDLE': str(self.cert)}):
                        self.assertEqual(network.test(), {'mode': 'decodo', 'ip': '93.184.215.14'})
                finally:
                    relay.shutdown()
                    relay_worker.join(3)
        finally:
            network.disconnect()
            target_pipe.shutdown(socket.SHUT_RDWR)
            for worker in workers:
                worker.join(4)
        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertFalse(errors, errors)
        self.assertEqual(len(headers), 2 if rejection else 1)
        if rejection:
            self.assertNotEqual(headers[0], headers[1])
            self.assertNotEqual(network.settings.proxy_username(), original)
            self.assertEqual(Network(network.settings.store).settings.proxy_username(), network.settings.proxy_username())
        self.assertIn(b'Proxy-Authorization: Basic ', headers[0])
        self.assertTrue(headers[0].startswith(b'CONNECT api.ipify.org:443 HTTP/1.1'))
