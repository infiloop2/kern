"""Architectural tests for the operator-side provider boundary."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from host.bootstrap import render


ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class ProviderBoundaryTests(unittest.TestCase):
    def test_shared_cli_modules_only_parse_and_dispatch(self) -> None:
        lifecycle = (ROOT / "host/cli/lifecycle.py").read_text()
        power = (ROOT / "host/cli/power.py").read_text()
        for source in (lifecycle, power):
            self.assertNotIn("subprocess", source)
            self.assertNotIn("_aws(", source)
            self.assertNotIn("_limactl(", source)
            self.assertNotIn("_bootstrap_payload", source)
        self.assertIn("lifecycle_aws.main_for_lifecycle", lifecycle)
        self.assertIn("lifecycle_lima.main_for_lifecycle", lifecycle)
        self.assertIn("power_aws.main_for_power", power)
        self.assertIn("lifecycle_lima.main_for_power", power)

    def test_provider_modules_never_enter_runtime_packages(self) -> None:
        forbidden = {
            "host.cli.aws_checks",
            "host.cli.aws_resources",
            "host.cli.lifecycle_aws",
            "host.cli.lifecycle_lima",
            "host.cli.power_aws",
        }
        for root_name in ("runtime", "apps", "tools"):
            root = ROOT / "host" / root_name
            if not root.exists():
                continue
            for path in root.rglob("*.py"):
                self.assertTrue(
                    _imports(path).isdisjoint(forbidden),
                    f"{path.relative_to(ROOT)} imports operator-side provider code",
                )

    def test_bootstrap_shell_consumes_only_the_two_device_contract(self) -> None:
        bootstrap = render._render_bootstrap()
        self.assertIn("python3 -m host.bootstrap.storage_resolver", bootstrap)
        self.assertNotIn("resolve_ebs_device", bootstrap)
        self.assertNotIn("resolve_lima_device", bootstrap)
        self.assertNotIn("storage.resolver_input", bootstrap)
        self.assertNotIn("/run/kern-provider/lima-disks.json", bootstrap)

    def test_guest_storage_dispatcher_has_a_fixed_adapter_registry(self) -> None:
        resolver = (ROOT / "host/bootstrap/storage_resolver.py").read_text()
        self.assertIn('"aws": storage_aws.resolve', resolver)
        self.assertIn('"lima": storage_lima.resolve', resolver)
        self.assertNotIn("importlib", resolver)
        self.assertNotIn("entry_points", resolver)


if __name__ == "__main__":
    unittest.main()
