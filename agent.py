#!/usr/bin/env python3
"""agent.py — how a Claude Code agent sees and talks to the other agents in The Office.

    python agent.py who                       who is working on what, right now
    python agent.py read <agent> [n]          the last n messages of another agent's conversation
    python agent.py tell <agent> <message>    leave a message for another agent
    python agent.py tell all <message>        leave the same message for every agent on the board
    python agent.py inbox [--wait SECONDS]    messages for you (optionally wait for one)
    python agent.py new <directory> <task>    start a new agent (its own terminal) on a task
    python agent.py task list                 the shared task list
    python agent.py task add <directory> <title> [details] [--kind K] [--after T-1,T-2]
                             [--check NAME] [--to <agent>]
    python agent.py task run                  start the runner that works through the list
    python agent.py task done|blocked <id> <note>   close a task assigned to you
    python agent.py task retry <id> [better instruction] | task cancel <id>
    python agent.py manager start|stop|status the floor manager registers itself
                                              (start prints its rulebook, manager.md)
    python agent.py wait [--timeout 100]      sleep until something changes, then say what

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
    if r.get("broadcast"):
        recs = r.get("recipients") or []
        busy = [x["label"] for x in recs if x.get("mid_turn")]
        idle = [x["label"] for x in recs if not x.get("mid_turn")]
        print("Message left for %d agent(s)." % len(recs))
        if busy:
            print("Mid-turn, will read it when the current turn ends: %s."
                  % "; ".join(busy))
        if idle:
            print("IDLE at their prompt, will read it with their next prompt (this "
                  "alone does not wake them; SendMessage from ListAgents does): %s."
                  % "; ".join(idle))
        return 0
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


def _flag(args, name):
    """Pull `--name value` out of an argument list."""
    if name in args:
        i = args.index(name)
        if len(args) > i + 1:
            value = args[i + 1]
            del args[i:i + 2]
            return value
        del args[i]
    return None


def task(args):
    """The shared task list: work queued for the task runner instead of being done by
    whoever happens to be talking."""
    sub = args[0] if args else "list"
    if sub == "list":
        data = _get("/api/tasks")
        tasks = data["tasks"]
        if not tasks:
            print("The task list is empty.")
        for t in tasks:
            after = (" after %s" % ",".join("T-%d" % d for d in t["deps"])) if t["deps"] else ""
            check = (", check %s" % t["check_name"]) if t.get("check_name") else ""
            print("%s [%s] %s  (%s, attempt %d/%d%s%s)" % (
                t["ref"], t["status"], t["title"], t.get("dir") or "no folder",
                t["attempts"], t["max_attempts"], after, check))
            if t.get("summary") and t["status"] != "pending":
                print("     %s" % t["summary"][:300])
        print("The runner works on %d task(s) at a time; tasks in the same folder take "
              "turns.%s" % (data.get("parallel") or 1,
                            (" Checks you can name with --check: %s."
                             % ", ".join(data["checks"])) if data.get("checks") else ""))
        return 0
    if sub == "show" and len(args) >= 2:
        for t in _get("/api/tasks")["tasks"]:
            if t["ref"].lower() == args[1].lower() or str(t["id"]) == args[1]:
                print(json.dumps(t, indent=2, ensure_ascii=False))
                return 0
        print("error: no such task")
        return 1
    if sub == "add" and len(args) >= 3:
        rest = list(args[1:])
        kind = _flag(rest, "--kind")
        after = _flag(rest, "--after")
        to = _flag(rest, "--to")
        check = _flag(rest, "--check")
        if len(rest) < 2:
            print("usage: task add <directory> <title> [details] [--kind K] "
                  "[--after T-1,T-2] [--check NAME] [--to <agent>]")
            return 2
        r = _post("/api/tasks", {
            "dir": os.path.abspath(rest[0]), "title": rest[1],
            "body": " ".join(rest[2:]), "kind": kind, "to": to, "check": check,
            "deps": [d for d in (after or "").split(",") if d.strip()],
            "created_by": ME or "an agent"})
        if not r.get("ok"):
            print("error: %s" % r.get("error"))
            return 1
        rec = r.get("recipient")
        if not rec:
            print("Queued as %s for the task runner (`task run` starts it)."
                  % r["task"]["ref"])
        elif rec.get("mid_turn"):
            print("%s assigned to %s. It is mid-turn and gets the task when that turn "
                  "ends." % (r["task"]["ref"], rec.get("label")))
        else:
            print("%s assigned to %s, but it is IDLE: it will not see the task until "
                  "it is woken. Wake it with your built-in SendMessage tool (name in "
                  "ListAgents), telling it to run `inbox`."
                  % (r["task"]["ref"], rec.get("label")))
        return 0
    if sub in ("done", "blocked", "cancel", "retry") and len(args) >= 2:
        note = " ".join(args[2:]).strip()
        body = {"id": args[1]}
        if sub == "retry":
            body["retry"] = True
            if note:
                body["body_append"] = "Added on retry: " + note
        else:
            body["status"] = {"done": "done", "blocked": "blocked",
                              "cancel": "cancelled"}[sub]
            if note:
                body["summary"] = note
        r = _post("/api/tasks/update", body)
        if not r.get("ok"):
            print("error: %s" % r.get("error"))
            return 1
        print("%s is now %s." % (r["task"]["ref"], r["task"]["status"]))
        return 0
    if sub == "run":
        r = _post("/api/tasks/run", {})
        if not r.get("ok"):
            print("error: %s" % r.get("error"))
            return 1
        print("The runner was already going." if r.get("already_running")
              else "Runner started.")
        return 0
    print("usage: task list | task show <id> | task add <directory> <title> [details] "
          "[--kind K] [--after T-1,T-2] [--check NAME] [--to <agent>] | task run | "
          "task done <id> <summary> | task blocked <id> <why> | "
          "task retry <id> [better instruction] | task cancel <id>")
    return 2


def _rulebook():
    """manager.md, next to this file. It is printed rather than left for the manager to
    open: a session may not read a file outside its own project without asking its
    operator, and a manager that starts by waiting for a click is not managing."""
    path = os.environ.get("OFFICE_MANAGER_FILE") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "manager.md")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        return "(the rulebook %s could not be read: %s)" % (path, e)


def manager(args):
    """Register (or step down as) the floor manager."""
    action = args[0] if args else "status"
    if action == "rules":
        print(_rulebook())
        return 0
    if action not in ("start", "stop", "status"):
        print("usage: manager start | manager stop | manager status | manager rules")
        return 2
    r = _post("/api/manager", {"action": action, "session_id": ME})
    if not r.get("ok"):
        print("error: %s" % r.get("error"))
        return 1
    if action == "start":
        print("You are registered as the floor manager. The other agents are told.")
        print("Your rulebook follows. Follow it exactly.\n")
        print(_rulebook())
    elif action == "stop":
        print("No floor manager is registered now.")
    else:
        print("Floor manager: %s" % (r.get("label") or "none"))
    return 0


def _snapshot():
    """What a manager watches: every task's status, every other agent's state."""
    tasks = {str(t["id"]): {"status": t["status"], "title": t["title"],
                            "summary": t.get("summary") or ""}
             for t in _get("/api/tasks")["tasks"]}
    agents = {a["session_id"]: {"state": a["state"],
                                "label": "[%s] %s" % (a["name"], a.get("task") or "(untitled)"),
                                "activity": a.get("activity") or ""}
              for a in _get("/api/agents")["agents"]
              if a["session_id"] != ME and not a["session_id"].startswith("task-")}
    return {"tasks": tasks, "agents": agents}


def _changes(old, new):
    """Lines describing what happened between two snapshots."""
    out = []
    for tid, t in new["tasks"].items():
        before = old["tasks"].get(tid)
        if before is None:
            line = "new task T-%s [%s] %s" % (tid, t["status"], t["title"])
        elif before["status"] != t["status"]:
            line = "T-%s %s: %s -> %s" % (tid, t["title"], before["status"], t["status"])
        else:
            continue
        if t["status"] in ("done", "blocked", "failed") and t["summary"]:
            line += " | " + t["summary"][:400]
        out.append(line)
    for sid, a in new["agents"].items():
        before = old["agents"].get(sid)
        if before is None:
            out.append("agent arrived: %s" % a["label"])
        elif before["state"] != a["state"] and a["state"] in ("waiting", "blocked", "idle"):
            what = {"waiting": "finished its turn and is idle at its prompt",
                    "blocked": "is waiting on the operator for a permission",
                    "idle": "went quiet"}[a["state"]]
            out.append("%s %s" % (a["label"], what))
    for sid, a in old["agents"].items():
        if sid not in new["agents"]:
            out.append("agent left: %s" % a["label"])
    return out


def wait(timeout=100):
    """Sleep until something changes in the office, then say what: a task changed
    status, an agent finished its turn or needs the operator, an agent arrived or left,
    a message came for you. This is how a manager stays on duty without burning turns.
    The last state you saw is remembered between calls, so nothing is missed while you
    were busy."""
    import tempfile
    state_file = os.path.join(tempfile.gettempdir(),
                              "the-office-wait-%s.json" % (ME[:12] or "anon"))
    try:
        with open(state_file, encoding="utf-8") as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = None
    deadline = time.time() + max(0, int(timeout))
    while True:
        now = _snapshot()
        lines = _changes(seen, now) if seen else []
        msgs = []
        if ME:
            msgs = _post("/api/messages/pending", {"session_id": ME}).get("messages") or []
        if seen is None or lines or msgs:
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump(now, f)
            if seen is None:
                print("Watching from now on: %d task(s), %d other agent(s). Call `wait` "
                      "again to sleep until something changes."
                      % (len(now["tasks"]), len(now["agents"])))
            for line in lines:
                print("- " + line)
            for m in msgs:
                print("- message from %s: %s" % (m.get("from_name") or "another agent",
                                                 m.get("text") or ""))
            return 0
        if time.time() >= deadline:
            print("Nothing changed in %ds." % int(timeout))
            return 0
        time.sleep(4)


def new(directory, task):
    r = _post("/api/spawn", {"cwd": os.path.abspath(directory), "task": task})
    if not r.get("ok"):
        print("error: %s" % r.get("error"))
        return 1
    if r.get("area"):
        print("New agent started in %s (the folder whose sessions report to The Office), "
              "with %s as its area." % (r.get("cwd"), r["area"]))
    else:
        print("New agent started in %s." % r.get("cwd"))
    if r.get("warning"):
        print("WARNING: %s" % r["warning"])
    else:
        print("It appears in `who` within about twenty seconds. If it does not, its "
              "terminal is waiting on the operator (Claude Code asks once before it "
              "works in a folder it has never been started in): tell the operator "
              "which window, and do not start a second one.")
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
        if cmd == "task":
            return task(argv[2:])
        if cmd == "manager":
            return manager(argv[2:])
        if cmd == "wait":
            rest = list(argv[2:])
            t = _flag(rest, "--timeout")
            return wait(int(t) if t and t.isdigit() else 100)
        if cmd == "inbox":
            secs = _flag(list(argv[2:]), "--wait")
            return inbox(int(secs) if secs and secs.isdigit() else 0)
    except OSError as e:
        print("The Office is not reachable at %s (%s). Is office.py running?"
              % (BASE, e))
        return 1
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
