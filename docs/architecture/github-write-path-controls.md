# GitHub Push Controls

The GitHub integration blocks direct pushes to `main` by default and supports
one independent file-level approval control:
`require_dot_github_approval`. When this boolean is true, a write-repository
push that changes `.github/` is held for operator approval before anything is
sent to GitHub.

## Policy Surface

Both toggles live under `network_integrations.github` next to
`write_repositories`. `block_direct_main_pushes` defaults to true whenever the
integration is enabled. The operator may set it to false in the Network
settings. Reads are unchanged, and normal writes to configured write
repositories still pass.

The main-branch block applies only to smart-HTTP Git pushes. A receive-pack
transaction containing an update or deletion of `refs/heads/main` is rejected
as `github_main_push_denied`; every ref in a multi-ref push is rejected because
none of the request is forwarded. The proxy parses only the command list and
does not index the pack or create an approval row. Feature-branch pushes and
pull-request merges remain allowed.

For `.github` approval, the guard denies REST write paths that can create or
move `.github` changes without entering `git-receive-pack`, including
`contents/.github...`, `git/{refs,trees,commits}`, and lower-level branch merge
APIs. Those denials use `github_dot_github_rest_write_denied`. Pull-request
merges remain allowed for configured write repositories; the same REST endpoint
supports regular (`merge`) and squash (`squash`) merge methods (as well as
`rebase`). This is a deliberate exception: merging a PR can land `.github/`
changes without entering Kern's local push-approval queue. Agent-authored PR
content already crossed the gate when its branch was pushed; the merge API only
promotes content already present on GitHub. PR branches populated elsewhere are
governed by the configured repository-scoped merge permission.

## Push Flow

For smart-HTTP `git-receive-pack` pushes to a configured write repository, the
proxy buffers the request body. It first rejects a command list that targets
`refs/heads/main` when main protection is enabled. Otherwise, if `.github`
approval is enabled, it asks `host/network_integrations/github/push_gate/` to
inspect the pack. The approval gate resolves thin packs against a per-repo
quarantine mirror and uses real `git` plumbing (`index-pack` and `diff-tree`) to
compute changed paths.

- If no changed path is `.github` or under `.github/`, the proxy forwards the
  original push body upstream.
- If `.github` is touched, the gate stores the new objects under
  `refs/pending/<push_id>/...` in the quarantine mirror, writes a
  `pending_pushes` row, and returns a git `report-status` rejection that tells
  the agent the push is queued for approval. The network event reason is
  `github_push_queued_for_approval`.

The proxy parses pkt-line command framing only. Main-branch rejection stops at
that command list. Pack objects are parsed by `git`, not by custom Python
object logic, only when `.github` inspection is needed.

## Queue Bound

The proxy permits at most ten pending pushes globally. It checks the count
before `index-pack` writes the request into the durable mirror; the proxy's
existing 128 MiB request cap is the per-push bound. A full queue rejects the
push with `github_push_queue_full` until the operator resolves an item. There
is deliberately no TTL, free-space heuristic, or background maintenance pass.

## Approval Flow

Operators list and resolve held pushes through the admin API:
`/v1/network-tools/github-pending-pushes`. Approving a push invokes the
root-owned `approve-github-push` helper with the reviewed ref updates and the
working GitHub token. The helper replays the queued refs to GitHub from the
quarantine mirror with `git push --atomic` and per-ref
`--force-with-lease=<ref>:<old>` checks. Rejecting a push invokes the same
helper in cleanup mode and removes the pending refs.

The proxy's inspection path and the root helper share one file lock. After an
approve or reject, the helper deletes that push's refs and runs
`git gc --prune=now` before releasing the lock, so no concurrent pack can be
pruned between indexing and creation of its pending ref.

Approve and reject actions serialize inside the admin service (the single
resolver: only its role can update pending pushes, and the port bind keeps it
single-instance); a concurrent duplicate action gets a conflict error. A crash
mid-resolve leaves the row `pending`, so the operator simply approves or
rejects again. Approving with no working GitHub token is refused and the row
stays `pending` — fix the credential and approve again. If a replay fails, the
row is marked `failed` with the failure detail; the recovery for every failure
is the same — the agent pushes again, which starts a fresh gate round.
Pending-ref cleanup is housekeeping of the proxy-private mirror. A leftover
`refs/pending/*` ref is inert — it is never pushed anywhere.

## Failure Behavior

The controls fail closed. If receive-pack parsing, quarantine indexing, mirror
fetch, pending-row insertion, or approval replay cannot complete, the push is
not forwarded silently. A held push is marked with a terminal resolved state
(`approved`, `rejected`, or `failed`) with the failure recorded in `detail`;
recovery from any failed resolution is a fresh agent push.
