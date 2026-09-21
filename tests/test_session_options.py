from __future__ import annotations

from pathlib import Path
import re
import unittest

from host.session_options import (
    DEFAULT_INTERACTIVE_MODELS,
    INTERACTIVE_RUNTIMES,
    INTERACTIVE_SESSION_OPTIONS,
    RUNTIMES,
    SCRIPT_SESSION_OPTIONS,
    SESSION_OPTIONS,
    public_session_options,
    recorded_session_config,
    schedule_session_options,
    session_config_error,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
_ALL_LABELS = {identity.label for identity in RUNTIMES.values()}


def _object_body(source: str) -> str:
    """The rest of one JavaScript object literal, whose opening brace is read."""
    depth = 1
    for index, character in enumerate(source):
        depth += {"{": 1, "}": -1}.get(character, 0)
        if depth == 0:
            return source[:index]
    raise AssertionError("unterminated object literal")


class RuntimeListTests(unittest.TestCase):
    """Hold every copy of the runtime list equal to RUNTIMES.

    Adding a runtime touches Python, SQL, shell and JavaScript. The copies
    below cannot import each other, so each one that can drift silently is
    compared here instead.
    """

    def test_every_runtime_has_a_model_matrix_and_a_default(self) -> None:
        self.assertEqual(tuple(INTERACTIVE_SESSION_OPTIONS), INTERACTIVE_RUNTIMES)
        self.assertEqual(tuple(DEFAULT_INTERACTIVE_MODELS), INTERACTIVE_RUNTIMES)
        self.assertEqual(tuple(SESSION_OPTIONS), tuple(RUNTIMES))

    def test_the_delegation_schema_offers_every_interactive_runtime(self) -> None:
        from host import agent_tool_surface
        schema = agent_tool_surface.SPAWN_AGENT_TOOL["input_schema"]
        self.assertEqual(
            schema["properties"]["agent_runtime"]["enum"], list(INTERACTIVE_RUNTIMES)
        )

    def test_the_python_runtime_sets_are_the_same_list(self) -> None:
        from host import config
        from host.runtime.admin_api import runtime_accounts
        from host.runtime.workspace.chat import backend
        self.assertEqual(config.AGENT_RUNTIMES, set(RUNTIMES))
        self.assertEqual(backend.RUNTIME_OPTIONS, set(INTERACTIVE_RUNTIMES))
        self.assertEqual(runtime_accounts.AGENT_RUNTIME_TYPES, INTERACTIVE_RUNTIMES)
        # Every OAuth runtime is a runtime, and the one that is not has no
        # device login to offer.
        self.assertEqual(
            set(runtime_accounts.OAUTH_RUNTIME_TYPES) - set(INTERACTIVE_RUNTIMES), set()
        )

    def test_the_harness_registry_covers_every_runtime(self) -> None:
        from host.runtime.agent_runtime.harness_registry import HARNESSES
        self.assertEqual(tuple(HARNESSES), tuple(RUNTIMES))
        for runtime, adapter in HARNESSES.items():
            with self.subTest(runtime=runtime):
                self.assertEqual(adapter.label, RUNTIMES[runtime].label)
                self.assertEqual(adapter.managed_provider, RUNTIMES[runtime].provider)

    def test_every_browser_copy_of_the_labels_agrees(self) -> None:
        # Six bundles across three served apps name the runtimes, because the
        # admin UI, Chat and the Web App builder ship separately and share no
        # module. None can import Python, so each is compared here. A file may
        # list a subset (Analytics shows no script runtime; the device-login
        # table only covers OAuth runtimes) but may not rename one.
        for path, (anchor, expected) in self._runtime_label_sources().items():
            source = (REPO_ROOT / path).read_text()
            # Read the declaration itself, not whichever quoted string happens
            # to sit beside a runtime name: a renamed label has to fail here,
            # and searching by known label would simply stop finding it.
            self.assertIn(anchor, source, path)
            block = _object_body(source.split(anchor, 1)[1])
            declared = {}
            for runtime in RUNTIMES:
                key = rf'"?{re.escape(runtime)}"?\s*:\s*(?:\{{[^}}]*label:\s*)?"([^"]+)"'
                match = re.search(key, block)
                if match:
                    declared[runtime] = match.group(1)
            with self.subTest(path=path):
                # Compare against the runtimes this file must carry, not
                # against the ones it happens to carry: an expectation built
                # from `declared` cannot notice a missing entry.
                self.assertEqual(
                    declared,
                    {runtime: RUNTIMES[runtime].label for runtime in expected},
                )

    def test_no_browser_copy_of_the_labels_is_unchecked(self) -> None:
        # The check above is a fixed list, so a seventh copy would escape it.
        # Runtime labels are distinctive strings: any bundle carrying two of
        # them is a copy and belongs in that list.
        checked = set(self._runtime_label_sources())
        for path in sorted((REPO_ROOT / "host").rglob("*.js")):
            source = path.read_text()
            named = [label for label in _ALL_LABELS if f'"{label}"' in source]
            relative = str(path.relative_to(REPO_ROOT))
            if len(named) > 1 and relative not in checked:
                self.fail(f"{relative} names runtimes {named} but is not checked")

    @staticmethod
    def _runtime_label_sources() -> dict[str, tuple[str, tuple[str, ...]]]:
        """Each browser copy of the labels: the declaration, and what it covers.

        The covered set is stated per file and derived, so adding a runtime
        fails here until every bundle that must name it does.
        """
        from host.runtime.admin_api.runtime_accounts import OAUTH_RUNTIME_TYPES

        # Claude's login is not a device-code flow, so the health panel's
        # device-login table is the OAuth runtimes without it.
        device_logins = tuple(r for r in OAUTH_RUNTIME_TYPES if r != "claude_code")
        return {
            "host/runtime/admin_api/admin_ui/helpers.js":
                ("export const RUNTIME_PROVIDERS = {", INTERACTIVE_RUNTIMES),
            "host/runtime/admin_api/admin_ui/health.js":
                ("const DEVICE_LOGINS = {", device_logins),
            "host/runtime/admin_api/admin_ui/analytics.js":
                ("const runtimes = {", INTERACTIVE_RUNTIMES),
            "host/runtime/workspace/ui/workspace.js":
                ("const runtimeLabel = runtime => ({", tuple(RUNTIMES)),
            "host/runtime/workspace/chat/ui/agent_chat.js":
                ("const runtimeLabel = runtime => ({", INTERACTIVE_RUNTIMES),
            "host/runtime/workspace/web_apps/ui/personal_web_app_builder.js":
                ("const runtimeLabel = runtime => ({", INTERACTIVE_RUNTIMES),
        }

    def test_the_browser_default_models_agree(self) -> None:
        # Chat and the Web App builder preselect a model before the options
        # request returns, so they carry their own copy of the defaults.
        for path in (
            "host/runtime/workspace/chat/ui/agent_chat.js",
            "host/runtime/workspace/web_apps/ui/personal_web_app_builder.js",
        ):
            source = (REPO_ROOT / path).read_text()
            block = source.split("DEFAULT_MODELS = Object.freeze({", 1)[1].split("})", 1)[0]
            declared = {
                runtime.strip().strip('"'): model
                for runtime, model in re.findall(r'([\w"-]+):\s*"([^"]+)"', block)
            }
            with self.subTest(path=path):
                self.assertEqual(declared, DEFAULT_INTERACTIVE_MODELS)

    def test_the_root_grok_launchers_accept_exactly_the_grok_runtimes(self) -> None:
        # Both helpers validate against a literal allowlist: they run as root
        # before any Python is loaded, and an unknown runtime must be refused
        # rather than defaulted into someone else's home.
        from host.runtime.agent_runtime.grok_agent import GROK_RUNTIME_TYPES
        for helper in ("run-grok.sh", "read-grok-account.sh"):
            with self.subTest(helper=helper):
                source = (REPO_ROOT / "host/bootstrap/helpers" / helper).read_text()
                quoted = re.findall(r'"(grok(?:-\d+)?)"', source)
                self.assertEqual(set(quoted), set(GROK_RUNTIME_TYPES))


class SessionOptionsTests(unittest.TestCase):
    def test_interactive_defaults_are_explicit_and_selectable(self) -> None:
        self.assertEqual(
            DEFAULT_INTERACTIVE_MODELS,
            {
                "codex": "gpt-5.6-sol",
                "codex-2": "gpt-5.6-sol",
                "codex-3": "gpt-5.6-sol",
                "claude_code": "claude-opus-5",
                "grok": "grok-4.6",
                "grok-2": "grok-4.6",
                "hermes": "moonshotai.kimi-k2.5",
            },
        )
        for runtime, model in DEFAULT_INTERACTIVE_MODELS.items():
            with self.subTest(runtime=runtime):
                self.assertIn(model, INTERACTIVE_SESSION_OPTIONS[runtime])
                self.assertIn("high", INTERACTIVE_SESSION_OPTIONS[runtime][model])

    def test_exposes_only_the_operator_session_options(self) -> None:
        self.assertEqual(
            INTERACTIVE_SESSION_OPTIONS,
            {
                "codex": {
                    "gpt-5.6-terra": ("high", "max", "ultra"),
                    "gpt-5.6-sol": ("high", "max", "ultra"),
                    "gpt-5.6-luna": ("high", "max"),
                    "gpt-6-astra": ("high", "max", "ultra"),
                },
                "codex-2": {
                    "gpt-5.6-terra": ("high", "max", "ultra"),
                    "gpt-5.6-sol": ("high", "max", "ultra"),
                    "gpt-5.6-luna": ("high", "max"),
                    "gpt-6-astra": ("high", "max", "ultra"),
                },
                "codex-3": {
                    "gpt-5.6-terra": ("high", "max", "ultra"),
                    "gpt-5.6-sol": ("high", "max", "ultra"),
                    "gpt-5.6-luna": ("high", "max"),
                    "gpt-6-astra": ("high", "max", "ultra"),
                },
                "claude_code": {
                    "claude-opus-5": ("high", "max", "ultracode"),
                    "claude-fable-5-1": ("high", "max", "ultracode"),
                    "claude-sonnet-5": ("high", "max", "ultracode"),
                },
                "grok": {
                    "grok-4.6": ("xhigh", "high"),
                },
                "grok-2": {
                    "grok-4.6": ("xhigh", "high"),
                },
                "hermes": {
                    "deepseek.v3.2": ("high",),
                    "qwen.qwen3-coder-next": ("high",),
                    "moonshotai.kimi-k2.5": ("high",),
                    "zai.glm-5": ("high",),
                },
            },
        )

    def test_the_script_runtime_has_one_fixed_configuration(self) -> None:
        self.assertEqual(SCRIPT_SESSION_OPTIONS, {"script": {"bash": ("fixed",)}})
        self.assertEqual(
            SESSION_OPTIONS,
            {**INTERACTIVE_SESSION_OPTIONS, **SCRIPT_SESSION_OPTIONS},
        )

    def test_the_script_runtime_runs_only_where_it_is_opted_into(self) -> None:
        # Conversational surfaces leave allow_script off, so the runtime they
        # cannot use is rejected by name rather than reaching an adapter that
        # would read their prompt as a path.
        self.assertIsNotNone(session_config_error("script", "bash", "fixed"))
        self.assertIsNone(
            session_config_error("script", "bash", "fixed", allow_script=True)
        )
        # Opting in widens the runtimes, not the models: the one script
        # configuration is still the only one.
        for model, effort in (("bash", "high"), ("python", "fixed"), ("bash", "max")):
            with self.subTest(model=model, effort=effort):
                self.assertIsNotNone(
                    session_config_error("script", model, effort, allow_script=True)
                )
        # ...and it leaves the model runtimes exactly as they were.
        self.assertIsNone(
            session_config_error("codex", "gpt-5.6-sol", "ultra", allow_script=True)
        )
        self.assertIsNotNone(
            session_config_error("codex", "gpt-5.6-luna", "ultra", allow_script=True)
        )

    def test_only_schedules_offer_the_script_runtime(self) -> None:
        self.assertNotIn("script", public_session_options())
        self.assertEqual(schedule_session_options()["script"], {"bash": ["fixed"]})
        self.assertEqual(
            schedule_session_options()["codex"], public_session_options()["codex"]
        )

    def test_rejects_cross_runtime_and_luna_ultra_combinations(self) -> None:
        self.assertIsNone(session_config_error("codex", "gpt-5.6-sol", "ultra"))
        self.assertIsNone(session_config_error("claude_code", "claude-fable-5-1", "ultracode"))
        self.assertIsNotNone(session_config_error("codex", "gpt-5.6-luna", "ultra"))
        self.assertIsNotNone(session_config_error("codex", "claude-opus-5", "high"))
        self.assertIsNotNone(session_config_error("claude_code", "claude-fable-5-1", "ultra"))
        self.assertIsNotNone(session_config_error("unsupported", "deepseek.v3.2", "max"))
        self.assertIsNone(session_config_error("hermes", "deepseek.v3.2", "high"))
        self.assertIsNotNone(session_config_error("hermes", "deepseek.v3.2", "max"))
        self.assertIsNone(session_config_error("grok", "grok-4.6", "xhigh"))
        self.assertIsNotNone(session_config_error("grok", "grok-4.6", "max"))

    def test_rejects_the_superseded_claude_code_models(self) -> None:
        # Aliases and earlier exact ids remain readable from recorded sessions,
        # but cannot start a thread or run new work on one.
        for model in ("opus", "fable", "sonnet", "claude-fable-5"):
            self.assertIsNotNone(session_config_error("claude_code", model, "high"))

    def test_recorded_config_accepts_any_model_and_checks_only_the_shape(self) -> None:
        # The read path: a recorded configuration may predate the matrix, so
        # history stays readable whatever it names.
        self.assertEqual(
            recorded_session_config({"agent_runtime": "claude_code", "model": "opus", "effort": "high"}),
            ("claude_code", "opus", "high"),
        )
        self.assertEqual(
            recorded_session_config(
                {"agent_runtime": "retired_runtime", "model": "retired-model", "effort": "retired"}
            ),
            ("retired_runtime", "retired-model", "retired"),
        )
        for payload in (
            {},
            {"agent_runtime": "claude_code", "model": "claude-opus-5"},
            {"agent_runtime": "claude_code", "model": "", "effort": "high"},
            {"agent_runtime": "claude_code", "model": 5, "effort": "high"},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(recorded_session_config(payload))

    def test_public_options_are_json_facing_copies(self) -> None:
        options = public_session_options()
        self.assertEqual(options["codex"]["gpt-5.6-luna"], ["high", "max"])
        self.assertEqual(options["codex"]["gpt-6-astra"], ["high", "max", "ultra"])
        self.assertEqual(
            options["claude_code"]["claude-fable-5-1"],
            ["high", "max", "ultracode"],
        )
        self.assertEqual(options["grok"]["grok-4.6"], ["xhigh", "high"])
        options["codex"]["gpt-5.6-luna"].append("invalid")
        schedule_session_options()["script"]["bash"].append("invalid")
        self.assertEqual(SESSION_OPTIONS["codex"]["gpt-5.6-luna"], ("high", "max"))
        self.assertEqual(
            SESSION_OPTIONS["codex"]["gpt-6-astra"], ("high", "max", "ultra")
        )
        self.assertEqual(SESSION_OPTIONS["script"]["bash"], ("fixed",))


if __name__ == "__main__":
    unittest.main()
