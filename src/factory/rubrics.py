"""Grading rubrics for finance & accounting training data.

Rubrics drive both the autonomous LLM QC reviewer and human reviewers, so both
score against identical criteria. Each criterion is scored 0–5; the overall
score is the weighted mean. `critical=True` criteria are gate criteria: a score
below 3 on any of them forces revision regardless of the overall score.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Criterion:
    key: str
    description: str
    weight: float = 1.0
    critical: bool = False


@dataclass(frozen=True)
class Rubric:
    id: str
    name: str
    criteria: tuple[Criterion, ...] = field(default_factory=tuple)

    def weights_total(self) -> float:
        return sum(c.weight for c in self.criteria)


_COMMON = (
    Criterion(
        "technical_accuracy",
        "All accounting/finance assertions, calculations, and figures are correct. "
        "Journal entries balance, statements tie out, formulas are applied correctly.",
        weight=3.0, critical=True,
    ),
    Criterion(
        "standards_grounding",
        "Claims that depend on authoritative guidance cite the correct standard "
        "(ASC topic, IFRS/IAS number, IRC section) and characterize it accurately. "
        "No fabricated or misattributed citations.",
        weight=2.0, critical=True,
    ),
    Criterion(
        "completeness",
        "Fully answers the prompt: covers all requested items, states material "
        "assumptions, and addresses edge cases the prompt raises.",
        weight=1.5,
    ),
    Criterion(
        "reasoning_quality",
        "Shows clear, step-by-step professional reasoning a junior practitioner "
        "could follow and learn from; no leaps or circular logic.",
        weight=1.5,
    ),
    Criterion(
        "clarity_style",
        "Professional tone, well-structured, correct terminology, no filler or "
        "hedging boilerplate.",
        weight=1.0,
    ),
)

RUBRICS: dict[str, Rubric] = {
    "fin-sft-v1": Rubric(
        id="fin-sft-v1",
        name="Finance/Accounting SFT response quality v1",
        criteria=_COMMON,
    ),
    "fin-pref-v1": Rubric(
        id="fin-pref-v1",
        name="Finance/Accounting preference pair quality v1",
        criteria=_COMMON + (
            Criterion(
                "pair_contrast",
                "The chosen response is genuinely and instructively better than the "
                "rejected one; the rejected response contains realistic (not strawman) "
                "flaws typical of weaker model outputs.",
                weight=2.0, critical=True,
            ),
        ),
    ),
    "fin-eval-v1": Rubric(
        id="fin-eval-v1",
        name="Finance/Accounting eval item quality v1",
        criteria=_COMMON + (
            Criterion(
                "gradeability",
                "The reference answer is unambiguous and the grading notes allow an "
                "independent grader to score a candidate answer consistently.",
                weight=2.0, critical=True,
            ),
        ),
    ),
}


def get_rubric(rubric_id: str) -> Rubric:
    if rubric_id not in RUBRICS:
        raise KeyError(f"unknown rubric: {rubric_id}")
    return RUBRICS[rubric_id]


def default_rubric_for(task_type: str) -> str:
    return {"sft": "fin-sft-v1", "preference": "fin-pref-v1", "eval": "fin-eval-v1"}[task_type]


# --- Submission content shapes (enforced by validators.check_schema) ----------
#
# sft:        {"response": str, "citations": [str], "assumptions": [str]?, "financials": {...}?}
# preference: {"chosen": str, "rejected": str, "rejection_rationale": str, "citations": [str]}
# eval:       {"answer": str, "grading_notes": str, "citations": [str]}
#
# Optional "financials" enables deterministic tie-out checks, e.g.:
#   {"balance_sheet": {"assets": 100.0, "liabilities": 60.0, "equity": 40.0}}
#   {"journal_entries": [{"account": "Cash", "debit": 100, "credit": 0}, ...]}

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "sft": ("response", "citations"),
    "preference": ("chosen", "rejected", "rejection_rationale", "citations"),
    "eval": ("answer", "grading_notes", "citations"),
}

MIN_RESPONSE_CHARS = 200
