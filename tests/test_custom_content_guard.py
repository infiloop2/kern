"""The opt-in custom-domain guard covers request fields without exceptions."""
from __future__ import annotations

import unittest

from host.config import parse_network_controls
from host.network_integrations import runtime
from host.network_integrations.registry import denial_reason_catalog


class CustomContentGuardTests(unittest.TestCase):
    def controls(self, enabled: bool):
        return parse_network_controls({"network_integrations": {"custom": {"domains": {
            "*.example.com": {"allow_http_methods": ["GET", "POST"],
                              "guard_request_content": enabled, "allow_websocket": True},
        }}}})

    def test_enabled_guard_covers_headers_and_decoded_body_fields(self) -> None:
        key = "sk-proj-" + "a" * 24
        cases = [
            ([("Authorization", "Bearer " + key)], b"", "request_param_secret_denied"),
            ([("Cookie", "session=" + key)], b"", "request_param_secret_denied"),
            ([(key, "hello")], b"", "request_param_secret_denied"),
            ([("X-Note", "alice%2540example.com")], b"", "request_param_pii_denied"),
            ([("X-Note", "x" * 200)], b"", "request_param_encoded_blob_denied"),
            ([("X-Note", "weather")]*90, b"", "request_param_too_large"),
            ([], b"alice@example.com", "request_param_pii_denied"),
            ([("Content-Type", "application/json")], br'{"nested":[{"email":"alice\u0040example.com"}]}', "request_param_pii_denied"),
            ([("Content-Type", "application/json")], b'{"password":"huntertwo"}', "request_param_secret_denied"),
            ([("Content-Type", "application/json")], b'{"x":"alice@example.com","x":"weather"}', "request_param_pii_denied"),
            ([("Content-Type", "application/json")], b'{"alice@example.com":"weather"}', "request_param_pii_denied"),
            ([("Content-Type", "application/json")], b'{"code":12345678901}', "request_param_pii_denied"),
            ([("Content-Type", "application/x-www-form-urlencoded")], b'email=alice%40example.com&email=weather', "request_param_pii_denied"),
            ([("Content-Type", "application/x-www-form-urlencoded")], b'password=huntertwo', "request_param_secret_denied"),
            ([], b"word " * 205, "request_param_too_large"),
        ]
        for headers, body, reason in cases:
            for enabled in (True, False):
                with self.subTest(headers=headers, body=body, enabled=enabled):
                    denial = runtime.request_denied(self.controls(enabled), "POST", "api.example.com", "/lookup", "", headers, body)
                    self.assertEqual(denial, reason if enabled else None)

    def test_supported_bodies_and_protocol_headers_pass(self) -> None:
        for media_type, body in (
            ("text/plain", b"hello world"),
            ("application/json", b'{"query":"weather","page":2,"active":true}'),
            ("application/problem+json; charset=utf-8", b'{"title":"weather"}'),
            ("application/x-www-form-urlencoded", b"query=hello+world&page=2"),
        ):
            with self.subTest(media_type=media_type):
                headers = [("Host", "api.example.com"), ("User-Agent", "curl/8.0"),
                           ("Accept", "*/*"), ("Content-Type", media_type), ("Content-Length", str(len(body)))]
                self.assertIsNone(runtime.request_denied(self.controls(True), "POST", "api.example.com", "/lookup", "q=weather", headers, body))

    def test_json_escapes_are_inspected_independently_of_mime(self) -> None:
        for media_type in (None, "text/plain", "application/x-www-form-urlencoded", "application/json"):
            headers = [("Content-Type", media_type)] if media_type else []
            for body in (
                br'{"email":"alice\u0040example.com"}',
                br' ["alice\u0040example.com"]',
                br'"alice\u0040example.com"',
            ):
                with self.subTest(media_type=media_type, body=body):
                    self.assertEqual(runtime.request_denied(
                        self.controls(True), "POST", "api.example.com", "/", "", headers, body,
                    ), "request_param_pii_denied")
                    self.assertIsNone(runtime.request_denied(
                        self.controls(False), "POST", "api.example.com", "/", "", headers, body,
                    ))
            self.assertIsNone(runtime.request_denied(
                self.controls(True), "POST", "api.example.com", "/", "", headers, b'{"query":"weather"}',
            ))
            self.assertEqual(runtime.request_denied(
                self.controls(True), "POST", "api.example.com", "/", "", headers, b'{"query":',
            ), "custom_content_uninspectable")

    def test_form_fields_are_inspected_independently_of_mime(self) -> None:
        for media_type in (None, "text/plain", "application/x-www-form-urlencoded"):
            headers = [("Content-Type", media_type)] if media_type else []
            for body in (b"phone=415+555+1234", b"415+555+1234=weather",
                         b"phone=weather&phone=415+555+1234", b"phone+415+555+1234"):
                with self.subTest(media_type=media_type, body=body):
                    self.assertEqual(runtime.request_denied(
                        self.controls(True), "POST", "api.example.com", "/", "", headers, body,
                    ), "request_param_pii_denied")
                    self.assertIsNone(runtime.request_denied(
                        self.controls(False), "POST", "api.example.com", "/", "", headers, body,
                    ))
            self.assertIsNone(runtime.request_denied(
                self.controls(True), "POST", "api.example.com", "/", "", headers, b"query=hello+world&page=2",
            ))

    def test_uninspectable_bodies_are_denied_only_when_enabled(self) -> None:
        for headers, body in (
            ([], b"\xff"),
            ([("Content-Type", "application/json")], b"{"),
            ([("Content-Type", "application/json")], br'{"x":"\ud800"}'),
            ([("Content-Encoding", "gzip")], b"compressed bytes"),
            ([("Content-Type", "text/plain; charset=utf-16")], b"hello"),
            ([("Content-Type", "application/octet-stream")], b"hello"),
            ([("Content-Type", "multipart/form-data; boundary=sample")], b"hello"),
            ([("Content-Type", "text/html")], b"hello"),
            ([("Content-Type", "invalid")], b"hello"),
        ):
            for enabled in (True, False):
                with self.subTest(headers=headers, body=body, enabled=enabled):
                    denial = runtime.request_denied(self.controls(enabled), "POST", "api.example.com", "/", "", headers, body)
                    if enabled:
                        self.assertIn(denial, {"custom_content_uninspectable", "request_param_encoded_blob_denied"})
                    else:
                        self.assertIsNone(denial)
        self.assertIn("custom_content_uninspectable", denial_reason_catalog())

    def test_raw_url_limit_includes_the_actual_hostname(self) -> None:
        self.assertEqual(runtime.request_denied(
            self.controls(True), "GET", "api.example.com", "/lookup",
            "q=" + "word%20" * 142, [], b"",
        ), "request_param_too_large")

    def test_hostnames_and_websockets_do_not_escape_inspection(self) -> None:
        self.assertEqual(runtime.request_denied(self.controls(True), "GET", "sk-proj-" + "a"*24 + ".example.com", "/", "", [], b""), "request_param_secret_denied")
        for enabled in (True, False):
            self.assertEqual(runtime.websocket_allowed(self.controls(enabled), "api.example.com"), not enabled)
            self.assertEqual(runtime.request_denied(self.controls(enabled), "GET", "api.example.com", "/", "", [("Upgrade", "websocket")], b""), "websocket_not_allowed" if enabled else None)
