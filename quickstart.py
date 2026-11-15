"""Local first-run setup using the existing Python lifecycle commands."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import shlex
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from typing import Any, Iterator

from host.cli import lifecycle_lima as lima
from host.cli.lifecycle_types import LifecycleCommand
from host.config import ConfigError

ROOT = Path(__file__).resolve().parent


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ConfigError(f"expected a user-owned directory: {path}")
    path.chmod(0o700)
    return path


def private_file(path: Path) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ConfigError(f"expected a user-owned regular file: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ConfigError(f"private file permissions required: chmod 600 {path}")


def save(path: Path, text: str) -> None:
    if path.exists() or path.is_symlink():
        private_file(path)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def state_dir(name: str, base: Path | None = None) -> Path:
    base = base if base is not None else Path.home() / ".local/share/kern"
    if not base.is_absolute():
        raise ConfigError("local state directory must be absolute")
    private_dir(base)
    return private_dir(private_dir(base / "hosts") / lima._local_key(name))


@contextlib.contextmanager
def setup_lock(home: Path) -> Iterator[None]:
    # Separate from the provider's own OperationLock, which each child acquires.
    path = home / "quickstart.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w") as stream:
        private_file(path)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ConfigError("another quick-start is working on this host") from None
        yield


def native(verb: str, name: str, extra: list[str] | None = None) -> dict[str, Any]:
    """Preserve native JSON and stderr contracts; report liveness during waits."""
    command = [sys.executable, "-m", f"host.cli.{verb}", "--provider", "lima", "--agent-name", name, *(extra or [])]
    finished = threading.Event()
    started = time.monotonic()

    def progress() -> None:
        while not finished.wait(20):
            print(f"Kern {verb}: {int(time.monotonic() - started)}s elapsed…", file=sys.stderr, flush=True)

    reporter = threading.Thread(target=progress, daemon=True)
    reporter.start()
    try:
        result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE, text=True)
    finally:
        finished.set()
        reporter.join()
    if result.returncode:
        raise ConfigError(f"{verb} failed; use the lifecycle error and recovery guidance above")
    return json.loads(result.stdout)


def inventory(name: str) -> lima.LimaInventory:
    config = lima.build_lima_config(LifecycleCommand(mode="deploy", agent_name=name, provider="lima"))
    lima._preflight(config)
    return lima._inspect(config)


def ensure_key(home: Path) -> Path:
    key = home / "operator_ed25519"
    if key.exists() or key.is_symlink():
        private_file(key)
        if not key.with_suffix(".pub").is_file():
            raise ConfigError(f"public key missing beside {key}; restore it before continuing")
        print("Reusing the SSH key pair saved by quick-start.")
    else:
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "kern-local", "-f", str(key)], check=True)
        print("Created a dedicated Ed25519 SSH key pair for this Kern host (no passphrase).")
    derived = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)], check=True, capture_output=True, text=True).stdout.split()
    if key.with_suffix(".pub").read_text().split()[:2] != derived[:2]:
        raise ConfigError(f"SSH public key does not match {key}; restore the matching .pub file before deploying")
    print(f"Private SSH key: {key}\nPublic SSH key: {key.with_suffix('.pub')}")
    print("The private key stays on this computer; deployment installs the public key in the VM for operator access.")
    print("Quick-start reuses this key to open the local browser connection through SSH. Keep both key files and back them up securely.")
    return key


def control_socket(home: Path) -> Path:
    # OpenSSH appends a temporary suffix when binding its control socket.
    # macOS TMPDIR and user-supplied XDG paths can exceed sockaddr_un limits.
    base = private_dir(Path("/tmp") / f"kern-ssh-{os.getuid()}")
    digest = hashlib.sha256(str(home).encode()).hexdigest()[:16]
    return base / digest


def close_tunnel(home: Path) -> None:
    control = control_socket(home)
    if control.exists():
        subprocess.run(["ssh", "-F", "/dev/null", "-S", str(control), "-O", "exit", "kern-operator@127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        control.unlink(missing_ok=True)
    (home / "tunnel.json").unlink(missing_ok=True)


def ssh_base(home: Path, instance: lima.LimaInstance) -> list[str]:
    if instance.status != "Running" or not instance.ssh_local_port:
        raise ConfigError("host is not running; rerun python3 quickstart.py")
    key = home / "operator_ed25519"
    if not key.is_file():
        raise ConfigError("quick-start has no operator key for the existing host; use its original operator setup")
    private_file(key)
    # Trust the guest host key via Lima's authenticated local management channel,
    # not ssh-keyscan or StrictHostKeyChecking=no.
    result = subprocess.run(["limactl", "shell", instance.name, "cat", "/etc/ssh/ssh_host_ed25519_key.pub"], check=True, capture_output=True, text=True, timeout=30)
    parts = result.stdout.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519" or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=" for c in parts[1]):
        raise ConfigError("could not read the VM SSH host key")
    known = home / "known_hosts"
    save(known, f"[127.0.0.1]:{instance.ssh_local_port} {parts[0]} {parts[1]}\n")
    return ["ssh", "-F", "/dev/null", "-i", str(key), "-p", str(instance.ssh_local_port), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", f"UserKnownHostsFile={known}", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]


def wait_http(url: str, *, timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    # Loopback access must not accidentally use a machine's HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    while True:
        try:
            with opener.open(url, timeout=3) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            pass
        if time.monotonic() >= deadline:
            raise ConfigError("Kern did not answer locally; inspect the VM's kern-admin service logs, then rerun python3 quickstart.py")
        time.sleep(1)


def open_ui(home: Path, instance: lima.LimaInstance, *, browser: bool) -> str:
    command = ssh_base(home, instance)
    close_tunnel(home)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command += ["-M", "-S", str(control_socket(home)), "-fNT", "-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{port}:127.0.0.1:7443", "kern-operator@127.0.0.1"]
    subprocess.run(command, check=True, timeout=30)
    url = f"http://127.0.0.1:{port}"
    save(home / "tunnel.json", json.dumps({"url": url, "ssh_port": instance.ssh_local_port}))
    wait_http(url)
    print(f"Open Kern: {url}")
    if browser and not webbrowser.open(url):
        print("Open that address in your browser.")
    return url


def show_password_guidance(home: Path) -> None:
    password_file = home / "initial-password.txt"
    if password_file.exists() or password_file.is_symlink():
        private_file(password_file)
        if sys.stdout.isatty():
            password = password_file.read_text().rstrip("\n")
            print(f"Initial admin password: {password}")
        print(f"Initial password file: {password_file} (later password changes are not saved here).")
        print("Save the password in your password manager, then delete this password file.")
    else:
        print(f"Initial password file not found: {password_file}. Use the password you saved.")
        print("If you lost it, generate a new password with python3 -m host.cli.generate_password.")
        print("Reset it using python3 -m host.cli.reconfigure --help from your Kern checkout; supply the new password hash and all operator endpoints you want to retain.")


def verify_setup_password(home: Path, url: str) -> None:
    password_file = home / "initial-password.txt"
    if not password_file.is_file():
        raise ConfigError("cannot confirm interrupted setup without initial-password.txt; use your original operator credentials and the native lifecycle commands")
    private_file(password_file)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    body = json.dumps({"password": password_file.read_text().rstrip("\n")}).encode()
    try:
        with opener.open(urllib.request.Request(f"{url}/v1/login", data=body, headers={"Content-Type": "application/json"}), timeout=10):
            pass
        with opener.open(urllib.request.Request(f"{url}/v1/logout", data=b"", headers={"X-Kern-Csrf": "1"}), timeout=10):
            pass
    except (OSError, urllib.error.URLError) as exc:
        raise ConfigError("could not verify the saved password for interrupted setup; use your original admin password, or reset it with python3 -m host.cli.reconfigure --help") from exc


def remember_lima_home(home: Path) -> None:
    marker = home / "lima-home.txt"
    if not marker.exists():
        lima_home = str(Path(os.environ.get("LIMA_HOME", str(Path.home() / ".lima"))).expanduser().resolve())
        save(marker, lima_home + "\n")


def setup_local(name: str, home: Path, *, browser: bool) -> None:
    print("Checking Lima and local host state…", file=sys.stderr, flush=True)
    current = inventory(name)
    completed = home / "deploy-complete"
    if current.instance is None:
        # Reuse the provider's exact attachment/partial-disk recovery guidance.
        # Never suggest recovery of a missing pair or delete data automatically.
        lima._validate_lima_preflight(LifecycleCommand(mode="deploy", agent_name=name, provider="lima"), current)
        # Pin the validated home before deployment can create partial resources.
        remember_lima_home(home)
        print(f"Creating {name}: 2 CPUs, 2 GiB RAM and three 16 GiB disks. First setup may take several minutes.", flush=True)
        # Credentials are prepared before native deploy takes its lifecycle lock.
        # Only a successful deploy makes them eligible for automatic start.
        completed.unlink(missing_ok=True)
        key = ensure_key(home)
        password = secrets.token_urlsafe(24)
        save(home / "initial-password.txt", password + "\n")
        print(f"Initial admin password saved privately to {home / 'initial-password.txt'}. Read that file to recover the initial password.")
        native("deploy", name, ["--admin-password-sha256", hashlib.sha256(password.encode()).hexdigest(), "--operator-ssh-public-key", key.with_suffix(".pub").read_text().strip()])
        save(completed, "complete\n")
    else:
        # No bootstrap or credential replacement when retrying ordinary startup.
        if not (home / "operator_ed25519").is_file():
            raise ConfigError("existing host has no quick-start key; use its original credentials")
        if current.instance.status != "Running":
            if not completed.is_file():
                raise ConfigError("existing host has no completed quick-start setup; inspect the deploy output and start it with its original setup before retrying")
            print("Starting your existing Kern host…", flush=True)
            native("start", name)
    instance = inventory(name).instance
    if instance is None:
        raise ConfigError("host was not created; inspect the lifecycle output above")
    print("Connecting the local browser through SSH…", flush=True)
    url = open_ui(home, instance, browser=browser)
    # A deploy can finish before an interruption prevents saving its receipt.
    # Confirm SSH access and the prepared password before accepting that setup.
    if not completed.is_file():
        verify_setup_password(home, url)
        save(completed, "complete\n")
    remember_lima_home(home)
    print("Kern is ready. Sign in, then connect your AI account in the browser.")
    show_password_guidance(home)
    print(f"Keep your SSH key files in {home}; only initial-password.txt should be deleted after saving its password.")
    print(f"Open again: cd {shlex.quote(str(ROOT))} && python3 quickstart.py")
    print(f"Stop the VM: cd {shlex.quote(str(ROOT))} && python3 -m host.cli.stop --provider lima --agent-name {shlex.quote(name)}")
    print("Kern keeps working while this computer is powered on and awake.")


def run(*, name: str = "my-kern", state_root: Path | None = None, browser: bool = True) -> int:
    """Create/resume one local host; ongoing management uses host.cli modules."""
    try:
        if os.getuid() == 0 or sys.version_info < (3, 11):
            raise ConfigError("use a normal user account with Python 3.11 or newer")
        lima.build_lima_config(LifecycleCommand(mode="deploy", agent_name=name, provider="lima"))
        home = state_dir(name, state_root)
        with setup_lock(home):
            lima_home = str(Path(os.environ.get("LIMA_HOME", str(Path.home() / ".lima"))).expanduser().resolve())
            marker = home / "lima-home.txt"
            if marker.exists():
                private_file(marker)
                if marker.read_text().strip() != lima_home:
                    raise ConfigError("LIMA_HOME changed; restore the original Lima home before continuing")
            setup_local(name, home, browser=browser)
        return 0
    except (ConfigError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Kern setup stopped: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. Rerun python3 quickstart.py to inspect/resume the host; keep its data disks.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(run())
