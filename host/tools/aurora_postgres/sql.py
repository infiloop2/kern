"""Small structured DDL grammar. No agent SQL is ever run as a table owner."""
from __future__ import annotations

import json
import re
from typing import NoReturn, cast

from host.tools.json_types import JSONObject, JSONValue
from host.tools.shared.inputs import ToolInputValidationError

MAX_COLUMNS = 60
MAX_TABLES = 100
MAX_RELATIONS = 1000  # Includes partitions, separately from top-level tables.
MAX_CELL_BYTES = 5120
MAX_ROW_BYTES = 32768
MAX_INPUT_BYTES = 65536
TYPES = ("text", "boolean", "smallint", "integer", "bigint", "numeric", "double precision", "date", "timestamptz", "uuid", "jsonb")
OPERATIONS = ("create_table", "add_column", "create_index", "create_partition", "detach_partition")


def fail(message: str) -> NoReturn:
    raise ToolInputValidationError(message)


def identifier(value: JSONValue, label: str = "Identifier") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value):
        fail(f"{label} must be 1-63 lowercase ASCII letters, digits or underscores, starting with a letter.")
    return cast(str, value)


def quoted(value: str) -> str:
    return '"' + identifier(value) + '"'


def relation(schema: str, table: str) -> str:
    return f"{quoted(schema)}.{quoted(table)}"


def fields(value: JSONObject, allowed: set[str], required: set[str]) -> None:
    if set(value) - allowed or required - set(value):
        fail("Unsupported or missing fields. Use the exact action input schema.")


def names(value: JSONValue, label: str) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_COLUMNS:
        fail(f"{label} requires 1-60 column names.")
    result = [identifier(item, label) for item in cast(list[JSONValue], value)]
    if len(set(result)) != len(result):
        fail(f"{label} must not repeat column names.")
    return result


def column(value: JSONValue) -> tuple[str, str]:
    if not isinstance(value, dict):
        fail("Each column must be an object.")
    obj = cast(JSONObject, value)
    fields(obj, {"name", "type", "nullable", "max_bytes"}, {"name", "type", "nullable"})
    name = identifier(obj["name"], "Column name")
    kind = obj["type"]
    if kind not in TYPES or not isinstance(obj["nullable"], bool):
        fail("Use a supported column type and a boolean nullable value.")
    definition = f"{quoted(name)} {kind}" + ("" if obj["nullable"] else " NOT NULL")
    if kind in ("text", "jsonb"):
        maximum = obj.get("max_bytes", MAX_CELL_BYTES)
        if type(maximum) is not int or not 1 <= cast(int, maximum) <= MAX_CELL_BYTES:
            fail("max_bytes must be an integer from 1 to 5120.")
        definition += f" CHECK (octet_length({quoted(name)}::text) <= {maximum})"
    elif "max_bytes" in obj:
        fail("max_bytes applies only to text and jsonb columns.")
    return name, definition


def ddl(values: JSONObject, schema: str) -> tuple[str, str, str]:
    """Return operation, table and generated SQL; reject irrelevant fields."""
    op = values.get("operation")
    table = identifier(values.get("table"), "Table")
    target = relation(schema, table)
    base = {"operation", "table"}
    if op == "create_table":
        fields(values, base | {"columns", "primary_key", "partition_by"}, base | {"columns", "primary_key"})
        raw = values["columns"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_COLUMNS:
            fail("Create a table with 1-60 columns.")
        definitions = [column(item) for item in cast(list[JSONValue], raw)]
        colnames = [name for name, _ in definitions]
        if len(set(colnames)) != len(colnames):
            fail("Duplicate column names.")
        primary = names(values["primary_key"], "primary_key")
        if not set(primary) <= set(colnames):
            fail("primary_key must refer to declared columns.")
        parts = [definition for _, definition in definitions]
        parts.append("PRIMARY KEY (" + ",".join(map(quoted, primary)) + ")")
        statement = f"CREATE TABLE {target} (" + ",".join(parts) + ")"
        if "partition_by" in values:
            partition = identifier(values["partition_by"], "partition_by")
            kinds = {cast(str, cast(JSONObject, c)["name"]): cast(JSONObject, c)["type"] for c in cast(list[JSONValue], raw)}
            if partition not in primary or kinds.get(partition) not in ("date", "timestamptz", "integer", "bigint"):
                fail("partition_by must be a date, timestamptz, integer or bigint column included in primary_key.")
            statement += f" PARTITION BY RANGE ({quoted(partition)})"
    elif op == "add_column":
        fields(values, base | {"column"}, base | {"column"})
        _, definition = column(values["column"])
        if not cast(JSONObject, values["column"])["nullable"]:
            fail("Added columns must be nullable so existing rows remain valid.")
        statement = f"ALTER TABLE {target} ADD COLUMN {definition}"
    elif op == "create_index":
        fields(values, base | {"name", "columns", "unique"}, base | {"name", "columns", "unique"})
        index = identifier(values["name"], "Index name")
        cols = names(values["columns"], "Index columns")
        if not isinstance(values["unique"], bool):
            fail("unique must be a boolean.")
        statement = f"CREATE {'UNIQUE ' if values['unique'] else ''}INDEX {quoted(index)} ON {target} ({','.join(map(quoted, cols))})"
    elif op == "create_partition":
        fields(values, base | {"partition", "from", "to"}, base | {"partition", "from", "to"})
        partition = relation(schema, identifier(values["partition"], "Partition"))
        # DDL cannot use bind parameters here. This narrow literal grammar covers
        # integer, date and ISO timestamp bounds; no quotes or SQL expressions.
        bounds: list[str] = []
        for key in ("from", "to"):
            value = values[key]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9T Z:+.\-]{1,40}", value):
                fail("Partition bounds must be integer, ISO date or ISO timestamp strings (at most 40 ASCII characters).")
            bounds.append("'" + cast(str, value) + "'")
        statement = f"CREATE TABLE {partition} PARTITION OF {target} FOR VALUES FROM ({bounds[0]}) TO ({bounds[1]})"
    elif op == "detach_partition":
        fields(values, base | {"partition"}, base | {"partition"})
        partition = relation(schema, identifier(values["partition"], "Partition"))
        statement = f"ALTER TABLE {target} DETACH PARTITION {partition}"
    else:
        fail("Unsupported schema operation. Use create_table, add_column, create_index, create_partition or detach_partition.")
    return cast(str, op), table, statement


def json_bytes(value: JSONValue) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, UnicodeError):
        fail("Inputs must contain valid finite JSON and UTF-8 text.")
    raise AssertionError("unreachable")


# All catalog values are parameters; only tool-validated identifiers enter DDL.
TABLE_TARGET = """
SELECT c.oid::text, c.relkind::text, c.relispartition::text,
       (SELECT coalesce(jsonb_agg(p.relid::text ORDER BY p.relid::text), '[]'::jsonb)::text
        FROM pg_catalog.pg_partition_tree(c.oid) p) AS tree
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname=:schema AND c.relname=:table AND c.relkind IN ('r','p')
"""
TABLE_COUNTS = """
SELECT count(*) FILTER (WHERE NOT c.relispartition)::text, count(*)::text
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname=:schema AND c.relkind IN ('r','p')
"""
LIST_TABLES = """
SELECT c.relname AS table_name, c.relkind='p' AS partitioned,
       (SELECT greatest(count(*)-1,0) FROM pg_catalog.pg_partition_tree(c.oid)) AS partition_count,
       CASE WHEN c.relkind='p' THEN
         (SELECT sum(pg_catalog.pg_total_relation_size(p.relid)) FROM pg_catalog.pg_partition_tree(c.oid) p)
         ELSE pg_catalog.pg_total_relation_size(c.oid) END AS total_bytes,
       CASE WHEN c.relkind='p' THEN
         (SELECT sum(greatest(d.reltuples,0))::bigint FROM pg_catalog.pg_partition_tree(c.oid) p
          JOIN pg_catalog.pg_class d ON d.oid=p.relid WHERE p.isleaf)
         ELSE greatest(c.reltuples,0)::bigint END AS estimated_rows
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname=:schema AND c.relkind IN ('r','p') AND NOT c.relispartition
ORDER BY c.relname LIMIT 101
"""
DESCRIBE_TABLE = """
SELECT 'column' AS kind, a.attname::text AS name,
       pg_catalog.format_type(a.atttypid,a.atttypmod) ||
       CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE ' NULL' END ||
       coalesce(' DEFAULT ' || pg_catalog.pg_get_expr(d.adbin,d.adrelid),'') AS definition
FROM pg_catalog.pg_attribute a JOIN pg_catalog.pg_class c ON c.oid=a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN pg_catalog.pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
WHERE n.nspname=:schema AND c.relname=:table AND a.attnum>0 AND NOT a.attisdropped
UNION ALL
SELECT 'index', indexname, indexdef FROM pg_catalog.pg_indexes WHERE schemaname=:schema AND tablename=:table
UNION ALL
SELECT 'constraint', k.conname::text, pg_catalog.pg_get_constraintdef(k.oid)
FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_class c ON c.oid=k.conrelid
JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=:schema AND c.relname=:table
ORDER BY kind,name LIMIT 201
"""
