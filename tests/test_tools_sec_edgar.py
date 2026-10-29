"""SEC protocol, source completeness and outbound identifier boundaries."""
import hashlib
import http.client
import io
import json
import threading
import unittest
import urllib.error
from unittest.mock import patch

from host.runtime.tools import tools_host
from host.tools import sec_edgar as sec
from host.tools.sec_edgar import client
from host.tools.results import ActionExecuted, ActionFailed, StreamingAsset, StreamingAssetError
from host.tools.shared import web
from test_tools import FakeHostAPI, assert_matches_output_schema


CONTACT = "research@example.com"
CIK = "0000320193"
ACCESSION = "0000320193-25-000079"
ROOT = "https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/"


class Response(io.BytesIO):
    def __init__(self, body, *, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {"Content-Type": "application/json"}


class SecEdgarTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI(config={"SEC_CONTACT_EMAIL": CONTACT})
        self.open = self.enterContext(patch.object(web._OPENER, "open"))
        self.enterContext(patch.object(client, "pace"))

    def execute(self, action, value, api=None):
        return sec.BUNDLED_TOOL.execute(action, value, api or self.api)

    def consume(self, action, value):
        result = self.execute(action, value)
        self.assertIsInstance(result, StreamingAsset)
        with result.open_stream() as asset:
            return asset, asset.source.read()

    def reply(self, value):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.open.side_effect = None
        self.open.return_value = Response(body)

    def test_lookup_filters_locally_and_uses_declared_contact(self):
        self.reply({"fields": ["cik", "name", "ticker", "exchange"], "data": [
            [320193, "Apple Inc.", "AAPL", "Nasdaq"], [1, "Other", "OTHER", None]]})
        result = self.execute("lookup_company", {"ticker": "aapl"})
        assert_matches_output_schema(self, sec.MANIFEST, "lookup_company", result)
        self.assertEqual(result.result["matches"], [{"cik": CIK, "name": "Apple Inc.", "ticker": "AAPL", "exchange": "Nasdaq"}])
        request = self.open.call_args.args[0]
        self.assertEqual(request.full_url, sec.TICKERS_URL)
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(dict(request.header_items()), {"User-agent": "KernEDGAR/1.0 " + CONTACT, "Accept-encoding": "identity"})
        self.assertNotIn(CONTACT, json.dumps(result.result))
        self.assertEqual(self.api.approvals.records, {})

    def test_unknown_ticker_is_empty_and_bad_directory_fails(self):
        self.reply({"fields": ["cik", "name", "ticker", "exchange"], "data": []})
        self.assertEqual(self.execute("lookup_company", {"ticker": "NONE"}).result["matches"], [])
        for value in ({}, {"fields": ["ticker", "ticker"], "data": []}, {"fields": ["cik", "name", "ticker", "exchange"], "data": [[]]}):
            self.reply(value)
            self.assertIsInstance(self.execute("lookup_company", {"ticker": "AAPL"}), ActionFailed)

    def test_invalid_identifiers_and_headers_never_make_requests(self):
        invalid = [None, 1, True, [], "", "0", "-1", "１２", "1/2", "1%2f2", "1?x=1", "1\n", "1" * 11]
        for cik in invalid:
            self.assertIsInstance(self.execute("get_company_facts", {"cik": cik}), ActionFailed)
        for name in ["../a", "https://example.com", "xsl/a.xml", "a%2exml", "a?x=1", "a\\b", "a\n", "a" * 256]:
            self.assertIsInstance(self.execute("download_document", {"cik": CIK, "accession": ACCESSION, "document": name}), ActionFailed)
        for contact in ["", "name", "a@example.com\r\nCookie: private", "a..b@example.com"]:
            self.assertIsInstance(self.execute("get_submissions", {"cik": CIK}, FakeHostAPI(config={"SEC_CONTACT_EMAIL": contact})), ActionFailed)
        for value in ({"cik": CIK, "url": "https://example.com"}, {}, {"cik": CIK, "history_file": "CIK0000000001-submissions-001.json"}):
            self.assertIsInstance(self.execute("get_submissions", value), ActionFailed)
        self.assertIsInstance(self.execute("download_document", {"cik": CIK, "accession": "123", "document": "a.htm"}), ActionFailed)
        self.open.assert_not_called()

    def test_json_asset_preserves_exact_numbers_and_provenance(self):
        body = b'{"cik":320193,"facts":{"value":1234567890.1234567890123456789}}'
        self.reply(body)
        asset, saved = self.consume("get_company_facts", {"cik": "320193"})
        self.assertEqual(saved, body)
        self.assertEqual(asset.size_bytes, len(body))
        self.assertIn(hashlib.sha256(body).hexdigest(), asset.summary)
        self.assertIn("Complete: true", asset.summary)
        self.assertNotIn(CONTACT, asset.summary)
        self.assertEqual(self.open.call_args.args[0].full_url, "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json")
        self.assertTrue(asset.source.closed)

    def test_current_and_history_submissions_use_bound_paths(self):
        self.reply({"cik": "320193", "filings": {}})
        self.consume("get_submissions", {"cik": CIK})
        self.assertEqual(self.open.call_args.args[0].full_url, "https://data.sec.gov/submissions/CIK0000320193.json")
        self.reply({"accessionNumber": [ACCESSION]})
        self.consume("get_submissions", {"cik": CIK, "history_file": "CIK0000320193-submissions-001.json"})
        self.assertEqual(self.open.call_args.args[0].full_url, "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json")

    def test_directory_verified_numeric_document_name_and_pdf(self):
        name = "wf-form4_163709822033468.xml"
        index = {"directory": {"item": [{"name": name}]}}
        self.open.side_effect = [Response(json.dumps(index).encode()), Response(b"<ownershipDocument/>", headers={"Content-Type": "application/xml"})]
        asset, body = self.consume("download_document", {"cik": CIK, "accession": ACCESSION, "document": name})
        self.assertEqual(body, b"<ownershipDocument/>")
        self.assertEqual(asset.filename, name)
        self.assertEqual([call.args[0].full_url for call in self.open.call_args_list], [ROOT + "index.json", ROOT + name])
        self.open.reset_mock()
        self.reply(index)
        with self.assertRaisesRegex(StreamingAssetError, "not published"):
            self.consume("download_document", {"cik": CIK, "accession": ACCESSION, "document": "private-secret.txt"})
        self.open.assert_called_once()  # Unknown basename never leaves the host.
        self.open.side_effect = [Response(b'{"directory":{"item":[{"name":"report.pdf"}]}}'), Response(b"%PDF-1.7\n", headers={"Content-Type": "application/pdf"})]
        asset, _ = self.consume("download_document", {"cik": CIK, "accession": ACCESSION, "document": "report.pdf"})
        self.assertEqual(asset.media_type, "application/pdf")

    def test_bad_json_identity_or_index_never_returns_asset(self):
        for body in [b'{"cik":1}', b'{"cik":true}', b'{"cik":320193,"v":NaN}', b'{"cik":', b'[]']:
            self.reply(body)
            with self.assertRaises(StreamingAssetError):
                self.consume("get_company_facts", {"cik": CIK})
        self.reply({"directory": {"item": "bad"}})
        with self.assertRaises(StreamingAssetError):
            self.consume("list_filing_documents", {"cik": CIK, "accession": ACCESSION})

    def test_complete_large_source_without_length_and_hard_size_limit(self):
        body = b"x" * (4 * 1024 * 1024 + 1)
        self.reply(body)
        with client.source(ROOT + "report.htm", "test", len(body)) as (saved, size, sha, _, _):
            self.assertEqual(saved.read(), body)
            self.assertEqual(size, len(body))
            self.assertEqual(sha, hashlib.sha256(body).hexdigest())
        self.reply(body)
        with self.assertRaisesRegex(StreamingAssetError, "size limit"):
            with client.source(ROOT + "report.htm", "test", 1024):
                self.fail("Oversized source must not be yielded")

    def test_incomplete_encoded_challenge_and_invalid_length_fail_closed(self):
        cases = [(200, {"Content-Length": "5"}), (200, {"Content-Length": "-1"}),
                 (200, {"Content-Length": "999"}), (200, {"Content-Encoding": "gzip"}), (202, {})]
        for status, headers in cases:
            with self.subTest(status=status, headers=headers):
                self.open.return_value = Response(b"abc", status=status, headers=headers)
                with self.assertRaises(StreamingAssetError):
                    with client.source(ROOT + "report.htm", "test", 100):
                        self.fail("Incomplete source must not be yielded")
        self.reply(b"")
        with self.assertRaises(StreamingAssetError):
            with client.source(ROOT + "report.htm", "test", 100):
                self.fail("Empty source must not be yielded")

    def test_provider_errors_and_redirects_are_redacted_without_retry(self):
        for status in [302, 403, 404, 429, 503]:
            self.open.reset_mock()
            self.open.side_effect = urllib.error.HTTPError(ROOT, status, "private", {}, io.BytesIO(CONTACT.encode()))
            result = self.execute("lookup_company", {"ticker": "AAPL"})
            self.assertIsInstance(result, ActionFailed)
            self.assertNotIn(CONTACT, result.error)
            self.assertNotIn("private", result.error)
            self.open.assert_called_once()

    def test_midstream_disconnect_and_deadline_return_no_asset(self):
        response = Response(b"abc")
        response.read = lambda count: (_ for _ in ()).throw(http.client.IncompleteRead(b"private"))
        self.open.return_value = response
        with self.assertRaisesRegex(StreamingAssetError, "transfer failed"):
            with client.source(ROOT + "report.htm", "test", 100):
                self.fail("Disconnected source must not be yielded")
        self.reply(b"abc")
        with patch.object(client.time, "monotonic", side_effect=[1.0, 122.0]):
            with self.assertRaisesRegex(StreamingAssetError, "deadline"):
                with client.source(ROOT + "report.htm", "test", 100):
                    self.fail("Late source must not be yielded")

    def test_catalog_and_stage_lookup(self):
        self.assertIsNone(sec.BUNDLED_TOOL.credentials)
        self.assertEqual(len(sec.MANIFEST.actions), 5)
        for action in sec.MANIFEST.actions:
            self.assertEqual(action.approval, "direct")
            self.assertEqual(tools_host.unsupported_schema_error(action.input_schema), "")
            self.assertEqual(action.output_schema == {}, action.returns_asset)
        from tests.stage.stage_tool_checks import StageToolChecks
        probe = StageToolChecks()
        with patch.object(probe, "_successful_tool_call", return_value={}) as call:
            self.assertEqual(probe._check_tool_provider("sec_edgar"), "1 live read(s) completed")
        call.assert_called_once_with("sec_edgar_lookup_company", {"ticker": "AAPL"})

    def test_asset_limit_and_stage_configuration_match_host_contracts(self):
        from pathlib import Path
        from host.runtime.tools.api import MAX_STREAMING_ASSET_BYTES
        self.assertLessEqual(client.DOCUMENT_LIMIT, MAX_STREAMING_ASSET_BYTES)
        response = Response(b"unused", headers={"Content-Length": str(client.DOCUMENT_LIMIT + 1)})
        self.open.return_value = response
        with patch.object(response, "read", wraps=response.read) as read:
            with self.assertRaisesRegex(StreamingAssetError, "size limit"):
                with client.source(ROOT + "report.htm", "test", client.DOCUMENT_LIMIT):
                    self.fail("Host-oversized source must not be transferred")
            read.assert_not_called()
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/kern-stage.yml").read_text()
        self.assertIn("KERN_STAGE_SEC_CONTACT_EMAIL: ${{ secrets.KERN_STAGE_SEC_CONTACT_EMAIL }}", workflow)


class SecPacingTests(unittest.TestCase):
    def test_concurrent_starts_share_one_limit(self):
        clock = [10.0]
        starts = []
        def sleep(seconds):
            clock[0] += seconds
            starts.append(clock[0])
        with patch.object(client, "_LAST_START", 0.0), patch.object(client.time, "monotonic", side_effect=lambda: clock[0]), patch.object(client.time, "sleep", side_effect=sleep):
            threads = [threading.Thread(target=client.pace) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(starts, [10 + i * .25 for i in range(8)])
