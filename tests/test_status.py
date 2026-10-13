"""Tests for the manager's status call, dead-process reaping, the flags a new agent is
started with, and the tailnet access token. Standard library only:

    python -m unittest discover -s tests
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="office-status-test-")
os.environ.setdefault("OFFICE_DB", os.path.join(_TMP, "office.db"))
os.environ.setdefault("OFFICE_CONFIG", os.path.join(_TMP, "config.json"))
os.environ["OFFICE_TOKEN_FILE"] = os.path.join(_TMP, "office.token")
sys.path.insert(0, ROOT)
import office  # noqa: E402

office._PROCESS_NAME = "python"   # the tests' "live agent" is this interpreter


def write_config(**cfg):
    with open(os.environ["OFFICE_CONFIG"], "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def clear():
    conn = office._db()
    with conn:
        conn.execute("DELETE FROM agents")
    conn.close()


class Reaping(unittest.TestCase):
    def setUp(self):
        clear()

    def test_dead_process_leaves_the_board_live_one_stays(self):
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        office.report({"session_id": "dead", "hook_event_name": "Notification",
                       "cwd": _TMP, "pid": gone.pid})
        office.report({"session_id": "live", "hook_event_name": "Notification",
                       "cwd": _TMP, "pid": os.getpid()})
        office.report({"session_id": "nopid", "hook_event_name": "Stop", "cwd": _TMP})
        ids = {a["session_id"] for a in office.board()["agents"]}
        self.assertEqual(ids, {"live", "nopid"})


class Silent(unittest.TestCase):
    def test_idle_live_process_stays_past_the_ghost_age(self):
        clear()
        office.report({"session_id": "idle", "hook_event_name": "Stop", "cwd": _TMP,
                       "pid": os.getpid()})
        office.report({"session_id": "old", "hook_event_name": "Stop", "cwd": _TMP})
        conn = office._db()
        with conn:
            conn.execute("UPDATE agents SET updated_at=datetime('now','-5 hours')")
        conn.close()
        self.assertEqual([a["session_id"] for a in office.board()["agents"]], ["idle"])


class Status(unittest.TestCase):
    def setUp(self):
        clear()

    def test_blocked_agent_reports_its_ask_and_time(self):
        office.report({"session_id": "a1", "hook_event_name": "Notification",
                       "cwd": _TMP, "task": "Build the thing",
                       "message": "Claude needs your permission to use Bash"})
        conn = office._db()
        with conn:
            conn.execute("UPDATE agents SET state_since=datetime('now','-300 seconds')")
        conn.close()
        st = office.status()
        self.assertEqual(st["blocked"], 1)
        a = st["agents"][0]
        self.assertEqual(a["state"], "blocked")
        self.assertIn("permission", a["last_ask"])
        self.assertGreaterEqual(a["blocked_secs"], 299)
        self.assertEqual(a["title"], "Build the thing")

    def test_idle_notice_is_waiting_not_blocked(self):
        for sid, extra in (("i1", {"notification_type": "idle_prompt"}),
                           ("i2", {"message": "Claude is waiting for your input"})):
            office.report(dict({"session_id": sid, "hook_event_name": "Notification",
                                "cwd": _TMP}, **extra))
        office.report({"session_id": "p1", "hook_event_name": "Notification", "cwd": _TMP,
                       "notification_type": "permission_prompt",
                       "message": "Claude needs your permission to use Bash"})
        st = {a["session_id"]: a["state"] for a in office.board()["agents"]}
        self.assertEqual(st, {"i1": "waiting", "i2": "waiting", "p1": "blocked"})

    def test_state_clock_restarts_on_change(self):
        office.report({"session_id": "a2", "hook_event_name": "Notification", "cwd": _TMP})
        conn = office._db()
        with conn:
            conn.execute("UPDATE agents SET state_since=datetime('now','-900 seconds')")
        conn.close()
        office.report({"session_id": "a2", "hook_event_name": "PreToolUse", "cwd": _TMP,
                       "tool_name": "Read"})
        self.assertLess(office.status()["agents"][0]["in_state_secs"], 30)


class Flags(unittest.TestCase):
    def test_permission_mode_and_model_rules(self):
        write_config(permission_mode="auto", model_rules=[
            {"match": r"mockups|\bdemos?\b", "model": "claude-fable-5-1"}])
        f = office._agent_flags(["--model", "sonnet"], "C:/x/PORTFOLIO/clients", "fix cart")
        self.assertEqual(f, ["--model", "sonnet", "--permission-mode", "auto"])
        f = office._agent_flags(["--model", "sonnet"], "C:/x/PORTFOLIO/mockups", "a site")
        self.assertEqual(f, ["--model", "claude-fable-5-1", "--permission-mode", "auto"])
        f = office._agent_flags(["--model", "sonnet"], "C:/x/p/clients", "Build a demo")
        self.assertIn("claude-fable-5-1", f)
        f = office._agent_flags(["--model", "opus"], "C:/x/p/mockups", "demo", rules=False)
        self.assertEqual(f[:2], ["--model", "opus"])

    def test_never_adds_a_second_mode_or_allow_all(self):
        write_config(permission_mode="auto")
        f = office._agent_flags(["--permission-mode", "plan"], "x", "y")
        self.assertEqual(f.count("--permission-mode"), 1)
        f = office._agent_flags(["--dangerously-skip-permissions"], "x", "y")
        self.assertNotIn("--permission-mode", f)

    def test_no_config_means_no_extra_flags(self):
        write_config()
        self.assertEqual(office._agent_flags(["--model", "sonnet"], "x", "y"),
                         ["--model", "sonnet"])


class SpawnCommand(unittest.TestCase):
    """What `new` really runs, with the terminal launch captured instead of opened."""

    def setUp(self):
        self.calls = []
        self.popen = office.subprocess.Popen
        office.subprocess.Popen = lambda args, **kw: self.calls.append((args, kw))
        os.environ["OFFICE_ALLOW_SPAWN"] = "1"
        self.folder = os.path.join(_TMP, "PORTFOLIO")
        for sub in ("mockups", "clients"):
            os.makedirs(os.path.join(self.folder, sub), exist_ok=True)
        os.makedirs(os.path.join(self.folder, ".claude"), exist_ok=True)
        with open(os.path.join(self.folder, ".claude", "settings.json"), "w") as f:
            json.dump({"hooks": {"Stop": [{"hooks": [{"command": "python %s/hook.py"
                                                       % os.path.basename(ROOT)}]}]}}, f)
        write_config(permission_mode="auto", agent_args=["--model", "sonnet"],
                     manager_args=["--model", "opus"],
                     model_rules=[{"match": r"mockups|demos?", "model": "claude-fable-5-1"}])

    def tearDown(self):
        office.subprocess.Popen = self.popen

    def flag(self, name):
        a = self.argv()
        return a[a.index(name) + 1]

    def argv(self):
        args = self.calls[-1][0]
        return args[args.index("cmd", 1) + 2:] if sys.platform.startswith("win") else args

    def test_demo_agent_gets_fable_and_auto_mode(self):
        r = office.spawn_agent({"cwd": os.path.join(self.folder, "mockups"), "task": "a site"})
        self.assertTrue(r["ok"], r)
        argv = self.argv()
        self.assertEqual(self.flag("--model"), "claude-fable-5-1")
        self.assertEqual(self.flag("--permission-mode"), "auto")
        self.assertTrue(argv[-1].startswith("Your area is the folder"))
        self.assertEqual(os.path.normcase(self.calls[-1][1]["cwd"]), os.path.normcase(self.folder))

    def test_other_agent_keeps_sonnet_with_auto_mode(self):
        office.spawn_agent({"cwd": os.path.join(self.folder, "clients"), "task": "fix cart"})
        self.assertEqual((self.flag("--model"), self.flag("--permission-mode")), ("sonnet", "auto"))

    def test_manager_keeps_its_own_model(self):
        office.spawn_agent({"cwd": os.path.join(self.folder, "mockups"), "task": "demo",
                            "role": "manager"})
        self.assertEqual((self.flag("--model"), self.flag("--permission-mode")), ("opus", "auto"))

    def test_no_session_env_leaks(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "parent"
        office.spawn_agent({"cwd": self.folder, "task": "x"})
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", self.calls[-1][1]["env"])


class Remote(unittest.TestCase):
    HOST = "box.example.ts.net"

    @classmethod
    def setUpClass(cls):
        write_config(remote_host=cls.HOST)
        cls.httpd = office.ThreadingHTTPServer(("127.0.0.1", 0), office.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.token = office._remote_token()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, path, host=None, headers=None, method="GET", body=None):
        h = {"Host": host or self.HOST}
        h.update(headers or {})
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     headers=h, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        try:
            r = urllib.request.build_opener(NoRedirect).open(req, timeout=5)
            return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_no_token_is_refused(self):
        self.assertEqual(self.call("/api/agents")[0], 403)
        self.assertEqual(self.call("/api/agents", headers={"Authorization": "Bearer nope"})[0], 403)

    def test_token_works_and_post_needs_same_origin(self):
        auth = {"Authorization": "Bearer " + self.token}
        self.assertEqual(self.call("/api/status", headers=auth)[0], 200)
        bad = dict(auth, Origin="https://evil.example")
        self.assertEqual(self.call("/api/agents/report", headers=bad, method="POST", body={})[0], 403)
        good = dict(auth, Origin="https://" + self.HOST)
        self.assertEqual(self.call("/api/agents/report", headers=good, method="POST", body={})[0], 200)

    def test_funnel_requests_are_refused_even_with_token(self):
        auth = {"Authorization": "Bearer " + self.token, "Tailscale-Funnel-Request": "?1"}
        self.assertEqual(self.call("/api/agents", headers=auth)[0], 403)

    def test_forwarded_host_is_accepted_and_forwarded_local_host_is_not_local(self):
        # tailscale serve may hand us Host=<backend> and X-Forwarded-Host=<tailnet name>
        fwd = {"X-Forwarded-Host": self.HOST, "X-Forwarded-Proto": "https",
               "X-Forwarded-For": "100.64.0.9"}
        local = "127.0.0.1:%d" % self.port
        self.assertEqual(self.call("/api/status", host=local, headers=fwd)[0], 403)
        ok = dict(fwd, Authorization="Bearer " + self.token)
        self.assertEqual(self.call("/api/status", host=local, headers=ok)[0], 200)

    def test_pid_of_another_program_counts_as_dead(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(office._pid_alive(other.pid))
            office._PROCESS_NAME = "claude"
            self.assertFalse(office._pid_alive(other.pid))
        finally:
            office._PROCESS_NAME = "python"
            other.kill()

    def test_other_host_is_refused(self):
        auth = {"Authorization": "Bearer " + self.token}
        self.assertEqual(self.call("/api/agents", host="evil.example", headers=auth)[0], 403)

    def test_token_link_sets_cookie_and_redirects(self):
        code, headers, _ = self.call("/?token=" + self.token)
        self.assertEqual(code, 302)
        self.assertEqual(headers["Location"], "/")
        cookie = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertEqual(self.call("/api/status", headers={"Cookie": cookie.split(";")[0]})[0], 200)

    def test_local_still_works_without_token(self):
        self.assertEqual(self.call("/api/status", host="127.0.0.1:%d" % self.port)[0], 200)


if __name__ == "__main__":
    unittest.main()
