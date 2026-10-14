"""Native Prolific contract tests; every HTTP call is mocked, no live token."""
import copy
import json
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

from host.runtime.tools.tools_host import validate_against_schema
from host.tools import prolific as p
from host.tools.prolific import responses, validation
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.web import WebRequestError, ProviderWarning
from test_tools import FakeHostAPI

ID = "645e4403bdd06d5f66d8fbde"
OTHER = "645e4403bdd06d5f66d8fbdf"
TOKEN = "PRIVATE_PROLIFIC_TOKEN"
DRAFT = {"name": "Research preferences", "description": "Choose the best image.", "data_collection_method": "AI_TASK_BUILDER_COLLECTION", "data_collection_id": OTHER, "reward": 100, "total_available_places": 10, "estimated_completion_time": 5}
STUDY = {**DRAFT, "id": ID, "status": "UNPUBLISHED", "filters": [], "currency": "USD", "total_cost": 1500}
COLLECTION = {"name": "Image task", "workspace_id": ID, "task_details": {"task_name": "Choose", "task_introduction": "Read this", "task_steps": "Pick one"}, "collection_items": [{"order": 0, "page_items": ['{"type":"text","content":"Hello"}']}]}


class ProlificTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI()
        self.api.config["PROLIFIC_TOKEN"] = TOKEN
        self.calls = []
        self.study = copy.deepcopy(STUDY)
        self.cost = {"rewards": {"rewards": {"amount": 1000, "currency": "USD"}, "fees": {"amount": 428.57, "currency": "USD"}, "tax": {"amount": 85.72, "currency": "USD"}}}
        self.mock = self.enterContext(patch.object(p, "_request_bytes_and_headers", side_effect=self.http))

    def http(self, method, url, **kw):
        path = urlsplit(url).path.removeprefix("/api/v1")
        body = json.loads(kw["data"]) if kw.get("data") else None
        query = parse_qs(urlsplit(url).query)
        self.calls.append((method, path, query, body))
        self.assertEqual(kw["headers"]["Authorization"], "Token " + TOKEN)
        self.assertEqual(urlsplit(url).netloc, "api.prolific.com")
        self.assertEqual(kw["timeout"], 30)
        self.assertEqual(kw["max_bytes"], responses.MAX_FILTER_BYTES if path == "/filters/" else responses.MAX_BYTES)
        headers = {}
        result = {"id": ID, "name": "Existing collection", "results": []}
        if path == f"/studies/{ID}/" and method == "GET":
            result = self.study
        if path.endswith("/cost/"):
            result = self.cost
        if path == "/workspaces/":
            result = {"results": [{"id": ID, "title": "Research", "currency": "USD", "available_balance": 10000}]}
        if path == "/filters/":
            result = {"results": [{"filter_id": "age", "title": "Age", "type": "range", "question": "Your age?", "min": 18, "max": 100}]}
        if path.endswith("/submissions/counts/"):
            result = {"ACTIVE": 2, "TOTAL": 10}
        if path.endswith("/export"):
            result = {"export_id": OTHER, "status": "generating"}
        if path.endswith("/export/" + OTHER):
            result = {"status": "generating"}
        if path.endswith("/demographic-export/"):
            return b"participant_id,age\nexample,30\n", headers
        return json.dumps(result).encode(), headers

    def call(self, action, params=None):
        params = params or {}
        spec = p.MANIFEST.action(action)
        self.assertEqual(validate_against_schema(params, spec.input_schema), "")
        result = p.BUNDLED_TOOL.execute(action, params, self.api)
        if isinstance(result, ActionExecuted):
            self.assertEqual(validate_against_schema(result.result, spec.output_schema), "")
        return result

    def approve(self, result):
        self.assertIsInstance(result, ActionPendingApproval)
        return p.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(result.approval_id), self.api)

    def test_every_direct_action_request_mapping_and_defaults(self):
        cases = [
            ("get_filters", {"search": "age"}, "GET", "/filters/", {}, None),
            ("get_eligibility_count", {}, "POST", "/eligibility-count/", {}, {"filters": [], "study_type": "STUDY_TYPE_SINGLE"}),
            ("list_studies", {"state": "(ACTIVE|PAUSED)", "search": "Images"}, "GET", "/studies/", {"page": ["1"], "page_size": ["50"], "state": ["(ACTIVE|PAUSED)"], "search": ["Images"]}, None),
            ("create_study", {"study": DRAFT}, "POST", "/studies/", {}, {**DRAFT, "filters": [], "prolific_id_option": "not_required"}),
            ("list_collections", {"workspace_id": ID}, "GET", "/data-collection/collections", {"workspace_id": [ID], "limit": ["200"], "offset": ["0"]}, None),
            ("get_collection", {"collection_id": ID}, "GET", f"/data-collection/collections/{ID}", {}, None),
            ("export_collection", {"collection_id": ID}, "POST", f"/data-collection/collections/{ID}/export", {}, None),
            ("get_collection_export_status", {"collection_id": ID, "export_id": OTHER}, "GET", f"/data-collection/collections/{ID}/export/{OTHER}", {}, None),
        ]
        for action, params, method, path, query, body in cases:
            with self.subTest(action=action):
                self.calls.clear()
                result = self.call(action, params)
                self.assertIsInstance(result, ActionExecuted, result)
                self.assertEqual(self.calls, [(method, path, query, body)])
                self.assertEqual(self.api.costs.calls[-1], ("0", ""))
                if action == "get_collection":
                    self.assertEqual(result.result["preview_url"], f"https://app.prolific.com/data-collection-tool/collections/{ID}?preview=true")
                if action == "export_collection" and "export_id" not in params:
                    self.assertEqual(result.result["export_id"], OTHER)

    def test_account_joins_each_workspaces_projects_and_preserves_wallet_fields(self):
        def provider(method, url, **kw):
            path = urlsplit(url).path
            if path.endswith("/workspaces/"):
                return json.dumps({"results": [{"id": ID, "wallet": {"currency_code": "GBP", "available_balance": 2000}, "extension": "complete"}, {"id": OTHER}]}).encode(), {}
            return json.dumps({"results": [{"id": OTHER, "title": "Project"}]}).encode(), {}
        self.mock.side_effect = provider
        result = self.call("get_account").result
        self.assertEqual(self.mock.call_count, 3)
        self.assertEqual([urlsplit(call.args[1]).path for call in self.mock.call_args_list],
                         ["/api/v1/workspaces/", f"/api/v1/workspaces/{ID}/projects/", f"/api/v1/workspaces/{OTHER}/projects/"])
        self.assertEqual(result["workspaces"][0]["wallet"]["available_balance"], 2000)
        self.assertEqual(result["workspaces"][1]["projects"]["results"][0]["title"], "Project")
        self.assertEqual(json.loads(result["provider_details_json"])["workspaces"][0]["extension"], "complete")

    def test_account_paginates_workspaces_and_projects_with_fixed_paths(self):
        def provider(method, url, **kw):
            path, query = urlsplit(url).path, parse_qs(urlsplit(url).query)
            self.assertEqual(query["limit"], ["100"])
            offset = int(query["offset"][0])
            if path.endswith("/workspaces/"):
                rows, count = [{"id": (ID, OTHER)[offset]}], 2
            elif path == f"/api/v1/workspaces/{ID}/projects/":
                rows, count = [{"id": (ID, OTHER)[offset], "title": "Project"}], 2
            else:
                rows, count = [], 0
            return json.dumps({"results": rows, "meta": {"count": count}}).encode(), {}
        self.mock.side_effect = provider
        result = self.call("get_account").result
        self.assertEqual([row["id"] for row in result["workspaces"]], [ID, OTHER])
        self.assertEqual(len(result["workspaces"][0]["projects"]["results"]), 2)
        self.assertEqual(self.mock.call_count, 5)
        self.assertEqual([parse_qs(urlsplit(c.args[1]).query)["offset"] for c in self.mock.call_args_list],
                         [["0"], ["1"], ["0"], ["1"], ["0"]])

    def test_account_rejects_overbound_stalled_and_repeated_pages(self):
        for pages in (
            [{"results": [{"id": ID}], "meta": {"count": 101}}],
            [{"results": [], "meta": {"count": 1}}],
            [{"results": [{"id": ID}], "meta": {"count": 2}}] * 2,
        ):
            with self.subTest(pages=pages):
                self.mock.reset_mock()
                self.mock.side_effect = [(json.dumps(page).encode(), {}) for page in pages]
                self.assertIsInstance(self.call("get_account"), ActionFailed)
                self.assertEqual(self.mock.call_count, len(pages))
        self.mock.side_effect = [
            (json.dumps({"results": [{"id": ID}]}).encode(), {}),
            (json.dumps({"results": [], "meta": {"count": 101}}).encode(), {}),
        ]
        self.assertIsInstance(self.call("get_account"), ActionFailed)

    def test_submission_fallback_redacts_ip_and_strata_before_projection(self):
        record = {"id": ID, "participant_id": OTHER, "status": "APPROVED", "ip": "192.0.2.1",
                  "strata": {"date of birth": "1990-01-01", "sex": "female", "ethnicity (simplified)": "example"},
                  "unmodeled": "retained"}
        self.mock.side_effect = [(json.dumps({"results": [record]}).encode(), {}),
                                 (b'{"APPROVED":1}', {})]
        result = self.call("list_submissions", {"study_id": ID}).result
        fallback = json.loads(result["provider_details_json"])["results"][0]
        self.assertEqual(fallback["ip"], "[REDACTED]")
        self.assertEqual(fallback["strata"], "[REDACTED]")
        self.assertEqual(fallback["unmodeled"], "retained")
        self.assertEqual(result["results"][0]["participant_id"], OTHER)
        for sensitive in ("192.0.2.1", "1990-01-01", "female", "ethnicity"):
            self.assertNotIn(sensitive, json.dumps(result))
        self.mock.side_effect = [(b'participant_id,age,sex,ethnicity\nexample,30,female,example\n', {})]
        exported = self.call("export_demographics", {"study_id": ID})
        with exported.open_stream() as opened:
            self.assertIn("age,sex,ethnicity", opened.source.read().decode())

    def test_eligibility_exposes_privacy_floor_and_workspace_scope(self):
        for workspace in (None, ID):
            with self.subTest(workspace=workspace):
                params = {} if workspace is None else {"workspace_id": workspace}
                self.mock.side_effect = None
                self.mock.return_value = (b'{"count":0}', {})
                result = self.call("get_eligibility_count", params).result
                self.assertEqual(result["count"], 0)
                self.assertIn("fewer than five", result["count_note"])
                self.assertEqual(result["workspace_id"], workspace)
                self.assertEqual(result["workspace_scope"], "provider_current_workspace_unverified" if workspace is None else "requested_workspace")
                body = json.loads(self.mock.call_args.kwargs["data"])
                self.assertEqual(body.get("workspace_id"), workspace)

    def test_account_fallback_redacts_workspace_members(self):
        workspace = {"id": ID, "title": "Research", "users": [{"id": OTHER, "name": "Member Name", "email": "member@example.com"}],
                     "extension": "retained"}
        project = {"id": ID, "title": "Project", "users": [{"name": "Project Member", "email": "project@example.com"}]}
        self.mock.side_effect = [(json.dumps({"results": [workspace]}).encode(), {}), (json.dumps({"results": [project]}).encode(), {})]
        result = self.call("get_account").result
        fallback = json.loads(result["provider_details_json"])["workspaces"][0]
        self.assertEqual(fallback["users"], "[REDACTED]")
        self.assertEqual(fallback["projects"]["results"][0]["users"], "[REDACTED]")
        self.assertEqual(fallback["extension"], "retained")
        self.assertEqual(result["workspaces"][0]["id"], ID)
        for sensitive in (OTHER, "Member Name", "member@example.com", "Project Member", "project@example.com"):
            self.assertNotIn(sensitive, json.dumps(result))

    def test_submissions_join_counts_and_get_study_cost_matches_start_approval(self):
        result = self.call("list_submissions", {"study_id": ID}).result
        self.assertEqual(result["counts"], {"ACTIVE": 2, "TOTAL": 10})
        self.assertEqual(self.calls, [("GET", f"/studies/{ID}/submissions/", {"limit": ["200"], "offset": ["0"]}, None),
                                      ("GET", f"/studies/{ID}/submissions/counts/", {}, None)])
        for state, transition in (("UNPUBLISHED", "PUBLISH"), ("PAUSED", "START")):
            self.study["status"] = state
            self.calls.clear()
            result = self.call("get_study", {"study_id": ID}).result
            self.assertEqual(len(self.calls), 2)
            self.assertEqual(self.calls[-1][1:3], (f"/studies/{ID}/cost/", {"is_projected": ["true"]}))
            pending = self.call("start_study", {"study_id": ID})
            payload = self.api.approvals.records[pending.approval_id].payload
            self.assertEqual(result["cost"]["estimated_usd"], payload["study"]["estimated_total_cost_usd"])
            self.assertEqual(result["cost"]["projected_total_minor_units"], payload["study"]["projected_total_minor_units"])
            self.assertEqual(payload["study"]["transition"], transition)
        self.study["status"] = "ACTIVE"
        self.calls.clear()
        self.assertIsNone(self.call("get_study", {"study_id": ID}).result["cost"])
        self.assertEqual(len(self.calls), 1)

    def test_collection_create_replace_and_extension_fidelity(self):
        for replace in (False, True):
            content = copy.deepcopy(COLLECTION)
            if replace:
                del content["workspace_id"]
            content["collection_items"][0]["id"] = ID
            params = {"collection": json.dumps(content)}
            if replace:
                params["collection_id"] = ID
            self.calls.clear()
            result = self.call("save_collection", params)
            self.assertIsInstance(result, ActionPendingApproval, result)
            self.assertTrue(all(c[0] == "GET" for c in self.calls))
            payload = self.api.approvals.records[result.approval_id].payload
            self.assertEqual(payload["target"]["mode"], "replace" if replace else "create")
            self.assertIsInstance(self.approve(result), ApprovalExecuted)
            content["collection_items"][0]["page_items"] = [{"type": "text", "content": "Hello"}]
            self.assertEqual(self.calls[-1][3], content)
            self.assertEqual(self.calls[-1][0:2], ("PUT", f"/data-collection/collections/{ID}/") if replace else ("POST", "/data-collection/collections"))
        self.assertIsInstance(self.call("save_collection", {"collection_id": ID, "collection": COLLECTION}), ActionFailed)
        draft = {**DRAFT, "metadata": "experiment one"}
        self.assertIsInstance(self.call("create_study", {"study": json.dumps(draft)}), ActionExecuted)
        self.assertEqual(self.calls[-1][3]["metadata"], "experiment one")

    def test_task_builder_studies_omit_external_url_and_completion_codes(self):
        for method in ("AI_TASK_BUILDER_COLLECTION", "AI_TASK_BUILDER_BATCH"):
            for as_json in (False, True):
                with self.subTest(method=method, as_json=as_json):
                    draft = {k: v for k, v in DRAFT.items() if k not in ("external_study_url", "completion_codes")}
                    draft.update(data_collection_method=method, data_collection_id=OTHER)
                    result = self.call("create_study", {"study": json.dumps(draft) if as_json else draft})
                    self.assertIsInstance(result, ActionExecuted)
                    body = self.calls[-1][3]
                    self.assertEqual(body["data_collection_id"], OTHER)
                    self.assertNotIn("external_study_url", body)
                    self.assertNotIn("completion_codes", body)
                    self.assertEqual(body["prolific_id_option"], "not_required")
                    self.study = {**body, "id": ID, "status": "UNPUBLISHED", "currency": "USD"}
                    self.assertIsInstance(self.approve(self.call("start_study", {"study_id": ID})), ApprovalExecuted)
        base = {k: v for k, v in DRAFT.items() if k not in ("data_collection_method", "data_collection_id")}
        for extra in ({}, {"data_collection_method": "AI_TASK_BUILDER_COLLECTION"},
                      {"data_collection_method": "invalid", "data_collection_id": OTHER},
                      {"data_collection_method": "AI_TASK_BUILDER_COLLECTION", "data_collection_id": OTHER, "external_study_url": "https://survey.example.com/study"},
                      {"data_collection_method": "AI_TASK_BUILDER_COLLECTION", "data_collection_id": OTHER, "access_details": []},
                      {"external_study_url": "https://survey.example.com/study"}):
            self.calls.clear()
            self.assertIsInstance(self.call("create_study", {"study": json.dumps({**base, **extra})}), ActionFailed)
            self.assertEqual(self.calls, [])

    def test_batch_places_optional_and_other_study_types_require_places(self):
        batch = {k: v for k, v in DRAFT.items() if k not in ("external_study_url", "completion_codes", "total_available_places")}
        batch.update(data_collection_method="AI_TASK_BUILDER_BATCH", data_collection_id=OTHER)
        for as_json in (False, True):
            self.assertIsInstance(self.call("create_study", {"study": json.dumps(batch) if as_json else batch}), ActionExecuted)
            self.assertNotIn("total_available_places", self.calls[-1][3])
        for invalid in ({**batch, "data_collection_method": "AI_TASK_BUILDER_COLLECTION"},
                        {k: v for k, v in DRAFT.items() if k != "total_available_places"},
                        {**batch, "total_available_places": 0}, {**batch, "total_available_places": True}):
            self.calls.clear()
            self.assertIsInstance(self.call("create_study", {"study": json.dumps(invalid)}), ActionFailed)
            self.assertEqual(self.calls, [])

    def test_project_study_preserves_omitted_devices_and_explicit_choices(self):
        for as_json in (False, True):
            for explicit in (False, True):
                draft = {**DRAFT, "project": OTHER}
                if explicit:
                    draft.update(device_compatibility=["desktop"], peripheral_requirements=[])
                self.assertIsInstance(self.call("create_study", {"study": json.dumps(draft) if as_json else draft}), ActionExecuted)
                body = self.calls[-1][3]
                for field in ("device_compatibility", "peripheral_requirements"):
                    if explicit:
                        self.assertEqual(body[field], draft[field])
                    else:
                        self.assertNotIn(field, body)

    def test_collection_approval_binds_exact_content_and_token_without_direct_guard(self):
        content = copy.deepcopy(COLLECTION)
        content["name"] = "Research for alice@example.com"
        with patch.object(self.api.outbound, "guard_request_parameter_string", side_effect=AssertionError("direct guard")):
            pending = self.call("save_collection", {"collection": content})
            self.assertEqual(self.calls, [])
            payload = self.api.approvals.records[pending.approval_id].payload
            self.assertEqual(payload["parameters"]["collection"]["name"], content["name"])
            self.assertEqual(payload["target"]["workspace_id"], ID)
            self.api.config["PROLIFIC_TOKEN"] = "CHANGED"
            self.assertIsInstance(self.approve(pending), ActionFailed)
            self.assertEqual(self.calls, [])
        self.assertEqual(self.api.costs.calls, [])

    def test_collection_replacement_checks_target_before_queue_and_write(self):
        content = {k: v for k, v in COLLECTION.items() if k != "workspace_id"}
        pending = self.call("save_collection", {"collection_id": ID, "collection": content})
        record = self.api.approvals.records[pending.approval_id]
        self.assertIn("Omitted content is deleted", record.summary)
        self.assertEqual(record.payload["target"]["collection_id"], ID)
        self.assertEqual(self.calls, [("GET", f"/data-collection/collections/{ID}", {}, None)])
        self.mock.side_effect = [(json.dumps({"id": OTHER, "name": "Wrong"}).encode(), {})]
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertFalse(any(c[0] in ("POST", "PUT") for c in self.calls))
        self.mock.side_effect = [(json.dumps({"id": OTHER, "name": "Wrong"}).encode(), {})]
        self.assertIsInstance(self.call("save_collection", {"collection_id": ID, "collection": content}), ActionFailed)
        self.assertEqual(self.api.costs.calls, [])

    def test_collection_task_details_optional_for_create_and_replace(self):
        for replace in (False, True):
            for as_json in (False, True):
                content = {k: v for k, v in COLLECTION.items() if k != "task_details" and (not replace or k != "workspace_id")}
                params = {"collection": json.dumps(content) if as_json else content}
                if replace:
                    params["collection_id"] = ID
                self.assertIsInstance(self.approve(self.call("save_collection", params)), ApprovalExecuted)
                self.assertNotIn("task_details", self.calls[-1][3])
        for task in (None, {}, {"task_name": "Only name"}):
            self.calls.clear()
            self.assertIsInstance(self.call("save_collection", {"collection": json.dumps({**COLLECTION, "task_details": task})}), ActionFailed)
            self.assertEqual(self.calls, [])

    def test_publish_binds_full_preview_then_revalidates_and_reports_reservation(self):
        pending = self.call("start_study", {"study_id": ID})
        self.assertEqual([c[0] for c in self.calls], ["GET", "GET"])
        payload = self.api.approvals.records[pending.approval_id].payload
        self.assertEqual(payload["study"]["estimated_total_cost_usd"], "15.15")
        for k in ("name", "reward", "total_available_places", "estimated_completion_time", "filters"):
            self.assertIn(k, payload["study"])
        self.assertNotIn(TOKEN, json.dumps(payload))
        self.assertEqual(self.api.costs.calls, [])
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.assertEqual(self.calls[-1], ("POST", f"/studies/{ID}/transition/", {}, {"action": "PUBLISH"}))
        self.assertEqual(self.api.costs.calls, [("15.15", ID)])

    def test_halt_only_requests_approval_then_stops_exact_study(self):
        self.study.update(status="ACTIVE", name="Study " + TOKEN)
        pending = self.call("halt_study", {"study_id": ID.upper()})
        self.assertIsInstance(pending, ActionPendingApproval)
        self.assertEqual(self.calls, [("GET", f"/studies/{ID}/", {}, None)])
        self.assertEqual(self.api.costs.calls, [])
        record = self.api.approvals.records[pending.approval_id]
        self.assertEqual(record.payload["parameters"], {"study_id": ID})
        self.assertEqual(record.payload["study"]["id"], ID)
        self.assertEqual(record.payload["study"]["status"], "ACTIVE")
        self.assertIn("permanently stop", record.summary.lower())
        self.assertNotIn(TOKEN, json.dumps(record.payload) + record.summary)
        self.calls.clear()
        self.study["status"] = "PAUSED"  # STOP remains the approved effect, never PAUSE/START.
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.assertEqual(self.calls, [("GET", f"/studies/{ID}/", {}, None),
                                     ("POST", f"/studies/{ID}/transition/", {}, {"action": "STOP"})])
        self.assertEqual(self.api.costs.calls, [("0", "")])

    def test_halt_rejects_changed_token_or_unverified_target_without_stopping(self):
        pending = self.call("halt_study", {"study_id": ID})
        self.api.config["PROLIFIC_TOKEN"] = "CHANGED"
        self.calls.clear()
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertEqual(self.calls, [])
        self.api.config["PROLIFIC_TOKEN"] = TOKEN
        for field, value in (("id", OTHER), ("name", None), ("status", None)):
            with self.subTest(field=field):
                self.study = copy.deepcopy(STUDY)
                pending = self.call("halt_study", {"study_id": ID})
                self.study[field] = value
                self.calls.clear()
                self.assertIsInstance(self.approve(pending), ActionFailed)
                self.assertFalse(any(c[0] == "POST" for c in self.calls))
                self.assertIsInstance(self.call("halt_study", {"study_id": ID}), ActionFailed)
        self.assertEqual(self.api.costs.calls, [])

    def test_halt_rejects_removed_selector_and_missing_or_invalid_id(self):
        spec = p.MANIFEST.action("halt_study")
        self.assertEqual(set(spec.input_schema["properties"]), {"study_id"})
        self.assertEqual(spec.input_schema["required"], ["study_id"])
        for params in ({}, {"study_id": "invalid"}, {"study_id": ID, "action": "pause"},
                       {"study_id": ID, "action": "stop"}):
            with self.subTest(params=params):
                self.assertIsInstance(p.BUNDLED_TOOL.execute("halt_study", params, self.api), ActionFailed)
        self.mock.assert_not_called()

    def test_publish_rejects_changed_token_definition_state_and_cost(self):
        for field, value in (("status", "PAUSED"), ("id", OTHER), ("reward", 200), ("total_available_places", 11), ("filters", [{"filter_id": "age"}]), ("name", "Changed"), ("total_cost", 2000), ("status", "ACTIVE")):
            with self.subTest(field=field):
                self.study = copy.deepcopy(STUDY)
                pending = self.call("start_study", {"study_id": ID})
                self.study[field] = value
                self.calls.clear()
                self.assertIsInstance(self.approve(pending), ActionFailed)
                self.assertFalse(any(c[0] == "POST" for c in self.calls))
        self.study = copy.deepcopy(STUDY)
        pending = self.call("start_study", {"study_id": ID})
        self.api.config["PROLIFIC_TOKEN"] = "CHANGED"
        self.calls.clear()
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertEqual(self.calls, [])

    def test_projected_gbp_cost_tax_rounding_and_resume_deduplication(self):
        del self.study["currency"]
        del self.study["total_cost"]
        for part in self.cost["rewards"].values():
            part["currency"] = "GBP"
        pending = self.call("start_study", {"study_id": ID})
        self.assertEqual(self.calls[-1][2], {"is_projected": ["true"]})
        self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.assertEqual(self.api.costs.records[ID]["amount_usd"], "22.72")
        self.study["status"] = "PAUSED"
        self.assertIsInstance(self.approve(self.call("start_study", {"study_id": ID})), ApprovalExecuted)
        self.assertEqual(self.calls[-1][3], {"action": "START"})
        self.assertEqual(len(self.api.costs.records), 1)
        self.assertEqual(len(self.api.costs.calls), 2)

    def test_submission_approval_accepts_only_nonempty_bounded_submission_ids(self):
        spec = p.MANIFEST.action("approve_submissions")
        self.assertEqual(set(spec.input_schema["properties"]), {"submission_ids"})
        self.assertEqual(spec.input_schema["required"], ["submission_ids"])
        for params in ({}, {"submission_ids": []}, {"submission_ids": [ID] * 1001},
                       {"submission_ids": ["invalid"]}, {"study_id": ID, "participant_ids": [OTHER]},
                       {"submission_ids": [ID], "participant_ids": [OTHER]}, {"submission_ids": [ID], "study_id": ID}):
            with self.subTest(params=params):
                self.assertIsInstance(p.BUNDLED_TOOL.execute("approve_submissions", params, self.api), ActionFailed)
        self.mock.assert_not_called()

    def test_all_other_approvals_bind_token_and_exact_parameters(self):
        cases = [("approve_submissions", {"submission_ids": [ID]}, "POST", "/submissions/bulk-approve/", {"submission_ids": [ID]})]
        for action, params, method, path, body in cases:
            with self.subTest(action=action):
                self.calls.clear()
                pending = self.call(action, params)
                self.assertEqual(self.calls, [])
                result = self.approve(pending)
                self.assertIsInstance(result, ApprovalExecuted)
                self.assertIn("queued", result.message)
                self.assertIn("list_submissions", result.message)
                self.assertNotIn("completed", result.message)
                self.assertEqual(self.calls[-1], (method, path, {}, body))
                self.assertEqual(self.api.costs.calls[-1], ("0", ""))





    def test_recursive_guards_reject_secrets_pii_and_encoded_values_before_network(self):
        for bad in ("alice@example.com", "AKIAIOSFODNN7EXAMPLE", "alice%2540example.com"):
            cases = [("get_filters", {"search": bad}), ("list_studies", {"search": bad}),
                     ("get_eligibility_count", {"filters": json.dumps([{"filter_id": "custom", "selected_values": [bad]}])}),
                     ("create_study", {"study": json.dumps({**DRAFT, "description": bad})})]
            for action, params in cases:
                with self.subTest(action=action, bad=bad):
                    self.calls.clear()
                    result = self.call(action, params)
                    self.assertIsInstance(result, ActionFailed, result)
                    self.assertEqual(self.calls, [])

    def test_hosted_creation_rejects_removed_fields_including_json_and_nulls(self):
        properties = p.MANIFEST.action("create_study").input_schema["properties"]["study"]["oneOf"][0]["properties"]
        for field in ("external_study_url", "access_details", "completion_codes", "prolific_id_option"):
            self.assertNotIn(field, properties)
            for value in (None, [], "https://survey.example.com/study", "not_required", "question", "url_parameters"):
                draft = {**DRAFT, field: value}
                for as_json in (False, True):
                    result = p.BUNDLED_TOOL.execute("create_study", {"study": json.dumps(draft) if as_json else draft}, self.api)
                    self.assertIsInstance(result, ActionFailed)
        for field in ("data_collection_method", "data_collection_id"):
            self.assertIsInstance(self.call("create_study", {"study": json.dumps({k: v for k, v in DRAFT.items() if k != field})}), ActionFailed)
        self.mock.assert_not_called()

    def test_nested_identifiers_reject_every_non_string_shape_before_http(self):
        for field in ("participant_group", "workspace_id", "project", "id"):
            for bad in (None, 123, True, [ID], {"id": ID}):
                with self.subTest(field=field, bad=bad):
                    draft = copy.deepcopy(DRAFT)
                    draft["extension"] = {field: bad}
                    self.assertIsInstance(self.call("create_study", {"study": json.dumps(draft)}), ActionFailed)
        self.mock.assert_not_called()
        # The documented optional nullable pool field is omitted by StudyDraft.
        self.assertIsInstance(self.call("create_study", {"study": json.dumps({**DRAFT, "credential_pool_id": None})}), ActionExecuted)
        self.assertNotIn("credential_pool_id", self.calls[-1][3])

    def test_credential_pool_metadata_survives_response_and_approval_redaction(self):
        self.study.update(credential_pool_id=OTHER, has_credentials=True, credentials={"password": "hidden"})
        result = self.call("get_study", {"study_id": ID}).result
        self.assertEqual(result["study"]["credential_pool_id"], OTHER)
        self.assertIs(result["study"]["has_credentials"], True)
        self.assertNotIn("hidden", json.dumps(result))
        pending = self.call("start_study", {"study_id": ID})
        payload = self.api.approvals.records[pending.approval_id].payload
        self.assertIn(OTHER, json.dumps(payload))
        self.assertNotIn("hidden", json.dumps(payload))
        self.assertEqual(responses.redact({"has_credentials": "hidden", "credential_pool_id": "hidden"}),
                         {"has_credentials": "[REDACTED]", "credential_pool_id": "[REDACTED]"})

    def test_numeric_values_in_open_json_cannot_bypass_parameter_guard(self):
        for action, params in (
            ("get_eligibility_count", {"filters": '[{"filter_id":"custom","selected_range":{"lower":4155551234}}]'}),
            ("create_study", {"study": json.dumps({**DRAFT, "custom_numeric_field": 4155551234})}),
        ):
            with self.subTest(action=action):
                self.assertIsInstance(self.call(action, params), ActionFailed)
        self.mock.assert_not_called()

    def test_validation_rejects_bad_ids_ranges_bools_and_mixed_approval_inputs(self):
        cases = [("get_study", {"study_id": "../../invalid"}), ("list_studies", {"page_size": 201}),
                 ("list_collections", {"workspace_id": ID, "limit": True}), ("get_filters", {"search": " "}),
                 ("get_filters", {"search": "age", "limit": 101}), ("halt_study", {"study_id": ID, "action": "START"}),
                 ("approve_submissions", {"submission_ids": [ID], "study_id": ID}), ("approve_submissions", {}),
                 ("create_study", {"study": {**DRAFT, "reward": True}}),
                 ("create_study", {"study": json.dumps({**DRAFT, "data_collection_method": None})})]
        for action, params in cases:
            with self.subTest(action=action):
                self.assertIsInstance(p.BUNDLED_TOOL.execute(action, params, self.api), ActionFailed)
        self.mock.assert_not_called()

    def test_response_fidelity_redaction_size_and_error_mapping(self):
        body = {"id": ID, "new_field": {"all": [1, "provider text", {"token": TOKEN}], "echo": TOKEN}, "name": TOKEN}
        self.mock.side_effect = None
        self.mock.return_value = (json.dumps(body).encode(), {})
        result = self.call("get_study", {"study_id": ID})
        self.assertEqual(result.result["study"]["name"], "[REDACTED]")
        decoded = json.loads(result.result["provider_details_json"])
        self.assertEqual(decoded["study"]["new_field"]["all"][:2], [1, "provider text"])
        self.assertNotIn(TOKEN, json.dumps(result.result))
        self.mock.return_value = (b"x" * (responses.MAX_BYTES + 1), {})
        self.assertIsInstance(self.call("list_studies"), ActionFailed)
        for status, expected in ((401, "Reconfigure"), (402, "insufficient funds"), (403, "insufficient funds"), (429, "Back off"), (400, "rejected")):
            self.mock.side_effect = WebRequestError(TOKEN, status=status, body=b"insufficient funds " + TOKEN.encode())
            result = self.call("list_studies")
            self.assertIn(expected, result.error)
            self.assertNotIn(TOKEN, result.error)
        self.mock.side_effect = WebRequestError(TOKEN, status=500, body=TOKEN.encode())
        with self.assertRaises(ProviderWarning) as cm:
            self.call("list_studies")
        self.assertNotIn(TOKEN, str(cm.exception))
        self.assertEqual(cm.exception.response_body, "")

    def test_long_text_and_guard_return_value_reach_wire(self):
        draft = {**DRAFT, "description": "ordinary research words " * 150}
        result = self.call("create_study", {"study": draft})
        self.assertIsInstance(result, ActionExecuted, result)
        with patch.object(self.api.outbound, "guard_request_parameter_string", side_effect=lambda value, **kw: "rewritten" if value == "needle" else value):
            self.assertIsInstance(self.call("list_studies", {"search": "needle"}), ActionExecuted)
            self.assertEqual(self.calls[-1][2]["search"], ["rewritten"])


    def test_optional_nulls_defaults_and_escaped_response_secrets(self):
        self.assertIsInstance(self.call("list_studies", {"state": None, "search": None, "page": None}), ActionExecuted)
        self.assertEqual(self.calls[-1][2], {"page": ["1"], "page_size": ["50"]})
        self.assertIsInstance(self.call("get_eligibility_count", {"filters": None}), ActionExecuted)
        encoded = "".join("\\u%04x" % ord(c) for c in TOKEN)
        self.mock.side_effect = None
        self.mock.return_value = (json.dumps({"unknown": encoded, "nested": {"password": "hide me"}}).encode(), {})
        result = self.call("list_studies")
        details = json.loads(result.result["provider_details_json"])
        self.assertEqual(details["unknown"], "[REDACTED]")
        self.assertEqual(details["nested"]["password"], "[REDACTED]")

    def test_spending_rejects_unverified_currency_and_malformed_costs(self):
        for currency in ("EUR", "INVALID"):
            self.study["currency"] = currency
            result = self.call("start_study", {"study_id": ID})
            self.assertIsInstance(result, ActionFailed)
            self.assertEqual(self.api.approvals.records, {})
        self.study.pop("currency")
        self.study.pop("total_cost")
        for bad in ({}, {"rewards": {"rewards": {"amount": 100, "currency": "USD"}}, "bonuses": []}):
            self.cost = bad
            self.assertIsInstance(self.call("start_study", {"study_id": ID}), ActionFailed)
        self.assertEqual(self.api.approvals.records, {})

    def test_approval_token_rebinding_applies_to_every_operator_action(self):
        for action, params in (("approve_submissions", {"submission_ids": [ID]}), ("start_study", {"study_id": ID})):
            self.api.config["PROLIFIC_TOKEN"] = TOKEN
            pending = self.call(action, params)
            self.api.config["PROLIFIC_TOKEN"] = "REPLACEMENT"
            self.calls.clear()
            self.assertIsInstance(self.approve(pending), ActionFailed)
            self.assertEqual(self.calls, [])

    def test_filter_search_bounds_catalogue_and_prioritizes_matching_choices(self):
        catalogue = {"results": [{"filter_id": f"group_{i}", "title": "Group", "type": "select", "question": "Choose group",
                                  "choices": {str(j): f"ordinary choice {j} " + "word " * 40 for j in range(100)}} for i in range(100)]}
        catalogue["results"][70]["choices"]["99"] = "Rare Label " + TOKEN
        raw = json.dumps(catalogue).encode()
        self.assertGreater(len(raw), responses.MAX_BYTES)
        self.assertLess(len(raw), responses.MAX_FILTER_BYTES)
        self.mock.side_effect = None
        self.mock.return_value = (raw, {})
        result = self.call("get_filters", {"search": "rArE lAbEl"}).result
        self.assertEqual(result["match_count"], 1)
        self.assertFalse(result["truncated"])
        row = result["results"][0]
        self.assertEqual(row["id"], "group_70")
        self.assertEqual(row["choices"][0], {"id": "99", "label": "Rare Label [REDACTED]"})
        self.assertEqual(len(row["choices"]), 50)
        self.assertEqual(row["choice_count"], 100)
        self.assertTrue(row["truncated"])
        self.assertNotIn("provider_details_json", result)
        self.assertNotIn("?", self.mock.call_args.args[1])
        result = self.call("get_filters", {"search": "group", "limit": 2}).result
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(result["match_count"], 100)
        self.assertTrue(result["truncated"])
        result = self.call("get_filters", {"search": "absent"}).result
        self.assertEqual(result, {"results": [], "match_count": 0, "truncated": False})
        # Default limit is 50; small choice lists make this fit the result cap.
        self.mock.return_value = (json.dumps({"results": [{"filter_id": f"age_{i}"} for i in range(60)]}).encode(), {})
        result = self.call("get_filters", {"search": "AGE"}).result
        self.assertEqual(len(result["results"]), 50)
        self.assertTrue(result["truncated"])

    def test_filter_limits_reject_oversized_bytes_and_results_without_clipping(self):
        self.mock.side_effect = None
        self.mock.return_value = (b"x" * (responses.MAX_FILTER_BYTES + 1), {})
        self.assertIsInstance(self.call("get_filters", {"search": "age"}), ActionFailed)
        self.mock.return_value = (json.dumps({"results": [{"filter_id": "age", "question": "x" * responses.MAX_FILTER_RESULT_BYTES}]}).encode(), {})
        result = self.call("get_filters", {"search": "age"})
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("narrow search", result.error)

    def test_account_invalid_ids_cannot_redirect_project_requests(self):
        self.mock.side_effect = None
        self.mock.return_value = (json.dumps({"results": [{"id": "../../anything"}]}).encode(), {})
        self.assertIsInstance(self.call("get_account"), ActionFailed)
        self.assertEqual(self.mock.call_count, 1)

    def test_projected_cost_changes_block_start_after_readable_preview(self):
        # Declared total stays fixed while projected taxes change.
        readable = self.call("get_study", {"study_id": ID}).result
        self.assertEqual(readable["cost"]["estimated_usd"], "15.15")
        pending = self.call("start_study", {"study_id": ID})
        self.cost["rewards"]["tax"]["amount"] = 200
        self.calls.clear()
        self.assertIsInstance(self.approve(pending), ActionFailed)
        self.assertFalse(any(row[0] == "POST" for row in self.calls))
        self.assertNotEqual(self.call("get_study", {"study_id": ID}).result["cost"], readable["cost"])

    def test_cost_estimate_uses_highest_projection_declared_total_or_floor(self):
        for declared, reward, expected in ((2000, 100, "20.00"), (None, 100, "15.15"), (1000, 200, "28.58")):
            with self.subTest(declared=declared, reward=reward):
                self.study["total_cost"] = declared
                self.study["reward"] = reward
                result = self.call("get_study", {"study_id": ID})
                self.assertEqual(result.result["cost"]["estimated_usd"], expected)

    def test_nullable_or_absent_filters_allow_saved_filter_set_study(self):
        self.study["filter_set_id"] = OTHER
        for omitted in (False, True):
            if omitted:
                self.study.pop("filters", None)
            else:
                self.study["filters"] = None
            result = self.call("get_study", {"study_id": ID}).result
            self.assertEqual(result["study"]["filter_set_id"], OTHER)
            pending = self.call("start_study", {"study_id": ID})
            preview = self.api.approvals.records[pending.approval_id].payload["study"]
            self.assertIsNone(preview["filters"])
            self.assertEqual(preview["filter_set_id"], OTHER)
            self.assertIsInstance(self.approve(pending), ApprovalExecuted)
        self.study["filters"] = "invalid"
        self.assertIsInstance(self.call("get_study", {"study_id": ID}), ActionFailed)

    def test_contextual_filter_search_sends_only_validated_workspace(self):
        result = self.call("get_filters", {"search": "age", "limit": 1, "workspace_id": ID.upper()})
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(self.calls[-1], ("GET", "/filters/", {"workspace_id": [ID]}, None))
        self.calls.clear()
        self.assertIsInstance(self.call("get_filters", {"search": "age", "workspace_id": "../../bad"}), ActionFailed)
        self.assertEqual(self.calls, [])

    def test_conflicting_study_currency_fields_fail_read_and_approval(self):
        self.study.update(currency="USD", currency_code="GBP")
        for action in ("get_study", "start_study"):
            self.assertIsInstance(self.call(action, {"study_id": ID}), ActionFailed)
        self.assertEqual(self.api.approvals.records, {})
        self.study["currency_code"] = "USD"
        self.assertIsInstance(self.call("get_study", {"study_id": ID}), ActionExecuted)

    def test_object_ids_and_uuid_echoes_compare_canonical_case(self):
        for supplied, echoed in ((ID.upper(), ID), (ID, ID.upper())):
            self.study["id"] = echoed
            self.assertIsInstance(self.call("get_study", {"study_id": supplied}), ActionExecuted)
            self.assertIsInstance(self.approve(self.call("start_study", {"study_id": supplied})), ApprovalExecuted)
            self.assertEqual(self.calls[-1][1], f"/studies/{ID}/transition/")
        uuid = "9f4a11e1-1234-4abc-8def-0123456789ab"
        self.assertEqual(validation.identifier(uuid.upper()), uuid)
        original_request = p.request
        def provider(method, path, secret, **kwargs):
            if path == f"/studies/{uuid}/":
                return {**STUDY, "id": uuid.upper()}, {}
            if path == f"/studies/{uuid}/cost/":
                return self.cost, {}
            if path == f"/studies/{uuid}/transition/":
                return {}, {}
            return original_request(method, path, secret, **kwargs)
        with patch.object(p, "request", side_effect=provider):
            self.assertIsInstance(self.call("get_study", {"study_id": uuid.upper()}), ActionExecuted)
            self.assertIsInstance(self.approve(self.call("start_study", {"study_id": uuid.upper()})), ApprovalExecuted)

    def test_filter_set_creation_preserves_absent_and_null_inline_filters(self):
        for explicit_null in (False, True):
            draft = {**DRAFT, "filter_set_id": OTHER}
            if explicit_null:
                draft["filters"] = None
            self.assertIsInstance(self.call("create_study", {"study": json.dumps(draft)}), ActionExecuted)
            sent = self.calls[-1][3]
            self.assertEqual(sent["filter_set_id"], OTHER)
            self.assertEqual("filters" in sent, explicit_null)
            if explicit_null:
                self.assertIsNone(sent["filters"])
        for filters in ([], [{"filter_id": "age"}]):
            self.calls.clear()
            draft = {**DRAFT, "filter_set_id": OTHER, "filters": filters}
            self.assertIsInstance(self.call("create_study", {"study": json.dumps(draft)}), ActionFailed)
            self.assertEqual(self.calls, [])

    def test_prefixed_credential_pool_ids_are_preserved_but_values_stay_secret(self):
        pool_id = "cred_pool_12345"
        self.assertIsInstance(self.call("create_study", {"study": json.dumps({**DRAFT, "credential_pool_id": pool_id})}), ActionExecuted)
        self.assertEqual(self.calls[-1][3]["credential_pool_id"], pool_id)
        self.study.update(credential_pool_id=pool_id, credentials={"password": "private"})
        result = self.call("get_study", {"study_id": ID}).result
        self.assertEqual(result["study"]["credential_pool_id"], pool_id)
        self.assertNotIn("private", json.dumps(result))
        preview = self.api.approvals.records[self.call("start_study", {"study_id": ID}).approval_id].payload["study"]
        self.assertEqual(preview["study_definition"]["credential_pool_id"], pool_id)
        for bad in ("cred_pool_", "cred_pool_../invalid", "cred_pool_" + "x" * 65, 123, {"id": pool_id}):
            self.calls.clear()
            self.assertIsInstance(self.call("create_study", {"study": json.dumps({**DRAFT, "credential_pool_id": bad})}), ActionFailed)
            self.assertEqual(self.calls, [])

    def test_large_integer_extensions_fail_cleanly_before_network(self):
        huge = 10 ** 999
        for action, params in (
            ("create_study", {"study": json.dumps({**DRAFT, "extension": huge})}),
            ("save_collection", {"collection": json.dumps({**COLLECTION, "extension": huge})}),
            ("get_eligibility_count", {"filters": json.dumps([{"filter_id": "age", "selected_range": {"lower": huge}}])}),
        ):
            with self.subTest(action=action):
                self.assertIsInstance(self.call(action, params), ActionFailed)
        self.mock.assert_not_called()

    def test_schema_and_operator_policy_inventory(self):
        self.assertEqual({spec.id for spec in p.MANIFEST.actions}, {
            "get_account", "get_filters", "get_eligibility_count", "list_studies", "get_study", "list_submissions",
            "export_demographics", "list_collections", "get_collection", "export_collection", "get_collection_export_status", "create_study",
            "halt_study", "save_collection", "start_study", "approve_submissions", "save_collection_export"})
        self.assertEqual(p.APPROVED, {"start_study", "approve_submissions", "halt_study", "save_collection"})
        self.assertEqual({s.id for s in p.MANIFEST.actions if s.approval == "operator"}, set(p.APPROVED))
        for spec in p.MANIFEST.actions:
            self.assertTrue(spec.cost_description)
            self.assertEqual(bool(spec.output_schema), spec.approval != "operator" and not spec.returns_asset)
