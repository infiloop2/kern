"""CI covers account selection and the real guarded direct-read host path."""

from copy import deepcopy
from unittest.mock import patch

from host.runtime.core import state
from host.runtime.tools import tools_host
from host.tools import google_search_console as sc
from test_tools_host import ToolsHostTestCase
from test_tools_google_search_console import SITE, PROPERTY_RESPONSE, connected_api
from test_tools_search_console_batch import evidence


class SearchConsoleBatchHostTests(ToolsHostTestCase):
    def setUp(self):
        super().setUp()
        with state.mutation() as cur:
            state.set_tool_enabled(cur, "google_search_console", True)
        for connection in ("first", "selected"):
            credential = deepcopy(connected_api().credentials.load())
            credential["account"]["id"] = f"sub-{connection}"
            credential["secret"]["access_token"] = f"token-{connection}"
            state.put_tool_credential("google_search_console", credential, connection)

    def call(self, urls, **extra):
        return tools_host.execute_action("google_search_console", "inspect_urls",
            {"site_url": SITE, "urls": urls, **extra}, origin_thread_id=None, connection_id="selected")

    def test_selected_account_property_and_every_inspection_share_one_token_without_approval(self):
        urls = ["https://example.com/first", "https://example.com/second?version=2"]
        with patch.object(sc, "google_json_request", side_effect=[PROPERTY_RESPONSE, evidence("PASS"), evidence("NEUTRAL")]) as request, patch.object(state, "insert_tool_approval") as queue:
            result = self.call(urls)
        self.assertEqual(result["status"], "executed")
        self.assertEqual([call.args[2] for call in request.call_args_list], ["token-selected"] * 3)
        self.assertEqual([row["url"] for row in result["result"]["results"]], urls)
        self.assertEqual(state.page_tool_events_before(None)[0]["connection_id"], "selected")
        queue.assert_not_called()

    def test_later_url_guard_and_nested_input_rejection_happen_before_provider_read(self):
        with patch.object(sc, "google_json_request") as request:
            result = self.call(["https://example.com/safe", "https://example.com/?email=alice%2540example.com"])
            self.assertEqual(result["status"], "failed")
            self.assertIn("email", result["error"])
            for urls in ([{"url": "https://example.com/"}], [["https://example.com/"]]):
                with self.subTest(urls=urls), self.assertRaisesRegex(tools_host.ToolCallError, "Invalid input"):
                    self.call(urls)
            request.assert_not_called()
