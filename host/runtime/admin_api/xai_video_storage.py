"""Operator-only storage configuration for Grok video output."""
from __future__ import annotations

import re
from typing import Any

from host.runtime.core import state


def replace(body: Any) -> dict[str, Any]:
    fields = {"bucket", "region", "access_key_id", "secret_access_key"}
    if not isinstance(body, dict) or set(body) != fields:
        raise ValueError("Provide bucket, region, access_key_id and secret_access_key together")
    if any(not isinstance(body[k], str) or not body[k].strip() for k in fields):
        raise ValueError("All video storage fields are required")
    value = {k: v.strip() for k, v in body.items()}
    # Commercial AWS S3, with a derived endpoint and a fixed object prefix.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", value["bucket"]):
        raise ValueError("Use an S3 bucket name with 3-63 lowercase letters, digits or hyphens")
    if not re.fullmatch(r"(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:north|south|east|west|central|northeast|northwest|southeast|southwest)-[1-9]", value["region"]):
        raise ValueError("Use the bucket's commercial AWS region, for example us-east-1")
    if not re.fullmatch(r"AKIA[A-Z0-9]{16}", value["access_key_id"]):
        raise ValueError("Use a long-term IAM access key id beginning AKIA")
    if not re.fullmatch(r"[A-Za-z0-9/+=]{40}", value["secret_access_key"]):
        raise ValueError("Use the 40-character IAM secret access key")
    state.save_xai_video_storage(value)
    return state.xai_video_storage_metadata()


def delete() -> dict[str, Any]:
    state.save_xai_video_storage(None)
    return {"configured": False}
