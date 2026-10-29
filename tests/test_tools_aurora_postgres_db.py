"""Real PostgreSQL role, DDL, insert and approval tests; CI-only on Kern hosts.

The adapter replaces only AWS HTTPS transport. The tool's SQL, transactions,
bootstrap, role session identities and PostgreSQL enforcement all run for real.
AWS IAM/Secrets Manager and Aurora service-specific behavior still need a live
operator smoke test on a disposable cluster.
"""
import json
import os
import re
import unittest
from unittest.mock import patch

import pg_harness
from host.runtime.core import pgclient
from host.tools import aurora_postgres as pg
from host.tools.aurora_postgres import client
from host.tools.aurora_postgres.setup import BOOTSTRAP_SQL, ROLES
from host.tools.aurora_postgres.bootstrap import bootstrap_statements
from host.tools.results import ActionExecuted, ActionFailed, ApprovalExecuted
from host.tools.shared.web import WebRequestError
from test_tools_aurora_postgres import api, CREATE, INSERT


def connection(role):
    return pgclient.Connection(socket_dir=os.environ["KERN_DB_SOCKET_DIR"],dbname="agent_store",user=role)


class PostgreSQLTransport:
    def __init__(self):
        self.sessions = {}
        self.counter = 0
        self.last_error = ""

    def close(self):
        for conn in self.sessions.values():
            conn.close()
        self.sessions.clear()

    def __call__(self, method, url, **kwargs):
        body = json.loads(kwargs["data"])
        operation = url.rsplit("/",1)[-1]
        if operation == "BeginTransaction":
            role = "kern_"+body["secretArn"].split(":secret:")[1].split("-")[0]
            conn = connection(role)
            conn.execute("BEGIN")
            self.counter += 1
            tx = str(self.counter)
            self.sessions[tx] = conn
            return json.dumps({"transactionId":tx}).encode()
        conn = self.sessions[body["transactionId"]]
        try:
            if operation in ("CommitTransaction","RollbackTransaction"):
                conn.execute("COMMIT" if operation == "CommitTransaction" else "ROLLBACK")
                conn.close()
                del self.sessions[body["transactionId"]]
                return b'{}'
            params_by_name = {p["name"]:p["value"].get("stringValue") for p in body["parameters"]}
            params = []
            def placeholder(match):
                params.append(params_by_name[match[1]])
                return "%s"
            statement = re.sub(r"(?<!:):([a-z][a-z0-9_]*)",placeholder,body["sql"])
            is_insert = statement.startswith("INSERT")
            # Data API exposes a row count; the tiny test wire client does not.
            if is_insert:
                statement += " RETURNING 1"
            result = conn.execute(statement,params if params else None)
            if is_insert:
                return json.dumps({"numberOfRecordsUpdated":len(result.rows)}).encode()
            types = {16:"bool",20:"int8",21:"int2",23:"int4",25:"text",701:"float8",1700:"numeric",1184:"timestamptz",1082:"date",2950:"uuid",3802:"jsonb"}
            columns = [{"name":name,"typeName":types.get(oid,"text")} for name,oid in result.columns]
            rows = [[{"isNull":True} if v is None else {"booleanValue":v} if type(v) is bool else {"stringValue":str(v)} for v in row] for row in result.rows]
            return json.dumps({"columnMetadata":columns,"records":rows}).encode()
        except pgclient.Error as exc:
            self.last_error = str(exc)
            raise WebRequestError("Database error",status=400,body=b'{"__type":"DatabaseErrorException"}') from exc


class AuroraPostgresDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pg_harness.create_database("agent_store")
        master = connection("postgres")
        try:
            master.execute(BOOTSTRAP_SQL)
        finally:
            master.close()

    @classmethod
    def tearDownClass(cls):
        # Roles are cluster-wide; release only this fixture's roles/dependencies.
        master = connection("postgres")
        try:
            master.execute("DROP EVENT TRIGGER kern_block_unapproved_drop")
            master.execute("DROP SCHEMA agent_data CASCADE; DROP SCHEMA kern_control CASCADE")
            for role in reversed(ROLES):
                master.execute(f"DROP OWNED BY {role}; DROP ROLE {role}")
        finally:
            master.close()

    def setUp(self):
        self.api = api()
        self.transport = PostgreSQLTransport()
        self.addCleanup(self.transport.close)
        self.enterContext(patch.object(client,"request_bytes",side_effect=self.transport))

    def execute(self,action,values):
        result = pg.BUNDLED_TOOL.execute(action,values,self.api)
        self.assertIsInstance(result,ActionExecuted,msg=f"{result}: {self.transport.last_error}")
        return result.result

    def test_full_storage_flow_with_real_postgresql(self):
        self.execute("manage_schema",CREATE)
        self.assertEqual(self.execute("insert_rows",INSERT),{"inserted":1,"skipped":0})
        self.assertEqual(self.execute("insert_rows",INSERT),{"inserted":0,"skipped":1})
        result = self.execute("query",{"sql":"SELECT id, note FROM insights ORDER BY id","params":[]})
        self.assertEqual(result["rows"],[["1","A dated insight"]])
        self.assertEqual(len(self.execute("list_tables",{})["rows"]),1)
        self.assertTrue(any(row[0]=="constraint" for row in self.execute("describe_table",{"table":"insights"})["rows"]))
        self.execute("manage_schema",{"operation":"add_column","table":"insights","column":{"name":"score","type":"numeric","nullable":True}})
        self.execute("manage_schema",{"operation":"create_index","table":"insights","name":"insights_score","columns":["score"],"unique":False})
        failed = pg.BUNDLED_TOOL.execute("insert_rows",{**INSERT,"rows":[["2","New"],["1","Duplicate"]],"on_conflict":"error"},self.api)
        self.assertIsInstance(failed,ActionFailed)
        self.assertEqual(self.execute("query",{"sql":"SELECT count(*) FROM insights","params":[]})["rows"],[["1"]])
        proposal = pg.BUNDLED_TOOL.execute("drop_table",{"table":"insights"},self.api)
        outcome = pg.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(proposal.approval_id),self.api)
        self.assertIsInstance(outcome,ApprovalExecuted,msg=self.transport.last_error)
        self.assertEqual(self.execute("list_tables",{})["rows"],[])

    def test_native_partition_creation_rollup_detach_and_drop(self):
        self.execute("manage_schema",{**CREATE,"table":"snapshots","partition_by":"id"})
        self.execute("manage_schema",{"operation":"create_partition","table":"snapshots","partition":"snapshots_one","from":"0","to":"100"})
        self.execute("insert_rows",{**INSERT,"table":"snapshots"})
        listing = self.execute("list_tables",{})
        self.assertEqual(len(listing["rows"]),1)
        self.assertEqual(listing["rows"][0][2],"1")
        self.execute("manage_schema",{"operation":"detach_partition","table":"snapshots","partition":"snapshots_one"})
        self.assertEqual(self.execute("query",{"sql":"SELECT count(*) FROM snapshots","params":[]})["rows"],[["0"]])
        self.assertEqual(self.execute("query",{"sql":"SELECT count(*) FROM snapshots_one","params":[]})["rows"],[["1"]])
        for table in ("snapshots_one","snapshots"):
            proposal = pg.BUNDLED_TOOL.execute("drop_table",{"table":table},self.api)
            self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(proposal.approval_id),self.api),ApprovalExecuted,msg=self.transport.last_error)

    def test_database_grants_and_drop_guard_cannot_be_replaced_by_session_flag(self):
        self.execute("manage_schema",{**CREATE,"table":"permissions"})
        writer = connection("kern_writer")
        reader = connection("kern_reader")
        owner = connection("kern_ddl")
        for conn in (writer,reader,owner):
            self.addCleanup(conn.close)
        writer.execute("INSERT INTO agent_data.permissions VALUES (1,'first')")
        for statement in ("UPDATE agent_data.permissions SET note='changed'", "DELETE FROM agent_data.permissions", "TRUNCATE agent_data.permissions", "SET ROLE kern_dropper"):
            with self.assertRaises(pgclient.Error):
                writer.execute(statement)
        with self.assertRaises(pgclient.Error):
            reader.execute("INSERT INTO agent_data.permissions VALUES (2,'bad')")
        with self.assertRaises(pgclient.Error):
            writer.execute("INSERT INTO agent_data.permissions VALUES (2,%s)",["x"*5121])
        owner.execute("SET kern.approved_drop = 'on'")
        with self.assertRaises(pgclient.Error):
            owner.execute("DROP TABLE agent_data.permissions")
        with self.assertRaises(pgclient.Error):
            owner.execute("SET ROLE kern_dropper")
        master = connection("postgres")
        try:
            self.assertEqual(master.execute("SELECT note FROM agent_data.permissions").rows,[("first",)])
            master.execute("DROP TABLE agent_data.permissions")
        finally:
            master.close()


class BootstrapMembershipTests(unittest.TestCase):
    def test_role_grants_work_without_postgres_superuser(self):
        pg_harness.create_database("bootstrap_permissions")
        def connect(role):
            return pgclient.Connection(socket_dir=os.environ["KERN_DB_SOCKET_DIR"], dbname="bootstrap_permissions", user=role)
        admin = connect("postgres")
        try:
            admin.execute("CREATE ROLE aurora_master LOGIN NOSUPERUSER CREATEDB CREATEROLE INHERIT")
            admin.execute("ALTER DATABASE bootstrap_permissions OWNER TO aurora_master")
            master = connect("aurora_master")
            try:
                statements = bootstrap_statements("bootstrap_permissions", "agent_data", {role:"fixture-password" for role in ROLES})
                # Aurora's rds_superuser can create event triggers. Community
                # PostgreSQL requires SUPERUSER for that one statement, so the
                # separate full bootstrap test covers it as postgres.
                statements = [statement for statement in statements if not statement.startswith("CREATE EVENT TRIGGER")]
                master.execute("DO $test$ BEGIN " + ";\n".join(statements) + "; END; $test$")
                self.assertEqual(master.execute("SELECT pg_has_role(session_user, 'kern_ddl', 'MEMBER')").rows, [(False,)])
            finally:
                master.close()
            owner = connect("kern_ddl")
            writer = connect("kern_writer")
            reader = connect("kern_reader")
            try:
                owner.execute("CREATE TABLE agent_data.probe (id bigint PRIMARY KEY)")
                writer.execute("INSERT INTO agent_data.probe VALUES (1)")
                self.assertEqual(reader.execute("SELECT id FROM agent_data.probe").rows, [(1,)])
            finally:
                owner.close()
                writer.close()
                reader.close()
        finally:
            admin.execute("DROP SCHEMA IF EXISTS agent_data CASCADE; DROP SCHEMA IF EXISTS kern_control CASCADE")
            for role in reversed(ROLES):
                if admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", [role]).rows:
                    admin.execute(f"DROP OWNED BY {role}; DROP ROLE {role}")
            admin.execute("ALTER DATABASE bootstrap_permissions OWNER TO postgres")
            admin.execute("DROP OWNED BY aurora_master; DROP ROLE aurora_master")
            admin.close()
