# The Office 🏢

**A live dashboard for all your running Claude Code sessions — ranked by which one needs *you* right now.**

If you run more than one Claude Code session at a time, you know the pain: a dozen terminal tabs, and no idea which one is blocked on a permission, which one finished and is waiting for you, and which one is still grinding. The Office puts them all on one screen.

Each session becomes a "desk", headlined by **Claude Code's own session title**, ranked loudest-first:

```
🔴 blocked   — needs a decision / permission     (top, pulses + soft chime)
🟡 waiting   — finished its turn, wants your reply
🟢 working   — grinding; shows the live tool it's using
💤 idle      — went quiet
```

- **Zero dependencies.** Python 3.8+ standard library only.
- **Local only.** Binds `127.0.0.1`. Nothing leaves your machine.
- **No LLM, no API key.** Titles come from Claude Code's own rolling `aiTitle`, read straight from the session transcript.
- **Agents can talk to each other.** Leave a note for another session; it lands in that session's context on its next turn.

![The Office](docs/screenshot.jpg)

---

## Install (2 minutes)

**1. Get the files** (clone this repo, or drop `office.py` + `board.html` + `hook.py` in a folder).

**2. Start the server:**

```bash
python office.py            # http://127.0.0.1:8787
# python office.py --port 9000   (or set OFFICE_PORT)
```

**3. Add the hook** to your Claude Code settings so sessions report in. Edit
`~/.claude/settings.json` (global) or a project's `.claude/settings.json` and merge in a
`hooks` block. Point every event at `hook.py` with your Python and the absolute path to
`hook.py`:

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python /ABSOLUTE/PATH/the-office/hook.py" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python /ABSOLUTE/PATH/the-office/hook.py" }] }],
    "PostToolUse":      [{ "matcher": "*", "hooks": [{ "type": "command", "command": "python /ABSOLUTE/PATH/the-office/hook.py" }] }],
    "Notification":     [{ "hooks": [{ "type": "command", "command": "python /ABSOLUTE/PATH/the-office/hook.py" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python /ABSOLUTE/PATH/the-office/hook.py" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "python /ABSOLUTE/PATH/the-office/hook.py" }] }]
  }
}
```

See [`examples/settings.hooks.json`](examples/settings.hooks.json) for a copy-paste starting point. On Windows, use the full path to `python.exe` and escape backslashes in the JSON.

**4. Open** http://127.0.0.1:8787 and start a new Claude Code session. It appears on the board.

> Hooks only fire for sessions started **after** you install them — restart any terminal that was already open.

---

## How it works

- Each Claude Code hook event runs `hook.py`, which POSTs a small JSON payload
  (`session_id`, event, `cwd`, `tool_name`, `transcript_path`, and a Notification's
  message) to `office.py`.
- `office.py` keeps one row per session in a tiny SQLite file (`office.db`) and serves
  the ranked board at `/api/agents`. The page polls it every 2s.
- **Titles:** Claude Code writes its own rolling, conversation-aware title into the
  session transcript as `{"type":"ai-title","aiTitle":...}` entries. The Office reads the
  newest one — that's the clean headline you already see in Claude Code. Until a session
  has been titled, it falls back to a light heuristic clean-up of the first prompt (only
  if you opt in with `OFFICE_SEND_PROMPT=1`), then to the working-directory name.
- A session that goes silent while "working" ages to **idle** after 2 minutes; one silent
  for an hour is dropped. A clean `SessionEnd` removes its desk immediately.

## Agents talking to each other

Click a desk → type a note → **Send**. It's addressed to that session. On the recipient's
**next turn**, its `UserPromptSubmit` hook pulls the note and prints it — and Claude Code
injects a `UserPromptSubmit` hook's stdout into the session's context, so the agent
actually *receives* the message and can act on it. A desk with unread notes shows a 📬
badge.

Agents can message each other too, not just you — any session (or script) can POST:

```bash
curl -s localhost:8787/api/messages -H "Content-Type: application/json" \
  -d '{"from_name":"api","to_name":"webapp","text":"the /leads endpoint is live"}'
```

Address by `to_session` (exact session id) or `to_name` (a desk's folder tag). Give an
agent a one-line instruction — "when you finish, tell the `webapp` agent" — and it can
leave the note itself.

## Privacy

Everything is local. The hook forwards prompts **only** if you set
`OFFICE_SEND_PROMPT=1`, and even then only the first line, only to `127.0.0.1`. Transcript
*contents* are never sent — the server reads the transcript file itself, locally, just for
the title.

## Configuration

| Env | Default | What |
|-----|---------|------|
| `OFFICE_PORT` | `8787` | Port for both the server and the hook. |
| `OFFICE_DB` | `./office.db` | SQLite file location. |
| `OFFICE_SEND_PROMPT` | *(off)* | `1` = hook forwards the first prompt line as a fallback title. |

## License

MIT — see [LICENSE](LICENSE).
