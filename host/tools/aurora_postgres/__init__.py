"""Generic PostgreSQL storage through Aurora's HTTPS Data API: inserts, single-row updates/deletes and approved drops."""
from __future__ import annotations

import json
import re
from typing import cast

from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ActionResult, ApprovalExecuted, ApprovalResult
from host.tools.shared.inputs import ToolInputValidationError
from .client import Client, Config, parameter, result_rows
from .diagnostics import invalid_response
from .manifest import MANIFEST
from .sql import (COLUMN_TARGET, CONSTRAINT_TARGET, DESCRIBE_TABLE, INDEX_TARGET, LIST_TABLES, NOT_NULL_TARGET, PRIMARY_KEY, TABLE_COUNTS, TABLE_TARGET,
    MAX_COLUMNS, MAX_INPUT_BYTES, MAX_RELATIONS, MAX_ROW_BYTES, MAX_TABLES, TYPES,
    ddl, fail, fields, identifier, json_bytes, names, quoted, relation)


TYPE_ALIASES = {"int2":"smallint", "int4":"integer", "int8":"bigint", "float8":"double precision", "bool":"boolean", "varchar":"text", "timestamp with time zone":"timestamptz"}
MAX_RETURNED_ROW_BYTES = 60000
# Approval-gated drop actions: payload kind and the catalog query binding its identity.
DROP_OBJECTS = {"index":INDEX_TARGET, "constraint":CONSTRAINT_TARGET, "not_null":NOT_NULL_TARGET, "column":COLUMN_TARGET}


def cell(value: JSONValue) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        fail("Each value must be text or SQL null. Encode numbers and JSON as text.")
    return cast(str, value)


def table_params(config: Config, table: str) -> list[JSONObject]:
    return [parameter("schema", config.schema), parameter("table", table)]


def target(client: Client, table: str) -> list[str | None]:
    rows = result_rows(client.execute(TABLE_TARGET, table_params(client.config, table)))
    if len(rows) != 1 or len(rows[0]) != 4:
        fail("Table does not exist in the configured schema. Call list_tables.")
    return rows[0]


def tabular(response: JSONObject, limit: int) -> JSONObject:
    raw_columns = response.get("columnMetadata", [])
    if not isinstance(raw_columns, list):
        invalid_response("AWS returned invalid column metadata.")
    if len(raw_columns) > MAX_COLUMNS:
        fail("Select at most 60 columns.")
    columns: list[JSONValue] = []
    for raw in raw_columns:
        if not isinstance(raw, dict):
            invalid_response("AWS returned invalid column metadata.")
        name, kind = raw.get("name", raw.get("label", "")), raw.get("typeName", "")
        if not isinstance(name, str) or not isinstance(kind, str):
            invalid_response("AWS returned invalid column metadata.")
        columns.append({"name":name, "type":kind})
    result: JSONObject = {"columns":columns, "rows":[], "truncated":False}
    if len(json_bytes(result)) > 65536:
        fail("Result metadata exceeds 64 KiB. Select fewer columns or shorter aliases.")
    rows = result_rows(response)
    included = cast(list[JSONValue], result["rows"])
    for row in rows:
        if len(row) != len(columns):
            invalid_response("AWS returned mismatched result columns and rows.")
        if len(included) == limit:
            result["truncated"] = True
            break
        included.append(cast(list[JSONValue], row))
        if len(json_bytes(result)) > 65536:
            included.pop()
            result["truncated"] = True
            break
    return result


def query_input(values: JSONObject) -> tuple[str, list[JSONObject], int]:
    fields(values, {"sql","params","limit"}, {"sql","params"})
    statement = values["sql"]
    if not isinstance(statement, str) or not re.match(r"\s*(SELECT|WITH)\b", statement, re.I):
        fail("query requires one SELECT or WITH statement.")
    if any(token in statement for token in (";", "--", "/*", "*/")):
        fail("SQL cannot contain semicolons or comments. Pass such text in params instead.")
    raw_params = values["params"]
    if not isinstance(raw_params, list) or len(raw_params) > 60:
        fail("params must be a list of at most 60 named parameters.")
    params: list[JSONObject] = []
    used: set[str] = set()
    for raw in cast(list[JSONValue], raw_params):
        if not isinstance(raw, dict):
            fail("Each parameter requires name and value.")
        obj = cast(JSONObject, raw)
        fields(obj, {"name","value"}, {"name","value"})
        name = identifier(obj["name"], "Parameter name")
        if name in used:
            fail("Duplicate parameter names.")
        used.add(name)
        params.append(parameter(name, cell(obj["value"])))
    limit = values.get("limit", 200)
    if type(limit) is not int or not 1 <= cast(int, limit) <= 200:
        fail("limit must be an integer from 1 to 200.")
    # Query is a subquery, not a concatenated statement list. READ ONLY plus
    # reader grants are the mutation boundary, not the leading-keyword check.
    statement = f"SELECT * FROM ({statement}) AS kern_result LIMIT {cast(int, limit)+1}"
    if len(statement.encode("utf-8")) > 65536:
        fail("Wrapped query exceeds AWS's SQL limit. Use a shorter query or bind values as parameters.")
    return statement, params, cast(int, limit)


def insert_input(values: JSONObject) -> tuple[str, list[str], list[list[str | None]], str]:
    fields(values, {"table","columns","rows","on_conflict"}, {"table","columns","rows","on_conflict"})
    table = identifier(values["table"], "Table")
    columns = names(values["columns"], "columns")
    raw_rows = values["rows"]
    if not isinstance(raw_rows, list) or not 1 <= len(raw_rows) <= 500:
        fail("rows must contain 1-500 rows.")
    rows: list[list[str | None]] = []
    for raw in cast(list[JSONValue], raw_rows):
        if not isinstance(raw, list) or len(raw) != len(columns):
            fail("Every row must have exactly one cell per supplied column.")
        if len(json_bytes(raw)) > MAX_ROW_BYTES:
            fail("A serialized row exceeds 32768 UTF-8 bytes. Store a smaller record.")
        rows.append([cell(value) for value in cast(list[JSONValue], raw)])
    conflict = values["on_conflict"]
    if conflict not in ("error","ignore"):
        fail("on_conflict must be error or ignore.")
    return table, columns, rows, cast(str, conflict)


def insert(client: Client, table: str, columns: list[str], rows: list[list[str | None]], conflict: str) -> JSONObject:
    table_name = relation(client.config.schema, table)
    # This also acquires ACCESS SHARE until commit, preventing table replacement
    # or type changes between reading types and inserting.
    metadata = client.execute(f"SELECT {','.join(map(quoted, columns))} FROM {table_name} LIMIT 0", [])
    raw_types = metadata.get("columnMetadata", [])
    if not isinstance(raw_types, list) or len(raw_types) != len(columns):
        invalid_response("Could not resolve insert columns. Call describe_table.")
    kinds: list[str] = []
    for item in raw_types:
        if not isinstance(item, dict):
            invalid_response("AWS returned invalid column metadata.")
        if not isinstance(item.get("typeName"), str):
            invalid_response("AWS returned invalid column metadata.")
        value = str(item.get("typeName", ""))
        value = TYPE_ALIASES.get(value, value)
        if value not in TYPES:
            fail("Insert column has an unsupported type. Use tool-created columns with supported built-in types.")
        kinds.append(value)
    params: list[JSONObject] = []
    groups: list[str] = []
    for i, row in enumerate(rows):
        placeholders: list[str] = []
        for j, cell_value in enumerate(row):
            name = f"v{i}_{j}"
            params.append(parameter(name, cell_value))
            placeholders.append(f"CAST(:{name} AS {kinds[j]})")
        groups.append("(" + ",".join(placeholders) + ")")
    statement = f"INSERT INTO {table_name} ({','.join(map(quoted,columns))}) VALUES " + ",".join(groups)
    if conflict == "ignore":
        statement += " ON CONFLICT DO NOTHING"
    if len(statement.encode()) > 65536:
        fail("Generated INSERT exceeds AWS's SQL limit. Use a smaller batch.")
    response = client.execute(statement, params)
    inserted = response.get("numberOfRecordsUpdated")
    if type(inserted) is not int or not 0 <= cast(int, inserted) <= len(rows):
        invalid_response("AWS did not return a valid insert count; the transaction will be rolled back.")
    return {"inserted":inserted, "skipped":len(rows)-cast(int, inserted)}


def delete_input(values: JSONObject) -> tuple[str, dict[str, str]]:
    fields(values, {"table","key"}, {"table","key"})
    table = identifier(values["table"], "Table")
    raw = values["key"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
        fail("key requires 1-32 primary key columns.")
    key: dict[str, str] = {}
    for item in cast(list[JSONValue], raw):
        if not isinstance(item, dict):
            fail("Each key entry requires column and value.")
        obj = cast(JSONObject, item)
        fields(obj, {"column","value"}, {"column","value"})
        name = identifier(obj["column"], "Key column")
        if name in key or not isinstance(obj["value"], str):
            fail("Key columns must be unique and every value must be text.")
        key[name] = cast(str, obj["value"])
    return table, key


def delete_row(client: Client, table: str, key: dict[str, str]) -> JSONObject:
    table_name = relation(client.config.schema, table)
    target(client, table)
    primary = result_rows(client.execute(PRIMARY_KEY, [parameter("relation", table_name)]))
    if not primary:
        fail("Table has no primary key; delete_row requires one.")
    kinds = {cast(str, name): TYPE_ALIASES.get(cast(str, kind), cast(str, kind)) for name, kind in primary}
    if set(kinds) != set(key):
        fail("key must name exactly the primary key columns: " + ", ".join(cast(str, row[0]) for row in primary) + ".")
    if not set(kinds.values()) <= set(TYPES):
        fail("Primary key has an unsupported type. Use tool-created columns with supported built-in types.")
    params = [parameter(f"k{i}", key[name]) for i, name in enumerate(kinds)]
    condition = " AND ".join(f"{quoted(name)}=CAST(:k{i} AS {kind})" for i, (name, kind) in enumerate(kinds.items()))
    # The supported schema is managed exclusively by this tool. Its foreign keys are RESTRICT,
    # so a referenced row fails instead of deleting or changing other rows.
    rows = result_rows(client.execute(f"DELETE FROM {table_name} AS kern_row WHERE {condition} RETURNING CASE WHEN octet_length(to_jsonb(kern_row)::text) <= {MAX_RETURNED_ROW_BYTES} THEN to_jsonb(kern_row)::text ELSE NULL END", params))
    if not rows:
        fail("No row has that primary key. Nothing was deleted.")
    if len(rows) != 1 or len(rows[0]) != 1 or (rows[0][0] is not None and not isinstance(rows[0][0], str)):
        invalid_response("AWS returned an unexpected delete result; the transaction will be rolled back.")
    return {"deleted":1, "row":rows[0][0]}


def update_input(values: JSONObject) -> tuple[str, dict[str, str], dict[str, str | None]]:
    fields(values, {"table","key","set"}, {"table","key","set"})
    table, key = delete_input({"table":values["table"], "key":values["key"]})
    raw = values["set"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_COLUMNS:
        fail("set requires 1-60 non-primary-key columns.")
    changes: dict[str, str | None] = {}
    for item in cast(list[JSONValue], raw):
        if not isinstance(item, dict):
            fail("Each set entry requires column and value.")
        fields(item, {"column","value"}, {"column","value"})
        name = identifier(item["column"], "Set column")
        if name in changes:
            fail("set must not repeat column names.")
        changes[name] = cell(item["value"])
    return table, key, changes


def update_row(client: Client, table: str, key: dict[str, str], changes: dict[str, str | None]) -> JSONObject:
    table_name = relation(client.config.schema, table)
    target(client, table)
    # Hold ACCESS SHARE until commit so column types and the primary key cannot
    # change between validation and the generated UPDATE.
    metadata = client.execute(f"SELECT {','.join(map(quoted, changes))} FROM {table_name} LIMIT 0", [])
    primary = result_rows(client.execute(PRIMARY_KEY, [parameter("relation", table_name)]))
    if not primary:
        fail("Table has no primary key; update_row requires one.")
    kinds = {cast(str, name): TYPE_ALIASES.get(cast(str, kind), cast(str, kind)) for name, kind in primary}
    if set(kinds) != set(key):
        fail("key must name exactly the primary key columns: " + ", ".join(kinds) + ".")
    if set(changes) & set(kinds):
        fail("Primary key columns cannot be updated. Supply only non-primary-key columns in set.")
    if not set(kinds.values()) <= set(TYPES):
        fail("Primary key has an unsupported type. Use tool-created columns with supported built-in types.")
    raw_types = metadata.get("columnMetadata", [])
    if not isinstance(raw_types, list) or len(raw_types) != len(changes):
        invalid_response("Could not resolve update columns. Call describe_table.")
    params = [parameter(f"k{i}", key[name]) for i, name in enumerate(kinds)]
    assignments: list[str] = []
    for i, ((name, value), item) in enumerate(zip(changes.items(), raw_types)):
        if not isinstance(item, dict) or not isinstance(item.get("typeName"), str):
            invalid_response("AWS returned invalid column metadata.")
        kind = str(item["typeName"])
        kind = TYPE_ALIASES.get(kind, kind)
        if kind not in TYPES:
            fail("Update column has an unsupported type. Use tool-created columns with supported built-in types.")
        params.append(parameter(f"v{i}", value))
        assignments.append(f"{quoted(name)}=CAST(:v{i} AS {kind})")
    condition = " AND ".join(f"{quoted(name)}=CAST(:k{i} AS {kind})" for i, (name, kind) in enumerate(kinds.items()))
    rows = result_rows(client.execute(f"UPDATE {table_name} AS kern_row SET {','.join(assignments)} WHERE {condition} RETURNING CASE WHEN octet_length(to_jsonb(kern_row)::text) <= {MAX_RETURNED_ROW_BYTES} THEN to_jsonb(kern_row)::text ELSE NULL END", params))
    if not rows:
        fail("No row has that primary key. Nothing was updated.")
    if len(rows) != 1 or len(rows[0]) != 1 or (rows[0][0] is not None and not isinstance(rows[0][0], str)):
        invalid_response("AWS returned an unexpected update result; the transaction will be rolled back.")
    return {"updated":1, "row":rows[0][0]}


def drop_object(values: JSONObject, action: str) -> tuple[str, str, str]:
    """Return table, object kind and name for an approved object drop."""
    if action == "drop_column":
        fields(values, {"table","column"}, {"table","column"})
        return identifier(values["table"], "Table"), "column", identifier(values["column"], "Column name")
    if action == "drop_index":
        fields(values, {"table","name"}, {"table","name"})
        return identifier(values["table"], "Table"), "index", identifier(values["name"], "Index name")
    fields(values, {"table","constraint","not_null_column"}, {"table"})
    if ("constraint" in values) == ("not_null_column" in values):
        fail("Supply exactly one of constraint or not_null_column.")
    if "constraint" in values:
        return identifier(values["table"], "Table"), "constraint", identifier(values["constraint"], "Constraint name")
    return identifier(values["table"], "Table"), "not_null", identifier(values["not_null_column"], "Column name")


def object_identity(client: Client, table: str, kind: str, name: str) -> list[str | None]:
    rows = result_rows(client.execute(DROP_OBJECTS[kind], [parameter("relation", relation(client.config.schema, table)), parameter("name", name)]))
    if len(rows) != 1 or len(rows[0]) != {"index":4,"constraint":3,"not_null":5,"column":4}[kind]:
        fail({"index":"Index", "constraint":"Constraint", "not_null":"Column", "column":"Column"}[kind] + " does not exist on that table. Call describe_table.")
    identity = rows[0]
    if kind == "index" and identity[1] == "true":
        fail("This index enforces a constraint. Use drop_constraint with the constraint name.")
    if kind == "index" and identity[2] == "true":
        fail("Attached partition indexes cannot be dropped directly. Request drop_index on the partitioned parent index instead.")
    if kind == "index" and identity[3] == "true":
        fail("Foreign keys depend on this index. Remove the referencing foreign key constraints first.")
    if kind == "constraint" and identity[1] == "p":
        fail("Primary keys cannot be dropped; inserts, update_row and delete_row depend on them.")
    if kind in ("not_null", "column") and identity[2] == "true":
        fail("Primary key columns cannot be dropped or made nullable.")
    if kind == "column" and identity[3] == "true":
        fail("Inherited columns cannot be dropped directly. Request drop_column on the partitioned parent instead.")
    if (kind == "constraint" and identity[2] == "true") or (kind == "not_null" and identity[3] == "true"):
        fail("Inherited constraints cannot be dropped directly. Request drop_constraint on the partitioned parent instead.")
    if kind == "not_null" and identity[1] != "true":
        fail("Column is already nullable.")
    return identity


def drop_statement(schema: str, table: str, kind: str, name: str) -> str:
    target = relation(schema, table)
    if kind == "column":
        return f"ALTER TABLE {target} DROP COLUMN {quoted(name)} RESTRICT"
    if kind == "index":
        return f"DROP INDEX {relation(schema, name)} RESTRICT"
    if kind == "constraint":
        return f"ALTER TABLE {target} DROP CONSTRAINT {quoted(name)} RESTRICT"
    return f"ALTER TABLE {target} ALTER COLUMN {quoted(name)} DROP NOT NULL"


def manage(client: Client, operation: str, table: str, statements: list[str]) -> JSONObject:
    # Transaction-scoped lock serializes tool DDL quota checks across agents.
    # Dedicated database means a fixed lock key does not couple other tenants.
    client.execute("SELECT pg_catalog.pg_advisory_xact_lock(171837, 1)", [])
    counts = result_rows(client.execute(TABLE_COUNTS, [parameter("schema", client.config.schema)]))
    if len(counts) != 1 or len(counts[0]) != 2:
        fail("Could not read schema size limits.")
    top, total = int(cast(str, counts[0][0])), int(cast(str, counts[0][1]))
    if operation in ("create_table","detach_partition") and top >= MAX_TABLES:
        fail("Schema already has 100 top-level tables. Review and retire unused tables through approved drops.")
    if operation in ("create_table","create_partition") and total >= MAX_RELATIONS:
        fail("Schema already has 1000 tables including partitions. Review retained partitions.")
    if operation != "create_table":
        target(client, table)
    if operation == "add_column":
        count_rows = result_rows(client.execute("""SELECT count(*)::text FROM pg_catalog.pg_attribute
WHERE attrelid=CAST(:table AS regclass) AND attnum>0 AND NOT attisdropped""", [parameter("table", relation(client.config.schema, table))]))
        if not count_rows or int(cast(str, count_rows[0][0])) >= MAX_COLUMNS:
            fail("Table already has 60 columns.")
    for statement in statements:
        client.execute(statement, [])
    return {"message":f"{operation} completed for {table}."}


class AuroraPostgresTool:
    @property
    def manifest(self):
        return MANIFEST

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        try:
            if action not in {a.id for a in MANIFEST.actions}:
                fail("Unknown Aurora PostgreSQL action.")
            if len(json_bytes(tool_input)) > MAX_INPUT_BYTES:
                fail("Action input exceeds 65536 UTF-8 bytes. Use a smaller batch.")
            config = Config.read(api)
            if action == "manage_schema":
                operation, table, statements = ddl(tool_input, config.schema)
                with Client(config, "ddl").transaction(False) as client:
                    result = manage(client, operation, table, statements)
                return ActionExecuted(result)
            if action == "update_row":
                table, key, changes = update_input(tool_input)
                # Only generated single-row writes run as owner; writer stays insert-only.
                with Client(config, "ddl").transaction(False) as client:
                    result = update_row(client, table, key, changes)
                return ActionExecuted(result)
            if action == "delete_row":
                table, key = delete_input(tool_input)
                # The schema owner runs only this generated single-row DELETE;
                # the writer role stays insert-only.
                with Client(config, "ddl").transaction(False) as client:
                    result = delete_row(client, table, key)
                return ActionExecuted(result)
            if action in ("drop_index", "drop_constraint", "drop_column"):
                table, kind, name = drop_object(tool_input, action)
                with Client(config, "reader").transaction(True) as client:
                    identity = target(client, table)
                    object_id = object_identity(client, table, kind, name)
                label = {"index":f"index {name} on", "constraint":f"constraint {name} on", "not_null":f"the NOT NULL rule on column {name} of", "column":f"column {name} from"}[kind]
                consequence = ("ALL values in this column at execution, including attached partitions, are deleted. Local indexes and constraints involving it are removed automatically. External dependencies block the drop; no CASCADE."
                               if kind == "column" else "Rows are kept; no CASCADE.")
                approval = api.approvals.request(action_id=action, summary=f"Remove {label} {config.schema}.{table}. {consequence}", payload={
                    "table":table, "kind":kind, "name":name, "binding":config.binding(),
                    "target":cast(list[JSONValue], identity), "object":cast(list[JSONValue], object_id)})
                return ActionPendingApproval(approval.approval_id, approval.summary)
            if action == "insert_rows":
                table, columns, rows, conflict = insert_input(tool_input)
                with Client(config, "writer").transaction(False) as client:
                    result = insert(client, table, columns, rows, conflict)
                return ActionExecuted(result)
            if action == "query":
                statement, params, limit = query_input(tool_input)
                with Client(config, "reader").transaction(True) as client:
                    result = tabular(client.execute(statement, params), limit)
                return ActionExecuted(result)
            table = ""
            if action == "list_tables":
                fields(tool_input, set(), set())
            else:
                fields(tool_input, {"table"}, {"table"})
                table = identifier(tool_input["table"], "Table")
            with Client(config, "reader").transaction(True) as client:
                if action == "list_tables":
                    result = tabular(client.execute(LIST_TABLES, [parameter("schema", config.schema)]), 100)
                elif action == "describe_table":
                    target(client, table)
                    result = tabular(client.execute(DESCRIBE_TABLE, table_params(config, table)), 200)
                else:
                    identity = target(client, table)
            if action == "drop_table":
                summary = f"Drop {config.schema}.{table}, including ALL rows present at execution and all attached partitions. Dependent external objects block deletion; no CASCADE."
                approval = api.approvals.request(action_id=action, summary=summary, payload={
                    "table":table, "binding":config.binding(), "target":cast(list[JSONValue], identity)})
                return ActionPendingApproval(approval.approval_id, approval.summary)
            return ActionExecuted(result)
        except ToolInputValidationError as exc:
            return ActionFailed(str(exc))

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            if approval.action_id not in ("drop_table", "drop_index", "drop_constraint", "drop_column") or approval.status != "approved":
                fail("Invalid Aurora PostgreSQL approval.")
            payload = approval.payload
            keys = {"table","binding","target"} | (set() if approval.action_id == "drop_table" else {"kind","name","object"})
            fields(payload, keys, keys)
            config = Config.read(api)
            if config.binding() != payload["binding"]:
                fail("Database configuration changed. Request a new drop approval.")
            table = identifier(payload["table"], "Table")
            kind, name = "", ""
            if approval.action_id != "drop_table":
                kind, name = cast(str, payload["kind"]), identifier(payload["name"], "Name")
                expected_kinds = {"drop_index":{"index"}, "drop_constraint":{"constraint","not_null"}, "drop_column":{"column"}}
                if kind not in expected_kinds[approval.action_id]:
                    fail("Invalid Aurora PostgreSQL approval.")
            with Client(config, "dropper").transaction(False) as client:
                client.execute("SELECT pg_catalog.pg_advisory_xact_lock(171837, 1)", [])
                client.execute(f"LOCK TABLE {relation(config.schema, table)} IN ACCESS EXCLUSIVE MODE", [])
                if target(client, table) != payload["target"]:
                    fail("Table or attached partitions changed. Request a new drop approval.")
                if approval.action_id == "drop_table":
                    client.execute(f"DROP TABLE {relation(config.schema, table)} RESTRICT", [])
                else:
                    if object_identity(client, table, kind, name) != payload["object"]:
                        fail("The index, constraint or column changed. Request a new drop approval.")
                    client.execute(drop_statement(config.schema, table, kind, name), [])
            if approval.action_id == "drop_table":
                return ApprovalExecuted(f"Dropped {config.schema}.{table} and its attached partitions. All their rows were removed; AWS backups follow your configured retention.")
            if kind == "column":
                return ApprovalExecuted(f"Dropped column {name} from {config.schema}.{table} and its attached partitions. All column values and local dependent indexes/constraints were removed.")
            return ApprovalExecuted(f"Removed {kind.replace('_', ' ')} {name} from {config.schema}.{table}. No rows were removed.")
        except ToolInputValidationError as exc:
            return ActionFailed(str(exc))


BUNDLED_TOOL = AuroraPostgresTool()
