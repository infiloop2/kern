"""Resident Whisper worker, with no network or database access.

One inference at a time bounds CPU and memory. Audio lives only in the request;
the browser owns recovery. The model loads at startup and remains resident.
"""

from __future__ import annotations

from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import pwd
import socket
import threading
from typing import Any, cast

from host.constants import TRANSCRIPTION_SOCKET_PATH
from host.runtime.core.unix_socket_service import UnixSocketRequestHandler
from host.runtime.transcription.client import MAX_REQUEST_BYTES, decode_audio

MODEL_DIR = Path(os.environ.get("KERN_TRANSCRIPTION_MODEL_DIR",
                               "/usr/local/share/kern-transcription-models/small.en"))
_model_instance: Any = None
_inference_lock = threading.Lock()


def load_model() -> None:
    global _model_instance
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    _model_instance = WhisperModel(str(MODEL_DIR), device="cpu", compute_type="int8",
                                   cpu_threads=2, num_workers=1, local_files_only=True)


def transcribe(audio: bytes) -> str:
    if _model_instance is None:
        raise RuntimeError("model not ready")
    import numpy as np  # type: ignore[import-not-found]

    samples = np.frombuffer(audio, dtype="<i2").astype(np.float32) / 32768.0
    segments, _ = _model_instance.transcribe(
        samples, language="en", beam_size=1, condition_on_previous_text=False,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 400},
    )
    return "".join(segment.text for segment in segments).strip()


def allowed_uid() -> int:
    try:
        return pwd.getpwnam("kern-admin").pw_uid
    except KeyError:
        return -1


class Handler(UnixSocketRequestHandler):
    def do_GET(self) -> None:
        if self._peer()[1] != allowed_uid():
            self._send_json(401, {"error": "unauthorized"})
        elif self.path != "/ready":
            self._send_json(404, {"error": "route not found"})
        elif _model_instance is None:
            self._send_json(503, {"error": "model_not_ready"})
        else:
            self._send_json(200, {"ready": True})

    def do_POST(self) -> None:
        if self._peer()[1] != allowed_uid():
            self._send_json(401, {"error": "unauthorized"})
            return
        if self.path != "/transcribe":
            self._send_json(404, {"error": "route not found"})
            return
        if _model_instance is None:
            self._send_json(503, {"error": "model_not_ready"})
            return
        if not _inference_lock.acquire(blocking=False):
            self._send_json(503, {"error": "busy"})
            return
        try:
            self._transcribe_request()
        finally:
            _inference_lock.release()

    def _transcribe_request(self) -> None:
        length = self.bounded_content_length(MAX_REQUEST_BYTES)
        if length is None:
            return
        body = self.read_json_object_body(length)
        if body is None:
            return
        try:
            audio = decode_audio(body)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        try:
            self._send_json(200, {"text": transcribe(audio)})
        except Exception:
            # Never log speech or its transcript, including library exceptions.
            self._send_json(503, {"error": "transcription failed"})


class ActivatedServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX

    def __init__(self) -> None:
        super().__init__(TRANSCRIPTION_SOCKET_PATH, Handler, bind_and_activate=False)  # type: ignore[arg-type]
        self.socket.close()
        self.socket = socket.socket(fileno=3)
        if self.socket.getsockname() != TRANSCRIPTION_SOCKET_PATH:
            raise RuntimeError("wrong transcription listener")
        self.server_address = cast(Any, TRANSCRIPTION_SOCKET_PATH)


def main() -> int:
    if int(os.environ.get("LISTEN_PID", "0")) != os.getpid() or os.environ.get("LISTEN_FDS") != "1":
        raise RuntimeError("transcription must be started by its systemd socket")
    server = ActivatedServer()
    failed = threading.Event()

    def preload() -> None:
        try:
            load_model()
        except Exception:
            # A failed startup is restarted by systemd; never expose library
            # exceptions (which may contain host paths) to the browser.
            failed.set()
            server.shutdown()

    threading.Thread(target=preload, daemon=True).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 1 if failed.is_set() else 0


if __name__ == "__main__":
    raise SystemExit(main())
