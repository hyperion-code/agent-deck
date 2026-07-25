from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_deck


def record(
    payload_type: str,
    timestamp: float | None = None,
    **values: object,
) -> dict[str, object]:
    item: dict[str, object] = {
        "type": "event_msg",
        "payload": {"type": payload_type, **values},
    }
    if timestamp is not None:
        item["timestamp"] = timestamp
    return item


class ActivityTests(unittest.TestCase):
    def classify(
        self,
        records: list[dict[str, object]],
        unread: bool = False,
    ) -> tuple[str, int, bool, bool]:
        thread_id = "test-thread"
        return agent_deck.activity_from_records(
            thread_id=thread_id,
            reasoning_effort="medium",
            unread={thread_id} if unread else set(),
            service_tier="default",
            records=records,
            file_age=3_600,
        )

    def test_long_running_turn_stays_active_even_if_unread(self) -> None:
        records = [
            record("task_complete"),
            record("task_started"),
            record("user_message"),
            record("function_call", name="shell_command", call_id="call-1"),
        ]

        status, _bars, _fast, controlling = self.classify(records, unread=True)

        self.assertEqual("active", status)
        self.assertFalse(controlling)

    def test_nested_computer_use_call_is_control_state(self) -> None:
        records = [
            record("task_started"),
            record("user_message"),
            record(
                "custom_tool_call",
                name="exec",
                call_id="call-control",
                input=(
                    "await tools.mcp__node_repl__js("
                    "{code: 'await sky.click(20, 30)'})"
                ),
            ),
        ]

        status, _bars, _fast, controlling = self.classify(records)

        self.assertEqual("active", status)
        self.assertTrue(controlling)

    def test_control_ends_after_matching_output_and_completion(self) -> None:
        records = [
            record("task_started"),
            record(
                "custom_tool_call",
                name="exec",
                call_id="call-control",
                input=(
                    "await tools.mcp__node_repl__js("
                    "{code: 'await sky.press_key(\"ENTER\")'})"
                ),
            ),
            record("custom_tool_call_output", call_id="call-control"),
            record("task_complete"),
        ]

        status, _bars, _fast, controlling = self.classify(records)

        self.assertEqual("done", status)
        self.assertFalse(controlling)

    def test_recent_control_action_is_held_long_enough_to_display(self) -> None:
        records = [
            record("task_started", timestamp=990),
            record(
                "custom_tool_call",
                timestamp=998,
                name="exec",
                call_id="call-control",
                input=(
                    "await tools.mcp__node_repl__js("
                    "{code: 'await sky.click(20, 30)'})"
                ),
            ),
            record(
                "custom_tool_call_output",
                timestamp=999,
                call_id="call-control",
            ),
        ]

        status, _bars, _fast, controlling = agent_deck.activity_from_records(
            thread_id="test-thread",
            reasoning_effort="medium",
            unread=set(),
            service_tier="default",
            records=records,
            file_age=3_600,
            now=1_000,
        )

        self.assertEqual("active", status)
        self.assertTrue(controlling)

    def test_source_text_mentioning_sky_is_not_control(self) -> None:
        records = [
            record("task_started"),
            record(
                "custom_tool_call",
                name="exec",
                call_id="call-diagnostic",
                input=(
                    "await tools.shell_command({"
                    "command: \"rg 'sky.click' agent_deck.py\"})"
                ),
            ),
        ]

        status, _bars, _fast, controlling = self.classify(records)

        self.assertEqual("active", status)
        self.assertFalse(controlling)

    def test_read_only_window_observation_is_not_control(self) -> None:
        records = [
            record("task_started"),
            record(
                "custom_tool_call",
                name="js",
                namespace="node_repl",
                call_id="call-observe",
                input=(
                    "await new Promise(resolve => setTimeout(resolve, 20000)); "
                    "await sky.get_window_state({windowId: '123'})"
                ),
            ),
        ]

        status, _bars, _fast, controlling = self.classify(records)

        self.assertEqual("active", status)
        self.assertFalse(controlling)

    def test_read_only_nested_window_setup_is_not_control(self) -> None:
        records = [
            record("task_started"),
            record(
                "custom_tool_call",
                name="exec",
                call_id="call-observe",
                input=(
                    "await tools.mcp__node_repl__js({code: "
                    "'await setupComputerUseRuntime(); "
                    "await sky.list_windows(); await sky.get_window(123)'})"
                ),
            ),
        ]

        status, _bars, _fast, controlling = self.classify(records)

        self.assertEqual("active", status)
        self.assertFalse(controlling)


class CursorActivityTests(unittest.TestCase):
    NOW_MS = 1_784_914_000_000.0

    def cursor_thread(self, **overrides: object) -> dict[str, object]:
        thread: dict[str, object] = {
            "id": "composer-1",
            "title": "Cursor task",
            "source": "cursor",
            "blocking": False,
            "unread": False,
            "db_status": "completed",
            "checkpoint_ms": self.NOW_MS - 3_600_000,
        }
        thread.update(overrides)
        return thread

    def classify(self, **overrides: object) -> str:
        status, bars, fast, controlling = agent_deck.cursor_thread_activity(
            self.cursor_thread(**overrides), now_ms=self.NOW_MS
        )
        self.assertEqual((bars, fast, controlling), (0, False, False))
        return status

    def test_blocking_pending_action_needs_attention(self) -> None:
        self.assertEqual(
            "wait",
            self.classify(blocking=True, checkpoint_ms=self.NOW_MS - 1_000),
        )

    def test_fresh_checkpoint_without_completion_is_active(self) -> None:
        self.assertEqual(
            "active",
            self.classify(db_status="aborted", checkpoint_ms=self.NOW_MS - 30_000),
        )

    def test_completed_run_is_done_even_with_fresh_checkpoint(self) -> None:
        self.assertEqual(
            "done",
            self.classify(db_status="completed", checkpoint_ms=self.NOW_MS - 5_000),
        )

    def test_unread_completion_is_done(self) -> None:
        self.assertEqual("done", self.classify(db_status="aborted", unread=True))

    def test_stale_aborted_chat_is_idle(self) -> None:
        self.assertEqual("idle", self.classify(db_status="aborted"))


class CursorArchiveTests(unittest.TestCase):
    def test_archive_cursor_in_db_marks_header_and_payload(self) -> None:
        composer_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            db_path = Path(temp_dir) / "state.vscdb"
            with sqlite3.connect(str(db_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE composerHeaders (
                        composerId TEXT PRIMARY KEY,
                        isArchived INTEGER,
                        value TEXT
                    )
                    """
                )
                connection.execute(
                    "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)"
                )
                connection.execute(
                    "INSERT INTO composerHeaders VALUES (?, 0, ?)",
                    (
                        composer_id,
                        json.dumps(
                            {
                                "composerId": composer_id,
                                "name": "Temp chat",
                                "isArchived": False,
                            }
                        ),
                    ),
                )
                connection.execute(
                    "INSERT INTO cursorDiskKV VALUES (?, ?)",
                    (
                        f"composerData:{composer_id}",
                        json.dumps(
                            {
                                "composerId": composer_id,
                                "isArchived": False,
                            }
                        ),
                    ),
                )
                connection.commit()

            with patch.object(agent_deck, "CURSOR_GLOBAL_DB", db_path):
                self.assertTrue(agent_deck.archive_cursor_in_db(composer_id))

            connection = sqlite3.connect(str(db_path))
            try:
                archived, value = connection.execute(
                    "SELECT isArchived, value FROM composerHeaders WHERE composerId = ?",
                    (composer_id,),
                ).fetchone()
                payload = connection.execute(
                    "SELECT value FROM cursorDiskKV WHERE key = ?",
                    (f"composerData:{composer_id}",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(archived, 1)
            self.assertTrue(json.loads(value)["isArchived"])
            self.assertTrue(json.loads(payload)["isArchived"])


class AgentMergeTests(unittest.TestCase):
    def test_agents_from_both_ides_merge_by_recency(self) -> None:
        codex = [
            {"id": "codex-1", "source": "codex", "sort_ms": 300},
            {"id": "codex-2", "source": "codex", "sort_ms": 100},
        ]
        cursor = [{"id": "cursor-1", "source": "cursor", "sort_ms": 200}]
        with (
            patch.object(agent_deck, "load_threads", return_value=codex),
            patch.object(agent_deck, "load_cursor_threads", return_value=cursor),
        ):
            agents = agent_deck.load_agents(limit=2)
        self.assertEqual(
            [agent["id"] for agent in agents], ["codex-1", "cursor-1"]
        )


class CompletionHighlightTests(unittest.TestCase):
    def setUp(self) -> None:
        with agent_deck.highlight_lock:
            agent_deck.acknowledged_updates.clear()

    def test_acknowledgement_applies_only_to_current_completion(self) -> None:
        thread = {"id": "thread-1", "updated_ms": 100}

        self.assertTrue(
            agent_deck.completion_highlighted(thread, {"thread-1"})
        )
        with agent_deck.highlight_lock:
            agent_deck.acknowledged_updates["thread-1"] = 100
        self.assertFalse(
            agent_deck.completion_highlighted(thread, {"thread-1"})
        )

        thread["updated_ms"] = 101
        self.assertTrue(
            agent_deck.completion_highlighted(thread, {"thread-1"})
        )


if __name__ == "__main__":
    unittest.main()
