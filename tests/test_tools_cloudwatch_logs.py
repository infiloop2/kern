"""CloudWatch Logs scope, bounds, signing, filtering and failure tests."""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from host.tools import cloudwatch_logs as logs
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared.web import WebRequestError
from test_tools import FakeHostAPI, assert_matches_output_schema


ACCESS_KEY = "AKIAEXAMPLEKEY000001"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
APP_GROUP = "/aws/lambda/connectme-checkout"
BILLING_GROUP = "/aws/lambda/shared-billing"


def api(**changes):
    config = {
        "CLOUDWATCH_LOGS_AWS_ACCESS_KEY_ID": ACCESS_KEY,
        "CLOUDWATCH_LOGS_AWS_SECRET_ACCESS_KEY": SECRET_KEY,
        "CLOUDWATCH_LOGS_AWS_REGION": "us-east-1",
        **changes,
    }
    return FakeHostAPI(config=config)


def query(**changes):
    return {
        "log_group": APP_GROUP,
        "start_time": "2026-09-21T10:00:00Z",
        "end_time": "2026-09-21T10:05:00Z",
        **changes,
    }


def response(*, events=None, token=None):
    value = {"events": events or [], "searchedLogStreams": []}
    if token is not None:
        value["nextToken"] = token
    return json.dumps(value).encode()


class CloudWatchLogsTests(unittest.TestCase):
    def execute(self, values=None, provider_response=None):
        with patch.object(logs, "request_bytes", return_value=provider_response or response()) as request:
            result = logs.BUNDLED_TOOL.execute("filter_log_events", values or query(), api())
        return result, request

    def test_manifest_is_one_direct_read_with_write_only_connection_config(self):
        self.assertEqual([action.id for action in logs.MANIFEST.actions], ["filter_log_events"])
        action = logs.MANIFEST.actions[0]
        self.assertEqual(action.approval, "direct")
        self.assertFalse(action.returns_asset)
        self.assertEqual(action.output_schema["properties"]["events"]["maxItems"], logs.MAX_EVENTS)
        self.assertEqual(
            [item.key for item in logs.MANIFEST.config],
            [
                "CLOUDWATCH_LOGS_AWS_ACCESS_KEY_ID",
                "CLOUDWATCH_LOGS_AWS_SECRET_ACCESS_KEY",
                "CLOUDWATCH_LOGS_AWS_REGION",
            ],
        )
        self.assertTrue(any(step.show_config for step in logs.MANIFEST.setup_steps))
        iam_policy = json.loads(logs.MANIFEST.setup_steps[0].code)
        self.assertEqual(iam_policy["Statement"][0]["Action"], "logs:FilterLogEvents")
        self.assertEqual(iam_policy["Statement"][0]["Resource"], "*")
        self.assertNotIn("kms:", " ".join(step.code for step in logs.MANIFEST.setup_steps))
        protections = " ".join(logs.MANIFEST.protections)
        for forbidden in ("writes", "deletes", "retention changes", "unmasking", "deployment operations"):
            self.assertIn(forbidden, protections)

    def test_one_signed_fixed_endpoint_call_and_constructed_filters(self):
        event = {
            "timestamp": 1_789_984_801_123,
            "ingestionTime": 1_789_984_802_456,
            "logStreamName": "2026/09/21/[$LATEST]abc",
            "message": "checkout failed request 123e4567-e89b-12d3-a456-426614174000",
            "eventId": "ignored",
        }
        result, request = self.execute(
            query(
                request_id="123e4567-e89b-12d3-a456-426614174000",
                search_text="checkout failed",
                limit=5,
            ),
            response(events=[event]),
        )
        self.assertIsInstance(result, ActionExecuted)
        assert_matches_output_schema(self, logs.MANIFEST, "filter_log_events", result)
        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", "https://logs.us-east-1.amazonaws.com/"))
        self.assertEqual(kwargs["timeout"], 30)
        self.assertEqual(kwargs["max_bytes"], logs.MAX_PROVIDER_BYTES)
        self.assertGreaterEqual(logs.MAX_PROVIDER_BYTES, 6 * 1_048_576)
        self.assertEqual(kwargs["headers"]["x-amz-target"], logs.TARGET)
        self.assertIn("/us-east-1/logs/aws4_request", kwargs["headers"]["authorization"])
        self.assertNotIn(SECRET_KEY, json.dumps(kwargs["headers"]))
        body = json.loads(kwargs["data"])
        self.assertEqual(
            body,
            {
                "logGroupName": APP_GROUP,
                "startTime": 1_789_984_800_000,
                "endTime": 1_789_985_100_000,
                "limit": 5,
                "filterPattern": '"123e4567-e89b-12d3-a456-426614174000" "checkout failed"',
                "startFromHead": False,
            },
        )
        self.assertNotIn("unmask", body)
        self.assertEqual(result.result["event_count"], 1)
        self.assertEqual(result.result["events"][0]["log_group"], APP_GROUP)
        self.assertEqual(result.result["events"][0]["log_stream"], event["logStreamName"])
        self.assertEqual(result.result["events"][0]["timestamp"], "2026-09-21T10:00:01.123Z")

    def test_any_valid_exact_group_name_is_accepted_and_malformed_names_fail(self):
        invalid = [
            query(log_group="group with spaces"),
            query(log_group="é"),
            query(log_group="g" * 513),
            query(log_group=[APP_GROUP]),
        ]
        with patch.object(logs, "request_bytes") as request:
            for values in invalid:
                with self.subTest(values=values):
                    result = logs.BUNDLED_TOOL.execute("filter_log_events", values, api())
                    self.assertIsInstance(result, ActionFailed)
            bad_configs = (
                {"CLOUDWATCH_LOGS_AWS_REGION": "example.com"},
                {"CLOUDWATCH_LOGS_AWS_ACCESS_KEY_ID": "bad"},
            )
            for changes in bad_configs:
                with self.subTest(changes=changes):
                    result = logs.BUNDLED_TOOL.execute("filter_log_events", query(), api(**changes))
                    self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()

        result, request = self.execute(query(log_group="/aws/lambda/new-service"))
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(json.loads(request.call_args.kwargs["data"])["logGroupName"], "/aws/lambda/new-service")

    def test_missing_config_is_actionable_and_does_not_echo_partial_secrets(self):
        partial = api(CLOUDWATCH_LOGS_AWS_SECRET_ACCESS_KEY="private-partial-secret")
        del partial.config["CLOUDWATCH_LOGS_AWS_REGION"]
        with patch.object(logs, "request_bytes") as request:
            result = logs.BUNDLED_TOOL.execute("filter_log_events", query(), partial)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("Home > Integrations", result.error)
        self.assertNotIn("private-partial-secret", result.error)
        request.assert_not_called()

    def test_time_count_field_and_filter_bounds_fail_before_network(self):
        invalid = [
            query(start_time="2026-09-20T10:00:00Z"),
            query(end_time="2026-09-21T10:00:00Z"),
            query(end_time="2026-09-22T10:00:00.001Z"),
            query(start_time="2026-09-21 10:00:00Z"),
            query(limit=0),
            query(limit=21),
            query(order="sideways"),
            query(search_text='bad "pattern"'),
            query(request_id="space is not an id"),
            query(
                start_time="2023-12-31T23:00:00Z",
                end_time="2023-12-31T23:05:00Z",
                order="newest_first",
            ),
            query(extra="unsupported"),
        ]
        with patch.object(logs, "request_bytes") as request:
            for values in invalid:
                with self.subTest(values=values):
                    self.assertIsInstance(
                        logs.BUNDLED_TOOL.execute("filter_log_events", values, api()),
                        ActionFailed,
                    )
        request.assert_not_called()

    def test_guard_blocks_credentials_in_filters_and_continuation(self):
        with patch.object(logs, "request_bytes") as request:
            for values in (
                query(search_text="api key ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
                query(request_id="ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
                query(order="newest_first", next_token="bearer.ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"),
            ):
                with self.subTest(values=values):
                    self.assertIsInstance(logs.BUNDLED_TOOL.execute("filter_log_events", values, api()), ActionFailed)
        request.assert_not_called()

    def test_continuation_is_explicit_and_does_not_restart_ordering(self):
        first, _ = self.execute(query(order="oldest_first", limit=1), response(token="next-page-token"))
        self.assertEqual(first.result["incomplete_reason"], "more_events")
        self.assertEqual(first.result["next_token"], "next-page-token")
        with patch.object(logs, "request_bytes", return_value=response()) as request:
            second = logs.BUNDLED_TOOL.execute(
                "filter_log_events",
                query(order="oldest_first", limit=1, next_token="next-page-token"),
                api(),
            )
        body = json.loads(request.call_args.kwargs["data"])
        self.assertEqual(body["nextToken"], "next-page-token")
        self.assertNotIn("startFromHead", body)
        self.assertEqual(second.result["incomplete_reason"], "complete")

        with patch.object(logs, "request_bytes") as request:
            missing_order = logs.BUNDLED_TOOL.execute(
                "filter_log_events", query(next_token="next-page-token"), api()
            )
        self.assertIsInstance(missing_order, ActionFailed)
        self.assertIn("repeat the first page", missing_order.error)
        request.assert_not_called()

        with patch.object(logs, "request_bytes", return_value=response(token="same-token")):
            terminal = logs.BUNDLED_TOOL.execute(
                "filter_log_events",
                query(order="oldest_first", next_token="same-token"),
                api(),
            )
        self.assertFalse(terminal.result["incomplete"])
        self.assertEqual(terminal.result["incomplete_reason"], "complete")
        self.assertIsNone(terminal.result["next_token"])

    def test_full_length_continuation_token_and_historical_oldest_order_are_supported(self):
        token = "a" * logs.MAX_NEXT_TOKEN
        with patch.object(logs, "request_bytes", return_value=response()) as request:
            result = logs.BUNDLED_TOOL.execute(
                "filter_log_events",
                query(order="newest_first", next_token=token),
                api(),
            )
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(json.loads(request.call_args.kwargs["data"])["nextToken"], token)

        historical = query(
            start_time="2023-12-31T23:00:00Z",
            end_time="2023-12-31T23:05:00Z",
            order="oldest_first",
        )
        with patch.object(logs, "request_bytes", return_value=response()) as request:
            result = logs.BUNDLED_TOOL.execute("filter_log_events", historical, api())
        self.assertIsInstance(result, ActionExecuted)
        self.assertTrue(json.loads(request.call_args.kwargs["data"])["startFromHead"])

        with patch.object(logs, "request_bytes") as request:
            excessive = logs.BUNDLED_TOOL.execute(
                "filter_log_events",
                query(order="newest_first", next_token=token + "a"),
                api(),
            )
        self.assertIsInstance(excessive, ActionFailed)
        request.assert_not_called()

    def test_empty_partial_page_preserves_next_token(self):
        result, _ = self.execute(provider_response=response(events=[], token="still-more"))
        self.assertEqual(result.result["events"], [])
        self.assertEqual(result.result["event_count"], 0)
        self.assertTrue(result.result["incomplete"])
        self.assertEqual(result.result["next_token"], "still-more")

    def test_messages_are_redacted_clipped_and_whole_result_is_bounded(self):
        events = []
        for index in range(logs.MAX_EVENTS):
            events.append(
                {
                    "timestamp": 1_758_446_401_123 + index,
                    "ingestionTime": 1_758_446_402_456 + index,
                    "logStreamName": "s" * 512,
                    "message": (
                        f"configured={ACCESS_KEY} secret={SECRET_KEY} "
                        "authorization: ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789 "
                        + "😀" * 10_000
                    ),
                }
            )
        result, _ = self.execute(provider_response=response(events=events, token="more"))
        self.assertIsInstance(result, ActionExecuted)
        encoded = json.dumps(result.result).encode()
        self.assertLessEqual(len(encoded), logs.MAX_RESULT_BYTES)
        self.assertNotIn(ACCESS_KEY.encode(), encoded)
        self.assertNotIn(SECRET_KEY.encode(), encoded)
        self.assertNotIn(b"ghp_", encoded)
        self.assertEqual(result.result["incomplete_reason"], "more_events_and_fields_clipped")
        self.assertTrue(all(event["message_truncated"] for event in result.result["events"]))
        self.assertTrue(all(event["message_redacted"] for event in result.result["events"]))
        assert_matches_output_schema(self, logs.MANIFEST, "filter_log_events", result)

    def test_private_key_redaction_handles_complete_and_unterminated_blocks(self):
        complete = "before -----BEGIN PRIVATE KEY-----secret-----END PRIVATE KEY----- after"
        redacted, changed = logs._redact(complete, ACCESS_KEY, SECRET_KEY)
        self.assertTrue(changed)
        self.assertEqual(redacted, "before [redacted] after")

        unterminated = "before " + "-----BEGIN PRIVATE KEY-----" * 10_000 + "secret"
        redacted, changed = logs._redact(unterminated, ACCESS_KEY, SECRET_KEY)
        self.assertTrue(changed)
        self.assertEqual(redacted, "before [redacted]")

    def test_stream_attribution_is_redacted_and_byte_bounded(self):
        event = {
            "timestamp": 1_758_446_401_123,
            "ingestionTime": 1_758_446_402_456,
            "logStreamName": ACCESS_KEY + "秘密" * 246,
            "message": "checkout failed",
        }
        result, _ = self.execute(provider_response=response(events=[event]))
        self.assertIsInstance(result, ActionExecuted)
        returned = result.result["events"][0]
        self.assertNotIn(ACCESS_KEY, returned["log_stream"])
        self.assertTrue(returned["log_stream_redacted"])
        self.assertTrue(returned["log_stream_truncated"])
        self.assertEqual(result.result["incomplete_reason"], "fields_clipped")
        self.assertLessEqual(
            len(json.dumps(returned, ensure_ascii=True, separators=(",", ":")).encode()),
            logs.MAX_EVENT_JSON_BYTES,
        )
        assert_matches_output_schema(self, logs.MANIFEST, "filter_log_events", result)

    def test_malformed_or_excessive_provider_results_fail_closed(self):
        event = {
            "timestamp": 1,
            "ingestionTime": 2,
            "logStreamName": "stream",
            "message": "message",
        }
        invalid = (
            b"not json",
            b"[]",
            json.dumps({"events": {}}).encode(),
            response(events=[event] * (logs.MAX_EVENTS + 1)),
            response(events=[{**event, "timestamp": True}]),
            response(events=[{**event, "message": 1}]),
            response(token=" "),
        )
        for raw in invalid:
            with self.subTest(raw=raw[:40]), patch.object(logs, "request_bytes", return_value=raw):
                result = logs.BUNDLED_TOOL.execute("filter_log_events", query(), api())
                self.assertIsInstance(result, ActionFailed)

    def test_provider_failures_are_actionable_redacted_and_never_retried(self):
        cases = (
            (400, "AccessDeniedException", "logs:FilterLogEvents"),
            (400, "ResourceNotFoundException", "unavailable"),
            (400, "UnrecognizedClientException", "credentials"),
            (400, "InvalidParameterException", "continuation token"),
            (500, "ServiceUnavailableException", "temporarily unavailable"),
        )
        for status, code, marker in cases:
            body = json.dumps({"__type": code, "message": SECRET_KEY}).encode()
            with self.subTest(code=code), patch.object(
                logs,
                "request_bytes",
                side_effect=WebRequestError(SECRET_KEY, status=status, body=body),
            ) as request:
                result = logs.BUNDLED_TOOL.execute("filter_log_events", query(), api())
            self.assertIsInstance(result, ActionFailed)
            self.assertIn(marker, result.error)
            self.assertNotIn(SECRET_KEY, result.error)
            request.assert_called_once()
        with patch.object(
            logs,
            "request_bytes",
            side_effect=WebRequestError(logs.RESPONSE_TOO_LARGE_MESSAGE),
        ) as request:
            result = logs.BUNDLED_TOOL.execute("filter_log_events", query(), api())
        self.assertIn("smaller event limit", result.error)
        request.assert_called_once()

    def test_unknown_action_never_reaches_aws(self):
        with patch.object(logs, "request_bytes") as request:
            result = logs.BUNDLED_TOOL.execute("delete_log_group", {}, api())
        self.assertIsInstance(result, ActionFailed)
        request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
