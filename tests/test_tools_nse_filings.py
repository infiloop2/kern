"""NSE identity, source pagination and document membership boundaries.

Fixtures preserve the shapes observed in public NSE responses. These checks
exercise real action execution and HTTP plumbing without remote requests;
live research is separately recorded and does not certify exhaustive history.
"""
import hashlib
import codecs
import http.client
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from host.tools import nse_filings as nse
from host.tools.nse_filings import client
from host.tools.results import ActionFailed, StreamingAsset, StreamingAssetError
from host.tools.shared import web
from test_tools import FakeHostAPI, assert_matches_output_schema


LOOKUP = {"data": [{"symbol": "INFY", "companyName": "Infosys Limited", "segment": "in equity", "series": "EQ"}]}
WINDOW = {"ticker": "INFY", "category": "announcements", "date_from": "2025-01-01", "date_to": "2025-01-31"}
ARCHIVE = "https://nsearchives.nseindia.com/corporate/"


class Response(io.BytesIO):
    def __init__(self, value, *, status=200, headers=None):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}


class NseFilingsTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI()
        self.open = self.enterContext(patch.object(web._OPENER, "open"))
        self.enterContext(patch.object(client, "pace"))

    def execute(self, action, value):
        return nse.BUNDLED_TOOL.execute(action, value, self.api)

    def consume(self, action, value):
        result = self.execute(action, value)
        self.assertIsInstance(result, StreamingAsset)
        with result.open_stream() as opened:
            return opened, opened.source.read()

    def responses(self, *values):
        self.open.reset_mock()
        self.open.side_effect = [v if isinstance(v, Response) else Response(v) for v in values]

    def test_lookup_filters_exact_equity_and_does_not_return_fake_prices(self):
        self.responses({"data": LOOKUP["data"] + [dict(LOOKUP["data"][0], symbol="INFYOTHER"),
                       dict(LOOKUP["data"][0], segment="debt")]})
        result = self.execute("lookup_company", {"ticker": "infy"})
        assert_matches_output_schema(self, nse.MANIFEST, "lookup_company", result)
        self.assertEqual(result.result["matches"], [{"ticker": "INFY", "name": "Infosys Limited", "series": "EQ"}])
        req = self.open.call_args.args[0]
        self.assertEqual(req.full_url, nse.BASE + "NextApi/globalSearch/equity?symbol=INFY")
        self.assertEqual(dict(req.header_items()), {"User-agent": "KernFinancialResearch/1.0", "Accept-encoding": "identity"})
        self.assertEqual(self.api.approvals.records, {})

    def test_special_symbol_is_encoded_and_unknown_lookup_is_empty(self):
        self.responses({"data": []})
        result = self.execute("lookup_company", {"ticker": "m&m"})
        self.assertEqual(result.result["matches"], [])
        self.assertTrue(self.open.call_args.args[0].full_url.endswith("symbol=M%26M"))
        self.responses({"data": []})
        with self.assertRaisesRegex(StreamingAssetError, "No current"):
            self.consume("list_filings", WINDOW)
        self.open.assert_called_once()

    def test_invalid_inputs_never_leave_host(self):
        for ticker in [None, True, "", "INFY\n", "a" * 21, "https://evil", "INFY?token=x", "%49NFY", "इन्फी"]:
            self.assertIsInstance(self.execute("lookup_company", {"ticker": ticker}), ActionFailed)
        for change in [{"category": "other"}, {"date_from": "2025-02-30"}, {"date_to": "2024-01-01"},
                       {"date_to": None}, {"page": 1}, {"url": "https://evil"}]:
            self.assertIsInstance(self.execute("list_filings", {**WINDOW, **change}), ActionFailed)
        for page in [True, 0, -1, 100001, "1", 1.5]:
            self.assertIsInstance(self.execute("list_filings", {**WINDOW, "category": "integrated_financials", "page": page}), ActionFailed)
        for name in ["../a.xml", "a%2exml", "a.xml?x=1", "https://evil/a.xml", "a\\b.xml", "a.txt", "a\n.xml"]:
            self.assertIsInstance(self.execute("download_document", {**WINDOW, "document": name}), ActionFailed)
        self.assertIsInstance(self.execute("list_filings", {"ticker": "INFY", "category": "announcements"}), ActionFailed)
        self.assertIsInstance(self.execute("other", {}), ActionFailed)
        self.open.assert_not_called()

    def test_source_bytes_and_numbers_are_preserved(self):
        raw = b'[{"symbol":"INFY","value":1234567890.12345678901234567890}]'
        self.responses(LOOKUP, raw)
        asset, body = self.consume("list_filings", WINDOW)
        self.assertEqual(body, raw)
        self.assertIn(hashlib.sha256(raw).hexdigest(), asset.summary)
        self.assertIn("Rows in this response: 1", asset.summary)
        self.assertIn("No pagination total", asset.summary)
        self.assertEqual(asset.size_bytes, len(raw))
        self.assertIn("from_date=01-01-2025", self.open.call_args.args[0].full_url)
        self.assertTrue(asset.source.closed)

    def test_annual_archive_identity_and_input_contract(self):
        self.responses(LOOKUP, {"data": [{"companyName": "Infosys Limited", "fromYr": "2025", "toYr": "2026"}]})
        self.consume("list_filings", {"ticker": "INFY", "category": "annual_reports"})
        self.assertEqual(self.open.call_args.args[0].full_url, nse.BASE + "annual-reports?index=equities&symbol=INFY")
        self.responses(LOOKUP, {"data": [{"companyName": "Other Company"}]})
        with self.assertRaisesRegex(StreamingAssetError, "issuer"):
            self.consume("list_filings", {"ticker": "INFY", "category": "annual_reports"})
        self.open.reset_mock()
        self.assertIsInstance(self.execute("list_filings", {**WINDOW, "category": "annual_reports"}), ActionFailed)
        self.open.assert_not_called()

    def test_integrated_pagination_is_one_based_input_zero_based_source(self):
        for page, count, expected_next in [(1, 20, "2"), (2, 1, "none"), (3, 0, "none")]:
            self.responses(LOOKUP, {"data": [{"symbol": "INFY", "seq_Id": str(i)} for i in range(count)], "size": 20, "page": page - 1, "totalCount": 21})
            asset, _ = self.consume("list_filings", {**WINDOW, "category": "integrated_financials", "page": page})
            self.assertIn("Next page: " + expected_next, asset.summary)
            self.assertIn(f"page={page}&size=20", self.open.call_args.args[0].full_url)
        base = {"data": [], "size": 20, "page": 0, "totalCount": 0}
        for change in [{"totalCount": 1}, {"totalCount": True}, {"page": 1}, {"size": 10}, {"data": {}}]:
            self.responses(LOOKUP, {**base, **change})
            with self.assertRaises(StreamingAssetError):
                self.consume("list_filings", {**WINDOW, "category": "integrated_financials"})

    def test_wrong_issuer_or_wrong_index_shape_is_not_empty_success(self):
        for value in [{}, [{"symbol": "TCS"}], [None], b'<html>challenge</html>', b'[{"symbol":"INFY","v":NaN}]']:
            self.responses(LOOKUP, value)
            with self.assertRaises(StreamingAssetError):
                self.consume("list_filings", WINDOW)
        self.responses(LOOKUP, [])
        asset, _ = self.consume("list_filings", WINDOW)
        self.assertIn("Rows in this response: 0", asset.summary)

    def test_object_categories_reject_bare_arrays_before_processing_rows(self):
        for category in ("annual_reports", "integrated_financials"):
            args = {"ticker": "INFY", "category": category} if category == "annual_reports" else {**WINDOW, "category": category}
            for obj in ([], [{"companyName": "Infosys Limited", "symbol": "INFY"}]):
                self.responses(LOOKUP, obj)
                with self.assertRaisesRegex(StreamingAssetError, "envelope"):
                    self.consume("list_filings", args)

    def test_xml_bom_encodings_keep_original_bytes_and_reject_html(self):
        variants = [(codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"),
                    (codecs.BOM_UTF16_BE, "utf-16-be"), (codecs.BOM_UTF32_LE, "utf-32-le"),
                    (codecs.BOM_UTF32_BE, "utf-32-be")]
        for bom, encoding in variants:
            body = bom + '<?xml version="1.0"?><xbrl>₹</xbrl>'.encode(encoding)
            self.responses(body)
            with client.document(ARCHIVE + "report.xml", "report.xml") as asset:
                self.assertEqual(asset.source.read(), body)
                self.assertIn(hashlib.sha256(body).hexdigest(), asset.summary)
            self.responses(bom + '<!DOCTYPE html><html>challenge</html>'.encode(encoding))
            with self.assertRaisesRegex(StreamingAssetError, "unexpected body"):
                with client.document(ARCHIVE + "report.xml", "report.xml"):
                    self.fail("BOM must not hide HTML error content")

    def test_legacy_financial_and_all_document_families(self):
        fixtures = [
            ("announcements", [{"symbol": "INFY", "attchmntFile": ARCHIVE + "report.pdf"}], "report.pdf", b"%PDF-1.7\n"),
            ("financial_results", [{"symbol": "INFY", "xbrl": ARCHIVE + "xbrl/report.xml"}], "report.xml", b"<?xml version='1.0'?><xbrl/>"),
            ("annual_reports", {"data": [{"companyName": "Infosys Limited", "fileName": "https://nsearchives.nseindia.com/annual_reports/annual.pdf"}]}, "annual.pdf", b"%PDF-1.7\n"),
            ("integrated_financials", {"data": [{"symbol": "INFY", "ixbrl": ARCHIVE + "ixbrl/report.html", "pdf_attach": ARCHIVE + "null"}], "size": 20, "page": 0, "totalCount": 1}, "report.html", b"<html><body>Source iXBRL</body></html>"),
        ]
        for category, index, name, data in fixtures:
            self.responses(LOOKUP, index, data)
            args = {"ticker": "INFY", "category": category} if category == "annual_reports" else {**WINDOW, "category": category}
            opened, body = self.consume("download_document", {**args, "document": name})
            self.assertEqual(body, data)
            self.assertEqual(opened.filename, name)
            self.assertEqual(self.open.call_count, 3)
            if category == "financial_results":
                self.assertIn("period=quarterly", self.open.call_args_list[1].args[0].full_url)

    def test_document_membership_rejects_forged_hosts_paths_and_ambiguity(self):
        for links in [[], ["https://evil.example/report.pdf"], [ARCHIVE + "../report.pdf"],
                      [ARCHIVE + "report.pdf?secret=x"], ["https://nsearchives.nseindia.com@evil.example/corporate/report.pdf"],
                      [ARCHIVE + "a/report.pdf", ARCHIVE + "b/report.pdf"], [ARCHIVE + "%72eport.pdf"]]:
            self.responses(LOOKUP, [{"symbol": "INFY", "attchmntFile": x} for x in links])
            with self.assertRaisesRegex(StreamingAssetError, "absent, ambiguous or unsupported"):
                self.consume("download_document", {**WINDOW, "document": "report.pdf"})
            self.assertEqual(self.open.call_count, 2)

    def test_transport_rejects_redirect_errors_encoding_and_incomplete_bytes(self):
        for status in [301, 403, 429, 503]:
            self.open.reset_mock()
            self.open.side_effect = urllib.error.HTTPError("https://www.nseindia.com/", status, "private", {}, io.BytesIO(b"private"))
            result = self.execute("lookup_company", {"ticker": "INFY"})
            self.assertIsInstance(result, ActionFailed)
            self.assertIn(str(status), result.error)
            self.assertNotIn("private", result.error)
            self.open.assert_called_once()
        for response in [Response(b"abc", status=202), Response(b"abc", headers={"Content-Length": "9"}),
                         Response(b"abc", headers={"Content-Encoding": "gzip"}), Response(b"abc", headers={"Content-Length": "-1"}), Response(b"")]:
            self.responses(response)
            with self.assertRaises(StreamingAssetError):
                with client.source(nse.BASE, 100):
                    self.fail("Must not return incomplete content")

    def test_limit_disconnect_and_document_format_validation(self):
        self.responses(b"abcd")
        with self.assertRaisesRegex(StreamingAssetError, "byte limit"):
            with client.source(nse.BASE, 3):
                self.fail("Must not return oversized content")
        bad = Response(b"x")
        bad.read = lambda size: (_ for _ in ()).throw(http.client.IncompleteRead(b"private"))
        self.responses(bad)
        with self.assertRaises(StreamingAssetError):
            with client.source(nse.BASE, 10):
                self.fail("Must not return disconnected content")
        for name, body in [("report.pdf", b"<html>error</html>"), ("report.xml", b"<!DOCTYPE html><html>error"), ("report.zip", b"error")]:
            self.responses(body)
            with self.assertRaises(StreamingAssetError):
                with client.document(ARCHIVE + name, name):
                    self.fail("Must not return format mismatch")

    def test_stage_check_exercises_free_lookup(self):
        from tests.stage.stage_tool_checks import StageToolChecks
        probe = StageToolChecks()
        with patch.object(probe, "_successful_tool_call", return_value={"matches": []}) as call:
            self.assertEqual(probe._check_tool_provider("nse_filings"), "1 live read(s) completed")
        call.assert_called_once_with("nse_filings_lookup_company", {"ticker": "INFY"})
