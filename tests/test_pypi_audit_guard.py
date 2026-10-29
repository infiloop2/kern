"""Keep pip-audit's release lookup inside the read-only PyPI contract.
Accept project and version JSON metadata without opening other API paths.
Retain parameter protection, integration enablement and method restrictions.
These tests make no external requests.
"""

import unittest

from host.config import parse_network_controls
from host.network_integrations import runtime
from host.network_integrations.base import ManagedIntegration
from host.network_integrations.python_packages import guard


class PypiAuditGuardTests(unittest.TestCase):
    def denied(self, path: str, method: str = "GET", host: str = "pypi.org", query: str = "") -> str | None:
        return guard.request_denied(ManagedIntegration(True), method, host, path, query, [], b"")

    def test_project_and_release_metadata(self) -> None:
        for path in ("/pypi/pillow/json", "/pypi/pillow/13.0.0/json",
                     "/pypi/boto3/1.43.63/json", "/pypi/example/1!2.0rc1.post2.dev3+local/json"):
            for method in ("GET", "HEAD"):
                with self.subTest(path=path, method=method):
                    self.assertIsNone(self.denied(path, method))

    def test_no_new_methods_hosts_or_neighboring_routes(self) -> None:
        path = "/pypi/pillow/13.0.0/json"
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(self.denied(path, method), "network_policy_denied")
        for host in ("upload.pypi.org", "pypi.org.example.com", "files.pythonhosted.org"):
            self.assertEqual(self.denied(path, host=host), "network_policy_denied")
        for other in ("/pypi/json", path + "/", path + "/extra", "/pypi/pillow/13.0.0/../../../admin/json",
                      "/pypi/pillow/13.0.0/extra/json", "/pypi/pillow/13.0.0", "/pypi/pillow/13.0.0/xml"):
            self.assertEqual(self.denied(other), "network_policy_denied")

    def test_parameters_remain_guarded(self) -> None:
        for path in ("/pypi/AKIAIOSFODNN7EXAMPLE/1.0/json", "/pypi/example/AKIAIOSFODNN7EXAMPLE/json"):
            self.assertEqual(self.denied(path), "request_param_secret_denied")
        self.assertEqual(self.denied("/pypi/example/alice%40example.com/json"), "request_param_pii_denied")
        self.assertEqual(self.denied("/pypi/example/1.0/json", query="access_token=AKIAIOSFODNN7EXAMPLE"),
                         "network_policy_denied")

    def test_identifier_checks_also_apply_to_version_strings(self) -> None:
        # Intentional conservative limit: no timestamp or other ID exemptions.
        for version in ("1.0.post20240315123456", "1.0.post4111111111111111"):
            self.assertEqual(self.denied(f"/pypi/example/{version}/json"), "request_param_pii_denied")

    def test_disabled_integration_stays_denied(self) -> None:
        for enabled in (False, True):
            config = parse_network_controls({"network_integrations": {"python_packages": {"enabled": enabled}}})
            denial = runtime.request_denied(config, "GET", "pypi.org", "/pypi/pillow/13.0.0/json", "", [], b"")
            if enabled:
                self.assertIsNone(denial)
            else:
                self.assertIsNotNone(denial)
