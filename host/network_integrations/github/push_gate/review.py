"""Bounded, read-only evidence extraction from pinned quarantine objects.

Invoked by the admin-only root helper under the quarantine lock. Never checks
out files or runs repository code. Oversized/binary evidence fails as a whole.
"""
from pathlib import Path
from typing import Any

from host.network_integrations.github.push_gate import engine

# Full blobs plus diffs; the assembled policy/evidence prompt is separately
# bounded by the Sol adapter, including JSON escaping and metadata.
MAX_BYTES = 1_000_000
MAX_FILES = 200


def context(mirror: Path, push_id: str, updates: Any) -> list[dict[str, Any]]:
    if not isinstance(updates, list) or not updates:
        raise ValueError("Missing held ref updates")
    remaining = MAX_BYTES
    files_left = MAX_FILES

    def text(args: list[str]) -> str:
        nonlocal remaining
        raw = engine._run_git(mirror, args)
        remaining -= len(raw)
        if remaining < 0:
            raise ValueError("The complete .github/ changes are too large for automatic review.")
        if b"\0" in raw:
            raise ValueError("Binary .github/ content needs manual review.")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Non-UTF-8 .github/ content needs manual review.") from exc

    def blob(oid: str, mode: str) -> str | None:
        if oid == engine.ZERO_OID:
            return None
        if mode not in {"100644", "100755", "120000"}:
            raise ValueError("Unsupported .github/ file type needs manual review.")
        size = int(engine._run_git(mirror, ["cat-file", "-s", oid]))
        if size > remaining:
            raise ValueError("The complete .github/ changes are too large for automatic review.")
        return text(["cat-file", "blob", oid])

    result = []
    for index, update in enumerate(updates):
        if not isinstance(update, dict):
            raise ValueError("Invalid held ref update")
        old, new, ref = (update.get(k) for k in ("old", "new", "ref"))
        if (not isinstance(old, str) or not engine.OID_RE.fullmatch(old)
                or not isinstance(new, str) or not engine.OID_RE.fullmatch(new)
                or not isinstance(ref, str) or not engine.valid_ref_name(ref)):
            raise ValueError("Invalid held ref update")
        if new == engine.ZERO_OID:
            result.append({"old": old, "new": new, "ref": ref, "deleted": True})
            continue
        held = engine._run_git(mirror, ["rev-parse", "--verify", f"refs/pending/{push_id}/{index}"]).decode().strip()
        if held != new:
            raise ValueError("Held commit does not match the review request")
        bases = engine._run_git(mirror, ["for-each-ref", "--format=%(objectname)", f"refs/pending/{push_id}/base-{index}-*"]).decode().splitlines()
        if not bases:
            raise ValueError("This held push has no pinned review bases; review it manually.")
        comparisons = []
        for base in bases:
            changes = engine._run_git(mirror, ["diff-tree", "-r", "--no-commit-id", "--raw", "--no-abbrev", "--no-renames", "-z", base, new, "--", ".github"])
            if len(changes) > MAX_BYTES:
                raise ValueError("Too many .github/ changes for automatic review.")
            fields = changes.split(b"\0")
            files = []
            for offset in range(0, len(fields) - 1, 2):
                files_left -= 1
                if files_left < 0:
                    raise ValueError("Too many .github/ files for automatic review.")
                modes = fields[offset].decode("ascii").split()
                path = fields[offset + 1].decode("utf-8")
                files.append({"path": path, "status": modes[4], "before_mode": modes[0][1:], "after_mode": modes[1],
                              "before": blob(modes[2], modes[0][1:]), "after": blob(modes[3], modes[1])})
            # Blob size checks precede diff generation; full evidence is never truncated.
            diff = text(["diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--unified=3", base, new, "--", ".github"])
            comparisons.append({"base": base, "diff": diff, "files": files})
        result.append({"old": old, "new": new, "ref": ref, "comparisons": comparisons})
    return result
