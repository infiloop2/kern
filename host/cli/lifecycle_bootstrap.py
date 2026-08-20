"""SSH delivery for Kern host provisioning.

The artifacts themselves (payload, bootstrap script, runtime code archive)
are rendered by ``host.bootstrap.render``, shared with the GitHub delivery;
this module owns only the SSH transport: the single-use deploy key, copying
the artifacts to the instance, and running bootstrap over the connection.
"""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import select
import subprocess
import sys
import time
from typing import Any

from host.bootstrap.render import _write_runtime_code_archive
from host.config import ConfigError
from host.cli.lifecycle_constants import (
    BOOTSTRAP_TIMEOUT_SECONDS,
    SCP_TIMEOUT_SECONDS,
    SSH_PROBE_TIMEOUT_SECONDS,
    SSH_USER,
    SSH_WAIT_ATTEMPTS,
    SSH_WAIT_SECONDS,
)
from host.cli.lifecycle_logging import _log


def _generate_deploy_key(workdir: Path) -> Path:
    key_path = workdir / "deploy_key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", "kern-deploy", "-f", str(key_path)],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return key_path


CODE_ARCHIVE_NAME = "kern-host-code.tar.gz"


def _provision_over_ssh(
    public_dns: str,
    deploy_key: Path,
    workdir: Path,
    *,
    port: int = 22,
) -> None:
    """Push the local checkout's code archive to the instance and hand off to
    host.bootstrap.self_provision there, exactly like the GitHub delivery
    after its fetch. The provisioning payload was already staged by user
    data (EC2) or the stage-one script (Lima); only code delivery differs
    between the deliveries. ``port`` serves the local provider's loopback
    endpoint; EC2 hosts stay on 22."""
    code_path = workdir / CODE_ARCHIVE_NAME
    if not code_path.exists():
        _write_runtime_code_archive(code_path)

    ssh = [
        "ssh",
        "-i",
        str(deploy_key),
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={workdir / 'known_hosts'}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        # -o Port applies to both ssh and the scp reuse of these options.
        "-o",
        f"Port={port}",
    ]
    target = f"{SSH_USER}@{public_dns}"
    _log("waiting for SSH on the new instance (it is still booting)")
    _wait_for_ssh(ssh, target)
    _log("SSH is up; copying runtime code and bootstrap script to the host")
    subprocess.run(
        ["scp", *ssh[1:], str(code_path), f"{target}:/tmp/"],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=SCP_TIMEOUT_SECONDS,
    )
    _log("running bootstrap: apt, Postgres + schema migrations, npm,")
    _log("agent CLIs, services. This takes several minutes; the host's own output streams below.")
    print("-" * 70, file=sys.stderr, flush=True)
    _run_bootstrap(ssh, target)
    print("-" * 70, file=sys.stderr, flush=True)


# The delivered archive becomes the checkout self_provision renders and
# bootstraps from; fixed paths only, nothing user-controlled is interpolated.
_REMOTE_SELF_PROVISION = (
    "sudo bash -c '"
    "rm -rf /tmp/kern-checkout && mkdir -p /tmp/kern-checkout && "
    "tar -xzf /tmp/kern-host-code.tar.gz -C /tmp/kern-checkout && "
    "PYTHONPATH=/tmp/kern-checkout python3 -m host.bootstrap.self_provision "
    "--payload /tmp/kern_payload.json --checkout /tmp/kern-checkout"
    "'"
)


def _run_bootstrap(ssh: list[str], target: str) -> None:
    """Stream bootstrap output under a hard deadline. A remote hang with a
    live SSH connection would otherwise block forever — `ServerAliveInterval`
    detects only a dead connection — holding the operation's lock and keeping
    the caller's failure cleanup from ever running."""
    deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_SECONDS
    process = subprocess.Popen(
        [*ssh, target, _REMOTE_SELF_PROVISION],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:
        raise ConfigError("could not read bootstrap output")
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConfigError(
                    f"bootstrap did not finish within {BOOTSTRAP_TIMEOUT_SECONDS} seconds; "
                    "terminating the SSH session so failure cleanup can run"
                )
            ready, _, _ = select.select([process.stdout], [], [], min(remaining, 30.0))
            if not ready:
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            sys.stderr.write(decoder.decode(chunk))
            sys.stderr.flush()
        tail = decoder.decode(b"", final=True)
        if tail:
            sys.stderr.write(tail)
            sys.stderr.flush()
        returncode = process.wait(timeout=max(1.0, deadline - time.monotonic()))
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    if returncode != 0:
        raise ConfigError(f"bootstrap failed on the host (exit {returncode}); see the output above")


def _wait_for_ssh(ssh: list[str], target: str) -> None:
    total_seconds = SSH_WAIT_ATTEMPTS * SSH_WAIT_SECONDS
    deadline = time.monotonic() + total_seconds
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConfigError(f"could not reach {target} over SSH after {total_seconds} seconds")
        # ConnectTimeout bounds connection setup; the probe timeout bounds a
        # daemon that accepts the connection but never answers the command,
        # capped to the remaining budget so the advertised deadline holds.
        try:
            result = subprocess.run(
                [*ssh, target, "true"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=min(SSH_PROBE_TIMEOUT_SECONDS, remaining),
            )
        except subprocess.TimeoutExpired:
            continue
        if result.returncode == 0:
            return
        if attempt % 3 == 0:
            _log(f"  still waiting for SSH ({round(total_seconds - remaining)}s elapsed)")
        attempt += 1
        time.sleep(min(SSH_WAIT_SECONDS, max(0.0, deadline - time.monotonic())))
