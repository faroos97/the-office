#!/usr/bin/env python3
"""runner.py: works through the task list of The Office.

It claims the next ready task, hands it to a worker, shows that worker as a desk on the
board while it runs, records the outcome, and moves on. When nothing is ready it exits.

    python runner.py              # drain the ready tasks (at most 10), then exit
    python runner.py --max 3
    python runner.py --once       # one task
    python runner.py --parallel 3 # up to three tasks at the same time

The board's "Run tasks" button starts it for you.

SEVERAL AT ONCE. With --parallel N (or "parallel": N in config.json) the runner keeps
up to N workers busy. The task list never hands out a task while another one is running
in the same folder, or in a folder above or below it, so two workers do not write over
each other: tasks in different folders overlap, tasks in one folder take turns.

CHECKS. A worker may have no shell, so "the tests pass" is not something it can know.
A task can name a check, a command you defined in config.json:

    { "checks": { "tests": "python -m unittest discover -s tests" } }

After the worker says done, the runner (not the model) runs that command in the task's
folder. Exit code 0: the task is done. Anything else: the task goes back to a worker with
the command's output, until it is out of attempts; then it is blocked for a person to see.
A task can only pick a check by name. The commands themselves live in your config.

THE WORKER. By default a task is given to a headless Claude Code session:
`claude -p` in the task's folder, with the task as its prompt. Extra arguments come
from OFFICE_WORKER_ARGS (for example a permission mode or a model).

To use your own engine instead (a pipeline with planning, review, model routing...),
put its command in config.json next to this file:

    { "worker_cmd": ["python", "/path/to/my_worker.py"] }

The contract is small:
  - the task arrives as one JSON object on stdin: {id, ref, title, body, dir, kind, attempts}
  - with --parallel, several copies of your worker run at the same time, each on a
    task in a different folder; answer "pending" if yours cannot start right now
  - optional progress, one per line on stderr:   ##office activity: reviewing
  - the outcome, as the last such line on stdout:
        ##office result: {"status": "done", "summary": "...", ...}
    status is done, blocked (needs a person), failed, or pending (try again later).
    Anything else in the object is kept as the task's receipt and shown on the board.
  - no result line: exit code 0 means done, anything else failed.

Standard library only.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "http://127.0.0.1:%s" % (os.environ.get("OFFICE_PORT") or "8787")
HEARTBEAT_SECS = 20
WORKER_TIMEOUT = int(os.environ.get("OFFICE_WORKER_TIMEOUT") or 3600)
CHECK_TIMEOUT = int(os.environ.get("OFFICE_CHECK_TIMEOUT") or 900)
CHECK_OUTPUT_CHARS = 2500   # how much of a failed check's output goes back to the worker
IDLE_POLL_SECS = 3          # a free worker looks again this often while others still run
PARALLEL_MAX = 8
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Per-session variables of a Claude Code session that started us must not leak into a
# worker (it would think it is a child of that session).
_SESSION_ENV = {
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_BRIDGE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_PID", "CLAUDE_JOB_DIR",
    "CLAUDE_EFFORT", "AI_AGENT",
}


def _post(path, obj, timeout=10):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _config():
    """config.json, or {} when there is none. A file that exists but does not parse is
    an error, never 'no config': falling back to the default worker would silently run
    something other than what was configured."""
    path = os.environ.get("OFFICE_CONFIG") or os.path.join(HERE, "config.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("config.json must be a JSON object")
    return cfg


def _clean_env():
    return {k: v for k, v in os.environ.items() if k not in _SESSION_ENV}


def _claude_bin():
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


class Desk:
    """The worker's presence on the board while its task runs."""

    def __init__(self, task):
        self.task = task
        self.sid = "task-%d-a%d" % (task["id"], task["attempts"])
        self.activity = "starting"
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._beat, daemon=True)

    def _report(self, event="PostToolUse"):
        try:
            _post("/api/agents/report", {
                "session_id": self.sid, "hook_event_name": event,
                "cwd": self.task.get("dir") or HERE,
                "task": "%s %s" % (self.task["ref"], self.task["title"]),
                "activity": "task worker: %s" % self.activity}, timeout=3)
        except Exception:
            pass

    def _beat(self):
        while not self._stop.wait(HEARTBEAT_SECS):
            self._report()
            try:
                _post("/api/tasks/update", {"id": self.task["id"], "touch": True}, timeout=3)
            except Exception:
                pass

    def set(self, activity):
        self.activity = " ".join(str(activity).split())[:120] or self.activity
        self._report()

    def __enter__(self):
        self._report()
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._report("SessionEnd")


def run_claude(task, desk):
    """The built-in worker: a headless Claude Code session in the task's folder."""
    prompt = task["title"]
    if task.get("body"):
        prompt += "\n\n" + task["body"]
    args = [_claude_bin(), "-p", "--output-format", "json"]
    args += shlex.split(os.environ.get("OFFICE_WORKER_ARGS", ""), posix=os.name != "nt")
    cwd = task.get("dir") if task.get("dir") and os.path.isdir(task["dir"]) else None
    desk.set("claude is working")
    try:
        proc = subprocess.run(args, input=prompt.encode("utf-8"), capture_output=True,
                              cwd=cwd, env=_clean_env(), timeout=WORKER_TIMEOUT,
                              creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "summary": "timed out after %ds" % WORKER_TIMEOUT}
    except OSError as e:
        return {"status": "failed", "summary": "could not start claude: %s" % e}
    out = proc.stdout.decode("utf-8", "replace").strip()
    try:
        reply = json.loads(out)
    except ValueError:
        reply = None
    if isinstance(reply, dict):
        text = str(reply.get("result") or "").strip()
        failed = bool(reply.get("is_error")) or proc.returncode != 0
        return {"status": "failed" if failed else "done",
                "summary": (text or "no text returned")[:1500],
                "cost_usd": reply.get("total_cost_usd"),
                "turns": reply.get("num_turns")}
    tail = (out or proc.stderr.decode("utf-8", "replace")).strip()[-1500:]
    return {"status": "done" if proc.returncode == 0 else "failed",
            "summary": tail or "exit code %d" % proc.returncode}


def run_external(cmd, task, desk):
    """Your own worker command, speaking the contract in this file's docstring."""
    cwd = task.get("dir") if task.get("dir") and os.path.isdir(task["dir"]) else None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=cwd, env=_clean_env(),
                                creationflags=NO_WINDOW)
    except OSError as e:
        return {"status": "failed", "summary": "could not start the worker: %s" % e}

    # Each pipe gets exactly one reader: stderr for live progress, stdout for the result.
    def progress():
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("##office activity:"):
                desk.set(line.split(":", 1)[1])

    captured = []
    readers = [threading.Thread(target=progress, daemon=True),
               threading.Thread(target=lambda: captured.append(proc.stdout.read()),
                                daemon=True)]
    for r in readers:
        r.start()
    try:
        proc.stdin.write(json.dumps(task).encode("utf-8"))
        proc.stdin.close()
    except OSError:
        pass  # the worker exited before reading; its exit code tells the story
    try:
        proc.wait(timeout=WORKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"status": "failed", "summary": "timed out after %ds" % WORKER_TIMEOUT}
    for r in readers:
        r.join(5)
    text = b"".join(captured).decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        if line.startswith("##office result:"):
            try:
                result = json.loads(line.split(":", 1)[1])
            except ValueError:
                break
            if isinstance(result, dict) and result.get("status"):
                return result
            break
    return {"status": "done" if proc.returncode == 0 else "failed",
            "summary": text.strip()[-1500:] or "exit code %d" % proc.returncode}


def run_check(task, cfg, desk):
    """Run the check the task names (a command from config.json "checks") in the task's
    folder. Returns {"name", "ok", "exit", "output"}; "exit" is None when the command
    could not be run at all."""
    name = task["check_name"]
    checks = cfg.get("checks")
    cmd = checks.get(name) if isinstance(checks, dict) else None
    if not cmd:
        return {"name": name, "ok": False, "exit": None,
                "output": "config.json has no check named %r" % name}
    cwd = task.get("dir") if task.get("dir") and os.path.isdir(task["dir"]) else None
    desk.set("running the check: %s" % name)
    try:
        proc = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=_clean_env(),
                              timeout=CHECK_TIMEOUT, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"name": name, "ok": False, "exit": None,
                "output": "timed out after %ds" % CHECK_TIMEOUT}
    except OSError as e:
        return {"name": name, "ok": False, "exit": None,
                "output": "could not start the command: %s" % e}
    out = proc.stdout.decode("utf-8", "replace").strip()
    return {"name": name, "ok": proc.returncode == 0, "exit": proc.returncode,
            "output": out[-CHECK_OUTPUT_CHARS:]}


def settle(task, result, cfg, desk):
    """Turn a worker's answer into what gets recorded on the task. Returns the body for
    /api/tasks/update, and whether the runner should go on to other tasks."""
    status = result.get("status")
    if status not in ("done", "blocked", "failed", "pending"):
        status = "failed"
    update = {"id": task["id"], "summary": result.get("summary") or "", "result": result}
    # 'pending' from a worker means "not now" (a quota wait, an engine that is busy). It
    # is not a failed attempt, and the run stops rather than claim the same task again.
    go_on = status != "pending"
    if status == "pending":
        update["refund_attempt"] = True
    if status == "done" and task.get("check_name"):
        check = result["check"] = run_check(task, cfg, desk)
        name = check["name"]
        if check["ok"]:
            update["summary"] = ("%s\nCheck %s passed." % (update["summary"], name)).strip()
        elif check["exit"] is None:
            status = "blocked"
            update["summary"] = ("The work is finished but its check (%s) could not run: "
                                 "%s" % (name, check["output"]))
        elif task["attempts"] < task["max_attempts"]:
            status = "pending"  # a real attempt was used: no refund
            update["summary"] = ("Check %s failed (exit %d) on attempt %d of %d. Sent "
                                 "back to a worker with the output."
                                 % (name, check["exit"], task["attempts"],
                                    task["max_attempts"]))
            update["body_append"] = (
                "The check `%s` was run after the previous attempt and FAILED (exit code "
                "%d). Fix what it reports. The work only counts when this check passes. "
                "Its output:\n%s" % (name, check["exit"], check["output"]))
        else:
            status = "blocked"
            update["summary"] = ("Check %s still fails after %d attempts (exit %d). Last "
                                 "output:\n%s" % (name, task["attempts"], check["exit"],
                                                  check["output"][-600:]))
    update["status"] = status
    return update, go_on


def run_task(task, cfg):
    """Run one claimed task to its recorded outcome. Returns False when the whole run
    should stop (the worker answered "not now")."""
    with Desk(task) as desk:
        try:
            if cfg.get("worker_cmd"):
                result = run_external(cfg["worker_cmd"], task, desk)
            else:
                result = run_claude(task, desk)
        except Exception as e:  # noqa: BLE001  a broken worker must not strand the task
            result = {"status": "failed", "summary": "runner error: %s" % e}
        update, go_on = settle(task, result, cfg, desk)
    _post("/api/tasks/update", update)
    print("%s %s: %s" % (task["ref"], update["status"], update["summary"][:200]),
          flush=True)
    return go_on


class Run:
    """What the workers of one runner share."""

    def __init__(self, limit):
        self.lock = threading.Lock()
        self.left = limit    # tasks this run may still start
        self.done = 0
        self.stop = False    # a worker said "not now": finish what runs, start nothing
        self.error = None


def work(owner, run, cfg):
    """One worker: claim, run, repeat, until there is nothing left for it."""
    try:
        while True:
            with run.lock:
                if run.stop or run.left <= 0:
                    return
                run.left -= 1
            answer = _post("/api/tasks/claim", {"owner": owner})
            task = answer.get("task")
            if not task:
                with run.lock:
                    run.left += 1
                # Nothing for this worker right now. While other tasks are running, one
                # of them may free a folder or finish a dependency: look again shortly.
                if answer.get("running") and answer.get("pending"):
                    time.sleep(IDLE_POLL_SECS)
                    continue
                return
            go_on = run_task(task, cfg)
            with run.lock:
                run.done += 1
                if not go_on:
                    run.stop = True
    except OSError as e:
        with run.lock:
            run.error = e
            run.stop = True


def main():
    ap = argparse.ArgumentParser(description="Run the tasks queued on The Office")
    ap.add_argument("--max", type=int, default=10, help="tasks to run before exiting")
    ap.add_argument("--once", action="store_true", help="run a single task")
    ap.add_argument("--parallel", type=int, default=0,
                    help='tasks to work on at the same time (default: "parallel" in '
                         "config.json, else 1)")
    args = ap.parse_args()
    try:
        cfg = _config()
        parallel = int(args.parallel or cfg.get("parallel") or 1)
    except (OSError, ValueError, TypeError) as e:
        print("config.json is not valid (%s). Nothing was run." % e)
        return 1
    limit = 1 if args.once else max(1, args.max)
    run = Run(limit)
    workers = [threading.Thread(target=work, args=("runner-%d-%d" % (os.getpid(), n + 1),
                                                   run, cfg))
               for n in range(max(1, min(parallel, PARALLEL_MAX, limit)))]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    if run.error:
        print("The Office is not reachable at %s (%s)." % (BASE, run.error))
        return 1
    print("ran %d task(s)" % run.done, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
