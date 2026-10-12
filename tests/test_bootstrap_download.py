from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

from host.bootstrap.render import _render_bootstrap


class BootstrapDownloadTests(unittest.TestCase):
    def run_download(
        self, statuses: list[int], *, stalled: bool = False, retry_after: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], bytes, int]:
        requests: list[int] = []
        release = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                status = statuses[min(len(requests), len(statuses) - 1)]
                requests.append(status)
                self.send_response(status)
                if retry_after:
                    self.send_header("Retry-After", "600")
                body = b"downloaded binary" if status == 200 else b"gateway failure"
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if stalled:
                    release.wait(15)
                    return
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        for port in range(8000, 8016):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                break
            except OSError:
                continue
        else:
            self.fail("No test port available in 8000–8015")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            bootstrap = _render_bootstrap()
            helpers = bootstrap.split("bootstrap_log() {", 1)[1].split(
                "\ninstall_system_packages() {", 1,
            )[0]
            # Exercise the real helper and curl, shortening only deadlines for
            # timeout tests. The normal HTTP tests use the production policy.
            curl_override = ""
            if stalled:
                curl_override = 'curl() { command curl "$@" --max-time 0.1; }\n'
            elif retry_after:
                curl_override = 'curl() { command curl "$@" --retry-max-time 1; }\n'
            script = (
                "set -euo pipefail\nbootstrap_log() {" + helpers + "\n"
                + curl_override + 'bootstrap_download "$1" "$2"\necho continued\n'
            )
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "download"
                result = subprocess.run(
                    ["bash", "-c", script, "download-test", str(output),
                     f"http://127.0.0.1:{port}/binary"],
                    capture_output=True, text=True, timeout=15,
                    env={**os.environ, "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"},
                )
                contents = output.read_bytes() if output.exists() else b""
            return result, contents, len(requests)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_recovers_from_504_and_writes_only_successful_body(self) -> None:
        result, body, attempts = self.run_download([504, 504, 200])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(body, b"downloaded binary")
        self.assertEqual(attempts, 3)
        self.assertIn("continued", result.stdout)

    def test_persistent_504_stops_after_four_attempts(self) -> None:
        result, _, attempts = self.run_download([504])
        self.assertEqual(result.returncode, 22)
        self.assertEqual(attempts, 4)
        self.assertIn("download failed (exit 22): http://127.0.0.1:", result.stderr)
        self.assertNotIn("continued", result.stdout)

    def test_permanent_http_errors_are_not_retried(self) -> None:
        for status in (403, 404):
            with self.subTest(status=status):
                result, _, attempts = self.run_download([status])
                self.assertEqual(result.returncode, 22)
                self.assertEqual(attempts, 1)

    def test_stalled_downloads_time_out_and_exhaust_retries(self) -> None:
        result, _, attempts = self.run_download([200], stalled=True)
        self.assertEqual(result.returncode, 28)
        self.assertEqual(attempts, 4)
        self.assertNotIn("continued", result.stdout)

    def test_retry_after_cannot_exceed_retry_budget(self) -> None:
        result, _, attempts = self.run_download([503], retry_after=True)
        self.assertEqual(result.returncode, 22)
        self.assertEqual(attempts, 1)

    def test_rendered_bootstrap_has_valid_shell_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n"], input=_render_bootstrap(), text=True, capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
