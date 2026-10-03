"""Tests for the team channel: broadcast messages, live re-delivery of the team rules,
and prompt triggers. Standard library only:

    python -m unittest discover -s tests

Nothing is started: office.py's handlers are called directly and hook.py's pure
functions are imported.
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="office-team-test-")
os.environ.setdefault("OFFICE_DB", os.path.join(_TMP, "office.db"))
os.environ.setdefault("OFFICE_CONFIG", os.path.join(_TMP, "config.json"))
sys.path.insert(0, ROOT)
import office  # noqa: E402
import hook    # noqa: E402


def desk(sid, folder, title, event="UserPromptSubmit"):
    r = office.report({"session_id": sid, "hook_event_name": event,
                       "cwd": os.path.join(_TMP, folder), "task": title})
    assert r["ok"], r


def reset():
    conn = office._db()
    with conn:
        conn.execute("DELETE FROM agents")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM meta")
    conn.close()


class Broadcast(unittest.TestCase):
    def setUp(self):
        reset()
        desk("sender-0001", "api", "Refactor auth")
        desk("peer-000001", "webapp", "Checkout flow")
        desk("peer-000002", "webapp", "Pricing page")
        desk("peer-000003", "docs", "Release notes")

    def test_all_reaches_every_other_desk_once(self):
        r = office.send_message({"from_session": "sender-0001", "to": "all",
                                 "text": "commit when the operator says good job"})
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["broadcast"])
        self.assertEqual(r["count"], 3)
        for sid in ("peer-000001", "peer-000002", "peer-000003"):
            got = office.pending_messages(sid, name="webapp")
            self.assertEqual(len(got), 1, (sid, got))
            self.assertEqual(got[0]["text"], "commit when the operator says good job")
            self.assertIn("Refactor auth", got[0]["from_name"])
        # the sender gets nothing, and a second read finds nothing new
        self.assertEqual(office.pending_messages("sender-0001", name="api"), [])
        self.assertEqual(office.pending_messages("peer-000001", name="webapp"), [])

    def test_all_with_nobody_else_is_an_error(self):
        reset()
        desk("sender-0001", "api", "Alone")
        r = office.send_message({"from_session": "sender-0001", "to": "all", "text": "hi"})
        self.assertFalse(r["ok"])
        self.assertIn("no other agent", r["error"])

    def test_a_desk_literally_titled_all_is_not_a_match(self):
        desk("peer-000004", "ops", "Install all the deps")
        r = office.send_message({"from_session": "sender-0001", "to": "all", "text": "x"})
        self.assertTrue(r.get("broadcast"))
        self.assertEqual(r["count"], 4)


class RulesChange(unittest.TestCase):
    def setUp(self):
        reset()
        desk("me-00000001", "api", "Refactor auth", event="SessionStart")
        desk("peer-000001", "webapp", "Checkout flow")

    def test_rules_resent_only_when_the_fingerprint_moves(self):
        start = office.team({"session_id": "me-00000001", "force": True, "rules_sig": "v1"})
        self.assertTrue(start["rules_changed"])      # first time this session reads them
        same = office.team({"session_id": "me-00000001", "rules_sig": "v1"})
        self.assertFalse(same["rules_changed"])
        self.assertEqual(same["text"], "")           # roster unchanged: nothing to say
        moved = office.team({"session_id": "me-00000001", "rules_sig": "v2"})
        self.assertTrue(moved["rules_changed"])
        again = office.team({"session_id": "me-00000001", "rules_sig": "v2"})
        self.assertFalse(again["rules_changed"])

    def test_a_session_that_never_confirmed_the_rules_gets_them(self):
        # a desk from before this feature, or one re-created after being reaped
        r = office.team({"session_id": "me-00000001", "rules_sig": "v1"})
        self.assertTrue(r["rules_changed"])

    def test_no_fingerprint_means_no_claim(self):
        r = office.team({"session_id": "me-00000001"})
        self.assertFalse(r["rules_changed"])


class Triggers(unittest.TestCase):
    def setUp(self):
        self.cfg = os.path.join(_TMP, "triggers.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"prompt_triggers": [
                {"match": r"\bgood job\b", "say": "The operator said good job: commit now."},
                {"match": "[unclosed", "say": "never"},
                {"match": r"\bship it\b"},
                "not a dict",
            ]}, f)

    def test_matches_case_insensitively(self):
        out = hook.prompt_triggers("Good job agent, next the footer", self.cfg)
        self.assertEqual(out, ["[The Office] The operator said good job: commit now."])

    def test_no_match_no_output(self):
        self.assertEqual(hook.prompt_triggers("do a good jobs board", self.cfg), [])
        self.assertEqual(hook.prompt_triggers("", self.cfg), [])
        self.assertEqual(hook.prompt_triggers(None, self.cfg), [])

    def test_bad_entries_and_missing_file_are_skipped(self):
        self.assertEqual(hook.prompt_triggers("ship it", self.cfg), [])
        self.assertEqual(hook.prompt_triggers("good job", os.path.join(_TMP, "none.json")), [])

    def test_team_rules_fingerprint(self):
        path = os.path.join(_TMP, "team.md")
        old = os.environ.get("OFFICE_TEAM_FILE")
        os.environ["OFFICE_TEAM_FILE"] = path
        try:
            self.assertEqual(hook.team_rules(), ("", ""))
            with open(path, "w", encoding="utf-8") as f:
                f.write("- rule one\n")
            text1, sig1 = hook.team_rules()
            with open(path, "a", encoding="utf-8") as f:
                f.write("- rule two\n")
            text2, sig2 = hook.team_rules()
            self.assertEqual(text1, "- rule one")
            self.assertIn("rule two", text2)
            self.assertNotEqual(sig1, sig2)
            self.assertEqual(len(sig1), 40)
        finally:
            if old is None:
                del os.environ["OFFICE_TEAM_FILE"]
            else:
                os.environ["OFFICE_TEAM_FILE"] = old


if __name__ == "__main__":
    unittest.main()
