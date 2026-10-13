"""Tests for the model log, the verdicts and `models report`. Standard library only:

    python -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="office-modellog-test-")
os.environ.setdefault("OFFICE_DB", os.path.join(_TMP, "office.db"))
os.environ.setdefault("OFFICE_CONFIG", os.path.join(_TMP, "config.json"))
os.environ["OFFICE_MODEL_LOG"] = os.path.join(_TMP, "model_log.jsonl")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import hook  # noqa: E402
import models  # noqa: E402
import office  # noqa: E402
from set_model_table import TABLE, OPUS48, OPUS55, SONNET  # noqa: E402


def write_config(**cfg):
    with open(os.environ["OFFICE_CONFIG"], "w", encoding="utf-8") as f:
        json.dump(cfg, f)


def transcript(n_messages, per=100):
    """A transcript with n distinct API messages, each written as two content-block lines
    that share one usage record (as Claude Code does)."""
    path = os.path.join(_TMP, "t%d.jsonl" % n_messages)
    with open(path, "w") as f:
        for i in range(n_messages):
            for _ in range(2):
                f.write(json.dumps({"type": "assistant", "message": {
                    "id": "m%d" % i, "usage": {"input_tokens": per, "output_tokens": per,
                                               "cache_creation_input_tokens": per,
                                               "cache_read_input_tokens": 99999}}}) + "\n")
    return path


class Log(unittest.TestCase):
    def setUp(self):
        self.path = os.environ["OFFICE_MODEL_LOG"]
        if os.path.exists(self.path):
            os.remove(self.path)
        conn = office._db()
        with conn:
            conn.execute("DELETE FROM agents")
            conn.execute("DELETE FROM spawns")
        conn.close()
        write_config(permission_mode="auto", agent_args=["--model", "sonnet"],
                     model_rules=TABLE)
        self.popen = office.subprocess.Popen
        office.subprocess.Popen = lambda *a, **k: None
        os.environ["OFFICE_ALLOW_SPAWN"] = "1"
        self.folder = os.path.join(_TMP, "LOGFOLDER")
        os.makedirs(os.path.join(self.folder, ".claude"), exist_ok=True)
        with open(os.path.join(self.folder, ".claude", "settings.json"), "w") as f:
            json.dump({"hooks": {"Stop": [{"hooks": [{"command": "python %s/hook.py"
                                                       % os.path.basename(ROOT)}]}]}}, f)

    def tearDown(self):
        office.subprocess.Popen = self.popen

    def start(self, sid, task, **extra):
        office.spawn_agent({"cwd": self.folder, "task": task})
        office.report(dict({"session_id": sid, "hook_event_name": "SessionStart",
                            "cwd": self.folder, "task": task}, **extra))

    def test_tokens_count_each_message_once_and_skip_cache_reads(self):
        self.assertEqual(models.transcript_tokens(transcript(4)), 4 * 300)
        self.assertIsNone(models.transcript_tokens(os.path.join(_TMP, "nope.jsonl")))

    def test_close_writes_one_line_with_the_routed_model(self):
        self.start("s1", "Research competitor sites")
        office.report({"session_id": "s1", "hook_event_name": "Stop", "cwd": self.folder,
                       "transcript_path": transcript(2)})
        office.report({"session_id": "s1", "hook_event_name": "SessionEnd", "cwd": self.folder})
        (line,) = models.read_log(self.path)
        self.assertEqual((line["type"], line["model"], line["verdict"], line["tokens"]),
                         ("research", SONNET, "unrated", 600))
        self.assertEqual(line["session"], "s1")
        self.assertTrue(line["date"] and line["title"])

    def test_good_job_logs_once_and_close_does_not_repeat_it(self):
        self.start("s2", "Summarize my inbox")
        office.report({"session_id": "s2", "hook_event_name": "UserPromptSubmit",
                       "cwd": self.folder, "verdict": "good_job"})
        office.report({"session_id": "s2", "hook_event_name": "SessionEnd", "cwd": self.folder})
        (line,) = models.read_log(self.path)
        self.assertEqual((line["type"], line["verdict"]), ("triage", "good_job"))

    def test_mark_rewrites_the_verdict_before_and_after_close(self):
        self.start("s3", "Write a blog article")
        self.assertTrue(office.models_mark({"target": "s3", "verdict": "redone"})["ok"])
        self.assertEqual(models.read_log(self.path)[0]["verdict"], "redone")
        office.report({"session_id": "s3", "hook_event_name": "SessionEnd", "cwd": self.folder})
        self.assertEqual(len(models.read_log(self.path)), 1)
        self.assertTrue(office.models_mark({"target": "s3", "verdict": "fixed"})["ok"])
        self.assertEqual(models.read_log(self.path)[0]["verdict"], "fixed")
        self.assertFalse(office.models_mark({"target": "s3", "verdict": "great"})["ok"])
        self.assertFalse(office.models_mark({"target": "nobody", "verdict": "fixed"})["ok"])

    def test_agent_not_started_by_the_office_is_logged_as_unrouted(self):
        office.report({"session_id": "s4", "hook_event_name": "SessionStart",
                       "cwd": os.path.join(_TMP, "elsewhere"), "model": "claude-opus-4-8"})
        office.report({"session_id": "s4", "hook_event_name": "SessionEnd"})
        (line,) = models.read_log(self.path)
        self.assertEqual((line["type"], line["model"]), ("unrouted", "claude-opus-4-8"))

    def test_task_runner_desks_are_not_logged(self):
        office.report({"session_id": "task-7", "hook_event_name": "SessionStart", "cwd": _TMP})
        office.report({"session_id": "task-7", "hook_event_name": "SessionEnd"})
        self.assertEqual(models.read_log(self.path), [])

    def test_dead_process_is_logged_when_reaped(self):
        gone = self.popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.start("s5", "Plan the roadmap", pid=gone.pid)
        office.board()
        (line,) = models.read_log(self.path)
        self.assertEqual(line["type"], "strategy / QA / review")

    def test_hook_turns_good_job_into_a_verdict(self):
        cfg = os.path.join(_TMP, "hookcfg.json")
        with open(cfg, "w") as f:
            json.dump({"prompt_triggers": [
                {"match": r"\bgood job\b", "say": "commit", "verdict": "good_job"},
                {"match": "other", "say": "x"}]}, f)
        self.assertEqual(hook.prompt_verdict("ok, Good Job!", cfg), "good_job")
        self.assertIsNone(hook.prompt_verdict("other thing", cfg))
        self.assertIsNone(hook.prompt_verdict("nothing", cfg))


def runs(model, verdicts, typ="t"):
    return [{"type": typ, "model": model, "verdict": v, "tokens": 1000} for v in verdicts]


class Report(unittest.TestCase):
    def test_promote_when_a_junior_model_is_redone_over_30_percent(self):
        r = models.report(runs(SONNET, ["redone", "redone", "good_job", "fixed"]))["rows"][0]
        self.assertIn("promote to Opus", r["suggestion"])
        self.assertEqual((r["runs"], r["rated"], r["good_job_pct"], r["avg_tokens"]),
                         (4, 4, 25, 1000))

    def test_no_promotion_at_or_under_30_percent_or_on_too_few_runs(self):
        ten = models.report(runs(SONNET, ["redone"] * 3 + ["good_job"] * 7))["rows"][0]  # 30%
        self.assertEqual(ten["suggestion"], "")
        small = models.report(runs(SONNET, ["redone", "good_job"]))["rows"][0]
        self.assertEqual(small["suggestion"], "")

    def test_demote_when_opus_is_perfect_over_five_runs(self):
        self.assertIn("demote to Sonnet",
                      models.report(runs(OPUS55, ["good_job"] * 5))["rows"][0]["suggestion"])
        self.assertEqual(models.report(runs(OPUS55, ["good_job"] * 4))["rows"][0]["suggestion"], "")
        self.assertEqual(models.report(runs(OPUS55, ["good_job"] * 5 + ["fixed"]))["rows"][0]
                         ["suggestion"], "")

    def test_unrated_runs_do_not_count_toward_percentages(self):
        r = models.report(runs(OPUS48, ["good_job"] * 5 + ["unrated"] * 3))["rows"][0]
        self.assertEqual((r["runs"], r["rated"], r["good_job_pct"]), (8, 5, 100))
        self.assertIn("demote", r["suggestion"])

    def test_empty_log(self):
        self.assertEqual(models.report([])["rows"], [])


if __name__ == "__main__":
    unittest.main()
