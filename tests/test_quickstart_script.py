"""Exercise the piped bootstrap with fake Git and a real controlling terminal."""
from __future__ import annotations

import fcntl
import os
from pathlib import Path
import pty
import shlex
import subprocess
import sys
import tempfile
import termios
import unittest

ROOT = Path(__file__).resolve().parents[1]


class QuickstartScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.checkout = self.root / "source with spaces"
        tools = self.tools = self.root / "tools"
        tools.mkdir()
        (tools / "python3").write_text("#!/bin/sh\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        (tools / "python3").chmod(0o700)
        for name in ("limactl", "ssh", "ssh-keygen", "scp", "qemu-system-x86_64", "qemu-system-aarch64", "qemu-img"):
            path = tools / name
            path.write_text("#!/bin/sh\n" + ("echo limactl version 2.2.1\n" if name == "limactl" else "exit 0\n"))
            path.chmod(0o700)
        fake_git = tools / "git"
        fake_git.write_text("#!" + sys.executable + '\n' + '''import sys, os
from pathlib import Path
args = sys.argv[1:]
if args == ["--version"]:
    print("git version 2.0")
elif args[0] == "clone":
    target = Path(args[-1])
    target.mkdir(exist_ok=True)
    (target / ".git").mkdir()
    (target / "quickstart.py").write_text("import os; from pathlib import Path; assert os.isatty(0); Path('ran-setup').write_text('yes')")
elif args[0] == "-C":
    if args[2:] == ["rev-parse", "--is-inside-work-tree"]:
        if not (Path(args[1]) / ".git").exists():
            sys.exit(1)
        print("true")
    else:
        print(os.environ.get("KERN_TEST_REMOTE", "https://github.com/infiloop2/kern.git"))
elif args == ["rev-parse", "HEAD"]:
    print("a" * 40)
else:
    raise RuntimeError(args)
''')
        fake_git.chmod(0o700)
        self.env = {**os.environ, "PATH": str(tools) + os.pathsep + "/usr/bin:/bin", "KERN_SOURCE_DIR": str(self.checkout)}

    def run_script(self, *, terminal: bool = True, answer: str | None = None) -> subprocess.CompletedProcess[str]:
        if terminal:
            master, slave = pty.openpty()
            def session() -> None:
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            try:
                if answer is not None:
                    os.write(master, (answer + "\n").encode())
                return subprocess.run(["bash"], input=(ROOT / "quickstart.sh").read_text(), text=True, capture_output=True, env=self.env, preexec_fn=session, pass_fds=(slave,), timeout=30)
            finally:
                os.close(master)
                os.close(slave)
        return subprocess.run(["bash"], input=(ROOT / "quickstart.sh").read_text(), text=True, capture_output=True, env=self.env, start_new_session=True, timeout=30)

    def test_piped_script_clones_source_and_hands_python_a_terminal(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.checkout / "ran-setup").read_text(), "yes")
        self.assertIn("a" * 40, result.stdout)

    def test_clones_into_an_existing_empty_directory(self) -> None:
        self.checkout.mkdir()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.checkout / "ran-setup").read_text(), "yes")

    def test_rerun_preserves_existing_checkout_edits(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        (self.checkout / "local-change").write_text("keep")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("without changing its source", result.stdout)
        self.assertEqual((self.checkout / "local-change").read_text(), "keep")

    def test_pre_quickstart_checkout_gets_update_guidance_without_changes(self) -> None:
        self.checkout.mkdir()
        (self.checkout / ".git").mkdir()
        (self.checkout / "local-change").write_text("keep")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not contain quickstart.py", result.stderr)
        self.assertIn("preserving local edits", result.stderr)
        self.assertIn("KERN_SOURCE_DIR", result.stderr)
        self.assertNotIn("3/3 Starting Kern", result.stdout)
        self.assertEqual((self.checkout / "local-change").read_text(), "keep")
        self.assertFalse((self.checkout / "ran-setup").exists())

    def test_git_worktree_is_reused_without_changing_source(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        (self.checkout / ".git").rmdir()
        (self.checkout / ".git").write_text("gitdir: /existing/repo/.git/worktrees/kern")
        (self.checkout / "local-change").write_text("keep")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.checkout / "local-change").read_text(), "keep")

    def test_unrelated_directory_is_not_overwritten_or_executed(self) -> None:
        self.checkout.mkdir()
        (self.checkout / "quickstart.py").write_text("raise RuntimeError('must not execute')")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a Kern checkout", result.stderr)
        self.assertFalse((self.checkout / ".git").exists())

    def test_no_terminal_fails_before_clone(self) -> None:
        result = self.run_script(terminal=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("interactive terminal", result.stderr)
        self.assertFalse(self.checkout.exists())

    def dependency_fixture(self) -> Path:
        # An old Python remains selected until the consented package install.
        marker = self.root / "installed"
        python = self.tools / "python3"
        python.write_text("#!/bin/sh\nif [ ! -f " + shlex.quote(str(marker)) + " ]; then exit 1; fi\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
        brew = self.tools / "brew"
        brew.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" > " + shlex.quote(str(marker)) + "\n")
        brew.chmod(0o700)
        return marker

    def test_dependency_install_requires_consent_and_rechecks_before_setup(self) -> None:
        marker = self.dependency_fixture()
        uname = self.tools / "uname"
        uname.write_text('#!/bin/sh\nif [ "$1" = "-s" ]; then echo Darwin; else echo arm64; fi\n')
        uname.chmod(0o700)
        result = self.run_script(answer="y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text().strip(), "install python")
        self.assertTrue((self.checkout / "ran-setup").exists())
        self.assertIn("Dependencies ready", result.stdout)

    def test_decline_and_empty_answer_install_nothing(self) -> None:
        marker = self.dependency_fixture()
        for answer in ("n", ""):
            with self.subTest(answer=answer):
                result = self.run_script(answer=answer)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(marker.exists())
                self.assertFalse(self.checkout.exists())

    def test_failed_install_does_not_clone_or_provision(self) -> None:
        self.dependency_fixture()
        (self.tools / "brew").write_text("#!/bin/sh\nexit 1\n")
        result = self.run_script(answer="y")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.checkout.exists())

    def test_successful_installer_without_supported_dependency_stops(self) -> None:
        self.dependency_fixture()
        (self.tools / "brew").write_text("#!/bin/sh\nexit 0\n")
        result = self.run_script(answer="y")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Still unavailable", result.stderr)
        self.assertFalse(self.checkout.exists())

    def test_unsupported_lima_stops_before_clone(self) -> None:
        (self.tools / "limactl").write_text("#!/bin/sh\necho limactl version 3.0.0\n")
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs Lima >=2.2 and <3", result.stderr)
        self.assertFalse(self.checkout.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "apt path is Linux-only")
    def test_apt_install_is_prompted_and_rechecked(self) -> None:
        marker = self.dependency_fixture()
        (self.tools / "brew").unlink()
        log = self.root / "sudo.log"
        sudo = self.tools / "sudo"
        sudo.write_text("#!/bin/sh\necho \"$*\" >> " + shlex.quote(str(log)) + "\ncase \"$*\" in 'apt-get install -y python3') touch " + shlex.quote(str(marker)) + ";; esac\n")
        sudo.chmod(0o700)
        apt = self.tools / "apt-get"
        apt.write_text("#!/bin/sh\nexit 0\n")
        apt.chmod(0o700)
        apt_cache = self.tools / "apt-cache"
        apt_cache.write_text("#!/bin/sh\necho '  Candidate: 3.12.3-0ubuntu2'\n")
        apt_cache.chmod(0o700)
        declined = self.run_script(answer="n")
        self.assertNotEqual(declined.returncode, 0)
        self.assertFalse(log.exists())
        result = self.run_script(answer="y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().splitlines(), ["apt-get update", "apt-get install -y python3"])
        self.assertTrue((self.checkout / "ran-setup").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "apt path is Linux-only")
    def test_unsupported_apt_python_stops_before_prompt_or_sudo(self) -> None:
        self.dependency_fixture()
        (self.tools / "brew").unlink()
        log = self.root / "sudo.log"
        for name, body in {
            "sudo": "echo called > " + shlex.quote(str(log)),
            "apt-get": "exit 0",
        }.items():
            command = self.tools / name
            command.write_text("#!/bin/sh\n" + body + "\n")
            command.chmod(0o700)
        apt_cache = self.tools / "apt-cache"
        for candidate in ("3.10.6-1", "(none)"):
            with self.subTest(candidate=candidate):
                apt_cache.write_text("#!/bin/sh\necho '  Candidate: " + candidate + "'\n")
                apt_cache.chmod(0o700)
                result = self.run_script(answer="y")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("apt does not advertise Python 3.11", result.stderr)
                self.assertNotIn("Administrator permission", result.stdout)
                self.assertFalse(log.exists())
                self.assertFalse(self.checkout.exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux Lima archive path")
    def test_lima_checksum_failure_never_extracts_or_provisions(self) -> None:
        (self.tools / "limactl").unlink()
        log = self.root / "sudo.log"
        for name, body in {
            "sudo": "echo \"$*\" >> " + shlex.quote(str(log)),
            "apt-get": "exit 0",
            "curl": 'while [ "$1" != "-o" ]; do shift; done; printf corrupt > "$2"',
        }.items():
            command = self.tools / name
            command.write_text("#!/bin/sh\n" + body + "\n")
            command.chmod(0o700)
        result = self.run_script(answer="y")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum verification failed", result.stderr)
        self.assertFalse(log.exists())
        self.assertFalse(self.checkout.exists())

    def test_existing_checkout_accepts_official_remote_spellings_only(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        for remote in ("https://github.com/infiloop2/kern", "git@github.com:infiloop2/kern.git", "ssh://git@github.com/infiloop2/kern.git"):
            with self.subTest(remote=remote):
                self.env["KERN_TEST_REMOTE"] = remote
                self.assertEqual(self.run_script().returncode, 0)
        (self.checkout / "ran-setup").unlink()
        self.env["KERN_TEST_REMOTE"] = "https://github.com/someone-else/kern.git"
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertFalse((self.checkout / "ran-setup").exists())

    def test_missing_scp_prompts_for_openssh_before_clone(self) -> None:
        # No host commands leak into discovery; only scp is missing.
        (self.tools / "scp").unlink()
        self.env["PATH"] = str(self.tools)
        (self.tools / "bash").symlink_to("/bin/bash")
        log = self.root / "brew.log"
        for name, body in {
            "uname": "echo Linux",
            "curl": "exit 0",
            "brew": "printf '%s\\n' \"$*\" > " + shlex.quote(str(log)) + "; printf '#!/bin/sh\\nexit 0\\n' > " + shlex.quote(str(self.tools / "scp")) + "; /bin/chmod 700 " + shlex.quote(str(self.tools / "scp")),
            "qemu-system-Linux": "exit 0",
        }.items():
            command = self.tools / name
            command.write_text("#!/bin/sh\n" + body + "\n")
            command.chmod(0o700)
        result = self.run_script(answer="y")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(log.read_text().strip(), "install openssh")
        self.assertTrue((self.checkout / "ran-setup").exists())
