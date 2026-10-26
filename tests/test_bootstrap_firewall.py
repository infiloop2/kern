"""Execute the bootstrap's SSH rule generation without changing host rules.

Management ingress must survive a Cloudflare-only Lima boot while AWS keeps
its operator-selected ingress policy. Temporary files replace bootstrap's
fixed handoff paths; the actual embedded Python runs unchanged otherwise.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from host.bootstrap import render


class BootstrapSshFirewallTests(unittest.TestCase):
    def _rules(self, payload: dict, *, operator_ssh: bool) -> tuple[int, str]:
        source = render._render_bootstrap().split(
            "write_firewall() {\npython3 - <<'PY'\n", 1
        )[1].split("\nPY\n", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "kern_payload.json").write_text(json.dumps(payload))
            (root / "kern_effective_config.json").write_text(json.dumps({
                "operator_connections": [{
                    "mode": "ssh" if operator_ssh else "cloudflare_tunnel",
                }],
            }))
            result = subprocess.run(
                [sys.executable, "-c", source.replace("/tmp/", f"{directory}/")],
                capture_output=True, text=True, check=False,
            )
            output = root / "kern_ssh_rule"
            return result.returncode, output.read_text() if output.exists() else ""

    def test_management_ingress_is_independent_of_operator_access(self) -> None:
        for management_source in (None, "192.168.5.2"):
            for operator_ssh in (False, True):
                with self.subTest(source=management_source, operator_ssh=operator_ssh):
                    payload = render._bootstrap_payload(
                        "test", None, mode="deploy", target_version="1.0.0",
                        management_ssh_source=management_source,
                    )
                    code, rules = self._rules(payload, operator_ssh=operator_ssh)
                    self.assertEqual(code, 0)
                    expected = []
                    if management_source:
                        expected.append("    ip saddr 192.168.5.2 tcp dport 22 accept")
                    else:
                        self.assertNotIn("management_ssh_source", payload)
                    if operator_ssh:
                        expected.append("    tcp dport 22 accept")
                    self.assertEqual(rules.splitlines(), expected)
                    self.assertNotIn("management_ssh_source", payload["runtime_config"])

    def test_management_source_cannot_expand_into_a_network_or_expression(self) -> None:
        for source in ("192.168.5.0/24", "::1", "192.168.5.2 tcp accept", "", 123):
            with self.subTest(source=source):
                code, rules = self._rules(
                    {"management_ssh_source": source}, operator_ssh=True,
                )
                self.assertNotEqual(code, 0)
                self.assertEqual(rules, "")


if __name__ == "__main__":
    unittest.main()
