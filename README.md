# The Office

**One screen for all your running Claude Code sessions: who needs you, what each one is doing, and a way for them to work together.**

If you run several Claude Code sessions at once, you know the problem. A dozen terminal tabs, and no idea which one is blocked on a permission, which one finished and is waiting for you, and which one is still working. And when the marketing session needs something from the dev session, you are the one copying text between windows.

The Office fixes both.

![The Office](docs/screenshot.jpg)

## What it does

**A live board.** Every session is a desk, titled with Claude Code's own session title, sorted by how much it needs you:

| | State | Meaning |
|---|---|---|
| 🔴 | blocked | needs a decision or a permission (pulses, soft chime) |
| 🟡 | waiting | finished its turn, wants your reply |
| 🟢 | working | shows the tool it is using right now |
| ⚪ | idle | went quiet |

**Click a desk, read the conversation.** What you asked, what the agent answered, which tools it used. It updates while you watch. No more switching terminals to find out where a session is.

**Agents that know about each other.** Each session is told, when it starts, that it is not alone and how to reach the others:

```
agent.py who                      who is working on what
agent.py read <agent>             read another agent's conversation
agent.py tell <agent> <message>   leave it a message
agent.py inbox --wait 300         wait for the reply
agent.py new <directory> <task>   start a new agent, in its own terminal
```

It also gets the roster itself: who each teammate is, whether it is working or idle, and the last thing you asked it. The roster is repeated only when it changes. With it come the team rules: reuse what a teammate already did instead of redoing it, ask the agent that owns an area instead of doing its work, start an agent where nobody is on it, and report back when something a teammate needs is done. You can add your own rules (which folder owns what) in a `team.md` file next to `hook.py`; see [`examples/team.md`](examples/team.md).

So the marketing agent that needs a demo page checks whether a dev agent is already on it, reads what it has done, asks it, or starts a new dev agent with the task, without you telling it what the others are doing.

A message reaches an agent that is mid-turn when that turn ends (it reads it and keeps going). An agent sitting idle at its prompt is not woken by `tell`, and `tell` says so. Recent Claude Code builds have their own `SendMessage` tool between sessions, which does wake an idle one, and the rules point agents to it.

**+ New agent.** A button on the board opens a new terminal running Claude Code in the folder you pick, started on the task you type.

Standard library only (Python 3.8+), no API key, no LLM calls. It listens on `127.0.0.1` and nothing leaves your machine.

## Install

1. Clone this repo.

2. Start the server:

   ```bash
   python office.py --allow-spawn     # http://127.0.0.1:8787
   ```

   Leave out `--allow-spawn` if you do not want the board or the agents to start new sessions.

3. Add the hook to your Claude Code settings, `~/.claude/settings.json` for every project or `.claude/settings.json` for one. Every event runs the same `hook.py`:

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

   [`examples/settings.hooks.json`](examples/settings.hooks.json) is a copy-paste starting point. On Windows use the full path to `python.exe` and escape the backslashes.

4. Open http://127.0.0.1:8787 and start a Claude Code session.

Hooks only apply to sessions started after you add them. Restart a terminal that was already open.

## How it works

- **`hook.py`** runs on each Claude Code event and posts a small payload to the server: session id, event, working directory, tool name, transcript path. It fails silent and fast, so a stopped server never slows a session down.
- **`office.py`** keeps one row per session in a SQLite file and serves the board. A session that goes silent while working turns idle after two minutes and is dropped after an hour. A clean exit removes its desk at once.
- **Titles** come from Claude Code itself. It writes a rolling title for each session into the transcript (`ai-title` entries) and the server reads the newest one.
- **Conversations** are read from the end of the session's transcript file, on your machine, only for a session whose own hook reported that file.
- **Messages** are stored until the recipient's hook collects them. At the end of a turn the hook hands the message back to Claude as a reason to continue, at most once per turn, so two agents cannot keep each other running forever. `agent.py inbox --wait` lets an agent wait for an answer inside its own turn.
- **New agents** are started with `claude "<task>"` in a new terminal window. The task is passed as one argument, never as shell text.

- **Team awareness** is text the hook prints at session start and, when the roster changed, with a prompt. Claude Code adds that output to the session's context.

One limit to know: a session that is already idle has no hook running, so a message left with `tell` waits until its next prompt (its desk shows an unread badge). Waking it takes Claude Code's own `SendMessage` tool, or a new agent.

## Privacy and safety

- The server listens on `127.0.0.1` only.
- The hook sends no prompt text unless you set `OFFICE_SEND_PROMPT=1` (first 400 characters, used as a title until Claude Code has titled the session).
- The roster shows each agent the start of the last prompt you gave its teammates, read from their transcripts on your machine. Your own sessions see each other's work; nothing else does.
- Conversations are read from disk by the local server when you, or one of your own agents, ask for them. Any program on your machine that can reach localhost can do the same, which is also true of the transcript files themselves.
- Starting new sessions is off unless you pass `--allow-spawn`.

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `--port` / `OFFICE_PORT` | `8787` | Port for the server, the hook and `agent.py`. |
| `--allow-spawn` / `OFFICE_ALLOW_SPAWN=1` | off | Allow the board and agents to start new sessions. |
| `OFFICE_DB` | `./office.db` | Where the SQLite file lives. |
| `OFFICE_CLAUDE_BIN` | found on `PATH` | The `claude` executable used for new agents. |
| `OFFICE_TERMINAL` | `x-terminal-emulator` | Terminal used for new agents on Linux. |
| `OFFICE_BRIEF=0` | on | Do not tell sessions about the other agents. |
| `OFFICE_TEAM_FILE` | `team.md` next to `hook.py` | Your own team rules, added to the briefing. |
| `OFFICE_SEND_PROMPT=1` | off | Hook forwards the start of the prompt as a fallback title. |

New agents are tested on Windows. The macOS and Linux launchers are written but have not been run yet; reports welcome.

## License

MIT, see [LICENSE](LICENSE).
