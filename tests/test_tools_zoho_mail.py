"""Unit tests for the Zoho Mail bundled tool (all provider calls mocked)."""

from __future__ import annotations

import unittest
from typing import Any, cast
from unittest.mock import patch

from host.runtime.tools import tools_host
from host.tools import zoho_mail
from host.tools.json_types import JSONObject, JSONValue
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.web import WebRequestError
from host.tools.zoho_mail import ZohoMailTool

from test_tools import FRESH_EXPIRES_AT, FakeHostAPI


ACCOUNT: JSONObject = {
    "type": "ZOHO_ACCOUNT",
    "accountId": "2560636000000008002",
    "mailboxAddress": "owner@example.com",
    "primaryEmailAddress": "owner@example.com",
    "emailAddress": cast(list[JSONValue], [
        {
            "mailId": "owner@example.com",
            "isPrimary": True,
            "isAlias": False,
            "isConfirmed": True,
        },
        {
            "mailId": "alias@example.com",
            "isPrimary": False,
            "isAlias": True,
            "isConfirmed": True,
        },
    ]),
    "sendMailDetails": cast(list[JSONValue], [
        {"fromAddress": "owner@example.com", "status": True},
        {"fromAddress": "alias@example.com", "status": True},
    ]),
}


def success(data: JSONValue) -> JSONObject:
    return {"status": {"code": 200, "description": "success"}, "data": data}


def connected_api(*, expires_at: int = FRESH_EXPIRES_AT) -> FakeHostAPI:
    api = FakeHostAPI()
    api.config.update({
        "ZOHO_OAUTH_CLIENT_ID": "zoho-client",
        "ZOHO_OAUTH_CLIENT_SECRET": "zoho-secret",
        "ZOHO_DATA_CENTER": "eu",
    })
    api.credentials.save({
        "account": {
            "id": "2560636000000008002",
            "label": "owner@example.com",
            "scopes": list(zoho_mail.ZOHO_OAUTH_SCOPES),
        },
        "secret": {
            "access_token": "zoho-access",
            "expires_at": expires_at,
            "refresh_token": "zoho-refresh",
            "scope": ",".join(zoho_mail.ZOHO_OAUTH_SCOPES),
            "token_type": "Bearer",
        },
        "metadata": {"created_at": 1, "updated_at": 1, "data_center": "eu"},
    })
    return api


class ZohoMailToolTests(unittest.TestCase):
    def test_manifest_is_narrow_and_approval_gates_send(self) -> None:
        tool = ZohoMailTool()
        self.assertEqual(tool.manifest.connection, "oauth")
        self.assertEqual(
            [action.id for action in tool.manifest.actions],
            ["search_messages", "list_folders", "list_messages", "read_message", "send_email"],
        )
        send = tool.manifest.action("send_email")
        assert send is not None
        self.assertEqual(send.approval, "operator")
        self.assertEqual(set(zoho_mail.ZOHO_OAUTH_SCOPES), {
            "ZohoMail.accounts.READ",
            "ZohoMail.folders.READ",
            "ZohoMail.messages.READ",
            "ZohoMail.messages.CREATE",
        })
        self.assertTrue(all("DELETE" not in scope and "UPDATE" not in scope for scope in zoho_mail.ZOHO_OAUTH_SCOPES))
        self.assertIn("attachments", tool.manifest.agent_notes)

        rich_input = {
            "to": "client@example.com",
            "subject": "Rich message",
            "blocks": [
                {"type": "heading", "level": "2", "text": "Welcome"},
                {"type": "paragraph", "text": "Intro"},
                {"type": "line_group", "lines": ["Regards", "Akshay"]},
                {"type": "bullet_list", "items": ["One", "Two"]},
                {"type": "numbered_list", "items": ["First", "Second"]},
                {
                    "type": "rich_text",
                    "segments": [
                        {"text": "Important", "style": "bold"},
                        {"text": " details at "},
                        {"text": "the plan", "url": "https://example.com/plan"},
                    ],
                },
                {"type": "divider"},
            ],
        }
        self.assertEqual(
            tools_host.validate_against_schema(rich_input, send.input_schema),
            "",
        )

    def test_list_folders_maps_only_bounded_fields(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            seen.update(method=method, url=url, headers=kwargs.get("headers"))
            return success(cast(list[JSONValue], [{
                "folderId": "9000000002014",
                "folderName": "Inbox",
                "path": "/Inbox",
                "folderType": "Inbox",
                "imapAccess": True,
                "URI": "https://attacker.example/ignored",
                "unexpected": "ignored",
            }]))

        with patch.object(zoho_mail, "json_request", fake_json_request):
            result = ZohoMailTool().execute("list_folders", {}, connected_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(seen["method"], "GET")
        self.assertEqual(seen["url"], "https://mail.zoho.eu/api/accounts/2560636000000008002/folders")
        self.assertEqual(seen["headers"]["authorization"], "Zoho-oauthtoken zoho-access")
        folders = result.result["folders"]
        assert isinstance(folders, list)
        self.assertEqual(folders[0], {
            "folder_id": "9000000002014",
            "name": "Inbox",
            "path": "/Inbox",
            "type": "Inbox",
            "imap_access": True,
        })

    def test_search_guards_query_and_caps_provider_results(self) -> None:
        urls: list[str] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            urls.append(url)
            return success(cast(list[JSONValue], [
                {
                    "messageId": str(1000 + index),
                    "folderId": "9000000002014",
                    "fromAddress": "client@example.com",
                    "subject": f"Message {index}",
                    "summary": "hello",
                }
                for index in range(20)
            ]))

        with patch.object(zoho_mail, "json_request", fake_json_request):
            result = ZohoMailTool().execute(
                "search_messages",
                {"search_key": "sender:client@example.com::has:attachment", "limit": "5"},
                connected_api(),
            )
        assert isinstance(result, ActionExecuted)
        self.assertIn("searchKey=sender%3Aclient%40example.com%3A%3Ahas%3Aattachment", urls[0])
        messages = result.result["messages"]
        assert isinstance(messages, list)
        self.assertEqual(len(messages), 5)

        denied = ZohoMailTool().execute(
            "search_messages", {"search_key": "entire:AKIAIOSFODNN7EXAMPLE"}, connected_api()
        )
        assert isinstance(denied, ActionFailed)
        self.assertIn("credential", denied.error)

    def test_list_messages_validates_folder_id_before_request(self) -> None:
        with patch.object(zoho_mail, "json_request") as request:
            result = ZohoMailTool().execute("list_messages", {"folder_id": "../messages"}, connected_api())
        assert isinstance(result, ActionFailed)
        request.assert_not_called()

    def test_read_message_combines_metadata_and_plaintext_body(self) -> None:
        urls: list[str] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            urls.append(url)
            if url.endswith("/details"):
                return success({
                    "messageId": "1709876190693100009",
                    "folderId": "9000000002014",
                    "fromAddress": "client@example.com",
                    "toAddress": "owner@example.com",
                    "subject": "Proposal &amp; next steps",
                    "summary": "Hello",
                })
            if url.endswith("/content"):
                return success({
                    "messageId": "1709876190693100009",
                    "content": "<style>secret style</style><p>Hello <b>Akshay</b></p><script>bad()</script><div>Next step</div>",
                })
            raise AssertionError(url)

        with patch.object(zoho_mail, "json_request", fake_json_request):
            result = ZohoMailTool().execute(
                "read_message",
                {"folder_id": "9000000002014", "message_id": "1709876190693100009"},
                connected_api(),
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(urls), 2)
        message = result.result["zoho_message"]
        assert isinstance(message, dict)
        self.assertEqual(message["subject"], "Proposal & next steps")
        self.assertEqual(message["content"], "Hello Akshay\nNext step")
        self.assertFalse(message["content_truncated"])

    def test_send_queues_exact_message_then_executes_after_approval(self) -> None:
        api = connected_api()
        calls: list[tuple[str, str, JSONObject | None]] = []

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            calls.append((method, url, kwargs.get("body")))
            if url.endswith("/api/accounts"):
                return success(cast(list[JSONValue], [ACCOUNT]))
            if url.endswith("/messages"):
                return success({"messageId": "777"})
            raise AssertionError(url)

        with patch.object(zoho_mail, "json_request", fake_json_request):
            pending = ZohoMailTool().execute(
                "send_email",
                {
                    "from_address": "alias@example.com",
                    "to": "client@example.com",
                    "cc": "copy@example.com",
                    "bcc": "audit@example.com",
                    "subject": "Hello",
                    "blocks": [
                        {"type": "paragraph", "text": "Hi there"},
                        {"type": "line_group", "lines": ["Regards,", "Akshay"]},
                    ],
                },
                api,
            )
        assert isinstance(pending, ActionPendingApproval)
        self.assertIn("from alias@example.com", pending.summary)
        self.assertIn("bcc audit@example.com", pending.summary)
        self.assertIn("as html", pending.summary)
        self.assertIn("Hi there", pending.summary)
        self.assertEqual(len(calls), 1)
        record = api.approvals.get(pending.approval_id)
        assert record is not None
        proposal = record.payload["proposal"]
        assert isinstance(proposal, dict)
        approved_message = proposal["message"]
        self.assertEqual(approved_message, {
            "fromAddress": "alias@example.com",
            "toAddress": "client@example.com",
            "subject": "Hello",
            "content": "<p>Hi there</p>\n<p>Regards,<br>Akshay</p>",
            "mailFormat": "html",
            "ccAddress": "copy@example.com",
            "bccAddress": "audit@example.com",
        })

        approved = api.approvals.approve(pending.approval_id)
        with patch.object(zoho_mail, "json_request", fake_json_request):
            result = ZohoMailTool().execute_approved(approved, api)
        assert isinstance(result, ApprovalExecuted)
        self.assertIn("client@example.com", result.message)
        self.assertEqual(calls[-1][0], "POST")
        self.assertEqual(calls[-1][1], "https://mail.zoho.eu/api/accounts/2560636000000008002/messages")
        self.assertEqual(calls[-1][2], approved_message)

    def test_rich_html_is_escaped_and_rendered_from_typed_blocks(self) -> None:
        api = connected_api()
        with patch.object(
            zoho_mail,
            "json_request",
            return_value=success(cast(list[JSONValue], [ACCOUNT])),
        ):
            pending = ZohoMailTool().execute(
                "send_email",
                {
                    "to": "client@example.com",
                    "subject": "Rich message",
                    "blocks": [
                        {"type": "heading", "level": "2", "text": "Welcome <team>"},
                        {
                            "type": "rich_text",
                            "segments": [
                                {"text": "Hello "},
                                {"text": "<Akshay>", "style": "bold"},
                                {"text": ", see "},
                                {"text": "the plan", "url": "https://example.com/plan?x=1&y=2"},
                                {"text": ".", "style": "italic"},
                            ],
                        },
                        {"type": "bullet_list", "items": ["First <item>", "Second"]},
                        {"type": "numbered_list", "items": ["Review", "Approve"]},
                        {"type": "divider"},
                    ],
                },
                api,
            )
        assert isinstance(pending, ActionPendingApproval)
        record = api.approvals.get(pending.approval_id)
        assert record is not None
        proposal = record.payload["proposal"]
        assert isinstance(proposal, dict)
        message = proposal["message"]
        assert isinstance(message, dict)
        self.assertEqual(message["mailFormat"], "html")
        self.assertEqual(
            message["content"],
            "<h2>Welcome &lt;team&gt;</h2>\n"
            "<p>Hello <strong>&lt;Akshay&gt;</strong>, see "
            '<a href="https://example.com/plan?x=1&amp;y=2">the plan</a><em>.</em></p>\n'
            "<ul><li>First &lt;item&gt;</li><li>Second</li></ul>\n"
            "<ol><li>Review</li><li>Approve</li></ol>\n<hr>",
        )
        self.assertNotIn("<team>", str(message["content"]))
        self.assertIn("Welcome <team>", pending.summary)

    def test_plaintext_format_remains_available(self) -> None:
        api = connected_api()
        with patch.object(
            zoho_mail,
            "json_request",
            return_value=success(cast(list[JSONValue], [ACCOUNT])),
        ):
            pending = ZohoMailTool().execute(
                "send_email",
                {
                    "to": "client@example.com",
                    "subject": "Plain message",
                    "mail_format": "plaintext",
                    "blocks": [
                        {"type": "heading", "level": "2", "text": "Update"},
                        {"type": "bullet_list", "items": ["One", "Two"]},
                    ],
                },
                api,
            )
        assert isinstance(pending, ActionPendingApproval)
        record = api.approvals.get(pending.approval_id)
        assert record is not None
        proposal = record.payload["proposal"]
        assert isinstance(proposal, dict)
        message = proposal["message"]
        assert isinstance(message, dict)
        self.assertEqual(message["mailFormat"], "plaintext")
        self.assertEqual(message["content"], "Update\n\n- One\n- Two")

    def test_plaintext_format_is_not_capped_by_unused_html_rendering(self) -> None:
        api = connected_api()
        body = "&" * 16_001
        with patch.object(
            zoho_mail,
            "json_request",
            return_value=success(cast(list[JSONValue], [ACCOUNT])),
        ):
            pending = ZohoMailTool().execute(
                "send_email",
                {
                    "to": "client@example.com",
                    "subject": "Plain ampersands",
                    "mail_format": "plaintext",
                    "blocks": [{"type": "paragraph", "text": body}],
                },
                api,
            )
        assert isinstance(pending, ActionPendingApproval)
        record = api.approvals.get(pending.approval_id)
        assert record is not None
        proposal = record.payload["proposal"]
        assert isinstance(proposal, dict)
        message = proposal["message"]
        assert isinstance(message, dict)
        self.assertEqual(message["mailFormat"], "plaintext")
        self.assertEqual(message["content"], body)

    def test_rich_send_rejects_raw_html_and_unsafe_links(self) -> None:
        invalid_blocks = (
            [{"type": "html", "html": "<img src=https://tracker.example/pixel>"}],
            [
                {
                    "type": "rich_text",
                    "segments": [{"text": "click", "url": "javascript:alert(1)"}],
                }
            ],
            [
                {
                    "type": "rich_text",
                    "segments": [{"text": "click", "url": "https://user:pass@example.com"}],
                }
            ],
        )
        for blocks in invalid_blocks:
            with self.subTest(blocks=blocks), patch.object(
                zoho_mail,
                "json_request",
                return_value=success(cast(list[JSONValue], [ACCOUNT])),
            ):
                result = ZohoMailTool().execute(
                    "send_email",
                    {"to": "client@example.com", "subject": "Unsafe", "blocks": blocks},
                    connected_api(),
                )
            assert isinstance(result, ActionFailed)
            self.assertIn("structured body blocks", result.error)

    def test_rich_send_rejects_html_that_exceeds_approval_budget(self) -> None:
        api = connected_api()
        with patch.object(
            zoho_mail,
            "json_request",
            return_value=success(cast(list[JSONValue], [ACCOUNT])),
        ):
            result = ZohoMailTool().execute(
                "send_email",
                {
                    "to": "client@example.com",
                    "subject": "Escaping expansion",
                    "blocks": [{"type": "paragraph", "text": "&" * 13_010}],
                },
                api,
            )
        assert isinstance(result, ActionFailed)
        self.assertIn("too large to queue for approval", result.error)
        self.assertEqual(api.approvals.records, {})

    def test_send_rejects_unverified_from_address(self) -> None:
        with patch.object(zoho_mail, "json_request", return_value=success(cast(list[JSONValue], [ACCOUNT]))):
            result = ZohoMailTool().execute(
                "send_email",
                {
                    "from_address": "attacker@example.com",
                    "to": "client@example.com",
                    "subject": "Hello",
                    "blocks": [{"type": "paragraph", "text": "Body"}],
                },
                connected_api(),
            )
        assert isinstance(result, ActionFailed)
        self.assertIn("not a verified sender", result.error)

    def test_send_rejects_subject_header_injection(self) -> None:
        with patch.object(zoho_mail, "json_request", return_value=success(cast(list[JSONValue], [ACCOUNT]))):
            result = ZohoMailTool().execute(
                "send_email",
                {
                    "to": "client@example.com",
                    "subject": "Hello\r\nBcc: attacker@example.com",
                    "blocks": [{"type": "paragraph", "text": "Body"}],
                },
                connected_api(),
            )
        assert isinstance(result, ActionFailed)
        self.assertIn("subject", result.error)

    def test_approved_send_fails_closed_when_account_changes(self) -> None:
        api = connected_api()
        with patch.object(zoho_mail, "json_request", return_value=success(cast(list[JSONValue], [ACCOUNT]))):
            pending = ZohoMailTool().execute(
                "send_email",
                {
                    "to": "client@example.com",
                    "subject": "Hello",
                    "blocks": [{"type": "paragraph", "text": "Body"}],
                },
                api,
            )
        assert isinstance(pending, ActionPendingApproval)
        approved = api.approvals.approve(pending.approval_id)
        other = dict(ACCOUNT)
        other["accountId"] = "999999999999"
        other["mailboxAddress"] = "other@example.com"
        other["primaryEmailAddress"] = "other@example.com"
        with patch.object(
            zoho_mail,
            "json_request",
            return_value=success(cast(list[JSONValue], [cast(JSONObject, other)])),
        ):
            result = ZohoMailTool().execute_approved(approved, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIsNone(api.credentials.load())

    def test_provider_unauthorized_requires_reconnect(self) -> None:
        with patch.object(zoho_mail, "json_request", side_effect=WebRequestError("failed", status=401)):
            result = ZohoMailTool().execute("list_folders", {}, connected_api())
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)

    def test_every_direct_result_matches_declared_output_schema(self) -> None:
        cases = (
            ("search_messages", {"search_key": "entire:invoice"}),
            ("list_folders", {}),
            ("list_messages", {"folder_id": "123"}),
        )
        for action, tool_input in cases:
            with self.subTest(action=action), patch.object(
                zoho_mail, "json_request", return_value=success(cast(list[JSONValue], []))
            ):
                result = ZohoMailTool().execute(action, tool_input, connected_api())
            assert isinstance(result, ActionExecuted)
            spec = zoho_mail.MANIFEST.action(action)
            assert spec is not None
            self.assertEqual(tools_host.validate_against_schema(result.result, spec.output_schema, path="result"), "")


class ZohoMailCredentialFlowTests(unittest.TestCase):
    def test_all_documented_data_centers_pin_expected_endpoints(self) -> None:
        self.assertEqual(zoho_mail.ZOHO_DATA_CENTERS, {
            "us": ("https://accounts.zoho.com", "https://mail.zoho.com"),
            "eu": ("https://accounts.zoho.eu", "https://mail.zoho.eu"),
            "in": ("https://accounts.zoho.in", "https://mail.zoho.in"),
            "au": ("https://accounts.zoho.com.au", "https://mail.zoho.com.au"),
            "jp": ("https://accounts.zoho.jp", "https://mail.zoho.jp"),
            "ca": ("https://accounts.zohocloud.ca", "https://mail.zohocloud.ca"),
            "cn": ("https://accounts.zoho.com.cn", "https://mail.zoho.com.cn"),
            "ae": ("https://accounts.zoho.ae", "https://mail.zoho.ae"),
            "sa": ("https://accounts.zoho.sa", "https://mail.zoho.sa"),
        })

    def test_start_connect_uses_configured_eu_data_center_and_offline_scopes(self) -> None:
        result = ZohoMailTool().credentials.start_connect(
            {"redirect_uri": "https://host.example/oauth/callback"}, connected_api()
        )
        self.assertTrue(result["authorization_url"].startswith("https://accounts.zoho.eu/oauth/v2/auth?"))
        self.assertIn("access_type=offline", result["authorization_url"])
        self.assertIn("ZohoMail.messages.CREATE", result["authorization_url"])
        self.assertIn("state=", result["authorization_url"])

    def test_complete_connect_exchanges_code_and_saves_mailbox(self) -> None:
        api = FakeHostAPI()
        api.config.update({
            "ZOHO_OAUTH_CLIENT_ID": "zoho-client",
            "ZOHO_OAUTH_CLIENT_SECRET": "zoho-secret",
            "ZOHO_DATA_CENTER": "eu",
        })
        flow = ZohoMailTool().credentials
        started = flow.start_connect({"redirect_uri": "https://host.example/oauth/callback"}, api)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == "https://accounts.zoho.eu/oauth/v2/token":
                self.assertEqual(kwargs["form"]["grant_type"], "authorization_code")
                self.assertEqual(kwargs["form"]["scope"], ",".join(zoho_mail.ZOHO_OAUTH_SCOPES))
                return {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            if url == "https://mail.zoho.eu/api/accounts":
                self.assertEqual(kwargs["headers"]["authorization"], "Zoho-oauthtoken new-access")
                return success(cast(list[JSONValue], [ACCOUNT]))
            raise AssertionError(url)

        with patch.object(zoho_mail, "json_request", fake_json_request):
            result = flow.complete_connect(
                {
                    "code": "auth-code",
                    "state": started["state"],
                    "redirect_uri": "https://host.example/oauth/callback",
                },
                api,
            )
        self.assertEqual(result["account"]["label"], "owner@example.com")
        stored = api.credentials.load()
        assert stored is not None
        self.assertEqual(stored["secret"]["refresh_token"], "new-refresh")
        self.assertEqual(stored["account"]["scopes"], list(zoho_mail.ZOHO_OAUTH_SCOPES))
        self.assertEqual(stored["metadata"]["data_center"], "eu")

    def test_complete_connect_rejects_missing_scope_without_replacing_connection(self) -> None:
        api = connected_api()
        existing = api.credentials.load()
        flow = ZohoMailTool().credentials
        started = flow.start_connect({"redirect_uri": "https://host.example/cb"}, api)
        with patch.object(zoho_mail, "json_request", return_value={
            "access_token": "narrow",
            "refresh_token": "refresh",
            "expires_in": 3600,
            "scope": "ZohoMail.accounts.READ ZohoMail.messages.READ",
        }):
            with self.assertRaisesRegex(RuntimeError, "missing required permissions"):
                flow.complete_connect(
                    {"code": "code", "state": started["state"], "redirect_uri": "https://host.example/cb"}, api
                )
        self.assertEqual(api.credentials.load(), existing)

    def test_expired_access_token_refreshes_without_rotating_refresh_token(self) -> None:
        api = connected_api(expires_at=1)

        def fake_json_request(method: str, url: str, **kwargs: Any) -> JSONObject:
            if url == "https://accounts.zoho.eu/oauth/v2/token":
                self.assertEqual(kwargs["form"]["grant_type"], "refresh_token")
                return {"access_token": "refreshed-access", "expires_in": "3600"}
            if url.endswith("/folders"):
                self.assertEqual(kwargs["headers"]["authorization"], "Zoho-oauthtoken refreshed-access")
                return success(cast(list[JSONValue], []))
            raise AssertionError(url)

        with patch.object(zoho_mail, "json_request", fake_json_request):
            result = ZohoMailTool().execute("list_folders", {}, api)
        assert isinstance(result, ActionExecuted)
        stored = api.credentials.load()
        assert stored is not None
        self.assertEqual(stored["secret"]["access_token"], "refreshed-access")
        self.assertEqual(stored["secret"]["refresh_token"], "zoho-refresh")

    def test_invalid_grant_clears_connection(self) -> None:
        api = connected_api(expires_at=1)
        with patch.object(
            zoho_mail,
            "json_request",
            side_effect=WebRequestError("failed", status=400, body=b'{"error":"invalid_grant"}'),
        ):
            result = ZohoMailTool().execute("list_folders", {}, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIsNone(api.credentials.load())

    def test_changed_data_center_requires_reconnect_without_wrong_region_call(self) -> None:
        api = connected_api()
        api.config["ZOHO_DATA_CENTER"] = "us"
        with patch.object(zoho_mail, "json_request") as request:
            result = ZohoMailTool().execute("list_folders", {}, api)
        assert isinstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        request.assert_not_called()

    def test_invalid_data_center_is_rejected_before_oauth_url_is_built(self) -> None:
        api = connected_api()
        api.config["ZOHO_DATA_CENTER"] = "example.com"
        with self.assertRaisesRegex(RuntimeError, "must be one of"):
            ZohoMailTool().credentials.start_connect({"redirect_uri": "https://host.example/cb"}, api)


if __name__ == "__main__":
    unittest.main()
