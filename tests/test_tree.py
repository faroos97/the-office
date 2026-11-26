"""Tests for the tree board: names and turns read from the end of a transcript, the four
statuses, the order, the departments file and what the page is given. Every name and
message here is made up. Standard library only:

    python -m unittest discover -s tests
"""
import contextlib
import datetime
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="office-tree-test-")
os.environ.setdefault("OFFICE_DB", os.path.join(_TMP, "office.db"))
os.environ.setdefault("OFFICE_CONFIG", os.path.join(_TMP, "config.json"))
os.environ.setdefault("OFFICE_MODEL_LOG", os.path.join(_TMP, "model_log.jsonl"))
sys.path.insert(0, ROOT)
import office  # noqa: E402
import transcripts  # noqa: E402
import tree  # noqa: E402

BRIDGE = "cse_01AbCdEfGhIjKlMnOpQrStUv"
LINK = "https://claude.ai/code/session_01AbCdEfGhIjKlMnOpQrStUv"
NOW = time.time()
_n = [0]


def stamp(ago):
    return datetime.datetime.fromtimestamp(
        NOW - ago, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def said(text, ago=60, stop="end_turn"):
    _n[0] += 1
    return {"type": "assistant", "timestamp": stamp(ago), "message": {
        "id": "msg_%d" % _n[0], "stop_reason": stop,
        "content": [{"type": "text", "text": text}]}}


def used(name, ago=60, tid="t1", data=None):
    _n[0] += 1
    return {"type": "assistant", "timestamp": stamp(ago), "message": {
        "id": "msg_%d" % _n[0], "stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": data or {}}]}}


def result(tid="t1", ago=50):
    return {"type": "user", "timestamp": stamp(ago), "message": {"content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def asked(text, ago=90, **more):
    return dict({"type": "user", "timestamp": stamp(ago), "message": {"content": text}}, **more)


def closed(ago=55):
    return {"type": "system", "subtype": "turn_duration", "timestamp": stamp(ago)}


def head(title=None, ai=None, sid="s", bridge=BRIDGE):
    out = []
    if title:
        out.append({"type": "custom-title", "customTitle": title, "sessionId": sid})
    if ai:
        out.append({"type": "ai-title", "aiTitle": ai, "sessionId": sid})
    if bridge:
        out.append({"type": "bridge-session", "sessionId": sid, "bridgeSessionId": bridge})
    return out


def jsonl(entries):
    return "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for e in entries)


def write(entries, path=None):
    _n[0] += 1
    path = path or os.path.join(_TMP, "tr%d.jsonl" % _n[0])
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(jsonl(entries))
    return path


def facts(*entries):
    return transcripts.facts(write(entries))


def view(entries, state="waiting", activity="finished, waiting for you", since=55, seen=55):
    return tree.view({"state": state, "activity": activity, "since": NOW - since,
                      "seen": NOW - seen}, facts(*entries), NOW)


class Reading(unittest.TestCase):
    def test_custom_title_beats_ai_title_and_the_newest_wins(self):
        self.assertEqual(transcripts.title(write(
            head("Ancien", "Auto") + [said("x")] + head("Nouveau", "Auto deux"))), "Nouveau")
        self.assertEqual(transcripts.title(write(head(None, "Auto seul"))), "Auto seul")
        self.assertIsNone(transcripts.title(write([said("rien")])))
        self.assertIsNone(transcripts.title(os.path.join(_TMP, "absent.jsonl")))

    def test_words_inside_a_message_are_not_a_title(self):
        p = write(head("Vrai nom") + [said('"custom-title" {"customTitle":"Faux"}')])
        self.assertEqual(transcripts.title(p), "Vrai nom")

    def test_lines_from_end_match_the_file_at_every_chunk_size(self):
        p = write([said("é" * n) for n in (1, 50, 700, 3, 9000)])
        with open(p, "rb") as f:
            want = list(reversed(f.read().rstrip(b"\n").split(b"\n")))
        was = transcripts.CHUNK
        try:
            for size in (1, 7, 64, 1000, 1 << 20):
                transcripts.CHUNK = size
                got = list(transcripts.lines_from_end(p))
                self.assertEqual([x[1] for x in got], want)
                with open(p, "rb") as f:
                    for at, ln in got:
                        f.seek(at)
                        self.assertEqual(f.read(len(ln)), ln)
        finally:
            transcripts.CHUNK = was

    def test_a_half_written_last_line_is_ignored(self):
        p = write(head("Nom") + [said("Tu choisis ?")])
        with open(p, "ab") as f:
            f.write(b'{"type":"assistant","message":{"con')
        self.assertEqual(transcripts.facts(p)["phase"], "ended")
        self.assertEqual(transcripts.title(p), "Nom")

    def test_growth_keeps_the_name_and_a_new_name_is_seen(self):
        p = write(head("Nom") + [said("un")])
        self.assertEqual(transcripts.title(p), "Nom")
        with open(p, "a", encoding="utf-8") as f:
            f.write(jsonl([said("deux")]))
        self.assertEqual(transcripts.title(p), "Nom")
        with open(p, "a", encoding="utf-8") as f:
            f.write(jsonl(head("Autre nom")))
        self.assertEqual(transcripts.title(p), "Autre nom")

    def test_a_file_that_shrinks_is_read_again_from_nothing(self):
        p = write(head("Long nom de depart") + [said("x" * 500)])
        self.assertEqual(transcripts.title(p), "Long nom de depart")
        write([said("y")], p)
        self.assertIsNone(transcripts.title(p))

    def test_never_reads_further_than_the_cap(self):
        p = write(head("Trop loin") + [said("x" * 3000), said("fin")])
        was = transcripts.SCAN_MAX
        try:
            transcripts.SCAN_MAX = 1000
            self.assertIsNone(transcripts.title(p))
            self.assertEqual(transcripts.facts(p)["text"], "fin")
        finally:
            transcripts.SCAN_MAX = was

    def test_hundred_megabytes_stay_under_the_hook_timeout(self):
        p = os.path.join(_TMP, "cent.jsonl")
        chunk = jsonl([{"type": "api-request-blob", "data": "x" * 1000000}, asked("suite"),
                       used("Bash"), result(), said("y" * 2000)]).encode("utf-8")
        with open(p, "wb") as f:
            f.write(jsonl(head("Tout au début")).encode("utf-8"))
            while f.tell() < 100 * 1024 * 1024:
                f.write(chunk)
        size = os.path.getsize(p)
        t = time.perf_counter()
        self.assertIsNone(transcripts.title(p))      # the name is 100 MB back: worst case
        worst = time.perf_counter() - t
        with open(p, "ab") as f:
            f.write(jsonl([said("Oui ?")] + head("Près de la fin")).encode("utf-8"))
        transcripts._cache.clear()
        t = time.perf_counter()
        self.assertEqual(transcripts.title(p), "Près de la fin")
        usual = time.perf_counter() - t
        os.remove(p)
        print("\n  %d MB transcript: worst %.0f ms, usual %.1f ms" % (
            size // (1024 * 1024), worst * 1000, usual * 1000))
        self.assertLess(worst, 0.6)
        self.assertLess(usual, 0.6)


class Links(unittest.TestCase):
    def test_strict(self):
        self.assertEqual(transcripts.reply_url(("s", BRIDGE), "s"), LINK)
        for bad in (None, ("s", ""), ("s", "cse_court"), ("other", BRIDGE),
                    ("s", BRIDGE + "x"), ("s", BRIDGE + "\n"), ("s", BRIDGE + "/../x"),
                    ("s", "javascript:alert(1)"), ("s", "cse_01AbCdEfGhIjKlMnOpQr?t=v")):
            self.assertIsNone(transcripts.reply_url(bad, "s"), bad)


class Statuses(unittest.TestCase):
    def test_question_in_the_last_text_is_a_decision(self):
        v = view([asked("go"), said("Fait.\n\nJe publie la page **bleue** ?"), closed()])
        self.assertEqual((v["status"], v["source"], v["question"]),
                         ("decision", "auto", "Je publie la page bleue ?"))
        self.assertAlmostEqual(v["since"], NOW - 60, delta=1)

    def test_quoted_questions_do_not_count(self):
        for text in ("> Tu veux quoi ?\nC'est fait.", "Le mot « on y va ? » et c'est tout.",
                     "```\nquoi ?\n```\nFini.", "Voir `a ? b : c`.", 'Le titre "Prêt ?" est mis.',
                     "Voir https://exemple.test/p?x=1 pour le reste."):
            v = view([said(text), closed()])
            self.assertEqual((v["status"], v["question"]), ("repos", None), text)

    def test_asking_without_a_question_mark_is_a_decision(self):
        # the four waits a first version missed, told with other words
        for text, want in (
                ("Tout est prêt.\n\nIl me manque ton **accord**, ici, pour publier la recette.",
                 "Il me manque ton accord, ici, pour publier la recette."),
                ("Il me manque seulement ta réponse au point 3.",) * 2,
                ("La page est en ligne.\n\nIl te reste à choisir le four gris ou blanc.",
                 "Il te reste à choisir le four gris ou blanc."),
                ("Rien ne change : j’attends ta photo du comptoir.",) * 2,
                ("J'attends encore ton avis sur deux recettes :\n1. Le pain\n2. La tarte",
                 "J'attends encore ton avis sur deux recettes :\n1. Le pain\n2. La tarte"),
                ("Dis-moi laquelle tu gardes.",) * 2, ("C'est à toi de trancher.",) * 2,
                ("À toi de choisir.",) * 2, ("Regarde les deux. Réponds A ou B.",) * 2):
            v = view([said(text), closed()])
            self.assertEqual((v["status"], v["question"], v["source"]), ("decision", want, "auto"))

    def test_words_close_to_asking_are_not_a_request(self):
        for text in ("J'attends la QA.", "Il ne te manque rien.", "Je réponds demain.",
                     "Il me manque son avis.", "Je n'attends plus ta réponse.", "Il te reste 3 jours.",
                     "J'attendais ton oui, je l'ai.", "Ce n'est pas à toi de trancher.",
                     "Sa phrase « j'attends ton accord » est réglée.", "Le mot `réponds` reste.",
                     "> dis-moi si tu veux", "```\nil me manque ton accord\n```"):
            self.assertEqual(view([said(text), closed()])["status"], "repos", text)

    def test_a_question_that_is_not_asked_is_not_a_request(self):
        for text in ("Pourquoi ce choix ? Parce qu'il est simple.", "Ça marche, non ? Oui, testé.",
                     "**Est-ce prêt ?**\n\nOui, testé sur PC.", "Résultat ? Tout passe.",
                     "Q : On garde le bleu ?\nR : oui.", "# Pourquoi ce choix ?\n\nC'est plus simple.",
                     "'On garde ?' était la question d'hier.",
                     "Sa note disait 'on garde l'image ?' et c'est réglé.",
                     "| Q | R |\n|---|---|\n| On garde ? | oui |", "Q | R\n---|---\nOn garde ? | oui"):
            self.assertEqual(view([said(text), closed()])["status"], "repos", text)
        for text in ("Tu valides ? Oui ou non.", "Fini.\n\n## Tu valides ?",
                     "L'ami d'hier a dit 'oui' : tu valides ?", "On garde le bleu ? Captures jointes."):
            self.assertEqual(view([said(text), closed()])["status"], "decision", text)

    def test_hostile_text_does_not_slow_the_reading(self):
        start = time.time()
        for text in ("'" * 20000, "a ? " * 20000, "| " * 20000, "«" * 20000, "'a " * 10000,
                     "réponds " * 9000, "il te reste  " * 9000, "*" * 50000, "ÉTAT : " + " " * 50000):
            tree.question_in(text)
            tree.declared(text)
        self.assertLess(time.time() - start, 5)

    # unit texts an agent could repeat: signs that open and never close, tables, lists,
    # runs of marks, accents stacked on one letter
    UNITS = ("'", "‘", "’‘", "«", "«?", "“", '"', "`", "' a", " 'a", "« a ", "a ? ", "?", "? ", "?,",
             "??x", ".", "...x", "…!? ", "\n…!", "\n?", "\n\n?", "\n\nx?y", "pourquoi ? ",
             "oui ? non ", "? oui ", "?" + "_" * 399, "?" + " " * 399, "|", "| ", "|-", "|-|-|",
             "|-|-\n", "| a |\n", "a|b\n-|-\n", ":-", "-", "- ", "# \n", "> ", "```\n", "*", "* _",
             "\n", "\n \n", " ", "\t", "\xa0", "réponds ", ". réponds ", ".         ", "1) ",
             "dis-moi ", "dis-moi. ", "il te reste  ", "il me manque seulement ",
             "seulement ", "j'attends seulement encore ", "a", "\u0301", "\u0301\u0327", "e\u0301 ? ")

    @staticmethod
    def slowest(texts):
        """The worst of the best of three readings: a busy computer is not a slow reading."""
        worst = (0, "")
        for text in texts:
            best = 9
            for _ in range(3):
                t = time.perf_counter()
                tree._reading.__wrapped__(tree._end_of(text))
                tree.declared(text)
                tree.question_in(text)
                tree.last_paragraph(text)
                best = min(best, time.perf_counter() - t)
                if best < 0.05:
                    break
            worst = max(worst, (best, text[:30]))
        return worst

    def test_no_text_is_slow_to_read(self):
        tail = ("", "?", "x", "\nÉTAT : FINI : " + "* " * 9000)
        worst = self.slowest(
            before + (unit * (20000 // len(unit) + 1))[:20000] + after
            for unit in self.UNITS for before, after in (("Ok ? ", "x"), ("il te reste ", "?")))
        self.assertLess(worst[0], 0.6, worst[1])    # under 0.05 s, four readings, wide margin
        worst = self.slowest("*" * 9000 + "ÉTAT" + x + "*" * 9000 for x in tail)
        self.assertLess(worst[0], 0.6, worst[1])

    def test_a_text_of_one_megabyte_costs_what_its_end_costs(self):
        worst = self.slowest((unit * (1000000 // len(unit) + 1))[:1000000] for unit in self.UNITS)
        self.assertLess(worst[0], 0.6, worst[1])
        self.assertEqual(tree.question_in("a " * 500000 + "\n\nTu valides ?\n" + " \n" * 50000),
                         "Tu valides ?")
        self.assertIsNone(tree.declared("ÉTAT : FINI : " + "x" * 30000))
        self.assertIsNone(tree.declared("x" * 30000 + "ÉTAT : FINI : x".ljust(tree.TEXT_MAX, "x")))

    def test_an_answer_is_read_once_until_it_changes(self):
        calls = []
        with mock.patch.object(tree, "question_in", lambda text: calls.append(text)):
            for text in ("Lecture une fictive.", "Lecture une fictive.", "Lecture deux fictive."):
                for _ in range(3):
                    view([said(text), closed()])
        self.assertEqual(calls, ["Lecture une fictive.", "Lecture deux fictive."])

    def test_the_question_is_word_for_word(self):
        self.assertEqual(tree.question_in("- le bleu\n- le rouge\n\nTu choisis\nlequel ?\n\nMerci."),
                         "Tu choisis\nlequel ?")
        three = "Il me faut :\n1. Quelle couleur ?\n2. Combien de pages ?"
        self.assertEqual(tree.question_in("Fait.\n\n" + three + "\n\nFichiers :\n- a.txt"), three)
        self.assertEqual(tree.question_in("Le budget tient un mois. **Est-ce encore vrai?**"),
                         "Le budget tient un mois. Est-ce encore vrai?")
        long = "Une phrase qui explique. " * 30 + "Je commence lundi ? Merci. Ou mardi ?"
        self.assertEqual(tree.question_in(long), "Je commence lundi ?\nOu mardi ?")

    def test_etat_lines(self):
        for line, want in (
                ("ÉTAT : QUESTION : On garde le bleu", ("decision", None, "On garde le bleu")),
                ("ÉTAT : FINI : la page est en ligne", ("repos", "fini", None)),
                ("ÉTAT : PAUSE : attend le devis", ("repos", "en pause", None))):
            v = view([said("Tu veux voir ?\n\n" + line + "\n"), closed()])
            self.assertEqual((v["status"], v["note"], v["question"]), want)
            self.assertEqual(v["source"], "declared")

    def test_the_etat_line_tolerates_how_it_is_typed(self):
        for line in ("ETAT : FINI : x", "ÉTAT: FINI: x", "ÉTAT :FINI :x", "état : fini : x",
                     "Etat : Fini : x", "ÉTAT : FINI : x", "ÉTAT : FINI : x",
                     "ÉTAT  :  FINI  :  x", "**ÉTAT : FINI : x**", "**ÉTAT** : FINI : x",
                     "`ÉTAT : FINI : x`", "- ÉTAT : FINI : x", "* **ÉTAT : FINI : x**",
                     " ÉTAT : FINI : x", "ÉTAT : FINI : x  \n\n"):
            v = view([said("Tu veux voir ?\n" + line), closed()])
            self.assertEqual((v["status"], v["source"]), ("repos", "declared"), line)

    def test_a_declared_question_needs_no_question_mark(self):
        v = view([said("Voilà.\nÉTAT : QUESTION : On garde le bleu ou le vert"), closed()])
        self.assertEqual((v["status"], v["question"], v["source"]),
                         ("decision", "On garde le bleu ou le vert", "declared"))

    def test_fini_or_pause_that_asks_is_a_decision(self):
        for line, want in (("ÉTAT : FINI : tout est prêt, tu valides ?", "tout est prêt, tu valides ?"),
                           ("ÉTAT : PAUSE : attend ton choix, A ou B ?", "attend ton choix, A ou B ?")):
            v = view([said("Voilà.\n" + line), closed()])
            self.assertEqual((v["status"], v["question"]), ("decision", want))
        v = view([said("Voilà.\nÉTAT : FINI : la question « on garde ? » est réglée"), closed()])
        self.assertEqual(v["status"], "repos")

    def test_malformed_etat_lines_fall_back_to_detection(self):
        for line in ("ÉTAT : FINI :", "ÉTAT : FINI", "ÉTAT : PAUSE : le devis", "> ÉTAT : FINI : x",
                     "ÉTAT : FINI : x\nEt encore.", "ÉTAT : AUTRE : x", "Mon ÉTAT : FINI : x",
                     "```\nÉTAT : FINI : x\n```"):
            v = view([said("Tu veux voir ?\n\n" + line), closed()])
            self.assertEqual((v["status"], v["source"]), ("decision", "auto"), line)

    def test_a_decomposed_accent_is_the_same_line(self):
        self.assertEqual(tree.declared("E\u0301TAT : FINI : x"), ("fini", "x"))

    def test_a_new_message_cancels_the_line_and_the_question(self):
        for text in ("ÉTAT : QUESTION : On garde le bleu", "On garde le bleu ?"):
            for msg in (asked("oui", 5, origin={"kind": "human"}),
                        asked("Another session sent a message", 5, isMeta=True,
                              origin={"kind": "peer"}, promptSource="system"),
                        asked("[Cross-session idle notice]", 5, isMeta=True,
                              promptSource="system"),
                        {"type": "attachment", "timestamp": stamp(5),
                         "attachment": {"type": "queued_command", "prompt": "oui"}}):
                v = view([said(text), closed(), msg], "working", "new request", 5, 5)
                self.assertEqual((v["status"], v["question"]), ("travaille", None))

    def test_notes_from_the_harness_do_not_cancel_a_question(self):
        for note in (asked("[Image: source]", 5, isMeta=True),
                     asked("<local-command-stdout>ok</local-command-stdout>", 5),
                     asked("<command-name>/usage</command-name>", 5),
                     asked("This session is being continued", 5, isCompactSummary=True)):
            v = view([said("On garde le bleu ?"), closed(), note])
            self.assertEqual(v["status"], "decision")

    def test_the_end_of_a_background_task_does_not_cancel_a_question(self):
        for note in (asked("<task-notification>", 5, origin={"kind": "task-notification"}),
                     {"type": "attachment", "timestamp": stamp(5),
                      "attachment": {"type": "queued_command", "prompt": "fini",
                                     "commandMode": "task-notification"}}):
            v = view([said("On garde le bleu ?"), closed(), note], seen=1)
            self.assertEqual((v["status"], v["question"]), ("decision", "On garde le bleu ?"))
            v = view([said("On garde le bleu ?"), closed(), note, used("Bash", 2)], "working",
                     "using Bash", 2, 2)
            self.assertEqual(v["status"], "travaille")

    def test_ask_user_question(self):
        ask = used("AskUserQuestion", data={"questions": [
            {"question": "Quelle couleur ?", "options": [{"label": "Bleu"}, {"label": "Rouge"}]}]})
        for state in ("working", "blocked", "waiting"):
            v = view([asked("go"), ask], state, "using AskUserQuestion")
            self.assertEqual((v["status"], v["source"], v["question"], v["options"]),
                             ("decision", "ask", "Quelle couleur ?", ["Bleu", "Rouge"]))
        v = view([asked("go"), ask, result()], "working", "using AskUserQuestion", 5, 5)
        self.assertEqual(v["status"], "travaille")

    def test_permission_comes_before_the_line(self):
        v = view([asked("go"), used("Bash")], "blocked", "Claude needs your permission", 58, 58)
        self.assertEqual((v["status"], v["question"], v["source"]),
                         ("permission", "Claude needs your permission", "hook"))
        self.assertAlmostEqual(v["since"], NOW - 58, delta=1)
        v = view([said("ÉTAT : FINI : x", 60), closed(60)], "blocked", "permission", 58, 58)
        self.assertEqual(v["status"], "permission")

    def test_a_permission_the_turn_went_past_is_over(self):
        v = view([used("Bash", 60), result(ago=20), said("Refusé. Je fais quoi ?", 10), closed(9)],
                 "blocked", "permission", 58, 58)
        self.assertEqual(v["status"], "decision")

    def test_working_then_silent(self):
        v = view([asked("go"), used("Bash", 10)], "working", "using Bash", 10, 10)
        self.assertEqual(v["status"], "travaille")
        v = view([asked("go", 9000), used("Bash", 8000)], "working", "using Bash", 8000, 8000)
        self.assertEqual(v["status"], "repos")

    def test_interrupted_is_at_rest(self):
        stop = [{"type": "text", "text": "[Request interrupted by user for tool use]"}]
        v = view([used("Bash"), asked(stop, 20)])
        self.assertEqual((v["status"], v["question"]), ("repos", None))

    def test_without_a_transcript_the_hook_decides(self):
        for state, act, seen, want in (
                ("working", "using Bash", 5, "travaille"), ("working", "session started", 5, "repos"),
                ("working", "using Bash", 900, "repos"), ("waiting", "x", 5, "repos"),
                ("blocked", "permission", 5, "permission")):
            v = tree.view({"state": state, "activity": act, "since": NOW - seen,
                           "seen": NOW - seen}, transcripts.facts(None), NOW)
            self.assertEqual(v["status"], want)

    def test_odd_entries_do_not_break_the_reading(self):
        for line in ('[]', '"user"', '{"type":"user"}', '{"type":"assistant","message":null}',
                     '{"type":"user","message":"x"}', '{"type":"assistant","message":"x"}',
                     '{"type":"user","message":{"content":[null,3]}}',
                     '{"type":"assistant","message":{"content":[null,{"type":"tool_use",'
                     '"name":"AskUserQuestion","input":"x"}]}}',
                     '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"a",'
                     '"name":"AskUserQuestion","input":{"questions":3}}]}}',
                     '{"type":"assistant","message":{"content":[{"type":"text","text":3}]}}',
                     '{"type":"attachment","attachment":"queued_command"}',
                     '{"type":"bridge-session"}', 'not json "user"'):
            p = write([said("avant")])
            with open(p, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            view([json.loads(line)] if line.startswith("{") else [])
            transcripts.facts(p)

    def test_order(self):
        a = [{"session_id": s, "status": st, "wait_secs": w} for s, st, w in (
            ("a", "decision", 10), ("b", "repos", 9999), ("c", "decision", 500),
            ("d", "permission", 5), ("e", "travaille", 1), ("f", "permission", 50))]
        self.assertEqual([x["session_id"] for x in tree.urgency_order(a)], ["f", "d", "c", "a"])


class Departments(unittest.TestCase):
    def test_the_example_is_valid(self):
        org = tree.load_departments(os.path.join(ROOT, "examples", "departments.json"))
        self.assertIsNone(org["error"])
        self.assertTrue(org["owners"] and org["departments"])

    def test_missing_or_broken_file_says_so(self):
        self.assertIn("Aucun fichier", tree.load_departments(os.path.join(_TMP, "no.json"))["error"])
        p = os.path.join(_TMP, "bad.json")
        for text in ("{", "[]", '"x"', "\xff"):
            with open(p, "w", encoding="latin-1") as f:
                f.write(text)
            org = tree.load_departments(p)
            self.assertIn("illisible", org["error"])
            self.assertEqual(org["departments"], [])

    def test_odd_values_are_ignored(self):
        p = os.path.join(_TMP, "odd.json")
        with open(p, "w") as f:
            json.dump({"owners": "x", "manager": 3, "departments": [
                None, {}, {"label": "A", "id": "x"}, {"label": "B", "id": "x", "agents": "no"},
                {"label": "C", "id": "sans-equipe", "to_create": ["Chef", "Chef", 4]}]}, f)
        org = tree.load_departments(p)
        self.assertEqual([d["id"] for d in org["departments"]], ["x", "x-2", "sans-equipe-2"])
        self.assertEqual(org["departments"][2]["to_create"], ["Chef"])

    def departments(self, raw):
        p = os.path.join(_TMP, "damaged.json")
        with open(p, "wb") as f:
            f.write(raw)
        return tree.load_departments(p)

    def test_a_damaged_file_says_so_and_nothing_breaks(self):
        for raw in (b"", b"[]", b"null", b'{"departments":', b"\xff\xfe{", b"[" * 200000,
                    b'{"a":' + b"1" * 6000 + b"}"):
            org = self.departments(raw)
            self.assertEqual(org["departments"], [])
            self.assertTrue(org["error"])
            org["error"].encode("utf-8")

    def test_a_file_over_one_megabyte_is_not_read(self):
        big = json.dumps({"departments": [{"label": "A" * 1000 * 1000}]}).encode()
        self.assertIn("1 Mo", self.departments(big)["error"])

    def test_the_same_department_twice_is_kept_once_and_said(self):
        org = self.departments(json.dumps({"departments": [
            {"label": "Four"}, {"label": " four "}, {"label": "Comptoir"}]}).encode())
        self.assertEqual([d["label"] for d in org["departments"]], ["Four", "Comptoir"])
        self.assertIn("1 département en double", org["error"])
        start = time.time()
        org = self.departments(json.dumps({"departments": [{"id": "x", "label": str(i)}
                                                           for i in range(9000)]}).encode())
        self.assertEqual(len({d["id"] for d in org["departments"]}), 9000)
        self.assertLess(time.time() - start, 5)

    def test_half_characters_in_the_file_can_be_sent(self):
        org = self.departments(b'{"owners":["a\\ud83d"],"departments":[{"label":"b\\ud83d"}]}')
        json.dumps(tree.public(org), ensure_ascii=False).encode("utf-8")

    def test_classification(self):
        deps = [dict({k: [] for k in ("agents", "words", "includes", "folders")}, **d) for d in (
            {"id": "pub", "words": ["pub"], "includes": ["SEO"]},
            {"id": "site", "agents": ["Pub du site"], "folders": ["vitrine"]})]
        for title, cwd, want in (
                ("Pub du site", "/x", "site"), ("Une pub d'essai", "/x", "pub"),
                ("Publication", "/x", tree.NO_TEAM), ("Rapport seo", "/x", "pub"),
                ("Autre", "C:\\projets\\Vitrine\\", "site"), (None, None, tree.NO_TEAM)):
            self.assertEqual(tree.department_of(title, "s", cwd, deps), want, title)


class Board(unittest.TestCase):
    def setUp(self):
        self.real = office._PROCESS_NAME
        office._PROCESS_NAME = "python"
        with open(os.path.join(_TMP, "departments.json"), "w", encoding="utf-8") as f:
            json.dump({"departments": [{"id": "a", "label": "Atelier", "words": ["atelier"]}]}, f)
        os.environ["OFFICE_DEPARTMENTS"] = os.path.join(_TMP, "departments.json")
        self.clean()

    def tearDown(self):
        office._PROCESS_NAME = self.real
        os.environ.pop("OFFICE_DEPARTMENTS", None)
        self.clean()

    def clean(self):
        conn = office._db()
        with conn:
            conn.execute("DELETE FROM agents")
            conn.execute("DELETE FROM spawns")
        conn.close()

    def report(self, sid, event, entries=None, **more):
        p = dict({"session_id": sid, "hook_event_name": event, "cwd": "/projets/essai"}, **more)
        if entries is not None:
            p["transcript_path"] = write(entries, os.path.join(_TMP, sid + ".jsonl"))
        office.report(p)

    def agent(self, sid):
        return [a for a in office.board()["agents"] if a["session_id"] == sid][0]

    def row(self, sid):
        conn = office._db()
        try:
            return dict(conn.execute("SELECT * FROM agents WHERE session_id=?", (sid,)).fetchone())
        finally:
            conn.close()

    def test_no_agent(self):
        b = office.board()
        self.assertEqual(b["agents"], [])
        self.assertEqual((b["counts"]["asking"], b["counts"]["travaille"], b["counts"]["repos"]),
                         (0, 0, 0))
        self.assertEqual(b["tree"]["departments"][0]["label"], "Atelier")

    def test_fifteen_agents_are_all_there_and_ranked_once(self):
        for i in range(15):
            sid = "s%02d" % i
            self.report(sid, "Stop", head("Atelier %d" % i, sid=sid)
                        + [said("On continue ?" if i % 2 else "Fait.", 100 + i), closed()])
        b = office.board()
        self.assertEqual(len(b["agents"]), 15)
        ranked = sorted((a for a in b["agents"] if a["rank"]), key=lambda a: a["rank"])
        self.assertEqual([a["rank"] for a in ranked], list(range(1, 8)))
        self.assertEqual([a["session_id"] for a in ranked],
                         ["s13", "s11", "s09", "s07", "s05", "s03", "s01"])
        self.assertEqual({a["department"] for a in b["agents"]}, {"a"})
        self.assertEqual((b["counts"]["asking"], b["counts"]["repos"]), (7, 8))

    def test_title_order(self):
        self.report("s1", "Stop", head(None, "Nom automatique", sid="s1"))
        self.assertEqual(self.agent("s1")["title"], "Nom automatique")
        self.report("s1", "Stop", head("Nom donné", "Nom automatique", sid="s1"))
        self.assertEqual(self.agent("s1")["title"], "Nom donné")
        self.report("s2", "Stop", [asked("range les factures", origin={"kind": "human"})])
        self.assertEqual(self.agent("s2")["title"], "Range les factures")
        conn = office._db()
        with conn:
            conn.execute("INSERT INTO spawns (session_id, name, cwd, task) VALUES"
                         " ('s2', 'Nom de lancement', '/x', 'x')")
        conn.close()
        self.assertEqual(self.agent("s2")["title"], "Nom de lancement")

    def test_long_and_hostile_titles_come_through_whole(self):
        long = "Un titre très long " * 12
        self.report("s1", "Stop", head(long, sid="s1"))
        self.assertEqual(self.agent("s1")["title"], long.strip())
        evil = "<img src=x onerror=alert(1)>\"'</script>"
        self.report("s2", "Stop", head(evil, sid="s2"))
        self.assertEqual(self.agent("s2")["title"], evil)      # the page writes it as text

    def test_links(self):
        self.report("s1", "Stop", head("A", sid="s1"))
        self.report("s2", "Stop", head("B", sid="s2", bridge=None))
        self.report("s3", "Stop", head("C", sid="another"))
        self.assertEqual(self.agent("s1")["reply_url"], LINK)
        self.assertIsNone(self.agent("s2")["reply_url"])
        self.assertIsNone(self.agent("s3")["reply_url"])

    def test_session_start_does_not_erase_a_wait(self):
        ask = {"message": "Claude needs your permission", "notification_type": "permission_prompt"}
        for event, more, state in (("Stop", {}, "waiting"), ("Notification", ask, "blocked")):
            self.report("s1", event, [used("Bash")], pid=os.getpid(), **more)
            before = self.row("s1")
            self.assertEqual(before["state"], state)
            self.report("s1", "SessionStart", source="compact", pid=os.getpid())
            after = self.row("s1")
            for k in ("state", "needs_you", "activity", "state_since"):
                self.assertEqual(after[k], before[k], k)
            self.clean()

    def test_a_restarted_process_has_no_permission_prompt_left(self):
        self.report("s1", "Notification", [said("Fait.")], pid=111,
                    message="Claude needs your permission", notification_type="permission_prompt")
        self.report("s1", "SessionStart", source="resume", pid=222)
        self.assertEqual((self.row("s1")["state"], self.row("s1")["needs_you"]), ("waiting", 1))

    def test_a_new_session_still_starts_working(self):
        self.report("s1", "SessionStart")
        self.assertEqual(self.agent("s1")["state"], "working")
        self.assertEqual(self.agent("s1")["status"], "repos")

    def test_without_a_departments_file_everyone_is_without_a_team(self):
        os.environ["OFFICE_DEPARTMENTS"] = os.path.join(_TMP, "absent.json")
        self.report("s1", "Stop", head("Atelier", sid="s1"))
        self.assertEqual(self.agent("s1")["department"], tree.NO_TEAM)
        self.assertTrue(office.board()["tree"]["error"])

    def served(self, path, body=None, raw=None):
        """(HTTP status, answer) of a real server on a free port."""
        httpd = office.ThreadingHTTPServer(("127.0.0.1", 0), office.Handler)
        threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
        try:
            data = raw if raw is not None else (json.dumps(body).encode() if body else None)
            req = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (httpd.server_address[1], path), data,
                {"Content-Type": "application/json"} if data else {})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        finally:
            httpd.shutdown()
            httpd.server_close()

    DAMAGED = {
        "surrogate text": r'{"type":"assistant","message":{"id":"m","stop_reason":"end_turn",'
                          r'"content":[{"type":"text","text":"Fin \ud83d"}]}}',
        "surrogate title": r'{"type":"custom-title","customTitle":"Nom \ud83d","sessionId":"b"}',
        "nested": '{"type":"assistant","message":{"content":' + "[" * 100000 + "]" * 100000 + "}}",
        "list as id": '{"type":"user","message":{"content":[{"type":"tool_result",'
                      '"tool_use_id":["a"]}]}}',
        "object as id": '{"type":"assistant","message":{"id":"m","stop_reason":"tool_use",'
                        '"content":[{"type":"tool_use","id":{"a":1},"name":"Bash"}]}}',
        "odd types": '{"type":"assistant","message":{"id":[1],"stop_reason":["x"],"content":7}}',
        "message as text": '{"type":"assistant","message":"x"}',
        "text as object": '{"type":"assistant","message":{"content":[{"type":"text","text":{}}]}}',
    }

    def test_a_damaged_transcript_touches_only_its_own_agent(self):
        for kind, line in self.DAMAGED.items():
            self.clean()
            self.report("good", "Stop", head("Bon", sid="good") + [said("Tu valides ?"), closed()])
            path = write([said("Fait.")], os.path.join(_TMP, "b.jsonl"))
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            office.report({"session_id": "b", "hook_event_name": "Stop", "cwd": "/x",
                           "transcript_path": path})
            status, board = self.served("/api/agents")
            self.assertEqual((status, len(board["agents"])), (200, 2), kind)
            good = [a for a in board["agents"] if a["session_id"] == "good"][0]
            self.assertEqual((good["status"], good["title"]), ("decision", "Bon"), kind)
            for path in ("/api/conversation?session=b", "/api/conversation?session=good"):
                self.assertEqual(self.served(path)[0], 200, kind)

    def test_a_reading_that_fails_is_told_without_private_words(self):
        self.report("good", "Stop", head("Bon", sid="good") + [said("Tu valides ?"), closed()])
        self.report("bad", "Stop", head("Secret", sid="bad") + [said("Phrase privée ?"), closed()])
        real, err = transcripts.facts, io.StringIO()

        def broken(path):
            if path and "bad" in os.path.basename(path):
                raise KeyError("Phrase privée")
            return real(path)
        transcripts.facts = broken
        try:
            with contextlib.redirect_stderr(err):
                for _ in range(3):
                    agents = {a["session_id"]: a for a in office.board()["agents"]}
        finally:
            transcripts.facts = real
        self.assertEqual(agents["good"]["status"], "decision")
        self.assertEqual((agents["bad"]["status"], agents["bad"]["text"]),
                         ("repos", tree.UNREADABLE))
        self.assertEqual(err.getvalue().count("KeyError"), 1)     # once, not every 2 seconds
        self.assertNotIn("priv", err.getvalue())

    def test_odd_hook_events_never_fail(self):
        ok = {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/x"}
        for body in (dict(ok, session_id=["a"]), dict(ok, session_id={"a": 1}), dict(ok, session_id=7)):
            status, answer = self.served("/api/agents/report", body)
            self.assertEqual((status, answer["ok"]), (200, False))
        for more in ({"cwd": ["x"]}, {"task": {"a": 1}}, {"prompt": 3}, {"message": ["x"]},
                     {"model": {"a": 1}}, {"tool_name": [1]}, {"pid": "x"}, {"pid": 10 ** 30},
                     {"pid": {"a": 1}}, {"name": [1]}, {"hook_event_name": ["Stop"]},
                     {"transcript_path": {"a": 1}}, {"task": "coupé \ud83d", "name": "\udc00"},
                     {"transcript_path": "a\0b"}, {"verdict": [1]}, {"activity": [1]}):
            status, _ = self.served("/api/agents/report", dict(ok, **more))
            self.assertEqual(status, 200, more)
            self.assertEqual(self.served("/api/agents")[0], 200, more)
        for raw in (b"[" * 100000, b"[]", b"7", b"null", b"\xff"):
            self.assertEqual(self.served("/api/agents/report", raw=raw)[0], 200, raw[:9])

    def test_the_page_never_gets_the_matching_rules(self):
        self.assertNotIn("words", json.dumps(office.board()["tree"]))


class Page(unittest.TestCase):
    with open(os.path.join(ROOT, "board.html"), encoding="utf-8") as f:
        html = f.read()

    def test_the_reply_link_is_the_only_outside_address(self):
        self.assertEqual(re.findall(r"(?:src|href)\s*=\s*[\"'](https?:[^\"']+)", self.html), [])
        self.assertIn('target="_blank" rel="noopener noreferrer"', self.html)
        self.assertIn(r"/^https:\/\/claude\.ai\/code\/session_[A-Za-z0-9]{24}$/", self.html)

    def test_never_says_there_is_no_question(self):
        self.assertNotIn("Aucune question", self.html)
        self.assertIn("Cet agent n'a pas dit s'il attend quelque chose.", self.html)

    def test_an_older_server_shows_no_count(self):
        self.assertIn('<body class="nodata">', self.html)
        self.assertRegex(self.html, r"\.nodata \.key,.*\.nodata \.depts.*\{display:none\}")
        self.assertIn("Serveur à redémarrer", self.html)
        self.assertIn("if(!d.tree){", self.html)

    def test_a_phone_is_the_same_thing_in_the_style_and_the_script(self):
        query = "(max-width:760px),(max-height:500px) and (pointer:coarse)"
        self.assertIn('matchMedia("%s")' % query, self.html)
        self.assertIn("@media %s{" % query, self.html)

    def test_the_font_licence_is_in_the_repository(self):
        with open(os.path.join(ROOT, "licenses", "OFL-Atkinson-Hyperlegible-Next.txt"),
                  encoding="utf-8") as f:
            text = f.read()
        self.assertIn("The Atkinson Hyperlegible Next Project Authors", text)
        self.assertIn("SIL OPEN FONT LICENSE Version 1.1", text)
        self.assertIn("TERMINATION", text)
        self.assertIn("licenses/OFL-Atkinson-Hyperlegible-Next.txt", self.html)

    def test_no_reply_field(self):
        panel = self.html.split('id="panel"')[1].split("</aside>")[0]
        self.assertNotIn("<textarea", panel)
        self.assertNotIn("<input", panel)


if __name__ == "__main__":
    unittest.main()
