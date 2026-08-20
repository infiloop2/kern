# CI Testing

| Level | Command | Needs network? | Needs AWS? | Needs provider login? |
| --- | --- | --- | --- | --- |
| Static type checks | `python3 -m mypy --config-file mypy.ini` and `python3 -m pyright --project pyrightconfig.json` | No | No | No |
| Unit tests | `./tests/scripts/test` | No | No | No |
| Admin UI mock smoke | `python3 tests/smoke-ui/admin_ui_smoke.py --port 3100` | No | No | No |
| Real Lima host smoke | `python3 tests/smoke/smoke_lima.py` | Yes | No | No |

Run the static type checks and unit tests on every change; the admin UI mock
smoke runs in CI and is also useful locally while editing the files under
`host/runtime/admin_api/admin_ui/`. The automatic
[Fresh Lima smoke](fresh-lima-smoke.md) boots a real local host on every push
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
test execution is a potential data-exfiltration vector. So CI builds a minimal
Ubuntu image (`.github/ci/sandbox.Dockerfile`) and runs the compile and test
steps inside it with `--network none`, all capabilities dropped,
`no-new-privileges`, a read-only source mount, and a non-root user
(`.github/ci/run-in-sandbox.sh`). The workflow token is read-only and the
checkout does not persist credentials.

Consequently the **unit-test job** can never reach the internet or any account.
The admin UI mock smoke is safe there because it uses only localhost and
in-memory mock data. A separate read-only-token job runs the real Lima smoke
with network access and KVM but receives no provider or repository-write
credential. The real Lima and AWS smokes run automatically for trusted `main`
code; pull request runs require an explicit repository-admin request. The live
AWS stage workflows run separately and only after a repository admin starts
them.

## Admin UI mock smoke (`tests/smoke-ui/`)

For admin UI development, run the single-page UI against a deterministic local
mock backend instead of a deployed host:

```bash
python3 tests/smoke-ui/run_admin_ui_mock.py --port 8000 --demo
```

Open `http://127.0.0.1:8000/` and log in with password `dev`. Demo mode starts
Codex, Claude Code, and Hermes active with representative usage values so the
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
available, install Chromium and WebKit too:

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m playwright install chromium webkit
```

Then run:

```bash
python3 tests/smoke-ui/admin_ui_smoke.py --port 3100 --webkit
```

The smoke starts the mock server, exercises the core flows in Chromium, and
runs the generated Web App worker-startup regression in WebKit. The Chromium
coverage includes
operator flows across thread/session views, network and GitHub controls, files,
processes, bundled tools and approvals, audit logs, and workspaces
at desktop and mobile dimensions. CI installs Playwright, Chromium, and WebKit
during the Docker image build, then runs this smoke through
`.github/ci/run-in-sandbox.sh` with `--network none`. On development boxes with
a preinstalled Playwright browser cache, the smoke reuses the newest cached
Chromium automatically. To use a specific browser binary, set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE=/path/to/chrome`.
