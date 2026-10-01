"""The operator socket must outlive the entire approved X video operation."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from host.runtime.admin_api import tools_client
from host.tools.shared.web import DEFAULT_TIMEOUT_SECONDS
from host.tools.twitter.video import UPLOAD_TIMEOUT_SECONDS


class ToolsOperatorTimeoutTests(unittest.TestCase):
    def test_socket_budget_covers_video_and_surrounding_provider_requests(self):
        # Optional OAuth refresh, identity lookup, target lookup, then post.
        # The video deadline starts after those first three requests.
        approval_seconds = UPLOAD_TIMEOUT_SECONDS + 4 * DEFAULT_TIMEOUT_SECONDS
        with patch.object(tools_client.socket, "socket") as socket:
            connection = tools_client._ToolsSocketConnection("/unused/tools.sock")
            connection.connect()
            effective_timeout = socket.return_value.settimeout.call_args.args[0]
            self.assertGreater(effective_timeout, approval_seconds)
            socket.return_value.connect.assert_called_once_with("/unused/tools.sock")
            connection.close()
