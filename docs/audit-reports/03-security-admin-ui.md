# Audit: Admin UI Exposure of Agent-Controlled Content

Finding ID prefix: `UI-CONTENT`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can agent-controlled data displayed by the Admin UI execute code, alter
trusted UI, cause the browser to take an unintended action, or trick the
operator—for example, by opening a link?

## Reviewed commits

Latest reviewed commit: none.

| Commit | Reviewed by |
| --- | --- |
| _None yet_ | _No completed review_ |

The historical reviews under **Collaborative review** cover the parent UI
rendering surface but not the current end-to-end file-viewing checklist, so
they remain partial.

## Findings

| Finding | Severity | Found at | Found by | Description | Resolution |
| --- | --- | --- | --- | --- | --- |
| UI-CONTENT-002 | Low | `f28b50e87b61` | Claude Opus 4.8 | The HTML helpers interpolated badge values into markup and escaped text without quotes. Existing callers used controlled enums or text positions and CSP blocked inline script, but the helper contract was a latent XSS footgun if a future caller supplied attacker-controlled attribute data. Use context-correct escaping or DOM construction. | Open |

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

These historical sweeps cover the parent Admin UI rendering surface. They
support the existing findings but remain partial because they did not complete
the current end-to-end file-viewing checklist.

### `f28b50e87b61` — partial

Contributors: Claude Opus 4.8 (claude-opus-4-8); GPT-5.5 (gpt-5.5)

Methodology: static reading of the served HTML/JS and the API's static-asset
handling; enumerated every dynamic DOM sink and every URL the page can
request. No browser-driven test.

#### What was reviewed

- `host/runtime/admin_ui.html` (every external reference, inline script/style,
  favicon), `host/runtime/admin_ui.js` (every `innerHTML`/`setHtml` sink, the
  `esc()`/`badge()` helpers and the `api()` fetch targets), and
  `host/runtime/admin_ui.css` by reference.
- `host/runtime/admin_api/service.py`: `_send_ui_asset`, browser security
  headers, `_send_json`, and cache headers.
- Existing Admin UI smoke tests for malicious-looking strings and layout.

#### Coverage details

- **No external calls.** The HTML references only same-origin `/admin_ui.css`
  and `/admin_ui.js`, an inline `data:` favicon, and inline SVGs; there are no
  external scripts, styles, fonts, images, or prefetch. Every runtime request
  goes through `api()` to a relative `/v1/...` path. OAuth login URLs are
  rendered as operator-clicked `<a target="_blank">`, not auto-fetched. This
  is enforced in depth by the response CSP `default-src 'self'; connect-src
  'self'; img-src 'self' data:; script-src 'self'; style-src 'self'` plus
  `base-uri 'none'`/`object-src 'none'`.
- **No agent-reachable XSS.** The genuinely attacker-controlled strings —
  agent file names/paths, file contents, task output, process command lines,
  and proxied hosts/paths in the network log — are rendered via `textContent`/
  `dataset` (the file list) or `esc()` in text (`<pre>`/`<td>`) contexts, none
  in an attribute position. `esc()` neutralizes `<`/`>`/`&` there.

#### Coverage and confidence

- Checklist 1 (sink sweep): every `setHtml`/`innerHTML` template in the JS was
  enumerated; the only unescaped or quote-unsafe helpers are `badge()` and
  `esc()` (UI-CONTENT-002), and I traced their callers to confirm none currently pass
  agent-controlled data into an attribute.
- Checklist 2 (per-string XSS): file names/contents, thread/task ids, network
  event fields, process cmdlines, and provider metadata each traced to a safe
  sink.
- Browser containment: `frame-ancestors`/`X-Frame-Options` were present.
- UI and JSON responses also set `Referrer-Policy: no-referrer` and
  `X-Content-Type-Options: nosniff`.
- Byte-level external-origin check: confirmed by reading the
  served HTML and the static-asset handler; assets are read from disk and
  served with fixed content types and `nosniff`. Not verified against a live
  rendered response or a browser CSP report.
- Login, cookie, and CSRF observations from this historical combined sweep
  are tracked by axis 04 and do not define this rendering axis.
- Not done: no live browser test, no automated CSP evaluator run. Given the
  CSP strength and header-only auth, residual risk is low.
