"""Unit tests for the WhatsApp linked-device tool (gateway fully mocked)."""

from __future__ import annotations

import io
from http import HTTPStatus
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from host.runtime.admin_api import tools_client as tools_admin_api
from host.runtime.tools import api as tools_api
from host.runtime.tools import service as tools_service
from host.tools.whatsapp import gateway as whatsapp_gateway
from host.tools.whatsapp.gateway import WhatsAppGateway, WhatsAppGatewayError
from host.tools import whatsapp
from host.tools.host_api import ApprovalRecord
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.whatsapp import WhatsAppTool

from test_tools import FakeHostAPI, assert_matches_output_schema


CONNECTED = {
    "status": "connected",
    "connected": True,
    "account": {"id": "447700900000:1@s.whatsapp.net", "label": "Infiverse"},
    "qr_data_url": "data:image/png;base64,operator-only",
    "error": "",
}


class WhatsAppToolTests(unittest.TestCase):
    def test_manifest_declares_direct_reads_and_approved_single_send(self) -> None:
        manifest = WhatsAppTool().manifest
        self.assertEqual(manifest.connection, "whatsapp_linked_device")
        self.assertEqual(
            manifest.description,
            "Let agents read your WhatsApp chats and send messages.",
        )
        self.assertEqual(
            manifest.service,
            "host.tools.whatsapp.gateway:GATEWAY",
        )
        self.assertIsNone(WhatsAppTool().credentials)
        self.assertEqual(
            {action.id: action.approval for action in manifest.actions},
            {
                "connection_status": "direct",
                "list_chats": "direct",
                "read_messages": "direct",
                "send_message": "operator",
            },
        )
        self.assertIn("unofficial", " ".join(manifest.protections).lower())

    def test_connection_status_never_returns_qr_to_agent(self) -> None:
        with patch.object(whatsapp, "gateway_request", return_value=CONNECTED):
            result = WhatsAppTool().execute("connection_status", {}, FakeHostAPI())
        assert isinstance(result, ActionExecuted)
        self.assertNotIn("qr", result.result)
        self.assertEqual(result.result["account_label"], "Infiverse")
        assert_matches_output_schema(self, whatsapp.MANIFEST, "connection_status", result)

    def test_cached_reads_are_mapped_to_declared_outputs(self) -> None:
        chat = {"id": "447700900123@s.whatsapp.net", "name": "Shop", "last_message_at": 10, "unread_count": 1, "preview": "hello"}
        message = {"id": "m1", "chat_id": chat["id"], "sender_id": chat["id"], "from_me": False, "timestamp": 10, "text": "hello", "type": "text"}

        def gateway(method, params=None):
            if method == "list_chats":
                return {"chats": [chat]}
            if method == "read_messages":
                return {"chat_id": chat["id"], "messages": [message]}
            raise AssertionError(method)

        with patch.object(whatsapp, "gateway_request", side_effect=gateway):
            chats = WhatsAppTool().execute("list_chats", {"limit": 10}, FakeHostAPI())
            messages = WhatsAppTool().execute("read_messages", {"chat_id": chat["id"], "limit": 10}, FakeHostAPI())
        assert isinstance(chats, ActionExecuted)
        assert isinstance(messages, ActionExecuted)
        assert_matches_output_schema(self, whatsapp.MANIFEST, "list_chats", chats)
        assert_matches_output_schema(self, whatsapp.MANIFEST, "read_messages", messages)

    def test_send_queues_exact_account_bound_payload_without_sending(self) -> None:
        api = FakeHostAPI()
        with patch.object(whatsapp, "gateway_request", return_value=CONNECTED) as gateway:
            result = WhatsAppTool().execute(
                "send_message",
                {"recipient": "+447700900123", "text": "Hello from Infiverse"},
                api,
            )
        assert isinstance(result, ActionPendingApproval)
        gateway.assert_called_once_with("status")
        record = api.approvals.get(result.approval_id)
        assert record is not None
        self.assertEqual(record.payload["recipient"], "+447700900123")
        self.assertEqual(record.payload["text"], "Hello from Infiverse")
        self.assertEqual(record.payload["account_id"], CONNECTED["account"]["id"])

    def test_send_accepts_a_short_e164_number(self) -> None:
        api = FakeHostAPI()
        with patch.object(whatsapp, "gateway_request", return_value=CONNECTED):
            result = WhatsAppTool().execute(
                "send_message",
                {"recipient": "+6901234", "text": "Hello"},
                api,
            )
        assert isinstance(result, ActionPendingApproval)
        record = api.approvals.get(result.approval_id)
        assert record is not None
        self.assertEqual(record.payload["recipient"], "+6901234")

    def test_send_rejects_non_e164_bulk_and_empty_text(self) -> None:
        tool = WhatsAppTool()
        for tool_input in (
            {"recipient": "07700900123", "text": "hello"},
            {"recipient": "+447700900123,+447700900124", "text": "hello"},
            {"recipient": "+447700900123", "text": ""},
            {"recipient": "+447700900123", "text": "x" * 4097},
        ):
            with self.subTest(tool_input=tool_input), patch.object(whatsapp, "gateway_request") as gateway:
                self.assertIsInstance(tool.execute("send_message", tool_input, FakeHostAPI()), ActionFailed)
                gateway.assert_not_called()

    def test_approved_send_rechecks_account_before_write(self) -> None:
        approval = ApprovalRecord(
            approval_id="approval-1",
            action_id="send_message",
            status="approved",
            payload={
                "action": "send_message",
                "account_id": CONNECTED["account"]["id"],
                "account_label": "Infiverse",
                "recipient": "+447700900123",
                "text": "hello",
            },
            summary="send",
            created_at=1,
            decided_at=2,
        )
        changed = {**CONNECTED, "account": {"id": "different", "label": "Other"}}
        with patch.object(whatsapp, "gateway_request", return_value=changed) as gateway:
            result = WhatsAppTool().execute_approved(approval, FakeHostAPI())
        assert isinstance(result, ActionFailed)
        gateway.assert_called_once_with("status")
        self.assertIn("changed", result.error)

    def test_approved_send_uses_exact_recorded_payload(self) -> None:
        approval = ApprovalRecord(
            approval_id="approval-1",
            action_id="send_message",
            status="approved",
            payload={
                "action": "send_message",
                "account_id": CONNECTED["account"]["id"],
                "account_label": "Infiverse",
                "recipient": "+447700900123",
                "text": "exact approved text",
            },
            summary="send",
            created_at=1,
            decided_at=2,
        )
        calls = []

        def gateway(method, params=None):
            calls.append((method, params))
            return CONNECTED if method == "status" else {"message_id": "wamid-1", "recipient": "447700900123@s.whatsapp.net"}

        with patch.object(whatsapp, "gateway_request", side_effect=gateway):
            result = WhatsAppTool().execute_approved(approval, FakeHostAPI())
        assert isinstance(result, ApprovalExecuted)
        self.assertEqual(
            calls[-1],
            (
                "send_message",
                {
                    "account_id": "447700900000:1@s.whatsapp.net",
                    "recipient": "+447700900123",
                    "text": "exact approved text",
                },
            ),
        )

    def test_gateway_rejection_prevents_send_after_account_race(self) -> None:
        approval = ApprovalRecord(
            approval_id="approval-1",
            action_id="send_message",
            status="approved",
            payload={
                "action": "send_message",
                "account_id": CONNECTED["account"]["id"],
                "account_label": "Infiverse",
                "recipient": "+447700900123",
                "text": "do not send from another account",
            },
            summary="send",
            created_at=1,
            decided_at=2,
        )

        def gateway(method, params=None):
            if method == "status":
                return CONNECTED
            raise WhatsAppGatewayError(
                "The linked WhatsApp account changed after approval. Queue a new message."
            )

        with patch.object(whatsapp, "gateway_request", side_effect=gateway):
            result = WhatsAppTool().execute_approved(approval, FakeHostAPI())
        assert isinstance(result, ActionFailed)
        self.assertIn("changed after approval", result.error)


class WhatsAppOperatorRouteTests(unittest.TestCase):
    def test_service_route_dispatches_from_manifest_without_tool_id_special_case(self) -> None:
        service = MagicMock()
        def operator(operation, enabled):
            self.assertEqual(operation, "status")
            self.assertTrue(enabled())
            return {"status": "connected"}

        service.operator.side_effect = operator
        tool = MagicMock()
        tool.manifest.service = "example.service:INSTANCE"
        with (
            patch.dict(tools_api.tools_host.BUNDLED_TOOLS, {"example": tool}),
            patch.object(tools_api.tools_host, "tool_service", return_value=service),
            patch.object(tools_api.state, "enabled_tool_ids", return_value={"example"}),
        ):
            result = tools_api.handle_operator(
                "/operator/tools/example/service/status", {}
            )
        self.assertEqual(result, {"status": "connected"})
        service.operator.assert_called_once()

    def test_linked_device_status_holds_service_lifecycle_lock(self) -> None:
        held = False

        class Lifecycle:
            def __enter__(self):
                nonlocal held
                held = True

            def __exit__(self, _type, _value, _traceback):
                nonlocal held
                held = False

        def status(_method):
            self.assertTrue(held)
            return CONNECTED

        with (
            patch.object(
                whatsapp_gateway.GATEWAY, "lifecycle", return_value=Lifecycle()
            ) as lifecycle,
            patch.object(
                tools_api.state, "enabled_tool_ids", return_value={"whatsapp"}
            ),
            patch.object(whatsapp_gateway.GATEWAY, "request", side_effect=status),
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/status", {}
            )
        self.assertEqual(result, CONNECTED)
        self.assertFalse(held)
        lifecycle.assert_called_once_with()

    def test_operator_connect_returns_qr(self) -> None:
        qr = {
            "status": "qr",
            "connected": False,
            "account": None,
            "qr_data_url": "data:image/png;base64,qr",
            "error": "",
        }
        with (
            patch.object(
                tools_api.state, "enabled_tool_ids", return_value={"whatsapp"}
            ),
            patch.object(whatsapp_gateway.GATEWAY, "request", return_value=qr) as gateway,
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/connect", {}
            )
        self.assertEqual(result, qr)
        gateway.assert_called_once_with("connect")

    def test_operator_connect_without_qr_reports_safe_diagnostics(self) -> None:
        connecting = {
            "status": "connecting",
            "connected": False,
            "retained_data": True,
            "account": {"id": "secret-account", "label": "Private label"},
            "qr_data_url": "",
            "error": "",
            "diagnostic": {
                "connection_updates": 0,
                "last_connection_event": "none",
                "last_disconnect_code": None,
                "phase": "",
                "error_name": "",
                "error_code": "",
                "status_code": None,
                "transport_phase": "socket_created",
                "link_timed_out": False,
            },
        }
        with (
            patch.object(
                tools_api.state, "enabled_tool_ids", return_value={"whatsapp"}
            ),
            patch.object(
                whatsapp_gateway.GATEWAY, "request", return_value=connecting
            ),
            patch.object(
                whatsapp_gateway.host_errors, "report_warning"
            ) as report,
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/connect", {}
            )
        self.assertEqual(result, connecting)
        report.assert_called_once_with(
            "tools.whatsapp.gateway",
            "WhatsApp did not provide a QR code during the 30-second link wait.",
            context={
                "operation": "connect",
                "child_running": False,
                "child_exit_code": None,
                "status": "connecting",
                "connected": False,
                "retained_data": True,
                "qr_available": False,
                "account_present": True,
                "connection_updates": 0,
                "last_connection_event": "none",
                "last_disconnect_code": None,
                "phase": "",
                "error_name": "",
                "error_code": "",
                "status_code": None,
                "transport_phase": "socket_created",
                "link_timed_out": False,
            },
            kind="whatsapp_qr_unavailable",
        )

    def test_operator_connect_qr_timeout_reports_specific_diagnostic(self) -> None:
        timed_out = {
            "status": "error",
            "connected": False,
            "retained_data": True,
            "account": None,
            "qr_data_url": "",
            "error": "WhatsApp's network connection did not open. Check this host's internet and DNS access, then retry linking.",
            "diagnostic": {
                "connection_updates": 0,
                "last_connection_event": "none",
                "last_disconnect_code": None,
                "phase": "qr_timeout",
                "error_name": "Error",
                "error_code": "",
                "status_code": None,
                "transport_phase": "socket_created",
                "link_timed_out": True,
            },
        }
        with (
            patch.object(
                tools_api.state, "enabled_tool_ids", return_value={"whatsapp"}
            ),
            patch.object(
                whatsapp_gateway.GATEWAY, "request", return_value=timed_out
            ),
            patch.object(
                whatsapp_gateway.host_errors, "report_warning"
            ) as report,
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/connect", {}
            )

        self.assertEqual(result, timed_out)
        report.assert_called_once_with(
            "tools.whatsapp.gateway",
            "WhatsApp did not provide a QR code during the 30-second link wait.",
            context={
                "operation": "connect",
                "child_running": False,
                "child_exit_code": None,
                "status": "error",
                "connected": False,
                "retained_data": True,
                "qr_available": False,
                "account_present": False,
                "connection_updates": 0,
                "last_connection_event": "none",
                "last_disconnect_code": None,
                "phase": "qr_timeout",
                "error_name": "Error",
                "error_code": "",
                "status_code": None,
                "transport_phase": "socket_created",
                "link_timed_out": True,
            },
            kind="whatsapp_qr_unavailable",
        )

    def test_operator_connect_failure_reports_warning(self) -> None:
        error = WhatsAppGatewayError("WhatsApp gateway stopped unexpectedly.")
        process = MagicMock()
        process.poll.return_value = None
        diagnostic_status = {
            "status": "error",
            "connected": False,
            "retained_data": True,
            "account": None,
            "qr_data_url": "",
            "error": "WhatsApp connection failed.",
            "diagnostic": {
                "connection_updates": 1,
                "last_connection_event": "other",
                "last_disconnect_code": None,
                "phase": "socket_start",
                "error_name": "Error",
                "error_code": "ETIMEDOUT",
                "status_code": None,
                "transport_phase": "ws_open",
                "link_timed_out": False,
            },
        }
        with (
            patch.object(
                tools_api.state, "enabled_tool_ids", return_value={"whatsapp"}
            ),
            patch.object(
                whatsapp_gateway.GATEWAY,
                "request",
                side_effect=[error, diagnostic_status],
            ) as gateway,
            patch.object(whatsapp_gateway.GATEWAY, "_process", process),
            patch.object(
                whatsapp_gateway.host_errors, "report_warning"
            ) as report,
        ):
            with self.assertRaisesRegex(
                tools_api.OperatorError, "gateway stopped unexpectedly"
            ):
                tools_api.handle_operator(
                    "/operator/tools/whatsapp/service/connect", {}
                )
        report.assert_called_once_with(
            "tools.whatsapp.gateway",
            error,
            context={
                "operation": "connect",
                "child_running": True,
                "child_exit_code": None,
                "status": "error",
                "connected": False,
                "retained_data": True,
                "qr_available": False,
                "account_present": False,
                "connection_updates": 1,
                "last_connection_event": "other",
                "last_disconnect_code": None,
                "phase": "socket_start",
                "error_name": "Error",
                "error_code": "ETIMEDOUT",
                "status_code": None,
                "transport_phase": "ws_open",
                "link_timed_out": False,
            },
            kind="whatsapp_connect_failed",
        )
        self.assertEqual(
            [(entry.args, entry.kwargs) for entry in gateway.call_args_list],
            [
                (("connect",), {}),
                (("status",), {"timeout_seconds": 2}),
            ],
        )

    def test_status_and_disconnect_remain_available_while_disabled(self) -> None:
        suspended = {
            "status": "suspended",
            "connected": False,
            "account": CONNECTED["account"],
            "qr_data_url": "",
            "error": "",
        }
        with (
            patch.object(tools_api.state, "enabled_tool_ids", return_value=set()),
            patch.object(
                whatsapp_gateway.GATEWAY,
                "suspended_status_if_disabled",
                return_value=suspended,
            ) as status,
            patch.object(whatsapp_gateway.GATEWAY, "request") as gateway,
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/status", {}
            )
        self.assertEqual(result, suspended)
        status.assert_called_once()
        gateway.assert_not_called()

        disconnected = {
            "status": "disconnected",
            "connected": False,
            "retained_data": False,
            "account": CONNECTED["account"],
            "qr_data_url": "",
            "error": "",
        }
        with (
            patch.object(tools_api.state, "enabled_tool_ids", return_value=set()),
            patch.object(
                whatsapp_gateway.GATEWAY, "disconnect", return_value=disconnected
            ) as disconnect,
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/disconnect", {}
            )
        self.assertEqual(result, disconnected)
        disconnect.assert_called_once_with()

    def test_stale_disabled_status_does_not_stop_a_newly_enabled_gateway(self) -> None:
        with (
            patch.object(
                tools_api.state,
                "enabled_tool_ids",
                side_effect=[set(), {"whatsapp"}, {"whatsapp"}],
            ),
            patch.object(
                whatsapp_gateway.GATEWAY, "request", return_value=CONNECTED
            ) as live_status,
            patch.object(whatsapp_gateway.GATEWAY, "suspended_status") as suspended,
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/status", {}
            )
        self.assertEqual(result, CONNECTED)
        live_status.assert_called_once_with("status", None)
        suspended.assert_not_called()

    def test_operator_enable_starts_gateway_after_admin_enablement(self) -> None:
        with (
            patch.object(
                tools_api.state, "enabled_tool_ids", return_value={"whatsapp"}
            ),
            patch.object(whatsapp_gateway.GATEWAY, "start") as start,
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/enable", {}
            )
        self.assertTrue(result["enabled"])
        start.assert_called_once_with()

    def test_operator_enable_failure_reports_warning(self) -> None:
        error = WhatsAppGatewayError("WhatsApp gateway dependencies are not installed.")
        with (
            patch.object(
                tools_api.state, "enabled_tool_ids", return_value={"whatsapp"}
            ),
            patch.object(whatsapp_gateway.GATEWAY, "start", side_effect=error),
            patch.object(
                whatsapp_gateway.host_errors, "report_warning"
            ) as report,
        ):
            with self.assertRaisesRegex(
                tools_api.OperatorError, "dependencies are not installed"
            ):
                tools_api.handle_operator(
                    "/operator/tools/whatsapp/service/enable", {}
                )
        report.assert_called_once_with(
            "tools.whatsapp.gateway",
            error,
            context={
                "operation": "enable",
                "child_running": False,
                "child_exit_code": None,
            },
            kind="whatsapp_enable_failed",
        )

    def test_operator_enable_requires_admin_enablement(self) -> None:
        with (
            patch.object(tools_api.state, "enabled_tool_ids", return_value=set()),
            patch.object(whatsapp_gateway.GATEWAY, "start") as start,
        ):
            with self.assertRaisesRegex(tools_api.OperatorError, "not enabled"):
                tools_api.handle_operator(
                    "/operator/tools/whatsapp/service/enable", {}
                )
        start.assert_not_called()

    def test_operator_disable_stops_gateway(self) -> None:
        with (
            patch.object(tools_api.state, "enabled_tool_ids", return_value=set()),
            patch.object(whatsapp_gateway.GATEWAY, "stop") as stop,
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/disable", {}
            )
        self.assertFalse(result["enabled"])
        stop.assert_called_once_with()

    def test_stale_operator_disable_does_not_stop_a_newer_enable(self) -> None:
        with (
            patch.object(
                tools_api.state, "enabled_tool_ids", return_value={"whatsapp"}
            ),
            patch.object(whatsapp_gateway.GATEWAY, "stop") as stop,
        ):
            result = tools_api.handle_operator(
                "/operator/tools/whatsapp/service/disable", {}
            )
        self.assertTrue(result["enabled"])
        stop.assert_not_called()


class WhatsAppAdminRouteTests(unittest.TestCase):
    def test_enable_is_delegated_to_the_gateway_owner(self) -> None:
        expected = {"tool_id": "whatsapp", "enabled": True}
        with (
            patch.object(tools_admin_api.state, "enabled_tool_ids", return_value=set()),
            patch.object(tools_admin_api.state, "mutation") as mutation,
            patch.object(tools_admin_api.state, "set_tool_enabled") as set_enabled,
            patch.object(tools_admin_api.state, "record_tool_event") as audit,
            patch.object(tools_admin_api, "_tools_operator_request", return_value=expected) as request,
        ):
            result = tools_admin_api.tool_action_route("whatsapp", "enable", {})
        self.assertEqual(result, expected)
        set_enabled.assert_called_once_with(
            mutation.return_value.__enter__.return_value, "whatsapp", True
        )
        audit.assert_called_once_with("whatsapp", "enablement", "enabled", "")
        request.assert_called_once_with(
            "/operator/tools/whatsapp/service/enable", {}
        )

    def test_disable_is_delegated_to_the_gateway_owner(self) -> None:
        expected = {"tool_id": "whatsapp", "enabled": False}
        with (
            patch.object(tools_admin_api.state, "mutation") as mutation,
            patch.object(tools_admin_api.state, "set_tool_enabled") as set_enabled,
            patch.object(tools_admin_api.state, "record_tool_event") as audit,
            patch.object(tools_admin_api, "_tools_operator_request", return_value=expected) as request,
        ):
            result = tools_admin_api.tool_action_route("whatsapp", "disable", {})
        self.assertEqual(result, expected)
        set_enabled.assert_called_once_with(
            mutation.return_value.__enter__.return_value, "whatsapp", False
        )
        audit.assert_called_once_with("whatsapp", "enablement", "disabled", "")
        request.assert_called_once_with(
            "/operator/tools/whatsapp/service/disable", {}
        )

    def test_enable_stays_committed_when_gateway_start_fails(self) -> None:
        with (
            patch.object(tools_admin_api.state, "mutation") as mutation,
            patch.object(tools_admin_api.state, "set_tool_enabled") as set_enabled,
            patch.object(tools_admin_api.state, "record_tool_event") as audit,
            patch.object(
                tools_admin_api,
                "_tools_operator_request",
                side_effect=tools_admin_api.ApiError(
                    HTTPStatus.BAD_GATEWAY, "gateway unavailable"
                ),
            ) as request,
        ):
            with self.assertRaisesRegex(tools_admin_api.ApiError, "gateway unavailable"):
                tools_admin_api.tool_action_route("whatsapp", "enable", {})
        set_enabled.assert_called_once_with(
            mutation.return_value.__enter__.return_value, "whatsapp", True
        )
        request.assert_called_once_with(
            "/operator/tools/whatsapp/service/enable", {}
        )
        audit.assert_called_once_with("whatsapp", "enablement", "enabled", "")

    def test_disabled_entry_keeps_retained_session_visible(self) -> None:
        suspended = {
            "status": "suspended",
            "connected": False,
            "account": {"id": "447700900000:1@s.whatsapp.net", "label": "Infiverse"},
            "qr_data_url": "",
            "error": "",
        }
        with patch.object(
            tools_admin_api, "_tools_operator_request", return_value=suspended
        ):
            entry = tools_admin_api._tool_entry(WhatsAppTool(), set(), set())
        self.assertEqual(entry["connection_status"], suspended)


class WhatsAppGatewaySuspensionTests(unittest.TestCase):
    def test_gateway_tracks_phone_aliases_for_lid_cached_chats(self) -> None:
        source = (
            Path(__file__).parents[1]
            / "host/tools/whatsapp/gateway.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn('listen("lid-mapping.update"', source)
        self.assertIn("item.key.remoteJidAlt", source)
        self.assertIn("store.aliases[id]", source)
        self.assertIn("function chatIdentifiers(chatId)", source)
        self.assertIn("function trimConversationMessages(chatId)", source)
        self.assertIn("trimConversationMessages(pn)", source)
        self.assertIn("const previousLid = store.aliases[pn]", source)
        self.assertIn("delete store.aliases[previousLid]", source)
        self.assertIn("const previousPn = store.aliases[lid]", source)
        self.assertIn("delete store.aliases[previousPn]", source)
        self.assertIn("const unreadSource = [store.chats[pn], store.chats[lid]]", source)
        self.assertIn("aliasChat.unread_count", source)
        self.assertIn("aliasChat.preview", source)
        self.assertIn("cachedUnread + incomingUnread", source)
        self.assertIn("cachedMessageType(item.message)", source)
        self.assertIn("cachedMessageType(update.message)", source)
        self.assertIn("Number(left.chat_id === jid) - Number(right.chat_id === jid)", source)
        self.assertIn("chats.forEach(chat => upsertChat(chat, true))", source)
        self.assertIn('listen("chats.delete", chatIds => { chatIds.forEach(deleteCachedChat); trimAliases();', source)
        self.assertIn("store.chats[identifier].unread_count = unreadCount", source)
        self.assertIn("store.contacts[identifier] = name", source)
        self.assertIn("store.chats[identifier].name = name", source)
        self.assertIn("for (const identifier of [pn, lid])", source)
        self.assertIn("const messagesById = new Map();", source)
        self.assertIn("message.timestamp > previous.timestamp", source)
        self.assertIn("const retainedMessages = new Set(", source)
        self.assertIn("Number(left.chat_id === chatId) - Number(right.chat_id === chatId)", source)
        self.assertIn("storedIds.has(message.id)", source)
        self.assertIn("if (retainedMessages.size) refreshChatFromMessages(chatId)", source)
        self.assertIn("activeSocket.sendMessage(jid", source)
        self.assertNotIn("activeSocket.sendMessage(match.jid", source)
        self.assertIn(".flatMap(identifier => store.messages[identifier] || [])", source)
        self.assertIn("store.contacts[pn] || store.contacts[lid]", source)
        self.assertIn('name || store.contacts[identifier] || ""', source)
        self.assertIn("delete store.contacts[identifier]", source)
        self.assertIn("if (store.chats[lid]) store.chats[lid].name = name", source)
        self.assertIn("trimConversationMessages(jid)", source)
        self.assertIn("chatIdentifiers(item?.key?.remoteJid)", source)
        self.assertIn("chatIdentifiers(key?.remoteJid)", source)
        self.assertIn("const byConversation = new Map();", source)
        self.assertIn("const conversations = new Map();", source)
        self.assertIn("conversation.identifiers.add(identifier)", source)
        self.assertIn("delete store.chats[id];\n  store.chats[id] = updated;", source)
        self.assertIn("right.order - left.order", source)
        self.assertIn("const contactConversations = new Map();", source)
        self.assertIn("MAX_CHATS - activeContacts.length", source)
        self.assertIn("...pendingContacts.slice(-remainingContacts)", source)
        self.assertIn('listen("lid-mapping.update", mapping => { upsertLidMapping(mapping); trimChats();', source)
        self.assertIn("...pending.slice(-MAX_CHATS * 2)", source)
        self.assertIn(".flatMap(chatId => store.messages[chatId] || [])", source)
        self.assertIn("new Map(messages.map(message => [message.id, message]))", source)
        self.assertLess(
            source.index("(history.lidPnMappings || []).forEach(upsertLidMapping)"),
            source.index("(history.contacts || []).forEach(upsertContact)"),
        )

    def test_gateway_removes_deleted_chats_and_interrupted_logout_data(self) -> None:
        source = (
            Path(__file__).parents[1]
            / "host/tools/whatsapp/gateway.mjs"
        ).read_text(encoding="utf-8")
        self.assertIn('listen("chats.delete"', source)
        self.assertIn("chatIds.forEach(deleteCachedChat)", source)
        delete_start = source.index("function deleteCachedChat")
        delete_end = source.index("function trimAliases")
        delete_chat = source[delete_start:delete_end]
        self.assertNotIn("delete store.contacts[identifier]", delete_chat)
        self.assertIn("} else if (hasRetainedData()) {", source)
        self.assertIn("clearLocalData();", source)
        self.assertIn('status = "error";\n  qrDataUrl = "";', source)
        self.assertIn(
            "WhatsApp send outcome is unknown. Do not retry automatically",
            source,
        )
        self.assertIn("const LINK_QR_TIMEOUT_MS = 30000;", source)
        self.assertIn("const VERSION_FETCH_TIMEOUT_MS = 5000;", source)
        self.assertIn("fetchLatestWaWebVersion,", source)
        self.assertIn("signal: AbortSignal.timeout(VERSION_FETCH_TIMEOUT_MS)", source)
        self.assertIn("version,\n    });", source)
        self.assertIn('["connected to WA", "ws_open"]', source)
        self.assertIn('["not logged in, attempting registration...", "registration"]', source)
        self.assertIn("transport_phase: transportPhase", source)
        self.assertIn("await quiesceConnection();", source)
        self.assertIn(
            "lastError = linkTimeoutMessage(transportPhase);",
            source,
        )
        self.assertIn("WhatsApp's network connection did not open.", source)
        self.assertIn("but its handshake did not finish", source)
        self.assertIn("but it did not provide a pairing QR code", source)


    def test_disconnect_deletes_retained_data_when_gateway_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            auth_dir = state_dir / "auth"
            auth_dir.mkdir()
            (auth_dir / "creds.json").write_text(
                json.dumps(
                    {
                        "registered": True,
                        "me": {
                            "id": CONNECTED["account"]["id"],
                            "name": CONNECTED["account"]["label"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            (state_dir / "messages.json").write_text("{}", encoding="utf-8")
            gateway = WhatsAppGateway(
                script=state_dir / "missing.mjs",
                state_dir=state_dir,
                module_dir=state_dir / "missing-modules",
            )
            result = gateway.disconnect()
            self.assertEqual(result["status"], "disconnected")
            self.assertEqual(result["account"], CONNECTED["account"])
            self.assertFalse(auth_dir.exists())
            self.assertFalse((state_dir / "messages.json").exists())

    def test_disconnect_deletes_partial_auth_without_credentials_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            auth_dir = state_dir / "auth"
            auth_dir.mkdir()
            (auth_dir / "session-key.json").write_text("{}", encoding="utf-8")
            gateway = WhatsAppGateway(
                script=state_dir / "missing.mjs",
                state_dir=state_dir,
                module_dir=state_dir / "missing-modules",
            )

            result = gateway.disconnect()

            self.assertEqual(result["status"], "disconnected")
            self.assertFalse(auth_dir.exists())

    def test_disconnect_deletes_temporary_cache_without_other_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            temporary_cache = state_dir / "messages.json.tmp"
            temporary_cache.write_text('{"messages":{"chat":"private"}}', encoding="utf-8")
            gateway = WhatsAppGateway(
                script=state_dir / "missing.mjs",
                state_dir=state_dir,
                module_dir=state_dir / "missing-modules",
            )

            result = gateway.disconnect()

            self.assertEqual(result["status"], "disconnected")
            self.assertFalse(temporary_cache.exists())

    def test_gateway_start_requires_child_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "gateway.mjs"
            script.write_text("", encoding="utf-8")
            module_dir = root / "node_modules"
            (module_dir / "baileys").mkdir(parents=True)
            (module_dir / "qrcode").mkdir()
            process = MagicMock()
            process.poll.return_value = 1
            process.stdout = io.StringIO("")
            gateway = WhatsAppGateway(
                script=script,
                state_dir=root / "state",
                module_dir=module_dir,
            )
            with patch.object(whatsapp_gateway.subprocess, "Popen", return_value=process):
                with self.assertRaisesRegex(WhatsAppGatewayError, "readiness"):
                    gateway.start()

    def test_unexpected_child_exit_restarts_only_the_gateway(self) -> None:
        gateway = WhatsAppGateway()
        process = MagicMock()
        process.wait.return_value = 17
        gateway._process = process
        with (
            patch.object(whatsapp_gateway.host_errors, "report_warning") as report,
            patch.object(whatsapp_gateway.time, "sleep") as sleep,
            patch.object(whatsapp_gateway.state, "enabled_tool_ids", return_value={"whatsapp"}),
            patch.object(gateway, "_start_locked") as start,
        ):
            gateway._recover_child_after_exit(process)
        process.wait.assert_called_once_with()
        self.assertIsNone(gateway._process)
        report.assert_called_once_with(
            "tools.whatsapp.gateway",
            "WhatsApp gateway child exited unexpectedly.",
            context={"exit_code": 17},
        )
        sleep.assert_called_once_with(whatsapp_gateway.RESTART_DELAY_SECONDS)
        start.assert_called_once_with()

    def test_request_eof_schedules_gateway_recovery(self) -> None:
        gateway = WhatsAppGateway()
        process = MagicMock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO()
        process.poll.return_value = None
        gateway._process = process
        with (
            patch.object(gateway, "_start_locked", return_value=process),
            patch.object(whatsapp_gateway.select, "select", return_value=([process.stdout], [], [])),
            patch.object(whatsapp_gateway.threading, "Thread") as thread,
        ):
            with self.assertRaisesRegex(WhatsAppGatewayError, "stopped unexpectedly"):
                gateway.request("status")
        self.assertIsNone(gateway._process)
        process.terminate.assert_called_once_with()
        thread.assert_called_once_with(
            target=gateway._recover_child_after_exit,
            args=(process, True),
            daemon=True,
        )
        thread.return_value.start.assert_called_once_with()

    def test_old_child_exit_does_not_clear_its_replacement(self) -> None:
        gateway = WhatsAppGateway()
        old_process = MagicMock()
        replacement = MagicMock()
        gateway._process = replacement
        with patch.object(whatsapp_gateway.host_errors, "report_warning") as report:
            gateway._recover_child_after_exit(old_process)
        self.assertIs(gateway._process, replacement)
        report.assert_not_called()

    def test_timed_out_send_reports_an_unknown_outcome(self) -> None:
        gateway = WhatsAppGateway()
        process = MagicMock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO()
        process.poll.return_value = None
        with (
            patch.object(gateway, "_start_locked", return_value=process),
            patch.object(whatsapp_gateway.select, "select", return_value=([], [], [])),
        ):
            with self.assertRaisesRegex(
                WhatsAppGatewayError,
                "outcome is unknown.*Do not retry automatically",
            ):
                gateway.request("send_message", {"recipient": "+447700900123"})

    def test_gateway_registers_child_before_interruptible_readiness_wait(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "gateway.mjs"
            script.write_text("", encoding="utf-8")
            module_dir = root / "node_modules"
            (module_dir / "baileys").mkdir(parents=True)
            (module_dir / "qrcode").mkdir()
            process = MagicMock()
            process.poll.return_value = None
            process.stdout = io.StringIO("")
            gateway = WhatsAppGateway(
                script=script,
                state_dir=root / "state",
                module_dir=module_dir,
            )
            with (
                patch.object(whatsapp_gateway.subprocess, "Popen", return_value=process),
                patch.object(
                    whatsapp_gateway.select,
                    "select",
                    side_effect=SystemExit(0),
                ),
            ):
                with self.assertRaises(SystemExit):
                    gateway.start()
            self.assertIs(gateway._process, process)
            gateway._process = None

    def test_sigterm_unwinds_service_and_gracefully_stops_gateway(self) -> None:
        with (
            patch.object(tools_service.signal, "signal"),
            patch.object(tools_service.tools_host, "recover_interrupted_approvals"),
            patch.object(tools_service.state, "enabled_tool_ids", return_value=set()),
            patch.object(
                tools_service.tools_api,
                "serve_forever",
                side_effect=lambda: tools_service._terminate_on_signal(15, None),
            ),
            patch.object(whatsapp_gateway.GATEWAY, "start") as start,
            patch.object(whatsapp_gateway.GATEWAY, "stop") as stop,
        ):
            with self.assertRaises(SystemExit):
                tools_service.main()
        start.assert_not_called()
        stop.assert_called_once_with()

    def test_service_starts_enabled_adapters(self) -> None:
        with (
            patch.object(tools_service.signal, "signal"),
            patch.object(tools_service.tools_host, "recover_interrupted_approvals"),
            patch.object(
                tools_service.state,
                "enabled_tool_ids",
                return_value={"whatsapp"},
            ),
            patch.object(whatsapp_gateway.GATEWAY, "start") as start,
            patch.object(tools_service.tools_api, "serve_forever"),
            patch.object(whatsapp_gateway.GATEWAY, "stop"),
        ):
            self.assertEqual(tools_service.main(), 0)
        start.assert_called_once_with()

    def test_service_reports_enabled_adapter_start_failure(self) -> None:
        error = WhatsAppGatewayError("WhatsApp gateway failed its startup readiness check.")
        with (
            patch.object(tools_service.signal, "signal"),
            patch.object(tools_service.tools_host, "recover_interrupted_approvals"),
            patch.object(
                tools_service.state,
                "enabled_tool_ids",
                return_value={"whatsapp"},
            ),
            patch.object(whatsapp_gateway.GATEWAY, "start", side_effect=error),
            patch.object(whatsapp_gateway.GATEWAY, "stop"),
            patch.object(tools_service.tools_api, "serve_forever") as serve,
            patch.object(tools_service.host_errors, "report_warning") as report,
        ):
            self.assertEqual(tools_service.main(), 0)
        serve.assert_called_once_with()
        report.assert_called_once_with(
            "tools.service.start",
            error,
            context={"tool_id": "whatsapp"},
            kind="tool_service_start_failed",
        )

    def test_sigterm_during_adapter_start_still_stops_children(self) -> None:
        with (
            patch.object(tools_service.signal, "signal"),
            patch.object(tools_service.tools_host, "recover_interrupted_approvals"),
            patch.object(tools_service.state, "enabled_tool_ids", return_value={"whatsapp"}),
            patch.object(
                whatsapp_gateway.GATEWAY,
                "start",
                side_effect=lambda: tools_service._terminate_on_signal(15, None),
            ),
            patch.object(tools_service.tools_api, "serve_forever") as serve,
            patch.object(whatsapp_gateway.GATEWAY, "stop") as stop,
        ):
            with self.assertRaises(SystemExit):
                tools_service.main()
        serve.assert_not_called()
        stop.assert_called_once_with()

    def test_stop_requests_a_graceful_flush_before_termination(self) -> None:
        gateway = WhatsAppGateway()
        process = MagicMock()
        process.poll.return_value = None
        gateway._process = process
        with (
            patch.object(gateway, "request", return_value={"stopped": True}) as request,
            patch.object(gateway, "_stop_locked") as terminate,
        ):
            gateway.stop()
        request.assert_called_once_with("shutdown", timeout_seconds=2)
        terminate.assert_called_once_with()

    def test_disabled_request_cannot_restart_the_gateway(self) -> None:
        with (
            patch.object(whatsapp_gateway.state, "enabled_tool_ids", return_value=set()),
            patch.object(whatsapp_gateway.GATEWAY, "request") as request,
        ):
            with self.assertRaisesRegex(WhatsAppGatewayError, "disabled"):
                whatsapp_gateway.gateway_request("send_message", {"recipient": "+447700900123"})
        request.assert_not_called()

    def test_disable_stop_is_serialized_after_an_in_flight_request(self) -> None:
        gateway = WhatsAppGateway()
        request_started = threading.Event()
        allow_request_to_finish = threading.Event()
        stop_started = threading.Event()
        stop_finished = threading.Event()

        def request(_method, _params):
            request_started.set()
            allow_request_to_finish.wait(1)
            return {"ok": True}

        def stop() -> None:
            stop_started.set()
            gateway.stop()
            stop_finished.set()

        with patch.object(gateway, "request", side_effect=request):
            request_thread = threading.Thread(
                target=gateway.request_if_enabled,
                args=("status", None, lambda: True),
            )
            stop_thread = threading.Thread(target=stop)
            request_thread.start()
            self.assertTrue(request_started.wait(1))
            stop_thread.start()
            self.assertTrue(stop_started.wait(1))
            self.assertFalse(stop_finished.wait(0.05))
            allow_request_to_finish.set()
            request_thread.join(1)
            stop_thread.join(1)
        self.assertFalse(request_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertTrue(stop_finished.is_set())

    def test_suspended_status_reads_only_registered_account_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            auth_dir = state_dir / "auth"
            auth_dir.mkdir()
            (auth_dir / "creds.json").write_text(
                json.dumps(
                    {
                        "registered": True,
                        "me": {"id": "447700900000:1@s.whatsapp.net", "name": "Infiverse"},
                        "noiseKey": {"private": "must-not-leak"},
                    }
                ),
                encoding="utf-8",
            )
            result = WhatsAppGateway(state_dir=state_dir).suspended_status()
        self.assertEqual(result["status"], "suspended")
        self.assertFalse(result["connected"])
        self.assertTrue(result["retained_data"])
        self.assertEqual(
            result["account"],
            {"id": "447700900000:1@s.whatsapp.net", "label": "Infiverse"},
        )
        self.assertNotIn("noiseKey", result)

    def test_unregistered_credentials_are_not_reported_as_linked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            auth_dir = state_dir / "auth"
            auth_dir.mkdir()
            (auth_dir / "creds.json").write_text(
                json.dumps({"registered": False, "me": {"id": "not-linked"}}),
                encoding="utf-8",
            )
            result = WhatsAppGateway(state_dir=state_dir).suspended_status()
        self.assertEqual(result["status"], "disconnected")
        self.assertTrue(result["retained_data"])
        self.assertIsNone(result["account"])


if __name__ == "__main__":
    unittest.main()
