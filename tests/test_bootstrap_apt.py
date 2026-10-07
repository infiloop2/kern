"""Execute bootstrap APT flow with isolated sources and fake network/downloads."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest


BOOTSTRAP = Path(__file__).resolve().parents[1] / "host/bootstrap"
REGIONAL = "http://us-east-1.ec2.archive.ubuntu.com/ubuntu"
ARCHIVE = "https://archive.ubuntu.com/ubuntu"
SECURITY = "https://security.ubuntu.com/ubuntu"


class BootstrapAptTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.sources = self.root / "sources.list.d"
        self.sources.mkdir()
        (self.root / "sources.list").touch()
        (self.root / "os-release").write_text("VERSION_CODENAME=jammy\n")
        self.helper = (BOOTSTRAP / "apt.sh").read_text().replace("/etc/apt/", str(self.root) + "/")
        self.helper = self.helper.replace("/etc/os-release", str(self.root / "os-release"))

    def source(self, fmt="list"):
        path = self.sources / ("ubuntu." + fmt)
        path.write_text(
            f"deb {REGIONAL} jammy main\ndeb http://security.ubuntu.com/ubuntu jammy-security main\n"
            if fmt == "list" else
            f"Types: deb\nURIs: {REGIONAL}\nSuites: jammy\nComponents: main\n\n"
            "Types: deb\nURIs: http://security.ubuntu.com/ubuntu\nSuites: jammy-security\nComponents: main\n"
        )
        return path

    def run_shell(self, body, **env):
        return subprocess.run(["bash", "-c", "set -eu\nunset KERN_APT_MIRRORS KERN_APT_MIRROR_PROBED\n" + self.helper + body],
                              env={**os.environ, **env}, capture_output=True, text=True, timeout=10)

    def test_measured_fastest_mirror_wins_instead_of_fixed_regional_preference(self):
        for fmt in ("list", "sources"):
            for fastest in (REGIONAL, ARCHIVE, SECURITY):
                with self.subTest(fmt=fmt, fastest=fastest):
                    source = self.source(fmt)
                    other = self.sources / "vendor.list"
                    untouched = "deb https://vendor.example/ubuntu jammy main\n# deb http://security.ubuntu.com/ubuntu jammy-security main\n"
                    other.write_text(untouched)
                    result = self.run_shell('''
curl() {
  [[ "$*" == *"--connect-timeout 2 --max-time 6"* ]] || return 99
  if [[ "${!#}" == "$FASTEST/dists/jammy-security/InRelease" ]]; then
    echo '900000 0.1'
  else
    echo '100000 0.9'
  fi
}
apt_get_once() { echo apt-ok; }
apt_get update
''', FASTEST=fastest)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("selected " + fastest, result.stderr)
                    self.assertIn(fastest, source.read_text())
                    self.assertIn("jammy-security", source.read_text())
                    self.assertEqual(other.read_text(), untouched)
                    source.unlink()

    def test_failed_package_download_reselects_and_refreshes_indexes_before_install(self):
        source = self.source()
        result = self.run_shell('''
curl() {
  case "${!#}" in
    https://archive.ubuntu.com/*) echo '900000 0.1';;
    http://us-east-1.ec2.archive.ubuntu.com/*) echo '800000 0.2';;
    *) return 28;;
  esac
}
calls=0
apt_get_once() {
  shift 3
  echo "$*"
  calls=$((calls + 1))
  [[ "$calls" != 2 ]]
}
sleep() { :; }
apt_get update
apt_get install -y sudo
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["update", "install --no-upgrade -y sudo", "update", "install --no-upgrade -y sudo"])
        self.assertIn(REGIONAL, source.read_text())
        self.assertIn("selected " + ARCHIVE, result.stderr)
        self.assertIn("selected " + REGIONAL, result.stderr)

    def test_failed_probes_leave_sources_and_bounded_real_apt_attempts(self):
        source = self.source()
        before = source.read_text()
        result = self.run_shell('''
curl() { return 28; }
apt_get_once() { echo attempt; return 1; }
sleep() { :; }
apt_get update
echo unexpected-success
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), ["attempt"] * 4)
        self.assertEqual(source.read_text(), before)

    def test_git_prebootstrap_skips_existing_git_and_installs_missing_git(self):
        script = (BOOTSTRAP / "user_data_github.sh").read_text()
        block = script[script.index("if ! command -v git"):script.index("rm -rf /tmp/kern-checkout")]
        for installed in (True, False):
            with self.subTest(installed=installed):
                result = self.run_shell(f'\ncommand() {{ return {0 if installed else 1}; }}\napt_get() {{ echo "$*"; }}\n' + block)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), [] if installed else ["update", "install --no-upgrade -y git"])

    def test_failure_trap_restores_timers_without_hiding_original_error(self):
        script = (BOOTSTRAP / "user_data_github.sh").read_text()
        trap = script[script.index("on_exit() {"):script.index("id -u kern-operator")]
        result = self.run_shell('\nsystemctl() { echo "$*"; return 1; }\n' + trap + '\nexit 42')
        self.assertEqual(result.returncode, 42)
        self.assertIn("start apt-daily.timer apt-daily-upgrade.timer", result.stdout)
        self.assertIn("preserving instance", result.stderr)

    def test_browser_child_apt_uses_shared_policy_without_recursing(self):
        script = (BOOTSTRAP / "bootstrap.sh").read_text()
        wrapper = script.split("<<'APT_WRAPPER'\n", 1)[1].split("\nAPT_WRAPPER", 1)[0]
        real_apt = self.root / "real-apt"
        real_apt.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$APT_TEST_LOG"\n')
        real_apt.chmod(0o700)
        helper = self.root / "apt.sh"
        helper.write_text(self.helper.replace("/usr/bin/apt-get", str(real_apt)))
        entrypoint = self.root / "apt-get"
        entrypoint.write_text(wrapper.replace("/opt/kern-host/host/bootstrap/apt.sh", str(helper)))
        entrypoint.chmod(0o700)
        log = self.root / "calls"
        result = subprocess.run(["sh", "-c", "apt-get update && apt-get install -y --no-install-recommends libnss3"],
                                env={**os.environ, "PATH": str(self.root) + ":" + os.environ["PATH"], "APT_TEST_LOG": str(log)},
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text().splitlines()
        self.assertEqual(len(calls), 3)
        self.assertIn("--download-only", calls[1])
        self.assertIn("--no-download", calls[2])
        self.assertIn("install --no-upgrade -y --no-install-recommends libnss3", calls[1])
        self.assertTrue(all("APT::Update::Error-Mode=any" in call for call in calls))
