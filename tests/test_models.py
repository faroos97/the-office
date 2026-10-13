"""Tests for model routing and the model log. Standard library only:

    python -m unittest discover -s tests

The routing tests run against the real table in scripts/set_model_table.py, so the table
that ships is the table that is tested.
"""
import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="office-models-test-")
os.environ.setdefault("OFFICE_DB", os.path.join(_TMP, "office.db"))
os.environ.setdefault("OFFICE_CONFIG", os.path.join(_TMP, "config.json"))
os.environ["OFFICE_MODEL_LOG"] = os.path.join(_TMP, "model_log.jsonl")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import models  # noqa: E402
import office  # noqa: E402
from set_model_table import TABLE, FABLE, OPUS48, OPUS55, SONNET, HAIKU  # noqa: E402

AREA = "C:/Users/faroo/Documents/PORTFOLIO/clients"


def routed(task, area=AREA):
    return models.route(TABLE, area, task)


class Table(unittest.TestCase):
    def test_eight_sample_tasks(self):
        samples = [
            ("Propose three design directions for the Lunoor site", FABLE, "design proposal"),
            ("Build the accepted Nordvia hero with GSAP scroll animation", OPUS48,
             "build / demo / 3D / animation"),
            ("Write the Google Ads keywords and ad copy for Toitures Hogue", OPUS55, "marketing"),
            ("Check the Loi 25 consent wording in the contract", OPUS55, "legal / tax"),
            ("Review the PR and give me the QA verdict", OPUS55, "strategy / QA / review"),
            ("Research competitor pricing for roofers in Laval", SONNET, "research"),
            ("Write a blog article about gutter maintenance", SONNET, "content"),
            ("Summarize my inbox", HAIKU, "triage"),
        ]
        for task, model, typ in samples:
            m, t, why = routed(task)
            self.assertEqual((m, t), (model, typ), task)
            self.assertTrue(why, task)

    def test_default_row_catches_everything_else(self):
        self.assertEqual(routed("Fix the checkout button colour")[:2], (SONNET, "default"))

    def test_first_match_wins(self):
        # a demo that also mentions review is a build, not a review
        self.assertEqual(routed("Build the demo, then review it")[0], OPUS48)
        # a proposal for a demo is a proposal
        self.assertEqual(routed("Design proposal for the demo")[0], FABLE)

    def test_folder_counts(self):
        self.assertEqual(routed("a site", area="C:/x/PORTFOLIO/mockups")[0], OPUS48)

    def test_word_bounds_do_not_misroute(self):
        for task in ("Download the leads sheet", "Draw a rectangle", "Add a quaint footer"):
            self.assertEqual(routed(task)[:2], (SONNET, "default"), task)

    def test_every_row_compiles_and_has_a_model_id_and_a_reason(self):
        import re
        for row in TABLE:
            re.compile(row["match"])
            self.assertTrue(models.valid_model(row["model"]), row)
            self.assertTrue(row["why"] and row["type"], row)


class NoTable(unittest.TestCase):
    def test_missing_table_never_means_inherit(self):
        self.assertEqual(models.route(None, "x", "y")[0], models.DEFAULT_MODEL)
        self.assertEqual(models.route([], "x", "y", fallback="opus")[0], "opus")

    def test_bad_rows_are_skipped(self):
        rows = [{"match": "(", "model": "m1"}, {"model": "m2"}, {"match": "x", "model": "bad model!"},
                {"match": "x", "model": "m3", "type": "t", "why": "w"}]
        self.assertEqual(models.route(rows, "", "x"), ("m3", "t", "w"))


def write_config(**cfg):
    with open(os.environ["OFFICE_CONFIG"], "w", encoding="utf-8") as f:
        json.dump(cfg, f)


class Flags(unittest.TestCase):
    def setUp(self):
        write_config(permission_mode="auto", agent_args=["--model", "sonnet"],
                     manager_args=["--model", "opus"], model_rules=TABLE)

    def test_model_is_always_explicit(self):
        for task in ("anything at all", "Propose design directions", "Summarize my inbox"):
            flags = office._agent_flags(["--model", "sonnet"], AREA, task)
            self.assertEqual(flags.count("--model"), 1, task)
        # even when the config args name no model at all
        write_config(permission_mode="auto", model_rules=TABLE)
        self.assertIn("--model", office._agent_flags([], AREA, "fix the footer"))
        write_config(permission_mode="auto")
        self.assertEqual(office._agent_flags([], AREA, "x")[:2], ["--model", models.DEFAULT_MODEL])

    def test_override_wins_over_the_table(self):
        flags = office._agent_flags(["--model", "sonnet"], AREA, "Summarize my inbox",
                                    override="claude-opus-4-8")
        self.assertEqual(flags[flags.index("--model") + 1], "claude-opus-4-8")

    def test_manager_ignores_the_table(self):
        self.assertEqual(office._route_model(["--model", "opus"], AREA, "demo", rules=False)[:2],
                         ("opus", "manager"))


class Spawn(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.popen = office.subprocess.Popen
        office.subprocess.Popen = lambda args, **kw: self.calls.append((args, kw))
        os.environ["OFFICE_ALLOW_SPAWN"] = "1"
        self.folder = os.path.join(_TMP, "PORTFOLIO")
        os.makedirs(os.path.join(self.folder, ".claude"), exist_ok=True)
        with open(os.path.join(self.folder, ".claude", "settings.json"), "w") as f:
            json.dump({"hooks": {"Stop": [{"hooks": [{"command": "python %s/hook.py"
                                                       % os.path.basename(ROOT)}]}]}}, f)
        write_config(permission_mode="auto", agent_args=["--model", "sonnet"], model_rules=TABLE)

    def tearDown(self):
        office.subprocess.Popen = self.popen

    def model_arg(self):
        args = self.calls[-1][0]
        return args[args.index("--model") + 1]

    def test_spawn_reports_model_type_why_and_passes_it(self):
        r = office.spawn_agent({"cwd": self.folder, "task": "Research competitor sites"})
        self.assertTrue(r["ok"], r)
        self.assertEqual((r["model"], r["type"]), (SONNET, "research"))
        self.assertTrue(r["why"])
        self.assertEqual(self.model_arg(), SONNET)

    def test_spawn_override(self):
        r = office.spawn_agent({"cwd": self.folder, "task": "Summarize my inbox",
                                "model": "claude-opus-4-8"})
        self.assertEqual((r["model"], r["type"]), ("claude-opus-4-8", "override"))
        self.assertEqual(self.model_arg(), "claude-opus-4-8")

    def test_spawn_rejects_a_bad_model_name(self):
        r = office.spawn_agent({"cwd": self.folder, "task": "x", "model": "--dangerous; rm"})
        self.assertFalse(r["ok"])
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
