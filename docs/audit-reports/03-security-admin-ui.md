# Audit: Admin UI Exposure of Agent-Controlled Content

Finding ID prefix: `UI-CONTENT`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can agent-controlled data displayed by the Admin UI execute code, alter
trusted UI, cause the browser to take an unintended action, or trick the
operator—for example, by opening a link?

## Reviewed commits

Latest reviewed commit: `6151eea5abb61590684c4cf667ae6f619d705231`.

| Commit | Reviewed by |
| --- | --- |
| `6151eea5abb61590684c4cf667ae6f619d705231` | gpt-5.6-sol; Claude Opus 5 |

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| UI-CONTENT-002 | Low | `f28b50e87b61` | Claude Opus 4.8 | The HTML helpers interpolated badge values into markup and escaped text without quotes. Existing callers used controlled enums or text positions and CSP blocked inline script, but the helper contract was a latent XSS footgun if a future caller supplied attacker-controlled attribute data. Use context-correct escaping or DOM construction. | Fixed — esc() now escapes quotes as well as &<> so it is safe in attribute position, and badge() interpolates esc(value) in both the class and text, closing the attribute-injection footgun; existing enum callers render unchanged. |

## Threat model

- **Adversary:** a malicious or prompt-injected agent.
- **Assets:** the integrity of the trusted admin interface, the operator's
  authenticated browser authority, confidentiality of data already loaded in
  the page, and the guarantee that untrusted values cannot cause browser
  execution, navigation, or network disclosure.
- **Out of scope:** browser zero-days; compromise of the operator's machine;
  admin login, cookies, CSRF, unauthenticated routes, public/SSH transport, and
  listener exposure (axis 04 owns those); every installed app UI and its
  content, sandbox, bridge, backend, database, and host authority (axis 05
  owns those).

## Minimal scope checklist

This checklist is not comprehensive: it names known-important areas, but the
audit question and threat model define the scope. Account for each item in
your coverage section, and report anything else within scope even if no item
below names it.

1. Inventory every parent Admin UI document and module, including login,
   passkey, health, runtime, network, tools, logs, process, file, and shell
   views. Enumerate HTML/text/attribute/style/URL sinks, templates,
   `innerHTML`, object URLs, navigation, popups, downloads, clipboard writes,
   dynamic imports, event-handler binding, and data attributes. App-frame
   content and bridge behavior remain axis 05.
2. Trace every agent-influenceable source to its final browser context:
   network events and denial guidance; host-error summaries/tracebacks;
   filenames, paths, contents, and media metadata; process command lines;
   provider/runtime errors and account metadata; GitHub repository audit,
   refs and pending-push data; tool approval payloads/results/events; thread
   and health status; and backend/helper error strings.
3. Require context-correct rendering for text, quoted attributes, URLs, CSS,
   JSON, and code blocks. Test markup terminators, quotes, backticks, Unicode
   controls/bidi, nulls, invalid UTF-8, huge values, nested JSON, duplicate
   fields, and partial/stale updates. Agent data must not forge trusted labels,
   buttons, approvals, errors, or operator instructions.
4. Prove agent data cannot choose `href`, `src`, `action`, `srcdoc`, CSS URLs,
   module/worker names, forms, downloads, popups, navigation, clipboard
   content, or automatic requests. Any intentional operator-clicked link must
   validate scheme/origin, display its destination honestly, and isolate the
   opener/referrer.
5. Audit the parent file viewer end to end: helper path confinement and
   races, regular-file and size checks, streaming/error framing, fixed
   content types, `Content-Disposition`, `nosniff`, cache/range behavior,
   text replacement decoding, image/video metadata, sandbox policy, blob URL
   creation/revocation, and hostile HTML/SVG/media/polyglot or decompression
   inputs.
6. Verify parent static assets and all runtime requests are same-origin and
   expected: no CDN, analytics, fonts, images, prefetch, service worker,
   remote import, or destination derived from agent data. Audit CSP,
   `base-uri`, object/frame restrictions, referrer/cache/MIME headers, and
   module dependency closure as defense in depth.
7. Check that agent-controlled failures cannot alter authentication/passkey
   screens, overlay trusted dialogs, trigger privileged requests, suppress
   warnings, or persist active content across logout, navigation, refresh,
   polling, tab switches, and out-of-order responses.
8. Run browser tests with malicious fixtures in every parent renderer and
   inspect actual DOM, network requests, opened windows, downloads, clipboard,
   console/CSP reports, object-URL lifetime, and behavior on desktop/mobile.
   Include stored history, live polling, error paths, empty states, and values
   at every size limit.

## Collaborative review

### `6151eea5abb61590684c4cf667ae6f619d705231`

Reviewed by: gpt-5.6-sol; Claude Opus 5

Methodology: static, repository-level source-to-sink review of the complete
parent Admin UI and its API/helper inputs. HTML, text, attribute, URL,
navigation, clipboard, object-URL, and request sinks were grep-enumerated and
then traced from agent-, provider-, tool-, GitHub-, process-, file-, and
host-error-controlled values. File viewing and media delivery were followed
through the privileged helper and browser lifecycle. Existing browser smoke
fixtures were source-reviewed; no live browser or hostile-media run was
performed.

#### What was reviewed

- `host/runtime/admin_api/admin_ui.html`, its CSS, and every parent module:
  `api.js`, `app.js`, `connection_guide.js`, `files.js`, `health.js`,
  `helpers.js`, `integration_catalog.js`, `logs.js`, `network.js`,
  `passkeys.js`, `processes.js`, `threads.js`, and `tools.js`.
- Parent renderers and browser actions for health/runtime state, threads,
  provider/account errors, network events, tool approvals/results, GitHub
  audits and pending pushes, host errors, process command lines, file names,
  paths, text, images, and videos. Installed-app frame content and its bridge
  were deliberately left to axis 05.
- `host/runtime/admin_api/service.py` and
  `host/bootstrap/helpers/read-agent-file.sh`: authenticated file list/read
  routes, dirfd and `O_NOFOLLOW` path walking, open-fd regular-file checks,
  byte/type bounds, fixed media types, response security headers, text
  decoding, object-URL publication, and cleanup.
- Static asset mapping and module closure, CSP/cache/referrer/MIME/framing
  headers, login-screen separation, polling/re-render behavior, logout, and
  the malicious-string/file fixtures in the Playwright smoke suite.

#### Coverage and confidence

- Checklists 1–4: every parent sink and relative API request was enumerated,
  and agent-influenceable values were traced to their final context.
  Untrusted text is escaped or assigned through DOM text APIs; values used in
  quoted attributes are currently constrained enums, validated identifiers,
  or trusted manifests. `badge()` remains a latent quote-unsafe helper
  contract, already recorded as UI-CONTENT-002. No agent-controlled value was
  found selecting a browser destination or privileged action. An independent
  sink sweep agreed: one document (`admin_ui.html`, also served at
  `/oauth/callback`) and thirteen modules, with no `srcdoc`, `document.write`,
  `eval`, `new Function`, `window.open`, dynamic `import()`, service worker, or
  inline event-handler attribute anywhere, and no unquoted attribute
  (`data-page`, `colspan`, `stroke-dasharray`, `<progress value>` all take
  numbers). Login and passkey screens contain no HTML sink at all — every
  string is `textContent`.
- Checklist 4, verified negative worth recording because it was tested as a
  candidate finding and rejected: the OAuth login anchor
  (`admin_ui/health.js:404,414`) interpolates `esc(login.login_url)` into a
  quoted `href`, and `esc()` does not escape `"`. This is not separately
  reportable. The line is byte-identical to `f28b50e87b61`'s
  `admin_ui.js:501,509`, so it is the sink UI-CONTENT-002 was already filed
  against and examined at. More importantly it is unreachable for this axis's
  adversary: the URL comes from the provider's device-code/OAuth endpoint over
  proxied TLS, and both login processes are spawned by root helpers that build
  the environment themselves and exec root-owned binaries with `chattr +i`
  harness configuration, so a malicious agent does not influence the value.
  Every other external link in the UI carries `rel="noopener noreferrer"`;
  this one does not, which is ADM-002's subject, not this axis's.
- Checklist 5: file delivery rejects traversal/symlink swaps, non-regular and
  oversized files, unsupported or mismatched media, SVG, and HTML. Responses
  use fixed JPEG/PNG/WebP/MP4/MOV types, `nosniff`, no-store, and sandbox
  headers; text uses replacement decoding and blob URLs are revoked on
  selection/reset. Confidence is lower for decompression/dimension behavior
  because no hostile-media corpus or live decoder test was run.
- Checklist 6: the parent asset map and imports are fixed and same-origin,
  with no CDN, analytics, remote font/import, prefetch, or service worker.
  CSP, `base-uri`, frame/object restrictions, referrer policy, cache policy,
  and MIME headers provide defense in depth.
- Checklist 7: authentication views, 401 transitions, polling updates, lazy
  payload rendering, navigation, and logout/reload were traced. Agent data
  does not persist active content or trigger an authenticated mutation in the
  reviewed paths.
- Checklist 8: existing smoke fixtures cover quote/markup filenames,
  script-looking text, image decoding, and desktop/mobile overflow, but were
  read rather than rerun. They are not an exhaustive malicious fixture matrix
  for every renderer, window/download/clipboard path, CSP report, object-URL
  lifetime, or media edge case. Confidence is high for static source/sink
  containment and medium for browser/media implementation edges. Neither
  reviewer could run them: there is no live Kern host and loopback TCP is
  blocked in the review sandbox, so `tests/smoke-ui/` (Playwright against
  `run_admin_ui_mock.py`) did not execute, and no claim on this axis rests on
  observed DOM, network log, CSP report, object-URL lifetime, clipboard, or
  mobile layout. One concrete test-coverage gap: the suite's login fixtures are
  benign `https://` URLs only (`run_admin_ui_mock.py:1650-1664`), so the OAuth
  `href` sink discussed above has no hostile-fixture coverage even though it is
  the UI's one externally-sourced attribute value.
- Checklist 3, unmitigated but not a finding: Unicode bidi/RLO and other
  formatting controls are stripped or annotated nowhere, so agent-chosen text
  (a process command line, a network-log target, a tool approval summary) can
  render visually reordered beside trusted labels. No scenario was found in
  which this crosses a table cell or forges a specific trusted control — each
  agent string is confined to its own cell — so it is recorded here rather than
  in the register. Producer-side caps that bound the exposure were confirmed:
  1 MiB file read, 500-byte approval summary, 64 KiB payloads,
  `MAX_CHANGED_PATHS`, `ACTIVITY_TEXT_BYTES`.
- Checklist 5, additional verified negatives on the file viewer: a file that
  grows between `fstat` and the copy is truncated to the announced
  `Content-Length`, and one that shrinks leaves `remaining > 0`, kills the
  helper, and sets `close_connection`. An over-long or unterminated header
  line, non-JSON, missing/out-of-range `size_bytes`, or a `media_type` mismatch
  all abort before headers are committed. Because `_authenticate()` requires
  the `X-Kern-Csrf` header, `/v1/agent-files/content` cannot be reached by a
  top-level navigation at all, so hostile HTML/SVG/polyglot content cannot be
  rendered as a same-origin document regardless of the type checks.
