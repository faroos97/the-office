#!/usr/bin/env python3
"""The Office — Claude Code hook forwarder.

Claude Code runs this on every session event and pipes a JSON blob on stdin. We forward
the bits the board needs to POST /api/agents/report on the local Office server.

Wire it in your Claude Code settings (see README.md) for the events:
  SessionStart, UserPromptSubmit, PostToolUse, Notification, Stop, SessionEnd

Design: standard library only, FAIL SILENT and FAST (a hook must never slow down or
crash a session), and PII-safe by default — it forwards the event, cwd, tool name, the
transcript path (so the board can read Claude Code's own session title), and for a
Notification the message. It does NOT forward prompts or transcript contents unless you
set OFFICE_SEND_PROMPT=1 (a local-only convenience for the fallback title).

Config via env: OFFICE_PORT (default 8787), OFFICE_SEND_PROMPT (default off).
"""
import json
import os
import sys
import urllib.request

TIMEOUT = 0.6


def main():
    try:
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
        pass  # server not running / busy — never block the session

    # On a new user turn, pull any notes other agents left for this session and print
    # them: Claude Code injects a UserPromptSubmit hook's stdout into the session's
    # context, so the agent actually "receives" the message.
    if event == "UserPromptSubmit":
        name = ""
        cwd = (data.get("cwd") or "").replace("\\", "/").rstrip("/")
        if cwd:
            name = cwd.rsplit("/", 1)[-1]
        try:
            raw = _post("/api/messages/pending",
                        {"session_id": session_id, "name": name})
            msgs = (json.loads(raw.decode("utf-8")) or {}).get("messages") or []
        except Exception:
            msgs = []
        if msgs:
            lines = ["[The Office] You have %d message(s) from other agents:"
                     % len(msgs)]
            for m in msgs:
                lines.append("  • from %s: %s"
                             % (m.get("from_name") or "an agent", m.get("text") or ""))
            sys.stdout.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
