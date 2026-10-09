"""Local country lookup from Google's dated geo-target snapshot."""
from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from host.tools.json_types import JSONObject, JSONValue


def list_locations(values: JSONObject) -> JSONObject:
    snapshot = cast(JSONObject, json.loads(Path(__file__).with_name("countries.json").read_text(encoding="utf-8")))
    query = cast(str, values["query"]).casefold()
    country_code = values["country_code"]
    locations = cast(list[JSONObject], snapshot["rows"])
    matches = [row for row in locations
               if (not country_code or row["country_code"] == country_code)
               and (not query or query in cast(str, row["name"]).casefold())]
    limit = cast(int, values["limit"])
    return {
        "snapshot_date": snapshot["snapshot_date"],
        "source_url": snapshot["source_url"],
        "rows": cast(list[JSONValue], matches[:limit]),
        "truncated": len(matches) > limit,
    }
