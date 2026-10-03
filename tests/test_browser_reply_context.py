"""Public embed evidence reaches review without launching the browser."""
from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch

from host.runtime.admin_api import auto_approvals
from host.tools import ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.web import WebRequestError
from host.tools.browser import BUNDLED_TOOL
from host.tools.browser.reply_context import target_tweet
from test_tools import FakeHostAPI


def embed(text="Target &amp; text<br><br>with <a href='https://example.test'>a link</a> 👇", **changes):
    payload = {"url": "https://x.com/author/status/12345",
               "html": '<blockquote class="twitter-tweet"><p lang="en">' + text +
                       '</p>&mdash; Author (@author) <a href="https://x.com/author/status/12345">Date</a></blockquote>'}
    payload.update(changes)
    return payload


class BrowserReplyContextTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeHostAPI()
        self.account = {"accounts": [{"account_id": "acct_" + "a" * 32, "provider": "x",
                                      "provider_identifier": "example", "state": "connected"}]}
        self.proposal = {"account_id": "acct_" + "a" * 32, "text": "A relevant reply", "in_reply_to_tweet_id": "12345"}
        # Mock the transport boundary and exercise the real approval path.
        self.raw = json.dumps(embed(), ensure_ascii=False).encode()
        self.fetch = self.enterContext(patch("host.tools.shared.web._OPENER.open", side_effect=self.http_response))
        self.browser = self.enterContext(patch("host.tools.browser.client.request", return_value=self.account))
        self.launch = self.enterContext(patch("host.runtime.browser.accounts.Profile.launch",
                                              side_effect=AssertionError("No browser before approval")))

    def http_response(self, *args, **kwargs):
        response = io.BytesIO(self.raw)
        response.headers = {"content-type": "application/json"}
        return response

    def request(self, response):
        self.raw = json.dumps(response, ensure_ascii=False).encode()
        return BUNDLED_TOOL.execute("x_post_tweet", self.proposal, self.api)

    def test_reply_captures_provider_text_without_browser_and_reviewer_receives_it(self):
        pending = BUNDLED_TOOL.execute("x_post_tweet", self.proposal, self.api)
        self.assertIsInstance(pending, ActionPendingApproval)
        self.browser.assert_called_once_with("/actions/list")
        self.launch.assert_not_called()
        self.fetch.assert_called_once()
        request = self.fetch.call_args.args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn("Cookie", request.headers)
        self.assertEqual(self.fetch.call_args.kwargs["timeout"], 20)
        url = request.full_url
        self.assertEqual(url, "https://publish.x.com/oembed?url=https://x.com/i/status/12345&omit_script=true")
        saved = self.api.approvals.get(pending.approval_id)
        target = saved.payload["target_tweet"]
        self.assertEqual(target["text"], "Target & text\n\nwith a link 👇")
        self.assertEqual(target["status"], "loaded")
        self.assertEqual(target["author_username"], "author")
        self.assertEqual(target["url"], "https://x.com/author/status/12345")
        self.assertIn("cached", target["content_scope"])
        self.assertNotIn("Date", target["text"])
        self.assertNotIn("<a", target["text"])
        record = {"tool_id": "browser", "action_id": "x_post_tweet", "summary": saved.summary,
                  "payload": saved.payload, "connection_id": "", "account_id": "", "account_label": ""}
        with patch.object(auto_approvals.client, "openai_text_completion",
                          return_value={"approve": False, "reason": "Operator review"}) as judge:
            auto_approvals.review(record, "Only relevant replies")
        request = json.loads(judge.call_args.args[0])["request"]
        self.assertEqual(request["payload"]["target_tweet"], target)
        self.assertIn("untrusted evidence", judge.call_args.kwargs["instructions"])
        self.browser.side_effect = [self.account, {"status": "posted", "url": "https://x.com/example/status/67890"}]
        result = BUNDLED_TOOL.execute_approved(self.api.approvals.approve(pending.approval_id), self.api)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertEqual(self.browser.call_args.args, (
            "/actions/post_tweet", {**self.proposal, "provider_identifier": "example"}))
        self.fetch.assert_called_once()  # Execution uses the frozen evidence.

    def test_original_post_makes_no_public_fetch(self):
        pending = BUNDLED_TOOL.execute("x_post_tweet", {"account_id": self.proposal["account_id"], "text": "Original"}, self.api)
        self.assertIsInstance(pending, ActionPendingApproval)
        self.fetch.assert_not_called()
        self.launch.assert_not_called()
        self.assertNotIn("target_tweet", self.api.approvals.get(pending.approval_id).payload)

    def test_failed_embed_is_explicit_and_has_no_browser_fallback(self):
        self.fetch.side_effect = WebRequestError("private response", status=404)
        pending = BUNDLED_TOOL.execute("x_post_tweet", self.proposal, self.api)
        target = self.api.approvals.get(pending.approval_id).payload["target_tweet"]
        self.assertIn("target tweet content unavailable", pending.summary)
        self.assertEqual(target["status"], "unavailable")
        self.assertNotIn("text", target)
        self.assertNotIn("private response", json.dumps(target))
        self.assertIn("unavailable", target["error"])
        self.launch.assert_not_called()
        self.browser.assert_called_once_with("/actions/list")
        saved = self.api.approvals.get(pending.approval_id)
        record = {"tool_id": "browser", "action_id": "x_post_tweet", "summary": saved.summary,
                  "payload": saved.payload, "connection_id": "", "account_id": "", "account_label": ""}
        with patch.object(auto_approvals.client, "openai_text_completion",
                          return_value={"approve": False, "reason": "Target content unavailable"}) as judge:
            auto_approvals.review(record, "Only relevant replies")
        request = json.loads(judge.call_args.args[0])["request"]
        self.assertIn("target tweet content unavailable", request["summary"])
        self.assertEqual(request["payload"]["target_tweet"]["status"], "unavailable")

    def test_wrong_target_missing_text_and_incomplete_responses_are_unavailable(self):
        for response in [embed(url="https://x.com/author/status/999"),
                         embed(url=12345), embed(html=12345),
                         embed(url="https://evil.example/author/status/12345"),
                         embed(html="<script>unrelated content</script>"),
                         embed(text=""), embed(text="a" * 16001),
                         embed(html='<blockquote class="twitter-tweet"><p>One</p><p>Two</p></blockquote>'),
                         "invalid JSON", [], None]:
            with self.subTest(response=str(response)[:80]):
                pending = self.request(response)
                target = self.api.approvals.get(pending.approval_id).payload["target_tweet"]
                self.assertEqual(target["status"], "unavailable")
                self.assertNotIn("text", target)
        self.launch.assert_not_called()

    def test_caller_cannot_supply_target_content_or_arbitrary_url(self):
        for proposal in [{**self.proposal, "target_tweet": {"text": "Spoofed"}},
                         {**self.proposal, "in_reply_to_tweet_id": "https://evil.example"}]:
            self.assertIsInstance(BUNDLED_TOOL.execute("x_post_tweet", proposal, self.api), ActionFailed)
        self.assertEqual(self.api.approvals.records, {})
        self.fetch.assert_not_called()
        self.browser.assert_not_called()
        with self.assertRaises(ValueError):
            target_tweet("https://evil.example")

    def test_unclosed_or_misnested_target_markup_is_unavailable(self):
        for html in ('<blockquote class="twitter-tweet"><p>partial',
                     '<blockquote class="twitter-tweet"><p>partial</p>',
                     '<blockquote class="twitter-tweet"><p>partial</blockquote>',
                     '<blockquote class="twitter-tweet"><p>partial</blockquote></p>',
                     '<blockquote class="twitter-tweet"><blockquote><p>partial</p></blockquote>',
                     '<blockquote class="twitter-tweet"><blockquote class="twitter-tweet"><p>partial</p></blockquote>'):
            with self.subTest(html=html):
                pending = self.request(embed(html=html))
                self.assertIsInstance(pending, ActionPendingApproval)
                target = self.api.approvals.get(pending.approval_id).payload["target_tweet"]
                self.assertEqual(target["status"], "unavailable")
                self.assertNotIn("text", target)
                self.assertIn("target tweet content unavailable", pending.summary)
        self.launch.assert_not_called()

    def test_valid_numeric_ids_reach_transport_without_free_text_guard(self):
        for tweet_id in ("1234567890", "1234567890123", "1234567890123456", "2105531308642590814", "1" * 25):
            with self.subTest(tweet_id=tweet_id):
                self.proposal["in_reply_to_tweet_id"] = tweet_id
                response = embed(url=f"https://x.com/author/status/{tweet_id}")
                pending = self.request(response)
                self.assertIsInstance(pending, ActionPendingApproval)
                target = self.api.approvals.get(pending.approval_id).payload["target_tweet"]
                self.assertEqual(target["status"], "loaded")
                self.assertEqual(target["id"], tweet_id)
                self.assertEqual(self.fetch.call_args.args[0].full_url,
                    f"https://publish.x.com/oembed?url=https://x.com/i/status/{tweet_id}&omit_script=true")
        self.launch.assert_not_called()

    def test_oversized_or_invalid_json_response_still_queues(self):
        for raw in (b"x" * 100001, b"invalid JSON", b"\xff"):
            self.raw = raw
            pending = BUNDLED_TOOL.execute("x_post_tweet", self.proposal, self.api)
            self.assertIsInstance(pending, ActionPendingApproval)
            self.assertIn("target tweet content unavailable", pending.summary)

    def test_redirects_are_refused_and_still_queue(self):
        from urllib.error import HTTPError
        self.fetch.side_effect = HTTPError("https://publish.x.com/oembed", 302, "Found", {}, io.BytesIO())
        pending = BUNDLED_TOOL.execute("x_post_tweet", self.proposal, self.api)
        self.assertIsInstance(pending, ActionPendingApproval)
        self.assertIn("target tweet content unavailable", pending.summary)
        self.fetch.assert_called_once()
        self.launch.assert_not_called()

    def test_transport_exception_is_unavailable_and_still_queues(self):
        self.fetch.side_effect = ValueError("private transport detail")
        pending = BUNDLED_TOOL.execute("x_post_tweet", self.proposal, self.api)
        self.assertIsInstance(pending, ActionPendingApproval)
        self.assertIn("target tweet content unavailable", pending.summary)
        self.assertNotIn("private transport detail", json.dumps(self.api.approvals.get(pending.approval_id).payload))
        self.launch.assert_not_called()

    def test_unicode_and_json_escaping_leave_room_under_host_payload_limit(self):
        from host.runtime.tools.tools_host import _ensure_json_object, PAYLOAD_MAX_BYTES
        original_request = self.api.approvals.request
        def bounded_request(**kwargs):
            _ensure_json_object(kwargs["payload"], what="Approval payload", max_bytes=PAYLOAD_MAX_BYTES)
            return original_request(**kwargs)
        self.proposal["text"] = "👇" * 280
        for text in ("👇" * 16000, "\x00" * 3000, "a" * 15998):
            with self.subTest(text_size=len(text)), patch.object(self.api.approvals, "request", side_effect=bounded_request):
                pending = self.request(embed(text=text))
                self.assertIsInstance(pending, ActionPendingApproval)
                target = self.api.approvals.get(pending.approval_id).payload["target_tweet"]
                expected = "loaded" if text.startswith("a") else "unavailable"
                self.assertEqual(target["status"], expected)
                if expected == "unavailable":
                    self.assertIn("target tweet content unavailable", pending.summary)

    def test_legacy_approval_and_mismatched_evidence(self):
        pending = BUNDLED_TOOL.execute("x_post_tweet", self.proposal, self.api)
        approved = self.api.approvals.approve(pending.approval_id)
        bad = replace(approved, payload={**approved.payload, "target_tweet": {"id": "999"}})
        self.browser.reset_mock()
        self.assertIsInstance(BUNDLED_TOOL.execute_approved(bad, self.api), ActionFailed)
        self.browser.assert_not_called()
        legacy = replace(approved, payload={key: value for key, value in approved.payload.items() if key != "target_tweet"})
        self.browser.side_effect = [self.account, {"status": "posted", "url": "https://x.com/example/status/67890"}]
        self.assertIsInstance(BUNDLED_TOOL.execute_approved(legacy, self.api), ApprovalExecuted)
