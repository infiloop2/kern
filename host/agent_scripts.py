"""The contract for the script agent runtime: what a scheduled script is.

The script runtime runs one static bash script from the agent home instead of
a model turn. A schedule selects it exactly like any other runtime — through
``agent_runtime``, ``model``, and ``effort`` — and carries the script's
absolute path in the message field the model runtimes use as a prompt.

Two processes need to agree on that path shape: the workspace, which accepts
the schedule and must reject a malformed path while the operator is still
looking at the form, and the admin API, which runs it. Neither can stat the
path — the agent home is private to ``kern-agent`` — so both check the same
spelling here and the privileged ``run-agent-script`` launcher performs the
filesystem checks after it demotes to the agent user.

The turn budget is fixed rather than configurable: a scheduled script is
automation, not an interactive session, and the launcher enforces the same
number as a scope ``RuntimeMaxSec`` so a wedged script cannot outlive it even
if the admin API loses track of the process.
"""

from __future__ import annotations

from functools import lru_cache
import re


AGENT_HOME = "/mnt/kern-agent/agent-home"
SCRIPT_TIMEOUT_SECONDS = 15 * 60
# The launcher gives the scope a slightly longer life than the admin API gives
# the turn, so the host-side timeout is normally the one that fires and reports
# the clearer message. The scope limit is the backstop for the case the
# host-side one cannot cover: an admin API that stayed up but lost track of the
# child (a restart is already covered by the scope's BindsTo).
SCRIPT_SCOPE_GRACE_SECONDS = 30
SCRIPT_SCOPE_MAX_SECONDS = SCRIPT_TIMEOUT_SECONDS + SCRIPT_SCOPE_GRACE_SECONDS
MAX_SCRIPT_PATH_CHARS = 512


@lru_cache(maxsize=4)
def _script_path_re(home: str) -> re.Pattern[str]:
    """One absolute path under the home, spelled plainly.

    No whitespace, no quoting, no repeated separators, and a ``.sh`` name so a
    schedule cannot quietly point the runtime at a data file. The home is a
    parameter, and read from the module global on each call, so a test can
    stand the contract up over a temporary directory.
    """
    return re.compile(rf"^{re.escape(home)}/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.sh$")


def script_path_error(value: object) -> str | None:
    """Reject a script path that may not be scheduled, or None when it may.

    Spelling only. Whether the file exists, is a regular file, and is not a
    symlink is decided by the launcher as the agent user at run time, so this
    never becomes a filesystem oracle for a caller that cannot read the home.
    """
    if not isinstance(value, str) or not value:
        return "script path must be a non-empty string"
    if len(value) > MAX_SCRIPT_PATH_CHARS:
        return f"script path must be at most {MAX_SCRIPT_PATH_CHARS} characters"
    if _script_path_re(AGENT_HOME).fullmatch(value) is None:
        return (
            f"script path must be an absolute .sh path under {AGENT_HOME}"
            " containing only letters, numbers, '.', '_', '-', and '/'"
        )
    if any(part in {".", ".."} for part in value.split("/")):
        return "script path must not contain '.' or '..' segments"
    return None
