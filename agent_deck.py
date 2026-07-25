from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import date, datetime, time as datetime_time, timedelta, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

from icalendar import Calendar
from PIL import Image, ImageDraw, ImageFont
import recurring_ical_events


APP_DIR = Path(__file__).resolve().parent
CODEX_DIR = Path.home() / ".codex"
STATE_DB = CODEX_DIR / "state_5.sqlite"
LOGS_DB = CODEX_DIR / "logs_2.sqlite"
GLOBAL_STATE = CODEX_DIR / ".codex-global-state.json"
SESSION_INDEX = CODEX_DIR / "session_index.jsonl"
CACHE_DIR = APP_DIR / "cache"
LOG_PATH = APP_DIR / "agent_deck.log"
CURRENT_THREAD_PATH = APP_DIR / "current-thread.txt"
LAST_IDE_PATH = APP_DIR / "last-ide.txt"
MOBILE_CONFIG_PATH = APP_DIR / "agent-deck-mobile.json"
CALENDAR_FEED_PATH = APP_DIR / "calendar-feed-url.txt"
PRIVATE_CONFIG_FILENAME = "agent-deck-private.json"
PRIVATE_CONFIG_ENV = "AGENT_DECK_PRIVATE_CONFIG"
CURSOR_GLOBAL_DB = (
    Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
    / "Cursor"
    / "User"
    / "globalStorage"
    / "state.vscdb"
)
REFRESH_SECONDS = 0.1
STATE_REFRESH_SECONDS = 1.0
DEVICE_REFRESH_SECONDS = 2.0
ACTIVITY_REFRESH_SECONDS = 2.0
LED_REFRESH_SECONDS = 0.12
SERVICE_TIER_REFRESH_SECONDS = 5.0
USAGE_REFRESH_SECONDS = 15.0
CALENDAR_REFRESH_SECONDS = 60.0
IDE_FOCUS_REFRESH_SECONDS = 1.0
CURSOR_ACTIVE_WINDOW_SECONDS = 180.0
CONTROL_HOLD_SECONDS = 5.0
SECOND_AGENT_PRESS_SECONDS = 6.0
RECENT_CHROME_CONTROL_SECONDS = 30 * 60
HIGHLIGHT_PERIOD_SECONDS = 4.0
HIGHLIGHT_STEPS = 81
DEFAULT_AGENT_KEY_COUNT = 14


def private_config_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured_path = os.environ.get(PRIVATE_CONFIG_ENV, "").strip()
    if configured_path:
        candidates.append(Path(configured_path).expanduser())
    candidates.extend(
        (
            APP_DIR.parent / "AgentDeckPrivateConfig" / PRIVATE_CONFIG_FILENAME,
            APP_DIR.parent / "agent-deck-private-config" / PRIVATE_CONFIG_FILENAME,
            APP_DIR / PRIVATE_CONFIG_FILENAME,
        )
    )
    return candidates


def load_private_config() -> dict[str, Any]:
    for path in private_config_candidates():
        if not path.is_file():
            continue
        try:
            config = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(config, dict):
            return config
    return {}


PRIVATE_CONFIG = load_private_config()
raw_name_overrides = PRIVATE_CONFIG.get("name_overrides", {})
NAME_OVERRIDES = (
    {
        str(thread_id): str(title)
        for thread_id, title in raw_name_overrides.items()
        if str(thread_id).strip() and str(title).strip()
    }
    if isinstance(raw_name_overrides, dict)
    else {}
)
raw_site_hints = PRIVATE_CONFIG.get("site_hints", {})
PRIVATE_SITE_HINTS = (
    {
        str(domain).strip().casefold(): str(label).strip()
        for domain, label in raw_site_hints.items()
        if str(domain).strip() and str(label).strip()
    }
    if isinstance(raw_site_hints, dict)
    else {}
)

sys.path.insert(0, str(APP_DIR / "vendor"))
from StreamDock.Devices.StreamDockM18 import StreamDockM18  # noqa: E402
from StreamDock.Devices.StreamDockXL import StreamDockXL  # noqa: E402
from StreamDock.ImageHelpers.PILHelper import to_native_key_format  # noqa: E402
from StreamDock.InputTypes import ButtonKey, EventType  # noqa: E402
from StreamDock.Transport.LibUSBHIDAPI import LibUSBHIDAPI  # noqa: E402


class VSDM18(StreamDockM18):
    """M18-compatible 5548:1000 firmware."""


class VSDXL(StreamDockXL):
    """XL-compatible 5548:1034 firmware using JPEG key transfers."""

    def key_image_format(self) -> dict[str, Any]:
        image_format = super().key_image_format()
        image_format["format"] = "JPEG"
        return image_format

    def set_key_image(self, key: int | ButtonKey, path: str) -> int | None:
        logical_key = ButtonKey(key) if isinstance(key, int) else key
        hardware_key = self.get_image_key(logical_key)
        with Image.open(path) as source:
            image = to_native_key_format(self, source.copy())
        from io import BytesIO

        encoded = BytesIO()
        image.save(encoded, "JPEG", quality=94, subsampling=0)
        return self.transport.set_key_image_stream(encoded.getvalue(), hardware_key)


@dataclass(frozen=True)
class DeckProfile:
    name: str
    vendor_id: int
    product_id: int
    device_class: type[Any]
    key_count: int
    columns: int
    rows: int
    key_size: int
    display_brightness: int = 100
    mirror_input_rows: bool = False
    action_keys: dict[int, str] = field(default_factory=dict)
    calendar_key: int | None = None

    @property
    def usage_key(self) -> int:
        return self.key_count

    @property
    def agent_key_count(self) -> int:
        return len(self.task_keys)

    @property
    def task_keys(self) -> tuple[int, ...]:
        return tuple(
            key
            for key in range(1, self.key_count + 1)
            if key != self.usage_key
            and key not in self.action_keys
            and key != self.calendar_key
        )

    def screen_key(self, decoded_key: int) -> int:
        if not self.mirror_input_rows or not 1 <= decoded_key <= self.key_count:
            return decoded_key
        row, column = divmod(decoded_key - 1, self.columns)
        return (self.rows - 1 - row) * self.columns + column + 1


DEVICE_PROFILES = (
    DeckProfile(
        name="VSD XL",
        vendor_id=0x5548,
        product_id=0x1034,
        device_class=VSDXL,
        key_count=32,
        columns=8,
        rows=4,
        key_size=80,
        mirror_input_rows=True,
        calendar_key=31,
        action_keys={
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
    ),
    DeckProfile(
        name="VSD M18",
        vendor_id=0x5548,
        product_id=0x1000,
        device_class=VSDM18,
        key_count=15,
        columns=5,
        rows=3,
        key_size=64,
        action_keys={
            16: "new_chat",
            17: "microphone",
            18: "archive_chat",
        },
    ),
)


CACHE_DIR.mkdir(exist_ok=True)
os.chdir(APP_DIR)

logger = logging.getLogger("agent-deck")
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_PATH, maxBytes=512_000, backupCount=2, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logger.addHandler(handler)

assignment_lock = threading.Lock()
key_assignments: dict[int, dict[str, str]] = {}
current_thread_lock = threading.Lock()
current_thread_id: str | None = None
current_cursor_lock = threading.Lock()
current_cursor_agent: dict[str, str] | None = None
selected_ide_lock = threading.Lock()
selected_ide = "codex"
button_lock = threading.Lock()
last_button_press: dict[int, float] = {}
last_agent_press: dict[int, tuple[str, float]] = {}
archive_lock = threading.Lock()
mobile_server: ThreadingHTTPServer | None = None
usage_cache_lock = threading.Lock()
usage_cache_checked_at = 0.0
usage_cache_snapshot: dict[str, Any] = {
    "remainingPercent": None,
    "usedPercent": None,
    "windowMinutes": 0,
    "resetsAt": 0,
    "planType": "",
}
highlight_lock = threading.Lock()
acknowledged_updates: dict[str, int] = {}
calendar_cache_lock = threading.Lock()
calendar_cache_checked_at = 0.0
calendar_cache_snapshot: dict[str, Any] | None = None


def acquire_single_instance() -> Any:
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, "Local\\OpenAI-Codex-AgentDeck")
    if not handle:
        raise OSError("Could not create AgentDeck mutex")
    if kernel32.GetLastError() == 183:
        raise SystemExit(0)
    return handle


def load_fonts(key_size: int) -> tuple[
    ImageFont.FreeTypeFont,
    ImageFont.FreeTypeFont,
    ImageFont.FreeTypeFont,
    ImageFont.FreeTypeFont,
]:
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    regular = fonts / "segoeui.ttf"
    semibold = fonts / "seguisb.ttf"
    scale = key_size / 64
    return (
        ImageFont.truetype(str(semibold), round(10 * scale)),
        ImageFont.truetype(str(semibold), round(9 * scale)),
        ImageFont.truetype(str(regular), round(8 * scale)),
        ImageFont.truetype(str(semibold), round(19 * scale)),
    )


def unread_thread_ids() -> set[str]:
    try:
        data = json.loads(GLOBAL_STATE.read_text(encoding="utf-8"))
        atom = data.get("electron-persisted-atom-state", {})
        unread = atom.get("unread-thread-ids-by-host-v1", {})
        result: set[str] = set()
        if isinstance(unread, dict):
            for ids in unread.values():
                if isinstance(ids, list):
                    result.update(str(value) for value in ids)
        return result
    except Exception:
        return set()


def load_threads(limit: int = DEFAULT_AGENT_KEY_COUNT) -> list[dict[str, Any]]:
    uri = f"{STATE_DB.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, title, preview, rollout_path, model, reasoning_effort,
                   COALESCE(NULLIF(created_at_ms, 0), created_at * 1000) AS created_ms,
                   COALESCE(NULLIF(updated_at_ms, 0), updated_at * 1000) AS updated_ms,
                   COALESCE(NULLIF(recency_at_ms, 0), NULLIF(updated_at_ms, 0),
                            updated_at * 1000) AS sort_ms
            FROM threads
            WHERE archived = 0
              AND (thread_source IS NULL OR thread_source NOT LIKE '%subagent%')
              AND COALESCE(NULLIF(updated_at_ms, 0), updated_at * 1000)
                  >= (strftime('%s', 'now', '-1 day') * 1000)
            ORDER BY updated_ms DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    threads = [dict(row) for row in rows]
    for thread in threads:
        thread["source"] = "codex"
    try:
        indexed_names: dict[str, str] = {}
        for line in SESSION_INDEX.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            thread_id = item.get("id")
            thread_name = item.get("thread_name")
            if thread_id and thread_name:
                indexed_names[str(thread_id)] = str(thread_name)
        for thread in threads:
            thread_id = str(thread["id"])
            short_name = NAME_OVERRIDES.get(thread_id) or indexed_names.get(thread_id)
            if short_name:
                thread["title"] = short_name
    except Exception:
        logger.exception("Could not read Codex session names")
    return threads


def completion_highlighted(
    thread: dict[str, Any], unread: set[str]
) -> bool:
    thread_id = str(thread["id"])
    if thread_id not in unread:
        return False
    updated_ms = int(thread.get("updated_ms") or 0)
    with highlight_lock:
        return updated_ms > acknowledged_updates.get(thread_id, -1)


def acknowledge_completion(thread_id: str) -> None:
    updated_ms = int(time.time() * 1000)
    try:
        uri = f"{STATE_DB.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            row = connection.execute(
                """
                SELECT COALESCE(NULLIF(updated_at_ms, 0), updated_at * 1000)
                FROM threads
                WHERE id = ?
                """,
                (thread_id,),
            ).fetchone()
        if row is not None and row[0] is not None:
            updated_ms = int(row[0])
    except Exception:
        logger.exception("Could not read completion timestamp for %s", thread_id)
    with highlight_lock:
        acknowledged_updates[thread_id] = max(
            updated_ms, acknowledged_updates.get(thread_id, -1)
        )


def acknowledge_cursor_completion(thread_id: str) -> None:
    with highlight_lock:
        acknowledged_updates[thread_id] = max(
            int(time.time() * 1000), acknowledged_updates.get(thread_id, -1)
        )


def tail_payloads(path: str, max_bytes: int = 1_000_000) -> list[dict[str, Any]]:
    rollout = Path(path)
    if not rollout.exists():
        return []
    with rollout.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - max_bytes))
        if size > max_bytes:
            stream.readline()
        raw_lines = stream.readlines()
    payloads: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        try:
            item = json.loads(raw_line)
            payload = item.get("payload")
            if isinstance(payload, dict):
                payloads.append(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return payloads


def tail_records(path: str, max_bytes: int = 1_000_000) -> list[dict[str, Any]]:
    rollout = Path(path)
    if not rollout.exists():
        return []
    with rollout.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - max_bytes))
        if size > max_bytes:
            stream.readline()
        raw_lines = stream.readlines()
    records: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        try:
            item = json.loads(raw_line)
            if isinstance(item, dict):
                records.append(item)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return records


SPEED_BARS = {
    "none": 4,
    "minimal": 4,
    "low": 3,
    "medium": 2,
    "high": 1,
    "xhigh": 1,
    "max": 1,
    "ultra": 1,
}
CONTROL_TOOL_MARKERS = (
    "computer-use",
    "computer_use",
    "control-chrome",
    "control_chrome",
    "control-in-app-browser",
    "control_in_app_browser",
)
COMPUTER_INPUT_ACTION = re.compile(
    r"\bsky\s*\.\s*(?:"
    r"activate_window|click|double_click|drag|hover|launch_app|"
    r"move_mouse|press_key|scroll|triple_click|type_text"
    r")\s*\(",
    re.IGNORECASE,
)


def is_computer_control_call(payload: dict[str, Any]) -> bool:
    name = str(payload.get("name") or "").lower()
    namespace = str(payload.get("namespace") or "").lower()
    descriptor = " ".join(
        (name, namespace, str(payload.get("tool_name") or "").lower())
    )
    if any(marker in descriptor for marker in CONTROL_TOOL_MARKERS):
        return True

    raw_input = payload.get("input")
    if raw_input is None:
        raw_input = payload.get("arguments")
    if isinstance(raw_input, (dict, list)):
        input_text = json.dumps(raw_input, separators=(",", ":")).lower()
    else:
        input_text = str(raw_input or "").lower()
    has_control_action = bool(COMPUTER_INPUT_ACTION.search(input_text))
    if name == "js" and "node_repl" in namespace:
        return has_control_action
    if name == "exec":
        calls_node_repl = bool(
            re.search(
                r"tools\.(?:mcp__)?node_repl__js\s*\(",
                input_text,
            )
        )
        return calls_node_repl and has_control_action
    return False


def record_timestamp(record: dict[str, Any]) -> float:
    raw = record.get("timestamp")
    if isinstance(raw, (int, float)):
        return float(raw)
    if not isinstance(raw, str) or not raw:
        return 0.0
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def activity_from_records(
    thread_id: str,
    reasoning_effort: str,
    unread: set[str],
    service_tier: str,
    records: list[dict[str, Any]],
    file_age: float,
    now: float | None = None,
) -> tuple[str, int, bool, bool]:
    last_turn_start = -1
    last_terminal = -1
    last_attention = -1
    last_activity = -1
    last_user_message = -1
    last_attention_resolution = -1
    outstanding_calls: set[str] = set()
    outstanding_attention_calls: set[str] = set()
    outstanding_control_calls: set[str] = set()
    last_control_at = 0.0
    for index, record in enumerate(records):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "turn_context":
            collaboration = payload.get("collaboration_mode")
            settings = collaboration.get("settings", {}) if isinstance(collaboration, dict) else {}
            effort = (
                payload.get("reasoning_effort")
                or payload.get("effort")
                or settings.get("reasoning_effort")
            )
            if effort and not reasoning_effort:
                reasoning_effort = str(effort).lower()
        payload_type = str(payload.get("type") or "")
        if payload_type == "task_started":
            last_turn_start = index
        elif payload_type in {"task_complete", "turn_aborted"}:
            last_terminal = index
        elif payload_type == "user_message":
            last_user_message = index

        if payload_type in {"function_call", "custom_tool_call"}:
            name = str(payload.get("name") or "").lower()
            call_id = str(payload.get("call_id") or payload.get("id") or "")
            if call_id:
                outstanding_calls.add(call_id)
                if name in {"request_user_input", "request_permissions"}:
                    outstanding_attention_calls.add(call_id)
                if is_computer_control_call(payload):
                    outstanding_control_calls.add(call_id)
                    last_control_at = max(last_control_at, record_timestamp(record))
            if name in {"request_user_input", "request_permissions"}:
                last_attention = index
            last_activity = index
        elif payload_type in {"function_call_output", "custom_tool_call_output"}:
            call_id = str(payload.get("call_id") or "")
            outstanding_calls.discard(call_id)
            if call_id in outstanding_attention_calls:
                last_attention_resolution = index
            outstanding_attention_calls.discard(call_id)
            if call_id in outstanding_control_calls:
                last_control_at = max(last_control_at, record_timestamp(record))
            outstanding_control_calls.discard(call_id)
            last_activity = index
        elif payload_type in {
            "request_user_input",
            "request_permissions",
            "mcp_elicitation",
            "elicitation_request",
        }:
            last_attention = index
        elif payload_type in {
            "agent_reasoning",
            "agent_message",
            "reasoning",
            "mcp_tool_call_begin",
            "mcp_tool_call_end",
            "user_message",
        }:
            last_activity = index

    waiting = bool(outstanding_attention_calls) or (
        last_attention
        > max(last_terminal, last_user_message, last_attention_resolution)
    )
    turn_running = last_turn_start > last_terminal
    legacy_activity = (
        last_turn_start < 0
        and last_activity > last_terminal
        and file_age < 90
    )

    if waiting:
        status = "wait"
    elif turn_running or outstanding_calls or legacy_activity:
        status = "active"
    elif thread_id in unread or last_terminal >= 0:
        status = "done"
    else:
        status = "idle"

    speed_bars = SPEED_BARS.get(reasoning_effort or "medium", 2)
    fast = service_tier == "priority"
    current_time = time.time() if now is None else now
    recently_controlling = (
        last_control_at > 0
        and current_time - last_control_at <= CONTROL_HOLD_SECONDS
    )
    controlling = status == "active" and (
        bool(outstanding_control_calls) or recently_controlling
    )
    return status, speed_bars, fast, controlling


def thread_activity(
    thread: dict[str, Any], unread: set[str], service_tier: str
) -> tuple[str, int, bool, bool]:
    rollout_path = str(thread.get("rollout_path") or "")
    try:
        file_age = time.time() - Path(rollout_path).stat().st_mtime
    except OSError:
        file_age = 10_000
    return activity_from_records(
        thread_id=str(thread["id"]),
        reasoning_effort=str(thread.get("reasoning_effort") or "").lower(),
        unread=unread,
        service_tier=service_tier,
        records=tail_records(rollout_path),
        file_age=file_age,
    )


def load_cursor_threads(limit: int = DEFAULT_AGENT_KEY_COUNT) -> list[dict[str, Any]]:
    """Read named, non-archived Cursor agent chats from Cursor's global state."""

    if not CURSOR_GLOBAL_DB.is_file():
        return []
    try:
        uri = f"{CURSOR_GLOBAL_DB.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            rows = connection.execute(
                """
                SELECT h.composerId,
                       json_extract(h.value, '$.name') AS name,
                       COALESCE(
                           json_extract(h.value, '$.hasBlockingPendingActions'), 0
                       ) AS blocking,
                       COALESCE(
                           json_extract(h.value, '$.hasUnreadMessages'), 0
                       ) AS unread,
                       json_extract(
                           h.value, '$.workspaceIdentifier.uri.fsPath'
                       ) AS workspace_path,
                       COALESCE(h.recency, h.createdAt, 0) AS recency_ms,
                       COALESCE(h.checkpointAt, 0) AS checkpoint_ms,
                       json_extract(d.value, '$.status') AS db_status
                FROM composerHeaders h
                LEFT JOIN cursorDiskKV d ON d.key = 'composerData:' || h.composerId
                WHERE h.isSubagent = 0
                  AND h.isArchived = 0
                  AND json_extract(h.value, '$.name') IS NOT NULL
                  AND MAX(COALESCE(h.recency, h.createdAt, 0),
                          COALESCE(h.checkpointAt, 0))
                      >= (strftime('%s', 'now', '-1 day') * 1000)
                ORDER BY MAX(COALESCE(h.recency, h.createdAt, 0),
                             COALESCE(h.checkpointAt, 0)) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    except sqlite3.Error:
        logger.exception("Could not read Cursor agent chats")
        return []

    threads: list[dict[str, Any]] = []
    for row in rows:
        (
            composer_id,
            name,
            blocking,
            unread,
            workspace_path,
            recency_ms,
            checkpoint_ms,
            db_status,
        ) = row
        checkpoint = int(checkpoint_ms or 0)
        sort_ms = max(int(recency_ms or 0), checkpoint)
        threads.append(
            {
                "id": str(composer_id),
                "title": str(name),
                "source": "cursor",
                "workspace_path": str(workspace_path or ""),
                "blocking": bool(blocking),
                "unread": bool(unread),
                "db_status": str(db_status or ""),
                "checkpoint_ms": checkpoint,
                "updated_ms": checkpoint or sort_ms,
                "sort_ms": sort_ms,
            }
        )
    return threads


def cursor_thread_activity(
    thread: dict[str, Any], now_ms: float | None = None
) -> tuple[str, int, bool, bool]:
    """Classify a Cursor chat with the codex-style status vocabulary.

    Cursor's persisted status lags behind reality, but the header checkpoint
    keeps advancing while a run is in flight, so a fresh checkpoint that is
    not yet marked completed means the agent is still working.
    """

    current_ms = time.time() * 1000 if now_ms is None else now_ms
    checkpoint_age = (current_ms - float(thread.get("checkpoint_ms") or 0)) / 1000
    completed = str(thread.get("db_status") or "") == "completed"
    running = checkpoint_age < CURSOR_ACTIVE_WINDOW_SECONDS and not completed
    if thread.get("blocking"):
        status = "wait"
    elif running:
        status = "active"
    elif thread.get("unread") or completed:
        status = "done"
    else:
        status = "idle"
    return status, 0, False, False


def load_agents(limit: int = DEFAULT_AGENT_KEY_COUNT) -> list[dict[str, Any]]:
    """Merge Codex and Cursor agents, newest activity first."""

    agents = load_threads(limit) + load_cursor_threads(limit)
    agents.sort(key=lambda item: int(item.get("sort_ms") or 0), reverse=True)
    return agents[:limit]


def split_title(
    draw: ImageDraw.ImageDraw,
    title: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    raw_words = title.replace("_", " ").split()
    words: list[str] = []
    for raw_word in raw_words:
        word = raw_word
        while draw.textlength(word, font=font) > max_width and len(word) > 1:
            piece = ""
            while word and draw.textlength(piece + word[0], font=font) <= max_width:
                piece += word[0]
                word = word[1:]
            if not piece:
                piece, word = word[0], word[1:]
            words.append(piece)
        if word:
            words.append(word)
    if not words:
        return ["Untitled"]
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
        if len(lines) == 3:
            break
    if current and len(lines) < 3:
        lines.append(current)
    if len(lines) == 3 and len(words) > sum(len(line.split()) for line in lines):
        while draw.textlength(lines[-1] + "…", font=font) > max_width and lines[-1]:
            lines[-1] = lines[-1][:-1]
        lines[-1] += "…"
    return lines[:3]


def scaled(value: int, key_size: int) -> int:
    return round(value * key_size / 64)


def draw_speed_badge(
    draw: ImageDraw.ImageDraw,
    bars: int,
    fast: bool,
    key_size: int,
) -> None:
    color = (235, 244, 255)
    draw.ellipse(
        tuple(scaled(value, key_size) for value in (2, 2, 17, 17)),
        fill=(10, 20, 34),
        outline=(110, 190, 255),
        width=max(1, scaled(1, key_size)),
    )
    for index in range(4):
        height = scaled(3 + index * 2, key_size)
        fill = color if index < bars else (55, 72, 90)
        x = scaled(5 + index * 3, key_size)
        baseline = scaled(14, key_size)
        draw.rectangle(
            (x, baseline - height, x + max(1, scaled(1, key_size)), baseline),
            fill=fill,
        )
    if fast:
        bolt = [
            (scaled(x, key_size), scaled(y, key_size))
            for x, y in ((54, 2), (48, 10), (52, 10), (49, 18), (59, 8), (55, 8))
        ]
        draw.polygon(bolt, fill=(255, 225, 70))


def draw_cursor_badge(draw: ImageDraw.ImageDraw, key_size: int) -> None:
    """Mark a key as a Cursor agent with a small terminal-style badge."""

    draw.ellipse(
        tuple(scaled(value, key_size) for value in (2, 2, 17, 17)),
        fill=(10, 20, 34),
        outline=(210, 220, 235),
        width=max(1, scaled(1, key_size)),
    )
    icon_color = (235, 244, 255)
    width = max(1, scaled(2, key_size))
    chevron = [
        (scaled(6, key_size), scaled(6, key_size)),
        (scaled(10, key_size), scaled(10, key_size)),
        (scaled(6, key_size), scaled(14, key_size)),
    ]
    draw.line(chevron, fill=icon_color, width=width, joint="curve")
    draw.line(
        (
            scaled(11, key_size),
            scaled(14, key_size),
            scaled(15, key_size),
            scaled(14, key_size),
        ),
        fill=icon_color,
        width=width,
    )


def render_key(
    key: int,
    thread: dict[str, Any] | None,
    status: str,
    highlight_level: float,
    speed_bars: int = 2,
    fast: bool = False,
    highlighted: bool = False,
    key_size: int = 64,
    source: str = "codex",
) -> Path:
    palette = {
        "active": ((24, 53, 72), (95, 200, 255)),
        "wait": ((92, 10, 18), (255, 55, 70)),
        "done": ((8, 72, 35), (35, 225, 105)),
        "idle": ((24, 27, 33), (95, 103, 115)),
        "empty": ((5, 7, 10), (5, 7, 10)),
    }
    background, accent = palette[status]
    image = Image.new("RGB", (key_size, key_size), background)
    draw = ImageDraw.Draw(image)
    title_font, _status_font, _small_font, _usage_font = load_fonts(key_size)

    if thread is None:
        pass
    else:
        if source == "cursor":
            draw_cursor_badge(draw, key_size)
        else:
            draw_speed_badge(draw, speed_bars, fast, key_size)
        title = str(thread.get("title") or thread.get("preview") or "Untitled")
        lines = split_title(
            draw,
            title,
            title_font,
            key_size - scaled(8, key_size),
        )
        line_height = scaled(12, key_size)
        center = key_size // 2
        top = center - ((len(lines) - 1) * line_height // 2)
        text_color = (250, 252, 255)
        for offset, line in enumerate(lines):
            draw.text(
                (center, top + offset * line_height),
                line,
                font=title_font,
                fill=text_color,
                anchor="mm",
            )
        if highlighted:
            dim = (18, 62, 82)
            bright = (160, 235, 255)
            highlight_color = tuple(
                round(low + (high - low) * highlight_level)
                for low, high in zip(dim, bright)
            )
            margin = scaled(1, key_size)
            draw.rectangle(
                (margin, margin, key_size - margin - 1, key_size - margin - 1),
                outline=highlight_color,
                width=scaled(4, key_size),
            )

    path = CACHE_DIR / f"key-{key_size}-{key}.png"
    image.save(path, "PNG")
    return path


def render_usage_key(
    key: int,
    usage: dict[str, Any],
    key_size: int = 64,
) -> Path:
    remaining = usage.get("remainingPercent")
    if remaining is None:
        accent = (95, 103, 115)
        percent_text = "--%"
        label = "USAGE"
        arc_end = -90
    else:
        remaining = int(remaining)
        if remaining <= 20:
            accent = (255, 70, 78)
        elif remaining <= 40:
            accent = (255, 190, 55)
        else:
            accent = (80, 190, 255)
        percent_text = f"{remaining}%"
        label = usage_reset_label(int(usage.get("resetsAt") or 0))
        arc_end = -90 + round(360 * remaining / 100)

    image = Image.new("RGB", (key_size, key_size), (6, 13, 21))
    draw = ImageDraw.Draw(image)
    _title_font, _status_font, small_font, usage_font = load_fonts(key_size)
    ring = scaled(4, key_size)
    ring_end = key_size - ring - 1
    ring_width = scaled(4, key_size)
    draw.ellipse(
        (ring, ring, ring_end, ring_end),
        outline=(31, 49, 65),
        width=ring_width,
    )
    if remaining is not None and remaining > 0:
        draw.arc(
            (ring, ring, ring_end, ring_end),
            -90,
            arc_end,
            fill=accent,
            width=ring_width,
        )
    center = key_size // 2
    draw.text(
        (center, scaled(29, key_size)),
        percent_text,
        font=usage_font,
        fill=(245, 249, 255),
        anchor="mm",
    )
    draw.text(
        (center, scaled(45, key_size)),
        label,
        font=small_font,
        fill=accent,
        anchor="mm",
    )

    path = CACHE_DIR / f"key-{key_size}-{key}.png"
    image.save(path, "PNG")
    return path


def calendar_datetime(value: Any) -> datetime:
    local_zone = datetime.now().astimezone().tzinfo
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=local_zone)
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime_time.min, tzinfo=local_zone)
    raise TypeError(f"Unsupported calendar date value: {value!r}")


def calendar_event_url(component: Any, calendar_id: str = "") -> str:
    candidates: list[str] = []
    for field in ("url", "location", "description"):
        value = component.get(field)
        if value:
            candidates.extend(
                re.findall(r"https?://[^\s<>\"']+", str(value), flags=re.IGNORECASE)
            )
    cleaned = [url.rstrip(".,);]") for url in candidates]
    meeting_hosts = (
        "meet.google.com",
        "zoom.us",
        "teams.microsoft.com",
        "teams.live.com",
        "webex.com",
    )
    for url in cleaned:
        if any(host in urlparse(url).netloc.casefold() for host in meeting_hosts):
            return url
    uid = str(component.get("uid") or "").strip()
    if uid and calendar_id:
        event_id = base64.urlsafe_b64encode(
            f"{uid} {calendar_id}".encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"https://www.google.com/calendar/event?eid={event_id}"
    return cleaned[0] if cleaned else "https://calendar.google.com/"


def fetch_next_calendar_event(now: datetime | None = None) -> dict[str, Any] | None:
    if not CALENDAR_FEED_PATH.is_file():
        return None
    feed_url = CALENDAR_FEED_PATH.read_text(encoding="utf-8-sig").strip()
    if not feed_url.startswith("https://calendar.google.com/calendar/ical/"):
        logger.warning("Calendar feed configuration is invalid")
        return None

    request = Request(feed_url, headers={"User-Agent": "AgentDeck/1.0"})
    feed_parts = urlparse(feed_url).path.split("/")
    calendar_id = unquote(feed_parts[3]) if len(feed_parts) > 3 else ""
    with urlopen(request, timeout=12) as response:
        calendar = Calendar.from_ical(response.read())

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    occurrences = recurring_ical_events.of(calendar).between(
        current - timedelta(days=1),
        current + timedelta(days=370),
    )
    upcoming: list[tuple[datetime, dict[str, Any]]] = []
    for component in occurrences:
        if str(component.get("status") or "").upper() == "CANCELLED":
            continue
        try:
            decoded_start = component.decoded("dtstart")
            start = calendar_datetime(decoded_start)
        except (KeyError, TypeError, ValueError):
            continue
        try:
            end = calendar_datetime(component.decoded("dtend"))
        except (KeyError, TypeError, ValueError):
            end = start + timedelta(minutes=1)
        if end.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
            continue
        upcoming.append(
            (
                start.astimezone(timezone.utc),
                {
                    "title": str(
                        component.get("summary") or "Calendar event"
                    ).strip(),
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "all_day": isinstance(decoded_start, date)
                    and not isinstance(decoded_start, datetime),
                    "url": calendar_event_url(component, calendar_id),
                },
            )
        )
    if not upcoming:
        return None
    upcoming.sort(key=lambda item: item[0])
    return upcoming[0][1]


def cached_calendar_event() -> dict[str, Any] | None:
    global calendar_cache_checked_at, calendar_cache_snapshot
    with calendar_cache_lock:
        now = time.monotonic()
        if now - calendar_cache_checked_at < CALENDAR_REFRESH_SECONDS:
            return dict(calendar_cache_snapshot) if calendar_cache_snapshot else None
        try:
            calendar_cache_snapshot = fetch_next_calendar_event()
        except Exception:
            logger.exception("Could not refresh the next calendar event")
        calendar_cache_checked_at = now
        return dict(calendar_cache_snapshot) if calendar_cache_snapshot else None


def calendar_event_minutes(event: dict[str, Any] | None) -> float | None:
    if not event:
        return None
    try:
        start = datetime.fromisoformat(str(event["start"]))
    except (KeyError, TypeError, ValueError):
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return (start - datetime.now(start.tzinfo)).total_seconds() / 60


def render_calendar_key(
    key: int,
    event: dict[str, Any] | None,
    blink_on: bool = False,
    key_size: int = 64,
) -> Path:
    minutes = calendar_event_minutes(event)
    urgent = minutes is not None and 0 <= minutes <= 10
    background = (245, 248, 252) if urgent and blink_on else (7, 22, 31)
    accent = (14, 24, 31) if urgent and blink_on else (68, 210, 225)
    text_color = (14, 24, 31) if urgent and blink_on else (246, 251, 255)
    image = Image.new("RGB", (key_size, key_size), background)
    draw = ImageDraw.Draw(image)
    title_font, status_font, small_font, _usage_font = load_fonts(key_size)
    margin = scaled(2, key_size)
    draw.rounded_rectangle(
        (margin, margin, key_size - margin - 1, key_size - margin - 1),
        radius=scaled(7, key_size),
        outline=accent,
        width=scaled(3, key_size),
    )
    if event is None:
        draw.text(
            (key_size // 2, key_size // 2),
            "NO EVENTS",
            font=status_font,
            fill=(128, 145, 154),
            anchor="mm",
        )
    else:
        start = datetime.fromisoformat(str(event["start"])).astimezone()
        when = "ALL DAY" if event.get("all_day") else start.strftime("%I:%M %p").lstrip("0")
        draw.text(
            (key_size // 2, scaled(10, key_size)),
            when,
            font=small_font,
            fill=accent,
            anchor="mm",
        )
        lines = split_title(
            draw,
            str(event.get("title") or "Calendar event"),
            title_font,
            key_size - scaled(10, key_size),
        )[:3]
        line_height = scaled(12, key_size)
        first_y = scaled(28, key_size)
        for index, line in enumerate(lines):
            draw.text(
                (key_size // 2, first_y + index * line_height),
                line,
                font=title_font,
                fill=text_color,
                anchor="mm",
            )
    path = CACHE_DIR / f"key-{key_size}-{key}.png"
    image.save(path, "PNG")
    return path


def render_action_key(
    key: int,
    action: str,
    key_size: int,
) -> Path:
    labels = {
        "new_chat": ("NEW TASK", (75, 185, 255)),
        "microphone": ("MIC", (195, 125, 255)),
        "archive_chat": ("ARCHIVE", (255, 165, 70)),
        "launch_solidworks": ("SOLIDWORKS", (238, 76, 72)),
        "launch_altium": ("ALTIUM", (255, 185, 50)),
        "launch_slack": ("SLACK", (92, 207, 145)),
        "launch_chatgpt": ("CHATGPT", (80, 200, 160)),
        "launch_cursor": ("CURSOR", (210, 220, 235)),
        "launch_email": ("EMAIL", (90, 165, 255)),
    }
    label, accent = labels[action]
    image = Image.new("RGB", (key_size, key_size), (8, 14, 23))
    draw = ImageDraw.Draw(image)
    _title_font, status_font, _small_font, usage_font = load_fonts(key_size)
    center = key_size // 2
    border = scaled(3, key_size)
    draw.rounded_rectangle(
        (border, border, key_size - border - 1, key_size - border - 1),
        radius=scaled(8, key_size),
        outline=accent,
        width=scaled(3, key_size),
    )
    icon_color = (245, 249, 255)
    icon_width = max(2, scaled(2, key_size))
    if action == "new_chat":
        draw.text(
            (center, scaled(28, key_size)),
            "+",
            font=usage_font,
            fill=icon_color,
            anchor="mm",
        )
    elif action == "microphone":
        draw.rounded_rectangle(
            tuple(scaled(value, key_size) for value in (27, 10, 37, 32)),
            radius=scaled(5, key_size),
            outline=icon_color,
            width=icon_width,
        )
        draw.arc(
            tuple(scaled(value, key_size) for value in (23, 18, 41, 38)),
            0,
            180,
            fill=icon_color,
            width=icon_width,
        )
        draw.line(
            tuple(scaled(value, key_size) for value in (32, 38, 32, 43)),
            fill=icon_color,
            width=icon_width,
        )
        draw.line(
            tuple(scaled(value, key_size) for value in (27, 43, 37, 43)),
            fill=icon_color,
            width=icon_width,
        )
    elif action == "archive_chat":
        draw.rectangle(
            tuple(scaled(value, key_size) for value in (20, 21, 44, 40)),
            outline=icon_color,
            width=icon_width,
        )
        draw.rounded_rectangle(
            tuple(scaled(value, key_size) for value in (18, 14, 46, 23)),
            radius=scaled(2, key_size),
            fill=(8, 14, 23),
            outline=icon_color,
            width=icon_width,
        )
        draw.line(
            tuple(scaled(value, key_size) for value in (28, 28, 36, 28)),
            fill=icon_color,
            width=icon_width,
        )
    elif action == "launch_solidworks":
        draw.text(
            (center, scaled(27, key_size)),
            "SW",
            font=usage_font,
            fill=icon_color,
            anchor="mm",
        )
    elif action == "launch_altium":
        draw.polygon(
            [
                (scaled(32, key_size), scaled(10, key_size)),
                (scaled(20, key_size), scaled(39, key_size)),
                (scaled(27, key_size), scaled(39, key_size)),
                (scaled(32, key_size), scaled(26, key_size)),
                (scaled(37, key_size), scaled(39, key_size)),
                (scaled(44, key_size), scaled(39, key_size)),
            ],
            outline=icon_color,
        )
        draw.line(
            tuple(scaled(value, key_size) for value in (27, 31, 37, 31)),
            fill=icon_color,
            width=icon_width,
        )
    elif action == "launch_slack":
        slack_colors = (
            ((24, 27, 31, 38), (54, 197, 240)),
            ((34, 17, 45, 24), (46, 182, 125)),
            ((33, 26, 40, 37), (236, 178, 46)),
            ((19, 20, 26, 29), (224, 30, 90)),
        )
        for box, color in slack_colors:
            draw.rounded_rectangle(
                tuple(scaled(value, key_size) for value in box),
                radius=scaled(3, key_size),
                fill=color,
            )
    elif action == "launch_chatgpt":
        radius = scaled(12, key_size)
        inner = scaled(5, key_size)
        for index in range(6):
            angle = index * math.pi / 3
            x1 = center + round(inner * math.cos(angle))
            y1 = scaled(26, key_size) + round(inner * math.sin(angle))
            x2 = center + round(radius * math.cos(angle))
            y2 = scaled(26, key_size) + round(radius * math.sin(angle))
            draw.line((x1, y1, x2, y2), fill=icon_color, width=icon_width)
        draw.ellipse(
            (
                center - inner,
                scaled(26, key_size) - inner,
                center + inner,
                scaled(26, key_size) + inner,
            ),
            outline=icon_color,
            width=icon_width,
        )
    elif action == "launch_cursor":
        draw.text(
            (center, scaled(27, key_size)),
            ">_",
            font=usage_font,
            fill=icon_color,
            anchor="mm",
        )
    elif action == "launch_email":
        left, top, right, bottom = (
            scaled(value, key_size) for value in (19, 16, 45, 38)
        )
        draw.rectangle(
            (left, top, right, bottom),
            outline=icon_color,
            width=icon_width,
        )
        draw.line(
            (left, top, center, scaled(29, key_size), right, top),
            fill=icon_color,
            width=icon_width,
        )
    label_font = status_font
    if len(label) > 8:
        label_font = ImageFont.truetype(
            str(
                Path(os.environ.get("WINDIR", r"C:\Windows"))
                / "Fonts"
                / "seguisb.ttf"
            ),
            max(7, scaled(6, key_size)),
        )
    draw.text(
        (center, scaled(51, key_size)),
        label,
        font=label_font,
        fill=accent,
        anchor="mm",
    )
    path = CACHE_DIR / f"key-{key_size}-{key}.png"
    image.save(path, "PNG")
    return path


def remember_current_thread(thread_id: str | None) -> None:
    global current_thread_id
    with current_thread_lock:
        current_thread_id = thread_id
        try:
            CURRENT_THREAD_PATH.write_text(thread_id or "", encoding="utf-8")
        except OSError:
            logger.exception("Could not persist the current thread")


def restore_current_thread() -> None:
    try:
        saved = CURRENT_THREAD_PATH.read_text(encoding="utf-8").strip()
        if saved:
            remember_current_thread(saved)
            return
    except OSError:
        pass

    try:
        raw_log = LOG_PATH.read_text(encoding="utf-8", errors="replace")
        matches = re.findall(r"Opened thread ([0-9a-f-]{36})", raw_log)
        if matches:
            remember_current_thread(matches[-1])
    except OSError:
        pass


def remember_selected_ide(ide: str) -> None:
    global selected_ide
    with selected_ide_lock:
        if selected_ide == ide:
            return
        selected_ide = ide
        try:
            LAST_IDE_PATH.write_text(ide, encoding="utf-8")
        except OSError:
            logger.exception("Could not persist the selected IDE")
    logger.info("Most recently selected IDE: %s", ide)


def remember_current_cursor_agent(
    composer_id: str | None, title: str | None = None
) -> None:
    global current_cursor_agent
    with current_cursor_lock:
        if composer_id:
            current_cursor_agent = {
                "id": composer_id,
                "title": title or "",
            }
        else:
            current_cursor_agent = None


def current_cursor_agent_snapshot() -> dict[str, str] | None:
    with current_cursor_lock:
        return dict(current_cursor_agent) if current_cursor_agent else None


def restore_selected_ide() -> None:
    global selected_ide
    try:
        saved = LAST_IDE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if saved in {"codex", "cursor"}:
        with selected_ide_lock:
            selected_ide = saved


def current_selected_ide() -> str:
    with selected_ide_lock:
        return selected_ide


def detect_foreground_ide() -> str | None:
    """Report which agent IDE currently owns the foreground window, if any."""

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    if not process_id.value:
        return None
    handle = kernel32.OpenProcess(0x1000, False, process_id.value)
    if not handle:
        return None
    try:
        capacity = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(capacity.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(capacity)
        ):
            return None
        process_name = Path(buffer.value).name.casefold()
    finally:
        kernel32.CloseHandle(handle)
    if process_name == "cursor.exe":
        return "cursor"
    if process_name == "chatgpt.exe":
        return "codex"
    return None


def normalize_title(title: str) -> str:
    return " ".join(title.split()).casefold()


def active_thread_aliases() -> list[tuple[str, set[str]]]:
    uri = f"{STATE_DB.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
        rows = connection.execute(
            """
            SELECT id, title
            FROM threads
            WHERE archived = 0
              AND (thread_source IS NULL OR thread_source NOT LIKE '%subagent%')
            ORDER BY COALESCE(NULLIF(updated_at_ms, 0), updated_at * 1000) DESC
            """
        ).fetchall()

    aliases: dict[str, set[str]] = {
        str(thread_id): {normalize_title(str(title))}
        for thread_id, title in rows
        if title
    }
    ordered_ids = [str(thread_id) for thread_id, _title in rows]

    try:
        for line in SESSION_INDEX.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            thread_id = str(item.get("id") or "")
            thread_name = str(item.get("thread_name") or "")
            if thread_id in aliases and thread_name:
                aliases[thread_id].add(normalize_title(thread_name))
    except (OSError, json.JSONDecodeError):
        pass

    try:
        data = json.loads(GLOBAL_STATE.read_text(encoding="utf-8"))
        descriptions = data.get("electron-persisted-atom-state", {}).get(
            "thread-descriptions-v1", {}
        )
        if isinstance(descriptions, dict):
            for thread_id, description in descriptions.items():
                if str(thread_id) in aliases and description:
                    aliases[str(thread_id)].add(normalize_title(str(description)))
    except (OSError, json.JSONDecodeError):
        pass

    for thread_id, title in NAME_OVERRIDES.items():
        if thread_id in aliases:
            aliases[thread_id].add(normalize_title(title))
    return [(thread_id, aliases.get(thread_id, set())) for thread_id in ordered_ids]


def detect_viewed_thread() -> tuple[bool, str | None]:
    """Read the task heading exposed by the Codex window's accessibility tree."""

    script = r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$window = Get-Process -Name ChatGPT -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    Sort-Object StartTime |
    Select-Object -First 1
if ($null -eq $window) { exit 2 }
$root = [System.Windows.Automation.AutomationElement]::FromHandle($window.MainWindowHandle)
$top = $root.Current.BoundingRectangle.Top
$condition = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Text
)
$items = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
for ($index = 0; $index -lt $items.Count; $index++) {
    $item = $items.Item($index)
    $bounds = $item.Current.BoundingRectangle
    if (-not $item.Current.IsOffscreen -and
        $bounds.Height -gt 0 -and
        $bounds.Top -ge $top -and
        $bounds.Top -le ($top + 120) -and
        $item.Current.Name) {
        Write-Output $item.Current.Name
    }
}
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    if result.returncode != 0:
        return False, None

    visible_titles = {
        normalize_title(line)
        for line in result.stdout.splitlines()
        if line.strip()
    }
    for thread_id, aliases in active_thread_aliases():
        if visible_titles.intersection(aliases):
            logger.info("Detected viewed Codex thread %s", thread_id)
            return True, thread_id
    return True, None


def find_codex_executable() -> Path:
    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = list((local_app_data / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"))
    if candidates:
        return max(candidates, key=lambda path: path.stat().st_mtime)
    path = shutil.which("codex")
    if path:
        return Path(path)
    raise FileNotFoundError("Could not find the Codex app-server executable")


def codex_request(method: str, params: dict[str, Any], timeout: float = 12.0) -> Any:
    process = subprocess.Popen(
        [str(find_codex_executable()), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    responses: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def read_responses() -> None:
        assert process.stdout is not None
        for raw_line in process.stdout:
            try:
                message = json.loads(raw_line)
                if isinstance(message, dict):
                    responses.put(message)
            except json.JSONDecodeError:
                continue
        responses.put(None)

    threading.Thread(target=read_responses, daemon=True).start()

    def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def wait_for(request_id: int) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Codex request {request_id} timed out")
            try:
                message = responses.get(timeout=remaining)
            except queue.Empty as error:
                raise TimeoutError(f"Codex request {request_id} timed out") from error
            if message is None:
                raise RuntimeError("Codex app-server closed the connection")
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                if isinstance(error, dict):
                    detail = str(error.get("message") or error)
                else:
                    detail = str(error)
                raise RuntimeError(detail)
            return message.get("result")

    try:
        send(
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "agent-deck",
                        "title": "AgentDeck",
                        "version": "1.0.0",
                    },
                    "capabilities": {},
                },
            }
        )
        wait_for(1)
        send({"method": "initialized"})
        send({"id": 2, "method": method, "params": params})
        return wait_for(2)
    finally:
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()


def load_thread_service_tier(thread_id: str) -> str:
    """Read the latest Fast-mode override submitted by the Codex desktop app."""

    try:
        uri = f"{LOGS_DB.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            rows = connection.execute(
                """
                SELECT feedback_log_body
                FROM logs
                WHERE thread_id = ?
                  AND target = 'codex_core::session::handlers'
                  AND feedback_log_body LIKE '%Submission sub=Submission%'
                  AND feedback_log_body LIKE '%service_tier:%'
                ORDER BY id DESC
                LIMIT 20
                """,
                (thread_id,),
            ).fetchall()
        for (body,) in rows:
            match = re.search(
                r'service_tier:\s*Some\(Some\("([^"]+)"\)\)', str(body)
            )
            if match:
                return match.group(1).lower()
            if "service_tier: Some(None)" in str(body):
                return "default"
    except Exception:
        logger.exception("Could not read service tier for thread %s", thread_id)
    return "default"


def usage_reset_label(resets_at: int) -> str:
    if resets_at <= 0:
        return "RESET"
    try:
        reset = datetime.fromtimestamp(resets_at)
        return f"{reset.month}/{reset.day}"
    except (OSError, OverflowError, ValueError):
        return "RESET"


def load_usage_limit(threads: list[dict[str, Any]]) -> dict[str, Any]:
    candidates: list[Path] = []
    for thread in threads:
        raw_path = str(thread.get("rollout_path") or "")
        if not raw_path:
            continue
        path = Path(raw_path)
        if path.is_file() and path not in candidates:
            candidates.append(path)
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)

    newest: tuple[str, dict[str, Any]] | None = None
    for path in candidates:
        for record in reversed(tail_records(str(path), max_bytes=750_000)):
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            rate_limits = payload.get("rate_limits")
            if not isinstance(rate_limits, dict):
                continue
            limits = [
                value
                for name in ("primary", "secondary")
                if isinstance((value := rate_limits.get(name)), dict)
                and value.get("used_percent") is not None
            ]
            if not limits:
                continue
            selected = max(limits, key=lambda value: float(value["used_percent"]))
            timestamp = str(record.get("timestamp") or "")
            snapshot = {
                "remainingPercent": max(
                    0, min(100, round(100 - float(selected["used_percent"])))
                ),
                "usedPercent": max(
                    0, min(100, round(float(selected["used_percent"])))
                ),
                "windowMinutes": int(selected.get("window_minutes") or 0),
                "resetsAt": int(selected.get("resets_at") or 0),
                "planType": str(rate_limits.get("plan_type") or ""),
            }
            if newest is None or timestamp > newest[0]:
                newest = (timestamp, snapshot)
            break

    if newest is None:
        return {
            "remainingPercent": None,
            "usedPercent": None,
            "windowMinutes": 0,
            "resetsAt": 0,
            "planType": "",
        }
    return newest[1]


def cached_usage_limit(threads: list[dict[str, Any]]) -> dict[str, Any]:
    global usage_cache_checked_at, usage_cache_snapshot
    with usage_cache_lock:
        now = time.monotonic()
        if now - usage_cache_checked_at >= USAGE_REFRESH_SECONDS:
            usage_cache_snapshot = load_usage_limit(threads)
            usage_cache_checked_at = now
        return dict(usage_cache_snapshot)


def load_mobile_config() -> dict[str, Any]:
    try:
        config = json.loads(MOBILE_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(config, dict) and config.get("token"):
            return config
    except (OSError, json.JSONDecodeError):
        pass
    config = {
        "host": "127.0.0.1",
        "port": 8765,
        "token": secrets.token_urlsafe(32),
    }
    MOBILE_CONFIG_PATH.write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    return config


def mobile_state() -> dict[str, Any]:
    threads = load_threads()
    usage = cached_usage_limit(threads)
    unread = unread_thread_ids()
    items: list[dict[str, Any]] = []
    statuses: list[str] = []
    computer_control = False
    for thread in threads:
        thread_id = str(thread["id"])
        service_tier = load_thread_service_tier(thread_id)
        status, speed_bars, fast, controlling = thread_activity(
            thread, unread, service_tier
        )
        highlighted = status == "done" and completion_highlighted(thread, unread)
        statuses.append(status)
        computer_control = computer_control or controlling
        items.append(
            {
                "id": thread_id,
                "title": str(
                    thread.get("title") or thread.get("preview") or "Untitled"
                ),
                "status": status,
                "speedBars": speed_bars,
                "fast": fast,
                "computerControl": controlling,
                "highlighted": highlighted,
                "updatedAtMs": int(thread.get("updated_ms") or 0),
            }
        )
    with current_thread_lock:
        selected_id = current_thread_id
    return {
        "version": 1,
        "generatedAtMs": int(time.time() * 1000),
        "ledMode": aggregate_led_mode(statuses, computer_control),
        "selectedThreadId": selected_id,
        "threads": items,
        "usage": usage,
    }


def submit_thread_message(thread_id: str, text: str) -> None:
    text = text.strip()
    if not text:
        raise ValueError("Message is empty")
    uri = f"{STATE_DB.as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
        row = connection.execute(
            "SELECT cwd FROM threads WHERE id = ? AND archived = 0", (thread_id,)
        ).fetchone()
    if row is None:
        raise ValueError("Thread was not found")
    cwd = str(row[0] or APP_DIR)
    command = [
        str(find_codex_executable()),
        "exec",
        "resume",
        "--skip-git-repo-check",
        thread_id,
        text,
    ]
    subprocess.Popen(
        command,
        cwd=cwd if Path(cwd).exists() else APP_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    logger.info("Submitted a mobile message to thread %s", thread_id)


class MobileRequestHandler(BaseHTTPRequestHandler):
    server_version = "AgentDeckMobile/1"

    @property
    def token(self) -> str:
        return str(getattr(self.server, "agent_deck_token", ""))

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("Mobile API %s - %s", self.address_string(), format % args)

    def _authorized(self) -> bool:
        expected = f"Bearer {self.token}"
        supplied = self.headers.get("Authorization", "")
        return bool(self.token) and hmac.compare_digest(supplied, expected)

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 65_536:
            raise ValueError("Request is too large")
        if length == 0:
            return {}
        value = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON object required")
        return value

    def _require_auth(self) -> bool:
        if self._authorized():
            return True
        self._send_json(401, {"ok": False, "error": "Unauthorized"})
        return False

    def do_GET(self) -> None:
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/v1/state":
                self._send_json(200, mobile_state())
            elif path == "/api/v1/health":
                self._send_json(200, {"ok": True, "service": "agent-deck"})
            else:
                self._send_json(404, {"ok": False, "error": "Not found"})
        except Exception as error:
            logger.exception("Mobile API GET failed")
            self._send_json(500, {"ok": False, "error": str(error)})

    def do_POST(self) -> None:
        if not self._require_auth():
            return
        path = urlparse(self.path).path
        try:
            body = self._read_json()
            match = re.fullmatch(
                r"/api/v1/threads/([0-9a-f-]{36})/(open|message|archive)", path
            )
            if match:
                thread_id, action = match.groups()
                if action == "open":
                    open_thread(thread_id)
                elif action == "message":
                    submit_thread_message(thread_id, str(body.get("text") or ""))
                else:
                    codex_request("thread/archive", {"threadId": thread_id})
                    with current_thread_lock:
                        was_current = current_thread_id == thread_id
                    if was_current:
                        remember_current_thread(None)
                    logger.info("Archived thread %s from mobile", thread_id)
                self._send_json(202, {"ok": True, "action": action})
                return
            if path == "/api/v1/actions/new":
                open_new_chat()
                self._send_json(202, {"ok": True, "action": "new"})
                return
            self._send_json(404, {"ok": False, "error": "Not found"})
        except (ValueError, json.JSONDecodeError) as error:
            self._send_json(400, {"ok": False, "error": str(error)})
        except Exception as error:
            logger.exception("Mobile API POST failed")
            self._send_json(500, {"ok": False, "error": str(error)})


def start_mobile_server() -> ThreadingHTTPServer:
    global mobile_server
    config = load_mobile_config()
    configured_host = str(config.get("host") or "127.0.0.1")
    bind_host = "0.0.0.0" if configured_host == "127.0.0.1" else configured_host
    server = ThreadingHTTPServer(
        (bind_host, int(config.get("port") or 8765)),
        MobileRequestHandler,
    )
    server.daemon_threads = True
    setattr(server, "agent_deck_token", str(config["token"]))
    threading.Thread(
        target=server.serve_forever, name="agent-deck-mobile", daemon=True
    ).start()
    mobile_server = server
    logger.info(
        "Mobile API listening on http://%s:%s",
        bind_host,
        config.get("port"),
    )
    return server


def open_new_chat() -> None:
    try:
        os.startfile("codex://threads/new")
        remember_current_thread(None)
        logger.info("Opened a new Codex chat")
    except Exception:
        logger.exception("Could not open a new Codex chat")


def toggle_codex_dictation() -> None:
    """Start Codex dictation, or transcribe and send if it is already active."""

    script = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$window = Get-Process -Name ChatGPT -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    Sort-Object StartTime |
    Select-Object -First 1
if ($null -eq $window) { exit 2 }

$user32 = Add-Type -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
'@ -Name NativeWindow -Namespace AgentDeck -PassThru
$null = $user32::SetForegroundWindow($window.MainWindowHandle)

$root = [System.Windows.Automation.AutomationElement]::FromHandle(
    $window.MainWindowHandle
)
$typeCondition = [System.Windows.Automation.PropertyCondition]::new(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Button
)
$targets = @(
    @{ Name = "Transcribe and send"; Action = "sent" },
    @{ Name = "Dictate"; Action = "started" }
)
foreach ($target in $targets) {
    $nameCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $target.Name
    )
    $condition = [System.Windows.Automation.AndCondition]::new(
        $nameCondition,
        $typeCondition
    )
    $buttons = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        $condition
    )
    for ($index = 0; $index -lt $buttons.Count; $index++) {
        $button = $buttons.Item($index)
        if ($button.Current.IsOffscreen -or -not $button.Current.IsEnabled) {
            continue
        }
        $invoke = $button.GetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern
        )
        $invoke.Invoke()
        Write-Output $target.Action
        exit 0
    }
}
exit 3
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Codex Dictate button unavailable (exit {result.returncode})"
            )
        action = result.stdout.strip()
        if action == "sent":
            logger.info("Ended Codex dictation and submitted transcription")
        else:
            logger.info("Started Codex native dictation")
    except Exception:
        logger.exception("Could not toggle Codex native dictation")


def toggle_cursor_voice_input() -> None:
    """Start Cursor voice input, or send the recording if it is already active."""

    script = r"""
$ordered = @()
$agents = Get-AgentsWindow
if ($null -ne $agents) { $ordered += $agents }
foreach ($window in Get-CursorWindows) {
    if ($null -ne $agents -and $window.Id -eq $agents.Id) { continue }
    $ordered += $window
}
if ($ordered.Count -eq 0) { exit 2 }

function Find-VoiceAction($window) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle(
        $window.MainWindowHandle
    )
    $targets = @(
        @{ Name = 'Send voice input'; Action = 'sent' },
        @{ Name = 'Start voice input'; Action = 'started' }
    )
    foreach ($target in $targets) {
        $buttons = Find-ByControlType $root (
            [System.Windows.Automation.ControlType]::Button
        )
        for ($index = 0; $index -lt $buttons.Count; $index++) {
            $button = $buttons.Item($index)
            if ($button.Current.Name -ne $target.Name) { continue }
            if ($button.Current.IsOffscreen -or -not $button.Current.IsEnabled) {
                continue
            }
            if (Invoke-Element $button -or Click-Element $button) {
                return $target.Action
            }
        }
    }
    return $null
}

foreach ($window in $ordered) {
    Set-CursorWindowForeground $window.MainWindowHandle
    Start-Sleep -Milliseconds 150
    $action = Find-VoiceAction $window
    if ($null -ne $action) {
        Write-Output $action
        exit 0
    }
}

# Fallback: Cursor's voice shortcut (toggle in editor; hold-to-talk in Agents).
$focus = $ordered[0]
Set-CursorWindowForeground $focus.MainWindowHandle
Start-Sleep -Milliseconds 120
[System.Windows.Forms.SendKeys]::SendWait('^m')
Write-Output 'shortcut'
exit 0
"""
    remember_selected_ide("cursor")
    outcome = run_cursor_uia_script(script, {})
    if outcome == "sent":
        logger.info("Ended Cursor voice input and submitted transcription")
    elif outcome == "started":
        logger.info("Started Cursor voice input")
    elif outcome == "shortcut":
        logger.info("Toggled Cursor voice input via Ctrl+M")
    else:
        logger.warning("Could not toggle Cursor voice input")


def toggle_dictation() -> None:
    """Toggle native voice input in the most recently selected IDE."""

    if current_selected_ide() == "cursor":
        toggle_cursor_voice_input()
    else:
        toggle_codex_dictation()


def archive_viewed_chat_in_codex() -> str | None:
    """Invoke Codex's own Archive button so its sidebar updates immediately."""

    script = r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$window = Get-Process -Name ChatGPT -ErrorAction SilentlyContinue |
    Where-Object { $_.MainWindowHandle -ne 0 } |
    Sort-Object StartTime |
    Select-Object -First 1
if ($null -eq $window) { exit 2 }
$root = [System.Windows.Automation.AutomationElement]::FromHandle($window.MainWindowHandle)
$all = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)
$windowTop = $root.Current.BoundingRectangle.Top
$heading = $null
for ($index = 0; $index -lt $all.Count; $index++) {
    $item = $all.Item($index)
    $bounds = $item.Current.BoundingRectangle
    if ($item.Current.ControlType -eq [System.Windows.Automation.ControlType]::Text -and
        -not $item.Current.IsOffscreen -and
        $bounds.Top -ge $windowTop -and
        $bounds.Top -le ($windowTop + 120) -and
        $item.Current.Name) {
        $heading = $item.Current.Name
        break
    }
}
if (-not $heading) { exit 3 }
for ($index = 0; $index -lt $all.Count; $index++) {
    $item = $all.Item($index)
    if ($item.Current.ControlType -ne [System.Windows.Automation.ControlType]::ListItem -or
        $item.Current.Name -ne $heading) {
        continue
    }
    $children = $item.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    for ($childIndex = 0; $childIndex -lt $children.Count; $childIndex++) {
        $button = $children.Item($childIndex)
        if ($button.Current.ControlType -eq [System.Windows.Automation.ControlType]::Button -and
            $button.Current.Name -eq "Archive chat") {
            $pattern = $button.GetCurrentPattern(
                [System.Windows.Automation.InvokePattern]::Pattern
            )
            $pattern.Invoke()
            Write-Output ("ARCHIVED|" + $heading)
            exit 0
        }
    }
}
exit 4
"""
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("ARCHIVED|"):
            return line.partition("|")[2]
    return None


def archive_cursor_in_db(composer_id: str) -> bool:
    """Mark a Cursor chat archived in Cursor's local state database."""

    if not CURSOR_GLOBAL_DB.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(CURSOR_GLOBAL_DB), timeout=2.0)
        connection.execute("PRAGMA busy_timeout=2000")
        row = connection.execute(
            "SELECT value FROM composerHeaders WHERE composerId = ?",
            (composer_id,),
        ).fetchone()
        if row is None:
            return False
        header = json.loads(row[0])
        header["isArchived"] = True
        connection.execute(
            """
            UPDATE composerHeaders
            SET isArchived = 1, value = ?
            WHERE composerId = ?
            """,
            (json.dumps(header, separators=(",", ":")), composer_id),
        )
        data_row = connection.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{composer_id}",),
        ).fetchone()
        if data_row is not None:
            payload = json.loads(data_row[0])
            payload["isArchived"] = True
            connection.execute(
                "UPDATE cursorDiskKV SET value = ? WHERE key = ?",
                (
                    json.dumps(payload, separators=(",", ":")),
                    f"composerData:{composer_id}",
                ),
            )
        connection.commit()
        return True
    except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError):
        logger.exception("Could not archive Cursor chat %s in the local DB", composer_id)
        return False
    finally:
        if connection is not None:
            connection.close()


def archive_cursor_via_uia(title: str) -> bool:
    """Best-effort: archive the named chat through Cursor's Agents/history UI."""

    if not title:
        return False
    script = r"""
$title = [string]$env:AGENTDECK_CHAT_TITLE
if (-not $title) { exit 5 }
$agents = Open-AgentsWindow
if ($null -eq $agents) { exit 2 }
Set-CursorWindowForeground $agents.MainWindowHandle
Start-Sleep -Milliseconds 250
$root = [System.Windows.Automation.AutomationElement]::FromHandle(
    $agents.MainWindowHandle
)
$buttons = Find-ByControlType $root (
    [System.Windows.Automation.ControlType]::Button
)
for ($index = 0; $index -lt $buttons.Count; $index++) {
    $button = $buttons.Item($index)
    if (-not (Test-CursorAgentTitle $button.Current.Name $title)) { continue }
    if ($button.Current.IsOffscreen) { continue }
    Click-Element $button | Out-Null
    break
}
Start-Sleep -Milliseconds 500
$root = [System.Windows.Automation.AutomationElement]::FromHandle(
    $agents.MainWindowHandle
)
$items = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Descendants,
    [System.Windows.Automation.Condition]::TrueCondition
)
for ($index = 0; $index -lt $items.Count; $index++) {
    $item = $items.Item($index)
    if ($item.Current.Name -ne 'Archive') { continue }
    if ($item.Current.IsOffscreen) { continue }
    if (Click-Element $item) { Write-Output 'AGENTS_ARCHIVE'; exit 0 }
}
$actions = $null
$buttons = Find-ByControlType $root (
    [System.Windows.Automation.ControlType]::Button
)
for ($index = 0; $index -lt $buttons.Count; $index++) {
    if ($buttons.Item($index).Current.Name -eq 'Chat actions') {
        $actions = $buttons.Item($index)
        break
    }
}
if ($null -ne $actions) {
    Click-Element $actions | Out-Null
    Start-Sleep -Milliseconds 500
    $root = [System.Windows.Automation.AutomationElement]::FromHandle(
        $agents.MainWindowHandle
    )
    $items = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    for ($index = 0; $index -lt $items.Count; $index++) {
        $item = $items.Item($index)
        if ($item.Current.Name -ne 'Archive') { continue }
        if ($item.Current.IsOffscreen) { continue }
        if (Click-Element $item) { Write-Output 'CHAT_ACTIONS'; exit 0 }
    }
}
foreach ($window in Get-CursorWindows) {
    if ($window.MainWindowTitle -eq 'Cursor Agents') { continue }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle(
        $window.MainWindowHandle
    )
    $buttons = Find-ByControlType $root (
        [System.Windows.Automation.ControlType]::Button
    )
    $history = $null
    for ($index = 0; $index -lt $buttons.Count; $index++) {
        if ($buttons.Item($index).Current.Name -eq 'Show Chat History') {
            $history = $buttons.Item($index)
            break
        }
    }
    if ($null -eq $history) { continue }
    Set-CursorWindowForeground $window.MainWindowHandle
    Start-Sleep -Milliseconds 200
    if (-not (Invoke-Element $history)) {
        if (-not (Click-Element $history)) { continue }
    }
    Start-Sleep -Milliseconds 450
    $escaped = $title -replace '([+^%~(){}\[\]])', '{$1}'
    [System.Windows.Forms.SendKeys]::SendWait('^a')
    Start-Sleep -Milliseconds 40
    [System.Windows.Forms.SendKeys]::SendWait($escaped)
    Start-Sleep -Milliseconds 700
    $root = [System.Windows.Automation.AutomationElement]::FromHandle(
        $window.MainWindowHandle
    )
    $items = $root.FindAll(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    for ($index = 0; $index -lt $items.Count; $index++) {
        $item = $items.Item($index)
        if ($item.Current.Name -ne 'Archive') { continue }
        if ($item.Current.IsOffscreen) { continue }
        $bounds = $item.Current.BoundingRectangle
        if ($bounds.Width -le 0 -or $bounds.Height -le 0) { continue }
        if (Click-Element $item) { Write-Output 'HISTORY_ARCHIVE'; exit 0 }
    }
}
exit 4
"""
    outcome = run_cursor_uia_script(script, {"AGENTDECK_CHAT_TITLE": title})
    if outcome:
        logger.info("Archived Cursor chat via UI (%s)", outcome)
        return True
    return False


def resolve_cursor_agent_to_archive() -> dict[str, str] | None:
    """Pick the Cursor chat the archive button should target."""

    agent = current_cursor_agent_snapshot()
    if agent and agent.get("id"):
        return agent
    # Fall back to the most recently updated non-archived Cursor chat.
    threads = load_cursor_threads(1)
    if not threads:
        return None
    thread = threads[0]
    return {
        "id": str(thread["id"]),
        "title": str(thread.get("title") or ""),
    }


def archive_cursor_chat() -> None:
    agent = resolve_cursor_agent_to_archive()
    if not agent:
        logger.warning("Archive pressed while no Cursor chat is available")
        return
    composer_id = agent["id"]
    title = agent.get("title") or ""
    remember_selected_ide("cursor")
    ui_ok = archive_cursor_via_uia(title) if title else False
    db_ok = archive_cursor_in_db(composer_id)
    if db_ok or ui_ok:
        remember_current_cursor_agent(None)
        logger.info(
            "Archived Cursor chat %s (%s) ui=%s db=%s",
            composer_id,
            title,
            ui_ok,
            db_ok,
        )
    else:
        logger.warning(
            "Could not archive Cursor chat %s (%s)", composer_id, title
        )


def archive_current_chat() -> None:
    if not archive_lock.acquire(blocking=False):
        return
    try:
        if current_selected_ide() == "cursor":
            archive_cursor_chat()
            return

        archived_title = archive_viewed_chat_in_codex()
        if archived_title:
            remember_current_thread(None)
            logger.info("Archived viewed chat natively: %s", archived_title)
            return

        _detected, detected_thread_id = detect_viewed_thread()
        if detected_thread_id:
            thread_id = detected_thread_id
        else:
            with current_thread_lock:
                thread_id = current_thread_id
        if not thread_id:
            # If Codex has nothing selected but a Cursor chat was opened last,
            # archive that instead of failing silently.
            if current_cursor_agent_snapshot():
                archive_cursor_chat()
                return
            logger.warning("Archive pressed while no Codex chat is being viewed")
            return
        codex_request("thread/archive", {"threadId": thread_id})
        remember_current_thread(None)
        os.startfile("codex://threads/new")
        logger.info("Archived thread %s", thread_id)
    except Exception:
        logger.exception("Could not archive the current thread")
    finally:
        archive_lock.release()


def open_thread(thread_id: str) -> None:
    try:
        acknowledge_completion(thread_id)
        os.startfile(f"codex://threads/{thread_id}")
        remember_current_thread(thread_id)
        remember_selected_ide("codex")
        logger.info("Opened thread %s", thread_id)
    except Exception:
        logger.exception("Could not open thread %s", thread_id)


def find_cursor_executable() -> Path | None:
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")
    )
    return newest_executable(
        [
            local_app_data / "Programs" / "cursor" / "Cursor.exe",
            local_app_data / "Programs" / "Cursor" / "Cursor.exe",
        ]
    )


CURSOR_UIA_PRELUDE = r"""
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class AgentDeckCursorWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);
    public const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    public const uint MOUSEEVENTF_LEFTUP = 0x0004;
}
'@
[void][AgentDeckCursorWindow]::SetProcessDPIAware()
function Set-CursorWindowForeground([IntPtr]$handle) {
    if ([AgentDeckCursorWindow]::IsIconic($handle)) {
        [AgentDeckCursorWindow]::ShowWindowAsync($handle, 9) | Out-Null
        Start-Sleep -Milliseconds 150
    }
    [AgentDeckCursorWindow]::SetForegroundWindow($handle) | Out-Null
}
function Get-CursorWindows {
    @(
        Get-Process -Name Cursor -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 }
    )
}
function Find-ByControlType($root, $controlType) {
    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        $controlType
    )
    $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
}
function Invoke-Element($element) {
    try {
        $pattern = $element.GetCurrentPattern(
            [System.Windows.Automation.InvokePattern]::Pattern
        )
        $pattern.Invoke()
        return $true
    } catch {
    }
    try {
        $pattern = $element.GetCurrentPattern(
            [System.Windows.Automation.SelectionItemPattern]::Pattern
        )
        $pattern.Select()
        return $true
    } catch {
        return $false
    }
}
function Click-Element($element) {
    $bounds = $element.Current.BoundingRectangle
    if ($bounds.Width -le 0 -or $bounds.Height -le 0) { return $false }
    $x = [int]($bounds.Left + [Math]::Min(40, $bounds.Width / 3))
    $y = [int]($bounds.Top + ($bounds.Height / 2))
    [AgentDeckCursorWindow]::SetCursorPos($x, $y) | Out-Null
    Start-Sleep -Milliseconds 30
    [AgentDeckCursorWindow]::mouse_event(
        [AgentDeckCursorWindow]::MOUSEEVENTF_LEFTDOWN, 0, 0, 0, [UIntPtr]::Zero
    )
    Start-Sleep -Milliseconds 30
    [AgentDeckCursorWindow]::mouse_event(
        [AgentDeckCursorWindow]::MOUSEEVENTF_LEFTUP, 0, 0, 0, [UIntPtr]::Zero
    )
    return $true
}
function Test-CursorAgentTitle($name, $title) {
    if (-not $name -or -not $title) { return $false }
    if ($name -eq $title) { return $true }
    if ($name.StartsWith($title + ',')) { return $true }
    if ($name -match '^(done-seen|done-unseen)\s+(.+)$') {
        $rest = $Matches[2]
        if ($rest -match '^(.*?)\s+(\d+(?:mo|[smhdw]))$') {
            return $Matches[1] -eq $title
        }
    }
    if ($name.StartsWith($title) -and $name.Length -gt $title.Length) {
        $rest = $name.Substring($title.Length)
        return [bool]($rest -match '^[0-9]' -or $rest -match '^\s+\d')
    }
    return $false
}
function Get-AgentsWindow {
    Get-Process -Name Cursor -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle -eq 'Cursor Agents' } |
        Select-Object -First 1
}
function Open-AgentsWindow {
    $existing = Get-AgentsWindow
    if ($null -ne $existing) { return $existing }
    $windows = Get-CursorWindows
    foreach ($window in $windows) {
        $root = [System.Windows.Automation.AutomationElement]::FromHandle(
            $window.MainWindowHandle
        )
        $buttons = Find-ByControlType $root (
            [System.Windows.Automation.ControlType]::Button
        )
        for ($index = 0; $index -lt $buttons.Count; $index++) {
            $button = $buttons.Item($index)
            if (-not $button.Current.Name.StartsWith('Agents Window')) { continue }
            Set-CursorWindowForeground $window.MainWindowHandle
            if (Invoke-Element $button) {
                for ($attempt = 0; $attempt -lt 20; $attempt++) {
                    Start-Sleep -Milliseconds 150
                    $existing = Get-AgentsWindow
                    if ($null -ne $existing) { return $existing }
                }
            }
        }
    }
    return Get-AgentsWindow
}
"""


def run_cursor_uia_script(script: str, extra_env: dict[str, str]) -> str | None:
    """Run a Cursor UI-automation script; return its stdout tag or None."""

    encoded = base64.b64encode(
        (CURSOR_UIA_PRELUDE + script).encode("utf-16le")
    ).decode("ascii")
    environment = os.environ.copy()
    environment.update(extra_env)
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("Cursor UI automation script failed to run")
        return None
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if detail:
            logger.warning(
                "Cursor UI automation exited %s: %s",
                result.returncode,
                detail[:300],
            )
        return None
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else "OK"


def open_cursor_thread(thread_id: str, title: str, workspace_path: str) -> None:
    """Bring the given Cursor agent chat forward inside Cursor."""

    acknowledge_cursor_completion(thread_id)
    remember_selected_ide("cursor")
    remember_current_cursor_agent(thread_id, title)
    script = r"""
$title = [string]$env:AGENTDECK_CHAT_TITLE
if (-not $title) { exit 5 }
$hint = [string]$env:AGENTDECK_WS_HINT
$windows = Get-CursorWindows
if ($windows.Count -eq 0) { exit 2 }
$preferred = @(
    $windows | Where-Object {
        $hint -and $_.MainWindowTitle -like "*$hint*" -and
            $_.MainWindowTitle -ne 'Cursor Agents'
    }
)
$editors = @(
    $windows | Where-Object { $_.MainWindowTitle -ne 'Cursor Agents' }
)
$ordered = @($preferred) + @($editors | Where-Object { $preferred -notcontains $_ })
foreach ($window in $ordered) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle(
        $window.MainWindowHandle
    )
    $tabs = Find-ByControlType $root (
        [System.Windows.Automation.ControlType]::TabItem
    )
    for ($index = 0; $index -lt $tabs.Count; $index++) {
        $tab = $tabs.Item($index)
        if (Test-CursorAgentTitle $tab.Current.Name $title) {
            Set-CursorWindowForeground $window.MainWindowHandle
            if (Invoke-Element $tab) { Write-Output "TAB"; exit 0 }
        }
    }
    $items = Find-ByControlType $root (
        [System.Windows.Automation.ControlType]::ListItem
    )
    for ($index = 0; $index -lt $items.Count; $index++) {
        $item = $items.Item($index)
        if ($item.Current.Name -eq $title) {
            Set-CursorWindowForeground $window.MainWindowHandle
            if (Invoke-Element $item) { Write-Output "LIST"; exit 0 }
        }
    }
}
$agents = Open-AgentsWindow
if ($null -eq $agents) { exit 3 }
Set-CursorWindowForeground $agents.MainWindowHandle
Start-Sleep -Milliseconds 250
$root = [System.Windows.Automation.AutomationElement]::FromHandle(
    $agents.MainWindowHandle
)
$buttons = Find-ByControlType $root (
    [System.Windows.Automation.ControlType]::Button
)
for ($index = 0; $index -lt $buttons.Count; $index++) {
    $button = $buttons.Item($index)
    if (-not (Test-CursorAgentTitle $button.Current.Name $title)) { continue }
    if ($button.Current.IsOffscreen) { continue }
    if (Click-Element $button) {
        Start-Sleep -Milliseconds 400
        Write-Output "AGENTS"
        exit 0
    }
}
exit 4
"""
    workspace_hint = Path(workspace_path).name if workspace_path else ""
    outcome = run_cursor_uia_script(
        script,
        {
            "AGENTDECK_CHAT_TITLE": title,
            "AGENTDECK_WS_HINT": workspace_hint,
        },
    )
    if outcome:
        logger.info("Opened Cursor chat %s via %s", thread_id, outcome)
        return

    # Cold start: launch Cursor on the chat's workspace, then retry Agents Window.
    executable = find_cursor_executable()
    if executable is not None:
        arguments = [str(executable)]
        if workspace_path and Path(workspace_path).exists():
            arguments.append(workspace_path)
        subprocess.Popen(arguments, close_fds=True)
        time.sleep(2.5)
        outcome = run_cursor_uia_script(
            script,
            {
                "AGENTDECK_CHAT_TITLE": title,
                "AGENTDECK_WS_HINT": workspace_hint,
            },
        )
        if outcome:
            logger.info(
                "Opened Cursor chat %s via %s after launch", thread_id, outcome
            )
            return

    if focus_running_application({"cursor.exe"}):
        logger.info(
            "Focused Cursor without locating chat %s (%s)", thread_id, title
        )
        return
    logger.warning("Could not open Cursor chat %s (%s)", thread_id, title)


def open_new_cursor_agent() -> None:
    """Start a new agent chat in Cursor, preferring its own New Agent control."""

    remember_selected_ide("cursor")
    remember_current_cursor_agent(None)
    script = r"""
$windows = Get-CursorWindows
if ($windows.Count -eq 0) { exit 2 }
function Find-NewAgentControl($root) {
    $buttons = Find-ByControlType $root ([System.Windows.Automation.ControlType]::Button)
    for ($index = 0; $index -lt $buttons.Count; $index++) {
        $button = $buttons.Item($index)
        if ($button.Current.Name.StartsWith("New Agent")) { return $button }
    }
    $items = Find-ByControlType $root ([System.Windows.Automation.ControlType]::ListItem)
    for ($index = 0; $index -lt $items.Count; $index++) {
        $item = $items.Item($index)
        if ($item.Current.Name -eq "New Agent") { return $item }
    }
    return $null
}
foreach ($window in $windows) {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($window.MainWindowHandle)
    $control = Find-NewAgentControl $root
    if ($null -ne $control) {
        Set-CursorWindowForeground $window.MainWindowHandle
        if (Invoke-Element $control) { Write-Output "NEW"; exit 0 }
    }
}
$window = $windows[0]
$root = [System.Windows.Automation.AutomationElement]::FromHandle($window.MainWindowHandle)
$buttons = Find-ByControlType $root ([System.Windows.Automation.ControlType]::Button)
for ($index = 0; $index -lt $buttons.Count; $index++) {
    $button = $buttons.Item($index)
    if ($button.Current.Name.StartsWith("Toggle Agents")) {
        Set-CursorWindowForeground $window.MainWindowHandle
        if (Invoke-Element $button) {
            Start-Sleep -Milliseconds 500
            $control = Find-NewAgentControl $root
            if ($null -ne $control -and (Invoke-Element $control)) {
                Write-Output "TOGGLED"
                exit 0
            }
        }
        break
    }
}
exit 3
"""
    outcome = run_cursor_uia_script(script, {})
    if outcome:
        logger.info("Opened a new Cursor agent via %s", outcome)
        return
    if focus_running_application({"cursor.exe"}):
        logger.warning("Focused Cursor but could not reach its New Agent control")
        return
    executable = find_cursor_executable()
    if executable is None:
        logger.warning("Cursor executable was not found for a new agent")
        return
    subprocess.Popen([str(executable)], close_fds=True)
    logger.info("Launched Cursor for a new agent")


def open_new_agent() -> None:
    """Spawn a new agent in whichever IDE was selected most recently."""

    if current_selected_ide() == "cursor":
        open_new_cursor_agent()
    else:
        open_new_chat()


def newest_executable(candidates: list[Path]) -> Path | None:
    existing = [path for path in candidates if path.is_file()]
    if not existing:
        return None
    return max(existing, key=lambda path: path.stat().st_mtime)


def focus_running_application(process_names: set[str]) -> bool:
    class WindowPlacement(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.UINT),
            ("flags", wintypes.UINT),
            ("show_command", wintypes.UINT),
            ("minimum_position", wintypes.POINT),
            ("maximum_position", wintypes.POINT),
            ("normal_position", wintypes.RECT),
        ]

    wanted = {name.casefold() for name in process_names}
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsZoomed.argtypes = [wintypes.HWND]
    user32.GetWindowPlacement.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(WindowPlacement),
    ]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    user32.AttachThreadInput.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.BOOL,
    ]
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process_query_limited_information = 0x1000
    candidates: list[tuple[bool, int]] = []

    enum_callback = ctypes.WINFUNCTYPE(
        wintypes.BOOL,
        wintypes.HWND,
        wintypes.LPARAM,
    )

    @enum_callback
    def collect_window(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.GetWindowTextLengthW(hwnd) <= 0:
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            process_id.value,
        )
        if not handle:
            return True
        try:
            capacity = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if kernel32.QueryFullProcessImageNameW(
                handle,
                0,
                buffer,
                ctypes.byref(capacity),
            ):
                if Path(buffer.value).name.casefold() in wanted:
                    candidates.append((bool(user32.IsWindowEnabled(hwnd)), hwnd))
        finally:
            kernel32.CloseHandle(handle)
        return True

    user32.EnumWindows(collect_window, 0)
    if not candidates:
        return False
    hwnd = next(
        (candidate for enabled, candidate in candidates if enabled),
        candidates[0][1],
    )
    current_thread = kernel32.GetCurrentThreadId()
    target_thread = user32.GetWindowThreadProcessId(hwnd, None)
    foreground = user32.GetForegroundWindow()
    foreground_thread = (
        user32.GetWindowThreadProcessId(foreground, None)
        if foreground
        else 0
    )
    if foreground_thread and foreground_thread != current_thread:
        user32.AttachThreadInput(current_thread, foreground_thread, True)
    if target_thread and target_thread != current_thread:
        user32.AttachThreadInput(current_thread, target_thread, True)
    placement = WindowPlacement()
    placement.length = ctypes.sizeof(WindowPlacement)
    restore_to_maximized = bool(user32.IsZoomed(hwnd))
    if user32.GetWindowPlacement(hwnd, ctypes.byref(placement)):
        restore_to_maximized = restore_to_maximized or bool(placement.flags & 0x0002)
    if user32.IsIconic(hwnd):
        user32.ShowWindowAsync(hwnd, 3 if restore_to_maximized else 9)
    user32.BringWindowToTop(hwnd)
    user32.keybd_event(0x12, 0, 0, 0)
    user32.keybd_event(0x12, 0, 0x0002, 0)
    user32.SwitchToThisWindow(hwnd, True)
    focused = bool(user32.SetForegroundWindow(hwnd))
    user32.SetFocus(hwnd)
    if target_thread and target_thread != current_thread:
        user32.AttachThreadInput(current_thread, target_thread, False)
    if foreground_thread and foreground_thread != current_thread:
        user32.AttachThreadInput(current_thread, foreground_thread, False)
    return focused or user32.GetForegroundWindow() == hwnd


def focus_existing_chrome_tab(hints: list[str]) -> bool:
    cleaned_hints = [hint.strip() for hint in hints if hint.strip()]
    if not cleaned_hints:
        return False
    script = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class AgentDeckWindow {
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@
$root = [System.Windows.Automation.AutomationElement]::RootElement
$windows = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Children,
    [System.Windows.Automation.Condition]::TrueCondition
)
$tabCondition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::TabItem
)
$hints = ConvertFrom-Json $env:AGENTDECK_TAB_HINTS
foreach ($hint in $hints) {
    foreach ($window in $windows) {
        if (
            $window.Current.ClassName -ne 'Chrome_WidgetWin_1' -or
            $window.Current.Name -notlike '*Google Chrome*'
        ) {
            continue
        }
        $tabs = $window.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $tabCondition
        )
        foreach ($tab in $tabs) {
            if (
                $tab.Current.Name.IndexOf(
                    [string]$hint,
                    [System.StringComparison]::OrdinalIgnoreCase
                ) -lt 0
            ) {
                continue
            }
            try {
                $selection = $tab.GetCurrentPattern(
                    [System.Windows.Automation.SelectionItemPattern]::Pattern
                )
                $selection.Select()
                $handle = [IntPtr]$window.Current.NativeWindowHandle
                if ([AgentDeckWindow]::IsIconic($handle)) {
                    [AgentDeckWindow]::ShowWindowAsync($handle, 9) | Out-Null
                }
                [AgentDeckWindow]::SetForegroundWindow($handle) | Out-Null
                exit 0
            } catch {
                continue
            }
        }
    }
}
exit 2
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    environment = os.environ.copy()
    environment["AGENTDECK_TAB_HINTS"] = json.dumps(cleaned_hints)
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-WindowStyle",
                "Hidden",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=8,
            env=environment,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.exception("Could not search Chrome for an existing tab")
        return False
    if result.returncode == 0:
        return True
    if result.returncode != 2:
        logger.warning(
            "Chrome tab search failed with exit code %s",
            result.returncode,
        )
    return False


def chrome_site_hints(url_or_host: str) -> list[str]:
    value = url_or_host.strip().strip("'\"")
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").casefold()
    known = (
        ("mail.google.com", "Gmail"),
        ("calendar.google.com", "Google Calendar"),
        ("github.com", "GitHub"),
        ("amazon.com", "Amazon"),
        ("chatgpt.com", "ChatGPT"),
        ("slack.com", "Slack"),
        *PRIVATE_SITE_HINTS.items(),
    )
    hints = [label for domain, label in known if host == domain or host.endswith(f".{domain}")]
    labels = [
        part
        for part in host.split(".")
        if part not in {"www", "com", "org", "net", "co", "app"}
    ]
    if labels:
        fallback = labels[-1].replace("-", " ")
        if fallback and fallback.casefold() not in {hint.casefold() for hint in hints}:
            hints.append(fallback)
    return hints


def recent_chrome_tab_hints(thread_id: str) -> list[str]:
    try:
        uri = f"{STATE_DB.as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as connection:
            row = connection.execute(
                "SELECT rollout_path FROM threads WHERE id = ?",
                (thread_id,),
            ).fetchone()
    except sqlite3.Error:
        logger.exception("Could not find rollout for Chrome handoff %s", thread_id)
        return []
    if row is None or not row[0]:
        return []

    current_time = time.time()
    browser_markers = (
        ".playwright.",
        ".dom_cua.",
        ".cua.",
        ".claimtab(",
        ".getforurl(",
        ".opentabs(",
        ".goto(",
        "browser.user.",
        "chrome.user.",
    )
    for record in reversed(tail_records(str(row[0]), max_bytes=2_000_000)):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("type") not in {"function_call", "custom_tool_call"}:
            continue
        if str(payload.get("name") or "").casefold() != "js":
            continue
        namespace = str(payload.get("namespace") or "").casefold()
        if "node_repl" not in namespace:
            continue
        timestamp = record_timestamp(record)
        if timestamp and current_time - timestamp > RECENT_CHROME_CONTROL_SECONDS:
            break
        raw_input = payload.get("input")
        if raw_input is None:
            raw_input = payload.get("arguments")
        if isinstance(raw_input, dict):
            code = str(raw_input.get("code") or "")
        else:
            try:
                decoded = json.loads(str(raw_input or ""))
                code = str(decoded.get("code") or "") if isinstance(decoded, dict) else str(decoded)
            except json.JSONDecodeError:
                code = str(raw_input or "")
        lowered = code.casefold()
        if not any(marker in lowered for marker in browser_markers):
            continue

        urls = re.findall(r"https?://[^\s'\"`<>\\)]+", code)
        for candidate in reversed(urls):
            if "/scripts/" in candidate or candidate.endswith(".mjs"):
                continue
            hints = chrome_site_hints(candidate)
            if hints:
                return hints
        fragments = re.findall(
            r"(?:includes|startswith)\(\s*['\"]([^'\"]+\.[^'\"]+)['\"]",
            code,
            flags=re.IGNORECASE,
        )
        for candidate in reversed(fragments):
            hints = chrome_site_hints(candidate)
            if hints:
                return hints
    return []


def open_recent_chrome_tab(thread_id: str) -> None:
    hints = recent_chrome_tab_hints(thread_id)
    if not hints:
        logger.info("Second press found no recent Chrome tab for %s", thread_id)
        return
    if focus_existing_chrome_tab(hints):
        logger.info(
            "Second press focused Chrome tab for %s using %s",
            thread_id,
            hints,
        )
    else:
        logger.warning(
            "Second press could not find Chrome tab for %s using %s",
            thread_id,
            hints,
        )


def open_calendar_event() -> None:
    event = cached_calendar_event()
    if not event:
        logger.info("Calendar key pressed with no upcoming event")
        return
    try:
        os.startfile(str(event.get("url") or "https://calendar.google.com/"))
        logger.info("Opened next calendar event: %s", event.get("title"))
    except Exception:
        logger.exception("Could not open the next calendar event")


def launch_quick_access(action: str) -> None:
    program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    try:
        if action == "launch_solidworks":
            if focus_running_application({"sldworks.exe"}):
                logger.info("Focused existing XL quick access: solidworks")
                return
            executable = newest_executable(
                [
                    program_files
                    / "SOLIDWORKS Corp"
                    / "SOLIDWORKS"
                    / "SLDWORKS.exe",
                    *program_files.glob("SOLIDWORKS*/SOLIDWORKS/SLDWORKS.exe"),
                ]
            )
            if executable is None:
                raise FileNotFoundError("SOLIDWORKS executable was not found")
            os.startfile(executable)
        elif action == "launch_altium":
            if focus_running_application({"x2.exe"}):
                logger.info("Focused existing XL quick access: altium")
                return
            executable = newest_executable(
                list(program_files.glob("Altium/AD*/X2.EXE"))
            )
            if executable is None:
                raise FileNotFoundError("Altium Designer executable was not found")
            os.startfile(executable)
        elif action == "launch_slack":
            if focus_running_application({"slack.exe"}):
                logger.info("Focused existing XL quick access: slack")
                return
            os.startfile("slack://open")
        elif action == "launch_chatgpt":
            remember_selected_ide("codex")
            if focus_running_application({"chatgpt.exe"}):
                logger.info("Focused existing XL quick access: chatgpt")
                return
            os.startfile("codex://")
        elif action == "launch_cursor":
            remember_selected_ide("cursor")
            if focus_running_application({"cursor.exe"}):
                logger.info("Focused existing XL quick access: cursor")
                return
            executable = find_cursor_executable()
            if executable is None:
                raise FileNotFoundError("Cursor executable was not found")
            os.startfile(executable)
        elif action == "launch_email":
            if focus_existing_chrome_tab(["Gmail"]):
                logger.info("Focused existing XL quick access: email")
                return
            chrome = newest_executable(
                [
                    program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
                    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
                    / "Google"
                    / "Chrome"
                    / "Application"
                    / "chrome.exe",
                    local_app_data / "Google" / "Chrome" / "Application" / "chrome.exe",
                ]
            )
            if chrome is None:
                os.startfile("https://mail.google.com/")
            else:
                subprocess.Popen(
                    [str(chrome), "https://mail.google.com/"],
                    close_fds=True,
                )
        else:
            raise ValueError(f"Unknown quick-access action: {action}")
        logger.info("Launched XL quick access: %s", action.removeprefix("launch_"))
    except Exception:
        logger.exception("Could not launch XL quick access action %s", action)


def key_callback(device: Any, event: Any) -> None:
    if event.event_type != EventType.BUTTON or event.state != 1 or event.key is None:
        return
    profile: DeckProfile = device.agent_deck_profile
    decoded_key = int(event.key.value)
    key = profile.screen_key(decoded_key)

    now = time.monotonic()
    with button_lock:
        if now - last_button_press.get(key, 0.0) < 0.35:
            return
        last_button_press[key] = now

    action = profile.action_keys.get(key)
    if action == "new_chat":
        logger.info(
            "Pressed %s new-chat control (IDE: %s)",
            profile.name,
            current_selected_ide(),
        )
        threading.Thread(target=open_new_agent, daemon=True).start()
        return
    if action == "microphone":
        logger.info(
            "Pressed %s microphone control (IDE: %s)",
            profile.name,
            current_selected_ide(),
        )
        threading.Thread(target=toggle_dictation, daemon=True).start()
        return
    if action == "archive_chat":
        logger.info("Pressed %s archive control", profile.name)
        threading.Thread(target=archive_current_chat, daemon=True).start()
        return
    if action and action.startswith("launch_"):
        logger.info("Pressed %s quick access: %s", profile.name, action)
        threading.Thread(
            target=launch_quick_access,
            args=(action,),
            daemon=True,
        ).start()
        return
    if key == profile.calendar_key:
        logger.info("Pressed %s next-calendar-event control", profile.name)
        threading.Thread(target=open_calendar_event, daemon=True).start()
        return

    with assignment_lock:
        assignment = key_assignments.get(key)
    thread_id = assignment.get("id") if assignment else None
    source = assignment.get("source", "codex") if assignment else "codex"
    logger.info(
        "Pressed %s decoded key %s -> screen slot %s -> %s thread %s",
        profile.name,
        decoded_key,
        key,
        source,
        thread_id,
    )
    if thread_id and source == "cursor":
        with button_lock:
            last_agent_press.pop(key, None)
        threading.Thread(
            target=open_cursor_thread,
            args=(
                thread_id,
                assignment.get("title", ""),
                assignment.get("workspace", ""),
            ),
            daemon=True,
        ).start()
        return
    if thread_id:
        with button_lock:
            previous_thread, previous_at = last_agent_press.get(
                key,
                ("", 0.0),
            )
            second_press = (
                previous_thread == thread_id
                and now - previous_at <= SECOND_AGENT_PRESS_SECONDS
            )
            if second_press:
                last_agent_press.pop(key, None)
            else:
                last_agent_press[key] = (thread_id, now)
        if second_press:
            threading.Thread(
                target=open_recent_chrome_tab,
                args=(thread_id,),
                daemon=True,
            ).start()
            return
        threading.Thread(target=open_thread, args=(thread_id,), daemon=True).start()


def connect_device() -> Any | None:
    for profile in DEVICE_PROFILES:
        devices = LibUSBHIDAPI.enumerate_devices(
            vendor_id=profile.vendor_id,
            product_id=profile.product_id,
        )
        if not devices:
            continue
        info = devices[0]
        device_info = LibUSBHIDAPI.create_device_info_from_dict(info)
        deck = profile.device_class(LibUSBHIDAPI(device_info), info)
        deck.agent_deck_profile = profile
        deck.set_device()
        if not deck.open():
            continue
        deck.wakeScreen()
        deck.set_brightness(profile.display_brightness)
        deck.clearAllIcon()
        deck.refresh()
        deck.set_key_callback(key_callback)
        logger.info(
            "Connected %s %04X:%04X serial=%s keys=%s layout=%sx%s image=%spx brightness=%s%% firmware=%s",
            profile.name,
            profile.vendor_id,
            profile.product_id,
            info.get("serial_number", ""),
            profile.key_count,
            profile.columns,
            profile.rows,
            profile.key_size,
            profile.display_brightness,
            deck.transport.get_firmware_version(),
        )
        return deck
    return None


def led_worker(
    deck: Any,
    mode_state: dict[str, str],
    mode_lock: threading.Lock,
    stop_event: threading.Event,
    error_event: threading.Event,
) -> None:
    profile: DeckProfile = deck.agent_deck_profile
    previous_color: tuple[int, int, int] | None = None
    previous_mode: str | None = None
    try:
        while not stop_event.is_set():
            with mode_lock:
                mode = mode_state["mode"]

            if mode != previous_mode:
                logger.info("%s chassis mode: %s", profile.name, mode)
                previous_mode = mode

            if mode == "approval":
                color = (255, 0, 0) if int(time.monotonic() * 2.5) % 2 == 0 else (0, 0, 0)
            elif mode == "control":
                color = (80, 190, 255)
            elif mode == "solving":
                color = (0, 28, 92)
            elif mode == "done":
                color = (0, 255, 0)
            else:
                color = (12, 18, 28)

            if color != previous_color:
                deck.set_led_color(*color)
                previous_color = color
            stop_event.wait(LED_REFRESH_SECONDS)
    except Exception:
        logger.exception("%s chassis LED worker failed", profile.name)
        error_event.set()


def aggregate_led_mode(statuses: list[str], computer_control: bool) -> str:
    if "wait" in statuses:
        return "approval"
    if computer_control:
        return "control"
    if "active" in statuses:
        return "solving"
    if not statuses or all(status == "done" for status in statuses):
        return "done"
    return "idle"


def run_connected(deck: Any) -> None:
    profile: DeckProfile = deck.agent_deck_profile
    previous_signatures: dict[int, tuple[Any, ...]] = {}
    activity_cache: dict[
        str, tuple[float, tuple[str, int, bool, bool]]
    ] = {}
    service_tier_cache: dict[str, tuple[float, str]] = {}
    led_mode_state = {"mode": "idle"}
    led_mode_lock = threading.Lock()
    led_stop = threading.Event()
    led_error = threading.Event()
    led_thread = threading.Thread(
        target=led_worker,
        args=(deck, led_mode_state, led_mode_lock, led_stop, led_error),
        daemon=True,
    )
    led_thread.start()
    threads: list[dict[str, Any]] = []
    unread: set[str] = set()
    state_checked_at = 0.0
    device_checked_at = 0.0
    ide_checked_at = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - device_checked_at >= DEVICE_REFRESH_SECONDS:
                available = LibUSBHIDAPI().enumerate_devices(
                    vendor_id=profile.vendor_id,
                    product_id=profile.product_id,
                )
                if not available:
                    raise ConnectionError("StreamDock disconnected")
                device_checked_at = now
            if led_error.is_set():
                raise OSError(f"{profile.name} chassis LED worker stopped")

            if now - ide_checked_at >= IDE_FOCUS_REFRESH_SECONDS:
                focused_ide = detect_foreground_ide()
                if focused_ide:
                    remember_selected_ide(focused_ide)
                ide_checked_at = now

            if now - state_checked_at >= STATE_REFRESH_SECONDS:
                threads = load_agents(profile.agent_key_count)
                unread = unread_thread_ids() | {
                    str(thread["id"])
                    for thread in threads
                    if thread.get("source") == "cursor" and thread.get("unread")
                }
                state_checked_at = now
            highlight_level = 0.5 + 0.5 * math.sin(
                now * 2 * math.pi / HIGHLIGHT_PERIOD_SECONDS
            )
            highlight_step = round(
                highlight_level * (HIGHLIGHT_STEPS - 1)
            )
            next_assignments: dict[int, dict[str, str]] = {}
            statuses: list[str] = []
            computer_control = False
            changed = False
            usage = cached_usage_limit(threads)
            calendar_event = (
                cached_calendar_event()
                if profile.calendar_key is not None
                else None
            )
            threads_by_key = dict(zip(profile.task_keys, threads))
            for key in range(1, profile.key_count + 1):
                if key == profile.usage_key:
                    signature = (
                        "usage",
                        usage.get("remainingPercent"),
                        usage.get("windowMinutes"),
                        usage.get("resetsAt"),
                        profile.key_size,
                    )
                    if previous_signatures.get(key) != signature:
                        image_path = render_usage_key(
                            key,
                            usage,
                            profile.key_size,
                        )
                        result = deck.set_key_image(key, str(image_path))
                        if result not in (None, 0):
                            raise OSError(
                                f"Key {key} image update failed with code {result}"
                            )
                        previous_signatures[key] = signature
                        changed = True
                    continue

                if key == profile.calendar_key:
                    minutes = calendar_event_minutes(calendar_event)
                    urgent = minutes is not None and 0 <= minutes <= 10
                    blink_on = urgent and int(now * 2) % 2 == 0
                    signature = (
                        "calendar",
                        calendar_event.get("title") if calendar_event else "",
                        calendar_event.get("start") if calendar_event else "",
                        calendar_event.get("url") if calendar_event else "",
                        blink_on,
                        profile.key_size,
                    )
                    if previous_signatures.get(key) != signature:
                        image_path = render_calendar_key(
                            key,
                            calendar_event,
                            blink_on,
                            profile.key_size,
                        )
                        result = deck.set_key_image(key, str(image_path))
                        if result not in (None, 0):
                            raise OSError(
                                f"Key {key} image update failed with code {result}"
                            )
                        previous_signatures[key] = signature
                        changed = True
                    continue

                action = profile.action_keys.get(key)
                if action is not None:
                    signature = ("action", action, profile.key_size)
                    if previous_signatures.get(key) != signature:
                        image_path = render_action_key(
                            key,
                            action,
                            profile.key_size,
                        )
                        result = deck.set_key_image(key, str(image_path))
                        if result not in (None, 0):
                            raise OSError(
                                f"Key {key} image update failed with code {result}"
                            )
                        previous_signatures[key] = signature
                        changed = True
                    continue

                thread = threads_by_key.get(key)
                if thread is None:
                    status = "empty"
                    source = "codex"
                    speed_bars, fast, controlling = 0, False, False
                    highlighted = False
                    signature = (
                        "",
                        "",
                        status,
                        0,
                        speed_bars,
                        fast,
                        highlighted,
                        profile.key_size,
                    )
                else:
                    thread_id = str(thread["id"])
                    source = str(thread.get("source") or "codex")
                    if source == "cursor":
                        activity = cursor_thread_activity(thread)
                    else:
                        activity_checked_at, activity = activity_cache.get(
                            thread_id,
                            (0.0, ("idle", 2, False, False)),
                        )
                        if (
                            time.monotonic() - activity_checked_at
                            >= ACTIVITY_REFRESH_SECONDS
                        ):
                            checked_at, service_tier = service_tier_cache.get(
                                thread_id, (0.0, "default")
                            )
                            if (
                                time.monotonic() - checked_at
                                >= SERVICE_TIER_REFRESH_SECONDS
                            ):
                                service_tier = load_thread_service_tier(thread_id)
                                service_tier_cache[thread_id] = (
                                    time.monotonic(),
                                    service_tier,
                                )
                            activity = thread_activity(thread, unread, service_tier)
                            activity_cache[thread_id] = (
                                time.monotonic(),
                                activity,
                            )
                    status, speed_bars, fast, controlling = activity
                    highlighted = (
                        status == "done"
                        and completion_highlighted(thread, unread)
                    )
                    statuses.append(status)
                    computer_control = computer_control or controlling
                    animation = highlight_step if highlighted else 0
                    signature = (
                        source,
                        thread_id,
                        status,
                        animation,
                        speed_bars,
                        fast,
                        highlighted,
                        profile.key_size,
                    )
                    next_assignments[key] = {
                        "source": source,
                        "id": thread_id,
                        "title": str(
                            thread.get("title") or thread.get("preview") or ""
                        ),
                        "workspace": str(thread.get("workspace_path") or ""),
                    }

                if previous_signatures.get(key) != signature:
                    image_path = render_key(
                        key,
                        thread,
                        status,
                        highlight_step / (HIGHLIGHT_STEPS - 1),
                        speed_bars,
                        fast,
                        highlighted,
                        profile.key_size,
                        source,
                    )
                    result = deck.set_key_image(key, str(image_path))
                    if result not in (None, 0):
                        raise OSError(f"Key {key} image update failed with code {result}")
                    previous_signatures[key] = signature
                    changed = True

            with led_mode_lock:
                led_mode_state["mode"] = aggregate_led_mode(statuses, computer_control)
            with assignment_lock:
                key_assignments.clear()
                key_assignments.update(next_assignments)
            if changed:
                deck.refresh()
            time.sleep(REFRESH_SECONDS)
    finally:
        led_stop.set()
        led_thread.join(timeout=1)


def main() -> None:
    mutex = acquire_single_instance()
    logger.info("AgentDeck starting")
    restore_current_thread()
    restore_selected_ide()
    start_mobile_server()
    while True:
        deck: Any | None = None
        try:
            deck = connect_device()
            if deck is None:
                time.sleep(3)
                continue
            run_connected(deck)
        except Exception:
            logger.exception("AgentDeck connection loop restarting")
            time.sleep(3)
        finally:
            if deck is not None:
                try:
                    deck.close(notify=False)
                except Exception:
                    pass
    _ = mutex


if __name__ == "__main__":
    main()
