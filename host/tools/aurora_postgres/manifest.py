"""Six actions, exact schemas, and the Home > Integrations setup guide."""
from __future__ import annotations

from typing import cast
from host.tools.json_types import JSONObject, JSONValue
from host.tools.manifest import ActionSpec, ConfigRequirement, DataSummary, DataSummaryCard, DataSummaryLink, SetupStep, ToolManifest, protect_inputs, validated_input
from host.tools.shared.inputs import schema
from host.tools.shared import outputs
from .client import CONFIG_KEYS, PREFIX
from .setup import IAM_POLICY, SETUP_COMMAND
from .sql import TYPES, OPERATIONS

IDENTIFIER: JSONObject = {"type":"string", "description":"Unqualified lowercase ASCII name: letter first, then letters, digits or underscores; at most 63 bytes."}
CELL: JSONObject = {"oneOf":[{"type":"string",}, {"type":"null"}], "description":"SQL NULL or text representation of a typed value. Database column byte constraints apply to inserts. JSONB is encoded JSON text; booleans use true/false; decimals use decimal strings."}
COLUMNS: JSONObject = {"type":"array", "items":IDENTIFIER, "minItems":1, "maxItems":60, "description":"1-60 distinct column names, in order."}
COLUMN = schema({"name":IDENTIFIER, "type":{"type":"string", "enum":cast(list[JSONValue], list(TYPES)), "description":"Supported PostgreSQL built-in type."},
                 "nullable":{"type":"boolean", "description":"Whether SQL NULL is allowed. Primary keys are always NOT NULL."},
                 "max_bytes":{"type":"integer", "description":"Optional text/JSONB database CHECK limit in UTF-8 bytes, default 5120. Other types reject this field."}}, ["name","type","nullable"])
COLUMN["description"] = "Column definition. No defaults, expressions, generated columns, triggers or user-defined types."
SCHEMA_INPUT = schema({
    "operation":{"type":"string", "enum":cast(list[JSONValue], list(OPERATIONS)), "description":"One operation. Only the fields documented for that operation are accepted."},
    "table":IDENTIFIER,
    "columns":{"oneOf":[{"type":"array","items":COLUMN,"minItems":1,"maxItems":60}, COLUMNS], "description":"create_table: column definitions. create_index: column names. Required for those two operations only."},
    "primary_key":{**COLUMNS, "description":"create_table only, required. Names of the primary key columns."},
    "partition_by":{**IDENTIFIER, "description":"create_table only, optional RANGE key. A date, timestamptz, integer or bigint column included in primary_key."},
    "column":{**COLUMN, "description":"add_column only, required. Must be nullable; existing values become NULL."},
    "name":{**IDENTIFIER, "description":"create_index only, required index name."},
    "unique":{"type":"boolean", "description":"create_index only, required. Enforce uniqueness when true."},
    "partition":{**IDENTIFIER,"description":"create_partition or detach_partition only, required child name."},
    "from":{"type":"string","description":"create_partition only, required inclusive lower bound as integer, ISO date or timestamp text."},
    "to":{"type":"string","description":"create_partition only, required exclusive upper bound as integer, ISO date or timestamp text."},
}, ["operation","table"])
TABULAR_OUTPUT = outputs.obj({
    "columns": outputs.array_of(outputs.obj({"name":outputs.text("Column label; duplicates preserved by position."),"type":outputs.text("PostgreSQL result type name.")},["name","type"]), "Result columns, in order."),
    "rows":outputs.array_of({"type":"array","items":outputs.nullable(outputs.text("Exact text representation; decimal and bigint precision preserved."),"SQL NULL or text.")},"Rows in column order. Whole rows only; values are never clipped."),
    "truncated":outputs.boolean("More rows exist or the 64 KiB tool output budget was reached. Narrow the query or use stable keyset pagination."),
}, ["columns","rows","truncated"])
MESSAGE_OUTPUT = outputs.obj({"message":outputs.text("Operation completed and transaction committed.")},["message"])
COST = "AWS bills Data API requests and payload units, Secrets Manager, database compute, storage and I/O separately. Kern does not report a dollar amount because region, workload and AWS pricing determine it. Not free."
DESTINATION = "Requests use the configured cluster, database, schema and role secret at the AWS regional Data API endpoint. "
VALID_NAME = validated_input("Validated lowercase ASCII identifier, 1-63 bytes; schema comes only from configuration.")

ACTIONS = (
    ActionSpec(id="list_tables", description="List top-level tables, approximate row counts and sizes including their partitions.", data_policy=DESTINATION+"Sends fixed catalog SQL directly as reader and returns metadata, never table contents.", input_schema=schema({}), output_schema=TABULAR_OUTPUT, cost_description=COST),
    ActionSpec(id="describe_table", description="Read a table's columns, indexes and constraints.", data_policy=DESTINATION+"Sends the table name and fixed catalog SQL directly as reader; returns schema definitions.", input_schema=schema({"table":IDENTIFIER},["table"]), output_schema=TABULAR_OUTPUT, cost_description=COST),
    ActionSpec(id="query", description="Run one parameterized SELECT or WITH query in a read-only reader transaction; at most 200 rows and 64 KiB returned.", data_policy=DESTINATION+"Sends SQL and parameter values directly as reader. Returned database contents enter agent context.", input_schema=schema({
        "sql":{"type":"string","description":"One SELECT/WITH query within the 64 KiB action input limit. No semicolons or SQL comments, even inside literals; use parameters for those values. Named placeholders such as :ticker. Reader privileges and a read-only transaction enforce database writes being denied."},
        "params":{"type":"array","maxItems":60,"items":schema({"name":IDENTIFIER,"value":CELL},["name","value"]),"description":"Required, possibly empty, list of unique named text-or-null parameters. Use explicit SQL casts where needed."},
        "limit":{"type":"integer","description":"Optional result row limit, default 200. Use ORDER BY and keyset conditions for paging."}
    },["sql","params"]), output_schema=TABULAR_OUTPUT, cost_description=COST),
    ActionSpec(id="insert_rows", description="Insert 1-500 rows atomically; existing rows are never updated. Whole input at most 64 KiB, each row at most 32 KiB.", data_policy=DESTINATION+"Sends table, columns and values directly as writer to one parameterized INSERT. Writes persist until approved table deletion or operator maintenance.", input_schema=schema({
        "table":IDENTIFIER,"columns":COLUMNS,
        "rows":{"type":"array","minItems":1,"maxItems":500,"items":{"type":"array","minItems":1,"maxItems":60,"items":CELL},"description":"Rows aligned with columns. Each cell is text or null; each serialized row at most 32768 UTF-8 bytes. Omitted columns use database defaults. No silent splitting or truncation."},
        "on_conflict":{"type":"string","enum":["error","ignore"],"description":"Required. error rolls back the whole batch; ignore uses ON CONFLICT DO NOTHING and reports inserted/skipped counts. Supply stable unique keys before retrying an uncertain insert."}
    },["table","columns","rows","on_conflict"]),output_schema=outputs.obj({"inserted":outputs.integer("Rows actually inserted."),"skipped":outputs.integer("Rows skipped by ON CONFLICT DO NOTHING.")},["inserted","skipped"]),cost_description=COST),
    ActionSpec(id="manage_schema", description="Create tables or indexes, add nullable columns, create RANGE partitions or detach partitions. Uses structured fields, never arbitrary DDL SQL.",data_policy=DESTINATION+"Sends validated names, column definitions and partition bounds as generated DDL directly. Detaching preserves the child table's rows but removes them from parent queries.",input_schema=SCHEMA_INPUT,output_schema=MESSAGE_OUTPUT,cost_description=COST),
    ActionSpec(id="drop_table", description="Request approval to drop one table, including all current rows and attached partitions. Never CASCADE.",data_policy=DESTINATION+"Reads the table and partition identities as reader before approval. Only approved execution uses the dropper secret. It locks and rechecks the same target before DROP TABLE RESTRICT; all rows present at execution are removed.",input_schema=schema({"table":IDENTIFIER},["table"]),approval="operator",cost_description=COST,limit_runs_per_day=20),
)
PROTECTIONS = {
    "describe_table":{"table":VALID_NAME},
    "query":{"sql":validated_input("One SELECT/WITH statement; no semicolons or comments. Sent to your configured database without content scanning."),"params":validated_input("At most 60 unique named text-or-null parameters. Values are sent unchanged to your configured database without content scanning."),"limit":validated_input("Integer from 1 to 200, default 200.")},
    "insert_rows":{"table":VALID_NAME,"columns":VALID_NAME,"rows":validated_input("1-500 rows of text-or-null cells aligned with columns; each serialized row at most 32 KiB, whole input at most 64 KiB. Values are sent unchanged without content scanning; database column constraints apply."),"on_conflict":validated_input("Required error or ignore; never an update.")},
    "manage_schema":{key:validated_input("Structured operation grammar with validated identifiers, built-in types, booleans, bounded integers and restricted date/integer bounds. Irrelevant fields are rejected.") for key in cast(JSONObject, SCHEMA_INPUT["properties"])},
}
MANIFEST = ToolManifest(
    tool_id="aurora_postgres", display_name="Aurora PostgreSQL",
    description="Let agents store and query data in your Aurora PostgreSQL database, create tables and manage partitions. Dropping a table requires your approval.",
    connection="enable_only", actions=protect_inputs(ACTIONS, PROTECTIONS), reports_cost=False,
    config=tuple(ConfigRequirement(PREFIX+key, {
        "AWS_ACCESS_KEY_ID":"Dedicated IAM access key ID for this cluster and the four role secrets.",
        "AWS_SECRET_ACCESS_KEY":"Matching IAM secret access key; stored write-only.",
        "AWS_REGION":"Commercial AWS region supporting Data API for the selected Aurora PostgreSQL version, for example us-east-1.",
        "CLUSTER_ARN":"Exact Aurora cluster ARN with Data API enabled. Supports Express and private VPC clusters.",
        "DATABASE":"Dedicated database name, for example agent_store.",
        "SCHEMA":"Dedicated schema name from bootstrap, for example agent_data.",
        "READER_SECRET_ARN":"Secret ARN containing username kern_reader and its password.",
        "WRITER_SECRET_ARN":"Secret ARN containing username kern_writer and its password.",
        "DDL_SECRET_ARN":"Secret ARN containing username kern_ddl and its password.",
        "DROPPER_SECRET_ARN":"Secret ARN containing username kern_dropper and its password. Used only after approval.",
    }[key]) for key in CONFIG_KEYS),
    protections=("Reader and insert-only writer have separate database grants. Direct schema changes use a fixed grammar; no updates, row deletes, truncate, arbitrary functions or extensions are exposed as write actions.",
        "A master-owned event trigger blocks drops by direct roles using the authenticated session identity. A separate dropper role is available only through approved execution. No agent-settable approval flag.",
        "Drop approval binds connection configuration, table OID and attached partition OIDs. Execution locks and rechecks them; dependent objects outside the table prevent the drop. Newly inserted rows are included in the approved deletion.",
        "Tool-created text/JSONB columns have database byte constraints. Tool calls cap batches, rows, columns and results. Schema operations serialize quota checks: 100 top-level tables, 1000 tables including partitions, 60 live columns per table.",
        "These are tool limits, not a database storage quota. External SQL can exceed them. AWS storage grows with usage; configure budget and capacity alarms."),
    technical_details=(COST, "Fixed HTTPS POST paths /Execute, /BeginTransaction, /CommitTransaction and /RollbackTransaction on rds-data.<region>.amazonaws.com, signed with AWS SigV4. No Secrets Manager API calls from Kern; AWS Data API resolves the supplied secret ARN.",
        "Each operation uses one Data API transaction with a default 20 second statement timeout and 5 second lock timeout. Requests have a 50 second transport timeout; no automatic retries. AWS limits also apply, including a 64 KiB row and 1 MiB result limit. Oversize provider results fail; the tool never claims they are complete.",
        "SQL query parameters and insert cells are text or null; PostgreSQL casts them to column types. Results preserve decimal/bigint precision as text, with column type metadata. Cast arrays or unsupported types to text explicitly. Read privileges also allow PostgreSQL system catalog visibility.",
        "One configured database/schema per Kern integration. Use a dedicated database, keep default PUBLIC grants restricted, and do not install public SECURITY DEFINER functions or mutating triggers accessible to these roles. External ingestion uses its own insert-only credentials and is outside this tool's quotas.",
        "Partition creation and detachment are explicit agent operations. No automatic retention, background maintenance, pg_partman or pg_cron is installed. Detaching never archives data or frees storage."),
    agent_notes="Call list_tables and describe_table before writing. Use structured manage_schema fields, not SQL. Inserts require explicit on_conflict and unique keys for retry safety. query accepts SELECT/WITH with named :parameters; params is required, use [] if empty. SQL strings cannot contain comments or semicolons; bind such text as parameters. Outputs are positional text/null rows with type metadata. No update_rows/delete_rows/truncate. If no operation covers a need, report the limitation. Never retry uncertain writes blindly; inspect first. Scheduled ingestion/partition creation uses existing Kern schedules, not tool background jobs.",
    setup_steps=(
        SetupStep("Create a new Aurora cluster dedicated to Kern", "In AWS RDS use Create with express configuration for an Aurora PostgreSQL Serverless cluster in a Data API supported region. Name it kern-agent-store. Express uses IAM admin authentication through AWS's internet access gateway; no master password or master secret is needed. Keep the initial postgres database; the setup command creates agent_store. Start with 0.5 minimum and 4 maximum ACUs if appropriate for your workload. A zero minimum allows pausing and adds resume latency. Configure backups, deletion protection and AWS Budgets/storage/capacity alarms. Extra reader instances, I/O and storage cost extra.", "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/CHAP_GettingStartedAurora.AuroraPostgreSQL.ExpressConfig.html", "AWS Express configuration"),
        SetupStep("Alternative: create a private VPC cluster", "If you need private networking, use Create with full configuration (Standard create): Aurora PostgreSQL Serverless v2, initial database agent_store, encryption enabled, public access off and Data API enabled. Select Manage master credentials in AWS Secrets Manager with the default Secrets Manager encryption key. This path uses the generated master secret only for setup; keep it out of Kern. The command supports both creation paths.", "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/rds-secrets-manager.html", "AWS managed master credentials"),
        SetupStep("Copy the cluster ARN", "Wait until the cluster is Available, then open RDS > Databases > your cluster > Configuration and copy its cluster ARN. For Express, this is the only ARN needed for setup. For a private VPC cluster, also copy Master Credentials ARN; you do not need to retrieve its password.", "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.html", "AWS Data API"),
        SetupStep("Prepare Terminal on your Mac", "Install Python 3.10 or newer and the latest AWS CLI v2. For Express also install the PostgreSQL psql client version 17 or newer, with system CA certificates, and put psql on PATH. If you use Homebrew: brew install libpq, then export PATH=\"$(brew --prefix libpq)/bin:$PATH\". Sign in to AWS CLI with your existing AWS admin access. Run the checks below and confirm the AWS account contains your cluster. Express setup uses this session to describe the cluster, enable Data API, connect as the database master via rds-db:connect and create four Secrets Manager entries. It generates the temporary database login token automatically; you never copy or save it in Kern. Keep AWS admin credentials out of Kern.", "https://www.postgresql.org/download/macosx/", "PostgreSQL clients for macOS", code="python3 --version\naws --version\naws sts get-caller-identity\n# Express only:\npsql --version"),
        SetupStep("Copy and run the setup command", "Paste the complete block below into Terminal on your Mac. Enter the region and cluster ARN. At Master secret ARN, press Enter for Express; for a private VPC cluster paste its master secret ARN. Accept agent_store and agent_data as database/schema defaults. Express setup verifies the IAM connection with TLS, enables Data API if needed and creates a fresh agent_store database; it stops if that database already exists. Both paths generate four passwords, create their Secrets Manager entries, and install roles, grants and drop protection atomically. Kern's runtime uses these restricted role secrets through Data API, not your admin login. No repository checkout is needed. The command prints connection values, never passwords or login tokens. On failure, inspect the database, secrets and roles before rerunning; a timed-out call may have committed.", code=SETUP_COMMAND),
        SetupStep("Create a scoped IAM key", "Create a dedicated IAM user with the policy below. Replace the cluster ARN placeholder and all four role secret ARN placeholders with your actual values. Each placeholder is on its own line; use the role secret ARNs printed by the setup command. Generate an access key for Kern. If the secrets use a customer-managed KMS key, also grant kms:Decrypt on that exact key. Do not grant database creation, Secrets Manager writes, broad AWS access or the master secret.", "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.access.html", "AWS Data API permissions", code=IAM_POLICY),
        SetupStep("Connect in Kern", "Save the IAM key pair, region, cluster ARN, database, schema and four role secret ARNs below, then enable the tool. These values are write-only; AWS keeps role passwords in Secrets Manager. Changes to destination or role-secret selection invalidate pending drops. Rotate role passwords in PostgreSQL and Secrets Manager together.", show_config=True),
        SetupStep("Verify before enabling ingestion", "Ask an agent to list tables, create the example table, insert one row and query it. Verify a repeated insertion with on_conflict=ignore inserts zero rows. Finally request drop_table for this disposable table and approve it in Kern; this tests the separate drop credential and approval path. A saved key is not proof of AWS access. No live cluster test is performed merely by enabling the tool.", code='manage_schema: {"operation":"create_table","table":"setup_probe","columns":[{"name":"id","type":"bigint","nullable":false},{"name":"note","type":"text","nullable":true,"max_bytes":200}],"primary_key":["id"]}\ninsert_rows: {"table":"setup_probe","columns":["id","note"],"rows":[["1","Setup verified"]],"on_conflict":"ignore"}\nquery: {"sql":"SELECT id, note FROM setup_probe ORDER BY id","params":[],"limit":10}\ndrop_table: {"table":"setup_probe"}'),
    ),
    data_summary=DataSummary(cards=(
        DataSummaryCard("What leaves this host", "SQL, schema definitions, table names and inserted/parameter values go to your configured AWS Aurora database. AWS authentication uses the configured IAM key and secret ARNs. Read results return to the calling agent."),
        DataSummaryCard("Where it can go", "Only the configured AWS regional RDS Data API endpoint receives tool requests. Express clusters also expose AWS's IAM-authenticated internet access gateway, used by the operator's setup command. Full-configuration clusters can remain private in your VPC. Kern uses Data API HTTPS access for both.", links=(DataSummaryLink("AWS Data API", "https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/data-api.html"),)),
        DataSummaryCard("What AWS can do with it", "AWS processes queries and stores your data under your AWS agreement. You control database grants and IAM permissions. Tool access is available to agents when the integration is enabled; this is not per-agent data isolation.", links=(DataSummaryLink("AWS Privacy Notice", "https://aws.amazon.com/privacy/"),)),
        DataSummaryCard("How long AWS retains it", "Rows persist until an approved table drop or operator maintenance. Backups, snapshots and logs follow your AWS retention settings and may outlive a table drop. Detaching a partition retains all its data."),
    )),
)
