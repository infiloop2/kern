"""Bounded downloads when a provider omits Content-Length."""

from contextlib import contextmanager
import io
import unittest
from unittest.mock import patch

from host.tools.results import StreamingAssetError
from host.tools.shared import media
from host.tools.shared.web import WebRequestError


class MediaDownloadTests(unittest.TestCase):
    def open_video(self):
        return media.open_downloaded_video(
            "https://provider.example/content", provider="Provider",
            filename_stem="render", map_failure=lambda exc: "Safe transfer failure.",
        )

    @contextmanager
    def response(self, source, headers):
        @contextmanager
        def stream(*args, **kwargs):
            with source:
                yield source, headers
        with patch.object(media, "open_response_stream", stream):
            yield

    def test_missing_length_accepts_inclusive_bounds_and_short_reads(self):
        class ShortReads(io.BytesIO):
            def read(self, size=-1):
                return super().read(min(size, 37))

        for size in (512, 1024):
            with self.subTest(size=size), patch.object(media, "MAX_MEDIA_BYTES", 1024):
                source = ShortReads(b"x" * size)
                with self.response(source, {"content-type": "video/mp4"}):
                    with self.open_video() as opened:
                        self.assertEqual(opened.size_bytes, size)
                        self.assertEqual(opened.source.read(), b"x" * size)
                    self.assertTrue(opened.source.closed)
                self.assertTrue(source.closed)

    def test_missing_length_rejects_empty_small_and_oversized_body_and_closes_temp(self):
        for size in (0, 511, 1025):
            with self.subTest(size=size), patch.object(media, "MAX_MEDIA_BYTES", 1024):
                temporary = io.BytesIO()
                source = io.BytesIO(b"x" * size)
                with self.response(source, {"content-type": "video/mp4"}), patch.object(media, "TemporaryFile", return_value=temporary):
                    with self.assertRaisesRegex(StreamingAssetError, "outside the supported range"):
                        with self.open_video():
                            self.fail("Invalid body was admitted")
                self.assertTrue(temporary.closed)
                self.assertTrue(source.closed)

    def test_missing_length_stops_reading_at_limit_plus_one(self):
        source = io.BytesIO(b"x" * 4096)
        with patch.object(source, "read", wraps=source.read) as read:
            with self.response(source, {"content-type": "video/mp4"}), patch.object(media, "MAX_MEDIA_BYTES", 1024):
                with self.assertRaises(StreamingAssetError):
                    with self.open_video():
                        pass
            read.assert_called_once_with(1025)

    def test_invalid_headers_are_rejected_without_reading_body(self):
        headers_list = [
            {"content-type": "text/html"},
            *({"content-type": "video/mp4", "content-length": length}
              for length in ("", "garbage", "-1", "１２３４", "511", "200000001")),
        ]
        for headers in headers_list:
            with self.subTest(headers=headers):
                source = io.BytesIO(b"x" * 1024)
                with patch.object(source, "read", wraps=source.read) as read:
                    with self.response(source, headers), self.assertRaises(StreamingAssetError):
                        with self.open_video():
                            pass
                    read.assert_not_called()

    def test_transfer_failure_is_mapped_and_temp_is_closed(self):
        source = io.BytesIO()
        temporary = io.BytesIO()
        with patch.object(source, "read", side_effect=[b"x" * 512, WebRequestError("secret URL")]):
            with self.response(source, {"content-type": "video/mp4"}), patch.object(media, "TemporaryFile", return_value=temporary):
                with self.assertRaisesRegex(StreamingAssetError, "^Safe transfer failure.$"):
                    with self.open_video():
                        pass
        self.assertTrue(temporary.closed)
        self.assertTrue(source.closed)

    def test_declared_length_keeps_direct_streaming(self):
        source = io.BytesIO(b"x" * 1024)
        with self.response(source, {"content-type": "video/mp4", "content-length": "1024"}), patch.object(media, "TemporaryFile") as temporary:
            with self.open_video() as opened:
                self.assertIs(opened.source, source)
                self.assertEqual(opened.size_bytes, 1024)
            temporary.assert_not_called()


if __name__ == "__main__":
    unittest.main()
