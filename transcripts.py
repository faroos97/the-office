"""What a Claude Code transcript says about its session: its name, its Remote Control
link, and whether its turn is over. Standard library only.

Transcripts grow past 100 MB, and a hook waits 0.6 s for the server, so a file is read
from its END, backwards, and never further than SCAN_MAX bytes. Results are kept per
file: a file that did not change is not read again, and one that grew is searched for a
name or a link only back to where it was read last time.
"""
import datetime
import json
import os
import re
import threading

SCAN_MAX = 8 * 1024 * 1024   # never look further back than this
CHUNK = 256 * 1024

# Claude Code writes the bridge id as cse_<24 characters>; the claude.ai page of the same
# session is session_<the same 24 characters>.
BRIDGE_ID = re.compile(r"(?:cse|session)_([A-Za-z0-9]{24})")

_SURROGATE = re.compile("[\ud800-\udfff]")
_ENDED = ("end_turn", "stop_sequence", "max_tokens", "refusal")
_INTERRUPTED = "[Request interrupted by user"
_HEAD = (b'"custom-title"', b'"ai-title"', b'"bridge-session"')
_TURN = (b'"user"', b'"assistant"', b'"system"', b'"queued_command"')
# Bookkeeping lines (a whole API request can be megabytes): never parsed.
_SKIP = (b'{"type":"api-request', b'{"type":"data-flow', b'{"type":"file-history')

_cache = {}
_lock = threading.Lock()


def lines_from_end(path, limit=None):
    """Yield (offset, line) from the last line to the first, at most `limit` bytes back.
    A line cut by the limit is left out."""
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        pos = f.tell()
        floor = max(0, pos - (limit or SCAN_MAX))
        tail, size = b"", CHUNK
        while pos > floor:
            n = min(size, pos - floor)
            pos -= n
            f.seek(pos)
            parts = (f.read(n) + tail).split(b"\n")
            tail = parts[0]
            at = pos + sum(len(p) + 1 for p in parts)
            for p in reversed(parts[1:]):
                at -= len(p) + 1
                if p.strip():
                    yield at, p
            size = min(size * 2, 2 * 1024 * 1024)
        if floor == 0 and tail.strip():
            yield 0, tail


def _epoch(stamp):
    try:
        return datetime.datetime.fromisoformat(
            str(stamp).replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _dict(value):
    return value if isinstance(value, dict) else {}


def _user_kind(o):
    """What a 'user' entry is, with the tool calls it answers: 'received' (a message for
    the agent: the operator, another agent, the harness), 'interrupted', 'results', or
    (None, []) for what is not part of the conversation."""
    if o.get("isSidechain") or o.get("isCompactSummary"):
        return None, []
    if _dict(o.get("origin")).get("kind") == "task-notification":
        return None, []       # a background task ended: nobody answered the agent
    content = _dict(o.get("message")).get("content")
    text, results, other = "", [], False
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                results.append(str(b.get("tool_use_id")))
            elif b.get("type") == "text":
                text = text or str(b.get("text") or "")
            else:
                other = True
    text = text.lstrip()
    if text.startswith(_INTERRUPTED):
        return "interrupted", results
    if results:
        return "results", results
    if o.get("isMeta"):
        # harness notes (images, skills, caveats) are not messages; another agent's
        # message and one the Stop hook handed over are
        told = o.get("origin") or o.get("promptSource") == "system"
        if told or text.startswith("Stop hook feedback"):
            return "received", []
        return None, []
    if text.startswith(("<local-command", "<command-")):
        return None, []
    return ("received", []) if text or other else (None, [])


class _Turn:
    """Reads the last turn, newest entry first, until it knows how it stands."""

    def __init__(self):
        self.phase = None         # 'working' | 'ended'
        self.done = False
        self.ts = None            # when the newest entry of the conversation was written
        self.text = None          # the last text block, when the turn ended on one
        self.text_ts = None
        self.ask = None           # questions of an AskUserQuestion nobody answered
        self.ask_ts = None
        self.open_tools = 0       # tool calls without a result, AskUserQuestion excluded
        self._results = set()
        self._closed = False      # the harness wrote its end-of-turn line
        self._message = None      # id of the newest API message

    def _event(self, o):
        if self.ts is None:
            self.ts = _epoch(o.get("timestamp"))

    def _finish(self, phase):
        if self.phase is None:
            self.phase = phase
        self.done = True

    def _received(self):
        # nothing of the answer is written yet: any older question is cancelled
        self._finish("ended" if self._closed else "working")

    def feed(self, o):
        kind = o.get("type")
        if kind == "system":
            if self.ts is None and o.get("subtype") == "turn_duration":
                self._closed = True
        elif kind == "attachment":
            att = _dict(o.get("attachment"))
            if att.get("type") == "queued_command" \
                    and att.get("commandMode") != "task-notification":
                self._event(o)
                self._received()
        elif kind == "user":
            what, results = _user_kind(o)
            if not what:
                return
            self._event(o)
            self._results.update(results)
            if what == "received":
                self._received()
            elif what == "interrupted":
                self._finish("ended")
            elif self.phase is None:
                self.phase = "ended" if self._closed else "working"
        elif kind == "assistant" and not o.get("isSidechain"):
            self._assistant(o)

    def _assistant(self, o):
        msg = _dict(o.get("message"))
        mid = msg.get("id") or o.get("uuid")
        if self._message is not None and mid != self._message:
            self.done = True
            return
        self._message = mid
        self._event(o)
        blocks = msg.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        for b in reversed(blocks if isinstance(blocks, list) else []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                if str(b.get("id")) not in self._results:
                    if b.get("name") == "AskUserQuestion":
                        self._asked(b, o)
                    else:
                        self.open_tools += 1
                if self.phase is None:
                    self.phase = "working"
            elif b.get("type") == "text" and str(b.get("text") or "").strip():
                over = self._closed or msg.get("stop_reason") in _ENDED
                if self.phase is None:
                    self.phase = "ended" if over else "working"
                    if over:
                        self.text, self.text_ts = str(b["text"]), _epoch(o.get("timestamp"))
                        self.done = True
                        return
            elif self.phase is None:
                self.phase = "ended" if self._closed else "working"

    def _asked(self, block, o):
        out = []
        data = _dict(block.get("input")).get("questions")
        for q in data if isinstance(data, list) else []:
            if isinstance(q, dict) and str(q.get("question") or "").strip():
                labels = q.get("options")
                out.append({"question": str(q["question"]).strip(), "options": [
                    str(x.get("label")) for x in (labels if isinstance(labels, list) else [])
                    if isinstance(x, dict) and x.get("label")]})
        if out:
            self.ask, self.ask_ts = out, _epoch(o.get("timestamp"))


_NOTHING = {"custom_title": None, "ai_title": None, "bridge": None, "phase": None,
            "ts": None, "text": None, "text_ts": None, "ask": None, "ask_ts": None,
            "open_tools": 0}


def plain(value):
    """The same value without half characters (an emoji cut in two in the file): they can
    be neither stored nor sent."""
    if isinstance(value, str):
        return _SURROGATE.sub("�", value)
    if isinstance(value, dict):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(plain(v) for v in value)
    return value


def _read(path, old):
    """One pass over the end of `path`. `old` is (size, facts) of the previous pass."""
    known = old[0] if old else None
    custom = ai = bridge = None
    turn = _Turn()
    for at, ln in lines_from_end(path):
        new = known is None or at >= known
        if turn.done and ((custom and bridge) or not new):
            break
        if ln.startswith(_SKIP):
            continue
        if not ((not turn.done and any(w in ln for w in _TURN)) or
                (new and any(w in ln for w in _HEAD))):
            continue
        try:
            o = json.loads(ln.decode("utf-8", "replace"))
        except (ValueError, RecursionError):
            continue          # a line still being written, or a damaged one
        if not isinstance(o, dict):
            continue
        kind = o.get("type")
        if kind == "custom-title":
            custom = custom or " ".join(str(o.get("customTitle") or "").split())
        elif kind == "ai-title":
            ai = ai or " ".join(str(o.get("aiTitle") or "").split())
        elif kind == "bridge-session":
            if bridge is None:
                bridge = (str(o.get("sessionId") or ""), str(o.get("bridgeSessionId") or ""))
        elif not turn.done:
            turn.feed(o)
    was = old[1] if old else _NOTHING
    return plain({"custom_title": custom or was["custom_title"],
            "ai_title": ai or was["ai_title"], "bridge": bridge or was["bridge"],
            "phase": turn.phase, "ts": turn.ts, "text": turn.text,
            "text_ts": turn.text_ts, "ask": turn.ask, "ask_ts": turn.ask_ts,
            "open_tools": turn.open_tools})


def _before(path, size):
    """The last bytes before offset `size`: the same ones again mean the file only grew."""
    with open(path, "rb") as f:
        f.seek(max(0, size - 256))
        return f.read(min(size, 256))


def facts(path):
    """Everything above, for one transcript. All empty when there is no file."""
    if not path:
        return dict(_NOTHING)
    try:
        st = os.stat(path)
    except (OSError, ValueError, TypeError):
        return dict(_NOTHING)
    stamp = (st.st_size, st.st_mtime_ns)
    with _lock:
        hit = _cache.get(path)
    if hit and hit[0] == stamp:
        return dict(hit[1])
    try:
        grew = hit and hit[0][0] <= stamp[0] and _before(path, hit[0][0]) == hit[2]
        out = _read(path, (hit[0][0], hit[1]) if grew else None)
        end = _before(path, stamp[0])
    except OSError:
        return dict(hit[1]) if hit else dict(_NOTHING)
    with _lock:
        if len(_cache) > 300:
            _cache.clear()
        _cache[path] = (stamp, out, end)
    return dict(out)


def title(path):
    """The name the session was given, else the one Claude Code wrote for it, else None."""
    f = facts(path)
    return f["custom_title"] or f["ai_title"] or None


def reply_url(bridge, session_id):
    """The claude.ai page where the operator answers this session, or None. Strict on
    purpose: a wrong id would open another conversation."""
    if not bridge or bridge[0] != session_id:
        return None
    m = BRIDGE_ID.fullmatch(bridge[1])
    return "https://claude.ai/code/session_" + m.group(1) if m else None
