# Audit: Installed Apps and Agent-Authored Content

Finding ID prefix: `APP`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can malicious or buggy app code exceed its assigned authority, or can
agent-controlled data handled by an app execute in the browser or trick the
operator?

## Reviewed commits

Latest reviewed commit: none.

| Commit | Reviewed by |
| --- | --- |
| _None yet_ | _No completed review_ |

## Findings

No findings recorded.

## Threat model

- **Adversaries:** (a) deliberately malicious installed app backend/UI code;
  (b) an ordinary app bug; (c) a prompt-injected agent controlling messages,
  activity payloads, file names, file contents, generated HTML/data, paths,
  and app API inputs; (d) a compromised app backend process; and (e) a
  malicious page or frame sending crafted `postMessage` traffic.
- **Assets:** the operator's admin session and local files, admin API
  capabilities, agent runtime credentials and workspace data, other apps'
  schemas and backends, host/proxy/tool state, the integrity of what the
  operator sees, and the guarantee that agent-authored bytes remain data.
- **Out of scope:** browser or kernel zero-days; the agent's ordinary
  authority over its own workspace when no app or browser boundary is
  involved; parent Admin UI surfaces outside an installed app (axis 03 owns
  those).

## Minimal scope checklist

This checklist is not comprehensive. The audit question and threat model are
binding; report any in-scope defect even if no item names it.

1. Load every active and deprecated manifest through the real validator.
   Check ids, stable host slots, generated users/roles/schemas/services/ports,
   referenced paths, symlinks/traversal, exact fields, release-stage rules,
   agent/API and capability-worker declarations, collisions, and deterministic
   behavior across deploy/upgrade.
2. Trace provisioning for every app identity: Unix ownership and home,
   database role/schema/search path and grants, migration runner/versioning,
   loopback port, nftables callers, systemd environment/working directory,
   `kern_app.slice`, filesystem access, restart behavior, and cleanup. Prove
   one app cannot read/call another app or a host service directly.
3. Audit browser/Admin-to-app traffic end to end: installed-app lookup,
   UI-asset path confinement and MIME/cache/CSP headers, iframe creation,
   backend method/path/body/response/time limits, header stripping, host-marker
   provenance, disconnect/errors, and denial of direct app-port access.
4. Audit the app-backend-to-admin Unix socket as a distinct principal:
   `SO_PEERCRED` uid-to-app mapping, claimed app-id match, fixed route
   allowlist, reserved thread-prefix insertion, request/response filtering,
   app ownership checks before shared dispatch, body/time limits, malformed
   HTTP, and inability to reach login, passkeys, host admin, another app, or
   removed compatibility routes.
5. Audit the agent-to-app path independently: tools-shim `app_api`, agent-app
   socket peer checks, pid/cgroup-to-live-thread attribution, app-prefixed
   thread parsing, manifest opt-in, app ownership, proxy-header provenance,
   backend route grammar, body/response/time/concurrency bounds, process-exit
   races, and attempts from an unscoped or different app thread.
6. Enumerate every iframe sandbox token, app CSP directive and asset source,
   origin property, script/style/image/font/worker/connect allowance, MIME and
   cache rule, direct asset URL, popup/form/download/navigation capability,
   and parent-page API. Prove an app cannot acquire the Admin origin, cookies,
   credentials, DOM, or unrestricted browser/network authority.
7. Enumerate the complete parent bridge protocol: exact source window and app
   binding, request-id lifecycle, duplicate/stale/replayed replies,
   concurrency/timeouts, field and size validation, API path confinement,
   response filtering, copy behavior, and host-owned file picker/upload
   mediation. No bridge message may become arbitrary Admin API, file read,
   URL open, navigation, HTML render, or cross-app authority.
8. Grep every app UI and shared rich-text dependency for markup, attribute,
   CSS, URL, worker, object-URL, navigation, popup, download, clipboard,
   `postMessage`, and fetch sinks. Trace messages, activity, attachments,
   backend/provider errors, filenames/bytes, generated state, and restored
   history. Agent-authored data must not execute, auto-fetch/open a link,
   spoof trusted controls, or escape the frame.
9. Audit every app path into host file viewing/open/upload: picker ownership,
   path normalization, symlink/TOCTOU and regular-file checks, type/size/count
   bounds, media sniffing and `nosniff`, streaming/partial cleanup, blob URL
   lifecycle, filename rendering, atomic publish, and whether app or agent
   bytes can select/disclose operator-local or another-thread files.
10. Verify database and thread isolation under malformed ids, reserved
    prefixes, archive/rename/create races, pagination cursors, transactions,
    optimistic revisions, concurrent browser/agent writes, migration rollback,
    and oversized stored values. Every app response must expose only its
    unprefixed ids and own rows.
11. Audit Agent Chat specifically: runtime/session selection, create/list/
    archive/rename, per-thread draft/history cache, newest/older event paging,
    event ordering and byte caps, rich text, attachments, starting/finishing
    retries, steering, stop, failure/reload, and thread mapping across all
    harnesses.
12. Audit Agentic Web App specifically: app create/archive/rename, chat and
    attachments, agent action grammar, bundle/state/revision bounds,
    capability-worker bootstrap and global removal, HTML/CSS sanitizer,
    Shadow DOM/event bridge, generated JavaScript execution, data mutations,
    worker termination/races, persistence, and archived/read-only behavior.
13. Verify deprecated apps reserve ids/slots and run only required migrations:
    no backend, UI, agent discovery, route, service, port, or stale table
    remains; reusing a reserved identity must require an explicit future
    migration and review.
14. Run unit, malicious-browser, smoke, stage, and deployed-host probes for
    cross-app/database/socket/port access, forged markers and `postMessage`,
    hostile markup/URLs/files, traversal/symlink swaps, oversized or partial
    traffic, backend crash/restart, stale frames, and agent calls before,
    during, and after a live thread.

## Collaborative review

No completed reviews yet.
