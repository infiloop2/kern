"""Tests for the admin-state storage accessors (host.runtime.core.state).

These pin the contracts the rest of the runtime is built on: mutation() spans
whole check-then-act cycles (no lost updates), an exception rolls the whole
transaction back (including events appended inside it), readers see committed
snapshots (never a torn multi-row write), and event seqs never appear twice in
the log. They run against the scratch cluster from pg_harness.
"""

from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from typing import Any
from unittest.mock import MagicMock, patch

import pg_harness

from host.runtime.core import db, pgclient, secretbox, state
from state_seed import read_agent_events, read_network_events
from host.runtime.core.state import (
    load_config,
    network_proxy_cert_files,
    read_claude_account,
    read_openai_account,
    read_proxy_claude_account_id,
    read_proxy_openai_account_id,
    save_config,
    save_claude_account,
    save_openai_account,
    save_proxy_claude_account_id,
    save_proxy_openai_account_id,
)


def seed_thread(
    cur: Any,
    thread_id: str,
    *,
    runtime: str = "codex",
    provider_session_id: str | None = None,
    last_used_at: str | None = "2026-06-08T00:00:00Z",
    model: str = "gpt-5.6-terra",
    effort: str = "high",
) -> None:
    state.save_thread_session(
        cur, runtime, thread_id, provider_session_id, last_used_at, model, effort
    )


class StateStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.env_patch = patch.dict("os.environ", {"KERN_STATE_DIR": self.temp_dir.name})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_mutation_persists_on_normal_exit_and_rolls_back_on_exception(self) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "t1")
        with self.assertRaises(RuntimeError):
            with state.mutation() as cur:
                seed_thread(cur, "t2")
                state.append_agent_event(cur, "thread.message", "t2", {"message": "x", "source": "user"})
                raise RuntimeError("abort the transaction")
        # The session write and the event both rolled back together, while the
        # committed thread survives.
        self.assertIsNone(state.thread_session_config("t2"))
        self.assertIsNotNone(state.thread_session_config("t1"))
        self.assertEqual(read_agent_events(), [])

    def test_agent_history_counts_are_monotonic_and_share_write_transactions(self) -> None:
        self.assertEqual(
            state.agent_history_counts(),
            {"threads": 0, "messages": 0, "activities": 0},
        )
        with state.mutation() as cur:
            seed_thread(cur, "t1")
            state.append_agent_event(
                cur, "thread.message", "t1", {"message": "hello", "source": "user"}
            )
            state.append_agent_event(
                cur,
                "thread.activity",
                "t1",
                {"activity": {"kind": "command", "title": "Checked status"}},
            )
            state.append_agent_event(
                cur, "thread.error", "t1", {"error_message": "not counted"}
            )

        self.assertEqual(
            state.agent_history_counts(),
            {"threads": 1, "messages": 1, "activities": 1},
        )

        with state.mutation() as cur:
            # Updating the provider mapping does not count the same thread twice.
            seed_thread(cur, "t1", provider_session_id="session-1")
            state.append_agent_event(
                cur, "thread.message", "t1", {"message": "again", "source": "agent"}
            )
        self.assertEqual(
            state.agent_history_counts(),
            {"threads": 1, "messages": 1, "activities": 2},
        )

        with self.assertRaises(RuntimeError):
            with state.mutation() as cur:
                seed_thread(cur, "rolled-back")
                state.append_agent_event(
                    cur,
                    "thread.activity",
                    "rolled-back",
                    {"activity": {"kind": "status", "title": "Never committed"}},
                )
                raise RuntimeError("rollback")
        self.assertEqual(
            state.agent_history_counts(),
            {"threads": 1, "messages": 1, "activities": 2},
        )

    def test_activity_event_escapes_nul_before_jsonb_persistence(self) -> None:
        with state.mutation() as cur:
            state.append_agent_event(
                cur,
                "thread.activity",
                None,
                {
                    "activity": {
                        "activity_id": "command-1",
                        "title": "binary\x00output",
                        "nested": {"output": "before\x00after"},
                    }
                },
            )

        event = read_agent_events()[0]
        self.assertEqual(event["payload"]["activity"]["title"], r"binary\0output")
        self.assertEqual(
            event["payload"]["activity"]["nested"]["output"],
            r"before\0after",
        )

    def test_oversized_event_message_is_truncated_with_a_marker(self) -> None:
        oversized = "a" * (state.MAX_EVENT_MESSAGE_CHARS + 5000)
        with state.mutation() as cur:
            state.append_agent_event(
                cur, "thread.message", "t1", {"message": oversized, "source": "agent"}
            )
            state.append_agent_event(
                cur, "thread.error", "t1", {"error_message": oversized}
            )
        events = {event["event_type"]: event for event in read_agent_events()}
        stored_message = events["thread.message"]["payload"]["message"]
        stored_error = events["thread.error"]["payload"]["error_message"]
        for stored in (stored_message, stored_error):
            self.assertLess(len(stored), len(oversized))
            self.assertTrue(stored.startswith("a" * state.MAX_EVENT_MESSAGE_CHARS))
            self.assertIn(f"truncated {len(oversized)} chars", stored)

    def test_in_bound_event_message_is_stored_verbatim(self) -> None:
        exact = "b" * state.MAX_EVENT_MESSAGE_CHARS
        with state.mutation() as cur:
            state.append_agent_event(
                cur, "thread.message", "t1", {"message": exact, "source": "agent"}
            )
        self.assertEqual(read_agent_events()[0]["payload"]["message"], exact)

    def test_thread_summaries_report_the_canonical_session_rows(self) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "thread-t1", last_used_at="2026-06-08T00:00:01Z")
            state.append_agent_event(
                cur,
                "thread.message",
                "thread-t1",
                {"message": "hello", "source": "user"},
            )
            latest_event_seq = state.append_agent_event(
                cur,
                "thread.message",
                "thread-t1",
                {"message": "same-second follow-up", "source": "agent"},
            )
            seed_thread(
                cur,
                "thread-t2",
                runtime="claude_code",
                model="claude-fable-5",
                effort="max",
                provider_session_id="sess-2",
                last_used_at=None,
            )
        summaries = {
            row["thread_id"]: row
            for row in state.page_thread_summaries(None, 100)
        }
        self.assertEqual(set(summaries), {"thread-t1", "thread-t2"})
        # Exactly the session configuration: no provider session id, no status
        # (live status is in-process orchestrator state), no counts.
        self.assertEqual(
            summaries["thread-t1"],
            {
                "thread_id": "thread-t1",
                "agent_runtime": "codex",
                "model": "gpt-5.6-terra",
                "effort": "high",
                "last_used_at": "2026-06-08T00:00:01Z",
                "status": "idle",
                "latest_event_seq": latest_event_seq,
            },
        )
        # A never-used thread reads as an empty last_used_at, not None.
        self.assertEqual(summaries["thread-t2"]["last_used_at"], "")
        self.assertEqual(summaries["thread-t2"]["latest_event_seq"], 0)
        self.assertEqual(summaries["thread-t2"]["agent_runtime"], "claude_code")

    def test_thread_summary_pages_use_stable_sort_key_and_prefix_filter(self) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "thread-1", last_used_at="2026-06-08T00:00:02Z")
            seed_thread(cur, "thread-2", last_used_at="2026-06-08T00:00:02Z")
            seed_thread(cur, "app-1", last_used_at="2026-06-08T00:00:03Z")

        first = state.page_thread_summaries(
            None,
            1,
            thread_prefix="thread-",
        )
        self.assertEqual([row["thread_id"] for row in first], ["thread-2"])
        cursor = (
            first[-1]["last_used_at"],
            first[-1]["thread_id"],
        )
        second = state.page_thread_summaries(
            cursor,
            1,
            thread_prefix="thread-",
        )
        self.assertEqual([row["thread_id"] for row in second], ["thread-1"])

    def test_every_session_option_satisfies_the_thread_constraint(self) -> None:
        # The thread_sessions_options_check constraint must track the
        # operator-facing option matrix exactly: a combination the API offers
        # that the constraint rejects fails the first turn on that thread.
        from host.session_options import SESSION_OPTIONS

        with state.mutation() as cur:
            for runtime, models in SESSION_OPTIONS.items():
                for model, efforts in models.items():
                    for effort in efforts:
                        state.save_thread_session(
                            cur,
                            runtime,
                            f"opt-{runtime}-{model}-{effort}",
                            None,
                            "2026-06-08T00:00:00Z",
                            model,
                            effort,
                        )

    def test_thread_session_configuration_is_immutable(self) -> None:
        with state.mutation() as cur:
            state.save_thread_session(
                cur,
                "codex",
                "fixed-thread",
                None,
                "2026-06-08T00:00:00Z",
                "gpt-5.6-terra",
                "high",
            )

        with self.assertRaises(ValueError), state.mutation() as cur:
            state.save_thread_session(
                cur,
                "codex",
                "fixed-thread",
                None,
                "2026-06-08T00:00:01Z",
                "gpt-5.6-sol",
                "high",
            )

        # The cursor argument is optional and last: helpers inside a mutation
        # pass theirs, plain readers omit it.
        with state.mutation() as cur:
            config = state.thread_session_config("fixed-thread", cur)
        assert config is not None
        self.assertEqual((config["model"], config["effort"]), ("gpt-5.6-terra", "high"))
        self.assertEqual(state.thread_session_config("fixed-thread"), config)

    def test_idle_thread_session_can_rotate_and_running_thread_cannot(self) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "chat", provider_session_id="codex-session")
            state.rotate_thread_session(
                cur,
                "chat",
                "claude_code",
                "claude-opus-5",
                "max",
                "2026-06-08T00:00:09Z",
            )

        config = state.thread_session_config("chat")
        assert config is not None
        self.assertEqual(config["agent_runtime"], "claude_code")
        self.assertEqual(config["model"], "claude-opus-5")
        self.assertEqual(config["effort"], "max")
        self.assertIsNone(config["provider_session_id"])
        self.assertEqual(config["last_used_at"], "2026-06-08T00:00:09Z")

        with state.mutation() as cur:
            state.start_thread_run(cur, "chat")
        with self.assertRaisesRegex(ValueError, "running or does not exist"), state.mutation() as cur:
            state.rotate_thread_session(
                cur,
                "chat",
                "codex",
                "gpt-5.6-terra",
                "high",
                "2026-06-08T00:00:10Z",
            )
        self.assertEqual(state.thread_session_config("chat")["agent_runtime"], "claude_code")

    def test_clearing_working_memory_drops_the_session_and_fences_the_handoff(
        self,
    ) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "chat", provider_session_id="codex-session")
            state.append_agent_event(
                cur, "thread.message", "chat", {"message": "before", "source": "user"}
            )
            cleared_seq = state.append_agent_event(
                cur, "thread.activity", "chat", {"activity": {"title": "cleared"}}
            )
            state.clear_thread_context(cur, "chat", cleared_seq, "2026-06-08T00:00:09Z")

        config = state.thread_session_config("chat")
        assert config is not None
        self.assertIsNone(config["provider_session_id"])
        self.assertEqual(config["context_cleared_seq"], cleared_seq)
        # Runtime configuration is untouched; only the provider session goes.
        self.assertEqual(config["agent_runtime"], "codex")

        with state.mutation() as cur:
            # The marker itself is at the floor, so the next run is handed
            # nothing at all rather than a summary of what was cleared.
            self.assertEqual(
                state.recent_thread_handoff_events(
                    cur,
                    "chat",
                    message_character_limit=1000,
                    activity_character_limit=1000,
                    activity_event_character_limit=1000,
                    after_seq=config["context_cleared_seq"],
                ),
                [],
            )
            state.append_agent_event(
                cur, "thread.message", "chat", {"message": "after", "source": "user"}
            )
            resumed = state.recent_thread_handoff_events(
                cur,
                "chat",
                message_character_limit=1000,
                activity_character_limit=1000,
                activity_event_character_limit=1000,
                after_seq=config["context_cleared_seq"],
            )
        self.assertEqual([event["payload"]["message"] for event in resumed], ["after"])

    def test_clearing_working_memory_requires_idle_and_never_lowers_the_floor(
        self,
    ) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "chat", provider_session_id="codex-session")
            state.clear_thread_context(cur, "chat", 50, "2026-06-08T00:00:09Z")
            # A late or out-of-order caller cannot restore cleared context.
            state.clear_thread_context(cur, "chat", 20, "2026-06-08T00:00:10Z")
        self.assertEqual(
            state.thread_session_config("chat")["context_cleared_seq"], 50
        )

        with state.mutation() as cur:
            state.start_thread_run(cur, "chat")
        with self.assertRaisesRegex(ValueError, "running or does not exist"), state.mutation() as cur:
            state.clear_thread_context(cur, "chat", 90, "2026-06-08T00:00:11Z")
        self.assertEqual(
            state.thread_session_config("chat")["context_cleared_seq"], 50
        )

    def test_recent_thread_handoff_events_use_independent_activity_and_message_windows(
        self,
    ) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "chat")
            state.append_agent_event(
                cur, "thread.message", "chat", {"message": "oldest", "source": "user"}
            )
            state.append_agent_event(
                cur, "thread.error", "chat", {"error_message": "not conversation"}
            )
            state.append_agent_event(cur, "thread.stopped", "chat", {})
            state.append_agent_event(
                cur,
                "thread.activity",
                "chat",
                {
                    "activity": {
                        "provider": "codex",
                        "activity_id": "command-1",
                        "kind": "command",
                        "phase": "completed",
                        "title": "Inspect files",
                        "detail": "d" * 250,
                        "output": "full output",
                    }
                },
            )
            state.append_agent_event(
                cur, "thread.message", "chat", {"message": "newest", "source": "agent"}
            )
            events = state.recent_thread_handoff_events(
                cur,
                "chat",
                message_character_limit=200,
                activity_character_limit=200,
                activity_event_character_limit=200,
            )

        self.assertEqual(
            [event["event_type"] for event in events],
            ["thread.message", "thread.activity", "thread.message"],
        )
        activity = events[1]["payload"]["activity"]
        self.assertEqual(activity["detail"], "d" * 250)
        self.assertEqual(activity["output"], "full output")

    def test_handoff_window_weights_activity_by_its_compacted_size(self) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "chat")
            state.append_agent_event(
                cur,
                "thread.message",
                "chat",
                {"message": "important earlier decision", "source": "user"},
            )
            state.append_agent_event(
                cur,
                "thread.activity",
                "chat",
                {
                    "activity": {
                        "provider": "codex",
                        "activity_id": "command-1",
                        "kind": "command",
                        "phase": "completed",
                        "title": "Large output",
                        "output": "o" * 10_000,
                    }
                },
            )
            state.append_agent_event(
                cur, "thread.message", "chat", {"message": "newest", "source": "agent"}
            )
            events = state.recent_thread_handoff_events(
                cur,
                "chat",
                message_character_limit=1_000,
                activity_character_limit=200,
                activity_event_character_limit=200,
            )

        self.assertEqual(
            [event["event_type"] for event in events],
            ["thread.message", "thread.activity", "thread.message"],
        )
        self.assertEqual(events[0]["payload"]["message"], "important earlier decision")

    def test_provider_session_callback_updates_only_its_matching_run(self) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "chat", provider_session_id="existing-session")
            first_run = state.start_thread_run(cur, "chat")
            state.save_thread_provider_session(
                cur,
                "chat",
                first_run,
                "accepted-session",
            )
            state.finish_thread_run(cur, "chat", first_run)

        self.assertEqual(
            state.thread_session_config("chat")["provider_session_id"],
            "accepted-session",
        )
        with self.assertRaises(ValueError), state.mutation() as cur:
            state.save_thread_provider_session(cur, "chat", first_run + 1, "late-session")
        self.assertEqual(
            state.thread_session_config("chat")["provider_session_id"],
            "accepted-session",
        )

    def test_provider_session_callback_rejects_an_empty_id(self) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "chat", provider_session_id="existing-session")
            run_number = state.start_thread_run(cur, "chat")
        with self.assertRaisesRegex(ValueError, "must not be empty"), state.mutation() as cur:
            state.save_thread_provider_session(cur, "chat", run_number, "")
        self.assertEqual(
            state.thread_session_config("chat")["provider_session_id"],
            "existing-session",
        )

    def test_concurrent_mutations_never_lose_increments(self) -> None:
        # The mutation lock must span each whole read-modify-write cycle:
        # interleaved cycles would drop increments. 8 threads x 25 must land.
        def bump(cur: Any) -> int:
            cur.execute("SELECT value FROM counters WHERE name = 'test_counter'")
            row = cur.fetchone()
            value = (int(row[0]) if row else 0) + 1
            cur.execute(
                "INSERT INTO counters (name, value) VALUES ('test_counter', %s)"
                " ON CONFLICT (name) DO UPDATE SET value = EXCLUDED.value",
                (value,),
            )
            return value

        threads, per_thread = 8, 25
        barrier = threading.Barrier(threads)
        errors: list[BaseException] = []

        def work() -> None:
            try:
                barrier.wait(timeout=10)
                for _ in range(per_thread):
                    with state.mutation() as cur:
                        bump(cur)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                errors.append(exc)

        workers = [threading.Thread(target=work) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=60)
        self.assertEqual(errors, [])
        with state.mutation() as cur:
            self.assertEqual(bump(cur), threads * per_thread + 1)

    def test_reads_inside_a_mutation_see_committed_state_and_do_not_deadlock(self) -> None:
        # Helpers called from inside a mutation may take read-only snapshots
        # (the lock is reentrant and reads run on their own connections); the
        # snapshot sees the last *committed* state, not the in-progress write.
        with state.mutation() as cur:
            state.set_oauth_login(cur, "claude", {"status": "awaiting_code", "login_url": "u", "expires_at": "e"})
            self.assertIsNone(state.oauth_login("claude"))
        login = state.oauth_login("claude")
        assert login is not None
        self.assertEqual(login["status"], "awaiting_code")

    def test_concurrent_readers_never_see_a_torn_multi_row_write(self) -> None:
        # A writer keeps two oauth rows in sync inside one mutation; a reader
        # must never observe them apart (each read is one statement snapshot).
        def read_pair() -> tuple[Any, Any]:
            with db.transaction() as cur:
                cur.execute("SELECT runtime, status FROM oauth_logins")
                rows = dict(cur.fetchall())
            return rows.get("codex"), rows.get("claude")

        def login_pair(value: int) -> tuple[dict[str, Any], dict[str, Any]]:
            codex = {"status": f"step-{value}", "login_url": "u", "expires_at": "e",
                     "device_code": "D", "login_id": "L"}
            claude = {"status": f"step-{value}", "login_url": "u", "expires_at": "e"}
            return codex, claude

        with state.mutation() as cur:
            codex, claude = login_pair(0)
            state.set_oauth_login(cur, "codex", codex)
            state.set_oauth_login(cur, "claude", claude)
        stop = threading.Event()
        errors: list[str] = []

        def reader() -> None:
            while not stop.is_set():
                left, right = read_pair()
                if left != right:
                    errors.append(f"torn read: codex={left} claude={right}")
                    return

        reader_thread = threading.Thread(target=reader)
        reader_thread.start()
        try:
            for value in range(1, 100):
                with state.mutation() as cur:
                    codex, claude = login_pair(value)
                    state.set_oauth_login(cur, "codex", codex)
                    state.set_oauth_login(cur, "claude", claude)
        finally:
            stop.set()
            reader_thread.join(timeout=30)
        self.assertEqual(errors, [])

    def test_aborted_mutation_never_leaves_a_duplicate_event_seq(self) -> None:
        with self.assertRaises(RuntimeError):
            with state.mutation() as cur:
                state.append_agent_event(cur, "test.aborted", None, {})
                raise RuntimeError("abort after allocating a seq")
        with state.mutation() as cur:
            state.append_agent_event(cur, "test.committed", None, {})
        events = read_agent_events()
        self.assertEqual([event["event_type"] for event in events], ["test.committed"])
        seqs = [event["seq"] for event in events]
        self.assertEqual(len(seqs), len(set(seqs)), f"duplicate event seqs: {seqs}")

    def test_event_pages_are_newest_first_and_cursor_bounded(self) -> None:
        with state.mutation() as cur:
            state.append_agent_event(
                cur,
                "thread.activity",
                "t1",
                {"activity": {"activity_id": "command-1", "kind": "command", "output": "done"}},
            )
            for index in range(8):
                state.append_agent_event(cur, "thread.message", "t1" if index % 2 else None, {"message": f"m{index}", "source": "user"})
        page = state.page_agent_events_before(None, limit=5)
        self.assertEqual(len(page), 5)
        seqs = [event["seq"] for event in page]
        self.assertEqual(seqs, sorted(seqs, reverse=True))
        older = state.page_agent_events_before(seqs[-1], limit=5)
        self.assertTrue(older)
        self.assertTrue(all(event["seq"] < seqs[-1] for event in older))
        thread_page = state.page_thread_events("t1", None, 100)
        self.assertTrue(all(event["thread_id"] == "t1" for event in thread_page))
        activity = next(event for event in thread_page if event["event_type"] == "thread.activity")
        self.assertEqual(activity["payload"]["activity"]["output"], "done")

    def test_thread_events_open_at_latest_and_page_in_both_directions(self) -> None:
        with state.mutation() as cur:
            state.append_agent_event(cur, "thread.message", "chat", {"message": "a", "source": "user"})
            state.append_agent_event(cur, "thread.message", "chat", {"message": "b", "source": "agent"})
            state.append_agent_event(cur, "thread.message", "other", {"message": "c", "source": "agent"})
            state.append_agent_event(cur, "thread.message", "chat", {"message": "d", "source": "agent"})
        all_events = state.page_thread_events("chat", None, 100)
        # All of the thread's events, chronological, and never the other thread's.
        self.assertEqual([event["payload"]["message"] for event in all_events], ["a", "b", "d"])
        seqs = [event["seq"] for event in all_events]
        self.assertEqual(seqs, sorted(seqs))
        # An uncursored bounded page opens at the newest events.
        latest = state.page_thread_events("chat", None, 2)
        self.assertEqual([event["payload"]["message"] for event in latest], ["b", "d"])
        # A before cursor walks backward while preserving chronological output.
        older = state.page_thread_events("chat", None, 2, before=latest[0]["seq"])
        self.assertEqual([event["payload"]["message"] for event in older], ["a"])
        # A since cursor returns only newer events.
        rest = state.page_thread_events("chat", seqs[0], 100)
        self.assertEqual([event["payload"]["message"] for event in rest], ["b", "d"])

    def test_thread_events_can_filter_event_types_before_pagination(self) -> None:
        with state.mutation() as cur:
            state.append_agent_event(
                cur,
                "thread.message",
                "chat",
                {"message": "a", "source": "user"},
            )
            for index in range(8):
                state.append_agent_event(cur, "thread.activity", "chat", {
                    "activity": {
                        "activity_id": f"work-{index}",
                        "kind": "command",
                        "phase": "started",
                    }
                })
            state.append_agent_event(
                cur,
                "thread.message",
                "chat",
                {"message": "b", "source": "agent"},
            )
        messages = state.page_thread_events(
            "chat",
            None,
            2,
            event_types=("thread.message",),
        )
        self.assertEqual(
            [event["payload"]["message"] for event in messages],
            ["a", "b"],
        )

    def test_conversation_search_supports_relevance_time_and_stable_cursors(self) -> None:
        with state.mutation() as cur:
            cur.execute(
                "INSERT INTO chat_threads (thread_id, archived) VALUES"
                " ('thread-1', FALSE), ('thread-2', FALSE), ('thread-3', FALSE)"
            )
            for thread_id, timestamp, message, source in (
                ("thread-1", "2026-06-01T00:00:00Z", "Cloudflare tunnel is healthy", "user"),
                ("thread-2", "2026-06-02T00:00:00Z", "A degraded tunnel still serves traffic", "agent"),
                ("thread-3", "2026-06-03T00:00:00Z", "Unrelated package installation", "agent"),
            ):
                seq = state.append_agent_event(
                    cur,
                    "thread.message",
                    thread_id,
                    {"message": message, "source": source},
                )
                cur.execute(
                    "UPDATE agent_events SET created_at = %s WHERE seq = %s",
                    (timestamp, seq),
                )
            orphan = state.append_agent_event(
                cur,
                "thread.message",
                "thread-99",
                {"message": "degraded tunnel traffic", "source": "agent"},
            )
            cur.execute(
                "UPDATE agent_events SET created_at = %s WHERE seq = %s",
                ("2026-06-04T00:00:00Z", orphan),
            )

        relevant = state.search_thread_messages(
            ("degraded tunnel", "tunnel traffic"),
            from_timestamp=None,
            to_timestamp=None,
            thread_id=None,
            sources=("user", "agent"),
            limit=10,
            before=None,
        )
        self.assertEqual(relevant[0]["thread_id"], "thread-99")
        self.assertIn("thread-2", {row["thread_id"] for row in relevant})
        self.assertIn("degraded", relevant[0]["excerpt"].lower())
        self.assertGreater(relevant[0]["search_rank"], 0)

        complete = state.search_thread_messages(
            ("Cloudflare tunnel",),
            from_timestamp=None,
            to_timestamp=None,
            thread_id="thread-1",
            sources=("user",),
            limit=10,
            before=None,
        )
        self.assertEqual(complete[0]["excerpt"], "[[Cloudflare]] [[tunnel]] is healthy")
        self.assertFalse(complete[0]["excerpt_truncated"])

        timed = state.search_thread_messages(
            (),
            from_timestamp="2026-06-01T12:00:00Z",
            to_timestamp="2026-06-04T00:00:00Z",
            thread_id=None,
            sources=("agent",),
            limit=1,
            before=None,
        )
        self.assertEqual([row["thread_id"] for row in timed], ["thread-3"])
        older = state.search_thread_messages(
            (),
            from_timestamp="2026-06-01T12:00:00Z",
            to_timestamp="2026-06-04T00:00:00Z",
            thread_id=None,
            sources=("agent",),
            limit=10,
            before=(timed[-1]["timestamp"], timed[-1]["seq"]),
        )
        self.assertEqual([row["thread_id"] for row in older], ["thread-2"])

    def test_conversation_search_keeps_hostile_query_text_out_of_sql(self) -> None:
        hostile = "tunnel'); DROP TABLE agent_events; SELECT pg_sleep(30); --"
        cursor = MagicMock()
        cursor.fetchall.return_value = []
        transaction = MagicMock()
        transaction.__enter__.return_value = cursor
        transaction.__exit__.return_value = False

        with patch.object(state.db, "transaction", return_value=transaction):
            self.assertEqual(
                state.search_thread_messages(
                    (hostile,),
                    from_timestamp=None,
                    to_timestamp=None,
                    thread_id=None,
                    sources=("user", "agent"),
                    limit=10,
                    before=None,
                ),
                [],
            )

        search_sql, parameters = cursor.execute.call_args_list[-1].args
        self.assertNotIn(hostile, search_sql)
        self.assertEqual(parameters[0], hostile)

    def test_thread_events_around_requires_an_anchor_in_the_thread(self) -> None:
        with state.mutation() as cur:
            seqs = [
                state.append_agent_event(
                    cur,
                    "thread.message",
                    "thread-1",
                    {"message": str(index), "source": "user"},
                )
                for index in range(5)
            ]
            other = state.append_agent_event(
                cur,
                "thread.message",
                "thread-2",
                {"message": "other", "source": "user"},
            )

        around = state.page_thread_events_around(
            "thread-1",
            seqs[2],
            3,
            event_types=("thread.message",),
        )
        self.assertEqual(
            [event["payload"]["message"] for event in around or []],
            ["1", "2", "3"],
        )
        from_start = state.page_thread_events_around(
            "thread-1",
            seqs[0],
            3,
            event_types=("thread.message",),
        )
        self.assertEqual(
            [event["payload"]["message"] for event in from_start or []],
            ["0", "1", "2"],
        )
        self.assertIsNone(
            state.page_thread_events_around(
                "thread-1", other, 3, event_types=("thread.message",)
            )
        )

    def test_recover_interrupted_thread_runs_returns_them_to_idle(self) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "done")
            seed_thread(cur, "open")
            done_run = state.start_thread_run(cur, "done")
            state.finish_thread_run(cur, "done", done_run)
            open_run = state.start_thread_run(cur, "open")
        with state.mutation() as cur:
            self.assertEqual(
                state.recover_interrupted_thread_runs(cur),
                [("open", open_run)],
            )
        self.assertEqual(state.thread_session_config("done")["status"], "idle")
        self.assertEqual(state.thread_session_config("open")["status"], "idle")
        with state.mutation() as cur:
            self.assertEqual(state.recover_interrupted_thread_runs(cur), [])

    def test_activity_ids_are_scoped_by_private_run_number(self) -> None:
        with state.mutation() as cur:
            seed_thread(cur, "chat")
            first = state.start_thread_run(cur, "chat")
            state.append_agent_event(
                cur,
                "thread.activity",
                "chat",
                {"activity": {"activity_id": "command-1"}},
                run_number=first,
            )
            state.finish_thread_run(cur, "chat", first)
            second = state.start_thread_run(cur, "chat")
            state.append_agent_event(
                cur,
                "thread.activity",
                "chat",
                {"activity": {"activity_id": "command-1"}},
                run_number=second,
            )
        activity_ids = [
            event["payload"]["activity"]["activity_id"]
            for event in read_agent_events()
        ]
        self.assertEqual(activity_ids, [f"{first}:command-1", f"{second}:command-1"])

    def test_prune_thread_sessions_keeps_event_referenced_and_newest_threads(self) -> None:
        with state.mutation() as cur:
            for number in range(1, 8):
                seed_thread(
                    cur,
                    f"thread-t{number}",
                    provider_session_id=f"ct_{number}",
                    last_used_at=f"2026-06-08T00:00:{number:02d}Z",
                )
            seed_thread(cur, "thread-c1", runtime="claude_code", model="claude-fable-5", effort="high")
            state.append_agent_event(cur, "thread.message", "thread-t1", {"message": "keep", "source": "user"})
        with state.mutation() as cur:
            state.prune_thread_sessions(cur, "codex", 3)
        remaining = {row["thread_id"] for row in state.page_thread_summaries(None, 100)}
        self.assertEqual(remaining, {"thread-t1", "thread-t5", "thread-t6", "thread-t7", "thread-c1"})
        with state.mutation() as cur:
            cur.execute("DELETE FROM agent_events WHERE thread_id = 'thread-t1'")
            state.prune_thread_sessions(cur, "codex", 3)
        remaining = {row["thread_id"] for row in state.page_thread_summaries(None, 100)}
        self.assertEqual(remaining, {"thread-t5", "thread-t6", "thread-t7", "thread-c1"})

    def test_event_logs_prune_to_the_newest_cap(self) -> None:
        # Retention is a primary-key range delete below MAX(seq) - cap: cheap
        # enough for the append cadence even at the 10M agent-event production
        # cap, pinned here with small limits.
        with state.mutation() as cur:
            for index in range(8):
                state.append_agent_event(cur, "thread.message", None, {"message": f"m{index}", "source": "user"})
        with patch.object(state, "AGENT_EVENT_LIMIT", 5), state.mutation() as cur:
            state.prune_agent_events(cur)
        seqs = [event["seq"] for event in read_agent_events()]
        self.assertEqual(len(seqs), 5)
        self.assertEqual(seqs, sorted(seqs))

        for index in range(8):
            state.append_network_event("https", "GET", "example.com", 443, f"/p{index}", "", True)
        with patch.object(state, "NETWORK_EVENT_LIMIT", 5), state.mutation() as cur:
            state.prune_network_events(cur)
        network_seqs = [event["seq"] for event in read_network_events()]
        self.assertEqual(len(network_seqs), 5)
        self.assertEqual(network_seqs, sorted(network_seqs))

        for index in range(8):
            state.record_tool_event("calendar", "list", "succeeded", f"event {index}")
        with patch.object(state, "TOOL_EVENT_LIMIT", 5), state.mutation() as cur:
            state.prune_event_logs(cur)
        tool_seqs = [event["seq"] for event in state.page_tool_events_before(None)]
        self.assertEqual(len(tool_seqs), 5)
        self.assertEqual(tool_seqs, sorted(tool_seqs, reverse=True))

    def test_network_event_url_fields_are_size_capped(self) -> None:
        # The agent's own request stream feeds this log; without field caps a
        # hostile client could turn the row cap into unbounded disk growth.
        state.append_network_event(
            "https", "GET", "h" * 600, 443, "/" + "p" * 5000, "q" * 5000, False, reason_code="r" * 900
        )
        event = read_network_events()[-1]
        self.assertEqual(len(event["host"]), 512)
        self.assertEqual(len(event["path"]), 2048)
        self.assertEqual(len(event["query"]), 2048)
        self.assertEqual(len(event["reason_code"]), 128)

    def test_proxy_cert_files_can_be_split_from_admin_state(self) -> None:
        # TLS material is the one proxy-owned file family left: the ssl module
        # and openssl consume paths, and the CA key stays out of the database.
        with tempfile.TemporaryDirectory() as proxy_tmp, patch.dict(
            "os.environ",
            {"KERN_STATE_DIR": self.temp_dir.name, "KERN_PROXY_STATE_DIR": proxy_tmp},
        ):
            cert_files = network_proxy_cert_files("example.com")
            self.assertEqual(cert_files.ca_cert, Path(proxy_tmp) / "network_proxy_ca.crt")
            self.assertEqual(cert_files.ca_key, Path(proxy_tmp) / "network_proxy_ca.key")
            self.assertEqual(cert_files.directory, Path(proxy_tmp) / "generated-certs")

    def test_proxy_pins_and_network_policy_live_in_the_database(self) -> None:
        save_proxy_openai_account_id("acct_pin")
        save_proxy_claude_account_id("claude_pin")
        self.assertEqual(read_proxy_openai_account_id(), "acct_pin")
        self.assertEqual(read_proxy_claude_account_id(), "claude_pin")
        self.assertIsNone(state.network_policy_record())
        state.save_network_policy({"network_integrations": {}}, "2026-06-08T00:00:00Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["updated_at"], "2026-06-08T00:00:00Z")

    def test_provider_account_anchor_is_immutable_in_the_database(self) -> None:
        # The anchor guard trigger: an anchored row accepts metadata rewrites
        # for the same account and the reset that clears it, and nothing else.
        anchored = {
            "account_id": "acct-1",
            "identity_attestation": "anthropic_oauth_profile",
            "email": "op@example.com",
        }
        save_claude_account(anchored)
        save_claude_account(anchored | {"claude_usage": {"weekly_used_percent": 3}})
        self.assertEqual(read_claude_account()["claude_usage"], {"weekly_used_percent": 3})
        with self.assertRaises(pgclient.Error):
            save_claude_account(anchored | {"account_id": "acct-2"})
        with self.assertRaises(pgclient.Error):
            # Stripping the approval marker would demote the row so a second
            # write could re-anchor it; the guard refuses the demotion.
            save_claude_account({"account_id": "acct-1", "email": "op@example.com"})
        with self.assertRaises(pgclient.Error):
            with state.mutation() as cur:
                cur.execute("DELETE FROM provider_accounts WHERE provider = %s", ("claude",))
        self.assertEqual(read_claude_account()["account_id"], "acct-1")
        save_claude_account(None)  # the operator reset clears the anchor
        save_claude_account({"account_id": "acct-2", "identity_attestation": "anthropic_oauth_profile"})
        self.assertEqual(read_claude_account()["account_id"], "acct-2")

    def test_provider_account_rows_without_an_anchor_are_unrestricted(self) -> None:
        # Legacy or unapproved rows carry no approval marker: they are not
        # anchors, and the first-capture flow may overwrite them freely.
        save_openai_account({"account_id": "legacy"})
        save_openai_account({"account_id": "other"})
        save_openai_account({"account_id": "acct-1", "operator_approval": "codex_device_login"})
        with self.assertRaises(pgclient.Error):
            save_openai_account({"account_id": "acct-2", "operator_approval": "codex_device_login"})
        self.assertEqual(read_openai_account()["account_id"], "acct-1")

    def test_network_policy_round_trips_claude_web_search(self) -> None:
        controls = {
            "network_integrations": {"claude": {"enabled": True, "web_search": True}},
        }
        state.save_network_policy(controls, "2026-06-08T00:00:00Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["controls"], controls)
        self.assertTrue(state.read_claude_web_search())
        # Disabling web search clears the row and the read helper reports off.
        state.save_network_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:01Z",
        )
        self.assertFalse(state.read_claude_web_search())
        record = state.network_policy_record()
        assert record is not None
        self.assertNotIn("web_search", record["controls"]["network_integrations"]["claude"])

    def test_agent_network_role_cannot_read_bedrock_connection(self) -> None:
        with db.transaction() as cur:
            cur.execute(
                "SELECT has_table_privilege('kern-agent-network', "
                "'bedrock_credentials', 'SELECT')"
            )
            self.assertEqual(cur.fetchone(), (False,))

    def test_network_policy_round_trips_integrations_and_github_repos(self) -> None:
        controls = {
            "network_integrations": {
                "openai": {"enabled": True},
                "github": {
                    "enabled": True,
                    "write_repositories": [
                        {"owner": "infiloop2", "repo": "kern"},
                        {"owner": "infiloop2", "repo": "infibot"},
                    ],
                },
                "npm_packages": {"enabled": True},
                "custom": {
                    "domains": {
                        "example.com": {
                            "allow_http_methods": ["GET"],
                            "allow_websocket": True,
                        }
                    }
                },
            },
        }
        state.save_network_policy(controls, "2026-06-08T00:00:00Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["controls"], controls)
        # Narrowing the repository list round-trips too.
        narrowed = {
            "network_integrations": {
                "github": {"enabled": True, "write_repositories": [{"owner": "infiloop2", "repo": "kern"}]},
            },
        }
        state.save_network_policy(narrowed, "2026-06-08T00:00:01Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["controls"], narrowed)
        # Replacing with a github-free policy clears the repository rows too.
        state.save_network_policy(
            {"network_integrations": {"claude": {"enabled": True}}},
            "2026-06-08T00:00:02Z",
        )
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(
            record["controls"]["network_integrations"], {"claude": {"enabled": True}}
        )

    def test_require_dot_github_approval_round_trips(self) -> None:
        controls = {
            "network_integrations": {
                "github": {
                    "enabled": True,
                    "write_repositories": [{"owner": "infiloop2", "repo": "kern"}],
                    "require_dot_github_approval": True,
                }
            },
        }
        state.save_network_policy(controls, "2026-06-08T00:00:00Z")
        record = state.network_policy_record()
        assert record is not None
        self.assertEqual(record["controls"], controls)

    def test_pending_pushes_lifecycle(self) -> None:
        state.enqueue_pending_push(
            "abc123",
            "infiloop2",
            "kern",
            [{"old": "0" * 40, "new": "1" * 40, "ref": "refs/heads/main"}],
            [".github/workflows/ci.yml"],
        )
        pending = state.read_pending_pushes()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["id"], "abc123")
        self.assertEqual(pending[0]["changed_paths"], [".github/workflows/ci.yml"])
        self.assertEqual(pending[0]["status"], "pending")
        self.assertEqual(state.count_pending_pushes(), 1)
        self.assertEqual(state.get_pending_push("abc123")["ref_updates"][0]["ref"], "refs/heads/main")
        approved = state.resolve_pending_push("abc123", "approved")
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(state.count_pending_pushes(), 0)
        self.assertEqual(state.get_pending_push("abc123")["status"], "approved")
        # Resolving a row that is no longer pending is a programming error
        # (the caller checks under RESOLVE_LOCK) and fails loudly.
        with self.assertRaises(RuntimeError):
            state.resolve_pending_push("abc123", "rejected")
        self.assertEqual(state.get_pending_push("abc123")["status"], "approved")

    def test_pending_push_retention_keeps_pending_and_newest_resolved(self) -> None:
        for push_id in ("aa0001", "aa0002", "aa0003", "aa0004"):
            state.enqueue_pending_push(
                push_id,
                "infiloop2",
                "kern",
                [{"old": "0" * 40, "new": "1" * 40, "ref": "refs/heads/main"}],
                [".github/workflows/ci.yml"],
            )
        for push_id in ("aa0001", "aa0002", "aa0003"):
            state.resolve_pending_push(push_id, "approved")

        with state.mutation() as cur:
            state.prune_pending_pushes(cur, keep=2)

        retained = {push["id"] for push in state.read_pending_pushes()}
        self.assertEqual(retained, {"aa0002", "aa0003", "aa0004"})

    def test_bedrock_usage_retention_drops_only_days_before_cutoff(self) -> None:
        with state.mutation() as cur:
            for day in ("2025-01-01", "2026-01-01", "2026-01-02"):
                cur.execute(
                    "INSERT INTO bedrock_usage (model_id, day) VALUES (%s, %s)",
                    ("deepseek.v3.2", day),
                )
            state.prune_bedrock_usage(cur, "2026-01-01")

        with state.db.transaction() as cur:
            cur.execute("SELECT day::text FROM bedrock_usage ORDER BY day")
            self.assertEqual(
                [row[0] for row in cur.fetchall()],
                ["2026-01-01", "2026-01-02"],
            )

    def test_encrypt_secret_refuses_non_string_values(self) -> None:
        # Secrets are either absent or non-empty strings; anything else is a
        # programming error that must never be stored (unencrypted or at all).
        self.assertIsNone(state._encrypt_secret(None))
        for bad in ("", 42, b"bytes", {"nested": "value"}):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                state._encrypt_secret(bad)

    def test_github_credential_round_trips_and_masks_metadata(self) -> None:
        self.assertEqual(state.read_github_credential_metadata(), {"configured": False})
        state.save_github_credential(
            {
                "mode": "pat",
                "token": "github_pat_secret",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )
        self.assertEqual(state.read_github_credential()["token"], "github_pat_secret")
        with state.db.transaction() as cur:
            cur.execute("SELECT token FROM github_credential")
            raw_token = cur.fetchone()[0]
        self.assertTrue(raw_token.startswith("enc:v1:"))
        self.assertNotIn("github_pat_secret", raw_token)
        metadata = state.read_github_credential_metadata()
        self.assertEqual(metadata["mode"], "pat")
        self.assertTrue(metadata["configured"])
        self.assertNotIn("github_pat_secret", str(metadata))

        state.save_github_credential(
            {
                "mode": "app",
                "app_id": "12345",
                "installation_id": "67890",
                "private_key_pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIB\n-----END RSA PRIVATE KEY-----",
                "updated_at": "2026-06-08T00:00:00Z",
                "validation": {"status": "not_checked"},
            }
        )
        with state.db.transaction() as cur:
            cur.execute("SELECT private_key_pem FROM github_credential")
            (raw_key,) = cur.fetchone()
        self.assertTrue(raw_key.startswith("enc:v1:"))
        # The minted working token lives only in the proxy row; its expiry
        # surfaces as app_token_expires_at in the credential metadata.
        state.save_proxy_github_token("ghs_minted", "2026-06-08T01:00:00Z")
        self.assertEqual(
            state.read_proxy_github_token_record(),
            {"token": "ghs_minted", "expires_at": "2026-06-08T01:00:00Z"},
        )
        metadata = state.read_github_credential_metadata()
        self.assertEqual(metadata["mode"], "app")
        self.assertEqual(metadata["app_token_expires_at"], "2026-06-08T01:00:00Z")
        self.assertNotIn("ghs_minted", str(metadata))
        self.assertNotIn("PRIVATE KEY", str(metadata))
        state.save_proxy_github_token(None)

        state.save_github_credential(None)
        self.assertEqual(state.read_github_credential_metadata(), {"configured": False})

    def test_proxy_github_token_round_trips_and_drives_injection_headers(self) -> None:
        from host.network_integrations.github.guard import rewrite_request_headers

        def github_credential_headers(host: str, headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
            # Only host and headers matter to the GitHub rewrite; the fuller
            # hook signature exists for integrations that re-sign bodies.
            return rewrite_request_headers(None, "GET", host, "/", "", headers, b"")

        self.assertIsNone(state.read_proxy_github_token())
        # Without a working token: agent-supplied Authorization is stripped
        # on GitHub domains (a smuggled token cannot substitute another
        # identity), nothing is injected, and other domains pass untouched.
        smuggled = [("Authorization", "token smuggled"), ("Accept", "application/json")]
        # The hook also replaces User-Agent with the host value, so it appears
        # in every expectation below.
        agent_header = ("User-Agent", "kern-proxy/1")
        self.assertEqual(github_credential_headers("api.github.com", smuggled), [("Accept", "application/json"), agent_header])
        self.assertEqual(
            github_credential_headers("example.com", smuggled), [*smuggled, agent_header]
        )

        state.save_proxy_github_token("ghs_working")
        self.assertEqual(state.read_proxy_github_token(), "ghs_working")
        # The row itself holds secretbox ciphertext (encrypted at rest like
        # every other secret); only the read path yields the plaintext.
        with db.transaction() as cur:
            cur.execute("SELECT token FROM proxy_github_token")
            (stored,) = cur.fetchone()
        self.assertTrue(stored.startswith(secretbox.PREFIX))
        self.assertNotIn("ghs_working", stored)
        # A replaced row can never serve a cached stale token.
        state.save_proxy_github_token("ghs_replaced")
        self.assertEqual(state.read_proxy_github_token(), "ghs_replaced")
        state.save_proxy_github_token("ghs_working")
        self.assertEqual(state.read_proxy_github_token(), "ghs_working")
        # REST hosts take Bearer; git smart HTTP (and raw/codeload) take the
        # token as the Basic password with the x-access-token username.
        self.assertEqual(
            github_credential_headers("api.github.com", smuggled),
            [
                ("Accept", "application/json"),
                agent_header,
                ("Authorization", "Bearer ghs_working"),
            ],
        )
        import base64 as _b64

        basic = _b64.b64encode(b"x-access-token:ghs_working").decode()
        self.assertEqual(
            github_credential_headers("github.com", [("Authorization", "token smuggled")]),
            [agent_header, ("Authorization", f"Basic {basic}")],
        )
        # Signed-URL domains are strip-only: an Authorization header breaks
        # the presigned download, and the signed URL is the access control.
        self.assertEqual(
            github_credential_headers(
                "objects.githubusercontent.com", [("Authorization", "token x")]
            ),
            [agent_header],
        )
        self.assertEqual(
            github_credential_headers("github-cloud.githubusercontent.com", [("Authorization", "token x")]),
            [agent_header]
        )
        state.save_proxy_github_token(None)
        self.assertIsNone(state.read_proxy_github_token())
        with self.assertRaises(ValueError):
            state.save_proxy_github_token("")

    def test_bedrock_proxy_reads_the_one_validated_row(self) -> None:
        self.assertIsNone(state.read_bedrock_proxy_credential())
        with state.mutation() as cur:
            state.save_bedrock_credential("AKIASHAREDOPERATOR01", "shared-secret-material", "us-east-1", cur)
        self.assertEqual(
            state.read_bedrock_proxy_credential(),
            ("AKIASHAREDOPERATOR01", "shared-secret-material", "us-east-1"),
        )
        # The one row holds ciphertext at rest. Policy enablement is a soft
        # state the proxy guard checks before it uses this accessor.
        with db.transaction() as cur:
            cur.execute("SELECT secret_access_key_encrypted FROM bedrock_credentials WHERE singleton = TRUE")
            (stored,) = cur.fetchone()
        self.assertTrue(stored.startswith(secretbox.PREFIX))
        self.assertNotIn("shared-secret-material", stored)

        # Replacing the validated row invalidates the decryption cache.
        # Disabling does not mutate the durable credential; the proxy's parsed
        # policy rejects the request earlier.
        with state.mutation() as cur:
            state.save_bedrock_credential("AKIASHAREDOPERATOR01", "shared-new-secret", "us-west-2", cur)
        self.assertEqual(
            state.read_bedrock_proxy_credential(),
            ("AKIASHAREDOPERATOR01", "shared-new-secret", "us-west-2"),
        )
        state.save_network_policy({"network_integrations": {}}, "2026-06-08T00:00:01Z")
        self.assertIsNotNone(state.read_bedrock_proxy_credential())
        self.assertEqual(state.read_bedrock_access_key_id(), "AKIASHAREDOPERATOR01")
        self.assertEqual(state.read_bedrock_region(), "us-west-2")

    def test_github_repo_audits_upsert_and_prune(self) -> None:
        self.assertEqual(state.read_github_repo_audits(), {})
        state.save_github_repo_audit(
            "infiloop2",
            "kern",
            {"visibility": "public", "pages_public": False},
            None,
        )
        state.save_github_repo_audit("infiloop2", "infibot", {}, "audit fetch failed: 403")
        audits = state.read_github_repo_audits()
        self.assertEqual(
            audits[("infiloop2", "kern")]["facts"],
            {"visibility": "public", "pages_public": False},
        )
        self.assertNotIn("error", audits[("infiloop2", "kern")])
        self.assertEqual(audits[("infiloop2", "infibot")]["error"], "audit fetch failed: 403")
        # Re-auditing replaces the stored facts for that repo.
        state.save_github_repo_audit(
            "infiloop2",
            "kern",
            {"visibility": "private", "pages_public": None},
            None,
        )
        self.assertEqual(
            state.read_github_repo_audits()[("infiloop2", "kern")]["facts"],
            {"visibility": "private", "pages_public": None},
        )
        # An errored row is never fresh: the poller retries it on the next
        # pass instead of waiting out the TTL.
        from host.runtime.admin_api import github_repo_audit

        audits = state.read_github_repo_audits()
        self.assertTrue(github_repo_audit._stale(audits[("infiloop2", "infibot")]))
        self.assertFalse(github_repo_audit._stale(audits[("infiloop2", "kern")]))
        # Pruning drops repositories no longer in the policy.
        state.prune_github_repo_audits({("infiloop2", "kern")})
        self.assertEqual(list(state.read_github_repo_audits()), [("infiloop2", "kern")])

    def test_admin_provider_accounts_live_in_the_database(self) -> None:
        save_openai_account({"account_id": "acct_rich", "planType": "pro"})
        save_claude_account({"access_token_sha256": "e" * 64})

        self.assertEqual(read_openai_account(), {"account_id": "acct_rich", "planType": "pro"})
        self.assertEqual(read_claude_account(), {"access_token_sha256": "e" * 64})
        # Clearing leaves an empty record.
        save_openai_account(None)
        save_claude_account(None)
        self.assertEqual(read_openai_account(), {})
        self.assertEqual(read_claude_account(), {})

    def test_clearing_openai_account_leaves_an_empty_record(self) -> None:
        save_openai_account({"account_id": "acct", "planType": "pro"})
        save_openai_account(None)

        self.assertEqual(read_openai_account(), {})

    def test_config_replaces_wholesale(self) -> None:
        hash_1 = "1" * 64
        save_config({"agent_name": "one", "admin_password_sha256": hash_1})
        self.assertEqual(load_config()["agent_name"], "one")
        self.assertEqual(load_config()["admin_password_sha256"], hash_1)
        save_config({"agent_name": "two"})
        self.assertEqual(load_config(), {"agent_name": "two"})

    def test_host_runtime_has_no_third_party_imports(self) -> None:
        # The runtime is standard library only — the admin-state database is
        # spoken to by the in-repo protocol client, not a driver. This walks
        # every host/ module and rejects any import outside the stdlib, the
        # host package itself, and the in-repo tools package (which is
        # likewise pinned to the stdlib by test_tools), so a dependency
        # cannot sneak back in.
        import ast
        import sys

        repo_root = Path(__file__).resolve().parents[1]
        allowed_roots = set(sys.stdlib_module_names) | {"host", "tools"}
        offenders: list[str] = []
        for path in sorted((repo_root / "host").rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    roots = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    roots = [node.module.split(".")[0]]
                else:
                    continue
                for root in roots:
                    if root not in allowed_roots:
                        offenders.append(f"{path.relative_to(repo_root)}: {root}")
        self.assertEqual(offenders, [])


class BedrockUsageCounterTests(unittest.TestCase):
    """The proxy-written live usage counters: one row per (model, UTC day),
    incremented per allowed invocation, aggregated per month for
    the admin API's cost estimate."""

    def setUp(self) -> None:
        pg_harness.reset_database()

    USAGE = {
        "input_tokens": 100,
        "output_tokens": 40,
        "cache_read_tokens": 8,
        "cache_write_tokens": 2,
    }
    # The proxy prices each response before recording; record_bedrock_usage
    # only stores and sums the cost it is handed, so the test pins an exact,
    # NUMERIC(_,6)-representable value rather than re-deriving a rate.
    COST = 0.0625

    def test_usage_increments_one_row_per_model_and_day(self) -> None:
        state.record_bedrock_usage("deepseek.v3.2", dict(self.USAGE), self.COST)
        state.record_bedrock_usage("deepseek.v3.2", dict(self.USAGE), self.COST)
        state.record_bedrock_usage("moonshotai.kimi-k2.5", dict(self.USAGE), self.COST)
        rows = state.read_bedrock_usage("1970-01-01")
        self.assertEqual(
            [(row["model_id"], row["requests"]) for row in rows],
            [
                ("deepseek.v3.2", 2),
                ("moonshotai.kimi-k2.5", 1),
            ],
        )
        doubled = rows[0]
        self.assertEqual(doubled["metered_requests"], 2)
        self.assertEqual(doubled["input_tokens"], 200)
        self.assertEqual(doubled["output_tokens"], 80)
        self.assertEqual(doubled["cache_read_tokens"], 16)
        self.assertEqual(doubled["cache_write_tokens"], 4)
        self.assertAlmostEqual(doubled["cost_usd"], 2 * self.COST)

    def test_unmetered_request_counts_without_tokens_or_cost(self) -> None:
        # A cost is passed, but an unmetered response records none of it.
        state.record_bedrock_usage("deepseek.v3.2", None, self.COST)
        (row,) = state.read_bedrock_usage("1970-01-01")
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["metered_requests"], 0)
        self.assertEqual(row["input_tokens"], 0)
        self.assertEqual(row["cost_usd"], 0.0)

    def test_read_since_day_excludes_prior_months(self) -> None:
        state.record_bedrock_usage("deepseek.v3.2", dict(self.USAGE), self.COST)
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO bedrock_usage (model_id, day, requests, metered_requests,"
                " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)"
                " VALUES ('deepseek.v3.2', '2001-01-31', 7, 7, 999, 999, 0, 0)",
            )
        (row,) = state.read_bedrock_usage("2001-02-01")
        self.assertEqual(row["requests"], 1)
        self.assertEqual(row["input_tokens"], 100)
        self.assertEqual(len(state.read_bedrock_usage("1970-01-01")), 1)
        self.assertEqual(state.read_bedrock_usage("1970-01-01")[0]["requests"], 8)


class HostErrorStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()

    @staticmethod
    def event(*, service: str = "kern-admin-api") -> dict[str, object]:
        return {
            "service": service,
            "component": "admin_api.request",
            "severity": "error",
            "kind": "unexpected_exception",
            "exception_type": "RuntimeError",
            "summary": "request handler escaped",
            "traceback": 'File "host/runtime/admin_api/service.py", line 1, in handle',
            "context": {"method": "GET", "route": "/v1/status"},
            "fingerprint": "a" * 64,
            "host_version": "1.3.3",
            "boot_id": "boot-1",
            "pid": 4321,
        }

    def test_ingest_coalesces_brief_repeats_and_rotates_ordering(self) -> None:
        first_usec = 1_800_000_000_000_000
        first = state.ingest_host_diagnostic(first_usec, self.event())
        other = state.ingest_host_diagnostic(
            first_usec + 5_000_000,
            self.event(service="kern-tools"),
        )
        repeated = state.ingest_host_diagnostic(
            first_usec + 10_000_000, self.event()
        )

        self.assertEqual(repeated, first)
        self.assertGreater(other, first)
        detail = state.host_diagnostic(first)
        assert detail is not None
        self.assertEqual(detail["occurrence_count"], 2)
        self.assertEqual(detail["context"]["route"], "/v1/status")
        self.assertEqual(detail["fingerprint"], "a" * 64)
        page = state.page_host_diagnostics_before(None)
        self.assertEqual([row["id"] for row in page], [first, other])
        self.assertGreater(page[0]["seq"], page[1]["seq"])

        later = state.ingest_host_diagnostic(
            first_usec + 80_000_000, self.event()
        )
        self.assertNotEqual(later, first)
        page = state.page_host_diagnostics_before(None)
        self.assertEqual([row["id"] for row in page], [later, first, other])
        self.assertNotIn("traceback", page[0])
        self.assertNotIn("context", page[0])

    def test_service_filter(self) -> None:
        seen_usec = 1_800_000_000_000_000
        state.ingest_host_diagnostic(seen_usec, self.event())
        state.ingest_host_diagnostic(
            seen_usec + 1_000_000, self.event(service="kern-tools")
        )
        filtered = state.page_host_diagnostics_before(None, service="kern-tools")
        self.assertEqual([row["service"] for row in filtered], ["kern-tools"])
        self.assertEqual(len(state.page_host_diagnostics_before(None)), 2)

    def test_severity_filter(self) -> None:
        seen_usec = 1_800_000_000_000_000
        state.ingest_host_diagnostic(seen_usec, self.event())
        warning = dict(
            self.event(service="kern-tools"),
            severity="warning",
            kind="provider_failure",
            fingerprint="b" * 64,
        )
        state.ingest_host_diagnostic(seen_usec + 1_000_000, warning)

        filtered = state.page_host_diagnostics_before(None, severity="warning")
        self.assertEqual([row["service"] for row in filtered], ["kern-tools"])

    def test_coalesced_sequence_rotation_does_not_consume_retention_slots(self) -> None:
        seen_usec = 1_800_000_000_000_000
        with (
            patch.object(state, "HOST_DIAGNOSTIC_LIMIT", 2),
            patch.object(state, "HOST_DIAGNOSTIC_PRUNE_EVERY", 1),
        ):
            state.ingest_host_diagnostic(seen_usec, self.event())
            state.ingest_host_diagnostic(
                seen_usec + 1_000_000,
                self.event(service="kern-tools"),
            )
            for repeat in range(2, 7):
                state.ingest_host_diagnostic(
                    seen_usec + repeat * 1_000_000,
                    self.event(),
                )

            rows = state.page_host_diagnostics_before(None)
            self.assertEqual({row["service"] for row in rows}, {"kern-admin-api", "kern-tools"})
            repeated = next(row for row in rows if row["service"] == "kern-admin-api")
            self.assertEqual(repeated["occurrence_count"], 6)

            state.ingest_host_diagnostic(
                seen_usec + 7_000_000,
                self.event(service="kern-agent-network"),
            )
            self.assertEqual(
                [row["service"] for row in state.page_host_diagnostics_before(None)],
                ["kern-agent-network", "kern-admin-api"],
            )


if __name__ == "__main__":
    unittest.main()
