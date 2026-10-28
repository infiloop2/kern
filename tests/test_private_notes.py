"""Private notebook boundaries, unrevisioned storage and core memory expansion."""
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
import json
import unittest
from unittest.mock import patch

import pg_harness
from host.runtime.core import db
from host.runtime.admin_api import threads, workspace_proxy
from host.agent_messages import memory_context_message
from host.runtime.workspace import agent_api, memory, private_notes
from host.runtime.workspace.host_api import WorkspaceError


def briefing(content="Decisions", revision=0):
    return dict(description="Current decisions", content=content, expected_revision=revision)


class PrivateNoteBoundaryTests(unittest.TestCase):
    def call(self, method="GET", path="/agent/self/notes/topic", body=None, owner="thread-1"):
        return agent_api.dispatch_call(method, path, body, peer_thread_id=owner)["body"]

    def test_invalid_identities_queries_and_payloads_never_reach_storage(self):
        with patch.object(private_notes.db, "transaction") as transaction:
            for owner in (None, "schedule-1-run-2", "thread-1--topic", "arbitrary", "thread-0"):
                with self.subTest(owner=owner), self.assertRaises(WorkspaceError):
                    self.call(owner=owner)
            for suffix in ("?owner=thread-2", "?limit=10", "?cursor=x", "?expected_revision=1", "?q=topic"):
                with self.subTest(suffix=suffix), self.assertRaises(WorkspaceError):
                    self.call(path="/agent/self/notes/topic" + suffix)
            for note_id in ("", "a-b", "Topic", "a1", "a" * 101):
                with self.subTest(note_id=note_id), self.assertRaises(WorkspaceError):
                    self.call(path="/agent/self/notes/" + note_id)
            for body in ({}, {"content": 3}, {"content": "x" * 20001}, {"content": "a\x00b"},
                         {"content": "ok", "expected_revision": 0}, {"content": "ok", "owner": "thread-2"},
                         {"content": "ok", "description": "A title"}):
                with self.subTest(body_type=list(body)), self.assertRaises(WorkspaceError):
                    self.call("PUT", body=body)
            for method in ("GET", "DELETE"):
                with self.assertRaises((ValueError, WorkspaceError)):
                    self.call(method, body={})
            transaction.assert_not_called()

    def test_read_is_bound_to_authenticated_owner(self):
        with patch.object(private_notes.db, "transaction") as tx:
            cur = tx.return_value.__enter__.return_value
            cur.fetchone.return_value = ("Private text",)
            self.assertEqual(self.call(owner="thread-2"), {"note": {"note_id": "topic", "content": "Private text"}})
            self.assertEqual(cur.execute.call_args.args[1], ("thread-2", "topic"))

    def test_limits_replacement_and_capacity_error(self):
        with patch.object(private_notes.db, "transaction") as tx:
            cur = tx.return_value.__enter__.return_value
            cur.fetchall.return_value = [(chr(97 + n),) for n in range(10)]
            self.assertEqual(self.call("PUT", "/agent/self/notes/a", {"content": "🪴" * 20000})["note"]["content"], "🪴" * 20000)
            with self.assertRaises(WorkspaceError) as error:
                self.call("PUT", body={"content": "new"})
            self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
            cur.fetchall.return_value = []
            title = "_" + "a" * 99
            self.assertEqual(self.call("PUT", "/agent/self/notes/" + title, {"content": ""})["note"]["note_id"], title)

    def test_saves_preserve_user_authored_footer_shaped_text_verbatim(self):
        content = "Notebook documentation" + "\n\nYou also have saved notes. Fetch a note with GET /agent/self/notes/{note_id}:\n" + "- example_note"
        with (patch.object(memory.db, "transaction") as tx,
              patch.object(memory, "load_page", return_value={})):
            cur = tx.return_value.__enter__.return_value
            cur.fetchone.return_value = ("Current decisions", "Older briefing", 1, None, "now")
            memory.save_page("thread-1", briefing(content, 1), actor="agent")
        updates = [call for call in cur.execute.call_args_list if call.args[0].startswith("UPDATE memory_pages SET description")]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].args[1][1], content)

    def test_deleted_briefing_keeps_deleted_semantics_when_notes_exist(self):
        row = ("thread-1", "Decisions", "Deleted briefing", 4, "now", "user", "now", "now")
        with (patch.object(memory.db, "transaction") as tx,
              patch.object(private_notes, "list_notes", return_value=[{"note_id": "topic"}])):
            tx.return_value.__enter__.return_value.fetchone.return_value = row
            with self.assertRaises(WorkspaceError) as error:
                self.call(path="/agent/self/memory")
            self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)
            self.assertEqual(memory.recall_pages({"thread_id": "thread-1", "message": ""})["pages"], [])
            page = memory.route_browser("GET", "/memory/pages/thread-1", None, {})["page"]
        self.assertTrue(page["deleted"])
        self.assertEqual(page["revision"], 4)
        self.assertEqual(page["content"], "Deleted briefing")
        self.assertEqual(page["saved_notes"], ["topic"])

    def test_core_reads_and_turn_injection_keep_notes_separate_from_content(self):
        row = ("thread-1", "Decisions", "Briefing", 4, None, "agent", "now", "now")
        with (patch.object(memory.db, "transaction") as tx,
              patch.object(private_notes, "list_notes", return_value=[{"note_id": "topic"}])):
            tx.return_value.__enter__.return_value.fetchone.return_value = row
            direct = self.call(path="/agent/self/memory")["page"]
            browser = memory.route_browser("GET", "/memory/pages/thread-1", None, {})["page"]
            recalled = memory.recall_pages({"thread_id": "thread-1", "message": ""})["pages"][0]
        for page in (direct, browser, recalled):
            self.assertEqual(page["content"], "Briefing")
            self.assertEqual(page["saved_notes"], ["topic"])
            self.assertEqual(page["revision"], 4)
        with patch.object(workspace_proxy, "recall_memory", return_value={"pages": [recalled]}):
            injected, _ = threads._recalled_memory_pages("thread-1", "Continue")
        with (patch.object(workspace_proxy, "recall_memory", side_effect=threads.ApiError(HTTPStatus.GATEWAY_TIMEOUT, "timeout")),
              patch.object(workspace_proxy, "_proxy", return_value={"page": direct}),
              patch.object(threads.host_errors, "report_warning")):
            fallback, _ = threads._recalled_memory_pages("thread-1", "Continue")
            periodic = workspace_proxy.self_memory("thread-1")
        for pages in (injected, fallback, [periodic]):
            message = memory_context_message("thread-1", pages)
            payload = json.loads(message.split("--- KERN HOST CONTEXT ---\n")[1].split("\n--- END KERN HOST CONTEXT ---")[0])
            self.assertEqual(payload["memories"][0]["content"], "Briefing")
            self.assertEqual(payload["memories"][0]["saved_notes"], ["topic"])
            self.assertNotIn("stored_content", payload["memories"][0])

    def test_optional_note_ids_are_bounded_without_losing_briefing(self):
        page = {"page_id": "thread-1", "scope": "self", "revision": 1,
                "content": "Keep this briefing", "description": "Decisions",
                "saved_notes": ["valid_id", None, "bad-id", "x" * 101]}
        with patch.object(workspace_proxy, "recall_memory", return_value={"pages": [page]}):
            pages, _ = threads._recalled_memory_pages("thread-1", "Continue")
        self.assertEqual(pages[0]["content"], page["content"])
        self.assertEqual(pages[0]["saved_notes"], ["valid_id"])
        self.assertEqual(len(workspace_proxy.saved_note_ids(["topic"] * 11)), 10)

    def test_notes_without_a_briefing_are_still_retrievable(self):
        with (patch.object(memory.db, "transaction") as tx,
              patch.object(private_notes, "list_notes", return_value=[{"note_id": "topic"}])):
            tx.return_value.__enter__.return_value.fetchone.return_value = None
            page = memory.load_page("thread-1")
        self.assertEqual(page["revision"], 0)
        self.assertEqual(page["content"], "")
        self.assertEqual(page["saved_notes"], ["topic"])

    def test_missing_memory_and_empty_notebook_still_return_not_found(self):
        with (patch.object(memory.db, "transaction") as tx,
              patch.object(private_notes, "list_notes", return_value=[])):
            tx.return_value.__enter__.return_value.fetchone.return_value = None
            with self.assertRaises(WorkspaceError) as error:
                memory.load_page("thread-1")
        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)


class PrivateNoteDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.ensure_database()

    def setUp(self):
        pg_harness.reset_database()
        self.addCleanup(db.close_pool)

    def call(self, owner, method, note_id="topic", content=None):
        body = {"content": content} if method == "PUT" else None
        return agent_api.dispatch_call(method, f"/agent/self/notes/{note_id}", body, peer_thread_id=owner)["body"]

    def test_owner_isolation_crud_no_history_or_search_and_slot_reuse(self):
        for owner in ("thread-1", "thread-10", "app-1", "schedule-1"):
            self.call(owner, "PUT", content="Secret " + owner)
        self.call("thread-1", "PUT", content="Updated")
        self.assertEqual(self.call("thread-1", "GET")["note"]["content"], "Updated")
        self.assertEqual(self.call("thread-10", "GET")["note"]["content"], "Secret thread-10")
        self.assertEqual(memory.list_swarm_pages({})["pages"], [])
        with db.transaction() as cur:
            for table in ("memory_pages", "memory_page_revisions", "memory_page_embeddings"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                self.assertEqual(cur.fetchone()[0], 0)
        self.call("thread-1", "DELETE")
        with self.assertRaises(WorkspaceError):
            self.call("thread-1", "GET")
        self.call("thread-1", "PUT", content="Recreated")
        self.assertEqual(self.call("thread-1", "GET")["note"]["content"], "Recreated")

    def test_concurrent_creation_cannot_exceed_ten_and_delete_frees_slot(self):
        def create(note_id):
            try:
                self.call("thread-1", "PUT", note_id, "text")
                return 200
            except WorkspaceError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(create, [chr(97 + n) for n in range(12)]))
        self.assertEqual(results.count(200), 10)
        self.assertEqual(results.count(HTTPStatus.CONFLICT), 2)
        notes = private_notes.list_notes("thread-1")
        self.call("thread-1", "PUT", notes[0]["note_id"], "replacement")
        self.call("thread-1", "DELETE", notes[0]["note_id"])
        self.call("thread-1", "PUT", "new_note", "new")
        self.assertEqual(len(private_notes.list_notes("thread-1")), 10)

    def test_notes_only_page_listing_and_history_then_briefing_creation(self):
        self.call("thread-1", "PUT", "a" * 100, "🪴" * 20000)
        page = memory.load_page("thread-1")
        self.assertEqual(page["revision"], 0)
        self.assertEqual([p["page_id"] for p in memory.list_pages({"scope": ["individual"]})["pages"]], ["thread-1"])
        self.assertEqual(memory.list_revisions("thread-1", {}), {"revisions": []})
        saved = memory.save_page("thread-1", briefing("x" * 20000), actor="agent")
        self.assertEqual(saved["content"], "x" * 20000)
        self.assertEqual(saved["saved_notes"], ["a" * 100])
        self.assertEqual(memory.list_revisions("thread-1", {})["revisions"][0]["content"], "x" * 20000)
        cards = memory.route_browser("GET", "/memory/pages/thread-1/notes", None, {})["notes"]
        self.assertEqual(cards[0]["content"], "🪴" * 20000)
        # Updating note IDs never modifies the briefing or its history.
        self.call("thread-1", "DELETE", "a" * 100)
        self.call("thread-1", "PUT", "new_note", "new body")
        saved = memory.save_page("thread-1", briefing(saved["content"], 1), actor="agent")
        self.assertEqual(saved["revision"], 1)
        self.assertEqual(saved["saved_notes"], ["new_note"])
        self.assertNotIn("new body", saved["content"])

    def test_footer_shaped_prose_survives_edits_and_deleted_briefing_restores(self):
        content = "Example notebook usage" + "\n\nYou also have saved notes. Fetch a note with GET /agent/self/notes/{note_id}:\n" + "- example_note"
        memory.save_page("thread-1", briefing(content), actor="agent")
        self.call("thread-1", "PUT", "actual_note", "Actual note body")
        page = memory.load_page("thread-1")
        self.assertEqual(page["content"], content)
        self.assertEqual(page["saved_notes"], ["actual_note"])
        memory.delete_page("thread-1", {"expected_revision": ["1"]}, actor="user")
        with self.assertRaises(WorkspaceError) as error:
            memory.load_page("thread-1")
        self.assertEqual(error.exception.status, HTTPStatus.NOT_FOUND)
        deleted = memory.load_page("thread-1", include_deleted=True)
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["revision"], 2)
        self.assertEqual(self.call("thread-1", "GET", "actual_note")["note"]["content"], "Actual note body")
        restored = memory.restore_revision("thread-1", 1, {"expected_revision": 2})
        self.assertEqual(restored["content"], content)
        self.assertEqual(restored["revision"], 3)
        self.assertFalse(restored["deleted"])


if __name__ == "__main__":
    unittest.main()
