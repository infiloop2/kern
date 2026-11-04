"""Aurora tool transport, bounds, transaction failures and approval binding."""
from __future__ import annotations

import json
import io
import socket
import ssl
import urllib.error
from email.message import Message
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from host.tools import aurora_postgres as pg
from host.tools.aurora_postgres import client, sql
from host.tools.aurora_postgres import diagnostics
from host.tools.aurora_postgres.setup import BOOTSTRAP_SQL
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.aws_sigv4 import sign_post
from host.tools.shared.web import WebRequestError
from host.tools.shared import web
from test_tools import FakeHostAPI, assert_matches_output_schema


def api():
    values = {"AWS_ACCESS_KEY_ID":"AKIA" + "A" * 16, "AWS_SECRET_ACCESS_KEY":"s" * 40,
        "AWS_REGION":"us-east-1", "CLUSTER_ARN":"arn:aws:rds:us-east-1:123456789012:cluster:agent-store",
        "DATABASE":"agent_store", "SCHEMA":"agent_data"}
    values.update({role.upper()+"_SECRET_ARN":f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{role}-abcdef" for role in ("reader","writer","ddl","dropper")})
    return FakeHostAPI(config={client.PREFIX+k:v for k,v in values.items()})


def records(rows, names=None, kinds=None):
    width = len(rows[0]) if rows else len(names or [])
    return {"columnMetadata":[{"name":(names or ["column"]*width)[i],"typeName":(kinds or ["text"]*width)[i]} for i in range(width)],
        "records":[[{"isNull":True} if v is None else {"stringValue":str(v)} for v in row] for row in rows]}


class FakeAWS:
    def __init__(self):
        self.calls = []
        self.identity = ["100", "r", "false", "[]"]
        self.query_rows = [["9223372036854775807", None]]
        self.inserted = 1
        self.fail_sql = ""
        self.fail_commit = False
        self.wrong_role = False
        self.counts = [["0","0"]]
        self.primary_key = [["id","bigint"]]
        self.deleted = [['{"id": 1, "note": "A dated insight"}']]
        self.objects = {sql.INDEX_TARGET:[["200","false","false","false"]], sql.CONSTRAINT_TARGET:[["300","f","false"]], sql.NOT_NULL_TARGET:[["2","true","false","false","42"]], sql.COLUMN_TARGET:[["2","definition","false","false"]]}

    def __call__(self, method, url, **kwargs):
        body = json.loads(kwargs["data"])
        self.calls.append((url, body, kwargs))
        operation = url.rsplit("/",1)[-1]
        if operation == "BeginTransaction":
            result = {"transactionId":"transaction-1"}
        elif operation in ("CommitTransaction","RollbackTransaction"):
            if operation == "CommitTransaction" and self.fail_commit:
                raise WebRequestError("lost response")
            result = {"transactionStatus":"Transaction Committed"}
        else:
            statement = body["sql"]
            if self.fail_sql and self.fail_sql in statement:
                raise WebRequestError("failed", status=400, body=b'{"__type":"DatabaseErrorException","message":"private row and password"}')
            if statement == "SELECT session_user::text":
                role = body["secretArn"].split(":secret:")[1].split("-")[0]
                result = records([["postgres" if self.wrong_role else "kern_"+role]])
            elif statement == sql.TABLE_TARGET:
                result = records([self.identity])
            elif statement == sql.TABLE_COUNTS:
                result = records(self.counts)
            elif statement == sql.PRIMARY_KEY:
                result = records(self.primary_key)
            elif statement in self.objects:
                result = records(self.objects[statement])
            elif statement.startswith("DELETE"):
                result = records(self.deleted)
            elif statement.startswith("SELECT * FROM ("):
                result = records(self.query_rows, ["id","note"])
            elif statement.startswith("SELECT ") and statement.endswith("LIMIT 0"):
                result = records([], ["id","note"], ["int8","text"])
            elif statement.startswith("INSERT"):
                result = {"numberOfRecordsUpdated":self.inserted}
            else:
                result = records([])
        return json.dumps(result).encode()

    def statements(self):
        return [body["sql"] for _,body,_ in self.calls if "sql" in body]


CREATE = {"operation":"create_table","table":"insights","columns":[
    {"name":"id","type":"bigint","nullable":False}, {"name":"note","type":"text","nullable":True}],"primary_key":["id"]}
INSERT = {"table":"insights","columns":["id","note"],"rows":[["1","A dated insight"]],"on_conflict":"ignore"}


class AuroraTests(unittest.TestCase):
    def setUp(self):
        self.api = api()
        self.aws = FakeAWS()
        self.enterContext(patch.object(client, "request_bytes", side_effect=self.aws))
        self.diagnostics = self.enterContext(patch.object(diagnostics.host_errors, "emit_record"))

    def run_action(self, action, values):
        result = pg.BUNDLED_TOOL.execute(action, values, self.api)
        if isinstance(result, ActionExecuted):
            assert_matches_output_schema(self, pg.MANIFEST, action, result)
        return result

    def test_fresh_smoke_actions_fail_closed_with_missing_configuration(self):
        from tests.smoke.smoke_aws import SMOKE_TOOL_CALLS

        for missing in (None, *client.CONFIG_KEYS):
            host_api = api()
            if missing is None:
                host_api.config.clear()
            else:
                del host_api.config[client.PREFIX + missing]
            for action, values in SMOKE_TOOL_CALLS["aurora_postgres"]:
                with self.subTest(missing=missing, action=action):
                    result = pg.BUNDLED_TOOL.execute(action, values, host_api)
                    self.assertIsInstance(result, ActionFailed)
                    self.assertIn("not set", result.error)
                    self.assertIn("Home > Integrations", result.error)
        self.assertFalse(self.aws.calls)

    def test_query_signed_paths_reader_readonly_bounds_and_precision(self):
        result = self.run_action("query", {"sql":"SELECT id, note FROM insights WHERE id > CAST(:id AS bigint)", "params":[{"name":"id","value":"0"}],"limit":1})
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(result.result["rows"], self.aws.query_rows)
        statements = self.aws.statements()
        self.assertEqual(statements[0], "SET TRANSACTION READ ONLY")
        self.assertIn("LIMIT 2", statements[-1])
        self.assertTrue(self.aws.calls[-1][0].endswith("/RollbackTransaction"))
        for url, body, options in self.aws.calls:
            self.assertTrue(url.startswith("https://rds-data.us-east-1.amazonaws.com/"))
            self.assertIn("/us-east-1/rds-data/aws4_request", options["headers"]["authorization"])
            self.assertTrue(body["secretArn"].endswith("reader-abcdef"))
            self.assertNotIn("schema", body)  # Data API schema parameter unsupported.
            self.assertEqual(options["timeout"], 50)
        execute = self.aws.calls[-2][1]
        self.assertEqual(execute["parameters"], [{"name":"id","value":{"stringValue":"0"}}])
        self.assertEqual(execute["resultSetOptions"], {"decimalReturnType":"STRING","longReturnType":"STRING"})

    def test_wrapped_query_utf8_limit_before_aws(self):
        for value in ("a" * 65486, "é" * 32743):
            with self.subTest(multibyte=value[0] == "é"):
                self.aws.calls.clear()
                statement = "SELECT '" + value + "'"
                result = self.run_action("query", {"sql":statement, "params":[]})
                self.assertIsInstance(result, ActionExecuted)
                self.assertEqual(len(self.aws.calls[-2][1]["sql"].encode("utf-8")), 65536)
                self.aws.calls.clear()
                oversized = {"sql":statement + " ", "params":[]}
                self.assertLess(len(sql.json_bytes(oversized)), 65536)
                result = self.run_action("query", oversized)
                self.assertIsInstance(result, ActionFailed)
                self.assertIn("Wrapped query exceeds AWS's SQL limit", result.error)
                self.assertFalse(self.aws.calls)

    def test_insert_one_atomic_statement_casts_and_reports_skips(self):
        self.aws.inserted = 1
        result = self.run_action("insert_rows", {**INSERT,"rows":[["1","First"],["2",None]]})
        self.assertEqual(result.result, {"inserted":1,"skipped":1})
        inserts = [s for s in self.aws.statements() if s.startswith("INSERT")]
        self.assertEqual(len(inserts),1)
        self.assertIn("CAST(:v0_0 AS bigint)", inserts[0])
        self.assertIn("ON CONFLICT DO NOTHING", inserts[0])
        self.assertTrue(self.aws.calls[-1][0].endswith("/CommitTransaction"))
        self.assertTrue(all(body["secretArn"].endswith("writer-abcdef") for _,body,_ in self.aws.calls))

    def test_private_content_reaches_aws_unchanged_without_parameter_guard(self):
        value = "-----BEGIN PRIVATE KEY-----\npassword: hunter2\n" + "é" * 2561
        statement = "SELECT 'password: hunter2 " + "é" * 2561 + "', :value"
        with patch.object(self.api.outbound, "guard_request_parameter_string", side_effect=AssertionError("Aurora must not scan private content")):
            result = self.run_action("query", {"sql":statement, "params":[{"name":"value","value":value}]})
            self.assertIsInstance(result, ActionExecuted)
            query = self.aws.calls[-2][1]
            self.assertIn(statement, query["sql"])
            self.assertEqual(query["parameters"], [{"name":"value","value":{"stringValue":value}}])
            self.aws.calls.clear()
            result = self.run_action("insert_rows", {**INSERT,"rows":[["1",value]]})
            self.assertIsInstance(result, ActionExecuted)
            insert = self.aws.calls[-2][1]
            self.assertEqual(insert["parameters"][1], {"name":"v0_1","value":{"stringValue":value}})
        for action in pg.MANIFEST.actions:
            self.assertTrue(all(protection.kind == "validated" for protection in action.input_protections.values()))

    def test_insert_failure_rolls_back_without_retries_or_raw_error_data(self):
        self.aws.fail_sql = "INSERT"
        result = self.run_action("insert_rows", INSERT)
        self.assertIsInstance(result, ActionFailed)
        self.assertNotIn("private row", result.error)
        self.assertNotIn("password", result.error)
        self.assertEqual(sum(s.startswith("INSERT") for s in self.aws.statements()), 1)
        self.assertTrue(self.aws.calls[-1][0].endswith("/RollbackTransaction"))
        self.assertFalse(any(url.endswith("/CommitTransaction") for url,_,_ in self.aws.calls))

    def test_lost_commit_is_not_retried(self):
        self.aws.fail_commit = True
        result = self.run_action("insert_rows", INSERT)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("may have succeeded", result.error)
        self.assertEqual(sum(url.endswith("/CommitTransaction") for url,_,_ in self.aws.calls),1)
        self.assertEqual(sum(s.startswith("INSERT") for s in self.aws.statements()),1)
        context = self.diagnostics.call_args.args[0]["context"]
        self.assertEqual(context["phase"], "commit")
        self.assertEqual(context["operation"], "commit")
        self.assertEqual(context["failure_type"], "transport_error")

    def test_aws_http_failure_reaches_host_diagnostics_without_private_content(self):
        request_id = "12345678-1234-5678-9abc-123456789abc"
        headers = Message()
        headers["X-Amzn-RequestId"] = request_id
        headers["Set-Cookie"] = "private-cookie"
        error = urllib.error.HTTPError("https://private-url", 400, "private-reason", headers,
            io.BytesIO(b'{"__type":"com.amazonaws#UnrecognizedClientException","message":"private-row-password"}'))
        with patch.object(client, "request_bytes", web.request_bytes), patch.object(web._OPENER, "open", side_effect=error) as opening:
            result = self.run_action("query", {"sql":"SELECT :secret", "params":[{"name":"secret","value":"private-parameter"}]})
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("Check Host diagnostics", result.error)
        opening.assert_called_once()
        self.diagnostics.assert_called_once()
        record = self.diagnostics.call_args.args[0]
        self.assertEqual(record["component"], "tools.aurora_postgres")
        self.assertEqual(record["severity"], "warning")
        self.assertEqual(record["traceback"], "")
        context = record["context"]
        self.assertEqual(context["operation"], "begin")
        self.assertEqual(context["phase"], "begin")
        self.assertEqual(context["role"], "reader")
        self.assertEqual(context["http_status"], 400)
        self.assertEqual(context["aws_error_code"], "UnrecognizedClientException")
        self.assertEqual(context["aws_request_id"], request_id)
        self.assertEqual(context["failure_type"], "http_error")
        self.assertGreaterEqual(context["elapsed_ms"], 0)
        serialized = json.dumps(record) + result.error
        for forbidden in ("private-", "SELECT", "arn:", "agent_store", "agent_data", "transaction-1", "AKIA", "s" * 40):
            self.assertNotIn(forbidden, serialized)

    def test_execute_failure_phases_and_secondary_rollback_are_retained(self):
        for statement, phase in (("SET LOCAL", "transaction_setup"), ("SELECT session_user", "role_check"), ("INSERT", "statement")):
            with self.subTest(phase=phase):
                self.aws.calls.clear()
                self.diagnostics.reset_mock()
                self.aws.fail_sql = statement
                def fail_rollback(method, url, **kwargs):
                    if url.endswith("/RollbackTransaction"):
                        raise WebRequestError("private cleanup error")
                    return self.aws(method, url, **kwargs)
                with patch.object(client, "request_bytes", side_effect=fail_rollback):
                    result = self.run_action("insert_rows", INSERT)
                self.assertIn("PostgreSQL rejected", result.error)
                contexts = [call.args[0]["context"] for call in self.diagnostics.call_args_list]
                self.assertEqual([c["phase"] for c in contexts], [phase, "rollback"])
                self.assertEqual([c["operation"] for c in contexts], ["execute", "rollback"])
                self.assertEqual(contexts[0]["correlation_id"], contexts[1]["correlation_id"])
                self.assertEqual(contexts[0]["aws_error_code"], "DatabaseErrorException")
                self.assertEqual(contexts[1]["failure_type"], "transport_error")

    def test_transport_categories_do_not_log_exception_messages(self):
        for cause, category in ((TimeoutError("private timeout"), "timeout"),
                (ssl.SSLError("private TLS"), "tls_error"), (socket.gaierror("private DNS"), "dns_error"),
                (ConnectionRefusedError("private connection"), "connection_error"),
                (ValueError("private unknown"), "transport_error")):
            with self.subTest(category=category):
                self.diagnostics.reset_mock()
                error = WebRequestError("private failure")
                error.__cause__ = urllib.error.URLError(cause)
                with patch.object(client, "request_bytes", side_effect=error):
                    self.run_action("query", {"sql":"SELECT 1", "params":[]})
                record = self.diagnostics.call_args.args[0]
                self.assertEqual(record["context"]["failure_type"], category)
                self.assertEqual(record["context"]["http_status"], 0)
                self.assertNotIn("private", json.dumps(record))

    def test_untrusted_error_metadata_and_truncation(self):
        for body, code in ((b'{"code":"private-secret"}', "unrecognized"),
                (b'{"__type":{"password":"private-secret"}}', "unavailable"),
                (b'{"message":"private-secret"', "unavailable"), (b'[]', "unavailable")):
            with self.subTest(body=body):
                self.diagnostics.reset_mock()
                error = WebRequestError("private error", status=500, body=body, body_truncated=True,
                                        request_id="private-secret")
                with patch.object(client, "request_bytes", side_effect=error):
                    self.run_action("query", {"sql":"SELECT 1", "params":[]})
                record = self.diagnostics.call_args.args[0]
                self.assertEqual(record["context"]["aws_error_code"], code)
                self.assertEqual(record["context"]["aws_request_id"], "")
                self.assertTrue(record["context"]["error_body_truncated"])
                self.assertNotIn("private", json.dumps(record))

    def test_invalid_response_missing_transaction_and_role_mismatch_are_diagnosed(self):
        for raw, failure in ((b'private malformed response', "invalid_response"),
                             (b'{}', "missing_transaction_id")):
            with self.subTest(failure=failure):
                self.diagnostics.reset_mock()
                with patch.object(client, "request_bytes", return_value=raw):
                    self.assertIsInstance(self.run_action("query", {"sql":"SELECT 1", "params":[]}), ActionFailed)
                self.assertEqual(self.diagnostics.call_args.args[0]["context"]["failure_type"], failure)
                context = self.diagnostics.call_args.args[0]["context"]
                self.assertEqual("elapsed_ms" in context, failure == "invalid_response")
        self.diagnostics.reset_mock()
        self.aws.wrong_role = True
        self.run_action("query", {"sql":"SELECT 1", "params":[]})
        context = self.diagnostics.call_args.args[0]["context"]
        self.assertEqual(context["failure_type"], "role_mismatch")
        self.assertEqual(context["phase"], "role_check")
        self.assertNotIn("elapsed_ms", context)

    def test_success_does_not_emit_failure_diagnostics(self):
        self.run_action("query", {"sql":"SELECT 1", "params":[]})
        self.diagnostics.assert_not_called()

    def test_error_header_is_allowlisted_and_has_precedence_over_body(self):
        for header, body, expected in (
            ("com.amazon#DatabaseResumingException:private-detail", b'{}', "DatabaseResumingException"),
            ("AccessDeniedException", b'{"code":"DatabaseResumingException"}', "AccessDeniedException"),
            ("PrivateSecretToken", b'{"code":"DatabaseResumingException"}', "unrecognized"),
            ("private=secret", b'{"__type":"com.amazon#InvalidSecretException"}', "InvalidSecretException"),
            ("", b'{"code":"DatabaseResumingException"}', "DatabaseResumingException"),
            ("", b'{"code":"DatabaseResumingException:http://private/"}', "DatabaseResumingException"),
            ("", b'{"__type":"com.amazon#DatabaseResumingException:http://private/"}', "DatabaseResumingException"),
        ):
            with self.subTest(header=header):
                error = WebRequestError("private", status=400, body=body, aws_error_type=header)
                self.assertEqual(diagnostics.aws_error_code(error), expected)
                diagnostics.report_failure(operation="begin", role="reader", phase="begin", correlation_id="local", failure="http_error", exc=error)
                self.assertNotIn("private", json.dumps(self.diagnostics.call_args.args[0]).lower())

    def test_resume_then_original_read_or_write_runs_once(self):
        for action, values in (("query", {"sql":"SELECT 1", "params":[]}), ("insert_rows", INSERT)):
            with self.subTest(action=action):
                self.aws.calls.clear()
                self.diagnostics.reset_mock()
                def resume(method, url, **kwargs):
                    if not self.aws.calls:
                        self.aws.calls.append((url, json.loads(kwargs["data"]), kwargs))
                        raise WebRequestError("private", status=400, aws_error_type="DatabaseResumingException")
                    return self.aws(method, url, **kwargs)
                with patch.object(client, "request_bytes", side_effect=resume), patch.object(client.time, "sleep") as sleep:
                    result = self.run_action(action, values)
                self.assertIsInstance(result, ActionExecuted)
                sleep.assert_called_once_with(2)
                self.assertEqual(sum(url.endswith("/BeginTransaction") for url, _, _ in self.aws.calls), 2)
                statements = self.aws.statements()
                self.assertEqual(sum(s.startswith("INSERT") if action == "insert_rows" else s.startswith("SELECT * FROM (") for s in statements), 1)
                self.assertEqual(sum(url.endswith("/CommitTransaction") for url, _, _ in self.aws.calls), int(action == "insert_rows"))
                context = self.diagnostics.call_args.args[0]["context"]
                self.assertEqual(context["attempt"], 1)
                self.assertEqual(context["retry_outcome"], "will_retry")

    def test_resume_budget_includes_requests_and_waits(self):
        now = [0.0]
        timeouts = []
        def advance(seconds):
            now[0] += seconds
        def resume(*args, **kwargs):
            timeouts.append(kwargs["timeout"])
            advance(min(12, kwargs["timeout"]))
            raise WebRequestError("private", status=400, body=b'{"code":"DatabaseResumingException"}')
        with patch.object(client.time, "monotonic", side_effect=lambda: now[0]), patch.object(client.time, "sleep", side_effect=advance), patch.object(client, "request_bytes", side_effect=resume):
            result = self.run_action("insert_rows", INSERT)
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("startup wait budget was exhausted", result.error)
        self.assertIn("attempted 5 times", result.error)
        self.assertNotIn("not retried", result.error)
        self.assertEqual(timeouts, [50, 46, 32, 18, 4])
        self.assertEqual(now[0], 60)
        self.assertFalse(self.aws.calls)
        contexts = [c.args[0]["context"] for c in self.diagnostics.call_args_list]
        self.assertEqual([c["attempt"] for c in contexts], [1, 2, 3, 4, 5])
        self.assertEqual(contexts[-1]["retry_outcome"], "resume_budget_exhausted")
        self.assertEqual(len({c["correlation_id"] for c in contexts}), 1)

    def test_overslept_resume_wait_does_not_start_request_after_deadline(self):
        now = [0.0]
        def oversleep(seconds):
            now[0] += 65
        error = WebRequestError("private", status=400, aws_error_type="DatabaseResumingException")
        with patch.object(client.time, "monotonic", side_effect=lambda: now[0]), patch.object(client.time, "sleep", side_effect=oversleep), patch.object(client, "request_bytes", side_effect=error) as request:
            result = self.run_action("query", {"sql":"SELECT 1", "params":[]})
        request.assert_called_once()
        self.assertIn("exhausted", result.error)
        self.assertEqual(self.diagnostics.call_args.args[0]["context"]["retry_outcome"], "resume_budget_exhausted")

    def test_late_success_stops_before_sql_and_rolls_back_once(self):
        for failed_cleanup in (False, True):
            with self.subTest(failed_cleanup=failed_cleanup):
                now = [0.0]
                self.aws.calls.clear()
                self.diagnostics.reset_mock()
                def late_response(method, url, **kwargs):
                    response = self.aws(method, url, **kwargs)
                    if url.endswith("/BeginTransaction"):
                        now[0] += 61
                    elif failed_cleanup:
                        raise WebRequestError("private cleanup")
                    return response
                instance = client.Client(client.Config.read(self.api), "writer")
                with patch.object(client.time, "monotonic", side_effect=lambda: now[0]), patch.object(client, "request_bytes", side_effect=late_response):
                    with self.assertRaisesRegex(sql.ToolInputValidationError, "startup wait budget was exhausted"):
                        with instance.transaction(readonly=False):
                            self.fail("Must not enter SQL transaction after startup expires")
                self.assertEqual([url.rsplit("/", 1)[-1] for url, _, _ in self.aws.calls], ["BeginTransaction", "RollbackTransaction"])
                self.assertEqual(instance.transaction_id, "")
                context = self.diagnostics.call_args_list[0].args[0]["context"]
                self.assertEqual(context["failure_type"], "startup_deadline_exceeded")
                self.assertEqual(context["retry_outcome"], "resume_budget_exhausted")

    def test_signing_time_counts_toward_startup_deadline(self):
        now = [0.0]
        sign = client.sign_post
        def slow_sign(**kwargs):
            now[0] += 61
            return sign(**kwargs)
        with patch.object(client.time, "monotonic", side_effect=lambda: now[0]), patch.object(client, "sign_post", side_effect=slow_sign), patch.object(client, "request_bytes") as request:
            result = self.run_action("query", {"sql":"SELECT 1", "params":[]})
        request.assert_not_called()
        self.assertIn("startup wait budget was exhausted", result.error)
        self.assertEqual(self.diagnostics.call_args.args[0]["context"]["attempt"], 0)

    def test_malformed_success_after_body_resume_retains_final_attempt(self):
        for body in (b'{"code":"DatabaseResumingException:http://internal/"}',
                     b'{"__type":"com.amazon#DatabaseResumingException:http://internal/"}'):
            with self.subTest(body=body):
                self.diagnostics.reset_mock()
                with patch.object(client, "request_bytes", side_effect=[WebRequestError("private", status=400, body=body), b'private malformed response']), patch.object(client.time, "sleep"):
                    result = self.run_action("query", {"sql":"SELECT 1", "params":[]})
                self.assertIn("invalid response", result.error)
                contexts = [c.args[0]["context"] for c in self.diagnostics.call_args_list]
                self.assertEqual([c["attempt"] for c in contexts], [1, 2])
                self.assertEqual(contexts[-1]["failure_type"], "invalid_response")
                self.assertNotIn("private", json.dumps(contexts))

    def test_never_retry_other_errors_or_operations_even_if_resuming(self):
        cases = [("begin", "", WebRequestError("private", status=status, aws_error_type=code))
                 for status, code in ((400, "DatabaseUnavailableException"), (403, "DatabaseResumingException"),
                                      (500, "DatabaseResumingException"), (0, "DatabaseResumingException"),
                                      (400, "PrivateToken"), (429, "ThrottlingException"), (400, "AccessDeniedException"))]
        cases += [(operation, txn, WebRequestError("private", status=400, aws_error_type="DatabaseResumingException"))
                  for operation, txn in (("execute", ""), ("execute", "active"), ("commit", "active"), ("rollback", "active"), ("begin", "active"))]
        cases += [("begin", "", WebRequestError("private transport")),
                  ("begin", "", WebRequestError("private", status=400, body=b'{"message":"DatabaseResumingException"}'))]
        for operation, txn, error in cases:
            with self.subTest(operation=operation, txn=txn, status=error.status, code=error.aws_error_type):
                instance = client.Client(client.Config.read(self.api), "writer")
                instance.transaction_id = txn
                with patch.object(client, "request_bytes", side_effect=error) as request, patch.object(client.time, "sleep") as sleep:
                    with self.assertRaises(sql.ToolInputValidationError):
                        instance.request(operation, {})
                request.assert_called_once()
                sleep.assert_not_called()

    def test_transaction_does_not_repeat_failed_sql_commit_or_rollback(self):
        for failing_path, action, values in (
            ("Execute", "insert_rows", INSERT),
            ("CommitTransaction", "insert_rows", INSERT),
            ("RollbackTransaction", "query", {"sql":"SELECT 1", "params":[]}),
        ):
            with self.subTest(failing_path=failing_path):
                self.aws.calls.clear()
                failures = []
                def failed_operation(method, url, **kwargs):
                    if url.endswith("/" + failing_path):
                        failures.append(url)
                        raise WebRequestError("private", status=400, aws_error_type="DatabaseResumingException")
                    return self.aws(method, url, **kwargs)
                with patch.object(client, "request_bytes", side_effect=failed_operation), patch.object(client.time, "sleep") as sleep:
                    result = self.run_action(action, values)
                self.assertIsInstance(result, ActionFailed)
                self.assertEqual(len(failures), 1)
                self.assertIn("This request was not retried", result.error)
                sleep.assert_not_called()

    def test_nested_response_validation_emits_diagnostic_before_successful_rollback(self):
        cases = [
            ("query", {"records": "private malformed rows"}),
            ("query", {"records": ["private malformed row"]}),
            ("query", {"records": [[{"stringValue": "private", "longValue": 1}]]}),
            ("query", {"records": [[{"blobValue": "private"}]]}),
            ("query", {"columnMetadata": "private malformed metadata"}),
            ("query", {"columnMetadata": ["private malformed column"]}),
            ("query", {"columnMetadata": [{"name": {"private": True}}]}),
            ("query", {"columnMetadata": [{"name": "private", "typeName": "text"}], "records": [[]]}),
            ("insert_rows", {"numberOfRecordsUpdated": "private malformed count"}),
        ]
        for action, response in cases:
            with self.subTest(action=action, response=response):
                self.diagnostics.reset_mock()
                self.aws.calls.clear()
                def malformed(method, url, **kwargs):
                    raw = self.aws(method, url, **kwargs)
                    statement = json.loads(kwargs["data"]).get("sql", "")
                    if statement.startswith(("SELECT * FROM (", "INSERT")):
                        return json.dumps(response).encode()
                    return raw
                with patch.object(client, "request_bytes", side_effect=malformed):
                    result = self.run_action(action, INSERT if action == "insert_rows" else {"sql":"SELECT 1", "params":[]})
                self.assertIsInstance(result, ActionFailed)
                self.diagnostics.assert_called_once()
                record = self.diagnostics.call_args.args[0]
                self.assertEqual(record["context"]["failure_type"], "invalid_response")
                self.assertEqual(record["context"]["phase"], "statement")
                self.assertNotIn("elapsed_ms", record["context"])
                self.assertNotIn("private", json.dumps(record))
                self.assertTrue(self.aws.calls[-1][0].endswith("/RollbackTransaction"))
                self.assertFalse(any(url.endswith("/CommitTransaction") for url, _, _ in self.aws.calls))

    def test_invalid_action_input_is_not_reported_as_provider_failure(self):
        result = self.run_action("query", {"sql":"DELETE FROM private_table", "params":[]})
        self.assertIsInstance(result, ActionFailed)
        self.assertFalse(self.aws.calls)
        self.diagnostics.assert_not_called()

    def test_drop_is_queued_without_dropper_access_then_locked_rechecked_and_restrict(self):
        result = self.run_action("drop_table", {"table":"insights"})
        self.assertIsInstance(result, ActionPendingApproval)
        self.assertTrue(all(body["secretArn"].endswith("reader-abcdef") for _,body,_ in self.aws.calls))
        self.assertFalse(any(s.startswith("DROP") for s in self.aws.statements()))
        approval = self.api.approvals.approve(result.approval_id)
        self.assertNotIn("arn:", json.dumps(approval.payload))
        self.aws.calls.clear()
        completed = pg.BUNDLED_TOOL.execute_approved(approval, self.api)
        self.assertIsInstance(completed, ApprovalExecuted)
        statements = self.aws.statements()
        lock = next(i for i,s in enumerate(statements) if s.startswith("LOCK TABLE"))
        self.assertLess(lock, statements.index(sql.TABLE_TARGET))
        self.assertEqual(statements[-1], 'DROP TABLE "agent_data"."insights" RESTRICT')
        self.assertTrue(all(body["secretArn"].endswith("dropper-abcdef") for _,body,_ in self.aws.calls))

    def test_stale_or_unapproved_drop_never_drops(self):
        proposal = self.run_action("drop_table", {"table":"insights"})
        pending = self.api.approvals.get(proposal.approval_id)
        self.aws.calls.clear()
        self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(pending,self.api), ActionFailed)
        self.assertFalse(self.aws.calls)
        approval = self.api.approvals.approve(proposal.approval_id)
        self.aws.identity = ["101","r","false","[]"]
        result = pg.BUNDLED_TOOL.execute_approved(approval,self.api)
        self.assertIsInstance(result, ActionFailed)
        self.assertFalse(any(s.startswith("DROP") for s in self.aws.statements()))
        self.aws.calls.clear()
        self.api.config[client.PREFIX+"SCHEMA"] = "other_data"
        self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(approval,self.api),ActionFailed)
        self.assertFalse(self.aws.calls)

    def test_changed_partition_tree_invalidates_drop(self):
        self.aws.identity = ["100","p","false",'["100","102"]']
        proposal = self.run_action("drop_table", {"table":"insights"})
        approval = self.api.approvals.approve(proposal.approval_id)
        self.aws.identity = ["100","p","false",'["100","102","103"]']
        self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(approval,self.api),ActionFailed)
        self.assertFalse(any(s.startswith("DROP") for s in self.aws.statements()))

    def test_wrong_role_secret_fails_before_agent_sql(self):
        self.aws.wrong_role = True
        result = self.run_action("query", {"sql":"SELECT 1", "params":[]})
        self.assertIsInstance(result,ActionFailed)
        self.assertIn("wrong database user",result.error)
        self.assertFalse(any(s.startswith("SELECT * FROM (") for s in self.aws.statements()))

    def test_structured_ddl_and_quota_lock(self):
        result = self.run_action("manage_schema", CREATE)
        self.assertIsInstance(result, ActionExecuted)
        statements = self.aws.statements()
        self.assertIn("pg_advisory_xact_lock",statements[-3])
        self.assertEqual(statements[-2],sql.TABLE_COUNTS)
        self.assertIn('CHECK (octet_length("note"::text) <= 5120)',statements[-1])
        self.aws.counts = [["100","100"]]
        self.aws.calls.clear()
        self.assertIsInstance(self.run_action("manage_schema", CREATE),ActionFailed)
        self.assertFalse(any(s.startswith("CREATE TABLE") for s in self.aws.statements()))

    def test_destructive_or_untyped_schema_inputs_rejected_before_aws(self):
        bad = [{**CREATE,"sql":"DROP TABLE insights"}, {**CREATE,"operation":"drop_column"},
            {**CREATE,"columns":[{"name":"id","type":"text); DROP TABLE x; --","nullable":True}]},
            {"operation":"add_column","table":"insights","column":{"name":"x","type":"integer","nullable":False}},
            {"operation":"create_partition","table":"insights","partition":"part","from":"1'); DROP TABLE x; --","to":"2"}]
        for values in bad:
            with self.subTest(operation=values.get("operation")):
                self.assertIsInstance(self.run_action("manage_schema",values),ActionFailed)
        self.assertFalse(self.aws.calls)

    def test_constraint_ddl_is_restrict_only_and_check_text_is_dollar_quoted(self):
        cases = [
            ({"operation":"add_foreign_key","table":"listings","name":"listings_company","columns":["company_id"],
              "references_table":"companies","references_columns":["company_id"]},
             'ALTER TABLE "agent_data"."listings" ADD CONSTRAINT "listings_company" FOREIGN KEY ("company_id") '
             'REFERENCES "agent_data"."companies" ("company_id") ON UPDATE RESTRICT ON DELETE RESTRICT'),
            ({"operation":"add_check","table":"companies","name":"cik_digits","columns":["cik"],"check":{"pattern":"^[0-9]{10}$"}},
             'ALTER TABLE "agent_data"."companies" ADD CONSTRAINT "cik_digits" CHECK ("cik" ~ $kern_literal$^[0-9]{10}$$kern_literal$)'),
            ({"operation":"add_check","table":"companies","name":"kind_values","columns":["kind"],"check":{"allowed_values":["it's", "b;--"]}},
             'ALTER TABLE "agent_data"."companies" ADD CONSTRAINT "kind_values" CHECK ("kind" IN ($kern_literal$it\'s$kern_literal$,$kern_literal$b;--$kern_literal$))'),
            ({"operation":"add_check","table":"facts","name":"year_range","columns":["fiscal_year"],"check":{"min":"1990","max":"2100"}},
             'ALTER TABLE "agent_data"."facts" ADD CONSTRAINT "year_range" CHECK ("fiscal_year" >= \'1990\' AND "fiscal_year" <= \'2100\')'),
            ({"operation":"add_check","table":"companies","name":"name_present","columns":["name"],"check":{"not_blank":True}},
             'ALTER TABLE "agent_data"."companies" ADD CONSTRAINT "name_present" CHECK ("name" ~ \'[^[:space:]]\')'),
            ({"operation":"set_not_null","table":"companies","columns":["name","cik"]},
             'ALTER TABLE "agent_data"."companies" ALTER COLUMN "name" SET NOT NULL, ALTER COLUMN "cik" SET NOT NULL'),
        ]
        for values, expected in cases:
            with self.subTest(operation=values["operation"], name=values.get("name")):
                self.aws.calls.clear()
                self.assertIsInstance(self.run_action("manage_schema", values), ActionExecuted)
                self.assertEqual(self.aws.statements()[-1], expected)
                self.assertTrue(all(body["secretArn"].endswith("ddl-abcdef") for _,body,_ in self.aws.calls))
        # A pattern on an empty table is still compiled before the constraint exists.
        self.aws.calls.clear()
        self.run_action("manage_schema", cases[1][0])
        self.assertEqual(self.aws.statements()[-2], "SELECT '' ~ $kern_literal$^[0-9]{10}$$kern_literal$")

    def test_allowed_values_can_include_empty_string(self):
        result = self.run_action("manage_schema", {"operation":"add_check","table":"insights","name":"allowed_note","columns":["note"],"check":{"allowed_values":["","known"]}})
        self.assertIsInstance(result,ActionExecuted)
        self.assertIn("$kern_literal$$kern_literal$",self.aws.statements()[-1])

    def test_constraint_inputs_rejected_before_aws(self):
        check = {"operation":"add_check","table":"companies","name":"rule","columns":["cik"]}
        fk = {"operation":"add_foreign_key","table":"listings","name":"fk","columns":["company_id"],
              "references_table":"companies","references_columns":["company_id"]}
        bad = [{**check,"check":{}}, {**check,"check":{"pattern":"a","not_blank":True}},
            {**check,"check":{"pattern":"x$kern_literal"}}, {**check,"check":{"pattern":"a$kern_literal$); DROP TABLE x; --"}},
            {**check,"check":{"pattern":""}}, {**check,"check":{"pattern":"a"*501}}, {**check,"check":{"pattern":"a\x00"}},
            {**check,"check":{"allowed_values":[]}}, {**check,"check":{"allowed_values":["a","a"]}},
            {**check,"check":{"min":"1'); DROP TABLE x; --"}}, {**check,"check":{"not_blank":False}},
            {**check,"columns":["cik","name"],"check":{"not_blank":True}}, {**check,"check":{"sql":"true"}},
            {**fk,"references_columns":["company_id","name"]}, {**fk,"references_table":"other.companies"},
            {**fk,"on_delete":"cascade"}, {"operation":"set_not_null","table":"companies","columns":[]}]
        for values in bad:
            with self.subTest(values=repr(values)[:100]):
                self.assertIsInstance(self.run_action("manage_schema", values), ActionFailed)
        self.assertFalse(self.aws.calls)

    def test_delete_row_uses_owner_and_complete_primary_key(self):
        result = self.run_action("delete_row", {"table":"insights","key":[{"column":"id","value":"1"}]})
        self.assertIsInstance(result, ActionExecuted)
        self.assertEqual(result.result, {"deleted":1,"row":'{"id": 1, "note": "A dated insight"}'})
        statement = self.aws.statements()[-1]
        self.assertEqual(statement, 'DELETE FROM "agent_data"."insights" AS kern_row WHERE "id"=CAST(:k0 AS bigint) RETURNING CASE WHEN octet_length(to_jsonb(kern_row)::text) <= 60000 THEN to_jsonb(kern_row)::text ELSE NULL END')
        self.assertTrue(all(body["secretArn"].endswith("ddl-abcdef") for _,body,_ in self.aws.calls))
        self.assertEqual(pg.MANIFEST.action("delete_row").limit_runs_per_day, 100)
        self.aws.primary_key = [["company_id","text"],["accession_number","text"]]
        self.aws.calls.clear()
        self.run_action("delete_row", {"table":"filings","key":[{"column":"accession_number","value":"0000320193-25-000079"},{"column":"company_id","value":"apple"}]})
        self.assertIn('"company_id"=CAST(:k0 AS text) AND "accession_number"=CAST(:k1 AS text)', self.aws.statements()[-1])

    def test_foreign_key_column_limit_before_aws(self):
        base = {"operation":"add_foreign_key", "table":"child", "name":"fk", "references_table":"parent"}
        columns = [f"c{i}" for i in range(33)]
        for child, parent in ((columns, columns[:32]), (columns[:32], columns)):
            result = self.run_action("manage_schema", {**base,"columns":child,"references_columns":parent})
            self.assertIsInstance(result, ActionFailed)
            self.assertIn("1-32", result.error)
        self.assertFalse(self.aws.calls)
        self.assertIsInstance(self.run_action("manage_schema", {**base,"columns":columns[:32],"references_columns":columns[:32]}), ActionExecuted)

    def test_delete_row_accepts_sql_null_for_large_row(self):
        self.aws.deleted = [[None]]
        result = self.run_action("delete_row", {"table":"insights","key":[{"column":"id","value":"1"}]})
        self.assertEqual(result.result, {"deleted":1,"row":None})
        self.assertIn("CASE WHEN octet_length", self.aws.statements()[-1])

    def test_column_drop_approval_discloses_data_loss_and_rejects_pending(self):
        proposal = self.run_action("drop_column", {"table":"insights","column":"note"})
        self.assertIn("ALL values", proposal.summary)
        self.assertIn("indexes and constraints", proposal.summary)
        pending = self.api.approvals.get(proposal.approval_id)
        self.aws.calls.clear()
        self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(pending, self.api), ActionFailed)
        self.assertFalse(self.aws.calls)
        approved = self.api.approvals.approve(proposal.approval_id)
        self.aws.objects[sql.COLUMN_TARGET] = [["2","changed definition","false","false"]]
        self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(approved, self.api), ActionFailed)
        self.assertFalse(any("DROP COLUMN" in s for s in self.aws.statements()))

    def test_delete_row_never_runs_partial_or_missing_keys(self):
        self.aws.primary_key = [["company_id","text"],["accession_number","text"]]
        for key in ([{"column":"company_id","value":"apple"}],
                    [{"column":"company_id","value":"apple"},{"column":"note","value":"x"}]):
            with self.subTest(key=key):
                result = self.run_action("delete_row", {"table":"filings","key":key})
                self.assertIsInstance(result, ActionFailed)
                self.assertIn("company_id, accession_number", result.error)
        self.aws.primary_key = []
        self.assertIn("no primary key", self.run_action("delete_row", {"table":"logs","key":[{"column":"id","value":"1"}]}).error)
        self.assertFalse(any(s.startswith("DELETE") for s in self.aws.statements()))
        self.aws.primary_key = [["id","bigint"]]
        self.aws.deleted = []
        missing = self.run_action("delete_row", {"table":"insights","key":[{"column":"id","value":"9"}]})
        self.assertIn("Nothing was deleted", missing.error)
        self.aws.calls.clear()
        for values in ({"table":"insights","key":[]}, {"table":"insights","key":[{"column":"id","value":None}]},
                       {"table":"insights","key":[{"column":"id","value":"1"},{"column":"id","value":"2"}]},
                       {"table":"insights","key":[{"column":"id","value":"1"}],"where":"true"}):
            self.assertIsInstance(self.run_action("delete_row", values), ActionFailed)
        self.assertFalse(self.aws.calls)

    def test_index_and_constraint_drops_need_approval_and_recheck(self):
        cases = [("drop_column", {"table":"insights","column":"note"}, sql.COLUMN_TARGET,
                  'ALTER TABLE "agent_data"."insights" DROP COLUMN "note" RESTRICT'),
                 ("drop_index", {"table":"insights","name":"insights_score"}, sql.INDEX_TARGET,
                  'DROP INDEX "agent_data"."insights_score" RESTRICT'),
                 ("drop_constraint", {"table":"insights","constraint":"note_present"}, sql.CONSTRAINT_TARGET,
                  'ALTER TABLE "agent_data"."insights" DROP CONSTRAINT "note_present" RESTRICT'),
                 ("drop_constraint", {"table":"insights","not_null_column":"note"}, sql.NOT_NULL_TARGET,
                  'ALTER TABLE "agent_data"."insights" ALTER COLUMN "note" DROP NOT NULL')]
        for action, values, query, expected in cases:
            with self.subTest(action=action, values=values):
                self.aws.calls.clear()
                proposal = self.run_action(action, values)
                self.assertIsInstance(proposal, ActionPendingApproval)
                self.assertTrue(all(body["secretArn"].endswith("reader-abcdef") for _,body,_ in self.aws.calls))
                approval = self.api.approvals.approve(proposal.approval_id)
                original = self.aws.objects[query]
                self.aws.objects[query] = [["999", *original[0][1:]]]
                self.aws.calls.clear()
                self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(approval, self.api), ActionFailed)
                self.assertNotIn(expected, self.aws.statements())
                self.aws.objects[query] = original
                self.aws.calls.clear()
                self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(approval, self.api), ApprovalExecuted)
                statements = self.aws.statements()
                self.assertLess(next(i for i,s in enumerate(statements) if s.startswith("LOCK TABLE")), statements.index(query))
                self.assertEqual(statements[-1], expected)
                self.assertTrue(all(body["secretArn"].endswith("dropper-abcdef") for _,body,_ in self.aws.calls))
        refused = [("drop_index", {"table":"leaf","name":"leaf_idx"}, sql.INDEX_TARGET, [["200","false","true","false"]], "Attached partition indexes"),
                   ("drop_index", {"table":"parent","name":"parent_unique"}, sql.INDEX_TARGET, [["200","false","false","true"]], "Foreign keys depend"),
                   ("drop_constraint", {"table":"leaf","constraint":"check_parent"}, sql.CONSTRAINT_TARGET, [["3","c","true"]], "Inherited constraints"),
                   ("drop_constraint", {"table":"leaf","not_null_column":"note"}, sql.NOT_NULL_TARGET, [["2","true","false","true","42"]], "Inherited constraints"),
                   ("drop_column", {"table":"leaf","column":"note"}, sql.COLUMN_TARGET, [["2","definition","false","true"]], "Inherited columns"),
                   ("drop_column", {"table":"insights","column":"id"}, sql.COLUMN_TARGET, [["1","definition","true","false"]], "Primary key"),
                   ("drop_constraint", {"table":"insights","not_null_column":"id"}, sql.NOT_NULL_TARGET, [["1","true","true","false","42"]], "Primary key"),
                   ("drop_constraint", {"table":"insights","constraint":"insights_pkey"}, sql.CONSTRAINT_TARGET, [["300","p","false"]], "Primary keys"),
                   ("drop_index", {"table":"insights","name":"insights_pkey"}, sql.INDEX_TARGET, [["200","true","false","false"]], "enforces a constraint"),
                   ("drop_constraint", {"table":"insights","not_null_column":"note"}, sql.NOT_NULL_TARGET, [["2","false","false","false","42"]], "already nullable"),
                   ("drop_index", {"table":"insights","name":"missing"}, sql.INDEX_TARGET, [], "does not exist")]
        for action, values, query, rows, message in refused:
            with self.subTest(refused=values):
                self.aws.objects[query] = rows
                result = self.run_action(action, values)
                self.assertIsInstance(result, ActionFailed)
                self.assertIn(message, result.error)
        self.aws.calls.clear()
        for values in ({"table":"insights"}, {"table":"insights","constraint":"a","not_null_column":"b"}):
            self.assertIsInstance(self.run_action("drop_constraint", values), ActionFailed)
        self.assertFalse(self.aws.calls)

    def test_not_null_generation_change_invalidates_approval(self):
        values = {"table":"insights","not_null_column":"note"}
        proposal = self.run_action("drop_constraint",values)
        approval = self.api.approvals.approve(proposal.approval_id)
        self.aws.objects[sql.NOT_NULL_TARGET] = [["2","true","false","false","43"]]
        self.aws.calls.clear()
        self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(approval,self.api),ActionFailed)
        self.assertFalse(any("DROP NOT NULL" in s for s in self.aws.statements()))

    def test_input_bounds_and_unknown_fields_before_aws(self):
        bad = [({**INSERT,"rows":[["1","é"*16384]]}),
               ({**INSERT,"rows":[[str(i),"a"*5000] for i in range(14)]}),
               ({**INSERT,"rows":[["1","a"]]*501}), ({**INSERT,"rows":[["1"]]}),
               ({**INSERT,"on_conflict":"update"}), ({**INSERT,"table":"other.insights"}),
               ({**INSERT,"extra":True}), ({**INSERT,"rows":[["1",123]]})]
        for values in bad:
            with self.subTest(label=repr(values)[:80]):
                self.assertIsInstance(self.run_action("insert_rows",values),ActionFailed)
        for statement in ("DELETE FROM insights", "SELECT 1; DROP TABLE insights", "SELECT 1 -- hide suffix"):
            self.assertIsInstance(self.run_action("query",{"sql":statement,"params":[]}),ActionFailed)
        self.assertFalse(self.aws.calls)

    def test_result_rows_are_never_clipped_and_metadata_precision_is_preserved(self):
        self.aws.query_rows = [[str(i),"a"*4000] for i in range(30)]
        result = self.run_action("query",{"sql":"SELECT id,note FROM insights","params":[]})
        self.assertTrue(result.result["truncated"])
        self.assertLess(len(sql.json_bytes(result.result)),65536)
        self.assertTrue(all(len(row[1])==4000 for row in result.result["rows"]))
        self.aws.query_rows = [["1","a"*70000]]
        result = self.run_action("query",{"sql":"SELECT id,note FROM insights","params":[]})
        self.assertEqual(result.result["rows"],[])
        self.assertTrue(result.result["truncated"])

    def test_fixed_aws_paths_are_part_of_signature(self):
        args = dict(host="rds-data.us-east-1.amazonaws.com",region="us-east-1",service="rds-data",
            access_key_id="AKIA"+"A"*16,secret_access_key="s"*40,body=b"{}",content_type="application/json",now=datetime(2026,1,1,tzinfo=timezone.utc))
        a, b = sign_post(**args,path="/Execute"), sign_post(**args,path="/CommitTransaction")
        self.assertNotEqual(a.headers["authorization"],b.headers["authorization"])
        self.assertTrue(sign_post(**args).url.endswith("/"))
        with self.assertRaises(ValueError):
            sign_post(**args,path="/Execute?secret=wrong")

    def test_bootstrap_drop_guard_uses_session_identity_and_no_settable_flag(self):
        self.assertIn("session_user IN",BOOTSTRAP_SQL)
        self.assertNotIn("kern.approved_drop",BOOTSTRAP_SQL)
        self.assertIn("GRANT kern_ddl TO kern_dropper",BOOTSTRAP_SQL)
        self.assertNotIn("GRANT kern_dropper TO",BOOTSTRAP_SQL)
        self.assertTrue(BOOTSTRAP_SQL.startswith("DO $kern_setup$"))


if __name__ == "__main__":
    unittest.main()
