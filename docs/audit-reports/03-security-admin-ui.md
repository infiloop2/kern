# Audit: Admin UI Exposure of Agent-Controlled Content

Finding ID prefix: `UI-CONTENT`. See [README.md](README.md) for the sweep process,
finding format, and severity scale.

## Audit question

Can agent-controlled data displayed by the Admin UI execute code, alter
trusted UI, cause the browser to take an unintended action, or trick the
operator—for example, by opening a link?

## Reviewed commits

Latest reviewed commit: `a597a8d063d735d11c30d1f2b2f6b66e7479ceac`.

| Commit | Reviewed by |
| --- | --- |
| `a597a8d063d735d11c30d1f2b2f6b66e7479ceac` | GPT-6 (Codex) |

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

### `a597a8d063d735d11c30d1f2b2f6b66e7479ceac`

Reviewed by: GPT-6 (Codex)

Methodology: inventoried browser sinks across the trusted parent modules and
traced agent-controlled sources into rendering, link, file, and approval
contexts. Read the relevant helpers and complete sensitive file/approval
paths, with targeted source inspection elsewhere. Ran the Chromium desktop
and mobile mock smoke, including hostile file fixtures. This is a source and
fixture review, not an exhaustive browser fuzzer over every renderer.

#### What was reviewed

- `host/runtime/admin_api/admin_ui/`: shell/API helpers, login/passkey,
  runtime/provider, network, tools/approvals, logs, processes, files, health,
  diagnostics and service worker. Searched HTML, attributes, style/URL sinks,
  navigation, popups, clipboard, object URLs, workers and module loading.
- Text/quoted-attribute escaping in `helpers.js`; approval summaries, exact
  payloads and results in `approvals.js`; filenames/media and download flows
  in `files.js`; network/process/log/provider error rendering; static asset
  exposure and response policy in `admin_api/service.py`.
- `read-agent-file`, upload handling and Admin file routes, including fixed
  MIME/disposition, byte bounds, regular-file rules and download-only handling
  for active document formats. Workspace rich text and generated UI are
  reviewed separately in axis 05.

#### Outcome and coverage

No new parent-UI execution or automatic-disclosure defect was confirmed.
The existing quote-escaping remediation remains present. File bytes with
HTML/SVG behavior are delivered through the explicit download path; the
Chromium smoke passed hostile filename/content, unchanged download-byte and
navigation checks.

- Checklist 1: enumerated parent modules and sink categories with a repository
  search, then inspected sensitive dynamic templates and DOM construction.
  Module/worker sources are host-owned release assets; dynamic agent values
  are not used as module specifiers.
- Checklists 2–3: traced filenames, paths, process lines, network denials,
  error text, account metadata, Git push/repository descriptions and tool
  approval JSON into escaping or `textContent`. Approval action ids use
  encoded routing and escaped attributes. No new unsafe text-to-markup
  crossing was found. Not every Unicode/bidi/size-limit combination was
  browser-tested in every view.
- Checklist 4: inspected intentional link, copy and download construction,
  including URL validation and opener/referrer handling. Agent text is not
  allowed to assign arbitrary active HTML, form actions, or asset sources.
  Copy controls intentionally copy displayed data after operator interaction.
- Checklist 5: traced confined file reads and bounded response handling to
  the browser. Media object URLs are explicitly created/revoked; active
  document types do not receive an inline executable preview. Existing
  helper/unit and actual Chromium download tests ran. Browser media decoder
  vulnerabilities and an exhaustive polyglot corpus were not tested.
- Checklist 6: reviewed local asset imports, CSP, `base-uri`, frame/object
  policy, MIME/referrer/cache headers and service-worker behavior. The worker
  removes old caches and does not cache private API responses. The mock
  smoke checks normal browser requests; it is not evidence of real
  Cloudflare header delivery.
- Checklist 7: inspected text-only error paths, authentication-view
  separation and polling/navigation state guards; exercised existing stale
  response and overload recovery smoke cases. No forged approval control or
  trusted-dialog overlay was reproduced.
- Checklist 8: Chromium desktop/mobile smoke ran successfully through core
  and Workspace checks, including navigation, overload recovery and malicious
  file fixtures. The combined command then failed at the optional WebKit
  canary because the browser is absent. Installation was denied by Kern's
  proxy (`host_not_allowed` for `cdn.playwright.dev` and
  `playwright.download.prss.microsoft.com`); no alternate download route was
  used. CI's browser job supplies WebKit. Exhaustive malicious fixtures in
  every parent renderer, clipboard/window instrumentation, and every maximum
  size/partial-update case were not performed.

Confidence is strongest for escaping and the tested file/download behavior,
with remaining limitations in renderer fuzz coverage and untested browser
versions. No deployed operator session or private browser data was used.
