"""Focused query/grant, diagnostics and preview regressions."""
from __future__ import annotations

from http import HTTPStatus
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import re
import threading
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.workspace import agent_api
from host.runtime.workspace.web_apps import backend, collections
from host.runtime.core import pgclient
from tests.workspaces.web_apps import preview, smoke

ROOT = Path(__file__).resolve().parents[1]


def examples():
    source = (ROOT / "host/bootstrap/agent-home/references/web-app-ui.md").read_text()
    return [{"revision": 1, "html": html, "css": css, "javascript": js}
            for html, css, js in re.findall(r"```html\n(.*?)\n```\n```css\n(.*?)\n```\n```javascript\n(.*?)\n```", source, re.S)]


class QueryGrantContractTests(unittest.TestCase):
    def cursor(self, fetches):
        cursor = MagicMock()
        cursor.fetchone.side_effect = fetches
        self.enterContext(patch.object(collections.db, "transaction")).return_value.__enter__.return_value = cursor
        self.enterContext(patch.object(backend, "_require_web_app"))
        return cursor

    def test_projection_is_in_sql_and_sort_uses_original_values(self):
        cursor = self.cursor([(7, "now"), (2,)])
        cursor.fetchall.return_value = [("a", {"title": None}), ("b", {})]
        result = backend.query_collection("app-1", "leads", {"fields": ["title"], "sort": {"field": "score", "direction": "desc"}})
        self.assertEqual(result["rows"], [{"id": "a", "value": {"title": None}}, {"id": "b", "value": {}}])
        sql, params = cursor.execute.call_args.args
        self.assertIn("jsonb_each(value_json)", sql)
        self.assertIn("jsonb_object_agg", sql)
        self.assertIn("ORDER BY value_json -> %s DESC NULLS LAST, row_id ASC", sql)
        self.assertEqual(params[:4], ("title", "app-1", "leads", "score"))
        for param in params:
            pgclient._encode_parameter(param)  # The host driver does not adapt Python lists.

    def test_count_only_does_not_select_rows(self):
        cursor = self.cursor([(7, "now"), (42,)])
        result = backend.query_collection("app-1", "leads", {"count_only": True})
        self.assertEqual((result["rows"], result["total"], result["next_offset"]), ([], 42, None))
        cursor.fetchall.assert_not_called()
        self.assertEqual(cursor.execute.call_count, 2)

    def test_query_budget_counts_serialized_utf8_rows(self):
        cursor = self.cursor([(1, "now"), (2,)])
        cursor.fetchall.return_value = [("a", {"text": "é" * 40}), ("b", {"text": "é" * 40})]
        with patch.object(collections, "MAX_COLLECTION_QUERY_RESPONSE_BYTES", 150):
            with self.assertRaises(backend.WorkspaceError) as error:
                backend.query_collection("app-1", "leads", {})
        self.assertEqual(error.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertIn("fields", error.exception.message)
        self.assertIn("limit", error.exception.message)

    def test_query_rejects_invalid_fields_count_and_unknown_keys(self):
        with patch.object(backend, "_require_web_app"):
            for request in ({"fields": []}, {"fields": ["a"] * 33}, {"fields": [None]}, {"count_only": 1}, {"mystery": True}):
                with self.subTest(request=request), self.assertRaises(backend.WorkspaceError):
                    backend.query_collection("app-1", "leads", request)

    def test_collection_caller_is_forwarded_from_authenticated_identity(self):
        with patch.object(agent_api.agent_notices, "record") as notice, patch.object(backend, "route_agent", return_value={}) as route:
            body = {"thread_id": "app-1"}
            agent_api.dispatch_call("POST", "/agent/apps/app-1/collections/leads/actions", body, peer_thread_id="thread-42")
            self.assertEqual(route.call_args.kwargs["peer_thread_id"], "thread-42")
            self.assertEqual(notice.call_args.args[0], "thread-42")

    def test_non_grantee_and_wrong_collection_fail_under_row_lock(self):
        for caller, collection in [(None, "leads"), ("thread-9", "leads"), ("thread-42", "private")]:
            with self.subTest(caller=caller, collection=collection):
                cursor = self.cursor([(1, "", "", "", "{}", "now", False, {"thread-42": ["leads"]}, False)])
                with self.assertRaises(backend.WorkspaceError) as error:
                    backend.apply_collection_actions("app-1", collection, {"expected_revision": 1, "operations": [{"action": "upsert", "id": "one", "value": {}}]}, caller)
                self.assertEqual(error.exception.status, HTTPStatus.FORBIDDEN)
                self.assertIn("app-1", error.exception.message)
                self.assertIn("FOR UPDATE", cursor.execute.call_args.args[0])
                self.assertEqual(cursor.execute.call_count, 1)

    def test_grant_bounds_rejected(self):
        for body in ({"thread_id": "app-1", "collections": ["leads"]}, {"thread_id": "thread-0", "collections": []}, {"thread_id": "thread-2", "collections": ["leads"] * 17}, {"thread_id": "thread-2", "collections": ["UPPER"]}):
            with self.subTest(body=body), self.assertRaises(backend.WorkspaceError):
                backend.update_collection_grants("app-1", body, None)
        cursor = self.cursor([({f"thread-{n}": ["leads"] for n in range(1, 17)}, False, False)])
        with self.assertRaises(backend.WorkspaceError):
            backend.update_collection_grants("app-1", {"thread_id": "thread-99", "collections": ["leads"]}, None)
        self.assertEqual(cursor.execute.call_count, 1)

    def test_render_storage_caps_match(self):
        source = (ROOT / "host/runtime/workspace/web_apps/ui/personal_web_app_builder.js").read_text()
        for name, expected in [("HTML", backend.MAX_HTML_BYTES), ("CSS", backend.MAX_CSS_BYTES)]:
            expression = re.search(rf"const MAX_RENDER_{name}_BYTES = ([0-9 *]+);", source)[1]
            value = 1
            for factor in expression.split("*"):
                value *= int(factor)
            self.assertEqual(value, expected)

    def test_report_rejects_over_400_bytes_and_unknown_keys(self):
        with patch.object(backend, "_require_web_app"):
            for body in ({"report": "é" * 201}, {"report": "\ud800"}, {"report": "x", "caller": "app-1"}):
                with self.assertRaises(backend.WorkspaceError):
                    backend.save_runtime_report("app-1", body)


class PreviewLintTests(unittest.TestCase):
    def test_each_forbidden_global(self):
        for name in ("fetch", "XMLHttpRequest", "WebSocket", "setTimeout", "setInterval", "importScripts", "localStorage", "indexedDB", "document.body", "window.name", "navigator.language"):
            findings = preview.lint_ui({"javascript": f"app.onLoad(() => {{ {name}; }});"})
            self.assertIn("forbidden-global", [item["code"] for item in findings])
            for item in findings:
                self.assertEqual(set(item), {"code", "message", "snippet"})

    def test_load_actions_and_syntax(self):
        findings = preview.lint_ui({"html": '<button data-action="save"></button><input data-enter-action="enter"><div data-drop-action="move"></div>', "javascript": 'app.on("unused", () => {});'} )
        self.assertEqual([f["code"] for f in findings].count("missing-handler"), 3)
        self.assertIn("unused-handler", [f["code"] for f in findings])
        self.assertIn("missing-onload", [f["code"] for f in findings])
        if preview.shutil.which("node"):
            self.assertIn("javascript-syntax", [f["code"] for f in preview.lint_ui({"javascript": "app.onLoad(() => {"})])

    def test_cli_loads_saved_bodies_and_lints_before_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            ui = {"revision": 1, "html": "<p>Saved</p>", "css": "", "javascript": "app.onLoad(() => {});"}
            data = {"revision": 1, "data": {"summary": {}}}
            (folder / "ui.json").write_text(json.dumps({"app": ui}))
            (folder / "data.json").write_text(json.dumps({"app": data}))
            rows = [{"id": "one", "value": {"title": "Saved"}}]
            (folder / "rows.json").write_text(json.dumps(rows))
            args = ["--ui", str(folder / "ui.json"), "--data", str(folder / "data.json"), "--collection", f"leads={folder / 'rows.json'}", "--port", "8008", "--lint"]
            with patch.object(preview, "create_server") as create, patch("builtins.print"):
                self.assertEqual(preview.main(args), 0)
                create.assert_called_once_with(ui, data, {"leads": rows}, 8008)
                create.return_value.serve_forever.assert_called_once()
                create.return_value.server_close.assert_called_once()
            invalid_args = args.copy()
            invalid_args[5] = f"UPPER={folder / 'rows.json'}"
            with patch.object(preview, "create_server") as create, redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit) as error:
                    preview.main(invalid_args)
                self.assertEqual(error.exception.code, 2)
                self.assertIn("collection must start with a lowercase letter", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())
                create.assert_not_called()
            for filename, valid in (("ui.json", ui), ("data.json", data)):
                invalid_bodies = [None, [], {"app": None}, {"app": []}, {"app": "invalid"},
                                  {"app": {**valid, "revision": True}}, {"app": {**valid, "revision": -1}}]
                invalid_bodies.append({"app": {**valid, "javascript": None}} if filename == "ui.json" else {"app": {"revision": 1}})
                for body in invalid_bodies:
                    with self.subTest(filename=filename, body=body):
                        (folder / filename).write_text(json.dumps(body))
                        with patch.object(preview, "create_server") as create, redirect_stderr(io.StringIO()) as stderr:
                            with self.assertRaises(SystemExit) as error:
                                preview.main(args)
                            self.assertEqual(error.exception.code, 2)
                            self.assertIn("error:", stderr.getvalue())
                            self.assertNotIn("Traceback", stderr.getvalue())
                            create.assert_not_called()
                (folder / filename).write_text(json.dumps({"app": valid}))
            ui["javascript"] = "fetch('x')"
            (folder / "ui.json").write_text(json.dumps({"app": ui}))
            with patch.object(preview, "create_server") as create, patch("builtins.print"):
                self.assertEqual(preview.main(args), 1)
                create.assert_not_called()

    def test_starters_lint_and_port_bounds(self):
        self.assertEqual(len(examples()), 3)
        for ui in examples():
            self.assertEqual(preview.lint_ui(ui), [])
        with self.assertRaises(ValueError):
            preview.create_server({}, {}, {}, 9000)


class BuilderChromiumTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.server = preview.create_server({"revision": 1, "html": "<p>Saved preview</p>", "css": "", "javascript": "app.onLoad(() => {});"}, {"revision": 1, "data": {}}, {}, 8008)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.context = self.browser.new_context(service_workers="block")
        self.page = self.context.new_page()
        self.logs = []
        self.page.on("console", lambda message: self.logs.append(message.text))

    def tearDown(self):
        self.context.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def open(self, ui=None, data=None, rows=None):
        from playwright.sync_api import expect
        workspace = smoke.WORKSPACES["app-1"]
        if ui:
            workspace["app"].update(ui)
        if data is not None:
            workspace["app"]["data"] = data
        if rows is not None:
            workspace["collections"] = rows
        self.page.goto("http://127.0.0.1:8008/")
        self.page.locator("#password").fill("dev")
        self.page.get_by_role("button", name="Log in", exact=True).click()
        expect(self.page.locator("#app")).to_be_visible()
        smoke._open_host_app(self.page, "App preview")
        self.frame = self.page.locator("#panel-workspace-web-apps")
        expect(self.frame.locator("#generated-host")).to_be_visible()

    def test_saved_preview_and_read_many_use_one_request(self):
        from playwright.sync_api import expect
        requests = []
        self.page.on("request", lambda request: requests.append(request) if request.url.endswith("/runtime/data/read") else None)
        script = '''app.onLoad(async () => {
          const values = await app.readMany(Array.from({length:8}, (_, i) => ["b"+i]));
          app.render('<p id="result">'+JSON.stringify(values)+'</p>');
        }, {data:"targeted"});'''
        self.open({"javascript": script}, {f"b{i}": i for i in range(7)})
        expect(self.frame.locator("#result")).to_have_text("[0,1,2,3,4,5,6,null]")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].post_data_json, {"paths": [[f"b{i}"] for i in range(8)], "missing": "null"})

    def test_collection_query_passes_fields_and_count_only(self):
        from playwright.sync_api import expect
        self.open({"javascript": '''app.onLoad(async () => {
          const page = await app.query("leads", {fields:["title"], limit:1});
          const count = await app.query("leads", {count_only:true});
          app.render('<p id="result">'+JSON.stringify([page.rows, count.rows, count.total])+'</p>');
        }, {data:"targeted"});'''}, rows={"leads": [{"id": "one", "value": {"title": None, "large": "omitted"}}]})
        expect(self.frame.locator("#result")).to_have_text('[[{"id":"one","value":{"title":null}}],[],1]')

    def test_read_many_rejects_invalid_paths_before_host_request(self):
        from playwright.sync_api import expect
        requests = []
        self.page.on("request", lambda request: requests.append(request.url) if request.url.endswith("/runtime/data/read") else None)
        self.open({"javascript": '''app.onLoad(async () => {
          let rejected = 0;
          for (const paths of [[], [[]], [[-1]], [[true]], [["x".repeat(129)]], Array(17).fill(["x"]), [["x"],["x"]]]) {
            try { await app.readMany(paths); } catch (_) { rejected++; }
          }
          app.render('<p id="result">'+rejected+'</p>');
        }, {data:"targeted"});'''})
        expect(self.frame.locator("#result")).to_have_text("7")
        self.assertEqual(requests, [])

    def test_slow_read_succeeds_inside_fifteen_seconds(self):
        from playwright.sync_api import expect
        self.page.add_init_script('''(() => {
          const original = window.fetch;
          window.fetch = (...args) => String(args[0]).endsWith('/runtime/data/read')
            ? new Promise(resolve => setTimeout(resolve, 13000)).then(() => original(...args))
            : original(...args);
        })();''')
        self.open({"javascript": '''app.onLoad(async () => {
          const [value] = await app.readMany([["value"]]);
          app.render('<p id="result">'+value+'</p>');
        }, {data:"targeted"});'''}, {"value": "slow success"})
        expect(self.frame.locator("#result")).to_have_text("slow success", timeout=15_000)
        self.assertNotIn("last_runtime_report", smoke.WORKSPACES["app-1"])

    def test_starter_examples_render_without_sanitizer_removals(self):
        from playwright.sync_api import expect
        for index, ui in enumerate(examples()):
            with self.subTest(example=index):
                if index:
                    self.context.close()
                    self.context = self.browser.new_context(service_workers="block")
                    self.page = self.context.new_page()
                    self.page.on("console", lambda message: self.logs.append(message.text))
                self.logs.clear()
                self.open(ui, {"summary": {"completed": 7, "total": 10, "label": "A < B"}}, {
                    "leads": [{"id": "one", "value": {"title": "A < B", "status": "new", "private": "omitted"}}],
                    "cards": [{"id": "one", "value": {"title": "A < B", "lane": "todo"}}],
                })
                target = ["td", ".card", ".dashboard h2"][index]
                expect(self.frame.locator(target).first).to_have_text("A < B")
                self.assertFalse([line for line in self.logs if line.startswith("[app-sanitizer]")], self.logs)
                if index == 2:
                    expect(self.frame.locator(".bar")).to_have_class("bar width-70")

    def test_sanitizer_does_not_log_handled_links_and_control_types(self):
        from playwright.sync_api import expect
        html = ('<a href="https://example.com">x</a>'
                '<button id="typed-button" type="button" data-action="probe">x</button>'
                '<input id="typed-input" type="checkbox">')
        script = ('app.onLoad(() => app.render(' + json.dumps(html + '<p id="ready">Ready</p>') + '));'
                  + 'app.on("probe", () => app.render(' + json.dumps('<p id="probe-done">Done</p><img src="https://example.com/image"><input id="rejected-input" type="file"><button id="rejected-button" type="submit">x</button>') + '));')
        self.open({"html": html, "javascript": script})
        expect(self.frame.locator("#ready")).to_have_text("Ready")
        expect(self.frame.locator("#typed-button")).to_have_attribute("type", "button")
        expect(self.frame.locator("#typed-input")).to_have_attribute("type", "checkbox")
        expect(self.frame.locator(".kern-copy-link")).to_have_attribute("data-kern-copy-href", "https://example.com")
        self.assertEqual([line for line in self.logs if line.startswith("[app-sanitizer]")], [])
        self.frame.locator("#typed-button").click()
        expect(self.frame.locator("#probe-done")).to_have_text("Done")
        self.assertEqual([line for line in self.logs if line.startswith("[app-sanitizer]")],
                         ["[app-sanitizer] dropped tag IMG", "[app-sanitizer] rewrote INPUT.type to text", "[app-sanitizer] rewrote BUTTON.type to button"])
        expect(self.frame.locator("#rejected-input")).to_have_attribute("type", "text")
        expect(self.frame.locator("#rejected-button")).to_have_attribute("type", "button")
        self.assertEqual(self.frame.locator("#generated-host img").count(), 0)

    def test_sanitizer_logs_rejections_and_caps_per_render(self):
        from playwright.sync_api import expect
        ui = {"html": '<section id="safe">' + '<img src="https://invalid.test">' * 250 + '</section>', "css": "@supports(display:grid){p{color:red}}", "javascript": "app.onLoad(() => {});"}
        self.open(ui)
        expect(self.frame.locator("#safe")).to_be_attached()
        self.assertEqual(len([line for line in self.logs if line.startswith("[app-sanitizer]")]), 200)
        self.assertEqual(self.frame.locator("#safe img").count(), 0)

    def test_runtime_reports_wait_for_actual_post_settlement_in_order(self):
        from playwright.sync_api import expect
        self.page.add_init_script('''(() => {
          const original = window.fetch;
          window.__reportCalls = [];
          window.__reportsSettled = 0;
          window.fetch = (...args) => {
            if (!String(args[0]).endsWith('/runtime/report')) return original(...args);
            window.__reportCalls.push(JSON.parse(args[1].body).report);
            const delay = window.__reportCalls.length === 1 ? 13000 : 0;
            return new Promise(resolve => setTimeout(resolve, delay))
              .then(() => original(...args))
              .finally(() => { window.__reportsSettled++; });
          };
        })();''')
        self.open({"html": '<button data-action="first">First</button><button data-action="second">Second</button>', "javascript": '''
          app.onLoad(() => {});
          app.on("first", () => { throw new Error("first"); });
          app.on("second", () => { throw new Error("second"); });
        '''})
        self.frame.get_by_role("button", name="First", exact=True).click()
        self.page.wait_for_function("() => window.__reportCalls.length === 1")
        self.frame.locator("#runtime-status").evaluate("element => { element.textContent = ''; }")
        self.frame.get_by_role("button", name="Second", exact=True).click()
        expect(self.frame.locator("#runtime-status")).to_contain_text("This app action failed")
        self.assertEqual(self.page.evaluate("window.__reportCalls.length"), 1)
        self.page.wait_for_function("() => window.__reportsSettled === 2", timeout=20_000)
        reports = self.page.evaluate("window.__reportCalls")
        self.assertIn('action "first"', reports[0])
        self.assertIn('action "second"', reports[1])
        self.assertEqual(smoke.WORKSPACES["app-1"]["last_runtime_report"], reports[1])

    def test_endless_worker_is_terminated_and_report_is_sent(self):
        from playwright.sync_api import expect
        self.open({"javascript": "app.onLoad(() => { while (true) {} }, {data:'targeted'});"})
        expect(self.frame.locator("#runtime-status")).to_contain_text("took too long", timeout=18_000)
        # Wait for the asynchronous report POST rather than inspecting a race.
        for _ in range(30):
            if "last_runtime_report" in smoke.WORKSPACES["app-1"]:
                break
            self.page.wait_for_timeout(100)
        self.assertIn("startup was stopped", smoke.WORKSPACES["app-1"]["last_runtime_report"])

    def test_existing_turn_budget_journey(self):
        self.open()
        smoke.turn_budget_smoke(self.page)

    def test_late_write_reconciles_before_queued_action(self):
        from playwright.sync_api import expect
        self.page.add_init_script('''(() => {
          const original = window.fetch;
          let late = true;
          window.fetch = (...args) => {
            if (String(args[0]).endsWith('/runtime/actions') && late) {
              late = false;
              window.__lateWritePending = true;
              return new Promise(resolve => setTimeout(resolve, 16500)).then(() => original(...args));
            }
            return original(...args);
          };
        })();''')
        self.open({"html": '<button data-action="add">Add</button><p id="result">0</p>', "javascript": '''
          const render = value => app.render('<button data-action="add">Add</button><p id="result">'+value+'</p>');
          app.onLoad(async () => render(await app.read(["count"])), {data:"targeted"});
          app.on("add", async () => { const count = await app.read(["count"]); render(await app.set(["count"], count+1)); });
        '''}, {"count": 0})
        button = self.frame.get_by_role("button", name="Add", exact=True)
        button.click()
        self.page.wait_for_function("() => window.__lateWritePending")
        button.click()
        expect(self.frame.locator("#runtime-status")).to_contain_text("took too long", timeout=18_000)
        try:
            expect(self.frame.locator("#result")).to_have_text("2", timeout=10_000)
        except AssertionError:
            print("late-write mock:", smoke.WORKSPACES["app-1"]["app"], "report:", smoke.WORKSPACES["app-1"].get("last_runtime_report"), "console:", self.logs)
            raise
        self.assertEqual(smoke.WORKSPACES["app-1"]["app"]["data"]["count"], 2)
