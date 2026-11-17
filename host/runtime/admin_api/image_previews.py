"""Read bounded image headers without decoding pixels in the admin process."""
from __future__ import annotations

MAX_IMAGE_EDGE = 8192
MAX_IMAGE_PIXELS = 16_000_000


def _dimensions(media_type: str, data: bytes) -> tuple[int, int] | None:
    if media_type == "image/png":
        if len(data) >= 24 and data[:16] == b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    elif media_type == "image/jpeg" and data.startswith(b"\xff\xd8"):
        offset = 2
        while offset < len(data):
            if data[offset] != 0xFF:
                return None
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                return None
            marker = data[offset]
            offset += 1
            if marker in (0xD9, 0xDA):  # End of image / start of entropy-coded scan.
                return None
            if marker == 0x01 or 0xD0 <= marker <= 0xD7:
                continue
            if offset + 2 > len(data):
                return None
            length = int.from_bytes(data[offset:offset + 2], "big")
            if length < 2 or offset + length > len(data):
                return None
            # SOF markers contain precision, height, width, then components.
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if length < 8:
                    return None
                return (int.from_bytes(data[offset + 5:offset + 7], "big"),
                        int.from_bytes(data[offset + 3:offset + 5], "big"))
            offset += length
    elif media_type == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        if len(data) < 25:
            return None
        kind = data[12:16]
        length = int.from_bytes(data[16:20], "little")
        if 20 + length > len(data):
            return None
        # The first chunk carries dimensions for simple lossy/lossless WebP
        # or the canvas for extended WebP (including alpha and metadata).
        # https://developers.google.com/speed/webp/docs/riff_container
        if kind == b"VP8X" and length == 10:
            return (1 + int.from_bytes(data[24:27], "little"),
                    1 + int.from_bytes(data[27:30], "little"))
        if kind == b"VP8L" and length >= 5 and data[20] == 0x2F:
            packed = int.from_bytes(data[21:25], "little")
            return (packed & 0x3FFF) + 1, ((packed >> 14) & 0x3FFF) + 1
        if kind == b"VP8 " and length >= 10 and data[23:26] == b"\x9d\x01\x2a":
            return (int.from_bytes(data[26:28], "little") & 0x3FFF,
                    int.from_bytes(data[28:30], "little") & 0x3FFF)
    return None


def validate_image_dimensions(media_type: str, data: bytes) -> None:
    dimensions = _dimensions(media_type, data)
    if dimensions is None or min(dimensions) < 1:
        raise ValueError("file has invalid or unsupported image dimensions")
    width, height = dimensions
    if max(width, height) > MAX_IMAGE_EDGE or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("image exceeds the 8192 px edge or 16 megapixel chat preview limit")
