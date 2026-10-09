"""LinkedIn Browser contracts: account isolation, recipient binding and no retry."""
import json
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from host.runtime.browser.accounts import Accounts, Profile
from host.runtime.browser.client import BrowserError
from host.runtime.browser import client
from host.runtime.browser.providers import linkedin
from host.runtime.browser.actions import linkedin_dm as dm
from host.runtime.browser.service import authorized
from host.tools.browser import BUNDLED_TOOL, MANIFEST
from host.tools.results import ActionFailed, ActionPendingApproval, ApprovalExecuted
from browser_fakes import MemoryStore
from test_browser_sessions import FakeBrowser
from test_tools import FakeHostAPI, assert_matches_output_schema

ACCOUNT_ID = "acct_" + "a" * 32
OWNER = "https://www.linkedin.com/in/owner/"
TARGET = "https://www.linkedin.com/in/recipient/"
RECIPIENT = {"profile_url": TARGET, "name": "Recipient", "member_key": "ACoARecipient"}
ACCOUNT = {"account_id": ACCOUNT_ID, "provider": "linkedin", "provider_identifier": OWNER, "state": "connected", "checked_at": "2026-10-08T00:00:00Z"}
READ = {"status": "found", "recipient": RECIPIENT, "messages": [{"text": "hello", "text_truncated": False, "sender_name": "Recipient", "direction": "incoming", "timestamp": "10:00 AM", "has_attachment": False}], "older_messages": "unknown", "may_mark_read": True}


class LinkedInContractTests(unittest.TestCase):
    def test_profile_url_is_a_closed_grammar_and_canonicalized(self):
        self.assertEqual(linkedin.profile_url("https://linkedin.com/in/Some-Person"), "https://www.linkedin.com/in/some-person/")
        for bad in ("http://www.linkedin.com/in/person/", "https://linkedin.com.evil.test/in/person/", "https://user@linkedin.com/in/person/", "https://www.linkedin.com:443/in/person/", "https://www.linkedin.com/in/person/?foo=x", "https://www.linkedin.com/in/person/#x", "https://www.linkedin.com/in/a%2fb/", "https://www.linkedin.com/messaging/", "https://[", TARGET + "\n", None):
            with self.subTest(bad=bad), self.assertRaises(BrowserError):
                linkedin.profile_url(bad)

    def test_request_rejects_limit_conversation_ids_unknown_fields_and_bad_text(self):
        good = {"account_id": ACCOUNT_ID, "recipient_profile_url": TARGET}
        self.assertEqual(dm.validate_request(good), (ACCOUNT_ID, TARGET, ""))
        for extra in ({"limit": 5}, {"conversation_id": "thread"}, {"text": "hello"}, {"account_id": "../secret"}):
            with self.assertRaises(BrowserError):
                dm.validate_request({**good, **extra})
        for text in ("", "  ", "a" * 8001, "x\x00", "x\x7f", "\ud800", 42):
            with self.assertRaises(BrowserError):
                dm.validate_request({**good, "text": text}, send=True)
        text = "contact person@example.com\n" + "x" * 2000 + " 👍🏽"
        self.assertEqual(dm.validate_request({**good, "text": text}, send=True)[2], text)

    def test_unicode_at_character_limit_fits_the_private_request_bound(self):
        text = "🎉" * 8000
        payload = {"account_id": ACCOUNT_ID, "provider_identifier": OWNER,
                   "recipient": RECIPIENT, "text": text}
        response = Mock(status=200)
        response.read.return_value = b'{"status":"sent"}'
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(client, "Connection", return_value=connection):
            self.assertEqual(client.request("/actions/linkedin_send_dm", payload), {"status": "sent"})
        encoded = connection.request.call_args.args[2]
        self.assertIsInstance(encoded, bytes)
        self.assertLess(len(encoded), 65536)
        self.assertEqual(json.loads(encoded)["text"], text)
        connection.close.assert_called_once()

    def test_identity_extraction_ignores_unrelated_profiles_and_ambiguous_receipts(self):
        entity = {"publicIdentifier": "recipient", "entityUrn": "urn:li:fsd_profile:ACoARecipient"}
        self.assertEqual(dm._member_keys({"included": [entity, {**entity, "publicIdentifier": "someone"}]}, "recipient"), {"ACoARecipient"})
        self.assertEqual(dm._member_keys({"included": [{"entityUrn": entity["entityUrn"]}]}, "recipient"), set())
        self.assertEqual(dm._message_id({"value": {"entityUrn": "urn:li:msg_message:fixture-1"}}), "urn:li:msg_message:fixture-1")
        with self.assertRaises(BrowserError):
            dm._message_id({"included": [{"entityUrn": "urn:li:msg_message:a"}, {"entityUrn": "urn:li:msg_message:b"}]})
        with self.assertRaises(BrowserError):
            dm._message_id({"entityUrn": entity["entityUrn"]})

    def test_response_bounds_are_checked_before_materializing_bodies(self):
        response = Mock()
        for headers in ({}, {"content-length": "1000001"},
                        {"content-length": "2", "content-encoding": "gzip"},
                        {"content-length": "2", "content-encoding": "br"},
                        {"content-length": "invalid"}):
            response.reset_mock()
            response.header_value.side_effect = lambda name: headers.get(name)
            with self.assertRaises(BrowserError):
                dm._bounded_response_json(response)
            response.body.assert_not_called()
        response.header_value.side_effect = lambda name: "2" if name == "content-length" else None
        response.body.return_value = b'{}'
        self.assertEqual(dm._bounded_response_json(response), {})

    def test_non_string_public_identifiers_never_name_a_recipient(self):
        for value, slug in ((None, "none"), (True, "true"), (False, "false"), (123, "123")):
            self.assertEqual(dm._member_keys({"publicIdentifier": value,
                "entityUrn": "urn:li:fsd_profile:ACoAOther"}, slug), set())

    def test_new_provider_saves_and_reopens_without_changing_kern_identity(self):
        store = MemoryStore()
        accounts = Accounts(store, FakeBrowser)
        with patch.object(linkedin, "verify_account", return_value=OWNER):
            login = accounts.dispatch("create", {"provider": "linkedin"})
            lease = accounts.dispatch("open", login)["lease"]
            saved = accounts.dispatch("save", {**login, "lease": lease})
            self.assertEqual(saved["provider_identifier"], OWNER)
            restored = Accounts(store, FakeBrowser)
            self.assertEqual(restored.dispatch("list", {})["accounts"], [saved])
            self.assertEqual(restored.dispatch("check", {"account_id": saved["account_id"]})["account_id"], saved["account_id"])
        with patch.object(linkedin, "verify_account", return_value=TARGET):
            lease = restored.dispatch("open", {"account_id": saved["account_id"]})["lease"]
            with self.assertRaisesRegex(BrowserError, "another account"):
                restored.dispatch("save", {"account_id": saved["account_id"], "lease": lease})

    def test_provider_specific_status_filters_accounts(self):
        state = {"accounts": [ACCOUNT, {**ACCOUNT, "account_id": "acct_" + "b" * 32, "provider": "x", "provider_identifier": "example"}]}
        with patch("host.tools.browser.client.request", return_value=state):
            for action, provider in (("linkedin_connection_status", "linkedin"), ("x_connection_status", "x")):
                result = BUNDLED_TOOL.execute(action, {}, FakeHostAPI())
                assert_matches_output_schema(self, MANIFEST, action, result)
                self.assertEqual(len(result.result["accounts"]), 1)
                self.assertEqual(result.result["accounts"][0]["provider"], provider)

    def test_read_has_closed_schema_and_no_approval_or_content_guard(self):
        api = FakeHostAPI()
        with patch("host.tools.browser.client.request", side_effect=[{"accounts": [ACCOUNT]}, READ]) as request, patch.object(api.outbound, "guard_request_parameter_string", side_effect=AssertionError("closed profile grammar")):
            result = BUNDLED_TOOL.execute("linkedin_read_conversation", {"account_id": ACCOUNT_ID, "recipient_profile_url": TARGET}, api)
        assert_matches_output_schema(self, MANIFEST, "linkedin_read_conversation", result)
        self.assertEqual(request.call_args.args, ("/actions/linkedin_read_conversation", {"account_id": ACCOUNT_ID, "provider_identifier": OWNER, "recipient_profile_url": TARGET}))
        self.assertTrue(result.result["may_mark_read"])

    def approval(self):
        api = FakeHostAPI()
        with patch("host.tools.browser.client.request", side_effect=[{"accounts": [ACCOUNT]}, {"recipient": RECIPIENT}]):
            pending = BUNDLED_TOOL.execute("linkedin_send_dm", {"account_id": ACCOUNT_ID, "recipient_profile_url": TARGET, "text": "hello"}, api)
        self.assertIsInstance(pending, ActionPendingApproval)
        record = api.approvals.get(pending.approval_id)
        self.assertEqual(record.payload, {"action": "linkedin_send_dm", "account_id": ACCOUNT_ID, "provider_identifier": OWNER, "recipient": RECIPIENT, "text": "hello"})
        return api, api.approvals.approve(pending.approval_id)

    def test_exact_approval_executes_once_and_requires_receipt(self):
        api, record = self.approval()
        sent = {"status": "sent", "recipient_profile_url": TARGET, "message_id": "urn:li:msg_message:fixture-1"}
        with patch("host.tools.browser.client.request", side_effect=[{"accounts": [ACCOUNT]}, sent]) as request:
            result = BUNDLED_TOOL.execute_approved(record, api)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.args[1]["recipient"], RECIPIENT)
        with patch("host.tools.browser.client.request", side_effect=[{"accounts": [ACCOUNT]}, {"status": "sent", "recipient_profile_url": TARGET}]):
            self.assertIsInstance(BUNDLED_TOOL.execute_approved(record, api), ActionFailed)

    def test_approval_rejects_account_changes_and_uncertain_send_never_retries(self):
        api, record = self.approval()
        for changed in ({**ACCOUNT, "provider_identifier": TARGET}, {**ACCOUNT, "state": "needs_attention"}, {**ACCOUNT, "provider": "x"}):
            with patch("host.tools.browser.client.request", return_value={"accounts": [changed]}) as request:
                self.assertIsInstance(BUNDLED_TOOL.execute_approved(record, api), ActionFailed)
                request.assert_called_once_with("/actions/list")
        with patch("host.tools.browser.client.request", side_effect=[{"accounts": [ACCOUNT]}, BrowserError("Could not confirm sending. Check LinkedIn before approving another attempt.")]) as request:
            self.assertIsInstance(BUNDLED_TOOL.execute_approved(record, api), ActionFailed)
            self.assertEqual(request.call_count, 2)

    def test_missing_or_changed_recipient_does_not_queue_approval(self):
        for resolved in ({}, {"recipient": {**RECIPIENT, "profile_url": OWNER}}):
            with patch("host.tools.browser.client.request", side_effect=[{"accounts": [ACCOUNT]}, resolved]):
                result = BUNDLED_TOOL.execute("linkedin_send_dm", {"account_id": ACCOUNT_ID, "recipient_profile_url": TARGET, "text": "hello"}, FakeHostAPI())
                self.assertIsInstance(result, ActionFailed)

    def test_private_routes_are_tools_only_and_generic_browsing_stays_forbidden(self):
        with patch("host.runtime.browser.service.pwd.getpwnam", side_effect=lambda name: SimpleNamespace(pw_uid=1 if name == "kern-admin" else 2)):
            for operation in ("linkedin_resolve_recipient", "linkedin_read_conversation", "linkedin_send_dm"):
                self.assertEqual(authorized(2, "/actions/" + operation), operation)
                self.assertIsNone(authorized(1, "/actions/" + operation))
            self.assertIsNone(authorized(2, "/actions/evaluate"))
            self.assertIsNone(authorized(2, "/operator/open"))


class LinkedInSubmissionTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.data = {"provider_identifier": OWNER, "state": "connected", "checked_at": "", "usage": {}}
        self.store.save_account(ACCOUNT_ID, "linkedin", self.data, {})
        self.profile = Profile(ACCOUNT_ID, "linkedin", FakeBrowser, self.store, deepcopy(self.data))
        self.body = {"provider_identifier": OWNER, "recipient": RECIPIENT, "text": "hello"}
        self.page = Mock()
        self.enterContext(patch.object(dm, "_page", return_value=(self.page, OWNER)))
        self.resolve = self.enterContext(patch.object(dm, "resolve_recipient", return_value=RECIPIENT))
        self.enterContext(patch.object(dm, "open_conversation", return_value=Mock()))
        self.enterContext(patch.object(dm, "verify_conversation"))
        self.enterContext(patch.object(linkedin, "verify_account", return_value=OWNER))
        self.prepare = self.enterContext(patch.object(dm, "prepare_message", return_value=Mock()))
        self.submit = self.enterContext(patch.object(dm, "submit_message", return_value="urn:li:msg_message:fixture-1"))
        self.enterContext(patch.object(dm, "_report"))

    def test_counts_durable_attempt_before_the_only_send_and_preserves_receipt(self):
        def submit(*args):
            self.assertEqual(self.store.accounts()[ACCOUNT_ID][1]["usage"]["linkedin_send_dm"]["count"], 1)
            return "urn:li:msg_message:fixture-1"
        self.submit.side_effect = submit
        self.assertEqual(dm.send(self.profile, self.body)["status"], "sent")
        self.submit.assert_called_once()

    def test_changed_recipient_blocks_before_composing_or_counting(self):
        self.resolve.return_value = {**RECIPIENT, "member_key": "ACoDifferent"}
        with self.assertRaisesRegex(BrowserError, "recipient changed"):
            dm.send(self.profile, self.body)
        self.prepare.assert_not_called()
        self.submit.assert_not_called()
        self.assertEqual(self.profile.data["usage"], {})

    def test_ambiguous_send_counts_once_and_never_retries(self):
        self.submit.side_effect = RuntimeError("secret provider text")
        with self.assertRaisesRegex(BrowserError, "Check LinkedIn before approving another attempt"):
            dm.send(self.profile, self.body)
        self.submit.assert_called_once()
        self.assertEqual(self.profile.data["usage"]["linkedin_send_dm"]["count"], 1)

    def test_database_failure_and_daily_limit_prevent_submission(self):
        with patch.object(self.profile, "save", side_effect=OSError("database unavailable")):
            with self.assertRaises(OSError):
                dm.send(self.profile, self.body)
        self.submit.assert_not_called()
        day = datetime.now(timezone.utc).date().isoformat()
        self.profile.data["usage"] = {"linkedin_send_dm": {"day": day, "count": 50}}
        with self.assertRaisesRegex(BrowserError, "daily LinkedIn"):
            dm.send(self.profile, self.body)
        self.submit.assert_not_called()
