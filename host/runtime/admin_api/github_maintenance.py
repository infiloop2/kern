"""Admin-owned GitHub credential convergence and repository audit polling.

Startup attempts credential convergence before runtime status loops.
The refresh loop renews installation tokens and updates audit warnings.
Failed convergence retains the existing short retry interval.
"""

import time

from host.runtime.admin_api import github_credential, github_repo_audit

GITHUB_CREDENTIAL_REFRESH_CHECK_SECONDS = 300
GITHUB_CREDENTIAL_RETRY_SECONDS = 10


def initial_credential_convergence() -> bool:
    try:
        github_credential.reconcile()
        return True
    except Exception:
        # reconcile() records its own failures; reaching here means it could
        # not even run (the database briefly unavailable during startup), so
        # the refresh loop retries quickly rather than waiting a full cycle.
        return False


def github_credential_refresh_loop(converged: bool) -> None:
    # Converge the installed token file every cycle: App installation tokens
    # live one hour and are re-minted inside the refresh margin, and any
    # earlier failure (mint, install, disable-time removal) is retried here.
    # The initial convergence ran synchronously in service.main
    # before any turn could be admitted; while a convergence attempt cannot
    # run at all (the database unavailable), retry on the short interval so
    # turns are not admitted against stale credential files for a full cycle.
    while True:
        time.sleep(GITHUB_CREDENTIAL_REFRESH_CHECK_SECONDS if converged else GITHUB_CREDENTIAL_RETRY_SECONDS)
        try:
            github_credential.reconcile()
            github_repo_audit.refresh()
            converged = True
        except Exception:
            converged = False

