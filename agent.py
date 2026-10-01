#!/usr/bin/env python3
"""agent.py — how a Claude Code agent sees and talks to the other agents in The Office.

    python agent.py who                       who is working on what, right now
    python agent.py read <agent> [n]          the last n messages of another agent's conversation
    python agent.py tell <agent> <message>    leave a message for another agent
    python agent.py inbox [--wait SECONDS]    messages for you (optionally wait for one)
    python agent.py new <directory> <task>    start a new agent (its own terminal) on a task

<agent> is loose: its folder name, a few words of its title, or the start of its session
id. A message reaches an agent that is mid-turn when that turn ends; an agent sitting
idle at its prompt only sees it with its next prompt, and `tell` says which case it is.
Standard library only; talks to office.py on localhost.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:%s" % (os.environ.get("OFFICE_PORT") or "8787")
ME = os.environ.get("CLAUDE_CODE_SESSION_ID") or ""


def _get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(path, obj):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def _age(s):
    s = int(s or 0)
    return "%ds" % s if s < 60 else "%dm" % (s // 60) if s < 3600 else "%dh" % (s // 3600)


def who():
    agents = _get("/api/agents")["agents"]
    if not agents:
        print("No agents are reporting to The Office right now.")
        return
    print("%d agent(s) in The Office:" % len(agents))
    for a in agents:
        you = "  <- you" if ME and a["session_id"] == ME else ""
        print("- [%s] %s%s" % (a["name"], a.get("task") or "(untitled)", you))
        print("    %s, %s ago: %s   id %s" % (
            a["state"], _age(a["age_secs"]), a.get("activity") or "-",
            a["session_id"][:8]))


def read(target, n=12):
    c = _get("/api/conversation?target=%s&limit=%d"
             % (urllib.parse.quote(target), int(n)))
    if not c.get("ok"):
        print("error: %s" % c.get("error"))
        return 1
    print("Conversation of [%s] %s  (%s)" % (
        c.get("name"), c.get("title") or "(untitled)", c.get("state")))
    if c.get("note"):
        print("  (%s)" % c["note"])
    for m in c["messages"]:
        who_ = "USER" if m["role"] == "user" else "AGENT"
        tools = ("  [tools: %s]" % ", ".join(m["tools"][:8])) if m.get("tools") else ""
        print("\n%s:%s" % (who_, tools))
        if m.get("text"):
            print(m["text"])
    return 0


def tell(target, text):
    payload = {"to": target, "text": text}
    if ME:
        payload["from_session"] = ME
    else:
        payload["from_cwd"] = os.getcwd()
    r = _post("/api/messages", payload)
    if not r.get("ok"):
        print("error: %s" % r.get("error"))
        return 1
    rec = r.get("recipient")
    if not rec:
        print("Message left. That agent is not on the board right now; it gets the "
              "message if it comes back.")
    elif rec.get("mid_turn"):
        print("Message left for %s. It is mid-turn and will read it when that turn "
              "ends. Use `inbox --wait 300` if you need its answer to continue."
              % rec.get("label"))
    else:
        print("Message left for %s, but it is IDLE at its prompt: this alone will not "
              "wake it. To wake it now, send it the same request with your built-in "
              "SendMessage tool (its name is in ListAgents and matches its title). "
              "Without those tools, start a new agent with `new \"%s\" <task>` "
              "instead of waiting."
              % (rec.get("label"), rec.get("cwd") or "<its directory>"))
    return 0


def inbox(wait=0):
    """Print messages addressed to this session; with --wait N, block up to N seconds
    for one to arrive (use it right after `tell` when you need the answer to go on)."""
    if not ME:
        print("error: CLAUDE_CODE_SESSION_ID is not set, so I cannot tell which "
              "session is asking.")
        return 1
    deadline = time.time() + max(0, int(wait))
    while True:
        msgs = _post("/api/messages/pending", {"session_id": ME}).get("messages") or []
        if msgs:
            for m in msgs:
                print("- from %s: %s" % (m.get("from_name") or "another agent",
                                         m.get("text") or ""))
            return 0
        if time.time() >= deadline:
            print("No messages.")
            return 0
        time.sleep(3)


def new(directory, task):
    r = _post("/api/spawn", {"cwd": os.path.abspath(directory), "task": task})
    if not r.get("ok"):
        print("error: %s" % r.get("error"))
        return 1
    print("New agent started in %s. It will appear in `agent.py who` within a few "
          "seconds." % r.get("cwd"))
    return 0


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    cmd = argv[1] if len(argv) > 1 else ""
    try:
        if cmd == "who":
            return who() or 0
        if cmd == "read" and len(argv) >= 3:
            n = int(argv[3]) if len(argv) > 3 and argv[3].isdigit() else 12
            return read(argv[2], n)
        if cmd == "tell" and len(argv) >= 4:
            return tell(argv[2], " ".join(argv[3:]))
        if cmd == "new" and len(argv) >= 4:
            return new(argv[2], " ".join(argv[3:]))
        if cmd == "inbox":
            wait = 0
            if "--wait" in argv:
                i = argv.index("--wait")
                if len(argv) > i + 1 and argv[i + 1].isdigit():
                    wait = int(argv[i + 1])
            return inbox(wait)
    except OSError as e:
        print("The Office is not reachable at %s (%s). Is office.py running?"
              % (BASE, e))
        return 1
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
