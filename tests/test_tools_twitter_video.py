"""X native video approval and upload contracts; no live provider calls."""
from __future__ import annotations

import unittest
from unittest.mock import patch
from dataclasses import replace

from host.tools import twitter
from host.tools.results import ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.web import ProviderWarning, WebRequestError
from host.tools.twitter import video
from test_tools_twitter import connected_api, me_response


class XVideoTests(unittest.TestCase):
    def prepare(self, *, data=b"v" * 512, **asset_kwargs):
        api = connected_api()
        asset_id = api.assets.add(data=data, **asset_kwargs)
        with patch.object(twitter, "json_request", return_value=me_response()):
            pending = twitter.XTool().execute("post_tweet", {"text": "hello", "video_asset_id": asset_id}, api)
        self.assertIsInstance(pending, ActionPendingApproval)
        return api, api.approvals.approve(pending.approval_id)

    def test_queues_exact_snapshot_and_no_media_outbound_before_approval(self):
        api = connected_api()
        asset_id = api.assets.add(data=b"v" * 512)
        with patch.object(twitter, "json_request", return_value=me_response()) as request, patch.object(video, "json_request") as media_request, patch.object(video, "request_bytes") as append:
            pending = twitter.XTool().execute("post_tweet", {"text": "hello", "video_asset_id": asset_id}, api)
            self.assertIsInstance(pending, ActionPendingApproval)
            self.assertEqual(request.call_count, 1)
            media_request.assert_not_called()
            append.assert_not_called()
        self.assertIn("video.mp4", pending.summary)
        self.assertIn("512 bytes", pending.summary)
        record = api.approvals.get(pending.approval_id)
        self.assertEqual(record.payload["proposal"]["text"], "hello")
        self.assertEqual(record.payload["proposal"]["video_asset"], video.snapshot(asset_id, api))
        self.assertEqual(record.payload["x_account"]["id"], "111")

    def test_uploads_exact_chunks_then_waits_then_posts(self):
        data = bytes(range(256)) * 20480  # Crosses the real 4 MiB boundary.
        api, approval = self.prepare(data=data)
        events = []
        chunks = []
        status_count = 0

        def media_request(method, url, **kw):
            nonlocal status_count
            self.assertEqual(kw["headers"]["authorization"], "Bearer x-access")
            self.assertGreater(kw["timeout"], 0)
            events.append(url)
            if url.endswith("/initialize"):
                self.assertEqual(kw["body"], {"media_type": "video/mp4", "total_bytes": len(data), "media_category": "tweet_video"})
                return {"data": {"id": "123"}}
            if url.endswith("/finalize"):
                self.assertEqual(len(chunks), 2)
                return {"data": {"id": "123", "processing_info": {"state": "pending", "check_after_secs": 1}}}
            self.assertEqual(url, video.API_BASE + "?command=STATUS&media_id=123")
            status_count += 1
            return {"data": {"processing_info": {"state": "in_progress" if status_count == 1 else "succeeded", "check_after_secs": 1}}}

        def append(method, url, **kw):
            self.assertEqual(url, video.API_BASE + "/123/append")
            boundary = kw["headers"]["content-type"].split("boundary=")[1].encode()
            body = kw["data"]
            self.assertIn(f'\r\n\r\n{len(chunks)}\r\n'.encode(), body)
            chunk = body.split(b"application/octet-stream\r\n\r\n", 1)[1].removesuffix(b"\r\n--" + boundary + b"--\r\n")
            self.assertLessEqual(len(chunk), video.CHUNK_BYTES)
            chunks.append(chunk)
            events.append(url)
            return b"" if len(chunks) == 1 else b'{"data":{"expires_at":1}}'

        def post(method, url, **kw):
            if "/users/me" in url:
                return me_response()
            self.assertEqual(status_count, 2)
            self.assertEqual(url, twitter.X_API_BASE_URL + "/tweets")
            self.assertEqual(kw["body"], {"text": "hello", "media": {"media_ids": ["123"]}})
            events.append(url)
            return {"data": {"id": "777"}}

        with patch.object(twitter, "json_request", side_effect=post), patch.object(video, "json_request", side_effect=media_request), patch.object(video, "request_bytes", side_effect=append), patch.object(video.time, "sleep") as sleep:
            result = twitter.XTool().execute_approved(approval, api)
        self.assertIsInstance(result, ApprovalExecuted)
        self.assertEqual(b"".join(chunks), data)
        self.assertEqual(events[-1], twitter.X_API_BASE_URL + "/tweets")
        self.assertEqual(sleep.call_count, 2)

    def test_immediately_ready_video_keeps_reply_and_quote_targets(self):
        for target_key, expected in (("in_reply_to_tweet_id", {"reply": {"in_reply_to_tweet_id": "55"}}), ("quote_tweet_id", {"quote_tweet_id": "55"}), (None, {})):
            with self.subTest(target=target_key):
                api = connected_api()
                asset_id = api.assets.add(data=b"v" * 512, filename="clip.mov", media_type="video/quicktime")
                proposal = {"text": "caption", "video_asset_id": asset_id}
                if target_key:
                    proposal[target_key] = "55"
                def request(method, url, **kw):
                    if "/users/me" in url:
                        return me_response()
                    if "/tweets/55?" in url:
                        return {"data": {"id": "55", "text": "target"}}
                    self.assertEqual(kw["body"], {"text": "caption", "media": {"media_ids": ["123"]}, **expected})
                    return {"data": {"id": "777"}}
                with patch.object(twitter, "json_request", side_effect=request), patch.object(video, "json_request", side_effect=[{"data": {"id": "123"}}, {"data": {"id": "123"}}]), patch.object(video, "request_bytes", return_value=b""):
                    pending = twitter.XTool().execute("post_tweet", proposal, api)
                    result = twitter.XTool().execute_approved(api.approvals.approve(pending.approval_id), api)
                self.assertIsInstance(result, ApprovalExecuted)

    def test_invalid_missing_expired_or_wrong_type_video_stops_before_identity_read(self):
        for field in (None, 1, "", "missing"):
            with self.subTest(field=field), patch.object(twitter, "json_request") as request:
                result = twitter.XTool().execute("post_tweet", {"text": "hello", "video_asset_id": field}, connected_api())
                self.assertIsInstance(result, ActionFailed)
                request.assert_not_called()
        for mime, data in (("image/jpeg", b"v" * 512), ("audio/mpeg", b"v" * 512), ("video/mp4", b"short")):
            api = connected_api()
            asset_id = api.assets.add(media_type=mime, data=data)
            with patch.object(twitter, "json_request") as request:
                result = twitter.XTool().execute("post_tweet", {"text": "hello", "video_asset_id": asset_id}, api)
            self.assertIsInstance(result, ActionFailed)
            request.assert_not_called()

    def test_existing_connection_keeps_text_posting_but_requires_media_permission(self):
        api = connected_api()
        stored = api.credentials.load()
        stored["account"]["scopes"].remove("media.write")
        stored["secret"]["scope"] = "tweet.read users.read tweet.write offline.access"
        api.credentials.save(stored)
        asset_id = api.assets.add(data=b"v" * 512)
        with patch.object(twitter, "json_request", return_value=me_response()) as request:
            text = twitter.XTool().execute("post_tweet", {"text": "hello"}, api)
            request.reset_mock()
            result = twitter.XTool().execute("post_tweet", {"text": "hello", "video_asset_id": asset_id}, api)
            request.assert_not_called()
        self.assertIsInstance(text, ActionPendingApproval)
        self.assertIsInstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertIn("media.write", result.error)
        self.assertIsNotNone(api.credentials.load())

    def test_changed_expired_or_tampered_bytes_never_start_upload(self):
        for mutation in ("metadata", "bytes", "expired", "scope", "account"):
            with self.subTest(mutation=mutation):
                api, approval = self.prepare()
                asset_id = approval.payload["proposal"]["video_asset"]["asset_id"]
                identity = me_response()
                if mutation == "metadata":
                    api.assets.add(asset_id=asset_id, data=b"changed" * 100)
                elif mutation == "bytes":
                    meta, _ = api.assets.records[asset_id]
                    api.assets.records[asset_id] = (meta, b"x" * 512)
                elif mutation == "expired":
                    api.assets.delete(asset_id)
                elif mutation == "scope":
                    stored = api.credentials.load()
                    stored["account"]["scopes"].remove("media.write")
                    api.credentials.save(stored)
                else:
                    identity = {"data": {"id": "999", "username": "other"}}
                with patch.object(twitter, "json_request", return_value=identity), patch.object(video, "json_request") as request, patch.object(video, "request_bytes") as append:
                    result = twitter.XTool().execute_approved(approval, api)
                self.assertIsInstance(result, ActionFailed)
                request.assert_not_called()
                append.assert_not_called()

    def test_failed_malformed_or_never_ready_processing_does_not_post(self):
        for processing in ({"state": "failed"}, {"state": "unknown"}, {"state": "pending", "check_after_secs": "1"}, {"state": "pending", "check_after_secs": 1000}, {"state": "in_progress", "check_after_secs": 0}, {}, "bad"):
            with self.subTest(processing=processing):
                api, approval = self.prepare()
                def media_request(method, url, **kw):
                    if url.endswith("/initialize"):
                        return {"data": {"id": "123"}}
                    return {"data": {"id": "123", "processing_info": processing}}
                with patch.object(twitter, "json_request", return_value=me_response()) as post, patch.object(video, "json_request", side_effect=media_request) as media, patch.object(video, "request_bytes", return_value=b""), patch.object(video.time, "sleep"):
                    if isinstance(processing, dict) and processing.get("state") == "failed":
                        with self.assertRaises(ProviderWarning):
                            twitter.XTool().execute_approved(approval, api)
                    else:
                        result = twitter.XTool().execute_approved(approval, api)
                        self.assertIsInstance(result, ActionFailed)
                self.assertEqual(post.call_count, 1)  # Identity only.
                self.assertLessEqual(media.call_count, 2 + video.MAX_STATUS_CHECKS)

    def test_invalid_id_upload_errors_and_chunk_failures_stop_without_retry(self):
        for failure in ("id", "init-errors", "append-errors", "append-malformed", "append-http", "finalize-http", "finalize-malformed", "status-malformed", "finalize-wrong-id", "deadline"):
            with self.subTest(failure=failure):
                api, approval = self.prepare()
                def request(method, url, **kw):
                    if url.endswith("/initialize"):
                        if failure == "id":
                            return {"data": {"id": "../bad"}}
                        if failure == "init-errors":
                            return {"data": {"id": "123"}, "errors": [{"detail": "bad"}]}
                        return {"data": {"id": "123"}}
                    if failure == "finalize-http":
                        raise WebRequestError("failed", status=429)
                    if failure == "finalize-malformed":
                        return {"data": {}}
                    if failure == "finalize-wrong-id":
                        return {"data": {"id": "999"}}
                    if url.endswith("/finalize"):
                        return {"data": {"id": "123", "processing_info": {"state": "pending", "check_after_secs": 1}}}
                    return {"data": {}}
                def append(*args, **kw):
                    if failure == "append-http":
                        raise WebRequestError("failed", status=403)
                    return b'{"errors":[{}]}' if failure == "append-errors" else b"bad" if failure == "append-malformed" else b""
                with patch.object(twitter, "json_request", return_value=me_response()) as post, patch.object(video, "json_request", side_effect=request), patch.object(video, "request_bytes", side_effect=append) as chunk, patch.object(video.time, "sleep"), patch.object(video, "UPLOAD_TIMEOUT_SECONDS", 0 if failure == "deadline" else 300):
                    if failure in {"append-http", "finalize-http", "init-errors", "append-errors"}:
                        with self.assertRaises(ProviderWarning):
                            twitter.XTool().execute_approved(approval, api)
                    else:
                        result = twitter.XTool().execute_approved(approval, api)
                        self.assertIsInstance(result, ActionFailed)
                self.assertEqual(post.call_count, 1)
                self.assertLessEqual(chunk.call_count, 1)

    def test_summary_remains_bounded_with_multibyte_text_and_filename(self):
        api, approval = self.prepare(filename="😀" * 50 + ".mp4")
        proposal = approval.payload["proposal"]
        proposal["text"] = "😀" * 4000
        summary = twitter._post_summary(proposal, "@claw", {"id": "99", "text": "😀" * 300})
        self.assertLessEqual(len(summary.encode("utf-8")), twitter.SUMMARY_MAX_BYTES)

    def test_succeeds_on_last_permitted_status_check(self):
        api, approval = self.prepare()
        asset = approval.payload["proposal"]["video_asset"]
        with patch.object(video, "MAX_STATUS_CHECKS", 1), patch.object(video.time, "sleep"), patch.object(video, "request_bytes", return_value=b""), patch.object(video, "json_request", side_effect=[
            {"data": {"id": "123"}},
            {"data": {"id": "123", "processing_info": {"state": "pending", "check_after_secs": 1}}},
            {"data": {"processing_info": {"state": "succeeded"}}},
        ]) as request:
            self.assertEqual(video.upload("x-access", asset, api), "123")
        self.assertEqual(request.call_count, 3)

    def test_oversize_video_rejected_without_upload(self):
        api = connected_api()
        asset_id = api.assets.add(data=b"v" * 512)
        metadata, data = api.assets.records[asset_id]
        api.assets.records[asset_id] = (replace(metadata, size_bytes=video.MAX_VIDEO_BYTES + 1), data)
        with patch.object(twitter, "json_request") as request:
            result = twitter.XTool().execute("post_tweet", {"text": "hello", "video_asset_id": asset_id}, api)
        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()

    def test_upload_permission_lost_during_refresh_stops_before_upload(self):
        api, approval = self.prepare()
        stored = api.credentials.load()
        stored["secret"]["expires_at"] = 1
        api.credentials.save(stored)
        token = {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 7200,
                 "scope": "tweet.read users.read tweet.write offline.access"}
        with patch.object(twitter, "json_request", return_value=token) as request, patch.object(video, "json_request") as upload:
            result = twitter.XTool().execute_approved(approval, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertTrue(result.reconnect_required)
        self.assertEqual(request.call_count, 1)
        upload.assert_not_called()
        self.assertEqual(api.credentials.load()["secret"]["refresh_token"], "new-refresh")

    def test_video_refreshes_token_to_cover_full_operation_budget(self):
        for remaining in (61, 300, 450, 451):
            with self.subTest(remaining=remaining):
                api, approval = self.prepare()
                stored = api.credentials.load()
                stored["secret"]["expires_at"] = 1_800_000_000 + remaining
                api.credentials.save(stored)
                refreshed = remaining <= 450
                expected_token = "new" if refreshed else "x-access"
                def request(method, url, **kw):
                    if url == twitter.X_TOKEN_URL:
                        self.assertTrue(refreshed)
                        return {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 7200,
                                "scope": "tweet.read users.read tweet.write media.write offline.access"}
                    self.assertEqual(kw["headers"]["authorization"], "Bearer " + expected_token)
                    return me_response() if "/users/me" in url else {"data": {"id": "777"}}
                with patch.object(twitter, "now", return_value=1_800_000_000), patch.object(twitter, "json_request", side_effect=request) as requests, patch.object(video, "upload", return_value="123") as upload:
                    result = twitter.XTool().execute_approved(approval, api)
                self.assertIsInstance(result, ApprovalExecuted)
                self.assertEqual(upload.call_args.args[0], expected_token)
                self.assertEqual(requests.call_count, 3 if refreshed else 2)
                if refreshed:
                    self.assertEqual(api.credentials.load()["secret"]["refresh_token"], "new-refresh")

    def test_text_token_keeps_normal_freshness_window(self):
        api = connected_api(expires_at=1_800_000_061)
        with patch.object(twitter, "now", return_value=1_800_000_000), patch.object(twitter, "json_request") as request:
            self.assertEqual(twitter.X_CREDENTIALS.access_token(api, required_scopes=twitter.REQUIRED_X_WRITE_SCOPES), "x-access")
        request.assert_not_called()

    def test_short_refreshed_token_stops_before_any_video_upload(self):
        api, approval = self.prepare()
        stored = api.credentials.load()
        stored["secret"]["expires_at"] = 1
        api.credentials.save(stored)
        token = {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 300,
                 "scope": "tweet.read users.read tweet.write media.write offline.access"}
        with patch.object(twitter, "json_request", return_value=token) as request, patch.object(video, "upload") as upload:
            result = twitter.XTool().execute_approved(approval, api)
        self.assertIsInstance(result, ActionFailed)
        self.assertFalse(result.reconnect_required)
        self.assertEqual(request.call_count, 1)
        upload.assert_not_called()
        self.assertEqual(api.credentials.load()["secret"]["refresh_token"], "new-refresh")

    def test_connection_requests_media_scope(self):
        self.assertIn("media.write", twitter.X_OAUTH_SCOPES)
        url = twitter.XTool().credentials.start_connect({"redirect_uri": "https://example.com/cb"}, connected_api())["authorization_url"]
        self.assertIn("media.write", url)
