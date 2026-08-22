"""Unit tests for the Web Fetch tool (all network calls mocked)."""

from __future__ import annotations

import socket
import threading
import time
import unittest
from unittest.mock import patch

from host.tools import web_fetch
from host.tools.web_fetch import BUNDLED_TOOL
from host.tools.results import ActionExecuted, ActionFailed
from test_tools import FakeHostAPI, assert_matches_output_schema

HTML_PAGE = (
    b"<html><head><title>Example Title</title><style>body{color:red}</style>"
    b"<script>var secret = 1;</script></head>"
    b"<body><h1>Heading</h1><p>Hello <b>world</b> &amp; friends.</p>"
    b"<noscript>enable js</noscript></body></html>"
)


def _response(
    status: int = 200,
    headers: dict[str, str] | None = None,
    body: bytes = HTML_PAGE,
    truncated: bool = False,
) -> tuple[int, dict[str, str], bytes, bool]:
    return status, {"content-type": "text/html; charset=utf-8", **(headers or {})}, body, truncated


def _addrinfo(address: str) -> list[tuple[object, object, int, str, tuple[str, int]]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


class WebFetchUrlValidationTests(unittest.TestCase):
    """Structural URL rejection happens before any resolution or connection."""

    def test_guide_names_the_shared_fixed_user_agent(self) -> None:
        destination_card = web_fetch.MANIFEST.data_summary.cards[2]
        self.assertIn("User-Agent: kern-web-fetch/1", destination_card.description)
        self.assertIn("shared rather than unique", destination_card.description)

    def assert_rejected(self, url: object) -> None:
        with patch.object(web_fetch, "_fetch_once", side_effect=AssertionError("must not fetch")):
            result = BUNDLED_TOOL.execute("fetch_page", {"url": url}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertNotIn("Traceback", result.error)

    def test_rejects_structurally_invalid_urls(self) -> None:
        for url in (
            "",
            "http://example.com/page",
            "https://93.184.216.34/page",
            "https://[2606:2800:220:1::1]/page",
            "https://alice:secret@example.com/page",
            "https://example.com:8443/page",
            "https://localhost/page",
            "https://example.com./page",
            "https://exämple.com/page",
            "https://example.com/path;params",
            "https://example.com/" + "a" * web_fetch.MAX_URL_CHARS,
            "ftp://example.com/file",
            42,
        ):
            with self.subTest(url=url):
                self.assert_rejected(url)

    def test_unsupported_action_fails(self) -> None:
        result = BUNDLED_TOOL.execute("scrape_site", {"url": "https://example.com"}, FakeHostAPI())
        self.assertIsInstance(result, ActionFailed)


class WebFetchFetchTests(unittest.TestCase):
    def execute(self, url: str = "https://example.com/article") -> object:
        return BUNDLED_TOOL.execute("fetch_page", {"url": url}, FakeHostAPI())

    def test_returns_html_response_text_as_is(self) -> None:
        with patch.object(web_fetch, "_fetch_once", return_value=_response()) as fetch:
            result = self.execute()
        assert_matches_output_schema(self, web_fetch.MANIFEST, "fetch_page", result)
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["url"], "https://example.com/article")
        self.assertEqual(result.result["content_type"], "text/html")
        self.assertEqual(result.result["truncated"], False)
        self.assertEqual(result.result["content"], HTML_PAGE.decode())
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], "https://example.com/article")

    def test_strips_tracking_parameters_but_preserves_page_parameters(self) -> None:
        with patch.object(web_fetch, "_fetch_once", return_value=_response()) as fetch:
            result = self.execute(
                "https://example.com/article?id=42&utm_source=newsletter&"
                "%66bclid=click-id&sort=asc#section"
            )
        assert isinstance(result, ActionExecuted)
        expected = "https://example.com/article?id=42&sort=asc"
        self.assertEqual(fetch.call_args.args[0], expected)
        self.assertEqual(result.result["url"], expected)

    def test_accepts_the_explicit_normal_url_character_set(self) -> None:
        url = "https://sub.example.com/a_b-c.d/~page?q=hello+world&next=%2Fdocs#part"
        with patch.object(web_fetch, "_fetch_once", return_value=_response()) as fetch:
            result = self.execute(url)
        assert isinstance(result, ActionExecuted)
        expected = "https://sub.example.com/a_b-c.d/~page?q=hello+world&next=%2Fdocs"
        self.assertEqual(fetch.call_args.args[0], expected)

    def test_rejects_non_ascii_url_without_fetching(self) -> None:
        with patch.object(
            web_fetch, "_fetch_once", side_effect=AssertionError("must not fetch")
        ):
            result = self.execute("https://example.com/café?q=naïve")
        self.assertIsInstance(result, ActionFailed)

    def test_guard_checks_decoded_url_components(self) -> None:
        for encoded_value in (
            "alice%40example.com",
            "alice%2540example.com",
            "alice%2B%40example.com",
            "alice%252B%2540example.com",
            "alice+%2540example.com",
            "password%252Bis%252Bcorrecthorsebattery",
            "%41KIAIOSFODNN7EXAMPLE",
        ):
            with self.subTest(encoded_value=encoded_value), patch.object(
                web_fetch, "_fetch_once", side_effect=AssertionError("must not fetch")
            ):
                result = self.execute(f"https://example.com/lookup?q={encoded_value}")
            self.assertIsInstance(result, ActionFailed)

    def test_plain_text_returned_as_is(self) -> None:
        response = _response(headers={"content-type": "text/plain"}, body=b"line one\nline two")
        with patch.object(web_fetch, "_fetch_once", return_value=response):
            result = self.execute()
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["content"], "line one\nline two")

    def test_non_html_edge_whitespace_is_preserved(self) -> None:
        response = _response(headers={"content-type": "text/markdown"}, body=b"    code\nline  \n")
        with patch.object(web_fetch, "_fetch_once", return_value=response):
            result = self.execute()
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["content"], "    code\nline  \n")

    def test_text_is_decoded_as_utf8_with_bounded_replacement(self) -> None:
        body = b"\xef\xbb\xbfCaf\xc3\xa9 \xff"
        with patch.object(
            web_fetch,
            "_fetch_once",
            return_value=_response(headers={"content-type": "text/plain"}, body=body),
        ):
            result = self.execute()
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["content"], "Café �")

    def test_follows_redirect_after_revalidation(self) -> None:
        hops = [
            _response(status=301, headers={"location": "https://other.example.org/final"}, body=b""),
            _response(),
        ]
        with patch.object(web_fetch, "_fetch_once", side_effect=hops) as fetch:
            result = self.execute()
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["url"], "https://other.example.org/final")
        self.assertEqual(fetch.call_count, 2)

    def test_relative_redirect_resolves_against_current_url(self) -> None:
        hops = [
            _response(status=302, headers={"location": "/moved"}, body=b""),
            _response(),
        ]
        with patch.object(web_fetch, "_fetch_once", side_effect=hops):
            result = self.execute("https://example.com/article")
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["url"], "https://example.com/moved")

    def test_redirect_to_unsupported_destination_fails(self) -> None:
        for location in ("http://example.com/insecure", "https://10.0.0.8/internal"):
            hop = _response(status=301, headers={"location": location}, body=b"")
            with self.subTest(location=location), patch.object(
                web_fetch, "_fetch_once", return_value=hop
            ):
                result = self.execute()
            self.assertIsInstance(result, ActionFailed)
            assert isinstance(result, ActionFailed)
            self.assertIn("redirected", result.error)

    def test_redirect_loop_is_bounded(self) -> None:
        hop = _response(status=302, headers={"location": "https://example.com/loop"}, body=b"")
        with patch.object(web_fetch, "_fetch_once", return_value=hop) as fetch:
            result = self.execute()
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertIn("redirected more than", result.error)
        self.assertEqual(fetch.call_count, web_fetch.MAX_REDIRECTS + 1)

    def test_http_error_status_fails_without_body(self) -> None:
        response = _response(status=404, body=b"<html>secret error page</html>")
        with patch.object(web_fetch, "_fetch_once", return_value=response):
            result = self.execute()
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertEqual(result.error, "The page returned HTTP 404.")

    def test_binary_content_refused(self) -> None:
        response = _response(headers={"content-type": "image/png"}, body=b"\x89PNG")
        with patch.object(web_fetch, "_fetch_once", return_value=response):
            result = self.execute()
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertIn("text content type", result.error)

    def test_missing_content_type_refused(self) -> None:
        with patch.object(
            web_fetch, "_fetch_once", return_value=(200, {}, b"data", False)
        ):
            result = self.execute()
        self.assertIsInstance(result, ActionFailed)

    def test_compressed_content_refused(self) -> None:
        response = _response(headers={"content-encoding": "gzip"})
        with patch.object(web_fetch, "_fetch_once", return_value=response):
            result = self.execute()
        self.assertIsInstance(result, ActionFailed)
        assert isinstance(result, ActionFailed)
        self.assertIn("compressed", result.error)

    def test_truncation_is_flagged(self) -> None:
        with patch.object(web_fetch, "_fetch_once", return_value=_response(truncated=True)):
            result = self.execute()
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["truncated"], True)
        with patch.object(web_fetch, "MAX_CONTENT_CHARS", 5), patch.object(
            web_fetch,
            "_fetch_once",
            return_value=_response(headers={"content-type": "text/plain"}, body=b"longer text"),
        ):
            result = self.execute()
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["truncated"], True)
        content = result.result["content"]
        assert isinstance(content, str)
        self.assertEqual(len(content), 5)

    def test_redirects_share_one_wall_clock_deadline(self) -> None:
        deadlines: list[float] = []
        hops = [
            _response(status=302, headers={"location": "/final"}, body=b""),
            _response(),
        ]

        def fetch(url: str, deadline: float) -> tuple[int, dict[str, str], bytes, bool]:
            del url
            deadlines.append(deadline)
            return hops.pop(0)

        with patch.object(web_fetch.time, "monotonic", return_value=100.0), patch.object(
            web_fetch, "_fetch_once", side_effect=fetch
        ):
            result = self.execute()
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(deadlines, [120.0, 120.0])

    def test_tries_each_vetted_address_within_the_shared_deadline(self) -> None:
        deadline = 120.0
        with patch.object(web_fetch.time, "monotonic", return_value=100.0), patch.object(
            web_fetch,
            "_public_addresses",
            return_value=("2606:2800:220:1:248:1893:25c8:1946", "93.184.216.34"),
        ), patch.object(
            web_fetch,
            "_fetch_address",
            side_effect=[ValueError("first failed"), _response()],
        ) as fetch:
            result = web_fetch._fetch_once("https://example.com/", deadline)
        self.assertEqual(result, _response())
        self.assertEqual(
            [call.args for call in fetch.call_args_list],
            [
                ("https://example.com/", "2606:2800:220:1:248:1893:25c8:1946", 110.0),
                ("https://example.com/", "93.184.216.34", deadline),
            ],
        )

    def test_body_read_stops_at_absolute_deadline(self) -> None:
        class DripResponse:
            def read1(self, size: int) -> bytes:
                del size
                return b"x"

        class FakeSocket:
            def settimeout(self, timeout: float) -> None:
                self.timeout = timeout

        with patch.object(web_fetch.time, "monotonic", side_effect=[0.0, 21.0]):
            with self.assertRaisesRegex(ValueError, "timed out"):
                web_fetch._read_bounded_body(DripResponse(), FakeSocket(), 20.0)  # type: ignore[arg-type]

    def test_close_delimited_response_reads_its_retained_socket(self) -> None:
        class FakeSocket(socket.socket):
            def __init__(self) -> None:
                pass

            def settimeout(self, timeout: float | None) -> None:
                del timeout

            def shutdown(self, how: int) -> None:
                del how

            def close(self) -> None:
                pass

        response_socket = FakeSocket()

        class FakeResponse:
            status = 200
            fp = type("Buffered", (), {"raw": type("Raw", (), {"_sock": response_socket})()})()

            def getheaders(self) -> list[tuple[str, str]]:
                return [("Content-Type", "text/plain")]

            def read1(self, size: int) -> bytes:
                del size
                if hasattr(self, "read"):
                    return b""
                self.read = True
                return b"hello"

            def close(self) -> None:
                pass

        class FakeConnection:
            def __init__(self, hostname: str, address: str, deadline: float) -> None:
                del hostname, address, deadline
                self.sock: socket.socket | None = response_socket

            def connect(self) -> None:
                pass

            def request(self, method: str, target: str, headers: dict[str, str]) -> None:
                del method, target, headers

            def getresponse(self) -> FakeResponse:
                # HTTPConnection does this for Connection: close while the
                # response file still owns a readable reference.
                self.sock = None
                return FakeResponse()

            def close(self) -> None:
                pass

        with patch.object(
            web_fetch, "_public_addresses", return_value=("93.184.216.34",)
        ), patch.object(web_fetch, "_PinnedHTTPSConnection", FakeConnection):
            status, _headers, body, truncated = web_fetch._fetch_once(
                "https://example.com/", time.monotonic() + 20
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello")
        self.assertEqual(truncated, False)

    def test_header_read_is_interrupted_at_deadline(self) -> None:
        interrupted = threading.Event()

        class FakeSocket(socket.socket):
            def __init__(self) -> None:
                pass

            def shutdown(self, how: int) -> None:
                del how
                interrupted.set()

            def close(self) -> None:
                interrupted.set()

        class SlowHeaderConnection:
            def __init__(self, hostname: str, address: str, deadline: float) -> None:
                del hostname, address, deadline
                self.sock: socket.socket | None = FakeSocket()

            def connect(self) -> None:
                pass

            def request(self, method: str, target: str, headers: dict[str, str]) -> None:
                del method, target, headers

            def getresponse(self) -> object:
                interrupted.wait(1)
                raise OSError("interrupted")

            def close(self) -> None:
                pass

        with patch.object(
            web_fetch, "_public_addresses", return_value=("93.184.216.34",)
        ), patch.object(web_fetch, "_PinnedHTTPSConnection", SlowHeaderConnection):
            with self.assertRaisesRegex(ValueError, "timed out"):
                web_fetch._fetch_once("https://example.com/", time.monotonic() + 0.02)
        self.assertTrue(interrupted.is_set())


    def test_informational_responses_advance_to_final_response(self) -> None:
        client, server = socket.socketpair()
        response: web_fetch._InformationalHTTPResponse | None = None
        try:
            server.sendall(
                b"HTTP/1.1 102 Processing\r\nProgress: starting\r\n\r\n"
                b"HTTP/1.1 103 Early Hints\r\nLink: </style.css>; rel=preload\r\n\r\n"
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
            )
            response = web_fetch._InformationalHTTPResponse(client)
            response.begin()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), b"ok")
        finally:
            if response is not None:
                response.close()
            client.close()
            server.close()


class WebFetchAddressVettingTests(unittest.TestCase):
    """The resolver mirror of the proxy's connect_public: every resolved
    address must be publicly routable, and the vetted address is returned."""

    def test_public_addresses_returned(self) -> None:
        with patch("socket.getaddrinfo", return_value=_addrinfo("93.184.216.34")):
            self.assertEqual(web_fetch._public_addresses("example.com"), ("93.184.216.34",))

    def test_non_public_addresses_refused(self) -> None:
        for address in (
            "127.0.0.1", "10.0.0.8", "192.168.1.10", "169.254.169.254",
            "224.0.0.1", "::1", "fd00::1", "ff02::1",
        ):
            with self.subTest(address=address), patch(
                "socket.getaddrinfo", return_value=_addrinfo(address)
            ):
                with self.assertRaisesRegex(ValueError, "private or internal"):
                    web_fetch._public_addresses("rebound.example.com")

    def test_any_non_public_address_poisons_the_set(self) -> None:
        mixed = _addrinfo("93.184.216.34") + _addrinfo("127.0.0.1")
        with patch("socket.getaddrinfo", return_value=mixed):
            with self.assertRaisesRegex(ValueError, "private or internal"):
                web_fetch._public_addresses("rebound.example.com")

    def test_resolution_failure_is_sanitized(self) -> None:
        with patch("socket.getaddrinfo", side_effect=socket.gaierror("boom")):
            with self.assertRaisesRegex(ValueError, "could not be resolved"):
                web_fetch._public_addresses("nx.example.com")

    def test_resolution_wait_is_bounded_by_deadline(self) -> None:
        class TimedOutFuture:
            cancelled = False

            def add_done_callback(self, callback: object) -> None:
                self.callback = callback

            def result(self, timeout: float) -> object:
                self.timeout = timeout
                raise TimeoutError

            def cancel(self) -> None:
                self.cancelled = True
                self.callback(self)  # type: ignore[operator]

        future = TimedOutFuture()
        with patch.object(web_fetch._DNS_EXECUTOR, "submit", return_value=future):
            with self.assertRaisesRegex(ValueError, "timed out"):
                web_fetch._public_addresses("stalled.example.com", time.monotonic() + 10)
        self.assertTrue(future.cancelled)


if __name__ == "__main__":
    unittest.main()
