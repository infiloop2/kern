"""Python packages managed integration: static contract.

Read-only access to PyPI metadata and package downloads.
Project and release JSON routes support installation and advisory auditing.
The guard restricts methods and paths and checks metadata parameters.
"""

from __future__ import annotations

from host.network_integrations.base import IntegrationManifest, simple_integration_parser

MANIFEST = IntegrationManifest(
    integration_id="python_packages",
    display_name="Python packages",
    description=(
        "Read-only access to the PyPI simple index, project and release metadata, "
        "and package downloads (pip install and pip-audit)."
    ),
    owned_apexes=("pypi.org", "pythonhosted.org"),
)

parse = simple_integration_parser("network_integrations.python_packages")
