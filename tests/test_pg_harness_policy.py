import os
from types import SimpleNamespace
import unittest
from unittest import mock

import pg_harness


class PgHarnessHostPolicyTests(unittest.TestCase):
    def test_github_actions_runs_postgres_tests(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}, clear=True):
            self.assertIsNone(pg_harness._host_skip_reason())

    def test_live_kern_agent_host_self_detects_without_launcher_flag(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                pg_harness.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_name="kern-agent"),
            ),
            mock.patch.object(pg_harness.Path, "is_dir", return_value=True),
        ):
            reason = pg_harness._host_skip_reason()

        self.assertIn("GitHub Actions run the database suite", reason or "")

    def test_non_host_developer_machine_keeps_postgres_tests_available(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(
                pg_harness.pwd,
                "getpwuid",
                return_value=SimpleNamespace(pw_name="developer"),
            ),
            mock.patch.object(pg_harness.Path, "is_dir", return_value=True),
        ):
            self.assertIsNone(pg_harness._host_skip_reason())

    def test_postgres_child_is_bound_to_its_expected_parent(self) -> None:
        libc = mock.Mock()
        libc.prctl.return_value = 0
        with (
            mock.patch.object(pg_harness, "_LIBC", libc),
            mock.patch.object(pg_harness.os, "getppid", return_value=42),
            mock.patch.object(pg_harness.os, "kill") as kill,
        ):
            pg_harness._bind_server_to_test_process(42)

        libc.prctl.assert_called_once_with(
            pg_harness._PR_SET_PDEATHSIG,
            pg_harness.signal.SIGINT,
            0,
            0,
            0,
        )
        kill.assert_not_called()

    def test_postgres_child_closes_parent_exit_race(self) -> None:
        libc = mock.Mock()
        libc.prctl.return_value = 0
        with (
            mock.patch.object(pg_harness, "_LIBC", libc),
            mock.patch.object(pg_harness.os, "getppid", return_value=1),
            mock.patch.object(pg_harness.os, "kill") as kill,
        ):
            pg_harness._bind_server_to_test_process(42)

        kill.assert_called_once_with(pg_harness.os.getpid(), pg_harness.signal.SIGINT)

    def test_parent_death_hook_is_linux_only(self) -> None:
        with mock.patch.object(pg_harness.sys, "platform", "darwin"):
            self.assertIsNone(pg_harness._parent_death_hook(42))

        with mock.patch.object(pg_harness.sys, "platform", "linux"):
            hook = pg_harness._parent_death_hook(42)
        self.assertIsNotNone(hook)

    def test_postgres_cleanup_uses_fast_shutdown(self) -> None:
        postgres = mock.Mock()
        postgres.poll.return_value = None

        pg_harness._stop_postgres(postgres)

        postgres.send_signal.assert_called_once_with(pg_harness.signal.SIGINT)
        postgres.wait.assert_called_once_with(timeout=10)
        postgres.kill.assert_not_called()

    def test_postgres_cleanup_has_a_bounded_kill_fallback(self) -> None:
        postgres = mock.Mock()
        postgres.poll.return_value = None
        postgres.wait.side_effect = [
            pg_harness.subprocess.TimeoutExpired("postgres", 10),
            None,
        ]

        pg_harness._stop_postgres(postgres)

        postgres.send_signal.assert_called_once_with(pg_harness.signal.SIGINT)
        postgres.kill.assert_called_once_with()
        self.assertEqual(
            postgres.wait.call_args_list,
            [mock.call(timeout=10), mock.call(timeout=5)],
        )

    def test_postgres_readiness_uses_a_monotonic_deadline(self) -> None:
        postgres = mock.Mock()
        postgres.poll.return_value = None
        clock = iter((100.0, 100.0, 129.95, 130.0))
        sleeps: list[float] = []
        unavailable = SimpleNamespace(returncode=1)

        with (
            mock.patch.object(pg_harness.subprocess, "run", return_value=unavailable) as run,
            self.assertRaisesRegex(TimeoutError, "within 30 seconds"),
        ):
            pg_harness._wait_for_postgres(
                postgres,
                pg_harness.Path("/pg/bin/pg_isready"),
                pg_harness.Path("/tmp/socket"),
                pg_harness.Path("/tmp/postgres.log"),
                {},
                monotonic=lambda: next(clock),
                sleep=sleeps.append,
            )

        self.assertEqual(run.call_count, 3)
        self.assertEqual(sleeps[0], 0.1)
        self.assertAlmostEqual(sleeps[1], 0.05)


if __name__ == "__main__":
    unittest.main()
