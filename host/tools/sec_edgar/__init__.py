"""Public EDGAR data, using fixed destinations and published document membership."""
from __future__ import annotations

from contextlib import contextmanager
import re
from collections.abc import Iterator

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, OpenedStreamingAsset, StreamingAsset, StreamingAssetError
from host.tools.tool import Tool
from . import client
from .manifest import MANIFEST

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
_FIELDS = {
    "lookup_company": ({"ticker"}, {"ticker"}),
    "get_submissions": ({"cik", "history_file"}, {"cik"}),
    "get_company_facts": ({"cik"}, {"cik"}),
    "list_filing_documents": ({"cik", "accession"}, {"cik", "accession"}),
    "download_document": ({"cik", "accession", "document"}, {"cik", "accession", "document"}),
}


def cik_value(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value) or int(value) == 0:
        raise ValueError("SEC cik must be a positive 1–10 digit ASCII string.")
    return value.zfill(10)


def accession_value(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", value):
        raise ValueError("SEC accession must use the exact 10-2-6 digit hyphenated format.")
    return value


def document_value(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", value) or ".." in value:
        raise ValueError("SEC document must be a published ASCII basename, without a path, URL or encoding.")
    return value


def lookup(ticker: str, agent: str) -> ActionExecuted:
    with client.source(TICKERS_URL, agent, client.INDEX_LIMIT) as (saved, _, _, retrieved, _):
        obj = client.parse_object(saved)
        fields, rows = obj.get("fields"), obj.get("data")
        required = {"cik", "name", "ticker", "exchange"}
        if (not isinstance(fields, list) or not all(isinstance(x, str) for x in fields)
                or len(set(fields)) != len(fields) or not required.issubset(fields) or not isinstance(rows, list)):
            raise StreamingAssetError("SEC returned an invalid ticker directory.")
        matches: list[JSONValue] = []
        for row in rows:
            if not isinstance(row, list) or len(row) != len(fields):
                raise StreamingAssetError("SEC returned an invalid ticker-directory row.")
            record = dict(zip(fields, row))
            if record["ticker"] != ticker:
                continue
            if (not isinstance(record["name"], str)
                    or not (record["exchange"] is None or isinstance(record["exchange"], str))):
                raise StreamingAssetError("SEC returned an invalid issuer record.")
            matches.append({"cik": cik_value(str(record["cik"])), "name": record["name"],
                            "ticker": ticker, "exchange": record["exchange"]})
            if len(matches) > 20:
                raise StreamingAssetError("SEC returned too many exact ticker matches.")
        return ActionExecuted({"ticker": ticker, "matches": matches, "source_url": TICKERS_URL, "retrieved_at": retrieved})


@contextmanager
def document_asset(root: str, document: str, agent: str) -> Iterator[OpenedStreamingAsset]:
    with client.source(root + "index.json", agent, client.INDEX_LIMIT) as (saved, _, _, _, _):
        published = client.names(client.parse_object(saved))
    if document not in published:
        raise StreamingAssetError("Document is not published in this SEC filing directory. Use list_filing_documents for its exact basename.")
    with client.asset(root + document, agent, client.DOCUMENT_LIMIT, document) as opened:
        yield opened


class SecEdgarTool(Tool):
    @property
    def manifest(self):
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action not in _FIELDS:
                raise ValueError("Unsupported SEC EDGAR action.")
            allowed, required = _FIELDS[action]
            if set(tool_input) - allowed or not required.issubset(tool_input):
                raise ValueError("SEC EDGAR action has missing or unsupported input fields.")
            # Every outbound identifier is grammar-validated; document names also
            # require a match in an independently retrieved fixed SEC directory.
            if action == "lookup_company":
                ticker = tool_input["ticker"]
                if not isinstance(ticker, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9.-]{0,15}", ticker):
                    raise ValueError("SEC ticker must use the documented 1–16 character ASCII ticker format.")
                return lookup(ticker.upper(), client.contact_header(api.config.get("SEC_CONTACT_EMAIL", "")))
            cik = cik_value(tool_input["cik"])
            accession = accession_value(tool_input["accession"]) if "accession" in tool_input else ""
            document = document_value(tool_input["document"]) if "document" in tool_input else ""
            history = tool_input.get("history_file")
            if "history_file" in tool_input and (not isinstance(history, str) or not re.fullmatch(r"CIK" + cik + r"-submissions-[0-9]{3,6}\.json", history)):
                raise ValueError("SEC history_file must be a referenced historical filename belonging to this CIK.")
            agent = client.contact_header(api.config.get("SEC_CONTACT_EMAIL", ""))
            if action == "get_submissions":
                name = str(history) if history else "CIK" + cik + ".json"
                return StreamingAsset(lambda: client.asset("https://data.sec.gov/submissions/" + name, agent,
                    client.JSON_LIMIT, "sec-" + name, json_kind="history" if history else "submissions", cik=cik))
            if action == "get_company_facts":
                return StreamingAsset(lambda: client.asset("https://data.sec.gov/api/xbrl/companyfacts/CIK" + cik + ".json", agent,
                    client.JSON_LIMIT, "sec-companyfacts-" + cik + ".json", json_kind="companyfacts", cik=cik))
            root = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/"
            if action == "list_filing_documents":
                return StreamingAsset(lambda: client.asset(root + "index.json", agent, client.INDEX_LIMIT,
                    "sec-index-" + accession + ".json", json_kind="index"))
            return StreamingAsset(lambda: document_asset(root, document, agent))
        except (ValueError, StreamingAssetError) as exc:
            return ActionFailed(str(exc))


BUNDLED_TOOL = SecEdgarTool()
