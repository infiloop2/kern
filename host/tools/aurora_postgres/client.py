"""Bounded signed Data API requests and transaction lifetime."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import re
import time
from typing import Iterator, cast
import uuid

from host.tools.host_api import HostAPI
from host.tools.json_types import JSONObject, JSONValue
from host.tools.shared.aws_sigv4 import sign_post
from host.tools.shared.web import request_bytes, WebRequestError
from .sql import fail, identifier, json_bytes
from .diagnostics import ResponseValidationError, aws_error_code, failure_type, invalid_response, report_failure

PREFIX = "AURORA_POSTGRES_"
CONFIG_KEYS = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "CLUSTER_ARN", "DATABASE", "SCHEMA", "READER_SECRET_ARN", "WRITER_SECRET_ARN", "DDL_SECRET_ARN", "DROPPER_SECRET_ARN")
PATHS = {"execute": "/Execute", "begin": "/BeginTransaction", "commit": "/CommitTransaction", "rollback": "/RollbackTransaction"}


@dataclass(frozen=True)
class Config:
    values: dict[str, str]

    @classmethod
    def read(cls, api: HostAPI) -> Config:
        values = {key: api.config.get(PREFIX + key, "") for key in CONFIG_KEYS}
        if not all(values.values()):
            fail("Aurora PostgreSQL configuration is not set or is incomplete. Complete it in Home > Integrations, including four role secret ARNs.")
        region = values["AWS_REGION"]
        if not re.fullmatch(r"(?:af|ap|ca|eu|il|me|mx|sa|us)-[a-z]+-[0-9]", region):
            fail("Use an AWS commercial region with Aurora PostgreSQL Data API support.")
        if not re.fullmatch(r"AKIA[A-Z0-9]{16}", values["AWS_ACCESS_KEY_ID"]) or not re.fullmatch(r"[A-Za-z0-9/+=]{40}", values["AWS_SECRET_ACCESS_KEY"]):
            fail("Configure a dedicated long-lived IAM access key pair in Home > Integrations.")
        cluster = re.fullmatch(r"arn:aws:rds:" + re.escape(region) + r":([0-9]{12}):cluster:([a-zA-Z0-9-]{1,63})", values["CLUSTER_ARN"])
        if not cluster:
            fail("Cluster ARN must identify an Aurora cluster in the configured region.")
        assert cluster is not None
        for key in ("READER_SECRET_ARN", "WRITER_SECRET_ARN", "DDL_SECRET_ARN", "DROPPER_SECRET_ARN"):
            if not re.fullmatch(r"arn:aws:secretsmanager:" + re.escape(region) + ":" + cluster[1] + r":secret:[a-zA-Z0-9/_+=.@!-]{1,512}", values[key]):
                fail("Role secret ARNs must belong to the cluster's account and region.")
        if len({values[key] for key in CONFIG_KEYS if key.endswith("SECRET_ARN")}) != 4:
            fail("Configure four distinct Secrets Manager secrets, one for each database role.")
        identifier(values["DATABASE"], "Database")
        schema = identifier(values["SCHEMA"], "Schema")
        if schema == "public" or schema == "information_schema" or schema.startswith("pg_"):
            fail("Use a dedicated application schema, not a built-in schema.")
        return cls(values)

    @property
    def schema(self) -> str:
        return self.values["SCHEMA"]

    def binding(self) -> str:
        # Credentials may rotate, but destination, IAM identity and role-secret
        # selection must match the proposal. No config values enter approvals.
        values = {k: v for k, v in self.values.items() if k != "AWS_SECRET_ACCESS_KEY"}
        return hashlib.sha256(json_bytes(cast(JSONObject, values))).hexdigest()


def parameter(name: str, value: str | None) -> JSONObject:
    return {"name": name, "value": {"isNull": True} if value is None else {"stringValue": value}}


def result_rows(response: JSONObject) -> list[list[str | None]]:
    records = response.get("records", [])
    if not isinstance(records, list):
        invalid_response("AWS returned an invalid result. Narrow the query or cast unsupported types to text.")
    rows: list[list[str | None]] = []
    for record in records:
        if not isinstance(record, list):
            invalid_response("AWS returned an invalid row.")
        row: list[str | None] = []
        for field in cast(list[JSONValue], record):
            if not isinstance(field, dict) or len(field) != 1:
                invalid_response("AWS returned an unsupported field. Cast arrays and binary values to text.")
            obj = cast(JSONObject, field)
            if obj.get("isNull") is True:
                row.append(None)
            elif isinstance(obj.get("stringValue"), str):
                row.append(cast(str, obj["stringValue"]))
            elif type(obj.get("booleanValue")) is bool:
                row.append("true" if obj["booleanValue"] else "false")
            elif type(obj.get("longValue")) is int or type(obj.get("doubleValue")) in (int, float):
                row.append(json.dumps(next(iter(obj.values())), allow_nan=False))
            else:
                invalid_response("AWS returned an unsupported field. Cast arrays and binary values to text.")
        rows.append(row)
    return rows


class Client:
    def __init__(self, config: Config, role: str):
        self.config = config
        self.role = role
        self.transaction_id = ""
        self.phase = "statement"
        # Local correlation only; never log the AWS transaction ID.
        self.correlation_id = str(uuid.uuid4())

    def request(self, operation: str, extra: JSONObject) -> JSONObject:
        v = self.config.values
        body: JSONObject = {"resourceArn": v["CLUSTER_ARN"], "secretArn": v[self.role.upper() + "_SECRET_ARN"], **extra}
        if operation in ("begin", "execute"):
            body["database"] = v["DATABASE"]
        if self.transaction_id:
            body["transactionId"] = self.transaction_id
        payload = json_bytes(body)
        if len(payload) > 1024 * 1024:
            fail("AWS request exceeds 1 MiB. Use a smaller insert batch.")
        signed = sign_post(host=f"rds-data.{v['AWS_REGION']}.amazonaws.com", region=v["AWS_REGION"], service="rds-data",
                           access_key_id=v["AWS_ACCESS_KEY_ID"], secret_access_key=v["AWS_SECRET_ACCESS_KEY"],
                           body=payload, content_type="application/json", path=PATHS[operation])
        started = time.monotonic()
        phase = operation if operation != "execute" else self.phase
        try:
            raw = request_bytes("POST", signed.url, headers=signed.headers, data=payload,
                                timeout=50, max_bytes=1100000, failure_message="Aurora Data API request failed.")
        except WebRequestError as exc:
            report_failure(operation=operation, role=self.role, phase=phase, correlation_id=self.correlation_id,
                           failure=failure_type(exc), elapsed_ms=int((time.monotonic() - started) * 1000), exc=exc)
            code = aws_error_code(exc)
            messages = {
                "DatabaseResumingException": "Aurora is resuming. Wait briefly before a new read; inspect writes before retrying.",
                "HttpEndpointNotEnabledException": "Enable RDS Data API on the configured cluster.",
                "InvalidSecretException": "Check the role secret's username/password and SCRAM authentication in AWS Secrets Manager.",
                "SecretsErrorException": "Check the role secret, IAM GetSecretValue permission and any customer KMS key permission.",
                "StatementTimeoutException": "Aurora statement timed out. Narrow the query or schedule the schema operation when less busy.",
                "UnsupportedResultException": "AWS result limit or unsupported type. Select fewer/smaller columns and cast unsupported types to text.",
                "DatabaseErrorException": "PostgreSQL rejected the statement. Check table definitions, types, constraints, permissions and partition bounds.",
                "TransactionNotFoundException": "Aurora transaction expired. Inspect the table before proposing another write.",
            }
            message = messages.get(code, "Aurora Data API failed. Check cluster availability, Data API enablement, region and database configuration.")
            if exc.status in (401, 403):
                message = "AWS denied Aurora access. Check the dedicated IAM policy, key and role secret permissions in Home > Integrations."
            fail(message + " Check Host diagnostics for operation details. No request was retried. A write or commit with a lost response may have succeeded; inspect before retrying.")
        try:
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError
            json_bytes(result)
            return cast(JSONObject, result)
        except (ValueError, UnicodeError, RecursionError):
            report_failure(operation=operation, role=self.role, phase=phase, correlation_id=self.correlation_id,
                           failure="invalid_response", elapsed_ms=int((time.monotonic() - started) * 1000))
            fail("AWS returned an invalid response. Inspect write outcomes before retrying.")
        raise AssertionError("unreachable")

    def execute(self, statement: str, params: list[JSONObject]) -> JSONObject:
        return self.request("execute", {"sql": statement, "parameters": cast(list[JSONValue], params),
            "includeResultMetadata": True, "continueAfterTimeout": False,
            "resultSetOptions": {"decimalReturnType": "STRING", "longReturnType": "STRING"}})

    @contextmanager
    def transaction(self, readonly: bool) -> Iterator[Client]:
        response = self.request("begin", {})
        transaction_id = response.get("transactionId")
        if not isinstance(transaction_id, str) or not transaction_id:
            report_failure(operation="begin", role=self.role, phase="begin", correlation_id=self.correlation_id,
                           failure="missing_transaction_id")
            fail("AWS did not return a transaction. No SQL was submitted.")
        self.transaction_id = cast(str, transaction_id)
        try:
            self.phase = "transaction_setup"
            if readonly:
                self.execute("SET TRANSACTION READ ONLY", [])
            self.execute("SET LOCAL statement_timeout = '20s'", [])
            self.execute("SET LOCAL lock_timeout = '5s'", [])
            self.execute(f'SET LOCAL search_path = pg_catalog, "{self.config.schema}"', [])
            self.phase = "role_check"
            identity = result_rows(self.execute("SELECT session_user::text", []))
            if identity != [["kern_" + self.role]]:
                report_failure(operation="execute", role=self.role, phase=self.phase, correlation_id=self.correlation_id,
                               failure="role_mismatch")
                fail("Role secret points to the wrong database user. Fix the four role secrets in Home > Integrations.")
            self.phase = "statement"
            yield self
            if readonly:
                self.request("rollback", {})
            else:
                self.request("commit", {})
        except Exception as exc:
            if isinstance(exc, ResponseValidationError):
                report_failure(operation="execute", role=self.role, phase=self.phase, correlation_id=self.correlation_id,
                               failure="invalid_response")
            try:
                self.request("rollback", {})
            except Exception:
                pass  # Preserve the original error, never repeat the SQL/commit.
            raise
        finally:
            self.transaction_id = ""
