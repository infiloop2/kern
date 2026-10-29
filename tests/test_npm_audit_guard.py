"""Exercise the npm advisory POST without widening registry write access.
Cover npm's gzip transport, bounded decompression and exact payload shape.
Keep package parameter checks and credential stripping on the new route.
Reject alternate destinations, endpoints, methods and uninspectable content.
"""
from __future__ import annotations

import gzip
import io
import json
import unittest

from host.config import parse_network_controls
from host.network_integrations import runtime
from host.network_integrations.base import ManagedIntegration
from host.network_integrations.npm_packages import guard


class NpmAuditGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ManagedIntegration(True)
        self.headers = [("Content-Type", "application/json")]
        self.body = json.dumps({
            "next": ["16.4.1"], "@babel/core": ["7.28.0"],
            "JSONStream": ["1.3.5"], "token-types": ["6.0.0"],
            "@aws-sdk/credential-provider-env": ["3.1037.0"],
            "example": ["1.2.3-beta.1+build.2"],
            "workspace-a": ["1.0", "v1.2.3", "development"],
            "@_scope/pkg": ["1.0.0"], "@.scope/pkg": ["1.0.0"],
            "@-scope/pkg": ["1.0.0"], "@scope/_pkg": ["1.0.0"],
            "@scope/-pkg": ["1.0.0"], "legacy!name": ["1.0.0"],
            "-pkg": ["1.0.0-20240315123456", "1.0.0+20240315123456"],
            "legacy-" * 32: ["1.0.0"],
        }).encode()

    def deny(self, body: bytes, headers: list[tuple[str, str]] | None = None,
             method: str = "POST", host: str = "registry.npmjs.org",
             path: str = guard.AUDIT_PATH, query: str = "") -> str | None:
        return guard.request_denied(self.config, method, host, path, query,
                                    self.headers if headers is None else headers, body)

    def test_plain_and_npm_gzip_payloads(self) -> None:
        self.assertIsNone(self.deny(self.body))
        self.assertIsNone(self.deny(gzip.compress(self.body),
                                   self.headers + [("Content-Encoding", "gzip")]))

    def test_runtime_dispatch_requires_enabled_integration(self) -> None:
        for enabled in (True, False):
            controls = parse_network_controls({"network_integrations": {"npm_packages": {"enabled": enabled}}})
            result = runtime.request_denied(controls, "POST", "registry.npmjs.org",
                                            guard.AUDIT_PATH, "", self.headers, self.body)
            if enabled:
                self.assertIsNone(result)
            else:
                self.assertIsNotNone(result)

    def test_only_exact_endpoint_and_host(self) -> None:
        for path in ("/pkg", "/-/user/org.couchdb.user:someone", "/-/npm/v1/security/audits/quick",
                     guard.AUDIT_PATH + "/", guard.AUDIT_PATH + "/extra",
                     guard.AUDIT_PATH.replace("bulk", "%62ulk")):
            with self.subTest(path=path):
                self.assertEqual(self.deny(self.body, path=path), "network_policy_denied")
        for host in ("nodejs.org", "npmjs.org", "registry.npmjs.org.example.com"):
            with self.subTest(host=host):
                self.assertEqual(self.deny(self.body, host=host), "network_policy_denied")
        self.assertEqual(self.deny(self.body, query="extra=1"), "network_policy_denied")
        for method in ("PUT", "PATCH", "DELETE"):
            self.assertEqual(self.deny(self.body, method=method), "network_policy_denied")

    def test_invalid_or_extra_payload_fields(self) -> None:
        for payload in ([], None, "text", {"name": "project"}, {"dependencies": {"pkg": "1.0.0"}},
                        {"pkg": []}, {"pkg": [1]}, {"pkg": [None]}, {"pkg": [{}]},
                        {"pkg": [""]}, {"../pkg": ["1.0.0"]}, {"pkg": ["1.0.0-" * 50]},
                        {"@scope/.pkg": ["1.0.0"]}, {"_pkg": ["1.0.0"]}):
            with self.subTest(payload=payload):
                self.assertEqual(self.deny(json.dumps(payload).encode()), "network_policy_denied")
        for body in (b'{"pkg":["1.0.0"],"pkg":["2.0.0"]}', b'{', b'\xff', b'{} trailing'):
            self.assertEqual(self.deny(body), "network_policy_denied")

    def test_parameter_guard_applies_to_names_and_versions(self) -> None:
        for payload in ({"pkg-AKIAIOSFODNN7EXAMPLE": ["1.0.0"]},
                        {"pkg": ["1.0.0-AKIAIOSFODNN7EXAMPLE"]},
                        {"token-types-AKIAIOSFODNN7EXAMPLE": ["1.0.0"]}):
            self.assertEqual(self.deny(json.dumps(payload).encode()), "request_param_secret_denied")
        self.assertEqual(self.deny(json.dumps({"pkg": ["person-20240315123456"]}).encode()),
                         "request_param_pii_denied")
        for version in ("1.0.0-4111111111111111", "1.0.0-20241399123456"):
            self.assertEqual(self.deny(json.dumps({"pkg": [version]}).encode()),
                             "request_param_pii_denied")
        self.assertEqual(self.deny(json.dumps({"pkg": ["1.0.0-Q7xK9mP2vR8tW4yN6cF3hJ5sL0aD1gB"]}).encode()),
                         "request_param_encoded_blob_denied")

    def test_mime_and_encoding_must_be_inspectable(self) -> None:
        for headers in ([], [("Content-Type", "text/plain")], self.headers * 2,
                        [("Content-Type", "application/json; charset=utf-16")],
                        self.headers + [("Content-Encoding", "br")],
                        self.headers + [("Content-Encoding", "gzip"), ("Content-Encoding", "identity")]):
            self.assertEqual(self.deny(self.body, headers), "network_policy_denied")

    def test_gzip_limits_truncation_trailing_members_and_metadata(self) -> None:
        headers = self.headers + [("Content-Encoding", "gzip")]
        compressed = gzip.compress(self.body)
        for body in (b"invalid", compressed[:-4], compressed + b"trailing", compressed + compressed):
            self.assertEqual(self.deny(body, headers), "network_policy_denied")
        output = io.BytesIO()
        with gzip.GzipFile(fileobj=output, filename="private-note", mode="wb") as archive:
            archive.write(self.body)
        self.assertEqual(self.deny(output.getvalue(), headers), "network_policy_denied")
        large = b" " * (guard.AUDIT_MAX_BYTES + 1)
        self.assertEqual(self.deny(large), "request_body_too_large")
        self.assertEqual(self.deny(gzip.compress(large), headers), "request_body_too_large")

    def test_existing_downloads_and_header_sanitization(self) -> None:
        self.assertIsNone(self.deny(b"", method="GET", path="/next"))
        self.assertIsNone(self.deny(b"", method="HEAD", host="nodejs.org", path="/dist/index.json"))
        rewritten = guard.rewrite_request_headers(self.config, "POST", "registry.npmjs.org",
            guard.AUDIT_PATH, "", self.headers + [("Authorization", "secret"),
            ("Cookie", "session=secret"), ("User-Agent", "private-client")], self.body)
        self.assertNotIn("authorization", {name.lower() for name, _ in rewritten})
        self.assertNotIn("cookie", {name.lower() for name, _ in rewritten})
        self.assertIn(("User-Agent", "kern-proxy/1"), rewritten)
