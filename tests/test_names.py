"""Tests for agent names and `resume`: every launch passes --name and titles the window,
and resume never leaves the model or the name to a default. Standard library only:

    python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="office-names-test-")
os.environ.setdefault("OFFICE_DB", os.path.join(_TMP, "office.db"))
os.environ.setdefault("OFFICE_CONFIG", os.path.join(_TMP, "config.json"))
os.environ["OFFICE_MODEL_LOG"] = os.path.join(_TMP, "model_log.jsonl")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import models  # noqa: E402
import office  # noqa: E402
from set_model_table import TABLE, OPUS48, SONNET  # noqa: E402


class Names(unittest.TestCase):
    def test_three_to_six_words_and_no_shell_syntax(self):
        n = models.short_name("Fix and deploy The Office (repo C:/Users/faroo/Documents/the-office)")
        self.assertEqual(n, "Fix and deploy The Office")
        self.assertEqual(models.short_name("one two three four five six seven eight"),
                         "one two three four five six")
        n = models.short_name('Build "the" site & rm -rf | ^%PATH% > x')
        for bad in '"&|^%<>':
            self.assertNotIn(bad, n)
        self.assertEqual(models.short_name("", fallback="clients"), "clients")
        self.assertEqual(models.short_name("::: ((("), "Agent")

    def test_unique_adds_a_number(self):
        self.assertEqual(models.unique_name("Fix cart", ["other"]), "Fix cart")
        self.assertEqual(models.unique_name("Fix cart", ["fix CART"]), "Fix cart-2")
        self.assertEqual(models.unique_name("Fix cart", ["Fix cart", "Fix cart-2"]), "Fix cart-3")


def write_config(**cfg):
    with open(os.environ["OFFICE_CONFIG"], "w", encoding="utf-8") as f:
        json.dump(cfg, f)


class Launch(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.popen = office.subprocess.Popen
        office.subprocess.Popen = lambda args, **kw: self.calls.append((args, kw))
        os.environ["OFFICE_ALLOW_SPAWN"] = "1"
        write_config(permission_mode="auto", agent_args=["--model", "sonnet"], model_rules=TABLE)
        conn = office._db()
        with conn:
            conn.execute("DELETE FROM agents")
            conn.execute("DELETE FROM spawns")
        conn.close()
        self.folder = os.path.join(_TMP, "PORTFOLIO")
        os.makedirs(os.path.join(self.folder, ".claude"), exist_ok=True)
        with open(os.path.join(self.folder, ".claude", "settings.json"), "w") as f:
            json.dump({"hooks": {"Stop": [{"hooks": [{"command": "python %s/hook.py"
                                                       % os.path.basename(ROOT)}]}]}}, f)

    def tearDown(self):
        office.subprocess.Popen = self.popen

    def last(self):
        args, kw = self.calls[-1]
        return args, kw

    @staticmethod
    def flag(args, name):
        return args[args.index(name) + 1]

    def test_spawn_names_the_session_and_the_window(self):
        r = office.spawn_agent({"cwd": self.folder, "task": "Fix the checkout button. Details..."})
        self.assertEqual(r["name"], "Fix the checkout button")
        args, kw = self.last()
        if sys.platform.startswith("win"):
            self.assertEqual(args[:4], ["cmd", "/c", "start", "Fix the checkout button"])
        self.assertEqual(self.flag(args, "--name"), "Fix the checkout button")
        self.assertEqual(kw["env"]["CLAUDE_CODE_DISABLE_TERMINAL_TITLE"], "1")

    def test_second_agent_with_the_same_title_gets_a_number(self):
        a = office.spawn_agent({"cwd": self.folder, "task": "Fix the checkout button"})
        b = office.spawn_agent({"cwd": self.folder, "task": "Fix the checkout button"})
        self.assertEqual((a["name"], b["name"]), ("Fix the checkout button",
                                                  "Fix the checkout button-2"))

    def test_name_override_and_manager_name(self):
        r = office.spawn_agent({"cwd": self.folder, "task": "x y z", "name": "Hogue roofing site"})
        self.assertEqual(r["name"], "Hogue roofing site")
        self.assertEqual(self.flag(self.last()[0], "--name"), "Hogue roofing site")

    def test_name_is_not_taken_by_a_live_board_title(self):
        office.report({"session_id": "live1", "hook_event_name": "SessionStart",
                       "cwd": self.folder, "task": "Fix the checkout button"})
        r = office.spawn_agent({"cwd": self.folder, "task": "Fix the checkout button"})
        self.assertEqual(r["name"], "Fix the checkout button-2")


class Resume(unittest.TestCase):
    def setUp(self):
        Launch.setUp(self)
        office.report({"session_id": "11111111-aaaa-bbbb-cccc-222222222222",
                       "hook_event_name": "SessionStart", "cwd": self.folder,
                       "task": "Plan the Lunoor roadmap"})

    def tearDown(self):
        Launch.tearDown(self)

    def test_resume_names_models_and_never_inherits(self):
        r = office.resume_agent({"target": "Lunoor", "message": "carry on"})
        self.assertTrue(r["ok"], r)
        args, _ = Launch.last(self)
        self.assertEqual(args[args.index("--resume") + 1], "11111111-aaaa-bbbb-cccc-222222222222")
        self.assertEqual(Launch.flag(args, "--name"), "Plan the Lunoor roadmap")
        self.assertEqual(Launch.flag(args, "--model"), "claude-opus-5-5")   # strategy row
        self.assertEqual(Launch.flag(args, "--permission-mode"), "auto")
        self.assertEqual(args[-1], "carry on")
        if sys.platform.startswith("win"):
            self.assertEqual(args[3], "Plan the Lunoor roadmap")

    def test_resume_uses_the_original_task_of_a_spawned_agent(self):
        r = office.spawn_agent({"cwd": self.folder, "task": "Build the Nordvia demo hero"})
        office.report({"session_id": "33333333-aaaa-bbbb-cccc-444444444444",
                       "hook_event_name": "SessionStart", "cwd": self.folder,
                       "task": "Hero work"})
        r = office.resume_agent({"target": "33333333"})
        self.assertEqual(r["model"], OPUS48)
        self.assertEqual(r["type"], "build / demo / 3D / animation")

    def test_resume_overrides(self):
        r = office.resume_agent({"target": "Lunoor", "model": "claude-sonnet-5-5",
                                 "name": "Lunoor v2"})
        self.assertEqual((r["model"], r["name"], r["type"]), (SONNET, "Lunoor v2", "override"))

    def test_resume_name_is_unique_but_not_against_itself(self):
        r = office.resume_agent({"target": "Lunoor"})
        self.assertEqual(r["name"], "Plan the Lunoor roadmap")        # own title is not a clash
        office.report({"session_id": "55555555-aaaa-bbbb-cccc-666666666666",
                       "hook_event_name": "SessionStart", "cwd": self.folder,
                       "task": "Plan the Lunoor roadmap v2"})
        r = office.resume_agent({"target": "55555555", "name": "Plan the Lunoor roadmap"})
        self.assertEqual(r["name"], "Plan the Lunoor roadmap-2")

    def test_resume_a_closed_session_by_full_id(self):
        sid = "77777777-aaaa-bbbb-cccc-888888888888"
        saved = office._transcript_cwd
        office._transcript_cwd = lambda s: (self.folder, "x") if s == sid else (None, None)
        try:
            r = office.resume_agent({"target": sid, "name": "Old work"})
        finally:
            office._transcript_cwd = saved
        self.assertTrue(r["ok"], r)
        self.assertEqual(Launch.flag(self.last_args(), "--resume"), sid)

    def last_args(self):
        return Launch.last(self)[0]

    def test_resume_errors(self):
        self.assertFalse(office.resume_agent({"target": "nobody-here"})["ok"])
        self.assertFalse(office.resume_agent({"target": ""})["ok"])
        self.assertFalse(office.resume_agent({"target": "Lunoor", "model": "--bad"})["ok"])
        n = len(self.calls)
        office.resume_agent({"target": "nobody-here"})
        self.assertEqual(len(self.calls), n)

    def test_resume_warns_when_the_old_window_is_alive_and_kills_nothing(self):
        conn = office._db()
        with conn:
            conn.execute("UPDATE agents SET pid=?", (os.getpid(),))
        conn.close()
        saved = office._PROCESS_NAME
        office._PROCESS_NAME = "python"
        try:
            r = office.resume_agent({"target": "Lunoor"})
        finally:
            office._PROCESS_NAME = saved
        self.assertIn("still open", r["warning"])
        self.assertTrue(r["ok"])


class TranscriptLookup(unittest.TestCase):
    def test_finds_the_folder_of_a_closed_session(self):
        home = tempfile.mkdtemp(prefix="office-home-")
        sid = "99999999-aaaa-bbbb-cccc-000000000000"
        d = os.path.join(home, ".claude", "projects", "C--x")
        os.makedirs(d)
        with open(os.path.join(d, sid + ".jsonl"), "w") as f:
            f.write(json.dumps({"type": "summary"}) + "\n")
            f.write(json.dumps({"type": "user", "cwd": "C:/x/y"}) + "\n")
        saved = os.path.expanduser
        office.os.path.expanduser = lambda p: p.replace("~", home)
        try:
            self.assertEqual(office._transcript_cwd(sid)[0], "C:/x/y")
            self.assertEqual(office._transcript_cwd("not-a-uuid"), (None, None))
        finally:
            office.os.path.expanduser = saved


if __name__ == "__main__":
    unittest.main()
