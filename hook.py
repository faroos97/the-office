#!/usr/bin/env python3
"""The Office — Claude Code hook.

Claude Code runs this on every session event and pipes a JSON blob on stdin. It does
three things:

  1. Reports the event to office.py so the board stays live.
  2. SessionStart: tells the agent it is not alone and how to reach the other agents
     (agent.py). Claude Code adds a SessionStart hook's stdout to the session context.
  3. Delivers messages other agents left for this session:
       - UserPromptSubmit: printed, so they land in context with the new prompt;
       - Stop: returned as a "block" decision, so the agent reads the message and keeps
         going instead of going idle. Only once per turn (never when Claude is already
         continuing because of a stop hook), so two agents cannot ping-pong forever.

Wire it for: SessionStart, UserPromptSubmit, PostToolUse, Notification, Stop, SessionEnd
(see README.md). Standard library only. Fails silent and fast: a hook must never slow
down or break a session.

Privacy: forwards the event, cwd, tool name, transcript path, and a Notification's
message. Prompts are forwarded only with OFFICE_SEND_PROMPT=1 (first 400 chars, to
localhost, as a fallback title).

Env: OFFICE_PORT (8787) · OFFICE_SEND_PROMPT (off) · OFFICE_BRIEF=0 to skip step 2.
"""
import json
import os
import sys
import urllib.request

TIMEOUT = 0.6


def main():
    try:
        # bytes -> UTF-8 explicitly: on Windows sys.stdin defaults to the ANSI code page
        # and mangles accented characters
        raw = sys.stdin.buffer.read().decode("utf-8", "replace")
    except Exception:
        return
    if not raw:
        return
    try:
        data = json.loads(raw)
    except Exception:
        return

    session_id = data.get("session_id")
    if not session_id:
        return
    event = data.get("hook_event_name") or (sys.argv[1] if len(sys.argv) > 1 else "")

    payload = {
        "session_id": session_id,
        "hook_event_name": event,
        "cwd": data.get("cwd"),
        "tool_name": data.get("tool_name"),
        "transcript_path": data.get("transcript_path"),
    }
    if event == "Notification":
        payload["message"] = data.get("message")
    if event == "UserPromptSubmit" and os.environ.get("OFFICE_SEND_PROMPT") == "1":
        p = data.get("prompt")
        if p:
            payload["prompt"] = str(p)[:400]

    port = os.environ.get("OFFICE_PORT", "8787")
    if not str(port).isdigit():
        port = "8787"
    base = "http://127.0.0.1:%s" % port

    def _post(path, obj):
        req = urllib.request.Request(
            base + path, data=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        return urllib.request.urlopen(req, timeout=TIMEOUT).read()

    try:
        _post("/api/agents/report", payload)
    except Exception:
        return  # server not running: nothing else to do, never block the session

    out = sys.stdout.buffer

    if event == "SessionStart" and os.environ.get("OFFICE_BRIEF") != "0":
        here = os.path.dirname(os.path.abspath(__file__))
        tool = '"%s" "%s"' % (sys.executable.replace("\\", "/"),
                              os.path.join(here, "agent.py").replace("\\", "/"))
        brief = (
            "[The Office] You are one of several Claude Code agents working for the "
            "same person, each in its own terminal. To see and reach the others, run "
            "(in PowerShell, prefix the line with &):\n"
            "  %s <command>\n"
            "Commands: who (who is working on what) | read <agent> (another agent's "
            "conversation) | tell <agent> <message> | inbox --wait 300 (wait for a "
            "reply) | new <directory> <task> (start a new agent in its own terminal).\n"
            "<agent> = its folder name or a few words of its title. Use this when "
            "another agent has, or should produce, something you need, rather than "
            "asking the user to relay between terminals.\n" % tool)
        out.write(brief.encode("utf-8"))
        return

    if event not in ("UserPromptSubmit", "Stop"):
        return
    if event == "Stop" and data.get("stop_hook_active"):
        return  # already continuing because of a stop hook: do not chain

    try:
        raw = _post("/api/messages/pending", {"session_id": session_id})
        msgs = (json.loads(raw.decode("utf-8")) or {}).get("messages") or []
    except Exception:
        msgs = []
    if not msgs:
        return
    lines = ["[The Office] %d message(s) from other agents:" % len(msgs)]
    for m in msgs:
        lines.append("- from %s: %s" % (m.get("from_name") or "another agent",
                                        m.get("text") or ""))
    text = "\n".join(lines)
    if event == "Stop":
        text += ("\nRead it and act on it if it concerns your work; reply with the "
                 "agent.py tool if an answer is expected.")
        out.write(json.dumps({"decision": "block", "reason": text}).encode("utf-8"))
    else:
        out.write((text + "\n").encode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
