#!/usr/bin/env python3
"""The Office — a live dashboard for your running Claude Code sessions.

One glanceable screen that shows every Claude Code session you have open, ranked by
how much it needs YOU right now:

    blocked (needs a decision)  ->  waiting (finished, wants your reply)
        ->  working  ->  idle

Each desk is headlined by Claude Code's own rolling session title, so you can tell at
a glance which window is doing what — instead of hopping across a dozen terminals.

Zero dependencies (Python 3.8+ standard library only). Runs entirely on localhost.
Fed by Claude Code hooks that POST here on every session event — see README.md.

    python office.py            # serve on http://127.0.0.1:8787
    python office.py --port 9000
    OFFICE_PORT=9000 python office.py

Then open http://127.0.0.1:8787 and add the hook (README) so sessions report in.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"           # localhost only, always
DEFAULT_PORT = 8787
DB_PATH = os.environ.get("OFFICE_DB") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "office.db")

TASK_MAX = 48                # headline length cap
STALE_WORKING_SECS = 120     # a silent 'working' desk ages to idle after this
GHOST_SECS = 60 * 60         # a desk silent this long is reaped entirely
RECENT_CAP = 12              # activity lines kept per desk for the detail view

# ---------------------------------------------------------------- presence model
_RANK = {"blocked": 0, "waiting": 1, "working": 2, "idle": 3, "done": 4, "offline": 5}

# Claude Code hook event -> (state, needs_you). Notification is the "needs you" event
# (permission / idle prompt); Stop means the turn finished and it wants your reply.
_EVENT_MAP = {
    "SessionStart":     ("working", False),
    "UserPromptSubmit": ("working", False),
    "PreToolUse":       ("working", False),
    "PostToolUse":      ("working", False),
    "Notification":     ("blocked", True),
    "Stop":             ("waiting", True),
    "SubagentStop":     ("working", False),
    "SessionEnd":       ("offline", False),
}
# Events at which we refresh the headline from Claude Code's own title (not PostToolUse,
# which fires constantly and would re-read the transcript for no gain).
_TITLE_REFRESH_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop",
                         "SubagentStop", "Notification")

# ---------------------------------------------------------------- title cleaning
# Leading conversational filler to peel off a raw prompt so a fallback headline reads
# like a task ("Fix the login bug") not the raw ramble ("ok so um can you fix..."). EN
# + FR (extend for your language). Only used until Claude Code has titled the session.
_LEAD_FILLER = [
    "to be honest", "i want you to", "i need you to", "i'd like you to",
    "can you please", "could you please", "i want to", "i need to", "we need to",
    "we should", "what's up", "can you", "could you", "let's", "lets", "please",
    "hello", "hi", "okay", "ok", "so", "um", "uh", "hmm", "yeah", "yes", "well",
    "hey", "look", "alright", "honestly", "basically", "actually", "just", "like",
    "maybe", "now", "then", "and", "but", "i want", "i need",
    "est-ce que tu peux", "je veux que tu", "il faut que tu", "il faut", "peux-tu",
    "s'il te plait", "du coup", "en fait", "ou sinon", "wesh", "bon", "bah", "ben",
    "alors", "sinon", "genre", "voila", "euh", "heu", "donc", "franchement",
]
_TRAIL_FILLER = {"yes", "please", "pls", "thanks", "ok", "okay", "yeah", "too",
                 "now", "know", "you", "really", "quoi", "hein", "euh", "heu",
                 "voila", "stp", "svp"}
_INTERJECTIONS = re.compile(r"\b(?:u+m+|u+h+|e+u+h|h+e+u|h+m+|erm|ah|eh)\b\s*,?\s*",
                            re.IGNORECASE)


def titleize(prompt):
    """Best-effort short headline from a raw (often voice-to-text) prompt. No LLM."""
    if not prompt:
        return None
    text = ""
    for line in str(prompt).splitlines():
        line = " ".join(line.split())
        if line:
            text = line
            break
    if not text:
        return None
    text = _INTERJECTIONS.sub(" ", text)
    text = re.sub(r"\s+,", ",", re.sub(r",\s*,", ",", text))
    text = " ".join(text.split())
    # collapse consecutive duplicate words (stutter)
    out, prev = [], None
    for w in text.split():
        k = w.lower().strip(",.;:!?-")
        if k and k == prev:
            continue
        out.append(w)
        prev = k
    text = " ".join(out)
    # peel leading filler
    changed = True
    while changed:
        changed = False
        low = text.lower()
        for f in _LEAD_FILLER:
            if low == f:
                continue
            m = re.match(r"^" + re.escape(f) + r"\b[\s,.:;!?-]*", low)
            if m and len(text) > m.end():
                text = text[m.end():]
                changed = True
                break
    m = re.search(r"[.!?]", text)
    if m and m.start() >= 6:
        text = text[:m.start()]
    if len(text) > TASK_MAX:
        c = text.rfind(",", 0, TASK_MAX)
        if c >= 12:
            text = text[:c]
    words = text.strip(" ,.;:!?-").split()
    while words and words[-1].lower().strip(",.;:!?-") in _TRAIL_FILLER:
        words.pop()
    text = " ".join(words).strip(" ,.;:!?-")
    if not text:
        return None
    if len(text) > TASK_MAX:
        text = text[:TASK_MAX].rstrip() + "…"
    return text[0].upper() + text[1:]


def aititle_from_transcript(path):
    """Claude Code writes its OWN rolling, conversation-aware session title into the
    transcript as JSONL entries {"type":"ai-title","aiTitle":...}. That is the clean
    headline you see in Claude Code, so we prefer it. Reverse-scan for the newest one;
    a cheap substring pre-filter means we parse only the title lines near the end."""
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return None
    for ln in reversed(lines):
        if '"aiTitle"' not in ln:
            continue
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        if o.get("type") == "ai-title" and o.get("aiTitle"):
            t = " ".join(str(o["aiTitle"]).split())
            return t[:TASK_MAX] + ("…" if len(t) > TASK_MAX else "")
    return None


# ---------------------------------------------------------------- store (sqlite)
_DDL = """
CREATE TABLE IF NOT EXISTS agents (
  session_id TEXT PRIMARY KEY,
  name TEXT, task TEXT, cwd TEXT,
  state TEXT NOT NULL DEFAULT 'working',
  activity TEXT, last_tool TEXT, model TEXT,
  needs_you INTEGER NOT NULL DEFAULT 0,
  recent TEXT,
  started_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_agents_updated ON agents(updated_at);

-- Inter-agent messages. A note is addressed to a session id (exact) or a name
-- (folder tag). It is 'delivered' once the recipient's next UserPromptSubmit hook has
-- pulled it and injected it into that session's context.
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_session TEXT, from_name TEXT,
  to_session TEXT, to_name TEXT,
  text TEXT NOT NULL,
  ts TEXT DEFAULT (datetime('now')),
  delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_msg_pending ON messages(delivered);
"""


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    return conn


def _friendly_name(payload):
    name = (payload.get("name") or "").strip()
    if name:
        return name[:40]
    cwd = (payload.get("cwd") or "").strip().replace("\\", "/").rstrip("/")
    if cwd:
        base = cwd.rsplit("/", 1)[-1]
        if base:
            return base[:40]
    return None


def _push_recent(existing, line):
    if not line:
        return existing
    try:
        items = json.loads(existing) if existing else []
        if not isinstance(items, list):
            items = []
    except (ValueError, TypeError):
        items = []
    items.insert(0, line)
    return json.dumps(items[:RECENT_CAP], ensure_ascii=False)


def report(payload):
    """Ingest one Claude Code hook event and upsert the session's desk."""
    session_id = (payload.get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "missing session_id"}
    event = payload.get("hook_event_name") or payload.get("event") or ""

    conn = _db()
    try:
        if event == "SessionEnd":
            with conn:
                conn.execute("DELETE FROM agents WHERE session_id=?", (session_id,))
            return {"ok": True, "removed": session_id}

        state, needs_you = _EVENT_MAP.get(event, ("working", False))
        tool = payload.get("tool_name")

        task = None
        if event in _TITLE_REFRESH_EVENTS:
            task = aititle_from_transcript(payload.get("transcript_path"))
        if not task and (payload.get("task") or
                         event in ("UserPromptSubmit", "SessionStart")):
            task = payload.get("task") or titleize(payload.get("prompt"))

        if event == "Notification":
            activity = (payload.get("message") or "needs your attention").strip()[:160]
        elif event == "Stop":
            activity = "finished — waiting for you"
        elif tool:
            activity = "using %s" % tool
        elif event == "SessionStart":
            activity = "session started"
        elif event == "UserPromptSubmit":
            activity = "new request"
        else:
            activity = None

        recent_line = None
        if activity:
            recent_line = "%s  %s" % (
                datetime.datetime.now().strftime("%H:%M:%S"), activity)

        fields = {"name": _friendly_name(payload), "task": task,
                  "cwd": payload.get("cwd"), "state": state,
                  "needs_you": 1 if needs_you else 0, "activity": activity,
                  "last_tool": tool, "model": payload.get("model")}
        fields = {k: v for k, v in fields.items() if v is not None}

        with conn:
            row = conn.execute("SELECT recent FROM agents WHERE session_id=?",
                               (session_id,)).fetchone()
            if recent_line is not None:
                fields["recent"] = _push_recent(row["recent"] if row else None,
                                                recent_line)
            if row is None:
                cols = ["session_id"] + list(fields)
                conn.execute("INSERT INTO agents (%s) VALUES (%s)" % (
                    ",".join(cols), ",".join("?" * len(cols))),
                    tuple([session_id] + [fields[k] for k in fields]))
            else:
                sets = ", ".join("%s=?" % k for k in fields)
                sets = (sets + ", " if sets else "") + "updated_at=datetime('now')"
                conn.execute("UPDATE agents SET %s WHERE session_id=?" % sets,
                             tuple(fields[k] for k in fields) + (session_id,))
        return {"ok": True}
    finally:
        conn.close()


def board():
    """The whole office, aged and ranked (loudest first)."""
    conn = _db()
    try:
        with conn:
            conn.execute("DELETE FROM agents WHERE"
                         " (julianday('now')-julianday(updated_at))*86400 > ?",
                         (GHOST_SECS,))
        rows = conn.execute(
            "SELECT *, CAST((julianday('now')-julianday(updated_at))*86400 AS INTEGER)"
            " AS age_secs FROM agents ORDER BY updated_at DESC").fetchall()
    finally:
        conn.close()

    agents = []
    for r in rows:
        r = dict(r)
        age = r.get("age_secs") or 0
        state = r.get("state") or "working"
        if state == "working" and age > STALE_WORKING_SECS:
            state, r["needs_you"] = "idle", 0
        try:
            recent = json.loads(r.get("recent") or "[]")
        except (ValueError, TypeError):
            recent = []
        agents.append({
            "session_id": r["session_id"], "name": r.get("name") or "session",
            "task": r.get("task"), "cwd": r.get("cwd"), "state": state,
            "activity": r.get("activity"), "last_tool": r.get("last_tool"),
            "model": r.get("model"), "needs_you": bool(r.get("needs_you")),
            "recent": recent, "age_secs": int(age),
        })
    agents.sort(key=lambda a: (_RANK.get(a["state"], 9), a["age_secs"]))
    needs = [a for a in agents if a["state"] in ("blocked", "waiting")]
    work = [a for a in agents if a["state"] == "working"]
    idle = [a for a in agents if a["state"] in ("idle", "done", "offline")]
    # attach pending inbox counts so the board can badge desks with unread notes
    conn = _db()
    try:
        pend = conn.execute(
            "SELECT to_session, to_name, COUNT(*) c FROM messages"
            " WHERE delivered=0 GROUP BY to_session, to_name").fetchall()
    finally:
        conn.close()
    by_sid, by_name = {}, {}
    for r in pend:
        if r["to_session"]:
            by_sid[r["to_session"]] = by_sid.get(r["to_session"], 0) + r["c"]
        if r["to_name"]:
            by_name[r["to_name"]] = by_name.get(r["to_name"], 0) + r["c"]
    for a in agents:
        a["inbox"] = by_sid.get(a["session_id"], 0) + by_name.get(a["name"], 0)

    return {"agents": agents,
            "buckets": {"needs_you": needs, "working": work, "idle": idle},
            "counts": {"total": len(agents), "needs_you": len(needs),
                       "working": len(work), "idle": len(idle)}}


# ---------------------------------------------------------------- messaging
def send_message(payload):
    """Leave a note for another session. Address it by `to_session` (exact id) or
    `to_name` (folder tag / name). `from_session`/`from_name` identify the sender (an
    agent, or 'operator' from the board UI)."""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    to_session = payload.get("to_session")
    to_name = payload.get("to_name")
    if not to_session and not to_name:
        return {"ok": False, "error": "need to_session or to_name"}
    conn = _db()
    try:
        with conn:
            conn.execute(
                "INSERT INTO messages (from_session, from_name, to_session, to_name,"
                " text) VALUES (?,?,?,?,?)",
                (payload.get("from_session"), payload.get("from_name") or "operator",
                 to_session, to_name, text[:2000]))
        return {"ok": True}
    finally:
        conn.close()


def pending_messages(session_id, name=None, ack=True):
    """Return (and by default mark delivered) the undelivered notes addressed to this
    session — by exact id OR by its name. Called by the recipient's hook, which prints
    them to stdout so Claude Code injects them into the session's context."""
    if not session_id and not name:
        return []
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE delivered=0 AND (to_session=? OR"
            " (to_name IS NOT NULL AND to_name=?)) ORDER BY id",
            (session_id, name)).fetchall()
        msgs = [dict(r) for r in rows]
        if ack and msgs:
            with conn:
                conn.execute(
                    "UPDATE messages SET delivered=1 WHERE id IN (%s)"
                    % ",".join("?" * len(msgs)), tuple(m["id"] for m in msgs))
        return msgs
    finally:
        conn.close()


# ---------------------------------------------------------------- http server
def _read_page():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.html")
    with open(p, encoding="utf-8") as f:
        return f.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "TheOffice/1.0"

    def log_message(self, *a):  # quiet
        pass

    def _send(self, status, body, ctype):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, _read_page(), "text/html; charset=utf-8")
        if path == "/api/agents":
            return self._send(200, board(), "application/json; charset=utf-8")
        self._send(404, {"error": "not found"}, "application/json")

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        routes = {
            "/api/agents/report": report,
            "/api/messages": send_message,
            "/api/messages/pending": lambda p: {
                "messages": pending_messages(p.get("session_id"), p.get("name"))},
        }
        fn = routes.get(path)
        if fn is None:
            return self._send(404, {"error": "not found"}, "application/json")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except (ValueError, UnicodeDecodeError):
            return self._send(200, {"ok": False, "error": "bad body"},
                              "application/json")
        self._send(200, fn(payload), "application/json")


def main():
    ap = argparse.ArgumentParser(description="The Office — live Claude Code agent board")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("OFFICE_PORT") or DEFAULT_PORT))
    args = ap.parse_args()
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print("The Office running at http://%s:%d  (Ctrl-C to stop)" % (HOST, args.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.server_close()


if __name__ == "__main__":
    main()
