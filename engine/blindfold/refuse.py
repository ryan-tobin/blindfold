"""Refusals: every refused question gets a rule code and a plain reason.

The codes match the brief's five refusal rules. spec.py raises rules 1-3 and
the per-query epsilon cap of rule 4; app.py raises the rest of rule 4 (the
remaining budget) and rule 5 (the rate limit). A refusal costs no budget and
no payment, and its code and reason are what gets logged on-chain.
"""

from __future__ import annotations

NOT_AGGREGATE = "not_aggregate"  # 1. asks for rows, IDs, free text or individual records
OFF_SCHEMA = "off_schema"        # 2. unknown table, column, operator or value
TOO_FINE = "too_fine"            # 3. too many groups or cells, or narrows in on a few people
OVER_BUDGET = "over_budget"      # 4. epsilon above the per-query cap or the remaining budget
RATE_LIMITED = "rate_limited"    # 5. too many queries per buyer per hour

RULES = {NOT_AGGREGATE: 1, OFF_SCHEMA: 2, TOO_FINE: 3, OVER_BUDGET: 4, RATE_LIMITED: 5}


class Refused(Exception):
    """A question the engine will not answer, with the rule it broke."""

    def __init__(self, rule: str, reason: str):
        if rule not in RULES:
            raise ValueError(f"unknown refusal rule {rule!r}")
        super().__init__(f"{rule}: {reason}")
        self.rule = rule
        self.reason = reason

    def to_dict(self) -> dict:
        return {"refused": True, "rule": self.rule, "rule_number": RULES[self.rule], "reason": self.reason}