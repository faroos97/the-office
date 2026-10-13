"""Model routing for The Office: which model a new agent starts on, and the log that says
whether that choice was good. Standard library only; shared by office.py, runner.py and
agent.py.

config.json "model_rules" is an ordered table, first match wins:

    [{"type": "design proposal", "match": "design direction|proposal|...",
      "model": "claude-fable-5-1", "why": "taste decides the outcome"}, ...,
     {"type": "default", "match": ".*", "model": "claude-sonnet-5-5", "why": "..."}]

`match` is a case-insensitive regex tried against the task text and its folder. A row
without "type" or "why" still works. When the table is missing or nothing matches, the
agent gets DEFAULT_MODEL: a new agent is never left to inherit the operator's own default
model (that is the most expensive one).
"""
import datetime
import json
import os
import re

DEFAULT_MODEL = "claude-sonnet-5-5"
_MODEL_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-\[\]]{0,63}$")

# A model that costs the most is the one a result should earn: these are "senior" models,
# the others are "junior" ones, for the promote / demote suggestions.
_SENIOR = ("opus", "fable")


def valid_model(name):
    return bool(name) and bool(_MODEL_OK.match(str(name)))


def route(rows, area, task, fallback=None):
    """(model, type, why) for a task started in `area`: the first row whose match hits.
    `fallback` is the model to use when no row matches (config agent_args), else
    DEFAULT_MODEL."""
    text = "%s %s" % (area or "", task or "")
    for row in rows or []:
        try:
            if re.search(row["match"], text, re.IGNORECASE):
                model = str(row["model"])
                if valid_model(model):
                    return model, str(row.get("type") or "rule"), str(row.get("why") or "")
        except (KeyError, TypeError, re.error):
            continue
    return (fallback or DEFAULT_MODEL), "default", "no routing row matched"


def model_from_args(args):
    """The value after --model in an argument list, or None."""
    args = [str(a) for a in args or []]
    if "--model" in args:
        i = args.index("--model")
        if len(args) > i + 1:
            return args[i + 1]
    return None


# ---------------------------------------------------------------- names
_NAME_BAD = re.compile(r"[^\w .,'+-]", re.UNICODE)
_FIRST_CLAUSE = re.compile(r"[.:;(\[\u2014\n]")


def short_name(text, words=6, fallback="Agent"):
    """A window / session name from a task: its first clause, at most `words` words, with
    nothing that cmd.exe or a shell would treat as syntax. 'Fix and deploy The Office
    (repo C:/...)' becomes 'Fix and deploy The Office'."""
    text = " ".join(str(text or "").split())
    clause = _FIRST_CLAUSE.split(text, 1)[0]
    clause = _NAME_BAD.sub(" ", clause)
    out = clause.split()[:words]
    return " ".join(out).strip(" .,'-") or fallback


def unique_name(name, taken):
    """`name`, or name-2, name-3 ... when a live agent already has it (case-insensitive)."""
    taken = {str(t).strip().lower() for t in taken if t}
    if name.lower() not in taken:
        return name
    n = 2
    while ("%s-%d" % (name, n)).lower() in taken:
        n += 1
    return "%s-%d" % (name, n)


# ---------------------------------------------------------------- the log
def log_path():
    return os.environ.get("OFFICE_MODEL_LOG") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "model_log.jsonl")


def read_log(path=None):
    out = []
    try:
        with open(path or log_path(), encoding="utf-8") as f:
            for ln in f:
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(o, dict):
                    out.append(o)
    except OSError:
        pass
    return out


def append_log(entry, path=None):
    entry = dict(entry)
    entry.setdefault("date", datetime.date.today().isoformat())
    with open(path or log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def set_verdict(session_prefix, verdict, path=None):
    """Change the verdict of the logged agent whose session id starts with the prefix (or
    whose title contains it). True when a line was found."""
    path = path or log_path()
    lines = read_log(path)
    hit = False
    for o in reversed(lines):
        sid = str(o.get("session") or "")
        if (session_prefix and sid.startswith(session_prefix)) or (
                session_prefix and session_prefix.lower() in str(o.get("title") or "").lower()):
            o["verdict"] = verdict
            hit = True
            break
    if hit:
        with open(path, "w", encoding="utf-8") as f:
            for o in lines:
                f.write(json.dumps(o, ensure_ascii=False) + "\n")
    return hit


def transcript_tokens(path):
    """Input + output + cache-creation tokens of a Claude Code transcript (cache reads
    are left out: they are re-reads of the same context). Each API message is counted
    once, though the transcript writes one line per content block."""
    if not path or not os.path.exists(path):
        return None
    seen, total = set(), 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if '"usage"' not in ln:
                    continue
                try:
                    msg = (json.loads(ln).get("message") or {})
                except ValueError:
                    continue
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                key = msg.get("id") or ln[:80]
                if key in seen:
                    continue
                seen.add(key)
                total += sum(int(usage.get(k) or 0) for k in (
                    "input_tokens", "output_tokens", "cache_creation_input_tokens"))
    except OSError:
        return None
    return total


# ---------------------------------------------------------------- the report
PROMOTE_REDONE = 0.30        # junior model redone more than this share: try a senior one
DEMOTE_RUNS = 5              # senior model, every rated run a good job over this many


def _is_senior(model):
    return any(s in str(model).lower() for s in _SENIOR)


def report(entries):
    """Per type and model: runs, share rated good_job, average tokens, and a suggestion.
    Suggestions only; nothing here changes the routing table.
      promote: a non-senior model whose rated runs were redone more than 30% of the time
               (needs 3 rated runs, so one bad run is not a trend)
      demote:  a senior model with 100% good_job over 5 or more rated runs"""
    groups = {}
    for e in entries:
        groups.setdefault((e.get("type") or "unrouted", e.get("model") or "unknown"),
                          []).append(e)
    rows = []
    for (typ, model), es in sorted(groups.items()):
        rated = [e for e in es if e.get("verdict") in ("good_job", "fixed", "redone")]
        good = sum(1 for e in rated if e["verdict"] == "good_job")
        redone = sum(1 for e in rated if e["verdict"] == "redone")
        toks = [e["tokens"] for e in es if isinstance(e.get("tokens"), (int, float))]
        suggestion = ""
        if not _is_senior(model) and len(rated) >= 3 and redone / len(rated) > PROMOTE_REDONE:
            suggestion = "promote to Opus (%d of %d rated runs redone)" % (redone, len(rated))
        elif _is_senior(model) and len(rated) >= DEMOTE_RUNS and good == len(rated):
            suggestion = "demote to Sonnet (%d of %d rated runs good job)" % (good, len(rated))
        rows.append({
            "type": typ, "model": model, "runs": len(es), "rated": len(rated),
            "good_job_pct": round(100.0 * good / len(rated)) if rated else None,
            "avg_tokens": round(sum(toks) / len(toks)) if toks else None,
            "suggestion": suggestion})
    return {"rows": rows, "entries": len(entries)}
