#!/usr/bin/env python3
"""The Office: a live dashboard for your running Claude Code sessions.

One screen that shows every Claude Code session you have open, ranked by how much it
needs YOU right now:

    blocked (needs a decision)  ->  waiting (finished, wants your reply)
        ->  working  ->  idle

Each desk is headlined by Claude Code's own rolling session title. Click a desk to read
that agent's conversation. Agents can see each other, read each other's conversation,
leave each other messages, and start new agents (agent.py), so they coordinate without
you relaying between terminals.

Zero dependencies (Python 3.8+ standard library only). Listens on 127.0.0.1 only.
Fed by Claude Code hooks that POST here on every session event (see README.md).

    python office.py                  # serve on http://127.0.0.1:8787
    python office.py --port 9000      # or OFFICE_PORT=9000
    python office.py --allow-spawn    # also allow starting new agents

Then open http://127.0.0.1:8787 and add the hook (README) so sessions report in.
"""
import argparse
import datetime
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

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

-- Messages between agents. Addressed to a session id. 'delivered' once the recipient's
-- hook (end of turn, or next prompt) or its `agent.py inbox` has pulled it.
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_session TEXT, from_name TEXT,
  to_session TEXT, to_name TEXT,
  text TEXT NOT NULL,
  ts TEXT DEFAULT (datetime('now')),
  delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_msg_pending ON messages(delivered);

-- The task list: work queued for the task runner (runner.py). A task is ready when it
-- is pending, has attempts left, and every task it depends on is done.
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  body TEXT,                              -- the instruction handed to the worker
  dir TEXT,                               -- folder the work belongs to
  kind TEXT DEFAULT 'default',            -- free label the worker may use (e.g. a model class)
  check_name TEXT,                        -- a check from config.json the runner runs afterwards
  deps TEXT DEFAULT '[]',                 -- JSON list of task ids that must be done first
  status TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|blocked|failed|cancelled
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 2,
  owner TEXT, created_by TEXT,
  summary TEXT,                           -- one paragraph: what happened
  result TEXT,                            -- JSON receipt from the worker
  created_at TEXT DEFAULT (datetime('now')),
  updated_at TEXT DEFAULT (datetime('now')),
  started_at TEXT, finished_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_tasks_status ON tasks(status);

-- Small key/value facts about the office itself (who the floor manager is).
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_DDL)
    # columns added after the first release: back-fill them on an existing office.db
    have = {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
    if "transcript_path" not in have:
        conn.execute("ALTER TABLE agents ADD COLUMN transcript_path TEXT")
    if "roster_sig" not in have:
        conn.execute("ALTER TABLE agents ADD COLUMN roster_sig TEXT")
    if "check_name" not in {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}:
        conn.execute("ALTER TABLE tasks ADD COLUMN check_name TEXT")
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
            activity = "finished, waiting for you"
        elif tool:
            activity = "using %s" % tool
        elif event == "SessionStart":
            activity = "session started"
        elif event == "UserPromptSubmit":
            activity = "new request"
        else:
            activity = None

        if payload.get("activity"):
            # a reporter that is not a Claude Code hook (the task runner) says in its
            # own words what its worker is doing
            activity = " ".join(str(payload["activity"]).split())[:160]

        recent_line = None
        if activity:
            recent_line = "%s  %s" % (
                datetime.datetime.now().strftime("%H:%M:%S"), activity)

        fields = {"name": _friendly_name(payload), "task": task,
                  "cwd": payload.get("cwd"), "state": state,
                  "needs_you": 1 if needs_you else 0, "activity": activity,
                  "last_tool": tool, "model": payload.get("model"),
                  "transcript_path": payload.get("transcript_path")}
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
        boss = _manager_sid(conn)
    finally:
        conn.close()
    for a in agents:
        a["is_manager"] = a["session_id"] == boss
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
                       "working": len(work), "idle": len(idle)},
            "manager": boss, "manager_dir": _config().get("manager_dir") or "",
            "can_spawn": os.environ.get("OFFICE_ALLOW_SPAWN") == "1"}


# ---------------------------------------------------------------- finding an agent
def _resolve(conn, target):
    """Find one agent from a loose reference: exact session id, an id prefix (6+ chars),
    its folder name, or a fragment of its title. Most recently active wins."""
    target = (target or "").strip()
    if not target:
        return None
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM agents ORDER BY updated_at DESC")]
    low = target.lower()
    for r in rows:
        if r["session_id"] == target:
            return r
    if len(target) >= 6:
        for r in rows:
            if r["session_id"].startswith(target):
                return r
    for r in rows:
        if (r.get("name") or "").lower() == low:
            return r
    for r in rows:
        if low in (r.get("task") or "").lower():
            return r
    return None


def _label(row):
    """How an agent is named to the others: its title, with its folder."""
    if not row:
        return None
    title, name = row.get("task"), row.get("name")
    if title and name:
        return "%s (%s)" % (title, name)
    return title or name or row.get("session_id", "")[:8]


# ---------------------------------------------------------------- conversations
CONVO_TAIL_BYTES = 2000000   # only the end of a transcript is read (they get large)
CONVO_MAX_MSGS = 60
CONVO_MSG_CHARS = 6000


def _tail_lines(path, nbytes):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        start = max(0, f.tell() - nbytes)
        f.seek(start)
        data = f.read()
    lines = data.decode("utf-8", "replace").split("\n")
    return lines[1:] if start > 0 else lines   # drop the partial first line


def _entry_to_message(o):
    """One transcript JSONL entry -> {role, text, tools} or None. Keeps what a human
    would call the conversation: what the user typed, what the agent said, and which
    tools it used. Drops tool results, system reminders, sub-agent chatter."""
    role = o.get("type")
    if role not in ("user", "assistant") or o.get("isSidechain") or o.get("isMeta"):
        return None
    content = (o.get("message") or {}).get("content")
    texts, tools = [], []
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return None
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            t = (p.get("text") or "").strip()
            # harness-generated blocks (<system-reminder>, <local-command-...>) aren't
            # part of the conversation a person would recognise
            if t and not t.startswith("<"):
                texts.append(t)
        elif p.get("type") == "tool_use":
            tools.append(p.get("name") or "tool")
    text = "\n\n".join(texts)
    if not text and not tools:
        return None
    if role == "user" and not text:
        return None
    return {"role": role, "text": text, "tools": tools, "ts": o.get("timestamp")}


def conversation(target, limit=CONVO_MAX_MSGS):
    """The recent conversation of one agent, read from its Claude Code transcript.
    Only the transcript path the session's own hook reported is ever opened."""
    conn = _db()
    try:
        row = _resolve(conn, target)
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "no agent matches %r" % target}
    path = row.get("transcript_path")
    head = {"ok": True, "session_id": row["session_id"], "name": row.get("name"),
            "title": row.get("task"), "state": row.get("state"),
            "cwd": row.get("cwd"), "messages": []}
    if not path or not os.path.exists(path):
        head["note"] = "no transcript yet for this session"
        return head
    try:
        lines = _tail_lines(path, CONVO_TAIL_BYTES)
    except OSError:
        head["note"] = "transcript unreadable"
        return head
    msgs = []
    for ln in lines:
        if not ln.strip():
            continue
        try:
            m = _entry_to_message(json.loads(ln))
        except ValueError:
            continue
        if not m:
            continue
        # Claude Code writes each assistant content block as its own entry: fold
        # consecutive assistant entries into one message
        if msgs and m["role"] == "assistant" and msgs[-1]["role"] == "assistant":
            last = msgs[-1]
            if m["text"]:
                last["text"] = (last["text"] + "\n\n" + m["text"]).strip()
            last["tools"].extend(m["tools"])
            continue
        msgs.append(m)
    for m in msgs:
        # a long working turn folds into one big message: keep its END, which is what
        # the agent is saying now, not how it started
        if len(m["text"]) > CONVO_MSG_CHARS:
            m["text"] = "…" + m["text"][-CONVO_MSG_CHARS:]
    head["messages"] = msgs[-int(limit):]
    return head


# ---------------------------------------------------------------- team awareness
_STATE_WORDS = {
    "working": "working now",
    "blocked": "mid-turn, waiting on the user for a permission",
    "waiting": "idle at its prompt",
    "idle": "idle",
}


def _others(conn, me):
    """Every agent except `me`, with the same staleness rule as the board."""
    rows = conn.execute(
        "SELECT *, CAST((julianday('now')-julianday(updated_at))*86400 AS INTEGER)"
        " AS age_secs FROM agents WHERE session_id != ? ORDER BY updated_at DESC",
        (me or "",)).fetchall()
    out = []
    for r in rows:
        r = dict(r)
        if (r.get("state") or "working") == "working" and \
                (r.get("age_secs") or 0) > STALE_WORKING_SECS:
            r["state"] = "idle"
        out.append(r)
    return out


def _manager_sid(conn):
    """Session id of the floor manager, if one registered and is still on the board."""
    row = conn.execute("SELECT value FROM meta WHERE key='manager'").fetchone()
    if not row or not row["value"]:
        return None
    alive = conn.execute("SELECT 1 FROM agents WHERE session_id=?",
                         (row["value"],)).fetchone()
    return row["value"] if alive else None


def manager(payload):
    """The floor manager registers itself here (`agent.py manager start`), so the board
    can mark its desk and the other agents know who hands out the work."""
    action = payload.get("action") or "status"
    sid = (payload.get("session_id") or "").strip()
    conn = _db()
    try:
        current = _manager_sid(conn)
        if action == "start":
            if not sid:
                return {"ok": False, "error": "no session id (CLAUDE_CODE_SESSION_ID)"}
            if current and current != sid:
                other = _resolve(conn, current)
                return {"ok": False, "error": "there is already a floor manager: %s. "
                        "Work with it, or have the operator stop it first." % _label(other)}
            with conn:
                conn.execute("INSERT INTO meta (key, value) VALUES ('manager', ?)"
                             " ON CONFLICT(key) DO UPDATE SET value=excluded.value", (sid,))
            return {"ok": True, "manager": sid}
        if action == "stop":
            if current and sid and current != sid:
                return {"ok": False, "error": "you are not the floor manager"}
            with conn:
                conn.execute("DELETE FROM meta WHERE key='manager'")
            return {"ok": True, "manager": None}
        return {"ok": True, "manager": current,
                "label": _label(_resolve(conn, current)) if current else None}
    finally:
        conn.close()


def _last_user_line(path, max_chars=140):
    """The start of the most recent thing the user asked that session: what it is
    really on, in the user's own words."""
    if not path or not os.path.exists(path):
        return None
    try:
        lines = _tail_lines(path, 400000)
    except OSError:
        return None
    for ln in reversed(lines):
        if '"user"' not in ln:
            continue
        try:
            m = _entry_to_message(json.loads(ln))
        except ValueError:
            continue
        if m and m["role"] == "user" and m["text"]:
            one = " ".join(m["text"].split())
            return one[:max_chars] + ("…" if len(one) > max_chars else "")
    return None


def team(payload):
    """What one agent should know about the others, as text for its context.

    Sent in full when `force` is set (session start). Otherwise only when the team
    changed since this agent last saw it (someone arrived, left, or moved to a new
    subject), so a session is not re-told the same roster on every prompt."""
    me = (payload.get("session_id") or "").strip()
    conn = _db()
    try:
        others = _others(conn, me)
        boss = _manager_sid(conn)
        sig = "|".join(sorted("%s:%s" % (r["session_id"][:8], r.get("task") or "")
                              for r in others)) + "|mgr:" + (boss or "")[:8]
        mine = conn.execute("SELECT roster_sig FROM agents WHERE session_id=?",
                            (me,)).fetchone()
        if not payload.get("force") and mine is not None and mine["roster_sig"] == sig:
            return {"text": "", "changed": False, "count": len(others)}
        if mine is not None:
            with conn:
                conn.execute("UPDATE agents SET roster_sig=? WHERE session_id=?",
                             (sig, me))
    finally:
        conn.close()
    if not others:
        return {"text": "[The Office] No other agents are running right now.",
                "changed": True, "count": 0}
    lines = ["[The Office] Your teammates right now (other Claude Code agents working "
             "for the same person):"]
    for r in others:
        line = "- [%s] %s | %s" % (r.get("name") or "?", r.get("task") or "(untitled)",
                                   _STATE_WORDS.get(r["state"], r["state"]))
        if r["session_id"] == boss:
            line += " | FLOOR MANAGER"
        asked = _last_user_line(r.get("transcript_path"))
        if asked:
            line += ' | last asked: "%s"' % asked
        lines.append(line)
    if boss and boss != me:
        lines.append(
            "There is a floor manager (marked above). It hands out the work and keeps "
            "the task list. Tell it when you finish something it gave you or when you "
            "are blocked on something outside your area; if it assigned you a task, "
            "close it with `task done` or `task blocked`.")
    return {"text": "\n".join(lines), "changed": True, "count": len(others)}


# ---------------------------------------------------------------- messaging
def send_message(payload):
    """One agent leaves a message for another. `to` is a loose reference (folder name,
    title fragment, session id); `to_session` / `to_name` also work. The sender is
    identified by `from_session`, or by `from_cwd` (the directory it runs in)."""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "empty message"}
    conn = _db()
    try:
        sender = None
        if payload.get("from_session"):
            sender = _resolve(conn, payload["from_session"])
        elif payload.get("from_cwd"):
            want = payload["from_cwd"].replace("\\", "/").rstrip("/").lower()
            for r in conn.execute("SELECT * FROM agents ORDER BY updated_at DESC"):
                if (r["cwd"] or "").replace("\\", "/").rstrip("/").lower() == want:
                    sender = dict(r)
                    break
        from_name = _label(sender) or payload.get("from_name") or "another agent"

        to_session, to_name = payload.get("to_session"), payload.get("to_name")
        if payload.get("to") and not to_session:
            target = _resolve(conn, payload["to"])
            if not target:
                return {"ok": False, "error": "no agent matches %r" % payload["to"]}
            to_session = target["session_id"]
        if not to_session and not to_name:
            return {"ok": False, "error": "need `to` (or to_session / to_name)"}
        if sender and to_session == sender["session_id"]:
            return {"ok": False, "error": "that is your own session"}
        with conn:
            conn.execute(
                "INSERT INTO messages (from_session, from_name, to_session, to_name,"
                " text) VALUES (?,?,?,?,?)",
                (sender["session_id"] if sender else None, from_name,
                 to_session, to_name, text[:4000]))
        # Tell the sender the truth about when this will be read. Only an agent that is
        # mid-turn has a hook still to fire; one sitting at its prompt reads nothing
        # until the user types there.
        recipient = None
        for r in _others(conn, None):
            if r["session_id"] == to_session:
                recipient = {"label": _label(r), "state": r["state"],
                             "mid_turn": r["state"] in ("working", "blocked"),
                             "cwd": r.get("cwd")}
                break
        return {"ok": True, "to": to_session or to_name, "recipient": recipient}
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


# ---------------------------------------------------------------- task list
# pending = waiting for the runner; assigned = given to a live agent, which closes it
TASK_STATUSES = ("pending", "assigned", "running", "done", "blocked", "failed",
                 "cancelled")
TASK_STALE_SECS = 300    # a 'running' task whose runner went silent this long is retried


def _config():
    """Optional config.json next to this file: {"worker_cmd": [...], "kinds": [...]}.
    Read on demand so an edit applies without a restart."""
    path = os.environ.get("OFFICE_CONFIG") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError("it must be a JSON object")
        return cfg
    except (OSError, ValueError) as e:
        # A config that exists but cannot be read must not be ignored: the runner would
        # quietly fall back to the default worker, which is not what was configured.
        return {"_error": "config.json is not valid (%s). Fix it or remove it." % e}


PARALLEL_MAX = 8


def _parallel(cfg):
    """How many tasks the runner works on at once: config.json "parallel", default 1."""
    try:
        return max(1, min(int(cfg.get("parallel") or 1), PARALLEL_MAX))
    except (TypeError, ValueError):
        return 1


def _checks(cfg):
    """The named checks of config.json: {"tests": "python -m unittest", ...}. A task may
    name one; the runner executes it after the worker. Only the operator's config says
    which commands exist, so a task (or the agent that queued it) can pick a check but
    never supply a command."""
    checks = cfg.get("checks")
    return checks if isinstance(checks, dict) else {}


def _norm_dir(path):
    return os.path.normcase(os.path.abspath(path)).rstrip("\\/") if path else ""


def _dirs_overlap(a, b):
    """Two tasks may not run at the same time when one could write into the other's
    folder: the same folder, or one inside the other. Tasks without a folder all run in
    the runner's own folder, so they overlap each other."""
    a, b = _norm_dir(a), _norm_dir(b)
    if not a or not b:
        return a == b
    return a == b or a.startswith(b + os.sep) or b.startswith(a + os.sep)


def _task_id(value):
    """Accept 12, "12" or "T-12"."""
    try:
        return int(str(value).strip().upper().replace("T-", ""))
    except (TypeError, ValueError):
        return None


def _task_shape(r):
    r = dict(r)
    for key, empty in (("deps", []), ("result", None)):
        try:
            r[key] = json.loads(r[key]) if r.get(key) else empty
        except (ValueError, TypeError):
            r[key] = empty
    r["ref"] = "T-%d" % r["id"]
    return r


def tasks_list(payload=None):
    """Every open task, plus the ones that finished in the last week."""
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT *, CAST((julianday('now')-julianday(updated_at))*86400 AS INTEGER)"
            " AS age_secs FROM tasks WHERE status IN ('pending','assigned','running',"
            "'blocked','failed') OR julianday('now')-julianday(updated_at) < 7 ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    tasks = [_task_shape(r) for r in rows]
    counts = {s: 0 for s in TASK_STATUSES}
    for t in tasks:
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    runner = _RUNNER.get("proc")
    cfg = _config()
    return {"tasks": tasks, "counts": counts,
            "runner_active": bool(runner and runner.poll() is None),
            "can_run": os.environ.get("OFFICE_ALLOW_SPAWN") == "1",
            "kinds": cfg.get("kinds") or [],
            "checks": sorted(_checks(cfg)),
            "parallel": _parallel(cfg),
            "config_error": cfg.get("_error")}


def task_add(payload):
    title = " ".join((payload.get("title") or "").split())
    if not title:
        return {"ok": False, "error": "a task needs a title"}
    deps = []
    for d in payload.get("deps") or []:
        tid = _task_id(d)
        if tid is None:
            return {"ok": False, "error": "bad dependency: %r" % d}
        deps.append(tid)
    try:
        max_attempts = max(1, min(int(payload.get("max_attempts") or 2), 5))
    except (TypeError, ValueError):
        max_attempts = 2
    body = (payload.get("body") or "").strip()[:8000]
    creator = (payload.get("created_by") or "operator")[:80]
    check = (payload.get("check") or "").strip() or None
    if check and check not in _checks(_config()):
        have = ", ".join(sorted(_checks(_config()))) or "none are defined"
        return {"ok": False, "error": "no check named %r in config.json (%s)"
                % (check, have)}
    if check and payload.get("to"):
        return {"ok": False, "error": "a check is run by the task runner; a task given "
                "to a live agent is checked by that agent"}
    conn = _db()
    try:
        # `to` gives the task to a live agent instead of the runner: it is told about it
        # and closes it itself with `agent.py task done`.
        agent = None
        if payload.get("to"):
            agent = _resolve(conn, payload["to"])
            if not agent:
                return {"ok": False, "error": "no agent matches %r" % payload["to"]}
        with conn:
            cur = conn.execute(
                "INSERT INTO tasks (title, body, dir, kind, check_name, deps, max_attempts,"
                " created_by, status, owner) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (title[:200], body,
                 (payload.get("dir") or "").strip() or (agent or {}).get("cwd"),
                 (payload.get("kind") or "default").strip()[:40] or "default",
                 check, json.dumps(deps), max_attempts, creator,
                 "assigned" if agent else "pending",
                 agent["session_id"] if agent else None))
            tid = cur.lastrowid
            recipient = None
            if agent:
                sender = _resolve(conn, creator) if creator != "operator" else None
                conn.execute(
                    "INSERT INTO messages (from_session, from_name, to_session, text)"
                    " VALUES (?,?,?,?)",
                    (sender["session_id"] if sender else None,
                     _label(sender) or "the operator", agent["session_id"],
                     "Task T-%d is assigned to you: %s%s\nWhen it is finished run "
                     "`task done T-%d <what you did and where>`. If you cannot finish "
                     "it run `task blocked T-%d <why>`."
                     % (tid, title, ("\n" + body) if body else "", tid, tid)))
                state = "working"
                for r in _others(conn, None):
                    if r["session_id"] == agent["session_id"]:
                        state = r["state"]
                recipient = {"label": _label(agent), "state": state,
                             "mid_turn": state in ("working", "blocked")}
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return {"ok": True, "task": _task_shape(row), "recipient": recipient}
    finally:
        conn.close()


def task_claim(payload):
    """Hand the next ready task to a runner, atomically: no two runners get the same
    one. Along the way the queue settles itself, so it cannot spin on work that can
    never run: a task out of attempts fails, a task whose dependency did not finish is
    blocked, and a task left 'running' by a runner that went silent is put back.

    Several runners (or one runner with several workers) may claim at once. A task is
    held back while another task is running in a folder that overlaps its own, so two
    workers never write into the same folder at the same time. With no task to give,
    the answer says what is still going on ({running, pending}) so a worker can tell
    "wait, something will free up" from "nothing left for me"."""
    owner = (payload.get("owner") or "runner")[:80]
    conn = _db()
    conn.isolation_level = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE tasks SET status='pending', owner=NULL, updated_at=datetime('now'),"
            " summary='the runner stopped responding; queued again'"
            " WHERE status='running'"
            " AND (julianday('now')-julianday(updated_at))*86400 > ?",
            (TASK_STALE_SECS,))
        status = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM tasks")}
        busy = [r["dir"] for r in conn.execute("SELECT dir FROM tasks WHERE status='running'")]
        claimed = None
        for r in conn.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY id").fetchall():
            t = _task_shape(r)
            if t["attempts"] >= t["max_attempts"]:
                conn.execute("UPDATE tasks SET status='failed', finished_at=datetime('now'),"
                             " updated_at=datetime('now'), summary=? WHERE id=?",
                             ("out of attempts (%d)" % t["max_attempts"], t["id"]))
                status[t["id"]] = "failed"
                continue
            dep_states = [status.get(d) for d in t["deps"]]
            if any(s in ("failed", "blocked", "cancelled", None) for s in dep_states):
                conn.execute("UPDATE tasks SET status='blocked', finished_at=datetime('now'),"
                             " updated_at=datetime('now'), summary=? WHERE id=?",
                             ("a task it depends on did not finish", t["id"]))
                status[t["id"]] = "blocked"
                continue
            if all(s == "done" for s in dep_states):
                if any(_dirs_overlap(t["dir"], d) for d in busy):
                    continue  # its folder is being worked on; it stays pending for now
                conn.execute("UPDATE tasks SET status='running', owner=?, attempts=attempts+1,"
                             " started_at=datetime('now'), updated_at=datetime('now'),"
                             " finished_at=NULL WHERE id=?", (owner, t["id"]))
                claimed = t["id"]
                status[t["id"]] = "running"
                break
        conn.execute("COMMIT")
        if claimed is None:
            states = list(status.values())
            return {"ok": True, "task": None, "running": states.count("running"),
                    "pending": states.count("pending")}
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (claimed,)).fetchone()
        return {"ok": True, "task": _task_shape(row)}
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def task_update(payload):
    """Record what happened to a task. {id, status?, summary?, result?, touch?, retry?}
    `touch` is the runner's heartbeat. `retry` puts a task back in the queue with a
    fresh attempt budget (the board's Retry button)."""
    tid = _task_id(payload.get("id"))
    if tid is None:
        return {"ok": False, "error": "missing task id"}
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        if row is None:
            return {"ok": False, "error": "no task T-%d" % tid}
        sets, vals = ["updated_at=datetime('now')"], []
        if payload.get("body_append"):
            # a retry usually comes with a better instruction
            sets.append("body=?")
            vals.append(((row["body"] or "") + "\n\n" +
                         str(payload["body_append"]).strip())[:8000].strip())
        if payload.get("retry"):
            sets += ["status='pending'", "attempts=0", "owner=NULL", "finished_at=NULL"]
        elif payload.get("status"):
            st = payload["status"]
            if st not in TASK_STATUSES:
                return {"ok": False, "error": "status must be one of %s"
                        % ", ".join(TASK_STATUSES)}
            sets.append("status=?")
            vals.append(st)
            if st in ("done", "blocked", "failed", "cancelled"):
                sets.append("finished_at=datetime('now')")
            if st == "pending":
                sets.append("owner=NULL")
                if payload.get("refund_attempt"):
                    # "not now" (quota wait, engine busy) is not a failed attempt
                    sets.append("attempts=MAX(attempts-1, 0)")
        if payload.get("summary") is not None:
            sets.append("summary=?")
            vals.append(str(payload["summary"])[:2000])
        if payload.get("result") is not None:
            sets.append("result=?")
            vals.append(json.dumps(payload["result"], ensure_ascii=False)[:20000])
        with conn:
            conn.execute("UPDATE tasks SET %s WHERE id=?" % ", ".join(sets),
                         tuple(vals) + (tid,))
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        return {"ok": True, "task": _task_shape(row)}
    finally:
        conn.close()


_RUNNER = {"proc": None}


def tasks_run(payload):
    """Start the task runner (runner.py) in the background if it is not already going.
    It works through the ready tasks and exits. Under the same switch as new agents,
    because it starts Claude sessions."""
    if os.environ.get("OFFICE_ALLOW_SPAWN") != "1":
        return {"ok": False, "error": "running tasks is off. Start office.py with "
                "--allow-spawn to turn it on."}
    cfg = _config()
    if cfg.get("_error"):
        return {"ok": False, "error": cfg["_error"]}
    proc = _RUNNER.get("proc")
    if proc and proc.poll() is None:
        return {"ok": True, "already_running": True}
    here = os.path.dirname(os.path.abspath(__file__))
    env = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        _RUNNER["proc"] = subprocess.Popen(
            [sys.executable, os.path.join(here, "runner.py"),
             "--parallel", str(_parallel(cfg))], cwd=here, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags)
    except OSError as e:
        return {"ok": False, "error": "could not start the runner: %s" % e}
    return {"ok": True, "already_running": False}


# ---------------------------------------------------------------- new agent
_SESSION_ENV = {
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "CLAUDE_JOB_DIR",
    "CLAUDE_EFFORT", "AI_AGENT",
}


def _claude_bin():
    """The claude executable: OFFICE_CLAUDE_BIN, else whatever is on PATH, else the
    default install location (office.py may run from a context with a shorter PATH)."""
    explicit = os.environ.get("OFFICE_CLAUDE_BIN")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    for name in ("claude.exe", "claude"):
        p = os.path.join(os.path.expanduser("~"), ".local", "bin", name)
        if os.path.isfile(p):
            return p
    return "claude"


def spawn_agent(payload):
    """Open a new terminal window running an interactive `claude` session in `cwd`,
    started on `task`. This is how you (from the board) or an agent (from agent.py)
    creates another agent.

    Off unless the server was started with OFFICE_ALLOW_SPAWN=1: it starts a local
    program, so it is opt-in, and the server only ever listens on 127.0.0.1."""
    if os.environ.get("OFFICE_ALLOW_SPAWN") != "1":
        return {"ok": False, "error": "creating agents is off. Start office.py with "
                "OFFICE_ALLOW_SPAWN=1 to turn it on."}
    cwd = os.path.expanduser((payload.get("cwd") or "").strip() or "~")
    if not os.path.isdir(cwd):
        return {"ok": False, "error": "directory not found: %s" % cwd}
    cfg = _config()
    if cfg.get("_error"):
        return {"ok": False, "error": cfg["_error"]}
    extra = cfg.get("agent_args") or []
    if payload.get("role") == "manager":
        # the floor manager: its rulebook is manager.md, its goal comes from the operator
        conn = _db()
        try:
            if _manager_sid(conn):
                return {"ok": False, "error": "a floor manager is already running"}
        finally:
            conn.close()
        here = os.path.dirname(os.path.abspath(__file__)).replace("\\", "/")
        goal = " ".join((payload.get("task") or "").split())
        payload = dict(payload, task=(
            "You are the floor manager for this team of agents. Read the file "
            "%s/manager.md now and follow it exactly. %s"
            % (here, ("The operator's goal for this session: " + goal) if goal else
               "The operator gave no specific goal: work from the task list and the "
               "team's priorities, and ask if the next step is not clear.")))
        extra = cfg.get("manager_args") or extra
    # one line, no quotes: the task is passed as a single argument, never as shell text
    task = " ".join((payload.get("task") or "").split()).replace('"', "'")[:2000]
    args = [_claude_bin()] + [str(a) for a in extra] + ([task] if task else [])
    # If office.py itself was started from inside a Claude Code session, its per-session
    # variables must not leak into the new agent (it would think it is a child session).
    env = {k: v for k, v in os.environ.items() if k not in _SESSION_ENV}
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen(["cmd", "/c", "start", "Office agent", "cmd", "/k"] + args,
                             cwd=cwd, env=env)
        elif sys.platform == "darwin":
            line = "cd %s && %s" % (shlex.quote(cwd),
                                    " ".join(shlex.quote(a) for a in args))
            subprocess.Popen(["osascript", "-e",
                              'tell application "Terminal" to do script "%s"'
                              % line.replace("\\", "\\\\").replace('"', '\\"')], env=env)
        else:
            term = os.environ.get("OFFICE_TERMINAL", "x-terminal-emulator")
            subprocess.Popen([term, "-e"] + args, cwd=cwd, env=env)
    except OSError as e:
        return {"ok": False, "error": "could not open a terminal: %s" % e}
    return {"ok": True, "cwd": cwd, "task": task}


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

    def _local(self):
        """True when the request comes from the board itself or from a local program.
        The server starts sessions and queues work, so a page on some other site that
        happens to be open in the operator's browser must not be able to call it, and
        neither may a DNS name that someone pointed at 127.0.0.1. A browser names the
        calling page in Origin; local programs (hook.py, agent.py, runner.py) send none."""
        port = self.server.server_address[1]
        names = {"%s:%d" % (h, port) for h in ("127.0.0.1", "localhost")}
        if (self.headers.get("Host") or "").strip().lower() not in names:
            return False
        origin = self.headers.get("Origin")
        return origin is None or origin.strip().lower() in {"http://" + n for n in names}

    def do_GET(self):
        if not self._local():
            return self._send(403, {"error": "local requests only"}, "application/json")
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._send(200, _read_page(), "text/html; charset=utf-8")
        if path == "/api/agents":
            return self._send(200, board(), "application/json; charset=utf-8")
        if path == "/api/tasks":
            return self._send(200, tasks_list(), "application/json; charset=utf-8")
        if path == "/api/conversation":
            q = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            target = (q.get("target") or q.get("session") or [""])[0]
            try:
                limit = max(1, min(int((q.get("limit") or [CONVO_MAX_MSGS])[0]), 200))
            except ValueError:
                limit = CONVO_MAX_MSGS
            return self._send(200, conversation(target, limit),
                              "application/json; charset=utf-8")
        self._send(404, {"error": "not found"}, "application/json")

    def do_POST(self):
        if not self._local():
            return self._send(403, {"error": "local requests only"}, "application/json")
        path = self.path.split("?", 1)[0]
        routes = {
            "/api/agents/report": report,
            "/api/messages": send_message,
            "/api/spawn": spawn_agent,
            "/api/team": team,
            "/api/manager": manager,
            "/api/tasks": task_add,
            "/api/tasks/claim": task_claim,
            "/api/tasks/update": task_update,
            "/api/tasks/run": tasks_run,
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
    ap = argparse.ArgumentParser(description="The Office: live Claude Code agent board")
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("OFFICE_PORT") or DEFAULT_PORT))
    ap.add_argument("--allow-spawn", action="store_true",
                    help="let the board and agents start new Claude Code sessions "
                         "(same as OFFICE_ALLOW_SPAWN=1)")
    args = ap.parse_args()
    if args.allow_spawn:
        os.environ["OFFICE_ALLOW_SPAWN"] = "1"
    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print("The Office running at http://%s:%d  (Ctrl-C to stop)%s" % (
        HOST, args.port,
        "  [new agents: on]" if os.environ.get("OFFICE_ALLOW_SPAWN") == "1" else ""))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
        httpd.server_close()


if __name__ == "__main__":
    main()
