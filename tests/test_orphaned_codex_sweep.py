"""Unreferenced root session cleanup without PostgreSQL/inference."""

import copy
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from host.runtime.admin_api import codex_cleanup as cleanup
from host.runtime.agent_runtime import codex_app_server, orchestrator
from host.runtime.agent_runtime.orchestrator import live_provider_session_ids


def session(n, *, runtime="codex", **overrides):
    session_id = f"01900000-0000-7000-8000-{n:012d}"
    return {
        "id": session_id, "originator": "kern-host", "cwd": codex_app_server.AGENT_CWD,
        "updatedAt": 1, "status": {"type": "notLoaded"}, "ephemeral": False,
        "path": f"{codex_app_server.AGENT_CWD}/.{runtime}/sessions/rollout-{session_id}.jsonl",
        "parentThreadId": None, **overrides,
    }


class OrphanedCodexSweepTests(unittest.TestCase):
    def setUp(self):
        self.server = MagicMock(runtime_type="codex")
        self.enterContext(patch.object(codex_app_server, "CODEX_RUNTIME_TYPES", ("codex",)))
        self.enterContext(patch.object(codex_app_server, "CodexAppServer", return_value=self.server))
        self.sessions = self.enterContext(patch.object(codex_app_server, "stored_sessions", return_value=[]))
        self.delete = self.enterContext(patch.object(codex_app_server, "delete_session"))
        self.candidates = self.enterContext(patch.object(cleanup.state, "codex_cleanup_candidates", return_value=[]))
        self.references = self.enterContext(patch.object(cleanup.state, "referenced_provider_session_ids", return_value=set()))
        self.live = self.enterContext(patch.object(orchestrator, "live_provider_session_ids", return_value=set()))
        self.report = self.enterContext(patch.object(cleanup.host_errors, "report_unexpected"))
        self.info = self.enterContext(patch.object(cleanup.host_errors, "report_info"))
        self.warning = self.enterContext(patch.object(cleanup.host_errors, "report_warning"))
        self.enterContext(patch.object(cleanup.time, "time", return_value=2_000_000))

    def use(self, *threads):
        self.sessions.return_value = list(threads)
        by_id = {t["id"]: t for t in threads}
        self.server.call.side_effect = lambda method, params, **kwargs: {"thread": by_id[params["threadId"]]}

    def test_unmapped_old_kern_sessions_are_retired_even_without_archived_candidates(self):
        old = session(1)
        self.use(old)
        self.assertEqual(cleanup.reconcile_codex_sessions(), 1)
        self.delete.assert_called_once_with(self.server, old["id"])
        self.server.call.assert_called_once_with("thread/read", {"threadId": old["id"], "includeTurns": False}, timeout=5)
        self.server.close.assert_called_once()
        self.report.assert_not_called()

    def test_empty_pass_reports_start_and_completion(self):
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.server.start.assert_called_once_with(init_timeout=45)
        self.assertEqual(self.info.call_count, 2)
        started, finished = self.info.call_args_list
        self.assertIn("started", started.args[1])
        self.assertEqual(started.kwargs["context"]["limit_bytes"], 10 * 1024 * 1024)
        self.assertIn("completed", finished.args[1])
        context = finished.kwargs["context"]
        self.assertEqual(context["outcome"], "completed")
        self.assertEqual(context["mapped_checked"], 0)
        self.assertEqual(context["stored_roots"], 0)
        self.assertEqual(context["retired_roots"], 0)
        self.assertGreaterEqual(context["elapsed_seconds"], 0)
        self.warning.assert_not_called()

    def test_summary_counts_protected_roots_and_reasons(self):
        roots = [session(1), session(2), session(3, updatedAt=2_000_000),
                 session(4, originator="other"), session(5, path=None),
                 session(6, ephemeral=True), session(7, status={"type": "active"})]
        self.use(*roots)
        self.references.return_value = {roots[0]["id"]}
        self.live.return_value = {roots[1]["id"]}
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        context = self.info.call_args.kwargs["context"]
        self.assertEqual(context["stored_roots"], 7)
        for reason in ("referenced", "live_session", "inactivity_grace", "foreign_origin",
                       "missing_path", "ephemeral_or_unknown", "provider_active_or_unknown"):
            self.assertEqual(context[f"skip_{reason}"], 1)

    def test_nonfinite_or_invalid_updated_times_cannot_bypass_grace(self):
        self.use(*(session(index, updatedAt=updated) for index, updated in enumerate(
            (float("nan"), float("inf"), -1, None, True), start=1,
        )))
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.delete.assert_not_called()
        self.assertEqual(self.info.call_args.kwargs["context"]["skip_invalid_updated_at"], 5)

    def test_one_unmeasurable_rollout_does_not_starve_later_mappings(self):
        bad, good = session(1), session(2)
        self.candidates.return_value = [("app-1", bad["id"], False), ("app-2", good["id"], False)]
        with (
            patch.object(orchestrator, "live_thread_ids", return_value=set()),
            patch.object(codex_app_server, "session_rollout_size", side_effect=[
                codex_app_server.CodexRolloutMeasurementError("Could not measure Codex session rollout"),
                20 * 1024 * 1024,
            ]),
            patch.object(cleanup.state, "mutation"),
            patch.object(cleanup.state, "detach_idle_codex_session", return_value=True) as detach,
        ):
            self.assertEqual(cleanup.reconcile_codex_sessions(), 1)
        detach.assert_called_once()
        self.assertEqual(detach.call_args.args[3], good["id"])
        self.delete.assert_called_once_with(self.server, good["id"])
        self.warning.assert_called_once()
        self.assertEqual(self.warning.call_args.kwargs["context"]["session_id"], bad["id"])
        context = self.info.call_args.kwargs["context"]
        self.assertEqual(context["outcome"], "partial")
        self.assertEqual(context["measurement_failed"], 1)
        self.assertEqual(context["retired_oversized"], 1)
        self.assertEqual(context["measured_retired_bytes"], 20 * 1024 * 1024)
        self.report.assert_not_called()

    def test_transport_timeout_stops_account_and_reports_exact_phase(self):
        self.enterContext(patch.object(codex_app_server, "CODEX_RUNTIME_TYPES", ("codex", "codex-2")))
        root = session(1)
        self.candidates.side_effect = [[("app-1", root["id"], False), ("app-2", session(2)["id"], False)], []]
        with (
            patch.object(orchestrator, "live_thread_ids", return_value=set()),
            patch.object(codex_app_server, "session_rollout_size", side_effect=codex_app_server.CodexTimeout("timed out")),
        ):
            self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.delete.assert_not_called()
        self.assertEqual(self.sessions.call_count, 1)
        context = self.report.call_args.kwargs["context"]
        self.assertEqual(context["runtime"], "codex")
        self.assertEqual(context["phase"], "measure")
        self.assertEqual(context["session_id"], root["id"])
        self.assertEqual(context["mapped_checked"], 1)
        summaries = [call.kwargs["context"] for call in self.info.call_args_list if "outcome" in call.kwargs["context"]]
        self.assertEqual([row["outcome"] for row in summaries], ["failed", "completed"])

    def test_failed_startup_still_reports_summary_and_preserves_mappings(self):
        self.server.start.side_effect = codex_app_server.CodexTimeout("initialize timeout")
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.candidates.assert_not_called()
        self.delete.assert_not_called()
        self.server.close.assert_called_once()
        self.assertEqual(self.report.call_args.kwargs["context"]["phase"], "initialize")
        self.assertEqual(self.info.call_args.kwargs["context"]["outcome"], "failed")

    def test_detached_delete_failure_is_visible_without_counting_retirement(self):
        root = session(1)
        self.candidates.return_value = [("app-1", root["id"], False)]
        self.delete.side_effect = codex_app_server.CodexTimeout("delete timeout")
        with (
            patch.object(orchestrator, "live_thread_ids", return_value=set()),
            patch.object(codex_app_server, "session_rollout_size", return_value=20 * 1024 * 1024),
            patch.object(cleanup.state, "mutation"),
            patch.object(cleanup.state, "detach_idle_codex_session", return_value=True),
        ):
            self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        context = self.info.call_args.kwargs["context"]
        self.assertEqual(context["detached"], 1)
        self.assertEqual(context["retired_roots"], 0)
        self.assertEqual(context["measured_retired_bytes"], 0)
        self.assertEqual(self.report.call_args.kwargs["context"]["phase"], "delete_mapped")

    def test_stale_listing_does_not_retire_a_mapped_root_twice(self):
        root = session(1)
        self.use(root)
        self.candidates.return_value = [("app-1", root["id"], False)]
        with (
            patch.object(orchestrator, "live_thread_ids", return_value=set()),
            patch.object(codex_app_server, "session_rollout_size", return_value=20 * 1024 * 1024),
            patch.object(cleanup.state, "mutation"),
            patch.object(cleanup.state, "detach_idle_codex_session", return_value=True),
        ):
            self.assertEqual(cleanup.reconcile_codex_sessions(), 1)
        self.delete.assert_called_once_with(self.server, root["id"])
        self.assertEqual(self.info.call_args.kwargs["context"]["skip_already_retired"], 1)

    def test_mapped_live_recent_foreign_and_unsafe_sessions_are_preserved(self):
        threads = [session(1), session(2), session(3, updatedAt=2_000_000),
                   session(4, originator="codex_cli_rs"), session(5, status={"type": "active"}),
                   session(6, ephemeral=True), session(7, path="/etc/rollout.jsonl"),
                   session(8, cwd="/tmp"), session(9, originator=None),
                   session(10, updatedAt=True), session(11, runtime="codex-2")]
        self.use(*threads)
        self.references.return_value = {threads[0]["id"]}
        self.live.return_value = {threads[1]["id"]}
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.delete.assert_not_called()

    def test_native_children_are_not_independent_kern_sessions(self):
        root = session(1)
        child = session(2, parentThreadId=root["id"])
        self.use(root, child)
        self.references.return_value = {root["id"]}
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.delete.assert_not_called()
        self.references.return_value = set()
        self.assertEqual(cleanup.reconcile_codex_sessions(), 1)
        self.delete.assert_called_once_with(self.server, root["id"])

    def test_current_references_are_rechecked_after_the_listing(self):
        old = session(1)
        self.use(old)
        self.references.side_effect = [set(), {old["id"]}]
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.delete.assert_not_called()

    def test_new_activity_is_rechecked_before_deleting(self):
        old = session(1)
        self.use(old)
        fresh = copy.deepcopy(old)
        fresh["updatedAt"] = 2_000_000
        self.server.call.side_effect = None
        self.server.call.return_value = {"thread": fresh}
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.delete.assert_not_called()

    def test_delete_failure_keeps_leftovers_discoverable_on_next_pass(self):
        old = session(1)
        self.use(old)
        self.delete.side_effect = RuntimeError("timeout")
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.report.assert_called_once()
        self.delete.side_effect = None
        self.assertEqual(cleanup.reconcile_codex_sessions(), 1)

    def test_one_pass_handles_oversized_idle_and_aged_archived_mappings(self):
        large, archived = session(1), session(2)
        self.candidates.return_value = [
            ("app-1", large["id"], False), ("thread-2", archived["id"], True),
        ]
        with (
            patch.object(orchestrator, "live_thread_ids", return_value=set()),
            patch.object(codex_app_server, "session_rollout_size", return_value=codex_app_server.SESSION_ROLLOUT_MAX_BYTES) as size,
            patch.object(cleanup.state, "mutation"),
            patch.object(cleanup.state, "detach_idle_codex_session", return_value=True) as detach,
        ):
            self.delete.side_effect = lambda _, sid: self.assertEqual(detach.call_args.args[3], sid)
            self.assertEqual(cleanup.reconcile_codex_sessions(), 2)
        size.assert_called_once_with(self.server, large["id"])
        self.assertEqual([c.kwargs["archived"] for c in detach.call_args_list], [False, True])
        self.assertEqual([c.args[1] for c in self.delete.call_args_list], [large["id"], archived["id"]])

    def test_small_live_and_changed_mappings_are_preserved(self):
        self.candidates.return_value = [("thread-1", session(1)["id"], False)]
        with (
            patch.object(orchestrator, "live_thread_ids", return_value={"thread-1"}) as live_threads,
            patch.object(codex_app_server, "session_rollout_size", return_value=1) as size,
            patch.object(cleanup.state, "mutation"),
            patch.object(cleanup.state, "detach_idle_codex_session", return_value=False) as detach,
        ):
            self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
            size.assert_not_called()
            live_threads.return_value = set()
            self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
            detach.assert_not_called()
            size.return_value = codex_app_server.SESSION_ROLLOUT_MAX_BYTES
            self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
            detach.assert_called_once()
        self.delete.assert_not_called()

    def test_idle_session_retirement_starts_at_ten_mib(self):
        small, boundary, large = session(1), session(2), session(3)
        self.candidates.return_value = [
            ("thread-1", small["id"], False),
            ("thread-2", boundary["id"], False),
            ("thread-3", large["id"], False),
        ]
        with (
            patch.object(orchestrator, "live_thread_ids", return_value=set()),
            patch.object(codex_app_server, "session_rollout_size", side_effect=[
                10 * 1024 * 1024 - 1, 10 * 1024 * 1024, 20 * 1024 * 1024,
            ]),
            patch.object(cleanup.state, "mutation"),
            patch.object(cleanup.state, "detach_idle_codex_session", return_value=True) as detach,
        ):
            self.assertEqual(cleanup.reconcile_codex_sessions(), 2)
        self.assertEqual([c.args[3] for c in detach.call_args_list], [boundary["id"], large["id"]])
        self.assertEqual([c.args[1] for c in self.delete.call_args_list], [boundary["id"], large["id"]])

    def test_measurement_failure_preserves_mapping(self):
        self.candidates.return_value = [("thread-1", session(1)["id"], False)]
        with (
            patch.object(orchestrator, "live_thread_ids", return_value=set()),
            patch.object(codex_app_server, "session_rollout_size", side_effect=RuntimeError("stat failed")),
            patch.object(cleanup.state, "detach_idle_codex_session") as detach,
        ):
            self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        detach.assert_not_called()
        self.delete.assert_not_called()
        self.report.assert_called_once()

    def test_a_send_during_measurement_prevents_detach(self):
        self.candidates.return_value = [("thread-1", session(1)["id"], False)]
        with (
            patch.object(orchestrator, "live_thread_ids", side_effect=[set(), {"thread-1"}]),
            patch.object(codex_app_server, "session_rollout_size", return_value=codex_app_server.SESSION_ROLLOUT_MAX_BYTES) as size,
            patch.object(cleanup.state, "mutation"),
            patch.object(cleanup.state, "detach_idle_codex_session") as detach,
        ):
            self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        size.assert_called_once()
        detach.assert_not_called()
        self.delete.assert_not_called()

    def test_partial_list_never_deletes_and_later_accounts_are_still_checked(self):
        self.enterContext(patch.object(codex_app_server, "CODEX_RUNTIME_TYPES", ("codex", "codex-2")))
        self.sessions.side_effect = [RuntimeError("incomplete listing"), []]
        self.assertEqual(cleanup.reconcile_codex_sessions(), 0)
        self.delete.assert_not_called()
        self.assertEqual(self.server.close.call_count, 2)
        self.report.assert_called_once()

    def test_listing_failure_does_not_block_exact_mapped_cleanup(self):
        archived = session(1)
        self.candidates.return_value = [("thread-1", archived["id"], True)]
        self.sessions.side_effect = RuntimeError("incomplete listing")
        with (
            patch.object(orchestrator, "live_thread_ids", return_value=set()),
            patch.object(cleanup.state, "mutation"),
            patch.object(cleanup.state, "detach_idle_codex_session", return_value=True),
        ):
            self.assertEqual(cleanup.reconcile_codex_sessions(), 1)
        self.delete.assert_called_once_with(self.server, archived["id"])
        self.report.assert_called_once()

    def test_live_provider_snapshot_includes_uncommitted_startup_and_finishing_ids(self):
        turn = SimpleNamespace(runtime_type="codex", provider_session_id="mapped",
                               server=SimpleNamespace(last_known_session_id="accepted"))
        other = SimpleNamespace(runtime_type="codex-2", provider_session_id="other", server=None)
        with patch.object(orchestrator, "_LIVE", {1: turn, 2: other}):
            self.assertEqual(live_provider_session_ids("codex"), {"mapped", "accepted"})
            self.assertEqual(live_provider_session_ids("codex-2"), {"other"})


class StoredSessionListingTests(unittest.TestCase):
    def test_pages_app_server_roots_and_both_archive_folders(self):
        server = MagicMock()
        root, child = session(1), session(2, parentThreadId=session(1)["id"])
        server.call.side_effect = [
            {"data": [root], "nextCursor": "more"},
            {"data": [], "nextCursor": None},
            {"data": [child, session(3)], "nextCursor": None},
        ]
        self.assertEqual(codex_app_server.stored_sessions(server), [root, session(3)])
        calls = server.call.call_args_list
        self.assertEqual([c.args[1]["archived"] for c in calls], [False, False, True])
        self.assertEqual(calls[1].args[1]["cursor"], "more")
        self.assertNotIn("subAgent", calls[0].args[1]["sourceKinds"])

    def test_invalid_or_repeated_cursor_aborts_snapshot(self):
        for bad in (False, "", "loop"):
            with self.subTest(cursor=bad):
                server = MagicMock()
                server.call.side_effect = [
                    {"data": [session(1)], "nextCursor": "loop"},
                    {"data": [], "nextCursor": bad},
                ]
                with self.assertRaises(codex_app_server.CodexAppServerError):
                    codex_app_server.stored_sessions(server)

    def test_page_limit_or_malformed_metadata_aborts_snapshot(self):
        server = MagicMock()
        server.call.side_effect = [{"data": [], "nextCursor": str(i)} for i in range(200)]
        with self.assertRaises(codex_app_server.CodexAppServerError):
            codex_app_server.stored_sessions(server)
        for page in ({}, {"data": {}}, {"data": [None]}, {"data": [{"id": "wrong"}]}):
            with self.subTest(page=page):
                server = MagicMock()
                server.call.return_value = page
                with self.assertRaises((ValueError, codex_app_server.CodexAppServerError)):
                    codex_app_server.stored_sessions(server)
