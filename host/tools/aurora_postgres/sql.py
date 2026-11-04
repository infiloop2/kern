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
OPERATIONS = ("create_table", "add_column", "create_index", "create_partition", "detach_partition",
              "add_foreign_key", "add_check", "set_not_null")
MAX_ALLOWED_VALUES = 100
MAX_LITERAL_BYTES = 500
# Same narrow grammar as partition bounds: integers, decimals, ISO dates and timestamps.
BOUND = r"[0-9T Z:+.\-]{1,40}"
DOLLAR_TAG = "$kern_literal$"


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


def names(value: JSONValue, label: str, maximum: int = MAX_COLUMNS) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        fail(f"{label} requires 1-{maximum} column names.")
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


def bound(value: JSONValue, label: str) -> str:
    # DDL cannot use bind parameters. No quotes or SQL expressions pass this grammar.
    if not isinstance(value, str) or not re.fullmatch(BOUND, value):
        fail(f"{label} must be integer, decimal, ISO date or ISO timestamp text (at most 40 ASCII characters).")
    return "'" + cast(str, value) + "'"


def literal(value: JSONValue, label: str) -> str:
    """Dollar-quote agent text for DDL; the fixed tag cannot occur inside it."""
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_LITERAL_BYTES:
        fail(f"{label} must be text of at most {MAX_LITERAL_BYTES} UTF-8 bytes.")
    # The tag's prefix also ends it, so "$kern_literal" at the end would close early.
    if DOLLAR_TAG[:-1] in value or "\x00" in value:
        fail(f"{label} cannot contain {DOLLAR_TAG[:-1]} or NUL characters.")
    return DOLLAR_TAG + cast(str, value) + DOLLAR_TAG


def check_expression(value: JSONValue, name: str) -> tuple[str, list[str]]:
    """Return a CHECK expression and any statements that must succeed first."""
    if not isinstance(value, dict):
        fail("check must be an object.")
    rule = cast(JSONObject, value)
    fields(rule, {"pattern", "allowed_values", "min", "max", "not_blank"}, set())
    kinds = {"pattern" if "pattern" in rule else "", "allowed_values" if "allowed_values" in rule else "",
             "range" if "min" in rule or "max" in rule else "", "not_blank" if "not_blank" in rule else ""} - {""}
    if len(kinds) != 1:
        fail("check requires exactly one rule: pattern, allowed_values, min/max or not_blank.")
    target = quoted(name)
    if "pattern" in rule:
        if rule["pattern"] == "":
            fail("pattern must be non-empty.")
        pattern = literal(rule["pattern"], "pattern")
        # An empty table never evaluates the CHECK, so compile the regex now.
        return f"{target} ~ {pattern}", [f"SELECT '' ~ {pattern}"]
    if "allowed_values" in rule:
        raw = rule["allowed_values"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ALLOWED_VALUES:
            fail(f"allowed_values requires 1-{MAX_ALLOWED_VALUES} values.")
        values = [literal(item, "Each allowed value") for item in cast(list[JSONValue], raw)]
        if len(set(values)) != len(values):
            fail("allowed_values must not repeat values.")
        return f"{target} IN (" + ",".join(values) + ")", []
    if "not_blank" in rule:
        if rule["not_blank"] is not True:
            fail("not_blank must be true.")
        return f"{target} ~ '[^[:space:]]'", []
    limits = [f"{target} >= {bound(rule['min'], 'min')}"] if "min" in rule else []
    limits += [f"{target} <= {bound(rule['max'], 'max')}"] if "max" in rule else []
    return " AND ".join(limits), []


def ddl(values: JSONObject, schema: str) -> tuple[str, str, list[str]]:
    """Return operation, table and generated SQL statements; reject irrelevant fields."""
    op = values.get("operation")
    table = identifier(values.get("table"), "Table")
    target = relation(schema, table)
    base = {"operation", "table"}
    statements: list[str] = []
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
        bounds = [bound(values[key], "Partition bounds") for key in ("from", "to")]
        statement = f"CREATE TABLE {partition} PARTITION OF {target} FOR VALUES FROM ({bounds[0]}) TO ({bounds[1]})"
    elif op == "detach_partition":
        fields(values, base | {"partition"}, base | {"partition"})
        partition = relation(schema, identifier(values["partition"], "Partition"))
        statement = f"ALTER TABLE {target} DETACH PARTITION {partition}"
    elif op == "add_foreign_key":
        fields(values, base | {"name", "columns", "references_table", "references_columns"},
               base | {"name", "columns", "references_table", "references_columns"})
        name = identifier(values["name"], "Constraint name")
        cols = names(values["columns"], "Foreign key columns", 32)
        parent = relation(schema, identifier(values["references_table"], "references_table"))
        parent_cols = names(values["references_columns"], "references_columns", 32)
        if len(cols) != len(parent_cols):
            fail("columns and references_columns must have the same length.")
        # RESTRICT only: deleting a referenced row fails; nothing is cascaded or nulled.
        statement = (f"ALTER TABLE {target} ADD CONSTRAINT {quoted(name)} FOREIGN KEY ({','.join(map(quoted, cols))}) "
                     f"REFERENCES {parent} ({','.join(map(quoted, parent_cols))}) ON UPDATE RESTRICT ON DELETE RESTRICT")
    elif op == "add_check":
        fields(values, base | {"name", "columns", "check"}, base | {"name", "columns", "check"})
        name = identifier(values["name"], "Constraint name")
        cols = names(values["columns"], "Check columns")
        if len(cols) != 1:
            fail("add_check applies to exactly one column.")
        expression, statements = check_expression(values["check"], cols[0])
        statement = f"ALTER TABLE {target} ADD CONSTRAINT {quoted(name)} CHECK ({expression})"
    elif op == "set_not_null":
        fields(values, base | {"columns"}, base | {"columns"})
        cols = names(values["columns"], "Required columns")
        statement = f"ALTER TABLE {target} " + ", ".join(f"ALTER COLUMN {quoted(c)} SET NOT NULL" for c in cols)
    else:
        fail("Unsupported schema operation. Use " + ", ".join(OPERATIONS[:-1]) + " or " + OPERATIONS[-1] + ".")
    return cast(str, op), table, statements + [statement]



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
PRIMARY_KEY = """
SELECT a.attname::text, pg_catalog.format_type(a.atttypid,a.atttypmod)
FROM pg_catalog.pg_constraint k JOIN pg_catalog.pg_attribute a ON a.attrelid=k.conrelid AND a.attnum=ANY(k.conkey)
WHERE k.conrelid=CAST(:relation AS regclass) AND k.contype='p'
ORDER BY array_position(k.conkey,a.attnum)
"""
# Drop approvals bind these identities; execution rechecks them under lock.
INDEX_TARGET = """
SELECT i.indexrelid::text,
       EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conindid=i.indexrelid AND k.conrelid=i.indrelid AND k.contype <> 'f')::text,
       EXISTS (SELECT 1 FROM pg_catalog.pg_inherits h WHERE h.inhrelid=i.indexrelid)::text,
       EXISTS (
         WITH RECURSIVE index_tree(oid) AS (
           SELECT i.indexrelid
           UNION ALL SELECT h.inhrelid FROM pg_catalog.pg_inherits h JOIN index_tree t ON h.inhparent=t.oid
         )
         SELECT 1 FROM pg_catalog.pg_constraint k JOIN index_tree t ON k.conindid=t.oid
         WHERE k.contype='f')::text
FROM pg_catalog.pg_index i JOIN pg_catalog.pg_class c ON c.oid=i.indexrelid
WHERE i.indrelid=CAST(:relation AS regclass) AND c.relname=:name
"""
CONSTRAINT_TARGET = """
SELECT k.oid::text, k.contype::text, (k.coninhcount > 0 OR k.conparentid <> 0)::text
FROM pg_catalog.pg_constraint k
WHERE k.conrelid=CAST(:relation AS regclass) AND k.conname=:name
"""
NOT_NULL_TARGET = """
SELECT a.attnum::text, a.attnotnull::text,
       EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid=a.attrelid
               AND k.contype='p' AND a.attnum=ANY(k.conkey))::text,
       EXISTS (SELECT 1 FROM pg_catalog.pg_inherits i
               JOIN pg_catalog.pg_attribute parent ON parent.attrelid=i.inhparent
               WHERE i.inhrelid=a.attrelid AND parent.attname=a.attname
                 AND parent.attnotnull AND NOT parent.attisdropped)::text,
       a.xmin::text
FROM pg_catalog.pg_attribute a
WHERE a.attrelid=CAST(:relation AS regclass) AND a.attname=:name AND a.attnum>0 AND NOT a.attisdropped
"""

COLUMN_TARGET = """
WITH relations AS (
    SELECT CAST(:relation AS regclass)::oid AS relid
    UNION SELECT p.relid::oid FROM pg_catalog.pg_partition_tree(CAST(:relation AS regclass)) p
), columns AS (
    SELECT a.*, a.xmin::text AS version FROM relations r JOIN pg_catalog.pg_attribute a ON a.attrelid=r.relid
    WHERE a.attname=:name AND a.attnum>0 AND NOT a.attisdropped
)
SELECT a.attnum::text,
       md5((SELECT jsonb_agg(jsonb_build_array(c.attrelid, c.attnum, c.version, c.atttypid,
         c.atttypmod, c.attcollation, c.attnotnull,
         (SELECT jsonb_agg(jsonb_build_array(k.oid, pg_catalog.pg_get_constraintdef(k.oid)) ORDER BY k.oid)
          FROM pg_catalog.pg_constraint k WHERE k.conrelid=c.attrelid AND c.attnum=ANY(k.conkey)),
         (SELECT jsonb_agg(jsonb_build_array(i.indexrelid, pg_catalog.pg_get_indexdef(i.indexrelid)) ORDER BY i.indexrelid)
          FROM pg_catalog.pg_index i WHERE i.indrelid=c.attrelid AND c.attnum=ANY(i.indkey)))
         ORDER BY c.attrelid) FROM columns c)::text),
       EXISTS (SELECT 1 FROM pg_catalog.pg_constraint k WHERE k.conrelid=a.attrelid
               AND k.contype='p' AND a.attnum=ANY(k.conkey))::text,
       (a.attinhcount > 0)::text
FROM pg_catalog.pg_attribute a
WHERE a.attrelid=CAST(:relation AS regclass) AND a.attname=:name AND a.attnum>0 AND NOT a.attisdropped
"""
