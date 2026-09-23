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

They need `openssl` (proxy certificate tests), `bash` (rendered-script
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
