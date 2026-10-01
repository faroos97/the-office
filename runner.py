#!/usr/bin/env python3
"""runner.py: works through the task list of The Office.

It claims the next ready task, hands it to a worker, shows that worker as a desk on the
board while it runs, records the outcome, and moves on. When nothing is ready it exits.

    python runner.py              # drain the ready tasks (at most 10), then exit
    python runner.py --max 3
    python runner.py --once       # one task

The board's "Run tasks" button starts it for you.

THE WORKER. By default a task is given to a headless Claude Code session:
`claude -p` in the task's folder, with the task as its prompt. Extra arguments come
from OFFICE_WORKER_ARGS (for example a permission mode or a model).

To use your own engine instead (a pipeline with planning, review, model routing...),
put its command in config.json next to this file:

    { "worker_cmd": ["python", "/path/to/my_worker.py"] }

The contract is small:
  - the task arrives as one JSON object on stdin: {id, ref, title, body, dir, kind, attempts}
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


def run_one(owner):
    """Claim and run one task. Returns False when nothing was ready."""
    task = _post("/api/tasks/claim", {"owner": owner}).get("task")
    if not task:
        return False
    worker_cmd = _config().get("worker_cmd")
    with Desk(task) as desk:
        try:
            if worker_cmd:
                result = run_external(worker_cmd, task, desk)
            else:
                result = run_claude(task, desk)
        except Exception as e:  # noqa: BLE001  a broken worker must not strand the task
            result = {"status": "failed", "summary": "runner error: %s" % e}
    status = result.get("status")
    if status not in ("done", "blocked", "failed", "pending"):
        status = "failed"
    _post("/api/tasks/update", {"id": task["id"], "status": status,
                                "summary": result.get("summary") or "", "result": result,
                                "refund_attempt": status == "pending"})
    print("%s %s: %s" % (task["ref"], status, (result.get("summary") or "")[:200]),
          flush=True)
    # 'pending' means "not now" (a quota wait, an engine that is busy): stop this run
    # rather than immediately claiming the same task again.
    return status != "pending"


def main():
    ap = argparse.ArgumentParser(description="Run the tasks queued on The Office")
    ap.add_argument("--max", type=int, default=10, help="tasks to run before exiting")
    ap.add_argument("--once", action="store_true", help="run a single task")
    args = ap.parse_args()
    try:
        _config()
    except (OSError, ValueError) as e:
        print("config.json is not valid (%s). Nothing was run." % e)
        return 1
    owner = "runner-%d" % os.getpid()
    done = 0
    limit = 1 if args.once else max(1, args.max)
    try:
        while done < limit and run_one(owner):
            done += 1
    except OSError as e:
        print("The Office is not reachable at %s (%s)." % (BASE, e))
        return 1
    print("ran %d task(s)" % done, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
