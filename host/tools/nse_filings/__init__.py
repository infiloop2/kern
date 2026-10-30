"""Public NSE issuer discovery, filing indexes and source-linked downloads.

Only typed symbols and date windows reach fixed listing endpoints. Downloads
must occur in a freshly retrieved issuer index; response bytes and source
vintages are preserved, without claiming exhaustive exchange coverage.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
import re
from typing import cast
from urllib.parse import urlencode, urlsplit

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.results import ActionExecuted, ActionFailed, ActionResult, OpenedStreamingAsset, StreamingAsset, StreamingAssetError
from host.tools.tool import Tool
from . import client
from .manifest import MANIFEST

BASE = "https://www.nseindia.com/api/"
CATEGORIES = {"announcements", "annual_reports", "financial_results", "integrated_financials"}
QUERY_FIELDS = {"ticker", "category", "date_from", "date_to", "page"}
ATTACHMENTS = {
    "announcements": ("attchmntFile",), "annual_reports": ("fileName",),
    "financial_results": ("xbrl",), "integrated_financials": ("xbrl", "ixbrl", "pdf_attach"),
}


def ticker_value(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9&-]{0,19}", value):
        raise ValueError("NSE ticker must be 1–20 ASCII letters/digits/ampersands/hyphens, starting with a letter or digit.")
    return value.upper()


def document_value(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", value) or ".." in value:
        raise ValueError("Document must be an exact published ASCII basename, without a URL, path, query or percent encoding.")
    if value.rsplit(".", 1)[-1].lower() not in {"pdf", "xml", "html", "htm", "zip"}:
        raise ValueError("Only published PDF, XML, HTML and ZIP documents are supported.")
    return value


def query(value: JSONObject) -> tuple[str, str, str, int]:
    ticker = ticker_value(value.get("ticker"))
    category = value.get("category")
    if not isinstance(category, str) or category not in CATEGORIES:
        raise ValueError("Choose a documented NSE filing category.")
    params: dict[str, str | int] = {"index": "equities", "symbol": ticker}
    if category == "annual_reports":
        if set(value) & {"date_from", "date_to", "page"}:
            raise ValueError("Annual reports return the available archive; omit dates and page.")
    else:
        dates = []
        for field in ("date_from", "date_to"):
            raw = value.get(field)
            if not isinstance(raw, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
                raise ValueError("Both date_from and date_to must be real ISO dates (YYYY-MM-DD).")
            dates.append(date.fromisoformat(raw))
        if dates[0] > dates[1]:
            raise ValueError("date_from must not be after date_to.")
        params.update(from_date=dates[0].strftime("%d-%m-%Y"), to_date=dates[1].strftime("%d-%m-%Y"))
    page = value.get("page", 1)
    if type(page) is not int or not 1 <= page <= 100000:
        raise ValueError("page must be an integer from 1 to 100000.")
    if "page" in value and category != "integrated_financials":
        raise ValueError("Only integrated_financials supports page; other dated indexes use date windows.")
    if category == "financial_results":
        params["period"] = "quarterly"
    elif category == "integrated_financials":
        params.update(type="Integrated Filing- Financials", page=page, size=20)
    route = {"announcements": "corporate-announcements", "annual_reports": "annual-reports",
             "financial_results": "corporates-financial-results", "integrated_financials": "integrated-filing-results"}[category]
    return ticker, category, BASE + route + "?" + urlencode(params), page


def lookup(ticker: str) -> ActionExecuted:
    url = BASE + "NextApi/globalSearch/equity?" + urlencode({"symbol": ticker})
    with client.source(url, client.JSON_LIMIT) as (saved, _, _, retrieved, _):
        obj = client.parse(saved)
        if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
            raise StreamingAssetError("NSE returned an invalid company directory.")
        matches: list[JSONValue] = []
        for row in obj["data"]:
            if not isinstance(row, dict) or not isinstance(row.get("symbol"), str):
                raise StreamingAssetError("NSE returned an invalid company record.")
            if row["symbol"] != ticker or row.get("segment") != "in equity":
                continue
            if not isinstance(row.get("companyName"), str) or not row["companyName"].strip() or not isinstance(row.get("series"), str):
                raise StreamingAssetError("NSE returned an invalid matched issuer.")
            matches.append({"ticker": ticker, "name": row["companyName"], "series": row["series"]})
            if len(matches) > 20:
                raise StreamingAssetError("NSE returned too many exact issuer matches.")
        return ActionExecuted({"ticker": ticker, "matches": matches, "source_url": url, "retrieved_at": retrieved})


def rows_for(obj, ticker: str, category: str, page: int, names: set[str]) -> tuple[list[dict], str]:
    if category in {"annual_reports", "integrated_financials"}:
        if not isinstance(obj, dict):
            raise StreamingAssetError("NSE returned an invalid filing envelope; expected an object with data.")
        rows = obj.get("data")
    else:
        rows = obj
    if not isinstance(rows, list):
        raise StreamingAssetError("NSE returned an unexpected filing index, not a verified empty history.")
    for row in rows:
        if not isinstance(row, dict):
            raise StreamingAssetError("NSE returned an invalid filing record.")
        if category == "annual_reports":
            name = row.get("companyName")
            if not isinstance(name, str) or " ".join(name.casefold().split()) not in names:
                raise StreamingAssetError("Annual-report issuer does not match the current NSE company directory.")
        elif row.get("symbol") != ticker:
            raise StreamingAssetError("NSE index contains a different issuer; no source was accepted.")
    info = f"Rows in this response: {len(rows)}\n"
    if category == "integrated_financials":
        total = obj.get("totalCount")
        if (type(total) is not int or total < 0 or type(obj.get("page")) is not int
                or obj["page"] != page - 1 or type(obj.get("size")) is not int or obj["size"] != 20
                or len(rows) != min(20, max(0, total - (page - 1) * 20))):
            raise StreamingAssetError("NSE returned inconsistent pagination; no complete page was accepted.")
        next_page = str(page + 1) if page * 20 < total else "none"
        info += f"Requested page (one-based): {page}\nReported total: {total}\nNext page: {next_page}\n"
    else:
        info += "No pagination total is supplied for this index. Check narrower date windows for historical coverage.\n"
    return rows, info


def attachment_urls(rows: list[dict], category: str, name: str) -> set[str]:
    urls = set()
    for row in rows:
        for field in ATTACHMENTS[category]:
            value = row.get(field)
            if not isinstance(value, str):
                continue
            # Source-provided URLs are untrusted. Match only canonical archive
            # paths with the exact safe basename; never follow a changed host.
            if not re.fullmatch(r"https://nsearchives\.nseindia\.com/(?:corporate|annual_reports)/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", value):
                continue
            parts = urlsplit(value)
            if ".." not in parts.path and parts.path.rsplit("/", 1)[-1] == name:
                urls.add(value)
    return urls


@contextmanager
def filing_asset(ticker: str, category: str, url: str, page: int, document: str | None) -> Iterator[OpenedStreamingAsset]:
    # lookup constructs this closed shape after validating the source fields.
    matches = cast(list[dict[str, str]], lookup(ticker).result["matches"])
    if not matches:
        raise StreamingAssetError("No current NSE equity issuer matches this ticker; filing coverage is unknown.")
    names = {" ".join(row["name"].casefold().split()) for row in matches}
    with client.source(url, client.JSON_LIMIT) as (saved, size, sha, retrieved, mime):
        rows, info = rows_for(client.parse(saved), ticker, category, page, names)
        if document is None:
            yield OpenedStreamingAsset(filename=f"nse-{ticker}-{category}-{page}.json", media_type="application/json",
                size_bytes=size, source=saved, summary=info + client.provenance(url, size, sha, retrieved, mime))
            return
        urls = attachment_urls(rows, category, document)
        if len(urls) != 1:
            raise StreamingAssetError("Document is absent, ambiguous or unsupported in this issuer's requested index page. Use an exact published archive basename from list_filings.")
    with client.document(urls.pop(), document) as opened:
        yield opened


class NseFilingsTool(Tool):
    @property
    def manifest(self):
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action == "lookup_company":
                if set(tool_input) != {"ticker"}:
                    raise ValueError("lookup_company requires only ticker.")
                return lookup(ticker_value(tool_input["ticker"]))
            if action not in {"list_filings", "download_document"}:
                raise ValueError("Unsupported NSE filings action.")
            allowed = QUERY_FIELDS | ({"document"} if action == "download_document" else set())
            if set(tool_input) - allowed:
                raise ValueError("NSE action has unsupported input fields.")
            ticker, category, url, page = query(tool_input)
            document = document_value(tool_input.get("document")) if action == "download_document" else None
            return StreamingAsset(lambda: filing_asset(ticker, category, url, page, document))
        except (ValueError, StreamingAssetError) as exc:
            return ActionFailed(str(exc))


BUNDLED_TOOL = NseFilingsTool()
