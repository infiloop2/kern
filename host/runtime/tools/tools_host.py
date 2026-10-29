"""The host side of the bundled tool framework.

This module is Kern's implementation of the host API contract in
docs/architecture/tools/tool-contract.md: it owns the registry of bundled tool
packages, builds the scoped ``HostAPI`` handed to every tool call (credentials
and approvals backed by admin state, config from the operator-supplied per-tool
config), validates action input against the manifest schemas, runs the
single-use approval lifecycle, and records every call as an audit event.

Each Kern host is single-operator. OAuth credentials are partitioned by tool
and host-generated connection id; approval records snapshot that selection.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import importlib
import ipaddress
import json
import re
from pathlib import Path
import time
import threading
from typing import Any, Iterator, Mapping, cast

from host.param_guard import OutboundGuardService
from host.runtime.core import host_errors, state
from host.runtime.tools import approval_assessment, assets as tool_assets
from host.runtime.tools.costs import HostCosts
from host.runtime.tools.schema import unsupported_schema_error, validate_against_schema
import host.tools
from host.tools import (
    ActionExecuted,
    ActionFailed,
    ActionPendingApproval,
    ActionResult,
    ApprovalExecuted,
    ApprovalRecord,
    ApprovalStatus,
    JSONObject,
    StoredCredential,
    StreamingAsset,
    Tool,
    ToolManifest,
    ToolService,
)
from host.tools.host_api import AssetMetadata, ConnectionAccount
from host.tools.manifest import ActionSpec, TOOL_ID_RE
from host.tools.shared.web import ProviderWarning

_DEFAULT_ASSET_STORE = tool_assets.ToolAssetStore(tool_assets.DEFAULT_ASSET_ROOT)

# Host approval policy: a pending decision is spent after 24 hours (swept by
# the admin API's hourly maintenance pass), and decided records are kept as
# bounded history like finished tasks.
APPROVAL_PENDING_TTL_SECONDS = 24 * 3600
PENDING_APPROVAL_LIMIT = 1000
APPROVAL_HISTORY_LIMIT = 10_000
# Size and format limits from docs/architecture/tools/tool-contract.md.
CREDENTIAL_VALUE_MAX_BYTES = 64 * 1024
PAYLOAD_MAX_BYTES = 64 * 1024
SUMMARY_MAX_BYTES = 500
CONFIG_VALUE_MAX_BYTES = 16_384

# Packages under host/tools that are framework helpers, not tools. Every other
# package must expose BUNDLED_TOOL, so a new tool cannot be silently skipped.
_NON_TOOL_PACKAGES = frozenset({"shared"})


def _tool_package_names(root: Path) -> tuple[str, ...]:
    names: list[str] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name):
        if child.name == "__pycache__":
            continue
        if child.is_symlink():
            raise RuntimeError(f"{child}: tool package directory must not be a symlink")
        if not child.is_dir():
            continue
        if not TOOL_ID_RE.fullmatch(child.name):
            raise RuntimeError(f"{child}: tool package directory must match {TOOL_ID_RE.pattern}")
        init_path = child / "__init__.py"
        if not init_path.is_file() or init_path.is_symlink():
            raise RuntimeError(f"{child}: tool package must contain a regular __init__.py file")
        if child.name not in _NON_TOOL_PACKAGES:
            names.append(child.name)
    return tuple(names)


def _discover_bundled_tools(
    root: Path | None = None,
    module_prefix: str = "host.tools",
) -> tuple[Tool, ...]:
    """The bundled tools are discovered from the packages under host/tools —
    like a package manifest scan — so adding a tool is adding its
    package (with a module-level ``BUNDLED_TOOL`` instance), not editing a
    hand-kept registry here."""
    if root is None:
        root = Path(host.tools.__path__[0])
    discovered: list[Tool] = []
    for package_name in _tool_package_names(root):
        module = importlib.import_module(f"{module_prefix}.{package_name}")
        tool = getattr(module, "BUNDLED_TOOL", None)
        if tool is None:
            raise RuntimeError(
                f"host/tools/{package_name} is not a tool package: it must expose a "
                "module-level BUNDLED_TOOL instance (or be listed in _NON_TOOL_PACKAGES)."
            )
        manifest = getattr(tool, "manifest", None)
        if not isinstance(manifest, ToolManifest):
            raise RuntimeError(
                f"host/tools/{package_name} BUNDLED_TOOL must expose a ToolManifest."
            )
        if manifest.tool_id != package_name:
            raise RuntimeError(
                f"host/tools/{package_name} declares tool_id {manifest.tool_id!r}; "
                "tool_id must match the package directory name."
            )
        discovered.append(tool)
    return tuple(discovered)


def _bundled_tool_map(tools: tuple[Tool, ...]) -> dict[str, Tool]:
    bundled: dict[str, Tool] = {}
    for tool in tools:
        tool_id = tool.manifest.tool_id
        if tool_id in bundled:
            raise RuntimeError(f"Duplicate bundled tool_id: {tool_id}")
        for spec in tool.manifest.actions:
            for label, schema in (("input_schema", spec.input_schema), ("output_schema", spec.output_schema)):
                error = unsupported_schema_error(schema, allow_empty=label == "output_schema")
                if error:
                    raise RuntimeError(f"{tool_id}.{spec.id}.{label}: {error}")
            if spec.approval == "direct" and set(spec.input_protections) != set(cast(JSONObject, spec.input_schema.get("properties", {}))):
                raise RuntimeError(f"{tool_id}.{spec.id}: every direct input must declare its protection.")
        # The manifest's connection kind and the Tool.credentials flow are two
        # declarations of one fact; pin them consistent at registration so no
        # consumer has to guess which to trust (tool-contract.md).
        if (tool.manifest.connection in {"oauth", "mcp_oauth"}) != (tool.credentials is not None):
            raise RuntimeError(
                f"{tool_id}: connection={tool.manifest.connection!r} disagrees with"
                f" credentials={'set' if tool.credentials is not None else 'None'}"
            )
        if tool.manifest.service and tool.credentials is not None:
            raise RuntimeError(
                f"{tool_id}: a manifest service and OAuth credentials cannot both be set"
            )
        bundled[tool_id] = tool
    return bundled


def tool_service(tool: Tool) -> ToolService | None:
    """Load the service implementation named by a trusted bundled manifest."""
    reference = tool.manifest.service
    if not reference:
        return None
    module_name, attribute = reference.split(":", 1)
    service = getattr(importlib.import_module(module_name), attribute, None)
    if service is None or any(
        not callable(getattr(service, method, None))
        for method in ("start", "stop", "operator")
    ):
        raise RuntimeError(f"{tool.manifest.tool_id}: invalid manifest service {reference!r}")
    return cast(ToolService, service)




class ToolCallError(Exception):
    """A tool call the host refuses to make (unknown tool or action, disabled
    tool, missing config, invalid input). The message is safe to return."""


class ApprovalBackpressureError(Exception):
    """The host refuses another pending approval until the operator catches up."""


class ToolConfigKeyUnsetError(RuntimeError):
    """A tool read a config key the operator has not set. The message is
    operator-actionable and safe to return even when the tool package lets it
    escape uncaught."""


class ConnectionAccountChangedError(ValueError):
    """A connection no longer resolves to the account selected for a call."""


def _ensure_json_object(value: Any, *, what: str, max_bytes: int) -> str:
    """Validate a JSON object per the host API's value rules and return its
    compact serialization (used for the byte-size limit). json.dumps with
    allow_nan=False is the whole check: it rejects NaN/inf and any non-JSON
    value; values come from in-repo tool packages only."""
    if not isinstance(value, dict):
        raise ValueError(f"{what} must be a JSON object.")
    try:
        serialized = json.dumps(value, separators=(",", ":"), allow_nan=False, ensure_ascii=False)
        size = len(serialized.encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{what} must be a JSON object with JSON-serializable values.") from exc
    if size > max_bytes:
        raise ValueError(f"{what} exceeds {max_bytes} bytes.")
    return serialized


@dataclass(frozen=True)
class ConnectionScope:
    """One host-selected account slot and its non-secret identity snapshot.

    Tool packages receive only the services already scoped to this value; they
    cannot enumerate connections or switch to another one.
    """

    connection_id: str
    account: ConnectionAccount | None

    @property
    def account_id(self) -> str:
        return self.account["id"] if self.account is not None else ""

    @property
    def account_label(self) -> str:
        return self.account["label"] if self.account is not None else ""


NO_CONNECTION = ConnectionScope(connection_id="", account=None)


class HostCredentials:
    """``tools.Credentials`` for one host-selected OAuth connection."""

    def __init__(self, tool_id: str, connection: ConnectionScope) -> None:
        self._tool_id = tool_id
        self._connection = connection

    def load(self) -> StoredCredential | None:
        if not self._connection.connection_id:
            return None
        record = state.tool_credential(self._tool_id, self._connection.connection_id)
        expected_account = self._connection.account
        if (
            record is not None
            and expected_account is not None
            and record["account"]["id"] != expected_account["id"]
        ):
            raise ConnectionAccountChangedError(
                "The connected account changed while the tool call was starting. Retry the call."
            )
        return cast(StoredCredential, record) if record is not None else None

    def save(self, credential: StoredCredential) -> None:
        self._save(credential)

    def save_if_current(self, loaded: StoredCredential, credential: StoredCredential) -> bool:
        return self._save(credential, expected=dict(loaded))

    def _save(self, credential: StoredCredential, *, expected: dict[str, Any] | None = None) -> bool:
        if not self._connection.connection_id:
            raise ValueError("Cannot save a credential without a connection scope.")
        record = dict(credential)
        _ensure_json_object(record, what="Credential", max_bytes=CREDENTIAL_VALUE_MAX_BYTES)
        expected_account = self._connection.account
        account = record.get("account")
        if (
            expected_account is not None
            and (
                not isinstance(account, dict)
                or account.get("id") != expected_account["id"]
            )
        ):
            raise ConnectionAccountChangedError(
                "This connection is already bound to a different provider account. "
                "Add a new connection instead."
            )
        return state.put_tool_credential(
            self._tool_id, record, self._connection.connection_id, expected=expected
        )

    def clear(self) -> None:
        if not self._connection.connection_id:
            return
        state.delete_tool_credential(self._tool_id, self._connection.connection_id)

    def clear_if_current(self, loaded: StoredCredential) -> bool:
        if not self._connection.connection_id:
            return False
        return state.delete_tool_credential(
            self._tool_id, self._connection.connection_id, expected=dict(loaded)
        )


class HostSecrets:
    """One private JSON object per tool definition."""

    def __init__(self, tool_id: str) -> None:
        self._tool_id = tool_id

    def load(self) -> JSONObject | None:
        return cast(JSONObject | None, state.tool_secret(self._tool_id))

    def save(self, value: JSONObject) -> None:
        serialized = _ensure_json_object(value, what="Tool secret", max_bytes=16 * 1024)
        # json.dumps accepts tuples and non-string keys, which would change on load.
        pending: list[object] = [value]
        while pending:
            item = pending.pop()
            if isinstance(item, dict):
                if any(not isinstance(key, str) for key in item):
                    raise ValueError("Tool secret object keys must be strings.")
                pending.extend(item.values())
            elif isinstance(item, list):
                pending.extend(item)
            elif item is not None and not isinstance(item, (str, bool, int, float)):
                raise ValueError("Tool secret must contain only JSON values.")
        state.put_tool_secret(self._tool_id, serialized)

    def clear(self) -> None:
        state.delete_tool_secret(self._tool_id)


class HostApprovals:
    """``tools.Approvals`` backed by the tool_approvals table, scoped to one
    tool's partition. Decisions and terminal outcomes are applied by the
    module-level lifecycle functions, not through this tool-facing surface."""

    def __init__(
        self,
        manifest: ToolManifest,
        connection: ConnectionScope,
        origin_thread_id: str | None,
    ) -> None:
        self._manifest = manifest
        self._connection = connection
        self._origin_thread_id = origin_thread_id

    def request(self, *, action_id: str, summary: str, payload: JSONObject) -> ApprovalRecord:
        spec = self._manifest.action(action_id)
        if spec is None:
            raise ValueError("Approval action is not in the tool manifest.")
        if not isinstance(summary, str) or not 1 <= len(summary.encode("utf-8")) <= SUMMARY_MAX_BYTES:
            raise ValueError(f"Approval summary must be 1-{SUMMARY_MAX_BYTES} UTF-8 bytes.")
        _ensure_json_object(payload, what="Approval payload", max_bytes=PAYLOAD_MAX_BYTES)
        try:
            record = state.insert_tool_approval(
                self._manifest.tool_id, action_id, summary, payload, int(time.time()),
                pending_limit=PENDING_APPROVAL_LIMIT,
                origin_thread_id=self._origin_thread_id,
                connection_id=self._connection.connection_id,
                account_id=self._connection.account_id,
                account_label=self._connection.account_label,
            )
        except state.PendingToolApprovalLimitReached as exc:
            raise ApprovalBackpressureError(
                f"Too many pending tool approvals. Decide or deny existing approvals before queuing more."
            ) from exc
        approval_assessment.schedule(
            record,
            action_description=spec.description,
            data_policy=spec.data_policy,
        )
        return _approval_record(record)


def _public_media_hostname(hostname: str) -> bool:
    # Only the configured Cloudflare hostname is used, never a caller URL.
    # Reject local names and IP literals even if configuration was malformed.
    try:
        ipaddress.ip_address(hostname)
        return False
    except ValueError:
        pass
    name = hostname.lower()
    if (len(name) > 253 or "." not in name or name.rsplit(".", 1)[-1].isdigit()
            or name.endswith((".localhost", ".local", ".internal", ".lan", ".home", ".invalid", ".test"))):
        return False
    return all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in name.split("."))


class HostAssets:
    """Tool-scoped view of the tools service's private staged assets."""

    def __init__(self, tool_id: str, store: tool_assets.ToolAssetStore) -> None:
        self._approved = False
        self._public_hostname: str | None = None
        self._approval_lock = threading.Lock()
        self._tool_id = tool_id
        self._store = store

    def describe(self, asset_id: str) -> AssetMetadata:
        return self._store.describe(self._tool_id, asset_id)

    def open(self, asset_id: str) -> Any:
        return self._store.open(self._tool_id, asset_id)

    def delete(self, asset_id: str) -> None:
        self._store.delete(self._tool_id, asset_id)

    @contextmanager
    def _approved_execution(self, public_hostname: str | None) -> Iterator[None]:
        """Host-authorized public access; retained APIs cannot mint links after exit."""
        with self._approval_lock:
            self._approved = True
            self._public_hostname = public_hostname
        try:
            yield
        finally:
            with self._approval_lock:
                self._approved = False
                self._public_hostname = None

    def public_asset_url(self, asset_id: str) -> str:
        # The store owns every link's bounded lifetime, including asynchronous
        # provider fetches after this authorized callback has returned.
        with self._approval_lock:
            if not self._approved:
                raise ValueError("Public asset access is not enabled for this tool execution.")
            # Supplied by host configuration or admin delegation, never an agent.
            hostname = self._public_hostname
            if not hostname or not _public_media_hostname(hostname):
                raise ValueError("Public asset access requires a Cloudflare HTTPS hostname. SSH, localhost and IP addresses are not supported. Configure Home > Connections.")
            token = self._store.create_asset_grant(self._tool_id, asset_id)
            return f"https://{hostname}/tool-media/{token}"


def _approval_record(record: dict[str, Any]) -> ApprovalRecord:
    status: ApprovalStatus = record["status"]
    return ApprovalRecord(
        approval_id=record["approval_id"],
        action_id=record["action_id"],
        status=status,
        payload=record["payload"],
        summary=record["summary"],
        created_at=record["created_at"],
        decided_at=record["decided_at"],
    )


@dataclass(frozen=True)
class HostToolAPI:
    """The ``tools.HostAPI`` bundle for one tool call."""

    credentials: HostCredentials
    secrets: HostSecrets
    config: Mapping[str, str]
    approvals: HostApprovals
    assets: HostAssets
    outbound: OutboundGuardService
    costs: HostCosts


class _ToolConfigView(dict[str, str]):
    """The config mapping handed to tool code. Enablement is not gated on
    config, so a tool can run before its keys are set; reading an unset key
    must fail with an operator-actionable message (which tool packages surface
    verbatim), not a bare KeyError whose str() is just the quoted key name."""

    def __missing__(self, key: str) -> str:
        raise ToolConfigKeyUnsetError(
            f"Tool config {key} is not set. The operator must set it under Home > Integrations in the admin UI."
        )


_OUTBOUND_GUARD = OutboundGuardService()


def connection_scope(tool: Tool, connection_id: str) -> ConnectionScope:
    """Resolve the host scope for one tool from its manifest and stored account."""
    if tool.manifest.connection not in {"oauth", "mcp_oauth"}:
        if connection_id:
            raise ValueError(f"Tool {tool.manifest.tool_id} does not use account connections.")
        return NO_CONNECTION
    if not connection_id:
        raise ValueError("connection_id must be non-empty.")
    if tool.manifest.connection == "mcp_oauth" and connection_id != "default":
        raise ValueError("This tool supports only its default connection.")
    credential = state.tool_credential(tool.manifest.tool_id, connection_id)
    account = cast(ConnectionAccount, credential["account"]) if credential is not None else None
    return ConnectionScope(connection_id=connection_id, account=account)


def host_api_for(
    tool: Tool,
    connection: ConnectionScope,
    origin_thread_id: str | None,
    *,
    asset_store: tool_assets.ToolAssetStore | None = None,
    action_id: str = "",
    approval_id: str | None = None,
) -> HostToolAPI:
    manifest = tool.manifest
    if manifest.connection not in {"oauth", "mcp_oauth"} and connection != NO_CONNECTION:
        raise ValueError(f"Tool {manifest.tool_id} does not use account connections.")
    config = state.tool_config_values(manifest.tool_id, [entry.key for entry in manifest.config])
    credentials = HostCredentials(manifest.tool_id, connection)
    return HostToolAPI(
        credentials=credentials,
        secrets=HostSecrets(manifest.tool_id),
        config=_ToolConfigView(config),
        approvals=HostApprovals(manifest, connection, origin_thread_id),
        assets=HostAssets(
            manifest.tool_id,
            asset_store or _DEFAULT_ASSET_STORE,
        ),
        outbound=_OUTBOUND_GUARD,
        costs=HostCosts(manifest.tool_id, connection.connection_id, action_id,
                        manifest.reports_cost,
                        origin_thread_id, approval_id),
    )


def bundled_tool(tool_id: str) -> Tool:
    tool = BUNDLED_TOOLS.get(tool_id) if isinstance(tool_id, str) else None
    if tool is None:
        raise ToolCallError(f"Unknown tool: {tool_id}.")
    return tool


def enabled_tool(tool_id: str) -> Tool:
    """The tool, ready to call: it only has to be enabled. Config is not gated --
    a tool may be enabled with partial or no config, so an action that needs a
    key that is not set fails when the tool reads it, and actions that do not need
    that key still work."""
    tool = bundled_tool(tool_id)
    if tool_id not in state.enabled_tool_ids():
        raise ToolCallError(f"Tool {tool_id} is not enabled. Ask the operator to enable it in Home > Integrations.")
    return tool


def resolve_connection(tool: Tool, requested: str | None) -> ConnectionScope:
    """Resolve an agent-selected OAuth connection without exposing credentials.

    A single connection remains implicit for backwards compatibility. Once a
    tool has several accounts, the agent must name one explicitly so a write
    can never drift to whichever account happened to be connected first.
    """
    if tool.manifest.connection not in {"oauth", "mcp_oauth"}:
        if requested:
            raise ToolCallError(f"Tool {tool.manifest.tool_id} does not use account connections.")
        return connection_scope(tool, "")
    connections = state.tool_connections(tool.manifest.tool_id)
    if requested:
        if any(item["connection_id"] == requested for item in connections):
            return connection_scope(tool, requested)
        raise ToolCallError(
            f"Unknown connection_id for {tool.manifest.tool_id}: {requested}. "
            f'Call list_bundled_tools with tool_ids=["{tool.manifest.tool_id}"] to see connected accounts.'
        )
    if len(connections) > 1:
        choices = ", ".join(
            f"{item['connection_id']} ({item['account']['label']})" for item in connections
        )
        raise ToolCallError(
            f"Tool {tool.manifest.tool_id} has multiple connected accounts. "
            f"Pass connection_id to call_tool: {choices}."
        )
    return connection_scope(tool, connections[0]["connection_id"]) if connections else NO_CONNECTION


# -- action execution ----------------------------------------------------------


@dataclass(frozen=True)
class StreamingAction:
    """One exclusive binary action result plus its eventual audit context."""

    asset: StreamingAsset
    tool_id: str
    action: str
    arguments: JSONObject
    connection: ConnectionScope


def _provider_warning_context(
    tool_id: str,
    action_id: str,
    exc: ProviderWarning,
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "tool_id": tool_id,
        "action_id": action_id,
        "provider": exc.provider,
        "operation": exc.operation,
        "http_status": exc.status,
    }
    if exc.response_body:
        context["provider_response"] = exc.response_body
    context.update(exc.diagnostic_context)
    if exc.body_truncated:
        context["provider_response_truncated"] = True
    return context


def _provider_failure_result(exc: ProviderWarning) -> ActionFailed:
    return ActionFailed(str(exc) or "Provider request failed. Check Host diagnostics for details.")


def _normalize_tool_input(value: Any) -> dict[str, Any]:
    """Decode at most one model-added JSON string wrapper before normal validation."""
    message = (
        'Tool input must be a JSON object matching describe_tool, e.g. "input": {"text": "hello"}, '
        "not a JSON-encoded string, array, or scalar."
    )
    if value is None:
        return {}
    if isinstance(value, str):
        def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = item
            return result

        try:
            if len(value.encode("utf-8")) > PAYLOAD_MAX_BYTES:
                raise ToolCallError(f"Tool input exceeds {PAYLOAD_MAX_BYTES} bytes.")
            value = json.loads(value, object_pairs_hook=unique_keys)
        except (ValueError, RecursionError) as exc:
            raise ToolCallError(message + " String input must contain valid JSON without duplicate keys.") from exc
    if not isinstance(value, dict):
        raise ToolCallError(message)
    return value


def _check_run_limit(tool_id: str, spec: ActionSpec, *, consume: bool) -> None:
    limit = spec.limit_runs_per_day
    if limit is None:
        return
    allowed = (state.consume_tool_action_run(tool_id, spec.id, limit) if consume
               else not state.tool_action_run_limit_reached(tool_id, spec.id, limit))
    if not allowed:
        raise ToolCallError(
            f"Daily run limit reached for {tool_id}.{spec.id} ({limit} executions per UTC day). "
            "Try again after 00:00 UTC."
        )


def execute_action(
    tool_id: str,
    action: str,
    tool_input: Any,
    origin_thread_id: str | None,
    asset_store: tool_assets.ToolAssetStore | None = None,
    *,
    connection_id: str | None = None,
) -> dict[str, Any] | StreamingAction:
    """Run one agent-initiated action call end to end: resolve the enabled
    tool, schema-validate the input, invoke the package, audit, and return
    either its JSON result shape or an exclusive streaming result."""
    tool = enabled_tool(tool_id)
    connection = resolve_connection(tool, connection_id)
    spec = tool.manifest.action(action) if isinstance(action, str) else None
    if spec is None:
        raise ToolCallError(f"Tool {tool_id} has no action {action}.")
    tool_input = _normalize_tool_input(tool_input)
    schema_error = validate_against_schema(tool_input, spec.input_schema)
    if schema_error:
        if schema_error.endswith("matches none of the allowed shapes."):
            schema_error += f' Check describe_tool with tool_id="{tool_id}", action_ids=["{action}"].'
        raise ToolCallError(f"Invalid input for {tool_id}.{action}: {schema_error}")
    # Snapshot the exact JSON before tool code runs. Besides enforcing one
    # storage bound across every manifest, the round trip prevents a package
    # from mutating the audit record through a shared nested object.
    try:
        serialized_input = _ensure_json_object(tool_input, what="Tool input", max_bytes=PAYLOAD_MAX_BYTES)
    except ValueError as exc:
        raise ToolCallError(str(exc)) from exc
    audit_arguments = cast(JSONObject, json.loads(serialized_input))
    _check_run_limit(tool_id, spec, consume=spec.approval == "direct")
    try:
        api = host_api_for(tool, connection, asset_store=asset_store, origin_thread_id=origin_thread_id, action_id=action)
        # Read only the public hostname; tunnel credentials remain protected.
        try:
            hostname = Path("/etc/kern-tools-public-hostname").read_text().strip()
        except OSError:
            hostname = None
        with api.assets._approved_execution(hostname):
            result = tool.execute(action, audit_arguments, api)
    except (
        ApprovalBackpressureError,
        ConnectionAccountChangedError,
        ToolConfigKeyUnsetError,
    ) as exc:
        result = ActionFailed(str(exc))
    except ProviderWarning as exc:
        host_errors.report_warning(
            "tools.provider_request",
            exc,
            context=_provider_warning_context(tool_id, action, exc),
            kind="provider_failure",
        )
        result = _provider_failure_result(exc)
    except Exception as exc:
        host_errors.report_warning(
            "tools.execute",
            exc,
            context={"tool_id": tool_id, "action_id": action},
        )
        result = ActionFailed(f"{tool_id}.{action} failed unexpectedly. Ask the operator to check Host diagnostics.")
    kind_error = _result_kind_error(spec, result)
    if kind_error:
        result_json = _result_kind_mismatch(tool_id, action, kind_error)
        _audit(
            tool_id, action, result_json, arguments=audit_arguments,
            connection_id=connection.connection_id, account_id=connection.account_id,
            account_label=connection.account_label,
        )
        return result_json
    if isinstance(result, StreamingAsset):
        return StreamingAction(
            result, tool_id, action, audit_arguments, connection,
        )
    result_json = _result_json(result)
    result_json = _validate_output(tool_id, action, spec, result_json)
    _audit(
        tool_id, action, result_json, arguments=audit_arguments,
        connection_id=connection.connection_id, account_id=connection.account_id,
        account_label=connection.account_label,
    )
    return result_json


def finish_streaming_action(streaming: StreamingAction, error: str | None = None) -> None:
    """Audit a stream only after the transport has completed or failed."""
    result_json: dict[str, Any]
    if error is None:
        result_json = {"status": "executed"}
    else:
        result_json = {
            "status": "failed",
            "error": error,
            "reconnect_required": False,
        }
    _audit(
        streaming.tool_id,
        streaming.action,
        result_json,
        arguments=streaming.arguments,
        connection_id=streaming.connection.connection_id,
        account_id=streaming.connection.account_id,
        account_label=streaming.connection.account_label,
    )


def _execute_approved(
    record: dict[str, Any], public_hostname: str | None,
    asset_store: tool_assets.ToolAssetStore | None = None,
) -> dict[str, Any]:
    """Run one approved action and audit the outcome. Never raises: any
    failure (tool disabled since queueing, config unset, tool exception)
    becomes a failed result so the approval always reaches a terminal state.
    Approved results carry a user-visible message, no output schema."""
    tool_id, action, approval_id = record["tool_id"], record["action_id"], record["approval_id"]
    try:
        tool = enabled_tool(tool_id)
        connection = connection_scope(tool, record["connection_id"])
        expected_account_id = record["account_id"]
        if expected_account_id and connection.account_id != expected_account_id:
            raise ConnectionAccountChangedError(
                "The account selected for this approval is no longer connected. "
                "Queue the action again."
            )
        api = host_api_for(tool, connection, origin_thread_id=record.get("origin_thread_id"), asset_store=asset_store, action_id=action, approval_id=approval_id)
        with api.assets._approved_execution(public_hostname):
            spec = tool.manifest.action(action)
            if spec is None:
                raise ToolCallError(f"Tool {tool_id} has no action {action}.")
            _check_run_limit(tool_id, spec, consume=True)
            result: Any = tool.execute_approved(_approval_record(record), api)
    except (
        ConnectionAccountChangedError,
        ToolCallError,
        ToolConfigKeyUnsetError,
    ) as exc:
        result = ActionFailed(str(exc))
    except ProviderWarning as exc:
        host_errors.report_warning(
            "tools.provider_request_approved",
            exc,
            context=_provider_warning_context(tool_id, action, exc),
            kind="provider_failure",
        )
        result = _provider_failure_result(exc)
    except Exception as exc:
        host_errors.report_warning(
            "tools.execute_approved",
            exc,
            context={"tool_id": tool_id, "action_id": action},
        )
        result = ActionFailed(
            f"{tool_id}.{action} failed unexpectedly; outcome unknown. "
            "Check the provider before requesting another execution."
        )
    if not isinstance(result, (ApprovalExecuted, ActionFailed)):
        # An approved action's outcome is one user-visible message. Any other
        # result kind is drift, and it must still reach a terminal state rather
        # than leaving the approval stuck in "approved".
        result = _approved_result_kind_failure(tool_id, action, result)
    result_json = _result_json(result)
    _audit(
        tool_id,
        action,
        result_json,
        detail=_approval_audit_detail(approval_id, result_json),
        arguments=cast(JSONObject, record["payload"]),
        connection_id=record["connection_id"],
        account_id=record["account_id"],
        account_label=record["account_label"],
    )
    return result_json


def _result_json(result: Any) -> dict[str, Any]:
    if isinstance(result, ActionExecuted):
        return {"status": "executed", "result": result.result}
    if isinstance(result, ApprovalExecuted):
        # An approved action's outcome is a single user-visible message.
        return {"status": "executed", "message": result.message}
    if isinstance(result, ActionPendingApproval):
        # The approval id already carries the poll token (state._approval_id).
        return {
            "status": "pending_approval",
            "approval_id": result.approval_id,
            "summary": result.summary,
        }
    return {
        "status": "failed",
        "error": result.error,
        "reconnect_required": result.reconnect_required,
    }


def _approved_result_kind_failure(tool_id: str, action: str, result: Any) -> ActionFailed:
    error = (
        f"{tool_id}.{action} returned {type(result).__name__} for an approved action, "
        "which reports one user-visible message."
    )
    host_errors.report_warning(
        "tools.result_kind",
        RuntimeError(error),
        context={"tool_id": tool_id, "action_id": action},
    )
    return ActionFailed(error)


def _result_kind_error(spec: ActionSpec, result: Any) -> str:
    """Name the drift when a result contradicts the manifest's declared kind.

    The manifest states which of the three result kinds an action produces, and
    describe_tool, the admin API, and output validation all publish that
    statement. A failure is always allowed: any action can fail.
    """
    if isinstance(result, ActionFailed):
        return ""
    if spec.approval == "operator":
        expected: type | tuple[type, ...] = ActionPendingApproval
        described = "queues an approval"
    elif spec.returns_asset:
        expected = StreamingAsset
        described = "returns a file"
    else:
        expected = ActionExecuted
        described = "returns a JSON result"
    if isinstance(result, expected):
        return ""
    return f"is declared as an action that {described} but returned {type(result).__name__}"


def _result_kind_mismatch(tool_id: str, action: str, what: str) -> dict[str, Any]:
    error = f"{tool_id}.{action} {what}."
    host_errors.report_warning(
        "tools.result_kind",
        RuntimeError(error),
        context={"tool_id": tool_id, "action_id": action},
    )
    return {"status": "failed", "error": error, "reconnect_required": False}


def _validate_output(tool_id: str, action: str, spec: ActionSpec, result_json: dict[str, Any]) -> dict[str, Any]:
    # Only direct actions carry a JSON "result" to validate; approved actions
    # return a user-visible "message" with no output schema.
    if result_json.get("status") != "executed" or "result" not in result_json:
        return result_json
    error = validate_against_schema(result_json["result"], spec.output_schema, path="result")
    if not error:
        return result_json
    return {
        "status": "failed",
        "error": f"Invalid output for {tool_id}.{action}: {error}",
        "reconnect_required": False,
    }


def _approval_audit_detail(approval_id: str, result_json: dict[str, Any]) -> str:
    error = result_json.get("error")
    return f"{approval_id}: {error}" if error else approval_id


def _audit(
    tool_id: str,
    action: str,
    result_json: dict[str, Any],
    *,
    detail: str | None = None,
    arguments: JSONObject | None = None,
    connection_id: str = "",
    account_id: str = "",
    account_label: str = "",
) -> None:
    outcome = result_json["status"]
    if detail is None:
        detail = result_json.get("error") or result_json.get("approval_id") or ""
    if not isinstance(detail, str):
        detail = ""
    state.record_tool_event(
        tool_id, action, outcome, detail, arguments,
        connection_id=connection_id,
        account_id=account_id,
        account_label=account_label,
    )


# -- approval lifecycle --------------------------------------------------------


def decide_approval(
    approval_id: str,
    decision: str,
    public_hostname: str | None,
    asset_store: tool_assets.ToolAssetStore | None = None,
) -> dict[str, Any]:
    """Apply an operator decision. Approving runs ``execute_approved`` at most
    once and records the terminal outcome; denying is terminal immediately.
    Raises ToolCallError when the record is absent or already decided."""
    record = state.tool_approval(approval_id)
    if record is None:
        raise ToolCallError(f"Unknown approval: {approval_id}.")
    now = int(time.time())
    if decision == "deny":
        if not state.transition_tool_approval(approval_id, "pending", "denied", now):
            raise ToolCallError(f"Approval {approval_id} is not pending.")
        # Deny has no execute_approved call to audit, so record it here.
        state.record_tool_event(
            record["tool_id"], record["action_id"], "denied", approval_id,
            cast(JSONObject, record["payload"]),
            connection_id=record["connection_id"],
            account_id=record["account_id"],
            account_label=record["account_label"],
        )
        return {"approval": state.tool_approval(approval_id)}
    if not state.transition_tool_approval(approval_id, "pending", "approved", now):
        raise ToolCallError(f"Approval {approval_id} is not pending.")
    # The transition above is the at-most-once gate; the record handed to the
    # tool carries the state the transition just wrote.
    result_json = _execute_approved(
        record | {"status": "approved", "decided_at": now}, public_hostname, asset_store
    )
    outcome = "executed" if result_json["status"] == "executed" else "failed"
    # The stored result is the outcome's single text per the contract: the
    # user-visible message when the approved action executed, the error when
    # it failed (the status column says which one it is).
    result_text = str(result_json.get("message") or result_json.get("error") or "")
    state.transition_tool_approval(approval_id, "approved", outcome, int(time.time()), result=result_text)
    return {"approval": state.tool_approval(approval_id), "result": result_json}


def recover_interrupted_approvals() -> None:
    """Mark approvals stuck in ``approved`` as failed at service start: the
    one execute_approved call cannot survive a restart, and a single-use
    approval whose outcome is unknown is spent."""
    state.fail_approved_tool_approvals(int(time.time()))


def maintain_approvals() -> None:
    state.expire_tool_approvals(int(time.time()) - APPROVAL_PENDING_TTL_SECONDS)
    state.prune_tool_approvals(APPROVAL_HISTORY_LIMIT)


# Discovery runs at import, using schema.py's declaration checker: an out-of-subset manifest fails the service at startup.
BUNDLED_TOOLS: dict[str, Tool] = _bundled_tool_map(_discover_bundled_tools())
