"""Run one Hermes prompt without placing prompt content in process arguments.

Hermes runs headless and quiet, so its stdout carries only the final answer.
To surface the same live agent activity Agent Chat shows for Codex and Claude
Code, this wrapper subscribes to Hermes's ``pre_tool_call``/``post_tool_call``
plugin hooks and prints one provider-independent activity record per event to
stdout, each on its own line behind an ASCII Record-Separator sentinel plus the
per-turn ``--activity-nonce`` secret. The host adapter
(``host.runtime.agent_runtime.hermes_agent``) mints that nonce, passes it in, and
splits the framed lines from the answer text as it streams stdout. Because the
model never sees the nonce, its (model-controlled) answer text cannot reproduce
the frame to forge a card or steal itself out of the response. Emission is
best-effort: a hook that raises is swallowed so agent progress can never fail
the turn, and the host re-validates and bounds every record before persisting
it.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys


MAX_PROMPT_BYTES = 1_000_000

# Kept byte-for-byte in sync with ``hermes_agent.ACTIVITY_LINE_PREFIX``. The
# full per-line frame is this sentinel followed by the per-turn nonce and a
# space; the host only trusts a line carrying the exact secret it minted.
ACTIVITY_LINE_PREFIX = "\x1ekern-activity "

# Set once in main() from ``--activity-nonce``. None disables emission (no
# nonce, no trusted channel — the turn still runs, just without live activity).
_ACTIVITY_MARKER: str | None = None
# Local guards only. The host applies the authoritative 256 KiB per-field clip
# in ``agent_activity``; these keep a single stdout line from ballooning when a
# tool returns a large payload (a file write, a long command output).
_DETAIL_MAX_BYTES = 8 * 1024
_OUTPUT_MAX_BYTES = 32 * 1024

# Tools whose activity reads as a shell command, a file edit, or a search.
# Everything else (bundled MCP tools, file reads, anything new) is a generic
# tool card, matching how the Claude Code adapter buckets its tools.
_COMMAND_TOOLS = frozenset({"terminal", "process", "read_terminal", "close_terminal", "kill_terminal"})
_FILE_CHANGE_TOOLS = frozenset({"write_file", "patch"})
_SEARCH_TOOLS = frozenset({"search_files"})


def _clip(value: object, maximum: int) -> str | None:
    if value in (None, ""):
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= maximum:
        return encoded.decode("utf-8", errors="replace")
    return encoded[:maximum].decode("utf-8", errors="ignore") + "\n… (truncated)"


def _tool_kind(tool_name: str) -> str:
    if tool_name in _COMMAND_TOOLS:
        return "command"
    if tool_name in _FILE_CHANGE_TOOLS:
        return "file_change"
    if tool_name in _SEARCH_TOOLS:
        return "search"
    return "tool"


def _tool_title(tool_name: str, args: dict) -> str:
    if tool_name == "terminal":
        return _clip(args.get("command") or "Shell command", 2048) or "Shell command"
    if tool_name == "process":
        return f"Process: {args.get('action')}" if args.get("action") else "Process"
    if tool_name == "write_file":
        return f"Write: {args['path']}" if args.get("path") else "Write file"
    if tool_name == "patch":
        return f"Edit: {args['path']}" if args.get("path") else "Edit file"
    if tool_name == "read_file":
        return f"Read: {args['path']}" if args.get("path") else "Read file"
    if tool_name == "search_files":
        return _clip(f"Search: {args['pattern']}", 2048) or "Search files" if args.get("pattern") else "Search files"
    return f"Tool: {tool_name}"


def _tool_detail(tool_name: str, args: dict) -> str | None:
    # Never echo a file's full contents into the started card: keep the path
    # and let the file-change card speak for the edit itself.
    if tool_name in {"write_file", "patch"}:
        summary = {key: value for key, value in args.items() if key not in {"content", "new_string", "old_string", "patch"}}
        return _clip(summary or None, _DETAIL_MAX_BYTES)
    return _clip(args or None, _DETAIL_MAX_BYTES)


def _emit_activity(record: dict) -> None:
    marker = _ACTIVITY_MARKER
    if not marker:
        return
    try:
        line = marker + json.dumps(record, ensure_ascii=False, default=str)
    except Exception:
        return
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        # A closed/broken stdout must never abort the running turn.
        pass


def _activity_id(kwargs: dict) -> str:
    call_id = kwargs.get("tool_call_id") or kwargs.get("turn_id")
    return str(call_id) if call_id else f"hermes:{kwargs.get('tool_name')}:{id(kwargs)}"


def _on_pre_tool_call(**kwargs: object) -> None:
    # Observer only: returning None leaves the tool untouched (a dict here
    # would be read as a block/approve directive by resolve_pre_tool_block).
    try:
        tool_name = str(kwargs.get("tool_name") or "")
        raw_args = kwargs.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        _emit_activity({
            "provider": "hermes",
            "activity_id": _activity_id(kwargs),
            "kind": _tool_kind(tool_name),
            "phase": "started",
            "title": _tool_title(tool_name, args),
            "detail": _tool_detail(tool_name, args),
            "status": "running",
        })
    except Exception:
        pass
    return None


def _on_post_tool_call(**kwargs: object) -> None:
    try:
        tool_name = str(kwargs.get("tool_name") or "")
        raw_args = kwargs.get("args")
        args = raw_args if isinstance(raw_args, dict) else {}
        is_error = str(kwargs.get("status") or "").lower() == "error"
        output = kwargs.get("error_message") if is_error else kwargs.get("result")
        _emit_activity({
            "provider": "hermes",
            "activity_id": _activity_id(kwargs),
            "kind": _tool_kind(tool_name),
            "phase": "completed",
            "title": _tool_title(tool_name, args),
            "output": _clip(output, _OUTPUT_MAX_BYTES),
            "status": "failed" if is_error else "completed",
        })
    except Exception:
        pass
    return None


def _register_activity_hooks() -> None:
    """Subscribe to Hermes tool-call hooks so each becomes an activity record.

    Deliberately does NOT trigger plugin discovery: it only appends two
    observer callbacks to the process-global plugin manager's hook registry.
    The universal tool dispatcher (``model_tools.handle_function_call``) checks
    ``has_hook``/``invoke_hook`` on that same registry for every tool it runs,
    so the callbacks fire on the single-query path without us loading any
    plugin Hermes would not otherwise load. Discovery only ever appends (it
    clears the registry only on a forced rediscover, which the single-query
    path never does), so a later Hermes-initiated sweep leaves these intact.
    Registration is best-effort: if the plugin surface is unavailable the turn
    still runs, just without live activity.
    """
    try:
        # Imported by string like the other Hermes packages this wrapper
        # touches, so the host type-checkers never try to resolve the
        # venv-only ``hermes_cli`` package.
        plugins = importlib.import_module("hermes_cli.plugins")
        manager = plugins.get_plugin_manager()
        manager._hooks.setdefault("pre_tool_call", []).append(_on_pre_tool_call)
        manager._hooks.setdefault("post_tool_call", []).append(_on_post_tool_call)
    except Exception:
        pass


def main() -> None:
    global _ACTIVITY_MARKER

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--resume")
    # The per-turn secret the host mints to frame activity lines. Absent means
    # the host is not consuming activity, so none is emitted.
    parser.add_argument("--activity-nonce")
    args = parser.parse_args()
    if args.activity_nonce:
        _ACTIVITY_MARKER = f"{ACTIVITY_LINE_PREFIX}{args.activity_nonce} "

    raw_prompt = sys.stdin.buffer.read(MAX_PROMPT_BYTES + 1)
    if len(raw_prompt) > MAX_PROMPT_BYTES:
        raise SystemExit("Hermes prompt exceeds the launcher byte limit")
    try:
        prompt = raw_prompt.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("Hermes prompt is not valid UTF-8") from exc
    if not prompt:
        raise SystemExit("Hermes prompt is empty")

    # cmd_chat normally translates --yolo into this variable before calling
    # cli.main. The wrapper calls cli.main directly so the prompt can arrive
    # over stdin instead of argparse/argv.
    os.environ["HERMES_YOLO_MODE"] = "1"

    # Connect the bundled-tools MCP shim (mcp_servers.kern in the
    # managed ~/.hermes/config.yaml) before the agent snapshots its tool
    # list. Hermes only starts MCP discovery from its TUI, gateway, and ACP
    # entrypoints, never from the single-query path this wrapper uses, so
    # discovery must run here, synchronously: the server is a local stdio
    # spawn, and a background thread could miss the first (only) turn. A
    # shim that fails to serve tools just leaves them unregistered, matching
    # the shim's own omit-unavailable contract for the other harnesses.
    importlib.import_module("tools.mcp_tool").discover_mcp_tools()

    # Stream live tool activity to stdout for Agent Chat, the same surface the
    # Codex and Claude Code adapters populate from their event streams. Only
    # when the host handed us a nonce to frame it with.
    if _ACTIVITY_MARKER:
        _register_activity_hooks()

    hermes_main = importlib.import_module("cli").main

    hermes_main(
        query=prompt,
        model=args.model,
        toolsets="terminal,file,kern",
        quiet=True,
        resume=args.resume,
        pass_session_id=True,
    )


if __name__ == "__main__":
    main()
