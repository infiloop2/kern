from __future__ import annotations

from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from host.runtime.browser.browser import Browser
from host.runtime.browser.client import BrowserError
from host.runtime.browser.display import Display


class BrowserDisplayTests(unittest.TestCase):
    def test_display_readiness_accepts_a_fragmented_pipe_write(self):
        process = Mock()
        process.poll.return_value = None
        with patch("host.runtime.browser.display.subprocess.Popen", return_value=process), patch(
            "host.runtime.browser.display.select.select", return_value=([process.stdout], [], []),
        ), patch("host.runtime.browser.display.os.read", side_effect=[b"12", b"\n"]):
            display = Display()
        self.assertEqual(display.environment['DISPLAY'], ':12')
        display.close()

    def test_failed_display_start_removes_authentication_files(self):
        directories = []

        def fail(command, **kwargs):
            authority = Path(command[command.index("-auth") + 1])
            directories.append(authority.parent)
            self.assertEqual(authority.stat().st_mode & 0o777, 0o600)
            self.assertEqual(authority.parent.stat().st_mode & 0o777, 0o700)
            raise OSError("display unavailable")

        with patch("host.runtime.browser.display.subprocess.Popen", side_effect=fail):
            with self.assertRaises(OSError):
                Display()
        self.assertTrue(directories)
        self.assertTrue(all(not directory.exists() for directory in directories))

    def test_timeout_and_invalid_readiness_stop_display_and_remove_files(self):
        for ready, number in [(False, b""), (True, b""), (True, b"invalid\n")]:
            process = Mock()
            process.poll.return_value = None
            with patch("host.runtime.browser.display.subprocess.Popen", return_value=process) as launch, patch(
                "host.runtime.browser.display.select.select", return_value=([process.stdout] if ready else [], [], []),
            ), patch("host.runtime.browser.display.os.read", return_value=number):
                with self.assertRaises(BrowserError):
                    Display()
            process.terminate.assert_called_once()
            process.wait.assert_called_once_with(timeout=3)
            process.stdout.close.assert_called_once()
            command = launch.call_args.args[0]
            self.assertFalse(Path(command[command.index("-auth") + 1]).exists())

    def test_unresponsive_display_is_killed_before_removing_credentials(self):
        display = Display.__new__(Display)
        display.directory = Mock()
        display.process = Mock()
        display.process.poll.return_value = None
        display.process.wait.side_effect = [subprocess.TimeoutExpired("Xvfb", 3), 0]
        display.close()
        display.process.kill.assert_called_once()
        self.assertEqual(display.process.wait.call_count, 2)
        display.directory.cleanup.assert_called_once()

    def test_browser_launch_failure_closes_display_and_playwright(self):
        runtime = Mock()
        with patch("playwright.sync_api.sync_playwright") as manager, patch(
            "host.runtime.browser.browser.Display",
        ) as display, patch("host.runtime.browser.browser.Chromium", side_effect=RuntimeError("launch failed")):
            manager.return_value.start.return_value = runtime
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                Browser(None, "https://x.com/", {"mode": "direct"})
        display.return_value.close.assert_called_once()
        runtime.stop.assert_called_once()

    def test_typing_and_paste_use_distinct_browser_events(self):
        browser = Browser.__new__(Browser)
        browser.page = Mock()
        browser.input({"kind": "type", "text": "A"})
        browser.page.keyboard.type.assert_called_once_with("A")
        browser.page.keyboard.insert_text.assert_not_called()
        browser.input({"kind": "text", "text": "pasted 😀 text"})
        browser.page.keyboard.insert_text.assert_called_once_with("pasted 😀 text")
        for value in (None, True, "", "multiple", "\n", "\x00"):
            with self.assertRaises(BrowserError):
                browser.input({"kind": "type", "text": value})


if __name__ == "__main__":
    unittest.main()
