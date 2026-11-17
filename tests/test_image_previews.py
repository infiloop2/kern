"""Header-only checks must reject decompression-sized images before browser decode."""
import struct
import unittest

from host.runtime.admin_api.image_previews import validate_image_dimensions


def png_header(width, height):
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", width, height)


def jpeg_header(width, height, marker=0xC0):
    return b"\xff\xd8\xff\xe1\x00\x06Exif\xff" + bytes([marker]) + struct.pack(">HBHHB", 8, 8, height, width, 1)


def webp_header(width, height, kind=b"VP8X"):
    if kind == b"VP8X":
        data = b"\0" * 4 + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    elif kind == b"VP8L":
        data = b"\x2f" + ((width - 1) | ((height - 1) << 14)).to_bytes(4, "little")
    else:
        data = b"\0" * 3 + b"\x9d\x01\x2a" + struct.pack("<HH", width, height)
    return b"RIFF" + (12 + len(data)).to_bytes(4, "little") + b"WEBP" + kind + len(data).to_bytes(4, "little") + data


class ImagePreviewDimensionsTests(unittest.TestCase):
    def test_supported_header_variants_and_exact_limits(self):
        for width, height in [(1, 1), (4000, 4000), (8192, 1), (1, 8192)]:
            for media, data in [
                ("image/png", png_header(width, height)),
                ("image/jpeg", jpeg_header(width, height)),
                ("image/jpeg", jpeg_header(width, height, 0xC2)),
                *( ("image/webp", webp_header(width, height, kind)) for kind in (b"VP8X", b"VP8L", b"VP8 ") ),
            ]:
                with self.subTest(media=media, width=width, height=height, data=data):
                    validate_image_dimensions(media, data)

    def test_byte_small_images_with_huge_dimensions_are_rejected(self):
        for width, height in [(16383, 16383), (4000, 4001), (8193, 1), (1, 8193)]:
            for media, data in [
                ("image/png", png_header(width, height)),
                ("image/jpeg", jpeg_header(width, height)),
                *( ("image/webp", webp_header(width, height, kind)) for kind in (b"VP8X", b"VP8L", b"VP8 ") ),
            ]:
                with self.subTest(media=media, width=width, height=height):
                    with self.assertRaisesRegex(ValueError, "preview limit"):
                        validate_image_dimensions(media, data)

    def test_invalid_truncated_and_zero_dimension_headers_fail_closed(self):
        for media, data in [
            ("image/png", png_header(0, 10)),
            ("image/png", png_header(10, 10)[:23]),
            ("image/jpeg", jpeg_header(10, 10)[:-1]),
            ("image/jpeg", b"\xff\xd8\xff\xe1\x00\x00"),
            ("image/jpeg", b"\xff\xd8\xff\xda\x00\x02"),
            ("image/webp", webp_header(10, 10)[:-1]),
            ("image/webp", b"RIFF\0\0\0\0WEBP"),
        ]:
            with self.subTest(media=media, data=data):
                with self.assertRaisesRegex(ValueError, "invalid or unsupported"):
                    validate_image_dimensions(media, data)
