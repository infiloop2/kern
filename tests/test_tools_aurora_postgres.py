"""Aurora tool transport, bounds, transaction failures and approval binding."""
from __future__ import annotations

import json
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from host.tools import aurora_postgres as pg
from host.tools.aurora_postgres import client, sql
from host.tools.aurora_postgres.setup import BOOTSTRAP_SQL
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ApprovalExecuted
from host.tools.shared.aws_sigv4 import sign_post
from host.tools.shared.web import WebRequestError
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
