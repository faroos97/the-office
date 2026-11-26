"""The tree of the board: who needs the operator and why, in what order, and which
department each agent belongs to. Pure functions over what office.py knows about a
session and what transcripts.py read. Standard library only.

Statuses: permission (the hook says the session is stuck on a permission prompt),
decision (it asked something), travaille, repos.
"""
import bisect
import functools
import json
import os
import re
import unicodedata

import transcripts

# An agent may end its answer with one of these lines. It then says itself how it stands
# and nothing is guessed. The three forms live here and nowhere else. The line is compared
# without its Markdown (list mark, bold, code); case and the kind of spaces do not matter.
ETAT_LINE = re.compile(r"[ÉE]TAT\s*:\s*(?:QUESTION\s*:\s*(?P<question>\S.*)"
                       r"|FINI\s*:\s*(?P<fini>\S.*)|PAUSE\s*:\s*(?P<pause>attend\s+\S.*))",
                       re.IGNORECASE)

# Ways to ask the operator for something without a question mark. They live here and
# nowhere else. Words between quotation marks, code and quoted lines never count.
ASKING = re.compile(r"""
    \bil\s(?:me\s|nous\s)?manque\s(?:(?:seulement|encore|toujours|juste|aussi|donc)\s)*
        (?:ton|ta|tes|votre|vos)\b
  | \bil\sne\s(?:me\s|nous\s)?manque\splus\sque\s(?:ton|ta|tes|votre|vos)\b
  | \bil\ste\sreste\s(?:(?:seulement|encore|juste|aussi|donc)\s)*à\s
  | \bil\sne\ste\sreste\splus\squ['’]à\s
  | \bj['’]attends\s(?:(?:seulement|encore|toujours|juste|aussi|donc|maintenant)\s)*
        (?:ton|ta|tes|votre|vos)\b
  | \bdis[-‑]moi\b
  | (?<!pas\s)\b[àa]\stoi\sde\s(?:choisir|trancher|décider)\b
  | (?:^|[.,;:!?…(–—])[ \t*_\xa0]{0,9}(?:[-•+]\s{1,3}|\d{1,3}[.)]\s{1,3})?[*_]{0,3}(?P<r>r[ée]ponds)\b
    """, re.IGNORECASE | re.VERBOSE | re.MULTILINE)

SILENT_SECS = 30 * 60        # a turn that wrote nothing for this long is not running
SUBAGENT_SECS = 120          # a tool seen this recently after a turn: a sub-agent works
QUESTION_MAX = 600           # a longer paragraph is reduced to the sentences that ask
SHOWN_MAX = 900              # longest text the panel shows
TEXT_MAX = 20000             # only the end of a longer answer is searched
MARKS_MAX = 300              # only the last question marks of an answer are looked at
_NEAR = 400                  # how far around a question mark its answer is looked for
_SLACK = 2.0                 # the hook and the transcript do not share a clock tick


# ---------------------------------------------------------------- what it last said
# Every text below is an agent's: it can be anything, and a regular expression holds the
# whole server while it runs. So a text is cut to its end first, what is open-ended is read
# in one pass, and an expression is kept only where it cannot start over many times.
_HEADING = re.compile(r"\s{0,3}#{1,6}\s")
_CELL = re.compile(r":?-+:?")
_OPENING = re.compile(r"[`«“\"]|(?<!\w)['‘](?=\S)")
# for each opening sign: what stops the search for its closing sign, and the closing signs
_SINGLE = (re.compile(r"(?<=\S)['’](?!\w)|\n"), "'’")
_CLOSING = {"`": (re.compile(r"[`\n]"), "`"), "«": (re.compile(r"[«»]"), "»"),
            "“": (re.compile(r"[“”]"), "”"), '"': (re.compile(r"[\"\n]"), '"'),
            "'": _SINGLE, "‘": _SINGLE}
_QUOTE_MAX = 300             # longest text between single quotes: the sign is also an apostrophe
# "e" followed by a combining accent, as some systems write "é". Only these: putting a whole
# text in normal form is slow when it stacks accents.
_COMPOSED = [(unicodedata.normalize("NFD", c), c) for c in "àâäçéèêëîïôöùûüÀÂÄÇÉÈÊËÎÏÔÖÙÛÜ"]
_MARK = re.compile(r"\?(?=[\s?!.,;:*_)\]»”\"'’]|$)")     # not the ? of a web address
_LIST = re.compile(r"\s*(?:[-*•+]|#{1,6})\s+")
_STOP = re.compile(r"(?<![.!?…])[.!?…]+(?=[\s*_)\]]|$)|\n")
_LEAD = re.compile(r"[\s*_)\]]*")
_REPLY = re.compile(r"(?:[-•+]\s+)?[*_]*(?:(?:réponse|r)\s*:|(?:oui|non|yes|no)\b"
                    r"[\s*_]*(?=[,.:!;–—]|$))", re.IGNORECASE)
_WHY = re.compile(r"\bpourquoi\b", re.IGNORECASE)
_BECAUSE = re.compile(r"(?:parce\sque?\b|parce\squ['’]|car\b|pour\b)", re.IGNORECASE)
_ALONE = re.compile(r"^[\s*_]{0,9}(?:pourquoi|comment|combien|quand|qui|quoi|résultat)"
                    r"[\s*_]{0,9}$", re.IGNORECASE)


def _end_of(text):
    """The end of a text, the only part that is searched. One character more than TEXT_MAX,
    so that a last line that is too long is known to be."""
    text = text or ""
    if text[-1:].isspace() and len(text) > TEXT_MAX:
        text = text.rstrip()
    return text[-(TEXT_MAX + 1):]


def _compose(text):
    if "\u0300" in text or "\u0301" in text or "\u0302" in text or "\u0308" in text \
            or "\u0327" in text:
        for apart, one in _COMPOSED:
            text = text.replace(apart, one)
    return text


def _clean(line):
    """A line as a person reads it: same words, without the Markdown signs around them."""
    m = _LIST.match(line)
    return (line[m.end():] if m else line).replace("**", "").replace("`", "").strip()


def _mask(text):
    """The text with its quoted parts blanked out, at the same length. One pass: a closing
    sign already found is not looked for again."""
    out, done, at, ahead, size = [], 0, 0, {}, len(text)
    m = _OPENING.search(text)
    while m:
        i = m.start()
        at = i + 1
        stop, signs = _CLOSING[text[i]]
        end = ahead.get(signs, -1)
        if end < at:
            m = stop.search(text, at)
            end = ahead[signs] = m.start() if m else size
        if end < size and text[end] in signs \
                and not (stop is _SINGLE[0] and end - at > _QUOTE_MAX):
            quoted = text[i:end + 1]
            out.append(text[done:i])
            out.append("x" * len(quoted) if "\n" not in quoted else
                       "\n".join(["x" * len(p) for p in quoted.split("\n")]))
            done = at = end + 1
        m = _OPENING.search(text, at)
    return "".join(out) + text[done:]


def _cut(text):
    return text if len(text) <= SHOWN_MAX else text[:SHOWN_MAX].rstrip() + "…"


def declared(text):
    """(kind, sentence) from the ÉTAT line an answer ends with, else None. A line that
    says FINI or PAUSE and asks something all the same is a question."""
    last = _end_of(text).rstrip().rpartition("\n")[2]
    if len(last) > TEXT_MAX:
        return None
    last = _compose(last).strip()
    m = _LIST.match(last)
    i, j = m.end() if m else 0, len(last)
    while j - i > 1 and last[i] == last[j - 1] and last[i] in "*_`":
        i, j = i + 1, j - 1
        while i < j and last[i].isspace():
            i += 1
        while i < j and last[j - 1].isspace():
            j -= 1
    last = last[i:j]
    m = ETAT_LINE.fullmatch(last.replace("__", "").replace("*", "").replace("`", "").strip())
    if not m:
        return None
    kind = "question" if _MARK.search(_mask(last)) else m.lastgroup
    return kind, m.group(m.lastgroup).strip()


def _is_rule(line):
    """The line of dashes under the head of a table."""
    cells = line.strip()
    cells = cells[1:] if cells.startswith("|") else cells
    cells = (cells[:-1] if cells.endswith("|") else cells).split("|")
    return len(cells) > 1 and all(_CELL.fullmatch(c.strip()) for c in cells)


def _tables(lines):
    """Which lines belong to a table: a row between two bars, and the lines with a bar around
    a rule without them."""
    table, run, rule = set(), [], False
    for i, ln in enumerate(lines + [""]):
        if "|" not in ln:
            if rule:
                table.update(run)
            run, rule = [], False
            continue
        run.append(i)
        row = ln.strip()
        if len(row) > 1 and row[0] == row[-1] == "|":
            table.add(i)
        elif not rule:
            rule = _is_rule(ln)
    return table


def _blocks(text):
    """The paragraphs and lists of a text, without what is not the agent asking: code
    blocks, quoted lines, tables, and headings with something under them."""
    out, block, fenced = [], [], False
    lines = text.replace("\r", "").replace("**", "").split("\n")
    table = _tables(lines)
    full = next((i for i in range(len(lines) - 1, -1, -1) if lines[i].strip()), -1)
    for i, ln in enumerate(lines + [""]):
        words = ln.strip()
        if words.startswith(("```", "~~~")):
            fenced = not fenced
            words = ""
        elif fenced or i in table or words.startswith(">"):
            words = ""
        elif words.startswith("#") and i < full and _HEADING.match(ln):
            words = ""
        if words:
            block.append(ln)
        elif block:
            out.append("\n".join(block))
            block = []
    return out


def _stops(masked):
    """Where the sentences of a paragraph end."""
    return [m.end() for m in _STOP.finditer(masked)]


def _answered(masked, at, after, stops):
    """Does the text answer its own question? `at` is where its question mark is and
    `after` the next paragraph. "Oui ou non", or a choice between the two, is no answer."""
    i = bisect.bisect_right(stops, at)
    start = max(stops[i - 1] if i else 0, at - _NEAR, 0)
    if masked[at - 1:at] in (".", "!", "?", "…"):
        start = at            # "!?": the question has ended already
    asked, rest = masked[start:at], masked[at + 1:at + 1 + _NEAR]
    if not rest.strip(" \n*_)]"):
        rest = after[:_NEAR]
    if _ALONE.match(asked) and rest.strip() and "?" not in rest.split("\n")[0]:
        return True
    first = _LEAD.match(rest).end()
    if _BECAUSE.match(rest, first) and _WHY.search(asked):
        return True
    return bool(_REPLY.match(rest, first)) and not (re.search(r"\boui\b", rest, re.I)
                                             and re.search(r"\bnon\b", rest, re.I))


def question_in(text):
    """What a text asks, word for word, or None: the paragraph (or list) its last question
    is in, so that a question keeps the sentence it depends on and a list of questions
    stays whole, then the last paragraph that asks without a question mark (ASKING). A
    long one is reduced to the sentences that ask. Quoted words do not count."""
    blocks = _blocks(_compose(_end_of(text)[-TEXT_MAX:]))
    asks, said, after, left = None, None, "", MARKS_MAX
    for i in range(len(blocks) - 1, -1, -1):
        masked = _mask(blocks[i])
        if not asks and left > 0:
            marks = [m.start() for m in _MARK.finditer(masked)][-min(100, left):]
            left -= len(marks)
            stops = _stops(masked) if marks else []
            marks = [at for at in marks if not _answered(masked, at, after, stops)]
            asks = (i, masked, marks) if marks else None
        if not said:
            found = [m.start("r") if m.group("r") else m.start()
                     for m in ASKING.finditer(masked)]
            said = (i, masked, found) if found else None
        if asks and said:
            break
        after = masked
    out = []
    for i in sorted({x[0] for x in (asks, said) if x}):
        whole = "\n".join(_clean(b) for b in blocks[i].split("\n"))
        if len(whole) > QUESTION_MAX:
            here = [x for x in (asks, said) if x and x[0] == i]
            whole = _sentences(blocks[i], here[0][1], sorted(
                at for x in here for at in x[2])) or whole
        out.append(whole)
    return _cut("\n\n".join(out)) if out else None


def _sentences(block, masked, at):
    """The sentences of a paragraph where something is asked: `at` says where, in order."""
    out, start, n = [], 0, 0
    for end in _stops(masked) + [len(masked)]:
        if n < len(at) and at[n] < end:
            out.append(_clean(block[start:end]))
        while n < len(at) and at[n] < end:
            n += 1
        start = end
    return "\n".join(out)


def last_paragraph(text):
    lines = []
    for ln in reversed(_end_of(text).rstrip()[-TEXT_MAX:].replace("\r", "").split("\n")):
        if ln.strip():
            lines.append(_clean(ln))
        elif lines:
            break
    return _cut("\n".join(reversed(lines)))


@functools.lru_cache(maxsize=300)
def _reading(end):
    """(declared, question, last paragraph) of the end of a text. The board asks every 2 s
    and an answer does not change once it is written: it is read once."""
    line = declared(end)
    question = None if line else question_in(end)
    return line, question, "" if line or question else last_paragraph(end)


# ---------------------------------------------------------------- status
def view(hook, facts, now):
    """How one agent stands. `hook` is what the hooks reported: state, activity, since
    and seen (epoch seconds). `facts` comes from transcripts.facts().

    Returns status, since (epoch), question, options, note ('fini' or 'en pause'),
    text (what it last said, when it asks nothing) and source: hook, ask, declared
    (its own ÉTAT line) or auto (guessed from its words)."""
    out = {"status": "repos", "since": hook["since"], "question": None, "options": [],
           "note": None, "text": "", "source": "hook"}
    state, phase, ts = hook["state"], facts["phase"], facts["ts"]

    if state == "blocked" and not (facts["ask"] and not facts["open_tools"]):
        # The permission stands until the transcript shows the turn went on without it
        # (refusing one sends no hook event).
        if not (phase and ts and ts > hook["since"] + _SLACK and not facts["open_tools"]):
            out.update(status="permission", question=hook["activity"] or None)
            return out

    if facts["ask"]:
        out.update(status="decision", source="ask", since=facts["ask_ts"] or hook["since"],
                   question=_cut("\n".join(q["question"] for q in facts["ask"])),
                   options=[o for q in facts["ask"] for o in q["options"]][:12])
        return out

    if phase is None:         # nothing to read: only the hook to go by
        if state == "working" and hook["activity"] != "session started" \
                and now - hook["seen"] <= SUBAGENT_SECS:
            out["status"] = "travaille"
        return out

    if phase == "working":
        stopped = state == "waiting" and ts and hook["seen"] > ts + _SLACK
        if not stopped and now - max(ts or 0, hook["seen"]) <= SILENT_SECS:
            out["status"] = "travaille"
        out["since"] = ts or hook["since"]
        return out

    # the turn is over
    out["since"] = facts["text_ts"] or ts or hook["since"]
    line, asked, words = _reading(_end_of(facts["text"]))
    if line and line[0] == "question":
        out.update(status="decision", source="declared", question=_cut(line[1]))
        return out
    if line:
        out.update(source="declared", text=_cut(line[1]),
                   note="fini" if line[0] == "fini" else "en pause")
    else:
        out["source"] = "auto"
        if asked:
            out.update(status="decision", question=asked)
            return out
        out["text"] = words
    if state == "working" and str(hook["activity"] or "").startswith("using ") \
            and now - hook["seen"] <= SUBAGENT_SECS:
        out.update(status="travaille", since=hook["since"], note=None)
    return out


UNREADABLE = "Sa conversation n'a pas pu être lue."


def urgency_order(agents):
    """The agents that need the operator, most urgent first. The ONLY place that decides
    the order: permissions, then the longest wait."""
    waiting = [a for a in agents if a["status"] in ("permission", "decision")]
    return sorted(waiting, key=lambda a: (a["status"] != "permission", -a["wait_secs"],
                                          a["session_id"]))


# ---------------------------------------------------------------- departments
NO_TEAM = "sans-equipe"


FILE_MAX = 1000 * 1000       # a bigger departments.json is not read
def _label(value, size):
    return transcripts.plain(" ".join(str(value or "").split()))[:size]


def _texts(value):
    """The texts of a list, each one once."""
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(_label(v, 120) for v in value
                              if isinstance(v, str) and v.strip()))


def load_departments(path):
    """The local departments file (see examples/departments.json). Read on demand, so an
    edit applies without a restart. A file that is missing or broken leaves every agent
    in "Sans équipe", and `error` says why."""
    out = {"owners": [], "manager_label": "Master Orchestrator", "manager_to_create": [],
           "departments": [], "error": None}
    if not os.path.exists(path):
        out["error"] = "Aucun fichier departments.json : tous les agents sont sans équipe."
        return out
    try:
        with open(path, "rb") as f:
            raw = f.read(FILE_MAX + 1)
        if len(raw) > FILE_MAX:
            out["error"] = "departments.json dépasse 1 Mo : il n'est pas lu."
            return out
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("il doit contenir un objet JSON")
    except RecursionError:
        out["error"] = "departments.json est illisible (trop imbriqué)."
        return out
    except (OSError, ValueError) as e:
        out["error"] = transcripts.plain("departments.json est illisible (%s)." % str(e)[:160])
        return out
    out["owners"] = _texts(data.get("owners"))
    boss = data.get("manager") if isinstance(data.get("manager"), dict) else {}
    out["manager_label"] = _label(boss.get("label"), 80) or out["manager_label"]
    out["manager_to_create"] = _texts(boss.get("to_create"))
    listed = data.get("departments")
    ids, labels, dropped = {NO_TEAM: 1}, set(), 0
    for i, d in enumerate(listed if isinstance(listed, list) else []):
        label = _label(d.get("label"), 80) if isinstance(d, dict) else ""
        if not label:
            continue
        if _key(label) in labels:
            dropped += 1
            continue
        labels.add(_key(label))
        base = re.sub(r"[^a-z0-9-]+", "-", str(d.get("id") or "")[:80].lower()).strip("-") \
            or "d%d" % i
        did = base
        while did in ids:
            ids[base] = ids.get(base, 1) + 1
            did = "%s-%d" % (base, ids[base])
        ids[did] = 1
        out["departments"].append(dict(
            {k: _texts(d.get(k)) for k in
             ("includes", "roles", "to_create", "agents", "words", "folders")},
            id=did, label=label))
    if dropped:
        out["error"] = "departments.json : %d département%s en double, ignoré%s." % (
            dropped, *("s" if dropped > 1 else "",) * 2)
    return out


def _key(text):
    return " ".join(unicodedata.normalize("NFC", str(text or "")).casefold().split())


def _has_word(name, word):
    """Is `word` in `name` as whole words?"""
    at = name.find(word) if word else -1
    while at >= 0:
        end = at + len(word)
        if not re.match(r"\w", name[at - 1:at]) and not re.match(r"\w", name[end:end + 1]):
            return True
        at = name.find(word, at + 1)
    return False


def department_of(title, session_id, cwd, departments):
    """The department id of one agent, or NO_TEAM. An agent named in the file wins, then
    a word of its title, then the folder it works in."""
    name = _key(title)
    for d in departments:
        if session_id in d["agents"] or (name and name in [_key(a) for a in d["agents"]]):
            return d["id"]
    for d in departments:
        for w in d["words"] + d["includes"]:
            if _has_word(name, _key(w)):
                return d["id"]
    folder = _key(str(cwd or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1])
    for d in departments:
        if folder and folder in [_key(f) for f in d["folders"]]:
            return d["id"]
    return NO_TEAM


def public(tree):
    """What the page needs to draw the tree, without the matching rules."""
    out = {k: tree[k] for k in ("owners", "manager_label", "manager_to_create", "error")}
    out["departments"] = [{k: d[k] for k in ("id", "label", "includes", "roles", "to_create")}
                          for d in tree["departments"]]
    return out
