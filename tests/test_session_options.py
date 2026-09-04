from __future__ import annotations

import unittest

from host.session_options import (
    DEFAULT_INTERACTIVE_MODELS,
    INTERACTIVE_SESSION_OPTIONS,
    SCRIPT_SESSION_OPTIONS,
    SESSION_OPTIONS,
    public_session_options,
    recorded_session_config,
    schedule_session_options,
    session_config_error,
)


class SessionOptionsTests(unittest.TestCase):
    def test_interactive_defaults_are_explicit_and_selectable(self) -> None:
        self.assertEqual(
            DEFAULT_INTERACTIVE_MODELS,
            {
                "codex": "gpt-5.6-sol",
                "claude_code": "claude-opus-5",
                "grok": "grok-4.6",
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
                "claude_code": {
                    "claude-opus-5": ("high", "max", "ultracode"),
                    "claude-fable-5-1": ("high", "max", "ultracode"),
                    "claude-sonnet-5": ("high", "max", "ultracode"),
                },
                "grok": {
                    "grok-4.6": ("xhigh", "high"),
                },
                "hermes": {
                    "deepseek.v3.2": ("high",),
                    "qwen.qwen3-coder-next": ("high",),
                    "moonshotai.kimi-k2.5": ("high",),
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
