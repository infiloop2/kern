"""Generic append-only PostgreSQL storage through Aurora's HTTPS Data API."""
from __future__ import annotations

import json
import re
from typing import cast

from host.param_guard import ParamGuardDenied
from host.tools.host_api import ApprovalRecord, HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.results import ActionExecuted, ActionFailed, ActionPendingApproval, ActionResult, ApprovalExecuted, ApprovalResult
from host.tools.shared.inputs import ToolInputValidationError
from .client import Client, Config, parameter, result_rows
from .manifest import MANIFEST
from .sql import (DESCRIBE_TABLE, LIST_TABLES, TABLE_COUNTS, TABLE_TARGET,
    MAX_COLUMNS, MAX_INPUT_BYTES, MAX_RELATIONS, MAX_ROW_BYTES, MAX_TABLES, TYPES,
    ddl, fail, fields, identifier, json_bytes, names, quoted, relation)


def guarded(value: str, api: HostAPI) -> str:
    return api.outbound.guard_request_parameter_string(value, allow_identifiers=True, allow_machine_tokens=True, allow_longer_text=True)


def cell(value: JSONValue, api: HostAPI) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        fail("Each value must be text or SQL null. Encode numbers and JSON as text.")
    return guarded(cast(str, value), api)


def table_params(config: Config, table: str) -> list[JSONObject]:
    return [parameter("schema", config.schema), parameter("table", table)]


def target(client: Client, table: str) -> list[str | None]:
    rows = result_rows(client.execute(TABLE_TARGET, table_params(client.config, table)))
    if len(rows) != 1 or len(rows[0]) != 4:
        fail("Table does not exist in the configured schema. Call list_tables.")
    return rows[0]


def tabular(response: JSONObject, limit: int) -> JSONObject:
    raw_columns = response.get("columnMetadata", [])
    if not isinstance(raw_columns, list) or len(raw_columns) > MAX_COLUMNS:
        fail("Select at most 60 columns.")
    columns: list[JSONValue] = []
    for raw in raw_columns:
        if not isinstance(raw, dict):
            fail("AWS returned invalid column metadata.")
        name, kind = raw.get("name", raw.get("label", "")), raw.get("typeName", "")
        if not isinstance(name, str) or not isinstance(kind, str):
            fail("AWS returned invalid column metadata.")
        columns.append({"name":name, "type":kind})
    result: JSONObject = {"columns":columns, "rows":[], "truncated":False}
    if len(json_bytes(result)) > 65536:
        fail("Result metadata exceeds 64 KiB. Select fewer columns or shorter aliases.")
    rows = result_rows(response)
    included = cast(list[JSONValue], result["rows"])
    for row in rows:
        if len(row) != len(columns):
            fail("AWS returned mismatched result columns and rows.")
        if len(included) == limit:
            result["truncated"] = True
            break
        included.append(cast(list[JSONValue], row))
        if len(json_bytes(result)) > 65536:
            included.pop()
            result["truncated"] = True
            break
    return result


def query_input(values: JSONObject, api: HostAPI) -> tuple[str, list[JSONObject], int]:
    fields(values, {"sql","params","limit"}, {"sql","params"})
    statement = values["sql"]
    if not isinstance(statement, str) or not re.match(r"\s*(SELECT|WITH)\b", statement, re.I):
        fail("query requires one SELECT or WITH statement.")
    statement = guarded(cast(str, statement), api)
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
        params.append(parameter(name, cell(obj["value"], api)))
    limit = values.get("limit", 200)
    if type(limit) is not int or not 1 <= cast(int, limit) <= 200:
        fail("limit must be an integer from 1 to 200.")
    # Query is a subquery, not a concatenated statement list. READ ONLY plus
    # reader grants are the mutation boundary, not the leading-keyword check.
    return f"SELECT * FROM ({statement}) AS kern_result LIMIT {cast(int, limit)+1}", params, cast(int, limit)


def insert_input(values: JSONObject, api: HostAPI) -> tuple[str, list[str], list[list[str | None]], str]:
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
        rows.append([cell(value, api) for value in cast(list[JSONValue], raw)])
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
        fail("Could not resolve insert columns. Call describe_table.")
    aliases = {"int2":"smallint", "int4":"integer", "int8":"bigint", "float8":"double precision", "bool":"boolean", "varchar":"text", "timestamp with time zone":"timestamptz"}
    kinds: list[str] = []
    for item in raw_types:
        if not isinstance(item, dict):
            fail("AWS returned invalid column metadata.")
        value = str(item.get("typeName", ""))
        value = aliases.get(value, value)
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
        fail("AWS did not return a valid insert count; the transaction will be rolled back.")
    return {"inserted":inserted, "skipped":len(rows)-cast(int, inserted)}


def manage(client: Client, operation: str, table: str, statement: str) -> JSONObject:
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
                operation, table, statement = ddl(tool_input, config.schema)
                with Client(config, "ddl").transaction(False) as client:
                    result = manage(client, operation, table, statement)
                return ActionExecuted(result)
            if action == "insert_rows":
                table, columns, rows, conflict = insert_input(tool_input, api)
                with Client(config, "writer").transaction(False) as client:
                    result = insert(client, table, columns, rows, conflict)
                return ActionExecuted(result)
            if action == "query":
                statement, params, limit = query_input(tool_input, api)
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
        except (ToolInputValidationError, ParamGuardDenied) as exc:
            return ActionFailed(str(exc))

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        try:
            if approval.action_id != "drop_table" or approval.status != "approved":
                fail("Invalid Aurora PostgreSQL approval.")
            fields(approval.payload, {"table","binding","target"}, {"table","binding","target"})
            config = Config.read(api)
            if config.binding() != approval.payload["binding"]:
                fail("Database configuration changed. Request a new drop approval.")
            table = identifier(approval.payload["table"], "Table")
            with Client(config, "dropper").transaction(False) as client:
                client.execute("SELECT pg_catalog.pg_advisory_xact_lock(171837, 1)", [])
                client.execute(f"LOCK TABLE {relation(config.schema, table)} IN ACCESS EXCLUSIVE MODE", [])
                if target(client, table) != approval.payload["target"]:
                    fail("Table or attached partitions changed. Request a new drop approval.")
                client.execute(f"DROP TABLE {relation(config.schema, table)} RESTRICT", [])
            return ApprovalExecuted(f"Dropped {config.schema}.{table} and its attached partitions. All their rows were removed; AWS backups follow your configured retention.")
        except (ToolInputValidationError, ParamGuardDenied) as exc:
            return ActionFailed(str(exc))


BUNDLED_TOOL = AuroraPostgresTool()
