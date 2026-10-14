"""Guard the initial URL and return an anonymous Browser capture as a file."""
from __future__ import annotations

from contextlib import contextmanager
import io
import re
from collections.abc import Iterator
from urllib.parse import urlsplit

from host.param_guard import ParamGuardDenied
from host.runtime.browser import client
from host.tools import ActionFailed, ActionResult, ActionSpec, HostAPI, JSONObject
from host.tools.results import OpenedStreamingAsset, StreamingAsset, StreamingAssetError
from host.tools.shared.inputs import guard_url_parameter_string, decoded_url_component_values
from host.tools.shared.web import is_public_https_url

SPEC = ActionSpec(
    id="screenshot_url",
    description="Render one public HTTPS page with JavaScript in a fresh anonymous browser and save a full-page PNG screenshot, subject to capture limits.",
    data_policy="Checks the initial URL with the parameter guard, then loads it through the configured Browser connection. Redirects and page resources load under existing Browser network restrictions without repeating the input content guard. No saved login is loaded or retained. Returns a screenshot file and bounded capture metadata without approval; page content is untrusted.",
    input_schema={"type": "object", "properties": {
        "url": {"type": "string", "description": "Public HTTPS URL, up to 200 ASCII characters. No credentials, IP literals or non-standard ports."},
    }, "required": ["url"], "additionalProperties": False},
    returns_asset=True,
)


@contextmanager
def _open(url: str) -> Iterator[OpenedStreamingAsset]:
    try:
        result = client.screenshot(url)
        summary = (f"Anonymous Browser screenshot: {result.width} × {result.height}. "
                   f"Clipped: {'yes' if result.clipped else 'no'}. Final URL: {result.url}. "
                   "Lazy-loaded content may be incomplete; login walls and challenges are captured as displayed.")
        with io.BytesIO(result.png) as source:
            yield OpenedStreamingAsset("browser-screenshot.png", "image/png", len(result.png), source, summary)
    except client.BrowserError as exc:
        raise StreamingAssetError(str(exc)) from exc


def execute(tool_input: JSONObject, api: HostAPI) -> ActionResult:
    url = tool_input.get("url")
    if (set(tool_input) != {"url"} or not isinstance(url, str)
            or not re.fullmatch(r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+", url)
            or not is_public_https_url(url, max_chars=200)):
        return ActionFailed("Screenshot requires one public HTTPS URL of up to 200 ASCII characters, without credentials, IP literals or non-standard ports.")
    try:
        url = guard_url_parameter_string(url, api)
        # Unlike a raw HTTP fetch, a browser exposes the fragment to page scripts.
        for decoded in decoded_url_component_values(urlsplit(url).fragment, plus=False):
            api.outbound.guard_request_parameter_string(decoded)
    except ParamGuardDenied as exc:
        return ActionFailed(str(exc))
    return StreamingAsset(lambda: _open(url))
