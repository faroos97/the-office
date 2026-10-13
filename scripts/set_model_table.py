"""Writes the ordered model routing table into config.json (model_rules).

config.json is the operator's own file (gitignored), so the table is kept here, in the
repo, as the source: run `python scripts/set_model_table.py` to (re)apply it. Regexes are
raw strings; json.dump does the escaping (\\b in the file).
"""
import json
import os

PATH = os.environ.get("OFFICE_CONFIG") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

FABLE = "claude-fable-5-1"
OPUS48 = "claude-opus-4-8"
OPUS55 = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
HAIKU = "claude-haiku-4-5-20251001"

TABLE = [
    {"type": "design proposal",
     "match": r"design direction|proposal|concept|comps|art direction|maquette",
     "model": FABLE, "why": "taste decides the outcome: the best model proposes"},
    {"type": "build / demo / 3D / animation",
     "match": r"mockups|\bdemos?\b|nordvia|verso|\b3d\b|animation|gsap",
     "model": OPUS48, "why": "long careful build of an accepted design"},
    {"type": "marketing",
     "match": r"\bads\b|google ads|meta ads|keywords|mots-cl[eé]s|copywriting|\bcopy\b|\bangles?\b"
              r"|campaign|outreach|cold email",
     "model": OPUS55, "why": "persuasion and money-facing text"},
    {"type": "legal / tax",
     "match": r"contract|contrat|loi 25|law 25|legal|\btax|imp[oô]t|fiscal",
     "model": OPUS55, "why": "wrong answers are expensive"},
    {"type": "strategy / QA / review",
     "match": r"strategy|\bplan(ning)?\b|roadmap|review|\bQA\b",
     "model": OPUS55, "why": "judgment work, needs a strong second opinion"},
    {"type": "research",
     "match": r"research|competitor|concurrent|\bmarkets?\b",
     "model": SONNET, "why": "reading and summarising, cheap and good enough"},
    {"type": "content",
     "match": r"blog|article|checklist|seo page|content",
     "model": SONNET, "why": "routine writing"},
    {"type": "triage",
     "match": r"email triage|summar|inbox",
     "model": HAIKU, "why": "high volume, low stakes"},
    {"type": "default", "match": r".*",
     "model": SONNET, "why": "everything else"},
]

if __name__ == "__main__":
    with open(PATH, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["model_rules"] = TABLE
    with open(PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print("model_rules: %d rows written to %s" % (len(TABLE), PATH))
