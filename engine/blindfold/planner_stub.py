"""The offline planner: the 5 demo questions, no API key, no network.

Local mode (and the hackathon's AI screener) runs without a NEAR AI key, so
POST /plan falls back to this stub. It knows exactly the 5 demo questions in
datasets/riverside/planner_questions.json, matched ignoring case and
punctuation, and returns their QuerySpecs. Any other question gets a clear
"set NEAR_AI_API_KEY" error. It never guesses.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import load
from .planner import Plan, PlanError, check_spec
from .refuse import Refused

QUESTIONS = load.REPO_ROOT / "datasets" / "riverside" / "planner_questions.json"


def normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9+]+", " ", text.lower()).split())


def demo_specs(path: Path = QUESTIONS) -> dict[str, dict]:
    """Normalized demo question -> its QuerySpec."""
    out = {}
    for g in json.loads(Path(path).read_text())["groups"]:
        spec = g["stub_spec"] if g["expected"] == "refused" else g["expected"][0]
        out[normalize(g["question"])] = spec
    return out


class StubPlanner:
    def __init__(self, schema: dict, path: Path = QUESTIONS):
        self.schema = schema
        self.specs = demo_specs(path)
        self.questions = [g["question"] for g in json.loads(Path(path).read_text())["groups"]]

    def plan(self, question: str) -> Plan:
        raw = self.specs.get(normalize(question))
        if raw is None:
            raise PlanError("this engine has no NEAR AI key, so it only understands the 5 demo questions: "
                            + " | ".join(self.questions) + ". Set NEAR_AI_API_KEY to ask anything else.")
        try:
            spec, valid, refusal = check_spec(raw, self.schema)
        except Refused as r:
            raise PlanError(f"demo question has a broken spec: {r.reason}") from r
        return Plan("stub", None, spec, valid, refusal)