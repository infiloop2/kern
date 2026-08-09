"""Python packages managed integration: static contract.

Read-only access to PyPI metadata and package downloads. No guard: the
GET/HEAD-only, path-guarded rules are the whole control.
"""

from __future__ import annotations

from host.network_integrations.base import IntegrationManifest, simple_integration_parser

MANIFEST = IntegrationManifest(
    integration_id="python_packages",
    display_name="Python packages",
    description=(
        "Read-only access to the PyPI simple index, package metadata, and package "
        "downloads (pip install)."
    ),
    owned_apexes=("pypi.org", "pythonhosted.org"),
)

parse = simple_integration_parser("network_integrations.python_packages")
