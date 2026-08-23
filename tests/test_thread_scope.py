from __future__ import annotations

import subprocess
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.agent_runtime import thread_scope


class ThreadScopeTests(unittest.TestCase):
    def test_interrupt_is_signal_only_and_best_effort(self) -> None:
        with patch.object(
            thread_scope.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired("stop-agent-thread", 3),
        ) as run:
            thread_scope.interrupt_thread_scope(
                "thread-1",
                ["/launcher", "--thread-scope", "thread-1"],
                ["/launcher"],
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                *thread_scope.STOP_COMMAND,
                "--signal-only",
                "thread-1",
            ],
        )
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            thread_scope.INTERRUPT_TIMEOUT_SECONDS,
        )

    def test_close_requires_the_production_scope_to_be_reaped(self) -> None:
        result = MagicMock(returncode=1)
        with (
            patch.object(thread_scope.subprocess, "run", return_value=result) as run,
            self.assertRaisesRegex(
                thread_scope.ThreadScopeError,
                "did not close",
            ),
        ):
            thread_scope.stop_thread_scope(
                "thread-1",
                ["/launcher", "--thread-scope", "thread-1"],
                ["/launcher"],
            )

        self.assertEqual(
            run.call_args.args[0],
            [*thread_scope.STOP_COMMAND, "thread-1"],
        )
        self.assertEqual(
            run.call_args.kwargs["timeout"],
            thread_scope.CLOSE_TIMEOUT_SECONDS,
        )

    def test_test_commands_and_threadless_processes_have_no_scope(self) -> None:
        with patch.object(thread_scope.subprocess, "run") as run:
            thread_scope.interrupt_thread_scope(
                "thread-1",
                ["/usr/bin/fake-provider"],
                ["/launcher"],
            )
            thread_scope.stop_thread_scope(
                None,
                ["/launcher"],
                ["/launcher"],
            )
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
