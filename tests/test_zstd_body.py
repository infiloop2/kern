"""Grok compressed request regression and fail-closed inspection boundaries."""
import ctypes
import json
import unittest
from unittest.mock import patch

from host.network_integrations.xai import guard
from host.network_integrations.xai.manifest import XaiIntegration
from host.runtime.core import network_policy, zstd_body
from test_network_proxy import xai_bearer


def compress(data, *, content_size=True):
    # Independent producer through libzstd's stable compression API. Include
    # checksums so truncated/corrupt trailers exercise complete-frame checks.
    lib = ctypes.CDLL("libzstd.so.1")
    lib.ZSTD_createCCtx.restype = ctypes.c_void_p
    lib.ZSTD_freeCCtx.argtypes = [ctypes.c_void_p]
    lib.ZSTD_CCtx_setParameter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.ZSTD_CCtx_setParameter.restype = ctypes.c_size_t
    lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
    lib.ZSTD_compressBound.restype = ctypes.c_size_t
    lib.ZSTD_compress2.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    lib.ZSTD_compress2.restype = ctypes.c_size_t
    lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
    lib.ZSTD_isError.restype = ctypes.c_uint
    context = lib.ZSTD_createCCtx()
    try:
        assert not lib.ZSTD_isError(lib.ZSTD_CCtx_setParameter(context, 200, int(content_size)))
        assert not lib.ZSTD_isError(lib.ZSTD_CCtx_setParameter(context, 201, 1))
        output = ctypes.create_string_buffer(lib.ZSTD_compressBound(len(data)))
        size = lib.ZSTD_compress2(context, output, len(output), data, len(data))
        assert not lib.ZSTD_isError(size)
        return output.raw[:size]
    finally:
        lib.ZSTD_freeCCtx(context)


class ZstdBodyTests(unittest.TestCase):
    def test_native_decoder_borrows_original_binary_input(self):
        payload = bytes(range(256)) * 1000
        wire = compress(payload)
        self.assertIn(b"\x00", wire)
        original_address = ctypes.cast(ctypes.c_char_p(wire), ctypes.c_void_p).value
        lib = zstd_body._library()
        native_decode = lib.ZSTD_decompressStream

        def check_input(context, output, source):
            incoming = ctypes.cast(source, ctypes.POINTER(zstd_body._Buffer)).contents
            self.assertEqual(incoming.data, original_address)
            self.assertEqual(incoming.size, len(wire))
            return native_decode(context, output, source)

        with patch.object(lib, "ZSTD_decompressStream", side_effect=check_input) as decode:
            self.assertEqual(network_policy.decode_body(wire, "zstd"), payload)
            self.assertGreater(decode.call_count, 1)

    def test_known_and_unknown_size_frames_across_output_chunks(self):
        payload = b'{"input":"' + b"hello " * 30000 + b'"}'
        for known in (True, False):
            with self.subTest(content_size=known):
                self.assertEqual(network_policy.decode_body(compress(payload, content_size=known), " ZSTD "), payload)

    def test_output_limit_exact_boundary_and_bomb(self):
        for size in (127, 128, 129, 1000000):
            for known in (True, False):
                with self.subTest(size=size, content_size=known), patch.object(network_policy, "MAX_DECODED_BODY_BYTES", 128):
                    payload = b"x" * size
                    self.assertEqual(network_policy.decode_body(compress(payload, content_size=known), "zstd"), payload if size <= 128 else None)

    def test_truncated_and_corrupt_frames_fail_even_after_json_emitted(self):
        wire = compress(b'{"tools":[]}')
        for end in range(len(wire)):
            with self.subTest(truncation=end):
                self.assertIsNone(network_policy.decode_body(wire[:end], "zstd"))
        self.assertIsNone(network_policy.decode_body(wire[:-1] + bytes([wire[-1] ^ 1]), "zstd"))
        self.assertIsNone(network_policy.decode_body(b"not zstd", "zstd"))

    def test_concatenated_frames_inspected_in_full_with_shared_limit(self):
        wire = compress(b'{"tools":[') + compress(b'{"type":"web_search"}]}')
        self.assertEqual(guard._server_tool_denial([("Content-Encoding", "zstd")], wire), "xai_web_search_denied")
        with patch.object(network_policy, "MAX_DECODED_BODY_BYTES", 20):
            self.assertIsNone(network_policy.decode_body(wire, "zstd"))
        self.assertIsNone(network_policy.decode_body(wire + b"trailing garbage", "zstd"))

    def test_large_advertised_window_rejected_before_allocating_it(self):
        # Streaming frame header, window descriptor 0x90 = 256 MiB, then an
        # empty last raw block. Decoder's independent window cap is 128 MiB.
        self.assertIsNone(network_policy.decode_body(bytes.fromhex("28b52ffd0090010000"), "zstd"))

    def test_empty_and_skippable_frame_floods_have_bounded_work(self):
        lib = zstd_body._library()
        self.assertIsNotNone(lib)
        # A legal skippable frame with a zero-length payload, as well as an
        # ordinary empty frame: neither advances the decoded-byte limit.
        for frame in (compress(b""), bytes.fromhex("502a4d1800000000")):
            with self.subTest(frame=frame.hex()):
                permitted = frame * (zstd_body.MAX_FRAMES - 1) + compress(b"{}")
                self.assertEqual(network_policy.decode_body(permitted, "zstd"), b"{}")
                with patch.object(lib, "ZSTD_decompressStream", wraps=lib.ZSTD_decompressStream) as decode:
                    self.assertIsNone(network_policy.decode_body(frame * 100000 + compress(b"{}"), "zstd"))
                    self.assertEqual(decode.call_count, zstd_body.MAX_FRAMES)
                self.assertIsNone(network_policy.decode_body(frame * zstd_body.MAX_FRAMES + compress(b"{}"), "zstd"))

    def test_missing_library_and_unknown_encoding_fail_closed(self):
        with patch.object(zstd_body, "_library", return_value=None):
            self.assertIsNone(network_policy.decode_body(compress(b"{}"), "zstd"))
        for encoding in ("br", "zstd, gzip", "something-new"):
            self.assertIsNone(network_policy.decode_body(b"{}", encoding))

    def test_valid_empty_frame_is_decoded(self):
        self.assertEqual(network_policy.decode_body(compress(b""), "zstd"), b"")

    def test_grok_inference_still_checks_account_and_hosted_tools(self):
        headers = [("Authorization", xai_bearer("pinned")), ("Content-Type", "application/json"), ("Content-Encoding", "zstd")]
        cases = [
            ([{"type": "function", "name": "reference_to_video"}], None),
            ([{"type": "x_search"}], None),
            ([{"type": "web_search"}], "xai_web_search_denied"),
            ([{"type": "mcp", "server_url": "https://example.com"}], "xai_remote_mcp_denied"),
            ([{"type": "unknown_tool"}], "xai_server_tool_denied"),
        ]
        with patch.object(guard, "read_proxy_xai_account_ids", return_value={"pinned"}):
            for tools, expected in cases:
                with self.subTest(tools=tools):
                    wire = compress(json.dumps({"model": "grok-4.7", "tools": tools}).encode(), content_size=False)
                    self.assertEqual(guard.request_denied(XaiIntegration(enabled=True), "POST", "cli-chat-proxy.grok.com", "/v1/responses", "", headers, wire), expected)
            self.assertIsNotNone(guard.request_denied(XaiIntegration(enabled=True), "POST", "cli-chat-proxy.grok.com", "/v1/responses", "", headers[1:], compress(b"{}")))
        self.assertEqual(guard._server_tool_denial(headers, compress(b"{bad json")), "xai_body_not_json")


if __name__ == "__main__":
    unittest.main()
