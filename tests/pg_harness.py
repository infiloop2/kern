"""Scratch PostgreSQL cluster for the unit tests.

Admin state lives in Postgres, so state/admin-API/orchestrator tests need a
real database. This harness starts one throwaway cluster per test process —
Unix socket only, in a temp directory, no network — applies the repo's schema
migrations once, and exports the ``KERN_DB_*`` environment so the
runtime code under test connects to it. ``reset_database()`` truncates all
tables between tests, which is much faster than a cluster or database per
test. A failed setup is cleaned immediately and disables further setup attempts
in that process, so one infrastructure failure cannot allocate a cluster per
later test.

The server binaries come from PATH, from the newest ``/usr/lib/postgresql/*``
install, or from ``KERN_TEST_PG_BIN``. If the binaries are unavailable
the calling test is skipped with instructions; CI installs PostgreSQL in the
sandbox image, so the suite never silently loses this coverage there. No
Python driver is needed anywhere: the runtime brings its own protocol client
(host.runtime.core.pgclient) and cluster administration uses the createdb/dropdb
binaries.
"""

from __future__ import annotations

import atexit
from collections.abc import Callable
import ctypes
from functools import partial
import glob
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

_STARTED = False
_SKIP_REASON: str | None = None
_HOST_SKIP_MESSAGE = (
    "PostgreSQL integration tests are CI-only on a live Kern host; "
    "they were skipped to protect agent runtime capacity. Rebase onto current main "
    "and let GitHub Actions run the database suite."
)

_PR_SET_PDEATHSIG = 1
_LIBC = ctypes.CDLL(None, use_errno=True)


def _bind_server_to_test_process(parent_pid: int) -> None:
    """Ask Linux to terminate postgres if its test process disappears."""
    if _LIBC.prctl(_PR_SET_PDEATHSIG, signal.SIGINT, 0, 0, 0) != 0:
        os._exit(127)
    # Close the small race where the parent exits before prctl is installed.
    if os.getppid() != parent_pid:
        os.kill(os.getpid(), signal.SIGINT)


def _parent_death_hook(parent_pid: int) -> Callable[[], None] | None:
    """Return the Linux parent-death hook; other platforms use cleanup only."""
    if sys.platform != "linux" or not hasattr(_LIBC, "prctl"):
        return None
    return partial(_bind_server_to_test_process, parent_pid)


def _stop_postgres(postgres: subprocess.Popen[bytes]) -> None:
    """Stop a foreground test postmaster without waiting on pooled clients."""
    if postgres.poll() is not None:
        return
    try:
        # PostgreSQL maps SIGINT to fast shutdown: disconnect clients, roll
        # back active transactions, and exit cleanly. SIGTERM is smart
        # shutdown and can wait forever on the process-wide test pool.
        postgres.send_signal(signal.SIGINT)
        postgres.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            postgres.kill()
            postgres.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _wait_for_postgres(
    postgres: subprocess.Popen[bytes],
    pg_isready: Path,
    socket_dir: Path,
    postgres_log: Path,
    env: dict[str, str],
    *,
    timeout_seconds: float = 30,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Wait against one wall-clock deadline, not a number of fast polls."""
    deadline = monotonic() + timeout_seconds
    while True:
        if postgres.poll() is not None:
            detail = postgres_log.read_text(errors="replace")[-2_000:]
            raise RuntimeError(f"scratch PostgreSQL exited during startup: {detail}")
        ready = subprocess.run(
            [
                str(pg_isready),
                "-h",
                str(socket_dir),
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-t",
                "1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            env=env,
        )
        if ready.returncode == 0:
            return
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"scratch PostgreSQL did not become ready within {timeout_seconds:g} seconds"
            )
        sleep(min(0.1, remaining))


def _host_skip_reason() -> str | None:
    """Keep scratch database servers off the live single-tenant host.

    GitHub Actions is the authoritative environment for these integration
    tests. The filesystem/user check covers both the canonical test wrapper
    and direct test invocations without changing the environment inherited by
    unrelated agent work.
    """
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        return None
    try:
        username = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        username = ""
    if username == "kern-agent" and Path("/opt/kern-host").is_dir():
        return _HOST_SKIP_MESSAGE
    return None


def _subprocess_env(work_dir: Path) -> dict[str, str]:
    """initdb and postgres require a passwd entry for the effective uid. The
    CI sandbox runs as an arbitrary uid with none, so fake one through
    nss_wrapper when needed (and available)."""
    env = os.environ.copy()
    try:
        pwd.getpwuid(os.geteuid())
        return env
    except KeyError:
        pass
    wrappers = glob.glob("/usr/lib/*/libnss_wrapper.so") + glob.glob("/usr/lib/libnss_wrapper.so")
    if not wrappers:
        return env  # initdb will fail with its own clear error
    passwd_file = work_dir / "nss_passwd"
    group_file = work_dir / "nss_group"
    uid, gid = os.geteuid(), os.getegid()
    passwd_file.write_text(f"pgtest:x:{uid}:{gid}:pgtest:{work_dir}:/bin/sh\n")
    group_file.write_text(f"pgtest:x:{gid}:\n")
    env["LD_PRELOAD"] = wrappers[0]
    env["NSS_WRAPPER_PASSWD"] = str(passwd_file)
    env["NSS_WRAPPER_GROUP"] = str(group_file)
    return env


def _find_pg_bin() -> Path | None:
    override = os.environ.get("KERN_TEST_PG_BIN")
    if override:
        return Path(override)
    initdb = shutil.which("initdb")
    if initdb:
        return Path(initdb).resolve().parent
    versions = Path("/usr/lib/postgresql")
    if versions.is_dir():
        candidates = sorted(
            (path for path in versions.iterdir() if (path / "bin" / "initdb").exists()),
            key=lambda path: int(path.name) if path.name.isdigit() else 0,
        )
        if candidates:
            return candidates[-1] / "bin"
    return None


def ensure_database() -> None:
    """Start the scratch cluster once per process and point KERN_DB_* at
    it. Raises unittest.SkipTest when PostgreSQL is unavailable."""
    global _STARTED, _SKIP_REASON
    if _SKIP_REASON is not None:
        raise unittest.SkipTest(_SKIP_REASON)
    host_skip = _host_skip_reason()
    if host_skip is not None:
        _SKIP_REASON = host_skip
        raise unittest.SkipTest(_SKIP_REASON)
    if _STARTED:
        return
    pg_bin = _find_pg_bin()
    if pg_bin is None or not (pg_bin / "initdb").exists():
        _SKIP_REASON = (
            "PostgreSQL server binaries not found "
            "(apt install postgresql, or set KERN_TEST_PG_BIN to a bin directory)"
        )
        raise unittest.SkipTest(_SKIP_REASON)

    data_dir = Path(tempfile.mkdtemp(prefix="kern-pg-data.")) / "data"
    # A separate short socket path: Unix socket paths are limited to ~107
    # bytes and temp dirs under deep workspaces can exceed that.
    socket_dir = Path(tempfile.mkdtemp(prefix="tcpg.", dir="/tmp"))

    postgres: subprocess.Popen[bytes] | None = None
    postgres_log = data_dir.parent / "postgres.log"
    log_handle = None

    def cleanup() -> None:
        if postgres is not None:
            _stop_postgres(postgres)
        if log_handle is not None:
            log_handle.close()
        shutil.rmtree(data_dir.parent, ignore_errors=True)
        shutil.rmtree(socket_dir, ignore_errors=True)

    atexit.register(cleanup)

    def abort_setup() -> None:
        global _SKIP_REASON
        cleanup()
        atexit.unregister(cleanup)
        _SKIP_REASON = (
            "scratch PostgreSQL setup already failed in this test process; "
            "see the first setup error"
        )

    try:
        env = _subprocess_env(data_dir.parent)
    except BaseException:
        abort_setup()
        raise

    def run_setup(command: list[str]) -> None:
        try:
            subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                env=env,
            )
        except BaseException:
            abort_setup()
            raise

    run_setup(
        [
            str(pg_bin / "initdb"),
            "-D",
            str(data_dir),
            "-U",
            "postgres",
            "-A",
            "trust",
            "-E",
            "UTF8",
        ]
    )
    # Run the postmaster in the foreground and retain its process handle.
    # pg_ctl daemonization would reparent it to pid 1, leaving a live server
    # behind when a test runner is killed before Python can run atexit hooks.
    # Durability is off because this cluster is thrown away with the process.
    try:
        log_handle = postgres_log.open("ab", buffering=0)
        postgres = subprocess.Popen(
            [
                str(pg_bin / "postgres"),
                "-D",
                str(data_dir),
                "-c",
                "listen_addresses=",
                "-c",
                f"unix_socket_directories={socket_dir}",
                "-c",
                "fsync=off",
                "-c",
                "synchronous_commit=off",
                "-c",
                "full_page_writes=off",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
            preexec_fn=_parent_death_hook(os.getpid()),
        )
        _wait_for_postgres(
            postgres,
            pg_bin / "pg_isready",
            socket_dir,
            postgres_log,
            env,
        )
    except BaseException:
        abort_setup()
        raise
    os.environ["KERN_DB_SOCKET_DIR"] = str(socket_dir)
    os.environ["KERN_DB_NAME"] = "kern_test"
    os.environ["KERN_DB_USER"] = "postgres"

    # The scoped service roles must exist before migrations run (the schema
    # GRANTs them their tables). Tests connect as postgres either way.
    for role in (
        "kern-admin",
        "kern-proxy",
        "kern-tools",
        "kern-agent-network",
        "kern-workspace",
    ):
        run_setup([str(pg_bin / "createuser"), "-h", str(socket_dir), "-U", "postgres", role])
    run_setup(
        [str(pg_bin / "createdb"), "-h", str(socket_dir), "-U", "postgres", "kern_test"]
    )
    # Bootstrap owns server-level extension provisioning. Mirror that
    # prerequisite explicitly in the test environment rather than hiding it
    # inside an application schema migration.
    run_setup(
        [
            str(pg_bin / "psql"),
            "-h",
            str(socket_dir),
            "-U",
            "postgres",
            "-d",
            "kern_test",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "CREATE EXTENSION IF NOT EXISTS vector;",
        ]
    )

    from host.runtime.deploy import migrate

    try:
        migrate.up(quiet=True)
    except BaseException:
        abort_setup()
        raise
    _STARTED = True


def reset_database() -> None:
    """Truncate every state table and clear the in-process stores (test
    setUp); the schema stays migrated."""
    ensure_database()
    from host.runtime.agent_runtime import orchestrator

    orchestrator._RUNTIME_STATUSES.clear()
    from host.runtime.core import db

    with db.transaction() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables"
            " WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
        )
        tables = [row[0] for row in cur.fetchall()]
        if tables:
            names = ", ".join(f'"{name}"' for name in tables)
            cur.execute(f"TRUNCATE {names} RESTART IDENTITY")
        # The schema migration seeds the secretbox key at schema time
        # (schema present => key present); truncation wipes data, so restore
        # that invariant the same way the migration does.
        cur.execute(
            "INSERT INTO secret_keys (singleton, key_hex)"
            " VALUES (TRUE, translate(gen_random_uuid()::text || gen_random_uuid()::text, '-', ''))"
        )
        # Migration 0030 establishes these rows as a schema invariant. Tests
        # truncate counters along with the rest of state, so restore the empty
        # database values before each test just like the secretbox key above.
        cur.execute(
            "INSERT INTO counters (name, value) VALUES"
            " ('agent_history_threads', 0),"
            " ('agent_history_messages', 0),"
            " ('agent_history_activities', 0)"
        )


def create_database(name: str) -> None:
    """Create an extra empty database on the scratch cluster (migration-runner
    tests use their own so they can migrate down without disturbing the shared
    schema)."""
    ensure_database()
    from host.runtime.core import db

    # Pooled connections from a previous test would keep the database busy
    # and fail the DROP below.
    db.close_pool()
    pg_bin = _find_pg_bin()
    assert pg_bin is not None  # ensure_database already found it
    socket_dir = os.environ["KERN_DB_SOCKET_DIR"]
    subprocess.run(
        [str(pg_bin / "dropdb"), "--if-exists", "-h", socket_dir, "-U", "postgres", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )
    subprocess.run(
        [str(pg_bin / "createdb"), "-h", socket_dir, "-U", "postgres", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )
    subprocess.run(
        [
            str(pg_bin / "psql"),
            "-h",
            socket_dir,
            "-U",
            "postgres",
            "-d",
            name,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC;",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )
    # Extra databases that run the repository migrations need the same
    # bootstrap prerequisite as the shared scratch database above.
    subprocess.run(
        [
            str(pg_bin / "psql"),
            "-h",
            socket_dir,
            "-U",
            "postgres",
            "-d",
            name,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            "CREATE EXTENSION IF NOT EXISTS vector;",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=True,
    )
