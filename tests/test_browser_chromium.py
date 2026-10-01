"""Failure cleanup and isolation when attaching to separately started Chromium."""
from pathlib import Path
import signal
import subprocess
import unittest
from unittest.mock import Mock, patch

from playwright.sync_api import Error

from host.runtime.browser.chromium import Chromium
from host.runtime.browser.client import BrowserError


class ChromiumTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.chromium.executable_path = "/fixture/chrome"
        self.context = Mock()
        self.runtime.chromium.connect_over_cdp.return_value.contexts = [self.context]
        self.child = Mock(pid=12345)
        self.child.poll.return_value = None
        self.launch = self.enterContext(patch("host.runtime.browser.chromium.subprocess.Popen", return_value=self.child))
        self.signals = self.enterContext(patch("host.runtime.browser.chromium.os.killpg"))
        self.sockets = self.enterContext(patch("host.runtime.browser.chromium.socket.socket"))
        self.sockets.return_value.__enter__.return_value.connect_ex.return_value = 0

    def test_original_context_and_private_profile_are_used_and_removed(self):
        process = Chromium(self.runtime, {"DISPLAY": ":1"}, {"locale": "en-GB", "timezone": "Europe/London"})
        self.assertIs(process.context, self.context)
        process.browser.new_context.assert_not_called()
        self.assertEqual(Path(process.directory.name).stat().st_mode & 0o777, 0o700)
        command = self.launch.call_args.args[0]
        self.assertNotIn("--no-sandbox", command)
        self.assertNotIn("--enable-automation", command)
        self.assertIn("--proxy-bypass-list=<-loopback>", command)
        self.assertEqual(self.launch.call_args.kwargs["env"]["TZ"], "Europe/London")
        process.close()
        process.close()
        self.child.wait.assert_called_once_with(timeout=5)
        self.assertFalse(Path(process.directory.name).exists())

    def test_startup_timeout_and_early_exit_clean_up(self):
        for exited in (False, True):
            with self.subTest(exited=exited):
                self.child.poll.return_value = 1 if exited else None
                self.sockets.return_value.__enter__.return_value.connect_ex.return_value = 111
                with patch("host.runtime.browser.chromium.time.monotonic", side_effect=[0, 0, 31]), patch(
                    "host.runtime.browser.chromium.time.sleep",
                ):
                    with self.assertRaises(BrowserError):
                        Chromium(self.runtime, {}, {})
                profile = next(arg.split("=", 1)[1] for arg in self.launch.call_args.args[0] if arg.startswith("--user-data-dir="))
                self.assertFalse(Path(profile).exists())
                self.signals.assert_any_call(self.child.pid, signal.SIGTERM)

    def test_disconnect_failure_still_stops_child_and_cleans_profile(self):
        process = Chromium(self.runtime, {}, {})
        process.browser.close.side_effect = Error("disconnected")
        self.child.wait.side_effect = [subprocess.TimeoutExpired("chrome", 5), 0]
        with self.assertRaises(Error):
            process.close()
        self.signals.assert_any_call(self.child.pid, signal.SIGKILL)
        self.assertFalse(Path(process.directory.name).exists())

    def test_occupied_port_never_attaches_to_existing_browser(self):
        with patch("host.runtime.browser.chromium.socket.socket") as sockets:
            sockets.return_value.__enter__.return_value.bind.side_effect = OSError("address in use")
            with self.assertRaises(OSError):
                Chromium(self.runtime, {}, {})
        self.launch.assert_not_called()
        self.runtime.chromium.connect_over_cdp.assert_not_called()
