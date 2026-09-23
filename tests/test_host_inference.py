"""Dedicated host inference service, bounded adapters, and operator boundary."""

from __future__ import annotations

import json
import http.client
import os
from pathlib import Path
import struct
import tempfile
import unittest
import threading
from typing import Any
from unittest.mock import MagicMock, patch

from host.runtime.admin_api import service as admin_api
from host.runtime.core.state import host_inference as host_inference_state
from host.runtime.host_inference import (
    approval_risk,
    api,
    client,
    json_contract,
    openai,
    provider_http,
    providers,
    redaction,
    typesafe,
    usage,
)
from host.runtime.tools import api as tools_api


SCHEMA = {
    "type": "object",
    "properties": {
        "status_line": {"type": "string", "maxLength": 140},
        "needs_operator": {"type": "boolean"},
    },
    "required": ["status_line", "needs_operator"],
    "additionalProperties": False,
}


class InferenceRedactionTests(unittest.TestCase):
    def test_redacts_long_numbered_tokens_without_an_english_word_list(self) -> None:
        self.assertEqual(
            redaction.redact_text("abcdefghij abcdefghijk development 12345678901"),
            "abcdefghij abcdefghijk development <redacted>",
        )
        self.assertEqual(redaction.redact_text("order-Q7x8Y9z0A1b2"), "<redacted>")
        self.assertEqual(redaction.redact_text("XYZ-PASS=jbsdb749y3hb"), "XYZ-PASS=<redacted>")
        self.assertEqual(redaction.redact_text("api key=short"), "api key=<redacted>")
        self.assertNotIn("hunter2", redaction.redact_text('{"password": "hunter2"}'))
        self.assertEqual(
            redaction.redact_text('{"password": "correct horse battery staple"}'),
            '{"password": "<redacted>"}',
        )
        self.assertEqual(
            redaction.redact_text("password: correct horse battery staple"),
            "password: <redacted>",
        )
        self.assertEqual(
            redaction.redact_text("Authorization: Basic dXNlcjpwYXNz"),
            "Authorization: <redacted>",
        )
        for field in ("DATABASE_PASSWORD", "AWS_SECRET_ACCESS_KEY", "private_key", "credential"):
            with self.subTest(field=field):
                self.assertEqual(redaction.redact_text(f"{field}=development"), f"{field}=<redacted>")

    def test_redacts_short_values_under_credential_keys(self) -> None:
        self.assertEqual(
            redaction.redact_content({"payload": {
                "password": "hunter2", "access_token": "development",
                "accessToken": "development", "clientSecret": "development",
                "title": "development",
            }}),
            {"payload": {
                "password": "<redacted>", "access_token": "<redacted>",
                "accessToken": "<redacted>", "clientSecret": "<redacted>",
                "title": "development",
            }},
        )

    def test_retains_values_when_redacted_object_keys_collide(self) -> None:
        self.assertEqual(
            redaction.redact_content({"user.12345678901": "first", "user.12345678902": "second"}),
            {"user.<redacted>": ["first", "second"]},
        )

    def test_redacts_long_numeric_json_values(self) -> None:
        self.assertEqual(
            redaction.redact_content({"customer_id": 1234567890123456, "count": 42, "enabled": True}),
            {"customer_id": "<redacted>", "count": 42, "enabled": True},
        )


class OpenAITextAdapterTests(unittest.TestCase):
    def test_sends_one_bounded_fixed_endpoint_request_and_validates_result(self) -> None:
        seen = {}

        def transport(method, url, **kwargs):
            seen.update(method=method, url=url, **kwargs)
            return json.dumps({
                "choices": [{"message": {"content": json.dumps({
                    "status_line": "Finished the import.",
                    "needs_operator": False,
                })}}],
            }).encode()

        result = openai.complete(
            api_key="sk-test-secret",
            model="gpt-6-luna",
            prompt="Summarize the completed work.",
            schema_name="swarm_task",
            schema=SCHEMA,
            transport=transport,
        )
        self.assertEqual(result["status_line"], "Finished the import.")
        self.assertEqual((seen["method"], seen["url"]), ("POST", openai.ENDPOINT))
        self.assertEqual(seen["timeout"], openai.TIMEOUT_SECONDS)
        self.assertEqual(seen["max_bytes"], openai.MAX_RESPONSE_BYTES)
        request = json.loads(seen["data"])
        self.assertEqual(request["model"], "gpt-6-luna")
        self.assertEqual(request["reasoning_effort"], "none")
        self.assertEqual(request["response_format"]["json_schema"]["schema"], SCHEMA)
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        self.assertNotIn("sk-test-secret", seen["data"].decode())

    def test_redacts_machine_tokens_and_credentials_before_openai_transport(self) -> None:
        sent = {}
        prompt = "Review the completed deployment abcdefghijk Q7x8Y9z0A1b2 and sk-proj-short. sk-test-secret"

        def transport(_method, _url, **kwargs):
            sent.update(json.loads(kwargs["data"]))
            return b'{"choices":[{"message":{"content":"{\\"status_line\\":\\"Done\\",\\"needs_operator\\":false}"}}]}'

        openai.complete(
            api_key="sk-test-secret", model="gpt-5.6-luna", prompt=prompt,
            schema_name="status", schema=SCHEMA, transport=transport,
        )
        outgoing = sent["messages"][1]["content"]
        self.assertIn("completed", outgoing)
        self.assertEqual(outgoing.count("<redacted>"), 3)
        self.assertIn("abcdefghijk", outgoing)
        for secret in ("Q7x8Y9z0A1b2", "sk-proj-short", "sk-test-secret"):
            self.assertNotIn(secret, outgoing)
        self.assertEqual(prompt.split()[-1], "sk-test-secret")

    def test_rejects_malformed_or_schema_mismatched_responses(self) -> None:
        responses = [
            b"not json",
            b'{"choices":[]}',
            b'{"choices":[{"message":{"content":"{\\"status_line\\":1,\\"needs_operator\\":false}"}}]}',
            b'{"choices":[{"message":{"content":"{\\"status_line\\":\\"ok\\",\\"needs_operator\\":false,\\"extra\\":1}"}}]}',
        ]
        for response in responses:
            with self.subTest(response=response[:40]), self.assertRaises(openai.InferenceResponseError):
                openai.complete(
                    api_key="sk-test",
                    model="gpt-6-luna",
                    prompt="x",
                    schema_name="status",
                    schema=SCHEMA,
                    transport=lambda *_args, **_kwargs: response,
                )

    def test_rejects_oversized_inputs_before_transport(self) -> None:
        called = False

        def transport(*_args, **_kwargs):
            nonlocal called
            called = True
            return b"{}"

        with self.assertRaisesRegex(ValueError, "prompt"):
            openai.complete(
                api_key="sk-test",
                model="gpt-6-luna",
                prompt="x" * (openai.MAX_PROMPT_BYTES + 1),
                schema_name="status",
                schema=SCHEMA,
                transport=transport,
            )
        self.assertFalse(called)

    def test_utf8_prompt_does_not_expand_inside_the_request_envelope(self) -> None:
        prompt = "😀" * (openai.MAX_PROMPT_BYTES // 4)
        seen = {}

        def transport(_method, _url, **kwargs):
            seen.update(kwargs)
            return b'{"choices":[{"message":{"content":"{\\"status_line\\":\\"ok\\",\\"needs_operator\\":false}"}}]}'

        openai.complete(
            api_key="sk-test", model="gpt-6-luna", prompt=prompt,
            schema_name="status", schema=SCHEMA, transport=transport,
        )
        self.assertLess(len(seen["data"]), openai.MAX_REQUEST_BYTES)
        self.assertIn("😀".encode("utf-8"), seen["data"])
        self.assertNotIn(b"\\ud83d", seen["data"])

    def test_rejects_schema_keywords_the_local_validator_does_not_enforce(self) -> None:
        schema = {
            "type": "object",
            "properties": {"score": {"type": "integer", "minimum": 10}},
            "required": ["score"],
            "additionalProperties": False,
        }
        with self.assertRaisesRegex(ValueError, "unsupported keyword minimum"):
            openai.complete(
                api_key="sk-test",
                model="gpt-6-luna",
                prompt="x",
                schema_name="status",
                schema=schema,
                transport=lambda *_args, **_kwargs: b"{}",
            )

    def test_enum_comparison_preserves_json_boolean_and_number_types(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "value": {"type": ["boolean", "integer"], "enum": [1]},
            },
            "required": ["value"],
            "additionalProperties": False,
        }
        response = b'{"choices":[{"message":{"content":"{\\"value\\":true}"}}]}'
        with self.assertRaises(openai.InferenceResponseError):
            openai.complete(
                api_key="sk-test", model="gpt-6-luna", prompt="x",
                schema_name="status", schema=schema,
                transport=lambda *_args, **_kwargs: response,
            )

    def test_integer_schema_accepts_integral_json_numbers_only(self) -> None:
        schema = {"type": "integer"}
        self.assertTrue(json_contract.matches(1, schema))
        self.assertTrue(json_contract.matches(1.0, schema))
        self.assertFalse(json_contract.matches(1.5, schema))
        self.assertFalse(json_contract.matches(True, schema))


class ProviderHttpTests(unittest.TestCase):
    def test_post_uses_standard_https_timeout_and_bounded_read(self) -> None:
        response = MagicMock()
        response.read.return_value = b"{}"
        opened = MagicMock()
        opened.return_value.__enter__.return_value = response
        with patch.object(provider_http._OPENER, "open", opened):
            result = provider_http.post(
                "https://api.example.test/v1/call",
                headers={"Content-Type": "application/json"},
                data=b"{}",
                timeout=2.0,
                max_bytes=64,
                label="Example provider",
            )
        self.assertEqual(result, b"{}")
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.example.test/v1/call")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(opened.call_args.kwargs["timeout"], 2.0)
        response.read.assert_called_once_with(65)

    def test_redirects_are_rejected_before_credentials_can_be_forwarded(self) -> None:
        self.assertTrue(
            any(
                isinstance(handler, provider_http._NoRedirects)
                for handler in provider_http._OPENER.handlers
            )
        )


class HostInferenceUsageTests(unittest.TestCase):
    def test_connection_metadata_does_not_query_usage(self) -> None:
        cursor = MagicMock()
        cursor.fetchone.return_value = (
            "openai", True, {}, "encrypted", "2026-09-22T00:00:00Z",
        )
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        with (
            patch.object(host_inference_state.db, "transaction", return_value=transaction),
            patch.object(
                host_inference_state,
                "host_inference_usage",
                side_effect=AssertionError("configuration metadata must not query usage"),
            ),
        ):
            metadata = host_inference_state.host_inference_provider_metadata("openai")
        self.assertEqual(metadata["provider"], "openai")
        self.assertNotIn("usage", metadata)

    def test_adapters_forward_usage_even_when_feature_content_is_invalid(self) -> None:
        recorded = []
        response = {
            "model": "gpt-6-luna-2026-09-01",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
            "choices": [{"message": {"content": "not json"}}],
        }
        with self.assertRaises(openai.InferenceResponseError):
            openai.complete(
                api_key="sk-test", model="gpt-6-luna", prompt="x",
                schema_name="status", schema=SCHEMA,
                transport=lambda *_args, **_kwargs: json.dumps(response).encode(),
                usage_recorder=lambda model, payload: recorded.append((model, payload)),
            )
        self.assertEqual(recorded, [("gpt-6-luna", response)])

    def test_gpt6_luna_prices_alias_and_snapshot_at_its_own_rate(self) -> None:
        for model in ("gpt-6-luna", "gpt-6-luna-2026-09-22"):
            with self.subTest(model=model), patch.object(usage, "_schedule") as record:
                usage.record_openai_response("gpt-6-luna", {
                    "model": model, "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                             "prompt_tokens_details": {"cached_tokens": 20}},
                })
                self.assertEqual(record.call_args.args[:2], ("openai", "gpt-6-luna"))
                self.assertAlmostEqual(record.call_args.args[3], 0.0000132)

    def test_unsupported_openai_models_are_diagnosed_without_usage_rows(self) -> None:
        for model in (None, "", "gpt-5.6-luna", "gpt-5.6-luna-2026-09-01", "future-model"):
            with (self.subTest(model=model), patch.object(usage, "_schedule") as record,
                  patch.object(usage.host_errors, "report_warning") as diagnostic):
                usage.record_openai_response("gpt-6-luna", {
                    "model": model, "usage": {"prompt_tokens": 12, "completion_tokens": 3},
                })
                record.assert_not_called()
                diagnostic.assert_called_once()

    def test_malformed_cached_token_details_are_not_priced_as_uncached(self) -> None:
        for field in ("prompt_tokens_details", "input_tokens_details"):
            for malformed in (None, [], "missing", {}, {"other": 0}):
                with self.subTest(field=field, malformed=malformed):
                    response = {
                        "model": "gpt-6-luna",
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 2,
                            field: malformed,
                        }
                    }
                    with patch.object(usage, "_schedule") as record:
                        usage.record_openai_response("gpt-6-luna", response)
                    record.assert_called_once_with("openai", "gpt-6-luna", None, None)

    def test_typesafe_usage_uses_published_input_rate(self) -> None:
        response = {
            "model": "jev-latest",
            "usage": {"input_tokens": 1_000, "output_tokens": 4},
        }
        with patch.object(usage, "_schedule") as record:
            usage.record_typesafe_response("jev-latest", response)
        self.assertEqual(record.call_args.args[:2], ("typesafe", "jev"))
        self.assertEqual(record.call_args.args[2], {
            "input_tokens": 1_000, "cached_input_tokens": 0, "output_tokens": 4,
        })
        self.assertAlmostEqual(record.call_args.args[3], 0.000042)

    def test_unsupported_typesafe_models_are_diagnosed_without_usage_rows(self) -> None:
        for model in (None, "", "jev-2.0.0-preview"):
            with (self.subTest(model=model), patch.object(usage, "_schedule") as record,
                  patch.object(usage.host_errors, "report_warning") as diagnostic):
                usage.record_typesafe_response("jev-latest", {
                    "model": model, "usage": {"input_tokens": 1_000, "output_tokens": 4},
                })
                record.assert_not_called()
                diagnostic.assert_called_once()

    def test_usage_write_is_submitted_off_the_inference_response_path(self) -> None:
        submit = MagicMock()
        with patch.object(usage, "_EXECUTOR", submit):
            usage._schedule("typesafe", "jev", None, None)
        submit.submit.assert_called_once()
        # The worker has not run, so its permit remains charged; release it to
        # keep this module-global test fixture balanced.
        usage._WRITE_SLOTS.release()

    def test_usage_pruning_runs_in_the_inference_writer_once_per_day(self) -> None:
        transaction = MagicMock()
        transaction.__enter__.return_value = MagicMock()
        previous = usage._last_prune_day
        usage._last_prune_day = None
        try:
            with (
                patch.object(usage.state, "record_host_inference_usage"),
                patch.object(usage.state, "mutation", return_value=transaction),
                patch.object(usage.state, "prune_host_inference_usage") as prune,
            ):
                usage._save("typesafe", "jev", None, None)
                usage._save("typesafe", "jev", None, None)
            prune.assert_called_once()
        finally:
            usage._last_prune_day = previous

    def test_dropped_usage_write_is_reported_by_the_background_writer(self) -> None:
        previous = usage._dropped_writes
        usage._dropped_writes = 0
        try:
            with patch.object(usage._WRITE_SLOTS, "acquire", return_value=False):
                usage._schedule("typesafe", "jev", None, None)
            with patch.object(usage.host_errors, "report_warning") as warning:
                usage._report_dropped_writes()
            warning.assert_called_once()
            self.assertEqual(warning.call_args.kwargs["context"], {"dropped_records": 1})
        finally:
            usage._dropped_writes = previous



class TypeSafeJevAdapterTests(unittest.TestCase):
    def test_sends_host_defined_approval_questions_unchanged(self) -> None:
        seen = {}

        def transport(_method, _url, **kwargs):
            seen.update(json.loads(kwargs["data"]))
            return json.dumps({
                "model": "jev-latest",
                "answers": {
                    question_id: {"type": "noul", "noul": 0.5}
                    for question_id in approval_risk.APPROVAL_QUESTIONS
                },
            }).encode()

        typesafe.judge(
            api_key="jev-secret", model="jev-latest", state={"summary": "Review it"},
            questions=approval_risk.APPROVAL_QUESTIONS, transport=transport,
        )
        self.assertEqual(seen["model"], "jev-latest")
        self.assertEqual(seen["questions"], approval_risk.APPROVAL_QUESTIONS)

    def test_rejects_unsupported_question_types_before_egress(self) -> None:
        def no_transport(*_args, **_kwargs):
            self.fail("provider transport must not run")

        with self.assertRaisesRegex(ValueError, "noul instructions"):
            typesafe.judge(
                api_key="jev-secret", model="jev-latest", state={},
                questions={"risk": {"type": "choice", "instructions": "Assess risk"}},
                transport=no_transport,
            )

    def test_sends_one_bounded_fixed_endpoint_request(self) -> None:
        seen = {}
        def transport(method, url, **kwargs):
            seen.update(method=method, url=url, **kwargs)
            return json.dumps({
                "model": "jev-latest",
                "answers": {
                    "risk": {
                        "type": "noul",
                        "noul": 0.8,
                    }
                },
            }).encode()

        result = typesafe.judge(
            api_key="jev-secret",
            model="jev-latest",
            state={"action": "read"},
            questions={
                "risk": {
                    "type": "noul",
                    "instructions": "Classify risk.",
                }
            },
            timeout_seconds=1.2,
            transport=transport,
        )
        self.assertEqual(result["answers"]["risk"]["noul"], 0.8)
        self.assertEqual((seen["method"], seen["url"]), ("POST", typesafe.ENDPOINT))
        self.assertEqual(seen["timeout"], 1.2)
        self.assertNotIn("jev-secret", seen["data"].decode())
        self.assertEqual(list(json.loads(seen["data"])["questions"]), ["risk"])

    def test_redacts_nested_state_and_question_text_before_jev_transport(self) -> None:
        seen = {}
        state = {"payload": {
            "machine_id_123": "Review abcdefghijk and clientSecret=short",
            "other_id_456": "Keep the detail",
        }}
        question_id = "flag"

        def transport(_method, _url, **kwargs):
            seen.update(json.loads(kwargs["data"]))
            return b'{"model":"jev-latest","answers":{"flag":{"type":"noul","noul":0.5}}}'

        result = typesafe.judge(
            api_key="jev-secret", model="jev-latest", state=state,
            questions={question_id: {"type": "noul", "instructions": "Review Q7x8Y9z0A1b2"}},
            transport=transport,
        )
        self.assertEqual(
            seen["state"]["payload"]["<redacted>"],
            ["Review abcdefghijk and clientSecret=<redacted>", "Keep the detail"],
        )
        self.assertEqual(seen["questions"]["flag"]["instructions"], "Review <redacted>")
        self.assertNotIn("Q7x8Y9z0A1b2", json.dumps(seen))
        self.assertEqual(result["answers"][question_id]["noul"], 0.5)
        self.assertEqual(state["payload"]["machine_id_123"], "Review abcdefghijk and clientSecret=short")

    def test_preserves_question_definition_for_credential_word_id(self) -> None:
        seen = {}

        def transport(_method, _url, **kwargs):
            seen.update(json.loads(kwargs["data"]))
            return b'{"model":"jev-latest","answers":{"token_risk":{"type":"noul","noul":0.5}}}'

        typesafe.judge(
            api_key="jev-secret", model="jev-latest", state={},
            questions={"token_risk": {"type": "noul", "instructions": "Assess Q7x8Y9z0A1b2"}},
            transport=transport,
        )
        self.assertEqual(seen["questions"], {
            "token_risk": {"type": "noul", "instructions": "Assess <redacted>"},
        })

    def test_preserves_natural_state_and_rejects_oversize_without_clipping(self) -> None:
        body_text = "hello " * 10_000
        seen = {}

        def transport(_method, _url, **kwargs):
            seen["request"] = json.loads(kwargs["data"])
            return json.dumps({
                "model": "jev-1.13.0",
                "answers": {"flag": {"type": "noul", "noul": 0.5}},
            }).encode()

        typesafe.judge(
            api_key="jev-secret",
            model="jev-latest",
            state={"payload": {"body": body_text}},
            questions={"flag": {"type": "noul", "instructions": "Is it risky?"}},
            transport=transport,
        )
        self.assertEqual(seen["request"]["state"]["payload"]["body"], body_text)

        called = False

        def should_not_call(*_args, **_kwargs):
            nonlocal called
            called = True
            return b"{}"

        with self.assertRaisesRegex(ValueError, "too large"):
            typesafe.judge(
                api_key="jev-secret",
                model="jev-latest",
                state={"payload": "hello " * (typesafe.MAX_REQUEST_BYTES // 6 + 1)},
                questions={"flag": {"type": "noul", "instructions": "Is it risky?"}},
                transport=should_not_call,
            )
        self.assertFalse(called)

    def test_only_jev_latest_can_be_requested(self) -> None:
        with self.assertRaisesRegex(ValueError, "jev-latest"):
            typesafe.judge(
                api_key="jev-secret",
                model="jev-1.13.0",
                state={},
                questions={"flag": {"type": "noul", "instructions": "Is it risky?"}},
                transport=lambda *_args, **_kwargs: b"{}",
            )

    def test_socket_client_sizes_jev_state_as_utf8_without_ascii_expansion(self) -> None:
        state = {"payload": "😀" * 12_000}
        response = json.dumps({"result": {"model": "jev-latest", "answers": {}}}).encode()
        connection = MagicMock()
        connection.getresponse.return_value.status = 200
        connection.getresponse.return_value.read.return_value = response
        with patch.object(client, "_HostInferenceConnection", return_value=connection):
            result = client.typesafe_jev_judgment(
                state,
                {"flag": {"type": "noul", "instructions": "Is it risky?"}},
            )
        self.assertEqual(result["model"], "jev-latest")
        sent = connection.request.call_args.kwargs["body"]
        self.assertLess(len(sent), 64 * 1024)
        self.assertIn("😀".encode("utf-8"), sent)

class ConcreteProviderTests(unittest.TestCase):
    def test_disabled_openai_returns_none_without_calling_adapter(self) -> None:
        adapter = MagicMock()
        with patch.object(providers.state, "enabled_host_inference_provider", return_value=None):
            self.assertIsNone(
                providers.openai_text_completion(
                    "prompt", SCHEMA, "status", purpose="swarm_task", adapter=adapter
                )
            )
        adapter.assert_not_called()

    def test_openai_model_is_selected_by_feature_purpose(self) -> None:
        adapter = MagicMock(return_value={"status_line": "done", "needs_operator": False})
        with patch.object(
            providers.state,
            "enabled_host_inference_provider",
            return_value={"provider": "openai", "api_key": "sk-secret", "features": {}},
        ):
            result = providers.openai_text_completion(
                "prompt", SCHEMA, "status", purpose="swarm_task", adapter=adapter
            )
        self.assertEqual(result["status_line"], "done")
        self.assertEqual(
            adapter.call_args.kwargs["model"],
            "gpt-6-luna",
        )

    def test_typesafe_judgment_uses_concrete_adapter(self) -> None:
        adapter = MagicMock(return_value={"model": "jev-latest", "answers": {}})
        with patch.object(
            providers.state,
            "enabled_host_inference_provider",
            return_value={"provider": "typesafe", "api_key": "secret", "features": {}},
        ):
            result = providers.typesafe_jev_judgment(
                {"action": "read"},
                {"risk": {"type": "noul", "instructions": "Assess risk"}},
                timeout_seconds=1.2,
                adapter=adapter,
            )
        self.assertEqual(result["model"], "jev-latest")
        self.assertEqual(adapter.call_args.kwargs["model"], "jev-latest")
        self.assertEqual(adapter.call_args.kwargs["timeout_seconds"], 1.2)


class HostInferenceBoundaryTests(unittest.TestCase):
    def test_service_exposes_only_concrete_provider_actions(self) -> None:
        with patch.object(
            api.providers, "openai_text_completion", return_value={"ok": True}
        ) as complete:
            result = api.dispatch(
                "/openai/text-completion",
                {
                    "prompt": "p",
                    "schema": SCHEMA,
                    "schema_name": "status",
                    "purpose": "swarm_task",
                },
            )
        self.assertEqual(result, {"result": {"ok": True}})
        complete.assert_called_once_with(
            "p", SCHEMA, "status", purpose="swarm_task"
        )
        with self.assertRaisesRegex(ValueError, "invalid TypeSafe"):
            api.dispatch(
                "/typesafe/jev-judgment",
                {
                    "state": {},
                    "questions": {"risk": {"type": "noul", "instructions": "Assess risk"}},
                    "timeout_seconds": 2.1,
                },
            )
        with self.assertRaises(LookupError):
            api.dispatch("/slot/text", {})

    def test_host_socket_accepts_authenticated_host_services(self) -> None:
        with (
            patch.object(api, "peer_uids") as peers,
        ):
            peers.side_effect = lambda user: {
                "kern-admin": frozenset({101}),
                "kern-workspace": frozenset({102}),
                "kern-tools": frozenset({103}),
            }[user]
            self.assertEqual(api.caller_uids(), frozenset({101, 102, 103}))
        root = Path(__file__).parents[1]
        source = (root / "host/runtime/host_inference/api.py").read_text()
        self.assertNotIn("kern-agent", source)
        self.assertNotIn("path_allowed_uids", source)

    def test_socket_enforces_peer_uid_and_serves_concrete_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            socket_path = str(Path(directory) / "host-inference.sock")
            server = api.HostInferenceServer(socket_path, frozenset({os.getuid()}))
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            )
            thread.start()
            try:
                connection = client._HostInferenceConnection(1.0)
                with (
                    patch.object(client, "SOCKET_PATH", socket_path),
                    patch.object(
                        api.providers,
                        "openai_text_completion",
                        return_value={"status_line": "done"},
                    ),
                ):
                    connection.request(
                        "POST",
                        "/openai/text-completion",
                        body=json.dumps(
                            {
                                "prompt": "p",
                                "schema": SCHEMA,
                                "schema_name": "status",
                                "purpose": "swarm_task",
                            }
                        ),
                    )
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())["result"]["status_line"], "done")
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()

            denied_path = str(Path(directory) / "denied.sock")
            denied = api.HostInferenceServer(denied_path, frozenset({-1}))
            denied_thread = threading.Thread(
                target=denied.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            )
            denied_thread.start()
            try:
                connection = client._HostInferenceConnection(1.0)
                with patch.object(client, "SOCKET_PATH", denied_path):
                    with self.assertRaises(
                        (
                            BrokenPipeError,
                            http.client.RemoteDisconnected,
                            ConnectionResetError,
                        )
                    ):
                        connection.request("POST", "/typesafe/jev-judgment", body=b"{}")
                        connection.getresponse()
                    connection.close()
            finally:
                denied.shutdown()
                denied.server_close()

    def test_socket_bounds_connections_before_starting_handler_threads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = api.HostInferenceServer(
                str(Path(directory) / "bounded.sock"), frozenset({os.getuid()})
            )
            request = MagicMock()
            request.getsockopt.return_value = struct.pack(
                "3i", os.getpid(), os.getuid(), os.getgid()
            )
            for _ in range(api.MAX_CONCURRENT_CONNECTIONS):
                self.assertTrue(server._connection_slots.acquire(blocking=False))
            try:
                with patch.object(api.UnixSocketServer, "process_request") as dispatch:
                    server.process_request(request, None)
                dispatch.assert_not_called()
                request.close.assert_called_once()
            finally:
                for _ in range(api.MAX_CONCURRENT_CONNECTIONS):
                    server._connection_slots.release()
                server.server_close()

    def test_workspace_client_is_bounded_and_rejects_invalid_deadlines(self) -> None:
        connection = MagicMock()
        response = MagicMock(status=200)
        response.read.return_value = b'{"result":{"model":"jev-latest","answers":{}}}'
        connection.getresponse.return_value = response
        with patch.object(client, "_HostInferenceConnection", return_value=connection) as connect:
            result = client.typesafe_jev_judgment(
                {"x": 1}, {"risk": {"type": "noul", "instructions": "Assess risk"}}, timeout_seconds=1.2
            )
        self.assertEqual(result["model"], "jev-latest")
        connect.assert_called_once_with(1.2)
        self.assertEqual(
            connection.request.call_args.args[:2], ("POST", "/typesafe/jev-judgment")
        )
        connection.close.assert_called_once()

        with self.assertRaisesRegex(ValueError, "between 0.1 and 2.0"):
            client.typesafe_jev_judgment(
                {"x": 1}, {"risk": {"type": "noul", "instructions": "Assess risk"}}, timeout_seconds=2.1
            )

    def test_client_reports_recursive_serialization_as_an_explicit_error(self) -> None:
        with (
            patch.object(client.json, "dumps", side_effect=RecursionError("nested too deeply")),
            patch.object(client, "_HostInferenceConnection") as connect,
            patch.object(client.host_errors, "report_warning") as warning,
        ):
            with self.assertRaisesRegex(
                client.HostInferenceError, "could not be encoded"
            ):
                client.typesafe_jev_judgment(
                    {"value": 1}, {"risk": {"type": "noul", "instructions": "Assess risk"}}
                )
        connect.assert_not_called()
        warning.assert_called_once()

    def test_client_does_not_duplicate_a_provider_failure_diagnostic(self) -> None:
        connection = MagicMock()
        response = MagicMock(status=200)
        response.read.return_value = b'{"result":null}'
        connection.getresponse.return_value = response
        with (
            patch.object(client, "_HostInferenceConnection", return_value=connection),
            patch.object(client.host_errors, "report_warning") as warning,
        ):
            with self.assertRaisesRegex(
                client.HostInferenceError, "no usable result"
            ):
                client.openai_text_completion(
                    "prompt", {"type": "object"}, "answer", purpose="swarm_task"
                )
        warning.assert_not_called()
        connection.close.assert_called_once()

    def test_configuration_saves_key_without_enabling_and_has_no_model(self) -> None:
        current = {"configured": False, "enabled": False, "features": {}}
        with (
            patch.object(
                admin_api.state, "host_inference_provider_metadata", return_value=current
            ),
            patch.object(
                admin_api.state,
                "configure_host_inference_provider",
                return_value={"configured": True, "enabled": False},
            ) as save,
        ):
            admin_api.replace_host_inference_provider(
                "openai", {"api_key": "sk-test"}
            )
        save.assert_called_once_with(
            "openai", enabled=None, api_key="sk-test", features=None
        )
        with (
            patch.object(
                admin_api.state, "host_inference_provider_metadata", return_value=current
            ),
            self.assertRaisesRegex(admin_api.ApiError, "unsupported fields"),
        ):
            admin_api.replace_host_inference_provider("openai", {"model": "gpt-6-luna"})

    def test_enabling_does_not_require_a_saved_key(self) -> None:
        with (
            patch.object(
                admin_api.state,
                "host_inference_provider_metadata",
                return_value={"configured": False, "enabled": False, "features": {}},
            ),
            patch.object(
                admin_api.state,
                "configure_host_inference_provider",
                return_value={"configured": False, "enabled": True},
            ) as save,
        ):
            response = admin_api.replace_host_inference_provider("openai", {"enabled": True})
        save.assert_called_once_with("openai", enabled=True, api_key=None, features=None)
        self.assertEqual(response["provider"], {"configured": False, "enabled": True})

    def test_provider_feature_settings_are_closed_boolean_metadata(self) -> None:
        current = {
            "configured": True,
            "enabled": True,
            "features": {"recall_reranking": False},
        }
        with (
            patch.object(
                admin_api.state, "host_inference_provider_metadata", return_value=current
            ),
            patch.object(
                admin_api.state,
                "configure_host_inference_provider",
                return_value={},
            ) as save,
        ):
            admin_api.replace_host_inference_provider(
                "typesafe", {"features": {"recall_reranking": True}}
            )
        save.assert_called_once_with(
            "typesafe",
            enabled=None,
            api_key=None,
            features={"recall_reranking": True},
        )

    def test_migration_and_bootstrap_isolate_dedicated_service(self) -> None:
        root = Path(__file__).parents[1]
        migration = (root / "host/migrations/0064_host_inference.sql").read_text()
        self.assertIn(
            'GRANT SELECT ON host_inference_providers TO "kern-host-inference";',
            migration,
        )
        self.assertIn('GRANT SELECT ON secret_keys TO "kern-host-inference";', migration)
        self.assertNotIn('"kern-tools"', migration)
        self.assertNotIn("slot", migration)
        self.assertNotIn("model", migration)
        bootstrap = (root / "host/bootstrap/bootstrap.sh").read_text()
        self.assertIn("User=kern-host-inference", bootstrap)
        self.assertIn("kern-host-inference.service", bootstrap)
        self.assertIn('meta skuid "kern-host-inference" tcp dport 443 accept', bootstrap)
        harness = (root / "tests/pg_harness.py").read_text()
        self.assertIn('"kern-host-inference",', harness)
        self.assertIn("INSERT INTO host_inference_providers", harness)

    def test_approval_feature_grants_no_provider_credentials_to_tools(self) -> None:
        root = Path(__file__).parents[1]
        migration = (root / "host/migrations/0066_tool_approval_risk.sql").read_text()
        self.assertIn(
            'GRANT SELECT (provider, enabled) ON host_inference_providers TO "kern-tools";',
            migration,
        )
        self.assertNotIn("api_key_encrypted", migration)
        self.assertNotIn("secret_keys", migration)
        self.assertNotIn("claim", migration)

    def test_ui_has_two_model_free_cards_and_separate_save_enable_actions(self) -> None:
        root = Path(__file__).parents[1]
        catalog = (
            root / "host/runtime/admin_api/admin_ui/integration_catalog.js"
        ).read_text()
        self.assertIn("host_openai:", catalog)
        self.assertIn("host_typesafe:", catalog)
        self.assertNotIn("defaultModel", catalog)
        self.assertNotIn("infrastructure PR", catalog)
        self.assertNotIn("next PR", catalog)
        self.assertNotIn("Later PRs", catalog)
        network = (root / "host/runtime/admin_api/admin_ui/network.js").read_text()
        self.assertNotIn("host-inference-model-", network)
        self.assertIn("Save API key", network)
        self.assertIn("enable-host-inference-provider", network)
        self.assertNotIn("Egress:", network)

    def test_usage_migration_is_daily_bounded_and_inference_service_owned(self) -> None:
        root = Path(__file__).parents[1]
        migration = (root / "host/migrations/0067_host_inference_usage.sql").read_text()
        self.assertIn("PRIMARY KEY (provider, model, day)", migration)
        self.assertIn("model IN ('gpt-5.6-luna', 'jev', 'other')", migration)
        self.assertIn(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON host_inference_usage TO "kern-host-inference";',
            migration,
        )
        self.assertNotIn("api_key", migration)


if __name__ == "__main__":
    unittest.main()
