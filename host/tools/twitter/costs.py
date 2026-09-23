"""X pay-per-use rates, reviewed 2026-09-22.
https://docs.x.com/x-api/getting-started/pricing

Reports this host's resource usage at published rates. Provider-side discounts,
usage outside Kern and exceptions to X's daily deduplication are not observable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import re

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject


def record_response(api: HostAPI, response: JSONObject, kind: str, *, owned: bool = False) -> None:
    day = datetime.now(timezone.utc).date().isoformat()
    # The same OAuth app can have several connected accounts. Provider resource
    # deduplication spans those connections; never persist the client id itself.
    client_id = api.config.get("X_OAUTH_CLIENT_ID", "")
    if not client_id:
        return
    app = sha256(client_id.encode()).hexdigest()[:24]
    data = response.get("data")
    rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    resources = [(kind, row) for row in rows]
    includes = response.get("includes")
    if isinstance(includes, dict):
        for field, category in (("users", "user"), ("tweets", "post")):
            extra = includes.get(field)
            if isinstance(extra, list):
                resources.extend((category, row) for row in extra)
    if not resources:
        meta = response.get("meta")
        empty = isinstance(data, list) or isinstance(meta, dict) and meta.get("result_count") == 0
        if empty:
            api.costs.record("0")
        return
    for category, row in resources:
        resource_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(resource_id, str) or not 1 <= len(resource_id) <= 25 or not resource_id.isascii() or not resource_id.isdecimal():
            continue
        amount = "0.010" if category == "user" else "0.001" if owned else "0.005"
        api.costs.record(amount, charge_id=f"read:{app}:{day}:{category}:{resource_id}")


def owned_reads(api: HostAPI, user_id: str) -> bool:
    # Matching the authenticated user alone does not prove app ownership.
    owner = api.config.get("X_APP_OWNER_USER_ID", "")
    credential = api.credentials.load()
    return bool(owner and owner == user_id and credential and credential["account"]["id"] == owner)


def post_amount(proposal: JSONObject, response: JSONObject) -> str | None:
    text = str(proposal.get("text") or "")
    entities = response.get("entities")
    urls = entities.get("urls") if isinstance(entities, dict) else None
    if urls or re.search(r"(?i)https?://[^\s]+\.[^\s]+|www\.[^\s]+\.[^\s]+", text):
        return "0.200"
    # X can recognize bare/Unicode domains, and applies a special summoned
    # reply rate. Without provider confirmation those costs are not reported.
    if re.search(r"[\w-]+\.[\w-]+", text) or proposal.get("in_reply_to_tweet_id") or proposal.get("quote_tweet_id"):
        return None
    return "0.015"
