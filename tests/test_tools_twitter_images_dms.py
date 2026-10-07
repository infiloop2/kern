"""Official X image and private DM contracts, without live posts or messages."""
from __future__ import annotations

import base64
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from host.tools import twitter
from host.tools.twitter import images, dms
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.web import ProviderWarning, WebRequestError
from host.runtime.tools.tools_host import validate_against_schema
from test_tools_twitter import connected_api, me_response


class XImagesTests(unittest.TestCase):
    def asset(self, api, i=0, data=b"image", mime="image/png"):
        return api.assets.add(asset_id=f"image-{i}", filename=f"frame-{i}.png", media_type=mime, data=data)

    def prepare(self, count=1):
        api = connected_api()
        ids = [self.asset(api, i) for i in range(count)]
        with patch.object(twitter, "json_request", return_value=me_response()), patch.object(images, "json_request") as upload:
            pending = twitter.XTool().execute("post_tweet", {"text": "caption", "image_asset_ids": ids}, api)
            self.assertIsInstance(pending, ActionPendingApproval)
            upload.assert_not_called()
        return api, api.approvals.approve(pending.approval_id)

    def test_four_approved_images_upload_in_order_then_publish(self):
        api, approval = self.prepare(4)
        self.assertEqual(approval.payload["x_account"]["id"], "111")
        self.assertEqual(approval.payload["proposal"]["image_assets"], [images.snapshot(f"image-{i}", api) for i in range(4)])
        uploaded = []
        def upload(method, url, **kw):
            self.assertEqual(method, "POST")
            self.assertEqual(url, "https://api.x.com/2/media/upload")
            self.assertEqual(kw["body"], {"media": base64.b64encode(b"image").decode(), "media_category": "tweet_image"})
            self.assertEqual(kw["headers"]["authorization"], "Bearer x-access")
            uploaded.append(str(100 + len(uploaded)))
            return {"data": {"id": uploaded[-1]}}
        def post(method, url, **kw):
            if "/users/me" in url:
                return me_response()
            self.assertEqual(uploaded, ["100", "101", "102", "103"])
            self.assertEqual(kw["body"], {"text": "caption", "media": {"media_ids": uploaded}})
            return {"data": {"id": "777"}}
        with patch.object(images, "json_request", side_effect=upload), patch.object(twitter, "json_request", side_effect=post):
            self.assertIsInstance(twitter.XTool().execute_approved(approval, api), ApprovalExecuted)

    def test_validation_stops_before_identity_or_upload(self):
        api = connected_api()
        good = self.asset(api)
        bad = self.asset(api, 1, mime="video/mp4")
        huge = self.asset(api, 2, data=b"x" * (images.MAX_IMAGE_BYTES + 1))
        for proposal in ({"image_asset_ids": []}, {"image_asset_ids": [good] * 5},
                         {"image_asset_ids": [good, good]}, {"image_asset_ids": "url"},
                         {"image_asset_ids": [bad]}, {"image_asset_ids": [huge]},
                         {"image_asset_ids": [None]}, {"image_asset_ids": [good], "video_asset_id": "video"}):
            with self.subTest(proposal=proposal), patch.object(twitter, "json_request") as request:
                self.assertIsInstance(twitter.XTool().execute("post_tweet", {"text": "caption", **proposal}, api), ActionFailed)
                request.assert_not_called()

    def test_verify_every_digest_before_any_upload(self):
        for change in ("bytes", "metadata", "expired", "scope", "account"):
            with self.subTest(change=change):
                api, approval = self.prepare(2)
                if change == "bytes":
                    meta, _ = api.assets.records["image-1"]
                    api.assets.records["image-1"] = (meta, b"WRONG")
                elif change == "metadata":
                    self.asset(api, 1, data=b"new image")
                elif change == "expired":
                    api.assets.delete("image-1")
                elif change == "scope":
                    stored = api.credentials.load(); stored["account"]["scopes"].remove("media.write"); api.credentials.save(stored)
                with patch.object(twitter, "json_request", return_value={"data": {"id": "999", "username": "other"}} if change == "account" else me_response()), patch.object(images, "json_request") as upload:
                    self.assertIsInstance(twitter.XTool().execute_approved(approval, api), ActionFailed)
                    upload.assert_not_called()

    def test_upload_failures_never_publish_or_retry(self):
        for response in ({}, {"data": {"id": "bad"}}, {"data": {"id": "12", "processing_info": {"state": "pending"}}}, {"errors": [{"detail": "private provider message"}]}):
            api, approval = self.prepare(2)
            with self.subTest(response=response), patch.object(twitter, "json_request", return_value=me_response()) as post, patch.object(images, "json_request", return_value=response) as upload:
                if "errors" in response:
                    with self.assertRaises(ProviderWarning) as warning:
                        twitter.XTool().execute_approved(approval, api)
                    self.assertEqual(warning.exception.operation, "image upload")
                else:
                    self.assertIsInstance(twitter.XTool().execute_approved(approval, api), ActionFailed)
                self.assertEqual(upload.call_count, 1)
                self.assertEqual(post.call_count, 1)

    def test_reply_and_quote_keep_exact_target_with_images(self):
        for target in ("in_reply_to_tweet_id", "quote_tweet_id"):
            api = connected_api(); image = self.asset(api)
            def request(method, url, **kw):
                if "/users/me" in url: return me_response()
                if "/tweets/55?" in url: return {"data": {"id": "55", "text": "target"}}
                expected = {"reply": {"in_reply_to_tweet_id": "55"}} if target.startswith("in_reply") else {"quote_tweet_id": "55"}
                self.assertEqual(kw["body"], {"text": "caption", "media": {"media_ids": ["12"]}, **expected})
                return {"data": {"id": "777"}}
            with patch.object(twitter, "json_request", side_effect=request), patch.object(images, "json_request", return_value={"data": {"id": "12"}}):
                pending = twitter.XTool().execute("post_tweet", {"text": "caption", target: "55", "image_asset_ids": [image]}, api)
                self.assertEqual(api.approvals.get(pending.approval_id).payload["target_tweet"]["id"], "55")
                self.assertIsInstance(twitter.XTool().execute_approved(api.approvals.approve(pending.approval_id), api), ApprovalExecuted)


class XDmsTests(unittest.TestCase):
    def request(self, method, url, **kw):
        if "/users/me" in url: return me_response()
        if "/users/222?" in url: return {"data": {"id": "222", "username": "recipient", "name": "Name"}}
        return {"data": {"dm_event_id": "777", "dm_conversation_id": "111-222"}}

    def prepare(self, target=None):
        api = connected_api()
        with patch.object(twitter, "json_request", side_effect=self.request) as request:
            pending = twitter.XTool().execute("send_dm", {"text": " exact 📩 text \n", **(target or {"recipient_user_id": "222"})}, api)
        self.assertIsInstance(pending, ActionPendingApproval)
        self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))
        return api, api.approvals.approve(pending.approval_id)

    def test_reads_one_page_of_events_with_direction_and_provider_identifiers(self):
        for action, target, path in (("list_dm_events", {}, "/dm_events?"),
                ("read_dm_conversation", {"recipient_user_id": "222"}, "/dm_conversations/with/222/dm_events?"),
                ("read_dm_conversation", {"dm_conversation_id": "111-222"}, "/dm_conversations/111-222/dm_events?")):
            api = connected_api(); seen = []
            def read(method, url, **kw):
                seen.append(url)
                if "/users/me" in url: return me_response()
                self.assertIn(path, url)
                self.assertIn("max_results=3", url)
                self.assertIn("pagination_token=" + "A" * 16, url)
                query = parse_qs(urlsplit(url).query)
                self.assertEqual(query["expansions"], ["sender_id,participant_ids"])
                self.assertNotIn("sender_id", query["dm_event.fields"][0])
                self.assertNotIn("participant_ids", query["dm_event.fields"][0])
                return {"data": [{"id": str(900+i), "sender_id": sender, "dm_conversation_id": "111-222", "text": "text", "participant_ids": ["111", "222"], "event_type": "MessageCreate", "created_at": "2026-10-07T00:00:00Z"} for i, sender in enumerate(("111", "222", ""))], "meta": {"next_token": "B" * 16}}
            with patch.object(twitter, "json_request", side_effect=read):
                result = twitter.XTool().execute(action, {**target, "max_results": 3, "pagination_token": "A" * 16}, api)
            self.assertIsInstance(result, ActionExecuted)
            self.assertEqual(len(seen), 2)
            self.assertEqual([row["direction"] for row in result.result["events"]], ["outgoing", "incoming", "unknown"])
            self.assertEqual(result.result["events"][0]["participant_ids"], ["111", "222"])
            self.assertEqual(result.result["next_token"], "B" * 16)
            self.assertIn("30 days", result.result["coverage"])
            self.assertEqual(validate_against_schema(result.result, twitter.MANIFEST.action(action).output_schema, path="result"), "")
            self.assertEqual([row["amount_usd"] for row in api.costs.records.values()], ["0.010"] * 4)  # identity plus events

    def test_empty_page_is_not_exhaustive_history(self):
        api = connected_api()
        with patch.object(twitter, "json_request", side_effect=[me_response(), {"meta": {"result_count": 0}}]):
            result = twitter.XTool().execute("list_dm_events", {}, api)
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(result.result["events"], [])
        self.assertIsNone(result.result["next_token"])
        self.assertIn("not exhaustive", result.result["coverage"])

    def test_invalid_targets_pages_and_send_payloads_never_reach_x(self):
        for action, proposal in (("read_dm_conversation", {}), ("read_dm_conversation", {"recipient_user_id": "222", "dm_conversation_id": "111-222"}),
                ("read_dm_conversation", {"dm_conversation_id": "../users"}), ("read_dm_conversation", {"dm_conversation_id": "123"}), ("list_dm_events", {"max_results": True}),
                ("list_dm_events", {"max_results": 101}), ("list_dm_events", {"pagination_token": "secret%token"}),
                ("list_dm_events", {"recipient_user_id": "222"}), ("send_dm", {"recipient_user_id": "@name", "text": "hi"}),
                ("send_dm", {"recipient_user_id": "222", "text": " "}), ("send_dm", {"recipient_user_id": "222", "text": "x" * 10001}),
                ("send_dm", {"recipient_user_id": "222", "text": "hi", "media_asset_id": "x"})):
            with self.subTest(action=action, proposal=proposal), patch.object(twitter, "json_request") as request:
                self.assertIsInstance(twitter.XTool().execute(action, proposal, connected_api()), ActionFailed)
                request.assert_not_called()

    def test_scope_checks_keep_old_connections_and_avoid_requests(self):
        for action, scope, proposal in (("list_dm_events", "dm.read", {}), ("send_dm", "dm.write", {"recipient_user_id": "222", "text": "hi"})):
            api = connected_api(); stored = api.credentials.load(); stored["account"]["scopes"].remove(scope); api.credentials.save(stored)
            with patch.object(twitter, "json_request") as request:
                result = twitter.XTool().execute(action, proposal, api)
                self.assertIsInstance(result, ActionFailed); self.assertTrue(result.reconnect_required)
                request.assert_not_called()
            self.assertIsNotNone(api.credentials.load())

    def test_approved_send_binds_exact_sender_target_and_whitespace(self):
        for target, path in (({"recipient_user_id": "222"}, "/with/222/messages"), ({"dm_conversation_id": "111-222"}, "/111-222/messages")):
            api, approval = self.prepare(target)
            self.assertEqual(approval.payload["proposal"], {"text": " exact 📩 text \n", **target})
            self.assertEqual(approval.payload["x_account"]["id"], "111")
            def send(method, url, **kw):
                if method == "GET": return me_response()
                self.assertTrue(url.endswith(path))
                self.assertEqual(kw["body"], {"text": " exact 📩 text \n"})
                return self.request(method, url, **kw)
            with patch.object(twitter, "json_request", side_effect=send) as request:
                result = twitter.XTool().execute_approved(approval, api)
            self.assertIsInstance(result, ApprovalExecuted)
            self.assertIn("dm_event_id 777", result.message)
            self.assertIn("dm_conversation_id 111-222", result.message)
            self.assertEqual(sum(call.args[0] == "POST" for call in request.call_args_list), 1)
            self.assertEqual(list(api.costs.records.values())[-1]["amount_usd"], "0.015")

    def test_approval_account_or_permission_change_cannot_send(self):
        for change in ("account", "scope", "payload"):
            api, approval = self.prepare()
            if change == "scope":
                stored = api.credentials.load(); stored["account"]["scopes"].remove("dm.write"); api.credentials.save(stored)
            if change == "payload": approval.payload["proposal"]["recipient_user_id"] = "../users"
            with patch.object(twitter, "json_request", return_value={"data": {"id": "999", "username": "other"}} if change == "account" else me_response()) as request:
                self.assertIsInstance(twitter.XTool().execute_approved(approval, api), ActionFailed)
                self.assertTrue(all(call.args[0] != "POST" for call in request.call_args_list))

    def test_provider_failures_and_ambiguous_send_have_no_retry(self):
        for response in ({}, {"data": {"dm_event_id": "777"}}, {"data": {"dm_event_id": "777", "dm_conversation_id": "other"}}):
            api, approval = self.prepare()
            with patch.object(twitter, "json_request", side_effect=[me_response(), response]) as request:
                result = twitter.XTool().execute_approved(approval, api)
            self.assertIsInstance(result, ActionFailed); self.assertIn("check history", result.error)
            self.assertEqual(request.call_count, 2)
            self.assertEqual(api.costs.calls[-1], ("0.015", ""))
        for status in (403, 429, 500, None):
            api, approval = self.prepare()
            with patch.object(twitter, "json_request", side_effect=[me_response(), WebRequestError("private", status=status, body=b"private response")]) as request:
                with self.assertRaises(ProviderWarning): twitter.XTool().execute_approved(approval, api)
                self.assertEqual(request.call_count, 2)

    def test_conversation_id_grammar_matches_provider_boundaries(self):
        for value in ("1", "123", "1" * 14, "1" * 20, "1-", "1" * 20 + "-2"):
            with self.subTest(value=value):
                self.assertIsNone(dms.CONVERSATION_ID.fullmatch(value))
        for value in ("1" * 15, "1" * 19, "1-2", "1" * 19 + "-" + "2" * 19):
            with self.subTest(value=value):
                self.assertIsNotNone(dms.CONVERSATION_ID.fullmatch(value))

    def test_large_unicode_dm_summary_is_bounded_without_changing_payload(self):
        api = connected_api()
        text = "📩" * 10000
        with patch.object(twitter, "json_request", side_effect=self.request):
            pending = twitter.XTool().execute("send_dm", {"recipient_user_id": "222", "text": text}, api)
        self.assertIsInstance(pending, ActionPendingApproval)
        self.assertLessEqual(len(pending.summary.encode()), 500)
        self.assertEqual(api.approvals.get(pending.approval_id).payload["proposal"]["text"], text)

    def test_dm_resource_cost_deduplicates_without_assuming_owned_discount(self):
        from host.tools.twitter import costs
        api = connected_api()
        for _ in range(2):
            costs.record_response(api, {"data": [{"id": "123"}]}, "dm_event", owned=True)
        self.assertEqual(len(api.costs.records), 1)
        self.assertEqual(list(api.costs.records.values())[0]["amount_usd"], "0.010")

    def test_partial_or_malformed_read_is_not_silently_complete(self):
        for response in ({}, {"errors": [{"detail": "private"}]}, {"data": "bad"}, {"data": [{"id": "900"}], "meta": {"next_token": "bad"}}):
            with patch.object(twitter, "json_request", side_effect=[me_response(), response]):
                if "errors" in response:
                    with self.assertRaises(ProviderWarning): twitter.XTool().execute("list_dm_events", {}, connected_api())
                else:
                    self.assertIsInstance(twitter.XTool().execute("list_dm_events", {}, connected_api()), ActionFailed)


if __name__ == "__main__":
    unittest.main()
