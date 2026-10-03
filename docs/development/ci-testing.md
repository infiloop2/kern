# CI Testing

| Level | Command | Needs network? | Needs AWS? | Needs provider login? |
| --- | --- | --- | --- | --- |
| Static type checks | `python3 -m mypy --config-file mypy.ini` and `python3 -m pyright --project pyrightconfig.json` | No | No | No |
| Unit tests | `./tests/scripts/test` | No | No | No |
| Admin UI mock smoke | `python3 tests/smoke-ui/admin_ui_smoke.py --port 3100` | No | No | No |
| Real Lima host smoke | `python3 tests/smoke/smoke_lima.py` | Yes | No | No |

Run the static type checks and unit tests on every change; the admin UI mock
smoke runs in CI and is also useful locally while editing the files under
`host/runtime/admin_api/admin_ui/`. In the canonical `infiversehq/kern`
repository, the automatic [Fresh Lima smoke](fresh-lima-smoke.md) boots a real local host on every push
to `main` and when a repository admin requests it for a pull request. It runs
the fresh AWS smoke's complete credential-free live-host contract alongside
Lima-specific definition, lifecycle, disk, replacement, and recovery checks.
Live AWS checks are covered separately in
[Fresh AWS smoke](fresh-aws-smoke.md) and [Persistent AWS stage](persistent-aws-stage.md).

## Kern Cloud compatibility

Every PR also runs the [hardcoded Kern Cloud contract](kern-cloud-contract.md).
This static source test lives under `tests/` and runs with the normal unit
suite, as part of the required **Run all host tests** aggregate. It does not access Infiverse or run lifecycle
commands. Contract changes require matching Infiverse support updates.

## Static type checks (run on every change, and in CI)

```bash
python3 -m mypy --config-file mypy.ini
python3 -m pyright --project pyrightconfig.json
```

The type-check configs currently target `host/`, the production deploy and host
runtime package. The live AWS harnesses under `tests/smoke/` and `tests/stage/`
are intentionally outside the type-check gate for now; they remain covered by
syntax compilation and their live workflows.

## Unit tests (run on every change, and in CI)

```bash
./tests/scripts/test
```

For a focused run, pass ordinary unittest module or test names, for example
`./tests/scripts/test test_personal_web_app_builder`.

The launcher requires Python 3.11 or newer and the `playwright` package. Set
`KERN_TEST_PYTHON` to an executable interpreter or launcher to override
`python3`. The launcher checks the Python version, required `unittest` API, and
Playwright import before starting discovery, so an unsupported environment
fails with a short override example instead of producing a full suite of import
errors. Environment-specific interpreter paths belong in local development
guidance rather than this repository script.

They need `openssl` (proxy certificate tests), `curl` (Browser relay tests), `bash` (rendered-script
checks), and PostgreSQL server binaries (admin-state tests), but **no network
and no credentials**: the Codex protocol is exercised against a scripted fake
app-server, the Claude Code adapter is exercised against scripted CLI
processes, the AWS deploy against fake `aws`/`ssh`/`scp` CLIs, the proxy
against a local TLS server, and admin state against a throwaway Postgres
cluster that `tests/pg_harness.py` starts on a Unix socket in a temp directory
(one cluster per test run, truncated between tests). No Python database driver
is needed: the runtime brings its own protocol client.

If PostgreSQL is missing locally, the database-backed tests skip with
instructions; install it with `apt install postgresql` (or point
`KERN_TEST_PG_BIN` at a Postgres `bin/` directory). The CI sandbox image
installs it, so CI always runs the full suite.

## CI: tests inside a no-network sandbox

`.github/workflows/test-all-host.yml` runs on every pull request and push to
`main`. Because a pull request can change code that the workflow then executes,
test execution is a potential data-exfiltration vector. CI uses a dependency-only
Ubuntu image (`.github/ci/sandbox.Dockerfile`) and runs the compile and test
steps inside it with `--network none`, all capabilities dropped,
`no-new-privileges`, a read-only source mount, and a non-root user
(`.github/ci/run-in-sandbox.sh`). The workflow token is read-only and the
checkout does not persist credentials.

Test commands inside the sandbox cannot reach the internet or any account.
The admin UI mock smoke is safe there because it uses only localhost and
in-memory mock data. A separate read-only-token job runs the real Lima smoke
with network access and KVM but receives no provider or repository-write
credential. The real Lima and AWS smokes run automatically for trusted `main`
code; pull request runs require an explicit repository-admin request. The live
AWS stage workflows run separately and only after a repository admin starts
them.

Unit/database tests (including compilation and type checks), the Chromium core
smoke, and the Workspace smoke run concurrently on separate GitHub runners.
The Workspace job also runs the focused WebKit canary for generated Web App
worker startup. Each sandbox has its own writable workspace and temporary
database. The required **Run all host tests** check succeeds only when all
three suites succeed; a skipped or cancelled suite cannot satisfy it. All
three suites run on PRs and main. Running the browser scopes in parallel
reduces elapsed CI time while using an additional runner and image setup.

### Reusing the CI image

The canonical repository's `ci-sandbox-image.yml` publishes a private dependency
image to `ghcr.io/infiversehq/kern-ci` when the Dockerfile or test requirements
change on main. It can also be dispatched on main to seed a missing image.
The requirements live at `.github/ci/requirements.txt`, alongside the Dockerfile,
so edits to either build input use the same protected push path.
Tags contain a SHA-256 hash of both build inputs. Ordinary test jobs pull that
exact image directly into Docker, avoiding BuildKit cache restoration and a
second image export/import. If an image is missing, inaccessible (for example,
in a public mirror or fork), or the PR changes its dependencies, the test job
builds those exact inputs locally. It never silently uses older dependencies.

Publishing uses the built-in `GITHUB_TOKEN` with `packages: write` in a separate
main-only workflow. Test jobs have only `packages: read`, remove registry login
credentials before testing, and never publish PR images. The image build context
contains only the Dockerfile and test requirements. Package creation must be
allowed by the organization's policy; no extra registry secret is needed.
There is no Actions image cache or image artifact. Fresh runners still download
and unpack the image, and dependency changes still incur a cold build. Update
the Dockerfile (even a refresh comment) when a fresh OS/dependency rebuild is
needed without changing requirements; existing dependency tags are reused.

The pgvector extension is compiled with `OPTFLAGS=""` so a shared image is
portable across runner CPUs. HTTP test fixtures use a 10 ms server shutdown
polling interval; request timeouts and assertion deadlines are unchanged.

### Public mirror

The public mirror keeps compilation, type checks, unit/database tests, and the
mock browser smoke. Real AWS/Lima smoke and AWS stage workflows are restricted
to `infiversehq/kern`; the mirror does not provision live test environments or
require their credentials. These guards live in the canonical source so public
syncs preserve them. Existing public workflow runs are not changed retroactively.

## Admin UI mock smoke (`tests/smoke-ui/`)

For admin UI development, run the single-page UI against a deterministic local
mock backend instead of a deployed host:

```bash
python3 tests/smoke-ui/run_admin_ui_mock.py --port 8000 --demo
```

Open `http://127.0.0.1:8000/` and log in with password `dev`. Demo mode starts
Codex, Claude Code, Grok, and Hermes active with representative usage values so the
runtime toolbar is useful for visual inspection. The port is an argument so
multiple developers or agents can choose non-conflicting localhost ports.

The mock backend serves `host/runtime/admin_api/admin_ui/index.html` and implements the `/v1/*`
routes the UI uses with in-memory data. It is for UI wiring and interaction
checks only; it does not validate the real admin API, host state, sudo helpers,
agent runtimes, or network proxy.

In the local mock only, click the green version-status badge in the toolbar to
toggle between the upgrade-available and latest-version states.

To run type checks or the automated browser smoke locally, install the
development-only test dependencies once. If no cached browser builds are
available, install Chromium and, when using `--webkit`, WebKit too:

```bash
python3 -m pip install -r .github/ci/requirements.txt
python3 -m playwright install chromium webkit
```

Then run:

```bash
python3 tests/smoke-ui/admin_ui_smoke.py --port 3100 --webkit
```

The smoke starts the mock server, exercises the core flows in Chromium, and
runs one generated Web App worker-startup canary in WebKit. That canary covers
login, App creation, slow sandbox loading, the isolated worker bridge, the
first render, networkless CSP, and generated-App file links. The Chromium
coverage includes operator flows across thread/session views, network and GitHub controls, files,
processes, bundled tools and approvals, audit logs, and workspaces
at desktop and mobile dimensions. CI installs Playwright, Chromium, and WebKit
during the Docker image build, then runs this smoke through
`.github/ci/run-in-sandbox.sh` with `--network none`. On development boxes with
a preinstalled Playwright browser cache, the smoke reuses the newest cached
Chromium automatically. To use a specific browser binary, set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome`.
This override selects the admin UI test browser only. The Browser service adapter
journey in the `core` and `all` scopes always uses Playwright's managed full
Chromium on its own authenticated Xvfb display, matching the deployed service's
windowed `chromium` channel. Install with
`python3 -m playwright install --with-deps chromium --no-shell` even when using
a custom executable for the surrounding UI tests; a headless-shell-only cache
or an installation without Xvfb is insufficient. The adapter also verifies that
unauthenticated local X11 clients cannot connect and that displays close with
their browser. It tests native keyboard events and restored authentication with
fixtures, not live X login acceptance.

### Choosing browser coverage

Put deterministic logic, API contracts, validation, and state transitions in
unit tests. Add a Chromium smoke only when the behavior depends on a rendered
browser interaction, browser security boundary, layout, or navigation. Prefer
a focused journey over extending an unrelated end-to-end path, and synchronize
on observable state or requests rather than fixed sleeps.

Chromium owns broad browser coverage. Do not copy a Chromium journey into the
WebKit canary merely for cross-browser coverage. Expand WebKit only for a
confirmed WebKit-specific production regression or an engine-sensitive browser
primitive that Chromium cannot represent. Keep such coverage to the smallest
reproduction, with deterministic local fixtures and condition-based waits. A
new broad feature journey belongs in Chromium unless its change explains why
WebKit is materially different.

Before committing a WebKit change, run its focused path repeatedly. A timeout
increase or retry is not a reliability fix: remove races between the action and
the observed request/state, and make failures identify the condition that did
not settle. The WebKit canary is intentionally not a claim of complete Safari
or iOS coverage; device-only behavior still needs the appropriate live or
manual check.

### Reliability is part of coverage

Required browser checks should give a dependable signal on their first attempt.
Do not hide intermittent failures with whole-test retries, larger timeouts, or
weaker assertions. Prefer fewer focused journeys that prove a user outcome to
many overlapping journeys that depend on incidental timing. Use the same tests
in the canonical repository and public mirror.

Following [Playwright's testing guidance](https://playwright.dev/docs/best-practices)
and [auto-waiting assertions](https://playwright.dev/python/docs/actionability):

- Give each independent journey a fresh browser context and owned fixture data.
  A fresh context does not reset the shared mock server: intercept the relevant
  endpoints or explicitly restore mutations. Make fixtures valid for the UI
  action, such as enabling a provider before clicking its login button.
- Await the state that enables the next action. A response event can precede
  JSON parsing and rendering; `networkidle` does not prove that a particular
  control is ready. Use locators and assertions for visible state, and await the
  actual refresh promise for request-count contracts.
- Use [controlled time](https://playwright.dev/python/docs/clock) for deadlines.
  `clock.run_for()` dispatches timers but does not join asynchronous work started
  by their callbacks. For a wall-clock cooldown, pause timers, use
  `set_system_time()`, and await one refresh per assertion. Install the clock
  before navigation and pause ahead of its initial time: pausing at the same
  timestamp can already be in the past on a slow runner. Test interval wiring
  separately when it is the behavior under test.
- Keep browser checks for interaction wiring, focus, navigation, isolation and
  visible results. Keep protocol variants and combinatorial state transitions
  below the browser layer. Prefer semantic visibility/containment assertions to
  exact pixel geometry unless geometry is the regression being tested; then
  wait for the final layout condition after resize, not a single snapshot.
- Reproduce a failure before choosing a fix. Repeat the changed focused journey
  with fresh contexts and run the full affected scope. A repeated run validates
  a proposed fix; it is not a retry policy that turns a failing CI run green.

The OAuth recovery journey retains hidden-card suppression, absent-session
cooldown, explicit login, successful recovery and reload checks for both device
code and Claude flows. It controls refresh timing; it does not assert an exact
number of automatically scheduled health ticks. Opening an integration waits
for its replacement controls before requesting recovery, including after reload.

Navigation ordering retains desktop mouse drag/drop, Escape cancellation,
captured-handle preservation across refresh, mobile keyboard reordering, focus,
rejected saves, and order across reloads/tabs. The synthetic CDP touch-drag
journey is intentionally removed: raw coordinates raced sidebar scrolling and
bypassed Playwright's actionability checks. This reduces automated touch gesture
coverage. Mobile keyboard tests do **not** prove native touch dragging or iOS
behavior; those require device checks. Reintroduce an automated gesture check
only with a stable, focused reproduction that justifies its maintenance cost.

Focused commands (also useful for repeated validation):

```bash
python3 tests/smoke-ui/admin_ui_smoke.py --port 8000 --scope oauth-poll
python3 tests/smoke-ui/admin_ui_smoke.py --port 8000 --scope navigation-order
python3 tests/smoke-ui/admin_ui_smoke.py --port 8000 --scope swarm
```

## Codex approval stamp

`approve-codex-review.yml` turns a clean verdict from
`chatgpt-codex-connector[bot]` into an `APPROVE` review by `github-actions[bot]`,
pinned to the reviewed commit. The `stamp-codex-review` action and its
`stamp.py` resolve the reviewed commit, validate the Codex verdict, and create
that approval. It applies to open, non-draft, same-repository PRs targeting
`main`. GitHub Actions must be allowed to create and approve pull requests in
the repository Actions settings.

The separate `authorize-admin-or-codex-stamp` action uses its own `authorize.py`
to check for an active approval stamp on the requested SHA, which must still be
the current PR head. If no stamp qualifies, it calls the existing
`authorize-repo-admin` action. Authorization does not parse Codex comments or
re-evaluate the review verdict. A stamp remains valid until dismissed or the
head changes. Both smoke jobs load these actions from trusted `main` at the
workspace root before executing PR code, including on reruns.

The stamp means Codex found no major issues on that revision; CI checks and
human review remain separate signals. It enables non-admin `/smoke` and
`/lima-smoke` comments for the stamped head. Stale or dismissed approvals,
manual labels, other authors' approvals, and copied verdict text do not grant
smoke authority. Stage commands and manual workflow dispatch remain admin-only.
The new policy takes effect after it lands on `main`.
