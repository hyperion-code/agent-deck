# Codex AgentDeck

This local bridge shows both ChatGPT (Codex) tasks and Cursor agent chats on
the deck, merged into one list ordered by latest activity. It automatically
detects either supported VSDinside StreamDock:

- M18 at USB `5548:1000`: 15 LCD keys in a 5×3 grid using 64×64 images.
- XL at USB `5548:1034`: 32 LCD keys in an 8×4 grid using 80×80 images.

The connected model controls the task capacity, grid mapping, native image
resolution, and text scaling. The newest task remains at the physical top-left,
moving left-to-right and then down.

## Behavior

- Light-blue key: an agent is working.
- The upper-left badge shows the task's source. Codex tasks show the speed
  badge for the reasoning setting; more bars means less deliberation. A
  lightning bolt appears only when Codex's 1.5x Fast service tier is enabled
  for that task. Cursor chats show a small `>` terminal badge instead.
- Red key: the agent needs user input.
- Green key: the agent is done.
- A newly completed green key keeps a light-blue square outline until it is
  opened for the first time. The outline eases through a four-second
  sinusoidal pulse with 81 brightness levels.
- Gray key: idle.
- The bottom-right LCD is reserved for the remaining Codex usage percentage.
  The small label beneath it shows the numeric reset date. Its ring is cyan
  normally, amber below 40%, and red at 20% or lower.
- Press a key to open that task. Codex tasks open through their
  `codex://threads/<id>` deep link. Cursor chats are brought forward through
  UI automation: an existing chat tab is selected when visible; otherwise the
  Agents Window is opened and that chat is clicked there (including chats from
  other workspaces). If Cursor is not running, it is launched and the Agents
  Window path is retried.
- Press the lower-left round button to spawn a new agent in whichever IDE was
  selected most recently. The selection follows the foreground window
  (ChatGPT or Cursor) and deck presses, persists in `last-ide.txt`, and
  defaults to Codex. A new Cursor agent is created with Cursor's own
  New Agent control.
- Press the center round button to start native voice input in the most
  recently selected IDE. For Codex that is Dictate / Transcribe and send. For
  Cursor that is Start voice input / Send voice input (with Ctrl+M as a
  fallback). Press again to finish and send.
- Press the lower-right round button to archive the active chat in the most
  recently selected IDE. For Codex it archives the viewed/selected thread. For
  Cursor it archives the last opened Cursor chat (via Cursor's UI when
  possible, and always in Cursor's local state so it disappears from the deck).
- On the XL, which has no round controls, the three keys in the rightmost
  column above the usage indicator replace them: new task, microphone, and
  archive from top to bottom. The M18 controls are unchanged.
- The first six keys in the XL's bottom row are quick launchers for SolidWorks,
  Altium, Slack, ChatGPT, Cursor, and Gmail, in that order. A launcher focuses
  an existing app window instead of starting another instance. Gmail opens in
  an already-open Gmail tab; it creates a Gmail tab only when none exists.
- The seventh XL bottom-row key shows the next Google Calendar event. It blinks
  during the final ten minutes before the event and opens its meeting/event
  link when pressed. Its private iCal URL is stored locally in
  `calendar-feed-url.txt`, which is intentionally excluded from Git.
- The eighth XL bottom-row key is the usage meter.
- The chassis lights blink red when any visible task needs approval, turn light
  blue while an agent controls the computer, dark blue while any agent is
  solving, solid green when every visible task is done, and dim navy otherwise.
- Key-display brightness is set to the device maximum.

The bridge reads only local state: Codex threads under `%USERPROFILE%\.codex`
and Cursor chat headers from
`%APPDATA%\Cursor\User\globalStorage\state.vscdb` (read-only). A Cursor chat
counts as working while its conversation checkpoint keeps advancing without a
completed status, needs attention while it has blocking pending actions, and
is done once completed or unread.

Only tasks updated within the last 24 hours are shown; unused cubes stay blank.
The M18 provides 14 task cubes plus its usage meter. The XL provides 21 task
cubes, three right-column controls, six bottom-row app launchers, a calendar
key, and its bottom-right usage meter. Tasks
are ordered by latest activity from the top-left, moving left-to-right and then
down each row while skipping reserved controls. Each press is bound directly to
the hardware key that received that task's image.

Pressing a Codex task cube once opens its task. Pressing that same cube again
within six seconds brings forward the Chrome tab that task controlled during
the previous 30 minutes, when that tab is still open. Chrome's existing
maximized, full-screen, or windowed state is preserved.

## Private configuration

Machine- or workplace-specific task aliases and site labels belong in
`agent-deck-private.json`, outside this public repository. AgentDeck checks the
path in `AGENT_DECK_PRIVATE_CONFIG` first, then sibling checkouts named
`AgentDeckPrivateConfig` or `agent-deck-private-config`, and finally an ignored
file beside `agent_deck.py`.

The optional file has this shape:

```json
{
  "name_overrides": {
    "<thread-id>": "<display name>"
  },
  "site_hints": {
    "internal.example.com": "Example"
  }
}
```

AgentDeck retains generic task names and browser hints when the private
configuration is unavailable or invalid.

The `5548:1034` XL firmware reports pressed LCD rows opposite to its displayed
rows, so AgentDeck corrects the input Y axis only for the XL. The M18 uses
direct input mapping.

## Install

AgentDeck requires Windows, the Codex desktop app, Python 3.11 or newer, and
the included StreamDock transport library.

```powershell
python -m pip install -r requirements.txt
python agent_deck.py
```

To start it automatically at sign-in:

```powershell
.\install-startup.ps1
```

It starts at Windows login from the `Codex AgentDeck` per-user Run entry.
Logs are kept in `agent_deck.log`.

To remove automatic startup:

```powershell
.\uninstall-startup.ps1
```

## Android app

The native Android client in `android/` mirrors the M18 layout and connects to
the authenticated mobile API built into AgentDeck. It shows the same task
states, speed badge, Fast bolt, usage meter, and ambient status colors.

- Tap a task to select it and open it in Codex on the PC.
- Tap the microphone once to start Android speech recognition and again to
  transcribe and submit the result to the selected task.
- Use the round buttons to open a new Codex task or archive the selected task.

The Android client automatically tries both the configured home-LAN and
private-network addresses. Tailscale is only needed when the phone is away
from the PC's Wi-Fi. AgentDeck listens on TCP port `8765`; no public internet
port is opened. The local
`agent-deck-mobile.json` file contains the generated bearer token and is
excluded from Git.

Start the PC bridge once so it generates `agent-deck-mobile.json`, then provide
the PC addresses as environment variables and build the configured debug APK:

```powershell
cd android
$env:AGENT_DECK_LAN_URL = "http://<PC-LAN-IP>:8765"
$env:AGENT_DECK_URL = "http://<PC-TAILSCALE-IP>:8765"
.\gradlew.bat assembleDebug
```

The bearer token is read automatically from the generated configuration file.
For a Wi-Fi-only build, omit `AGENT_DECK_URL`. Never commit
`agent-deck-mobile.json`, `android/local.properties`, or a configured APK:
they contain machine-specific or private connection details.

The configured APK is written to
`android\app\build\outputs\apk\debug\app-debug.apk`.
