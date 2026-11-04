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
from unittest.mock import Mock, patch

import pg_harness
from host.runtime.core import pgclient
from host.tools import aurora_postgres as pg
from host.tools.aurora_postgres import client, sql
from host.tools.aurora_postgres.setup import BOOTSTRAP_SQL, ROLES
from host.tools.aurora_postgres.bootstrap import bootstrap_statements
from host.tools.results import ActionExecuted, ActionFailed, ApprovalExecuted
from host.tools.shared.web import WebRequestError
from test_tools_aurora_postgres import api, CREATE, INSERT


def connection(role):
    return pgclient.Connection(socket_dir=os.environ["KERN_DB_SOCKET_DIR"],dbname="agent_store",user=role)


# Match quoted SQL first so literal colons (such as [:space:]) are not binds.
# The tool rejects SQL comments; its DDL uses standard or dollar-quoted strings.
SQL_PARAMETERS = re.compile(
    r"(?<!\w)[eE]'(?:\\.|''|[^'\\])*'|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\""
    r"|(?P<tag>\$(?:[a-zA-Z_][a-zA-Z_0-9]*)?\$).*?(?P=tag)"
    r"|(?<!:):(?P<parameter>[a-z][a-z0-9_]*)",
    re.DOTALL,
)


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
                name = match.group("parameter")
                if name is None:
                    return match[0]
                params.append(params_by_name[name])
                return "%s"
            statement = SQL_PARAMETERS.sub(placeholder,body["sql"])
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


class PostgreSQLTransportTests(unittest.TestCase):
    """Exercise the test adapter without requiring a database on Kern hosts."""

    def setUp(self):
        self.transport = PostgreSQLTransport()
        self.conn = Mock()
        self.conn.execute.return_value.columns = []
        self.conn.execute.return_value.rows = []
        self.transport.sessions["test"] = self.conn

    def execute(self, statement, parameters):
        self.transport("POST", "https://example.invalid/Execute", data=json.dumps({
            "transactionId":"test", "sql":statement,
            "parameters":[client.parameter(name,value) for name,value in parameters.items()],
        }).encode())

    def test_not_blank_ddl_keeps_posix_class_literal(self):
        _, _, statements = sql.ddl({"operation":"add_check","table":"companies",
            "name":"name_present","columns":["name"],"check":{"not_blank":True}}, "agent_data")
        self.execute(statements[-1], {})
        self.conn.execute.assert_called_once_with(statements[-1], None)

    def test_only_unquoted_parameters_are_bound_in_occurrence_order(self):
        statement = "SELECT :value::text, ':value '' :space', \":value\", $tag$:value ':space'$tag$, $$:value$$, :other, :value"
        expected = "SELECT %s::text, ':value '' :space', \":value\", $tag$:value ':space'$tag$, $$:value$$, %s, %s"
        self.execute(statement, {"other":None,"value":"bound"})
        self.conn.execute.assert_called_once_with(expected, ["bound",None,"bound"])

    def test_escape_string_does_not_expose_parameter_like_content(self):
        statement = r"SELECT E'escaped\' :value', :value"
        self.execute(statement, {"value":"bound"})
        self.conn.execute.assert_called_once_with(r"SELECT E'escaped\' :value', %s", ["bound"])


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

    def approve(self,action,values):
        proposal = pg.BUNDLED_TOOL.execute(action,values,self.api)
        self.assertNotIsInstance(proposal,ActionFailed,msg=getattr(proposal,"error",""))
        outcome = pg.BUNDLED_TOOL.execute_approved(self.api.approvals.approve(proposal.approval_id),self.api)
        self.assertIsInstance(outcome,ApprovalExecuted,msg=self.transport.last_error)

    def fails(self,action,values):
        self.assertIsInstance(pg.BUNDLED_TOOL.execute(action,values,self.api),ActionFailed)

    def test_constraints_restrict_single_row_deletes_and_approved_drops(self):
        text = lambda name, nullable: {"name":name,"type":"text","nullable":nullable}
        self.execute("manage_schema",{"operation":"create_table","table":"companies","columns":[text("company_id",False),text("cik",True)],"primary_key":["company_id"]})
        self.execute("manage_schema",{"operation":"create_table","table":"listings","columns":[text("listing_id",False),text("company_id",True)],"primary_key":["listing_id"]})
        self.execute("insert_rows",{"table":"companies","columns":["company_id","cik"],"rows":[["apple","0000320193"],["india",None]],"on_conflict":"error"})
        self.execute("insert_rows",{"table":"listings","columns":["listing_id","company_id"],"rows":[["apple:AAPL","apple"]],"on_conflict":"error"})
        listing = lambda listing_id, company: {"table":"listings","columns":["listing_id","company_id"],"rows":[[listing_id,company]],"on_conflict":"error"}
        key = lambda table, column, value: {"table":table,"key":[{"column":column,"value":value}]}

        self.execute("manage_schema",{"operation":"add_foreign_key","table":"listings","name":"listings_company","columns":["company_id"],
                                      "references_table":"companies","references_columns":["company_id"]})
        self.fails("insert_rows",listing("x:X","missing"))
        self.execute("manage_schema",{"operation":"add_check","table":"companies","name":"cik_digits","columns":["cik"],"check":{"pattern":"^[0-9]{10}$"}})
        self.fails("insert_rows",{"table":"companies","columns":["company_id","cik"],"rows":[["bad","123"]],"on_conflict":"error"})
        self.fails("manage_schema",{"operation":"add_check","table":"listings","name":"bad_regex","columns":["listing_id"],"check":{"pattern":"("}})
        self.execute("manage_schema",{"operation":"add_check","table":"listings","name":"listing_present","columns":["listing_id"],"check":{"not_blank":True}})
        for blank in ("", "  ", "\t", "\n", " \r\n\t\v\f"):
            self.fails("insert_rows",listing(blank,"apple"))
        # Existing NULL breaks the rule, so nothing changes.
        self.fails("manage_schema",{"operation":"set_not_null","table":"companies","columns":["cik"]})
        self.execute("manage_schema",{"operation":"set_not_null","table":"listings","columns":["company_id"]})
        self.fails("insert_rows",listing("y:Y",None))

        self.fails("delete_row",key("companies","company_id","apple"))
        self.assertEqual(self.execute("query",{"sql":"SELECT count(*) FROM companies","params":[]})["rows"],[["2"]])
        deleted = self.execute("delete_row",key("listings","listing_id","apple:AAPL"))
        self.assertEqual(json.loads(deleted["row"]),{"listing_id":"apple:AAPL","company_id":"apple"})
        self.execute("delete_row",key("companies","company_id","apple"))
        self.fails("delete_row",key("companies","company_id","apple"))
        self.fails("delete_row",{"table":"companies","key":[{"column":"cik","value":"0000320193"}]})
        self.assertEqual(self.execute("query",{"sql":"SELECT company_id FROM companies","params":[]})["rows"],[["india"]])

        self.fails("drop_constraint",{"table":"companies","constraint":"companies_pkey"})
        self.fails("drop_index",{"table":"companies","name":"companies_pkey"})
        self.fails("drop_constraint",{"table":"companies","not_null_column":"company_id"})
        self.fails("drop_column",{"table":"companies","column":"company_id"})
        self.execute("manage_schema",{"operation":"create_index","table":"companies","name":"companies_cik","columns":["cik"],"unique":True})
        self.approve("drop_index",{"table":"companies","name":"companies_cik"})
        self.approve("drop_constraint",{"table":"listings","constraint":"listings_company"})
        self.approve("drop_constraint",{"table":"listings","not_null_column":"company_id"})
        self.execute("insert_rows",listing("z:Z",None))
        self.execute("insert_rows",listing("w:W","missing"))
        definitions = [row[2] for row in self.execute("describe_table",{"table":"listings"})["rows"]]
        self.assertFalse(any("FOREIGN KEY" in d for d in definitions))
        self.assertNotIn("companies_cik",[row[1] for row in self.execute("describe_table",{"table":"companies"})["rows"]])
        for table in ("listings","companies"):
            self.approve("drop_table",{"table":table})

    def test_column_drop_preserves_rows_removes_local_dependencies_and_checks_identity(self):
        self.execute("manage_schema", {**CREATE,"table":"column_drop"})
        self.execute("insert_rows", {**INSERT,"table":"column_drop"})
        self.execute("manage_schema", {"operation":"create_index","table":"column_drop","name":"note_idx","columns":["note"],"unique":False})
        values = {"table":"column_drop","column":"note"}
        proposal = pg.BUNDLED_TOOL.execute("drop_column", values, self.api)
        self.assertEqual(len(self.api.approvals.get(proposal.approval_id).payload["object"][1]),32)
        approval = self.api.approvals.approve(proposal.approval_id)
        self.execute("manage_schema", {"operation":"set_not_null","table":"column_drop","columns":["note"]})
        self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(approval,self.api), ActionFailed)
        self.approve("drop_column",values)
        self.assertEqual(self.execute("query",{"sql":"SELECT * FROM column_drop","params":[]})["rows"],[["1"]])
        definitions = self.execute("describe_table",{"table":"column_drop"})["rows"]
        self.assertFalse(any(r[1] in ("note","note_idx","column_drop_note_check") for r in definitions))
        self.fails("drop_column",values)
        self.approve("drop_table",{"table":"column_drop"})

    def test_column_drop_restricts_external_foreign_keys(self):
        self.execute("manage_schema",{**CREATE,"table":"column_parent"})
        self.execute("manage_schema",{**CREATE,"table":"column_child"})
        self.execute("manage_schema",{"operation":"create_index","table":"column_parent","name":"parent_note_unique","columns":["note"],"unique":True})
        self.execute("manage_schema",{"operation":"add_foreign_key","table":"column_child","name":"note_fk","columns":["note"],"references_table":"column_parent","references_columns":["note"]})
        blocked = pg.BUNDLED_TOOL.execute("drop_index",{"table":"column_parent","name":"parent_note_unique"},self.api)
        self.assertIsInstance(blocked,ActionFailed)
        self.assertIn("Foreign keys depend",blocked.error)
        proposal = pg.BUNDLED_TOOL.execute("drop_column",{"table":"column_parent","column":"note"},self.api)
        approval = self.api.approvals.approve(proposal.approval_id)
        self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(approval,self.api),ActionFailed)
        self.assertTrue(any(r[1]=="note" for r in self.execute("describe_table",{"table":"column_parent"})["rows"]))
        for table in ("column_child","column_parent"):
            self.approve("drop_table",{"table":table})

    def test_column_drop_covers_attached_partitions(self):
        self.execute("manage_schema",{**CREATE,"table":"column_parts","partition_by":"id"})
        self.execute("manage_schema",{"operation":"create_partition","table":"column_parts","partition":"column_parts_one","from":"0","to":"100"})
        self.execute("insert_rows",{**INSERT,"table":"column_parts"})
        result = pg.BUNDLED_TOOL.execute("drop_column",{"table":"column_parts_one","column":"note"},self.api)
        self.assertIsInstance(result,ActionFailed)
        self.assertIn("partitioned parent",result.error)
        self.approve("drop_column",{"table":"column_parts","column":"note"})
        self.assertEqual(self.execute("query",{"sql":"SELECT * FROM column_parts_one","params":[]})["rows"],[["1"]])
        self.approve("drop_table",{"table":"column_parts"})

    def test_inherited_constraint_drops_fail_before_approval(self):
        self.execute("manage_schema",{**CREATE,"table":"constraint_parts","partition_by":"id"})
        self.execute("manage_schema",{"operation":"create_partition","table":"constraint_parts","partition":"constraint_leaf","from":"0","to":"100"})
        self.execute("manage_schema",{"operation":"add_check","table":"constraint_parts","name":"note_present","columns":["note"],"check":{"not_blank":True}})
        self.execute("manage_schema",{"operation":"set_not_null","table":"constraint_parts","columns":["note"]})
        for selector in ({"constraint":"note_present"},{"not_null_column":"note"}):
            result = pg.BUNDLED_TOOL.execute("drop_constraint",{"table":"constraint_leaf",**selector},self.api)
            self.assertIsInstance(result,ActionFailed)
            self.assertIn("partitioned parent",result.error)
            self.approve("drop_constraint",{"table":"constraint_parts",**selector})
        # A leaf-only NOT NULL rule has no parent dependency and can be removed.
        self.execute("manage_schema",{"operation":"set_not_null","table":"constraint_leaf","columns":["note"]})
        self.approve("drop_constraint",{"table":"constraint_leaf","not_null_column":"note"})
        self.approve("drop_table",{"table":"constraint_parts"})

    def test_column_approval_binds_leaf_dependencies_and_attached_indexes_are_refused(self):
        self.execute("manage_schema",{**CREATE,"table":"dependency_parts","partition_by":"id"})
        self.execute("manage_schema",{"operation":"create_partition","table":"dependency_parts","partition":"dependency_leaf","from":"0","to":"100"})
        self.execute("manage_schema",{"operation":"create_index","table":"dependency_parts","name":"parent_note_idx","columns":["note"],"unique":False})
        indexes = self.execute("query",{"sql":"SELECT ci.relname FROM pg_catalog.pg_inherits h JOIN pg_catalog.pg_class ci ON ci.oid=h.inhrelid JOIN pg_catalog.pg_class cp ON cp.oid=h.inhparent WHERE cp.relname='parent_note_idx'","params":[]})["rows"]
        self.assertEqual(len(indexes),1)
        refused = pg.BUNDLED_TOOL.execute("drop_index",{"table":"dependency_leaf","name":indexes[0][0]},self.api)
        self.assertIsInstance(refused,ActionFailed)
        self.assertIn("partitioned parent index",refused.error)
        values = {"table":"dependency_parts","column":"note"}
        for operation in (
            {"operation":"create_index","table":"dependency_leaf","name":"leaf_note_idx","columns":["note"],"unique":False},
            {"operation":"add_check","table":"dependency_leaf","name":"leaf_note_check","columns":["note"],"check":{"not_blank":True}},
        ):
            proposal = pg.BUNDLED_TOOL.execute("drop_column",values,self.api)
            approval = self.api.approvals.approve(proposal.approval_id)
            self.execute("manage_schema",operation)
            self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(approval,self.api),ActionFailed)
        self.approve("drop_column",values)
        self.approve("drop_table",{"table":"dependency_parts"})

    def test_parent_index_drop_checks_foreign_keys_referencing_a_leaf_index(self):
        self.execute("manage_schema",{**CREATE,"table":"index_parent","partition_by":"id"})
        self.execute("manage_schema",{"operation":"create_partition","table":"index_parent","partition":"index_leaf","from":"0","to":"100"})
        self.execute("manage_schema",{"operation":"create_index","table":"index_parent","name":"index_parent_unique","columns":["id","note"],"unique":True})
        self.execute("manage_schema",{**CREATE,"table":"index_ref"})
        self.execute("manage_schema",{"operation":"add_foreign_key","table":"index_ref","name":"leaf_reference","columns":["id","note"],"references_table":"index_leaf","references_columns":["id","note"]})
        values = {"table":"index_parent","name":"index_parent_unique"}
        result = pg.BUNDLED_TOOL.execute("drop_index",values,self.api)
        self.assertIsInstance(result,ActionFailed)
        self.assertIn("Foreign keys depend",result.error)
        self.approve("drop_constraint",{"table":"index_ref","constraint":"leaf_reference"})
        self.approve("drop_index",values)
        for table in ("index_ref","index_parent"):
            self.approve("drop_table",{"table":table})

    def test_recreated_rules_invalidate_pending_drop_approvals(self):
        self.execute("manage_schema",{**CREATE,"table":"recreated_rules"})
        required = {"operation":"set_not_null","table":"recreated_rules","columns":["note"]}
        self.execute("manage_schema",required)
        values = {"table":"recreated_rules","not_null_column":"note"}
        proposal = pg.BUNDLED_TOOL.execute("drop_constraint",values,self.api)
        stale = self.api.approvals.approve(proposal.approval_id)
        self.approve("drop_constraint",values)
        self.execute("manage_schema",required)
        self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(stale,self.api),ActionFailed)
        for create, action, drop in (
            ({"operation":"add_check","table":"recreated_rules","name":"note_check","columns":["note"],"check":{"not_blank":True}},
             "drop_constraint",{"table":"recreated_rules","constraint":"note_check"}),
            ({"operation":"create_index","table":"recreated_rules","name":"note_index","columns":["note"],"unique":False},
             "drop_index",{"table":"recreated_rules","name":"note_index"}),
        ):
            self.execute("manage_schema",create)
            proposal = pg.BUNDLED_TOOL.execute("drop_column",{"table":"recreated_rules","column":"note"},self.api)
            stale = self.api.approvals.approve(proposal.approval_id)
            self.approve(action,drop)
            self.execute("manage_schema",create)
            self.assertIsInstance(pg.BUNDLED_TOOL.execute_approved(stale,self.api),ActionFailed)
        self.approve("drop_table",{"table":"recreated_rules"})

    def test_allowed_values_permits_explicit_empty_string(self):
        self.execute("manage_schema",{**CREATE,"table":"allowed_strings"})
        self.execute("manage_schema",{"operation":"add_check","table":"allowed_strings","name":"note_enum","columns":["note"],"check":{"allowed_values":["","known"]}})
        self.execute("insert_rows",{**INSERT,"table":"allowed_strings","rows":[["1",""],["2","known"],["3",None]]})
        self.fails("insert_rows",{**INSERT,"table":"allowed_strings","rows":[["4","unknown"]]})
        self.assertEqual(self.execute("query",{"sql":"SELECT note FROM allowed_strings ORDER BY id","params":[]})["rows"],[[""],["known"],[None]])
        self.approve("drop_table",{"table":"allowed_strings"})

    def test_large_deleted_row_is_capped_before_transport(self):
        self.execute("manage_schema",{**CREATE,"table":"wide_row"})
        self.execute("insert_rows",{**INSERT,"table":"wide_row"})
        # Multiple individually bounded inserts cannot fill a >60KB row, but the
        # read path must still cap results if column limits are later removed.
        owner = connection("kern_ddl")
        try:
            for i in range(13):
                owner.execute(f"ALTER TABLE agent_data.wide_row ADD COLUMN c{i} text")
                owner.execute(f"UPDATE agent_data.wide_row SET c{i}=%s",["x"*5000])
        finally:
            owner.close()
        result = self.execute("delete_row",{"table":"wide_row","key":[{"column":"id","value":"1"}]})
        self.assertEqual(result,{"deleted":1,"row":None})
        self.assertEqual(self.execute("query",{"sql":"SELECT count(*) FROM wide_row","params":[]})["rows"],[["0"]])
        self.approve("drop_table",{"table":"wide_row"})

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
