"""Tests for the task list and the runner. Standard library only:

    python -m unittest discover -s tests

No Claude session is started: the workers here are small Python scripts speaking the
runner's contract.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest import mock
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="office-test-")
os.environ["OFFICE_DB"] = os.path.join(_TMP, "office.db")
os.environ["OFFICE_CONFIG"] = os.path.join(_TMP, "config.json")
sys.path.insert(0, ROOT)
import office  # noqa: E402  (after the environment is set: office reads OFFICE_DB on import)

# A worker that records when it started and finished, then says done.
WORKER = """
import json, sys, time, os
task = json.loads(sys.stdin.read())
log = os.environ["TEST_LOG"]
with open(log, "a") as f: f.write("start %s %f\\n" % (task["ref"], time.time()))
time.sleep(float(os.environ.get("TEST_SLEEP", "0.8")))
with open(log, "a") as f: f.write("end %s %f\\n" % (task["ref"], time.time()))
print('##office result: ' + json.dumps({"status": "done", "summary": "ok"}))
"""


def write_config(cfg):
    with open(os.environ["OFFICE_CONFIG"], "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def reset():
    conn = office._db()
    with conn:
        conn.execute("DELETE FROM tasks")
    conn.close()


def add(title, folder, **extra):
    r = office.task_add(dict({"title": title, "dir": folder}, **extra))
    assert r["ok"], r
    return r["task"]


class Folders(unittest.TestCase):
    def test_overlap(self):
        a = os.path.join(_TMP, "a")
        self.assertTrue(office._dirs_overlap(a, a))
        self.assertTrue(office._dirs_overlap(a, os.path.join(a, "deep", "er")))
        self.assertTrue(office._dirs_overlap(os.path.join(a, "deep"), a))
        self.assertFalse(office._dirs_overlap(a, os.path.join(_TMP, "ab")))
        self.assertFalse(office._dirs_overlap(a, os.path.join(_TMP, "b")))
        self.assertTrue(office._dirs_overlap("", None))      # no folder: one at a time
        self.assertFalse(office._dirs_overlap("", a))


class Claim(unittest.TestCase):
    def setUp(self):
        reset()
        write_config({})

    def test_same_folder_takes_turns_other_folder_does_not_wait(self):
        a, b = os.path.join(_TMP, "a"), os.path.join(_TMP, "b")
        t1 = add("one", a)
        t2 = add("two", os.path.join(a, "inside"))
        t3 = add("three", b)
        self.assertEqual(office.task_claim({"owner": "w1"})["task"]["id"], t1["id"])
        # t2 is inside t1's folder: skipped. t3 is elsewhere: handed out.
        self.assertEqual(office.task_claim({"owner": "w2"})["task"]["id"], t3["id"])
        idle = office.task_claim({"owner": "w3"})
        self.assertIsNone(idle["task"])
        self.assertEqual((idle["running"], idle["pending"]), (2, 1))
        office.task_update({"id": t1["id"], "status": "done"})
        self.assertEqual(office.task_claim({"owner": "w3"})["task"]["id"], t2["id"])

    def test_nothing_left_says_so(self):
        idle = office.task_claim({"owner": "w"})
        self.assertEqual((idle["task"], idle["running"], idle["pending"]), (None, 0, 0))

    def test_check_must_be_defined_by_the_operator(self):
        r = office.task_add({"title": "x", "dir": _TMP, "check": "tests"})
        self.assertFalse(r["ok"])
        self.assertIn("no check named", r["error"])
        write_config({"checks": {"tests": "exit 0"}})
        r = office.task_add({"title": "x", "dir": _TMP, "check": "tests"})
        self.assertTrue(r["ok"])
        self.assertEqual(r["task"]["check_name"], "tests")
        self.assertEqual(office.tasks_list()["checks"], ["tests"])

    def test_parallel_setting_is_bounded(self):
        self.assertEqual(office._parallel({}), 1)
        self.assertEqual(office._parallel({"parallel": 3}), 3)
        self.assertEqual(office._parallel({"parallel": 99}), office.PARALLEL_MAX)
        self.assertEqual(office._parallel({"parallel": "many"}), 1)


class WhereAgentsStart(unittest.TestCase):
    """A new agent starts in the folder whose Claude Code settings run the hook."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="proj-", dir=_TMP)
        self.sub = os.path.join(self.root, "site", "blog")
        os.makedirs(self.sub)
        os.makedirs(os.path.join(self.root, ".claude"))
        hook = os.path.join(ROOT, "hook.py")
        with open(os.path.join(self.root, ".claude", "settings.local.json"), "w") as f:
            json.dump({"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": 'python "%s"' % hook}]}]}}, f)
        # a home folder without the hook, whatever the machine running the tests has
        home = tempfile.mkdtemp(prefix="home-", dir=_TMP)
        patcher = mock.patch.object(office.os.path, "expanduser",
                                    lambda p: p.replace("~", home, 1))
        patcher.start()
        self.addCleanup(patcher.stop)

    def spawn(self, cwd, **extra):
        with mock.patch.object(office.subprocess, "Popen") as popen, \
                mock.patch.dict(os.environ, {"OFFICE_ALLOW_SPAWN": "1"}):
            result = office.spawn_agent(dict({"cwd": cwd, "task": "Write the post"}, **extra))
        return result, popen

    def test_folder_with_the_hook_is_used_as_is(self):
        self.assertTrue(office._runs_hook(self.root))
        self.assertFalse(office._runs_hook(self.sub))
        result, popen = self.spawn(self.root)
        self.assertEqual((result["ok"], result["area"], result["warning"]), (True, None, None))
        self.assertEqual(popen.call_args.kwargs["cwd"], self.root)
        self.assertEqual(result["task"], "Write the post")

    def test_sub_folder_starts_where_the_hook_is_and_names_its_area(self):
        self.assertEqual(office._team_folder(self.sub), self.root)
        result, popen = self.spawn(self.sub)
        self.assertEqual(popen.call_args.kwargs["cwd"], self.root)
        self.assertEqual(result["area"], self.sub)
        self.assertTrue(result["task"].startswith("Your area is the folder"))
        self.assertIn("site/blog", result["task"])
        self.assertTrue(result["task"].endswith("Write the post"))

    def test_hook_in_the_users_own_settings_covers_every_folder(self):
        with mock.patch.object(office, "_runs_hook", lambda folder: "home-" in folder):
            self.assertEqual(office._team_folder(self.sub), self.sub)

    def test_no_hook_anywhere_warns_instead_of_pretending(self):
        with mock.patch.object(office, "_team_folder", lambda cwd: None):
            result, popen = self.spawn(self.sub)
        self.assertTrue(popen.called)
        self.assertIn("do not report to The Office", result["warning"])


class LiveServer(unittest.TestCase):
    """A real server on a free port, and the real runner started against it."""

    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), office.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.worker = os.path.join(_TMP, "worker.py")
        with open(cls.worker, "w", encoding="utf-8") as f:
            f.write(WORKER)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        reset()
        self.log = os.path.join(_TMP, "log-%s.txt" % self.id().rsplit(".", 1)[-1])
        if os.path.exists(self.log):
            os.remove(self.log)

    def post(self, path, body, headers=None):
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.port, path),
            data=json.dumps(body).encode(), method="POST",
            headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read().decode())

    def run_runner(self, *args, **env):
        full = dict(os.environ, OFFICE_PORT=str(self.port), TEST_LOG=self.log, **env)
        return subprocess.run([sys.executable, os.path.join(ROOT, "runner.py")] + list(args),
                              env=full, capture_output=True, text=True, timeout=120)

    def spans(self):
        """{ref: (start, end)} from the worker log."""
        out = {}
        with open(self.log) as f:
            for line in f:
                kind, ref, at = line.split()
                out.setdefault(ref, [None, None])[kind == "end"] = float(at)
        return out

    def test_a_page_on_another_site_cannot_post(self):
        code, _ = self.post("/api/tasks", {"title": "x"}, {"Origin": "https://evil.example"})
        self.assertEqual(code, 403)
        code, _ = self.post("/api/tasks", {"title": "x"}, {"Host": "evil.example"})
        self.assertEqual(code, 403)
        code, body = self.post("/api/tasks", {"title": "x"},
                               {"Origin": "http://localhost:%d" % self.port})
        self.assertEqual((code, body["ok"]), (200, True))
        code, body = self.post("/api/tasks", {"title": "y"})   # a local program: no Origin
        self.assertEqual((code, body["ok"]), (200, True))

    def test_different_folders_overlap_same_folder_takes_turns(self):
        write_config({"worker_cmd": [sys.executable, self.worker]})
        a, b = os.path.join(_TMP, "pa"), os.path.join(_TMP, "pb")
        for d in (a, b):
            os.makedirs(d, exist_ok=True)
        t1, t2, t3 = add("one", a), add("two", b), add("three", a)
        done = self.run_runner("--parallel", "3")
        self.assertIn("ran 3 task(s)", done.stdout, done.stdout + done.stderr)
        s = self.spans()
        one, two, three = s[t1["ref"]], s[t2["ref"]], s[t3["ref"]]
        self.assertLess(two[0], one[1], "tasks in different folders should overlap")
        self.assertGreaterEqual(three[0], one[1], "same folder: the second waits")
        self.assertEqual({t["status"] for t in office.tasks_list()["tasks"]}, {"done"})

    def test_one_at_a_time_by_default(self):
        write_config({"worker_cmd": [sys.executable, self.worker]})
        a, b = os.path.join(_TMP, "sa"), os.path.join(_TMP, "sb")
        for d in (a, b):
            os.makedirs(d, exist_ok=True)
        t1, t2 = add("one", a), add("two", b)
        self.run_runner(TEST_SLEEP="0.3")
        s = self.spans()
        self.assertGreaterEqual(s[t2["ref"]][0], s[t1["ref"]][1])

    def test_a_dependent_task_starts_when_its_dependency_finishes(self):
        write_config({"worker_cmd": [sys.executable, self.worker]})
        a, b = os.path.join(_TMP, "da"), os.path.join(_TMP, "db")
        for d in (a, b):
            os.makedirs(d, exist_ok=True)
        t1 = add("first", a)
        t2 = add("second", b, deps=[t1["id"]])
        done = self.run_runner("--parallel", "2", TEST_SLEEP="0.3")
        self.assertIn("ran 2 task(s)", done.stdout, done.stdout + done.stderr)
        s = self.spans()
        self.assertGreaterEqual(s[t2["ref"]][0], s[t1["ref"]][1])

    def test_failed_check_goes_back_to_the_worker_then_blocks(self):
        folder = os.path.join(_TMP, "chk")
        os.makedirs(folder, exist_ok=True)
        fail = [sys.executable, "-c", "import sys; print('2 tests failed'); sys.exit(3)"]
        write_config({"worker_cmd": [sys.executable, self.worker], "checks": {"tests": fail}})
        t = add("needs tests", folder, check="tests")
        self.run_runner("--once", TEST_SLEEP="0")
        got = [x for x in office.tasks_list()["tasks"] if x["id"] == t["id"]][0]
        self.assertEqual(got["status"], "pending")          # sent back, attempt used
        self.assertEqual(got["attempts"], 1)
        self.assertIn("2 tests failed", got["body"])
        self.run_runner("--once", TEST_SLEEP="0")
        got = [x for x in office.tasks_list()["tasks"] if x["id"] == t["id"]][0]
        self.assertEqual(got["status"], "blocked")          # out of attempts: a person looks
        self.assertEqual(got["result"]["check"]["exit"], 3)

    def test_passing_check_closes_the_task(self):
        folder = os.path.join(_TMP, "chk2")
        os.makedirs(folder, exist_ok=True)
        ok = [sys.executable, "-c", "import os; print(os.getcwd())"]
        write_config({"worker_cmd": [sys.executable, self.worker], "checks": {"where": ok}})
        t = add("has a check", folder, check="where")
        self.run_runner("--once", TEST_SLEEP="0")
        got = [x for x in office.tasks_list()["tasks"] if x["id"] == t["id"]][0]
        self.assertEqual(got["status"], "done")
        self.assertTrue(got["result"]["check"]["ok"])
        # the check ran in the task's folder
        self.assertEqual(os.path.normcase(got["result"]["check"]["output"]),
                         os.path.normcase(os.path.realpath(folder)))

    def test_a_check_removed_from_the_config_blocks_instead_of_passing(self):
        folder = os.path.join(_TMP, "chk3")
        os.makedirs(folder, exist_ok=True)
        write_config({"worker_cmd": [sys.executable, self.worker],
                      "checks": {"tests": [sys.executable, "-c", "pass"]}})
        t = add("check will vanish", folder, check="tests")
        write_config({"worker_cmd": [sys.executable, self.worker]})
        self.run_runner("--once", TEST_SLEEP="0")
        got = [x for x in office.tasks_list()["tasks"] if x["id"] == t["id"]][0]
        self.assertEqual(got["status"], "blocked")
        self.assertIn("could not run", got["summary"])


if __name__ == "__main__":
    unittest.main()
