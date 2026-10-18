"""Bounded HTTP body decoding using Ubuntu's libzstd stable streaming ABI.

Grok compresses inference requests with zstd. Keep inspection in the proxy;
do not forward opaque bodies or depend on a client compression preference.
"""

from __future__ import annotations

import ctypes
from functools import lru_cache


# Grok emits one frame. Allow a small concatenation margin, but bound work
# independently of decoded bytes: empty and skippable frames emit no output.
MAX_FRAMES = 64


class _Buffer(ctypes.Structure):
    # ZSTD_inBuffer and ZSTD_outBuffer have the same layout.
    _fields_ = [("data", ctypes.c_void_p), ("size", ctypes.c_size_t), ("pos", ctypes.c_size_t)]


@lru_cache(maxsize=1)
def _library() -> ctypes.CDLL | None:
    try:
        lib = ctypes.CDLL("libzstd.so.1")
        lib.ZSTD_createDStream.argtypes = []
        lib.ZSTD_createDStream.restype = ctypes.c_void_p
        lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
        lib.ZSTD_freeDStream.restype = ctypes.c_size_t
        lib.ZSTD_DCtx_setParameter.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        lib.ZSTD_DCtx_setParameter.restype = ctypes.c_size_t
        lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(_Buffer), ctypes.POINTER(_Buffer)]
        lib.ZSTD_decompressStream.restype = ctypes.c_size_t
        lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        lib.ZSTD_isError.restype = ctypes.c_uint
        return lib
    except (OSError, AttributeError):
        # Missing/incompatible host dependency must never skip inspection.
        return None


def decompress(body: bytes, limit: int) -> bytes | None:
    lib = _library()
    if lib is None or not body:
        return None
    context = lib.ZSTD_createDStream()
    if not context:
        return None
    try:
        # Stable ZSTD_d_windowLogMax=100. Bound decoder memory separately
        # from emitted output; reject frames advertising enormous windows.
        if lib.ZSTD_isError(lib.ZSTD_DCtx_setParameter(context, 100, 27)):
            return None
        # Borrow immutable bytes storage; c_char_p retains its owner for the
        # entire decode. libzstd reads this buffer using the explicit length,
        # including embedded NULs, and never writes into it.
        source = ctypes.c_char_p(body)
        incoming = _Buffer(ctypes.cast(source, ctypes.c_void_p), len(body), 0)
        output = ctypes.create_string_buffer(64 * 1024)
        decoded = bytearray()
        frames = 0
        while True:
            outgoing = _Buffer(ctypes.cast(output, ctypes.c_void_p), min(len(output), limit - len(decoded) + 1), 0)
            previous = incoming.pos
            remaining = lib.ZSTD_decompressStream(context, ctypes.byref(outgoing), ctypes.byref(incoming))
            if lib.ZSTD_isError(remaining):
                return None
            if outgoing.pos:
                decoded.extend(ctypes.string_at(output, outgoing.pos))
            if len(decoded) > limit:
                return None
            if remaining == 0:
                frames += 1
                if incoming.pos == incoming.size:
                    return bytes(decoded)
                if frames >= MAX_FRAMES:
                    return None
            # Continue through concatenated frames and buffered output. An
            # incomplete frame must not be accepted just because its JSON
            # bytes have already been emitted (e.g. a missing checksum).
            if incoming.pos == previous and outgoing.pos == 0:
                return None
    finally:
        lib.ZSTD_freeDStream(context)
