"""Quick-start boundary tests; fake providers never create VMs or contact accounts."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import quickstart as setup
from host.cli import lifecycle_lima as lima
from host.config import ConfigError


def running(port: int = 60022) -> lima.LimaInstance:
    return lima.LimaInstance(name="kern-test", status="Running", ssh_local_port=port, dir=None)


class QuickstartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        setup.save(self.home / "operator_ed25519", "private key")
        setup.save(self.home / "operator_ed25519.pub", "ssh-ed25519 AAAATEST")
        # Model key derivation at the subprocess boundary; Python CI does not
        # include OpenSSH. Keep real subprocesses for the printed shell command.
        real_run = subprocess.run

        def run(command, *args, **kwargs):
            if command[:2] == ["ssh-keygen", "-y"]:
                return subprocess.CompletedProcess(command, 0, "ssh-ed25519 AAAATEST\n", "")
            return real_run(command, *args, **kwargs)

        keygen = patch.object(setup.subprocess, "run", side_effect=run)
        keygen.start()
        self.addCleanup(keygen.stop)
        setup.save(self.home / "deploy-complete", "complete\n")

    def test_case_equivalent_names_have_distinct_credential_directories(self) -> None:
        with patch.object(setup.Path, "home", return_value=self.home):
            upper = setup.state_dir("Work")
            lower = setup.state_dir("work")
        self.assertNotEqual(str(upper).casefold(), str(lower).casefold())
        setup.save(upper / "initial-password.txt", "first")
        setup.save(lower / "initial-password.txt", "second")
        self.assertEqual((upper / "initial-password.txt").read_text(), "first")

    def test_up_running_host_never_redeploys_or_changes_credentials(self) -> None:
        current = lima.LimaInventory(instance=running(), disks={})
        with patch.object(setup, "inventory", return_value=current), patch.object(setup, "native") as native, patch.object(setup, "open_ui") as open_ui:
            setup.setup_local("test", self.home, browser=False)
        native.assert_not_called()
        open_ui.assert_called_once()
        self.assertFalse((self.home / "initial-password.txt").exists())

    def test_printed_stop_command_works_outside_checkout_with_spaces(self) -> None:
        checkout = self.home / "checkout with spaces"
        module = checkout / "host" / "cli"
        module.mkdir(parents=True)
        (module / "stop.py").write_text("from pathlib import Path; Path('stopped').write_text('yes')")
        current = lima.LimaInventory(instance=running(), disks={})
        with patch.object(setup, "ROOT", checkout), patch.object(setup, "inventory", return_value=current), patch.object(setup, "open_ui"), contextlib.redirect_stdout(io.StringIO()) as output:
            setup.setup_local("test", self.home, browser=False)
        command = next(line.removeprefix("Stop the VM: ") for line in output.getvalue().splitlines() if line.startswith("Stop the VM: "))
        result = subprocess.run(["/bin/bash", "-c", command], cwd=self.home, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((checkout / "stopped").read_text(), "yes")

    def test_reopen_password_shown_only_in_terminal_without_changing_file(self) -> None:
        password_file = self.home / "initial-password.txt"
        setup.save(password_file, "saved-secret\n")
        current = lima.LimaInventory(instance=running(), disks={})
        for terminal in (True, False):
            with self.subTest(terminal=terminal), contextlib.redirect_stdout(io.StringIO()) as output:
                with patch.object(output, "isatty", return_value=terminal), patch.object(setup, "inventory", return_value=current), patch.object(setup, "native") as native, patch.object(setup, "open_ui"):
                    setup.setup_local("test", self.home, browser=False)
                self.assertEqual("saved-secret" in output.getvalue(), terminal)
                self.assertIn("then delete this password file", output.getvalue())
                self.assertIn("later password changes are not saved here", output.getvalue())
                native.assert_not_called()
                self.assertEqual(password_file.read_text(), "saved-secret\n")

    def test_missing_password_guides_reset_without_recreating_file(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            setup.show_password_guidance(self.home)
        self.assertIn("password file not found", output.getvalue())
        self.assertIn("Use the password you saved", output.getvalue())
        self.assertIn("host.cli.reconfigure --help", output.getvalue())
        self.assertIn("all operator endpoints", output.getvalue())
        self.assertNotIn("host.cli.recover", output.getvalue())
        self.assertFalse((self.home / "initial-password.txt").exists())

    def test_password_guidance_rejects_symlink_and_public_file(self) -> None:
        target = self.home / "secret"
        setup.save(target, "do-not-print")
        password_file = self.home / "initial-password.txt"
        password_file.symlink_to(target)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(ConfigError):
                setup.show_password_guidance(self.home)
            password_file.unlink()
            password_file.write_text("do-not-print")
            password_file.chmod(0o644)
            with self.assertRaises(ConfigError):
                setup.show_password_guidance(self.home)
        self.assertNotIn("do-not-print", output.getvalue())

    def test_up_stopped_host_only_starts(self) -> None:
        current = lima.LimaInventory(instance=lima.LimaInstance(name="test", status="Stopped", ssh_local_port=60022, dir=None), disks={})
        with patch.object(setup, "inventory", return_value=current), patch.object(setup, "native") as native, patch.object(setup, "open_ui"):
            setup.setup_local("test", self.home, browser=False)
        native.assert_called_once_with("start", "test")

    def test_preserved_disks_require_explicit_recovery(self) -> None:
        current = lima.LimaInventory(instance=None, disks={
            role: lima.LimaDisk(name=lima._disk_name("test", role), instance="")
            for role in ("admin", "agent")
        })
        with patch.object(setup, "inventory", return_value=current), patch.object(setup, "native") as native:
            with self.assertRaisesRegex(ConfigError, "recover"):
                setup.setup_local("test", self.home, browser=False)
        native.assert_not_called()

    def test_partial_first_deploy_reports_exact_manual_cleanup_without_mutation(self) -> None:
        disk = lima._disk_name("test", "admin")
        current = lima.LimaInventory(instance=None, disks={"admin": lima.LimaDisk(name=disk, instance="")})
        with patch.object(setup, "inventory", return_value=current), patch.object(setup, "native") as native, patch.object(setup, "ensure_key") as ensure_key:
            with self.assertRaisesRegex(ConfigError, f"limactl disk delete {disk}") as error:
                setup.setup_local("test", self.home, browser=False)
        self.assertIn("once both admin and agent disks are present", str(error.exception))
        self.assertIn("unused disks from a failed first deploy", str(error.exception))
        native.assert_not_called()
        ensure_key.assert_not_called()

    def test_fresh_deploy_private_password_and_hash_only(self) -> None:
        empty = lima.LimaInventory(instance=None, disks={})
        current = lima.LimaInventory(instance=running(), disks={})
        with patch.object(setup, "inventory", side_effect=[empty, current]), patch.object(setup, "native") as native, patch.object(setup, "open_ui"), patch.object(setup.secrets, "token_urlsafe", return_value="private-password"), contextlib.redirect_stdout(io.StringIO()) as output:
            setup.setup_local("test", self.home, browser=False)
        self.assertNotIn("private-password", output.getvalue())
        self.assertNotIn("private-password", str(native.call_args))
        self.assertIn(hashlib.sha256(b"private-password").hexdigest(), str(native.call_args))
        self.assertEqual((self.home / "initial-password.txt").stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.home / "deploy-complete").is_file())

    def test_private_paths_reject_symlinks_and_public_secrets(self) -> None:
        target = self.home / "target"
        target.write_text("keep")
        link = self.home / "link"
        link.symlink_to(target)
        with self.assertRaises(ConfigError):
            setup.save(link, "overwrite")
        target.chmod(0o644)
        with self.assertRaises(ConfigError):
            setup.private_file(target)
        self.assertEqual(target.read_text(), "keep")

    def test_restored_key_pair_mismatch_blocks_deploy_before_password_creation(self) -> None:
        (self.home / "operator_ed25519.pub").write_text("ssh-ed25519 AAAAwrong restored-key\n")
        empty = lima.LimaInventory(instance=None, disks={})
        with patch.object(setup, "inventory", return_value=empty), patch.object(setup, "native") as native:
            with self.assertRaisesRegex(ConfigError, "public key does not match"):
                setup.setup_local("test", self.home, browser=False)
        native.assert_not_called()
        self.assertFalse((self.home / "initial-password.txt").exists())

    def test_matching_restored_keys_preserve_comment_and_key_material(self) -> None:
        public = self.home / "operator_ed25519.pub"
        saved = " ".join(public.read_text().split()[:2]) + " restored comment\n"
        public.write_text(saved)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(setup.ensure_key(self.home), self.home / "operator_ed25519")
        self.assertEqual(public.read_text(), saved)

    def test_control_socket_stays_short_with_long_home_and_runtime_paths(self) -> None:
        with patch.dict(os.environ, {"TMPDIR": "/var/folders/" + "x" * 70, "XDG_RUNTIME_DIR": "/run/" + "x" * 100}), patch.object(setup, "private_dir", side_effect=lambda path: path):
            first = setup.control_socket(self.home / ("long-home" * 20))
            second = setup.control_socket(self.home / "other-host")
        self.assertLess(len(os.fsencode(first)) + 17, 104)
        self.assertNotEqual(first, second)

    def test_open_uses_fresh_port_and_trusted_host_key(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "ssh-ed25519 AAAATEST guest\n")
        with patch.object(setup.subprocess, "run", return_value=completed) as run:
            first = setup.ssh_base(self.home, running(60022))
            second = setup.ssh_base(self.home, running(60123))
        self.assertIn("60022", first)
        self.assertIn("60123", second)
        self.assertIn("StrictHostKeyChecking=yes", second)
        self.assertIn("[127.0.0.1]:60123", (self.home / "known_hosts").read_text())
        self.assertEqual(run.call_args.args[0][:3], ["limactl", "shell", "kern-test"])

    def test_failed_native_never_opens_browser(self) -> None:
        empty = lima.LimaInventory(instance=None, disks={})
        with patch.object(setup, "inventory", return_value=empty), patch.object(setup, "native", side_effect=ConfigError("failed")), patch.object(setup, "open_ui") as open_ui, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ConfigError):
                setup.setup_local("test", self.home, browser=True)
        open_ui.assert_not_called()

    def test_failed_deploy_does_not_claim_a_concurrent_native_host(self) -> None:
        empty = lima.LimaInventory(instance=None, disks={})
        other_host = lima.LimaInventory(instance=running(), disks={})
        # A stale completion from an earlier deleted host must also be cleared.
        with patch.object(setup, "inventory", side_effect=[empty, other_host, other_host]), patch.object(setup, "native", side_effect=ConfigError("another lifecycle operation")) as native, patch.object(setup, "open_ui", side_effect=subprocess.CalledProcessError(255, "ssh")) as open_ui, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ConfigError, "another lifecycle operation"):
                setup.setup_local("test", self.home, browser=False)
            self.assertFalse((self.home / "deploy-complete").exists())
            with self.assertRaises(subprocess.CalledProcessError):
                setup.setup_local("test", self.home, browser=False)
        self.assertEqual(native.call_count, 1)
        open_ui.assert_called_once()
        self.assertFalse((self.home / "deploy-complete").exists())
        self.assertTrue((self.home / "initial-password.txt").is_file())

    def test_interrupted_completion_recovers_after_authenticated_connection(self) -> None:
        empty = lima.LimaInventory(instance=None, disks={})
        current = lima.LimaInventory(instance=running(), disks={})
        original_save = setup.save

        def interrupt_receipt(path: Path, text: str) -> None:
            if path.name == "deploy-complete":
                raise KeyboardInterrupt
            original_save(path, text)

        with patch.object(setup, "inventory", return_value=empty), patch.object(setup, "native"), patch.object(setup, "save", side_effect=interrupt_receipt):
            with self.assertRaises(KeyboardInterrupt):
                setup.setup_local("test", self.home, browser=False)
        password = (self.home / "initial-password.txt").read_text()
        self.assertFalse((self.home / "deploy-complete").exists())
        with patch.object(setup, "inventory", return_value=current), patch.object(setup, "native") as native, patch.object(setup, "open_ui", return_value="http://127.0.0.1:8000") as open_ui, patch.object(setup, "verify_setup_password") as verify:
            setup.setup_local("test", self.home, browser=False)
        native.assert_not_called()
        open_ui.assert_called_once()
        verify.assert_called_once_with(self.home, "http://127.0.0.1:8000")
        self.assertTrue((self.home / "deploy-complete").is_file())
        self.assertEqual((self.home / "initial-password.txt").read_text(), password)

    def test_same_ssh_key_with_different_admin_password_cannot_claim_setup(self) -> None:
        (self.home / "deploy-complete").unlink()
        setup.save(self.home / "initial-password.txt", "never-installed\n")
        current = lima.LimaInventory(instance=running(), disks={})
        error = setup.urllib.error.HTTPError("http://127.0.0.1:8000/v1/login", 401, "Unauthorized", {}, None)
        with patch.object(setup, "inventory", return_value=current), patch.object(setup, "native") as native, patch.object(setup, "open_ui", return_value="http://127.0.0.1:8000"), patch.object(setup.urllib.request, "build_opener") as build, contextlib.redirect_stdout(io.StringIO()) as output:
            build.return_value.open.side_effect = error
            with patch.object(output, "isatty", return_value=True), self.assertRaisesRegex(ConfigError, "could not verify the saved password"):
                setup.setup_local("test", self.home, browser=False)
        self.assertNotIn("never-installed", output.getvalue())
        self.assertNotIn("Kern is ready", output.getvalue())
        self.assertFalse((self.home / "deploy-complete").exists())
        self.assertEqual((self.home / "initial-password.txt").read_text(), "never-installed\n")
        native.assert_not_called()

    def test_interrupted_password_check_logs_out_and_keeps_secret_out_of_url(self) -> None:
        setup.save(self.home / "initial-password.txt", "saved-secret\n")
        with patch.object(setup.urllib.request, "build_opener") as build:
            setup.verify_setup_password(self.home, "http://127.0.0.1:8000")
        login, logout = [call.args[0] for call in build.return_value.open.call_args_list]
        self.assertEqual(login.full_url, "http://127.0.0.1:8000/v1/login")
        self.assertEqual(json.loads(login.data), {"password": "saved-secret"})
        self.assertEqual(logout.full_url, "http://127.0.0.1:8000/v1/logout")
        self.assertEqual(logout.get_header("X-kern-csrf"), "1")

    def test_unrecognized_stopped_host_is_not_started(self) -> None:
        (self.home / "deploy-complete").unlink()
        current = lima.LimaInventory(instance=lima.LimaInstance(name="test", status="Stopped", ssh_local_port=60022, dir=None), disks={})
        with patch.object(setup, "inventory", return_value=current), patch.object(setup, "native") as native, patch.object(setup, "open_ui") as open_ui:
            with self.assertRaisesRegex(ConfigError, "no completed quick-start setup"):
                setup.setup_local("test", self.home, browser=False)
        native.assert_not_called()
        open_ui.assert_not_called()

    def test_native_keeps_provider_phase_deadlines(self) -> None:
        with patch.object(setup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "{}")) as run:
            self.assertEqual(setup.native("deploy", "test"), {})
        self.assertNotIn("timeout", run.call_args.kwargs)

    def test_lima_home_change_rejected_before_mutation(self) -> None:
        with patch.dict(os.environ, {"LIMA_HOME": "/other"}), patch.object(setup, "setup_local") as local, contextlib.redirect_stderr(io.StringIO()):
            home = setup.state_dir("test", self.home)
            setup.save(home / "lima-home.txt", "/original\n")
            self.assertEqual(setup.run(name="test", state_root=self.home, browser=False), 1)
        local.assert_not_called()

    def test_invalid_initial_lima_home_does_not_block_corrected_selection(self) -> None:
        other = lima.LimaInventory(instance=running(), disks={})
        root = self.home / "state"
        with patch.object(setup, "inventory", return_value=other), patch.dict(os.environ, {"LIMA_HOME": "/mistaken"}), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(setup.run(name="test", state_root=root, browser=False), 1)
        home = setup.state_dir("test", root)
        self.assertFalse((home / "lima-home.txt").exists())
        with patch.dict(os.environ, {"LIMA_HOME": "/corrected"}), patch.object(setup, "setup_local") as local:
            self.assertEqual(setup.run(name="test", state_root=root, browser=False), 0)
        local.assert_called_once()

    def test_failed_deployment_retains_validated_lima_home(self) -> None:
        empty = lima.LimaInventory(instance=None, disks={})
        with patch.dict(os.environ, {"LIMA_HOME": "/validated"}), patch.object(setup, "inventory", return_value=empty), patch.object(setup, "native", side_effect=ConfigError("deploy failed")):
            with self.assertRaisesRegex(ConfigError, "deploy failed"):
                setup.setup_local("test", self.home, browser=False)
        self.assertEqual((self.home / "lima-home.txt").read_text(), "/validated\n")
