"""npm packages managed integration: static contract.

Public registry reads, Node.js downloads and the exact bulk advisory lookup.
The audit POST accepts only bounded package/version JSON, including npm gzip.
Publishing and other registry writes remain denied by the request guard.
"""

from __future__ import annotations

from host.network_integrations.base import IntegrationManifest, simple_integration_parser

MANIFEST = IntegrationManifest(
    integration_id="npm_packages",
    display_name="npm packages",
    description=(
        "Public npm package reads, bounded bulk vulnerability lookups (npm audit), "
        "and Node.js release downloads. Publishing and other writes are denied."
    ),
    owned_apexes=("npmjs.org", "nodejs.org"),
)

parse = simple_integration_parser("network_integrations.npm_packages")
