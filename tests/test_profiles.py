import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import agent_deck


class DeckProfileTests(unittest.TestCase):
    def test_m18_profile_and_input_mapping(self) -> None:
        profile = next(
            item for item in agent_deck.DEVICE_PROFILES if item.name == "VSD M18"
        )
        self.assertEqual(
            (profile.key_count, profile.columns, profile.rows, profile.key_size),
            (15, 5, 3, 64),
        )
        self.assertEqual(
            [profile.screen_key(key) for key in (1, 5, 6, 10, 11, 15)],
            [1, 5, 6, 10, 11, 15],
        )

    def test_xl_profile_and_input_mapping(self) -> None:
        profile = next(
            item for item in agent_deck.DEVICE_PROFILES if item.name == "VSD XL"
        )
        self.assertEqual(
            (profile.key_count, profile.columns, profile.rows, profile.key_size),
            (32, 8, 4, 80),
        )
        self.assertEqual(
            [profile.screen_key(key) for key in (1, 8, 9, 32)],
            [25, 32, 17, 8],
        )
        self.assertEqual(
            profile.action_keys,
            {
                8: "new_chat",
                16: "microphone",
                24: "archive_chat",
                25: "launch_solidworks",
                26: "launch_altium",
                27: "launch_slack",
                28: "launch_chatgpt",
                29: "launch_cursor",
                30: "launch_email",
            },
        )
        self.assertEqual(profile.usage_key, 32)
        self.assertEqual(profile.calendar_key, 31)
        self.assertEqual(profile.agent_key_count, 21)
        self.assertNotIn(8, profile.task_keys)
        self.assertNotIn(16, profile.task_keys)
        self.assertNotIn(24, profile.task_keys)
        self.assertNotIn(31, profile.task_keys)
        self.assertNotIn(32, profile.task_keys)

    def test_rendering_uses_native_profile_size(self) -> None:
        thread = {"title": "Newest agent activity"}
        for size in (64, 80):
            with self.subTest(size=size):
                key_path = agent_deck.render_key(
                    1,
                    thread,
                    "active",
                    0.5,
                    3,
                    True,
                    False,
                    size,
                )
                usage_path = agent_deck.render_usage_key(
                    32,
                    {"remainingPercent": 75, "resetsAt": 0},
                    size,
                )
                cursor_path = agent_deck.render_key(
                    2,
                    thread,
                    "active",
                    0.5,
                    0,
                    False,
                    False,
                    size,
                    "cursor",
                )
                with Image.open(key_path) as image:
                    self.assertEqual(image.size, (size, size))
                with Image.open(cursor_path) as image:
                    self.assertEqual(image.size, (size, size))
                with Image.open(usage_path) as image:
                    self.assertEqual(image.size, (size, size))
                calendar_path = agent_deck.render_calendar_key(
                    31,
                    {
                        "title": "Design review",
                        "start": "2026-07-24T16:22:00-05:00",
                        "url": "https://calendar.google.com/",
                    },
                    False,
                    size,
                )
                with Image.open(calendar_path) as image:
                    self.assertEqual(image.size, (size, size))

    def test_action_rendering_uses_native_profile_size(self) -> None:
        for action in (
            "new_chat",
            "microphone",
            "archive_chat",
            "launch_solidworks",
            "launch_altium",
            "launch_slack",
            "launch_chatgpt",
            "launch_cursor",
            "launch_email",
        ):
            with self.subTest(action=action):
                path = agent_deck.render_action_key(8, action, 80)
                with Image.open(path) as image:
                    self.assertEqual(image.size, (80, 80))

    def test_archive_falls_back_to_remembered_thread(self) -> None:
        thread_id = "00000000-0000-0000-0000-000000000001"
        agent_deck.current_thread_id = thread_id
        with (
            patch.object(agent_deck, "current_selected_ide", return_value="codex"),
            patch.object(agent_deck, "archive_viewed_chat_in_codex", return_value=None),
            patch.object(agent_deck, "detect_viewed_thread", return_value=(True, None)),
            patch.object(agent_deck, "codex_request") as request,
            patch.object(agent_deck, "remember_current_thread"),
            patch.object(agent_deck.os, "startfile"),
        ):
            agent_deck.archive_current_chat()
        request.assert_called_once_with("thread/archive", {"threadId": thread_id})

    def test_archive_routes_to_cursor_when_cursor_ide_selected(self) -> None:
        with (
            patch.object(agent_deck, "current_selected_ide", return_value="cursor"),
            patch.object(agent_deck, "archive_cursor_chat") as archive_cursor,
            patch.object(agent_deck, "archive_viewed_chat_in_codex") as archive_codex,
        ):
            agent_deck.archive_current_chat()
        archive_cursor.assert_called_once_with()
        archive_codex.assert_not_called()

    def test_microphone_routes_to_cursor_when_cursor_ide_selected(self) -> None:
        with (
            patch.object(agent_deck, "current_selected_ide", return_value="cursor"),
            patch.object(agent_deck, "toggle_cursor_voice_input") as toggle_cursor,
            patch.object(agent_deck, "toggle_codex_dictation") as toggle_codex,
        ):
            agent_deck.toggle_dictation()
        toggle_cursor.assert_called_once_with()
        toggle_codex.assert_not_called()

    def test_microphone_routes_to_codex_when_codex_ide_selected(self) -> None:
        with (
            patch.object(agent_deck, "current_selected_ide", return_value="codex"),
            patch.object(agent_deck, "toggle_cursor_voice_input") as toggle_cursor,
            patch.object(agent_deck, "toggle_codex_dictation") as toggle_codex,
        ):
            agent_deck.toggle_dictation()
        toggle_codex.assert_called_once_with()
        toggle_cursor.assert_not_called()

    def test_second_agent_press_routes_to_recent_chrome_tab(self) -> None:
        profile = next(
            item for item in agent_deck.DEVICE_PROFILES if item.name == "VSD M18"
        )
        thread_id = "00000000-0000-0000-0000-000000000001"
        device = SimpleNamespace(agent_deck_profile=profile)
        event = SimpleNamespace(
            event_type=agent_deck.EventType.BUTTON,
            state=1,
            key=SimpleNamespace(value=1),
        )
        agent_deck.key_assignments = {
            1: {"source": "codex", "id": thread_id, "title": "", "workspace": ""}
        }
        agent_deck.last_button_press.clear()
        agent_deck.last_agent_press.clear()
        started_targets = []

        class ImmediateThread:
            def __init__(self, target, args=(), daemon=None):
                self.target = target
                self.args = args

            def start(self):
                started_targets.append((self.target, self.args))

        with (
            patch.object(agent_deck.time, "monotonic", side_effect=(100.0, 101.0)),
            patch.object(agent_deck.threading, "Thread", ImmediateThread),
        ):
            agent_deck.key_callback(device, event)
            agent_deck.key_callback(device, event)

        self.assertEqual(
            started_targets,
            [
                (agent_deck.open_thread, (thread_id,)),
                (agent_deck.open_recent_chrome_tab, (thread_id,)),
            ],
        )

    def test_cursor_key_press_opens_cursor_chat(self) -> None:
        profile = next(
            item for item in agent_deck.DEVICE_PROFILES if item.name == "VSD M18"
        )
        composer_id = "6623e65d-ba6c-4b91-9cef-1c2bb09402e6"
        device = SimpleNamespace(agent_deck_profile=profile)
        event = SimpleNamespace(
            event_type=agent_deck.EventType.BUTTON,
            state=1,
            key=SimpleNamespace(value=2),
        )
        agent_deck.key_assignments = {
            2: {
                "source": "cursor",
                "id": composer_id,
                "title": "Device cursor support",
                "workspace": r"c:\Users\me\proj",
            }
        }
        agent_deck.last_button_press.clear()
        agent_deck.last_agent_press.clear()
        started_targets = []

        class ImmediateThread:
            def __init__(self, target, args=(), daemon=None):
                self.target = target
                self.args = args

            def start(self):
                started_targets.append((self.target, self.args))

        with patch.object(agent_deck.threading, "Thread", ImmediateThread):
            agent_deck.key_callback(device, event)

        self.assertEqual(
            started_targets,
            [
                (
                    agent_deck.open_cursor_thread,
                    (composer_id, "Device cursor support", r"c:\Users\me\proj"),
                )
            ],
        )

    def test_new_chat_key_spawns_agent_in_selected_ide(self) -> None:
        profile = next(
            item for item in agent_deck.DEVICE_PROFILES if item.name == "VSD M18"
        )
        device = SimpleNamespace(agent_deck_profile=profile)
        event = SimpleNamespace(
            event_type=agent_deck.EventType.BUTTON,
            state=1,
            key=SimpleNamespace(value=16),
        )
        agent_deck.last_button_press.clear()
        started_targets = []

        class ImmediateThread:
            def __init__(self, target, args=(), daemon=None):
                self.target = target
                self.args = args

            def start(self):
                started_targets.append(self.target)

        with patch.object(agent_deck.threading, "Thread", ImmediateThread):
            agent_deck.key_callback(device, event)
        self.assertEqual(started_targets, [agent_deck.open_new_agent])

        for ide, expected in (
            ("cursor", "open_new_cursor_agent"),
            ("codex", "open_new_chat"),
        ):
            with (
                patch.object(
                    agent_deck, "current_selected_ide", return_value=ide
                ),
                patch.object(agent_deck, "open_new_cursor_agent") as new_cursor,
                patch.object(agent_deck, "open_new_chat") as new_codex,
            ):
                agent_deck.open_new_agent()
                called = new_cursor if expected == "open_new_cursor_agent" else new_codex
                skipped = new_codex if expected == "open_new_cursor_agent" else new_cursor
                called.assert_called_once_with()
                skipped.assert_not_called()

    def test_chrome_site_hints(self) -> None:
        self.assertEqual(
            agent_deck.chrome_site_hints("https://mail.google.com/mail/u/0/"),
            ["Gmail", "google"],
        )
        with patch.dict(
            agent_deck.PRIVATE_SITE_HINTS,
            {"internal.example.com": "Example"},
            clear=True,
        ):
            self.assertEqual(
                agent_deck.chrome_site_hints("https://internal.example.com/item/1"),
                ["Example"],
            )


if __name__ == "__main__":
    unittest.main()
