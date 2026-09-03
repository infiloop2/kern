from __future__ import annotations

from http import HTTPStatus
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.workspace import seen
from host.runtime.workspace.host_api import WorkspaceError


class WorkspaceSeenTests(unittest.TestCase):
    def test_request_marker_requires_exact_non_negative_integers(self) -> None:
        self.assertEqual(
            seen.request_marker(
                {"message_seq": 12, "revision": 4}, include_revision=True
            ),
            (12, 4),
        )
        for body in (
            {"message_seq": 1},
            {"message_seq": True, "revision": 1},
            {"message_seq": -1, "revision": 1},
            {"message_seq": 1, "revision": 1, "extra": 1},
        ):
            with self.subTest(body=body), self.assertRaises(WorkspaceError) as error:
                seen.request_marker(body, include_revision=True)
            self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_index_items_receive_zero_for_a_new_unseen_item(self) -> None:
        cursor = MagicMock()
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        cursor.fetchall.return_value = [("app-1", 9, 3)]
        items = [{"app_id": "app-1"}, {"app_id": "app-2"}]
        with patch.object(seen.db, "transaction", return_value=transaction):
            seen.add_to_items("apps", items, "app_id")

        self.assertEqual(items[0]["seen_message_seq"], 9)
        self.assertEqual(items[0]["seen_revision"], 3)
        self.assertEqual(items[1]["seen_message_seq"], 0)
        self.assertEqual(items[1]["seen_revision"], 0)

    def test_save_uses_a_monotonic_upsert(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (15, 8)
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        with patch.object(seen.db, "transaction", return_value=transaction):
            result = seen.save("apps", "app-1", 12, 7)

        self.assertEqual(result, {"message_seq": 15, "revision": 8})
        statement = cursor.execute.call_args.args[0]
        self.assertIn("GREATEST(workspace_seen.message_seq", statement)
        self.assertIn("GREATEST(workspace_seen.revision", statement)
