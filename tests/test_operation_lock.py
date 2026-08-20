from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from host.cli.operation_lock import OperationLock
from host.config import ConfigError


class OperationLockTests(unittest.TestCase):
    def test_same_provider_and_agent_cannot_interleave(self) -> None:
        with tempfile.TemporaryDirectory() as runtime, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": runtime}
        ):
            first = OperationLock("lima", "kern-test")
            with first:
                with self.assertRaisesRegex(ConfigError, "another Kern lifecycle command"):
                    with OperationLock("lima", "kern-test"):
                        pass
            with OperationLock("lima", "kern-test"):
                pass

    def test_provider_scopes_share_one_implementation_without_contending(self) -> None:
        with tempfile.TemporaryDirectory() as runtime, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": runtime}
        ):
            aws = OperationLock("aws", "kern-test")
            lima = OperationLock("lima", "kern-test")
            self.assertNotEqual(aws.path, lima.path)
            with aws, lima:
                self.assertEqual(aws.path.parent, lima.path.parent)

    def test_runtime_path_is_private_and_contains_no_agent_name(self) -> None:
        with tempfile.TemporaryDirectory() as runtime, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": runtime}
        ):
            lock = OperationLock("aws", "sensitive-agent-name")
            self.assertEqual(lock.path.parent, Path(runtime) / "kern" / "locks")
            self.assertNotIn("sensitive-agent-name", lock.path.name)
            with lock:
                self.assertEqual(stat.S_IMODE(lock.path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(lock.path.parent.stat().st_mode), 0o700)

    def test_relative_runtime_directory_is_rejected(self) -> None:
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "relative"}):
            with self.assertRaisesRegex(ConfigError, "must be an absolute path"):
                OperationLock("aws", "kern-test")

    def test_fallback_is_private_temporary_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {}, clear=True), patch(
            "host.cli.operation_lock.tempfile.gettempdir", return_value=temporary
        ):
            lock = OperationLock("lima", "kern-test")
            self.assertEqual(lock.path.parent.parent, Path(temporary) / f"kern-{os.getuid()}")
            with lock:
                pass


if __name__ == "__main__":
    unittest.main()
