"""Local serialization for provider lifecycle and power commands."""

from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from typing import IO

from host.config import ConfigError


def _private_lock_dir() -> Path:
    """Return a user-owned runtime directory, never a lifecycle state store."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        root = Path(runtime)
        if not root.is_absolute():
            raise ConfigError("XDG_RUNTIME_DIR must be an absolute path")
        base = root / "kern"
    else:
        base = Path(tempfile.gettempdir()) / f"kern-{os.getuid()}"
    locks = base / "locks"
    try:
        locks.mkdir(mode=0o700, parents=True, exist_ok=True)
        for directory in (base, locks):
            info = directory.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ConfigError(f"Kern lock path {directory} is not a real directory")
            if info.st_uid != os.getuid():
                raise ConfigError(f"Kern lock path {directory} is not owned by the current user")
            if stat.S_IMODE(info.st_mode) != 0o700:
                directory.chmod(0o700)
    except OSError as exc:
        raise ConfigError(f"could not prepare Kern lock directory {locks}: {exc}") from exc
    return locks


class OperationLock:
    """Non-blocking local lock scoped to one provider and agent.

    This prevents two Kern commands started by the same OS user from
    interleaving. It is deliberately not infrastructure authority: another
    machine or direct provider command remains possible, so provider discovery
    and identity checks must still fail closed.
    """

    def __init__(self, provider: str, agent_name: str) -> None:
        if provider not in {"aws", "lima"}:
            raise ConfigError(f"unsupported lock provider {provider!r}")
        digest = hashlib.sha256(f"{provider}\0{agent_name}".encode()).hexdigest()[:16]
        self.path = _private_lock_dir() / f"{provider}-{digest}.lock"
        self._handle: IO[str] | None = None

    def __enter__(self) -> "OperationLock":
        flags = os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise ConfigError(f"could not open Kern operation lock {self.path}: {exc}") from exc
        handle = os.fdopen(descriptor, "w")
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ConfigError(f"Kern lock path {self.path} is not a user-owned regular file")
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ConfigError:
            handle.close()
            raise
        except BlockingIOError:
            handle.close()
            raise ConfigError(
                f"another Kern lifecycle command holds {self.path}; wait for it to finish"
            ) from None
        except OSError as exc:
            handle.close()
            raise ConfigError(f"could not lock Kern operation file {self.path}: {exc}") from exc
        self._handle = handle
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
