from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
import unittest
from unittest.mock import MagicMock, patch

import pg_harness
from host.runtime.core import db
from host.runtime.workspace import navigation_order as ordering, schedules
from host.runtime.workspace.chat import backend as chat
from host.runtime.workspace.host_api import WorkspaceError
from host.runtime.workspace.web_apps import backend as apps


class NavigationOrderTests(unittest.TestCase):
    def test_unsaved_items_append_in_numeric_creation_order(self) -> None:
        self.assertEqual(ordering.ordered_ids(
            ["app-10", "app-2", "app-4", "app-1"], ["app-4", "app-99", "app-2"]
        ), ["app-4", "app-2", "app-1", "app-10"])

    def test_activity_and_names_do_not_change_positions(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (["app-2", "app-1"],)
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        items = [
            {"app_id": "app-1", "last_used_at": "2099", "name": "A"},
            {"app_id": "app-2", "last_used_at": "2000", "name": "Z"},
        ]
        with patch.object(ordering.db, "transaction", return_value=transaction):
            ordering.sort_items("apps", items, "app_id")
        self.assertEqual([item["app_id"] for item in items], ["app-2", "app-1"])

    def test_invalid_move_never_accesses_database(self) -> None:
        with patch.object(ordering.db, "transaction") as transaction:
            for body in (
                None, {}, {"item_id": "app-1"},
                {"item_id": None, "before_id": None},
                {"item_id": "schedule-1", "before_id": None},
                {"item_id": "app-1", "before_id": "app-1"},
                {"item_id": "app-1", "before_id": False},
                {"item_id": "app-1", "before_id": None, "extra": 1},
            ):
                with self.subTest(body=body), self.assertRaises(WorkspaceError) as error:
                    ordering.move("apps", body)
                self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)
            transaction.assert_not_called()

    def test_browser_routes_bind_the_list_kind(self) -> None:
        body = {"item_id": "app-2", "before_id": "app-1"}
        with patch.object(ordering, "move", return_value={"status": "ok"}) as move:
            apps.route_browser("POST", "/apps/order", body)
            move.assert_called_once_with("apps", body)
        body = {"item_id": "schedule-2", "before_id": None}
        with patch.object(ordering, "move", return_value={"status": "ok"}) as move:
            chat.route_browser("POST", "/scheduled-agents/order", body)
            move.assert_called_once_with("schedules", body)


class NavigationOrderDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        pg_harness.ensure_database()

    def setUp(self) -> None:
        pg_harness.reset_database()
        self.addCleanup(db.close_pool)

    def app_ids(self, archived: bool = False) -> list[str]:
        with patch.object(apps, "_host_thread_summaries", return_value=[]):
            return [item["app_id"] for item in apps.list_web_apps(
                {"archived": ["true" if archived else "false"]}
            )["apps"]]

    def test_app_order_survives_activity_new_items_archive_and_reconnect(self) -> None:
        ids = [apps.create_web_app()["app_id"] for _ in range(3)]
        self.assertEqual(self.app_ids(), ids)
        ordering.move("apps", {"item_id": ids[2], "before_id": ids[0]})
        with db.transaction() as cur:
            cur.execute("UPDATE web_apps SET updated_at = '2099-01-01T00:00:00Z' WHERE app_id = %s", (ids[1],))
            cur.execute("UPDATE web_apps SET archived = TRUE WHERE app_id = %s", (ids[0],))
        new_id = apps.create_web_app()["app_id"]
        db.close_pool()
        self.assertEqual(self.app_ids(), [ids[2], ids[1], new_id])
        self.assertEqual(self.app_ids(archived=True), [ids[0]])
        with self.assertRaises(WorkspaceError) as error:
            ordering.move("apps", {"item_id": ids[0], "before_id": None})
        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        with db.transaction() as cur:
            cur.execute("UPDATE web_apps SET archived = FALSE WHERE app_id = %s", (ids[0],))
        self.assertEqual(self.app_ids(), [ids[2], ids[0], ids[1], new_id])

    def test_concurrent_relative_moves_preserve_both_edits(self) -> None:
        ids = [apps.create_web_app()["app_id"] for _ in range(4)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(ordering.move, "apps", body) for body in (
                {"item_id": ids[1], "before_id": ids[0]},
                {"item_id": ids[3], "before_id": ids[2]},
            )]
            for future in futures:
                future.result()
        self.assertEqual(self.app_ids(), [ids[1], ids[0], ids[3], ids[2]])

    def test_schedules_have_independent_order_and_deleted_target_is_rejected(self) -> None:
        ids = [schedules.create_schedule({
            "name": f"Schedule {index}", "message": "Review", "cadence": "interval",
            "interval_minutes": 60, "agent_runtime": "codex",
            "model": "gpt-5.6-terra", "effort": "high",
        }, actor="user")["thread_id"] for index in range(3)]
        ordering.move("schedules", {"item_id": ids[0], "before_id": None})
        with patch.object(chat, "_host_thread_summaries", return_value=[]):
            self.assertEqual([item["thread_id"] for item in chat.list_scheduled_agent_threads()["threads"]], [ids[1], ids[2], ids[0]])
        with db.transaction() as cur:
            cur.execute("UPDATE schedules SET deleted_at = '2026-09-15T00:00:00Z' WHERE thread_id = %s", (ids[2],))
        with self.assertRaises(WorkspaceError) as error:
            ordering.move("schedules", {"item_id": ids[1], "before_id": ids[2]})
        self.assertEqual(error.exception.status, HTTPStatus.CONFLICT)
        with patch.object(chat, "_host_thread_summaries", return_value=[]):
            self.assertEqual([item["thread_id"] for item in chat.list_scheduled_agent_threads()["threads"]], [ids[1], ids[0]])
