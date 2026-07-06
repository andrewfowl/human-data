"""Autonomous QC reviewer.

Primary path: a Claude model scores the submission against the project rubric
and returns a structured verdict (per-criterion scores, flags, pass/fail). The
response format is constrained with `output_config.format` (JSON schema), so
the result is machine-parseable by construction.

Fallback path: when no Anthropic credential is available (or HDF_QC_OFFLINE is
set), a deterministic heuristic reviewer produces a conservative score. Every
fallback review is flagged OFFLINE_FALLBACK in its detail so downstream
controls and auditors can distinguish the two — fallback reviews never qualify
a submission for auto-approval (see engine.py).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..config import settings
from ..rubrics import Rubric
from ..validators import ValidationResult

AUTO_REVIEWER_ID = "auto-qc/claude"
FALLBACK_REVIEWER_ID = "auto-qc/heuristic-v1"

# Flags the reviewer may raise; CRITICAL_FLAGS force revision regardless of score.
CRITICAL_FLAGS = {"CALCULATION_ERROR", "FABRICATED_CITATION", "HALLUCINATION_RISK", "PII"}


@dataclass
class AutoReview:
    reviewer_id: str
    overall_score: float  # 0–5 weighted
    passed: bool
    criterion_scores: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    summary: str = ""

    def as_detail(self) -> dict:
        return {
            "criterion_scores": self.criterion_scores,
            "flags": self.flags,
            "summary": self.summary,
            "model": settings.qc_model if self.reviewer_id == AUTO_REVIEWER_ID else None,
        }


def _review_schema(rubric: Rubric) -> dict:
    keys = [c.key for c in rubric.criteria]
    return {
        "type": "object",
        "properties": {
            "criterion_scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "criterion": {"type": "string", "enum": keys},
                        "score": {"type": "integer", "enum": [0, 1, 2, 3, 4, 5]},
                        "justification": {"type": "string"},
                    },
                    "required": ["criterion", "score", "justification"],
                    "additionalProperties": False,
                },
            },
            "flags": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": sorted(CRITICAL_FLAGS | {"STYLE_ISSUE", "INCOMPLETE", "WEAK_CONTRAST"}),
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["criterion_scores", "flags", "summary"],
        "additionalProperties": False,
    }


_SYSTEM = """You are the autonomous quality-control reviewer for a boutique data factory that \
produces finance and accounting training datasets for LLM labs. You review one expert \
submission at a time against a fixed rubric.

You are an exacting reviewer with deep expertise in US GAAP, IFRS, tax, audit, and \
financial analysis. Recompute every figure yourself. Verify every cited standard says \
what the submission claims it says. A submission that is fluent but contains a single \
wrong number, an unbalanced entry, or a misattributed standard must score low on \
technical_accuracy or standards_grounding.

Score each rubric criterion from 0 (unusable) to 5 (exemplary training data). Raise flags \
only when warranted: CALCULATION_ERROR, FABRICATED_CITATION, HALLUCINATION_RISK, PII, \
STYLE_ISSUE, INCOMPLETE, WEAK_CONTRAST (preference pairs only). Be strict — this data \
trains models; errors here propagate."""


def _build_user_prompt(task_prompt: str, task_context: dict, task_type: str,
                       content: dict, rubric: Rubric,
                       validation: ValidationResult | None) -> str:
    criteria = "\n".join(
        f"- {c.key} (weight {c.weight}{', CRITICAL' if c.critical else ''}): {c.description}"
        for c in rubric.criteria
    )
    warn = ""
    if validation and validation.findings:
        warn = "\nDeterministic pre-checks reported:\n" + "\n".join(
            f"- [{f.severity}] {f.code}: {f.message}" for f in validation.findings
        )
    return (
        f"TASK TYPE: {task_type}\n\n"
        f"TASK PROMPT GIVEN TO THE EXPERT:\n{task_prompt}\n\n"
        f"TASK CONTEXT:\n{json.dumps(task_context, indent=2, default=str)}\n\n"
        f"EXPERT SUBMISSION:\n{json.dumps(content, indent=2, default=str)}\n\n"
        f"RUBRIC ({rubric.name}):\n{criteria}\n{warn}\n\n"
        "Review the submission and return your structured verdict."
    )


def _weighted_overall(rubric: Rubric, criterion_scores: list[dict]) -> float:
    by_key = {c.key: c for c in rubric.criteria}
    total_w, acc = 0.0, 0.0
    for row in criterion_scores:
        c = by_key.get(row["criterion"])
        if c is None:
            continue
        acc += c.weight * float(row["score"])
        total_w += c.weight
    return round(acc / total_w, 3) if total_w else 0.0


def _critical_gate_failed(rubric: Rubric, criterion_scores: list[dict]) -> bool:
    critical = {c.key for c in rubric.criteria if c.critical}
    return any(row["criterion"] in critical and row["score"] < 3 for row in criterion_scores)


def _anthropic_available() -> bool:
    if settings.qc_offline:
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def review(task_prompt: str, task_context: dict, task_type: str, content: dict,
           rubric: Rubric, validation: ValidationResult | None = None) -> AutoReview:
    if _anthropic_available():
        try:
            return _review_with_claude(task_prompt, task_context, task_type, content, rubric, validation)
        except Exception:
            # API failure must not stall the pipeline; the fallback review routes
            # the submission to human review rather than approving it.
            pass
    return _review_heuristic(task_type, content, rubric, validation)


def _review_with_claude(task_prompt: str, task_context: dict, task_type: str,
                        content: dict, rubric: Rubric,
                        validation: ValidationResult | None) -> AutoReview:
    import anthropic

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=settings.qc_model,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": _review_schema(rubric)}},
        messages=[{
            "role": "user",
            "content": _build_user_prompt(task_prompt, task_context, task_type,
                                          content, rubric, validation),
        }],
    )
    if response.stop_reason == "refusal":
        # Treat a safety decline as non-reviewable; route to human review.
        return AutoReview(
            reviewer_id=AUTO_REVIEWER_ID, overall_score=0.0, passed=False,
            flags=["HALLUCINATION_RISK"],
            summary="Model declined to review this submission; routed to human review.",
        )
    text = next(b.text for b in response.content if b.type == "text")
    data = json.loads(text)
    overall = _weighted_overall(rubric, data["criterion_scores"])
    critical_flags = sorted(set(data["flags"]) & CRITICAL_FLAGS)
    passed = (
        overall >= settings.qc_pass_threshold
        and not critical_flags
        and not _critical_gate_failed(rubric, data["criterion_scores"])
    )
    return AutoReview(
        reviewer_id=AUTO_REVIEWER_ID,
        overall_score=overall,
        passed=passed,
        criterion_scores=data["criterion_scores"],
        flags=data["flags"],
        summary=data["summary"],
    )


def _review_heuristic(task_type: str, content: dict, rubric: Rubric,
                      validation: ValidationResult | None) -> AutoReview:
    """Conservative offline scorer. Never awards auto-approval-grade confidence."""
    main_key = {"sft": "response", "preference": "chosen", "eval": "answer"}.get(task_type, "response")
    text = str(content.get(main_key, ""))
    citations = content.get("citations") or []

    base = 3.0
    if len(text) >= 800:
        base += 0.5
    if citations:
        base += 0.5
    warnings = len(validation.findings) if validation else 0
    base -= 0.25 * warnings
    score = max(0.0, min(5.0, round(base, 2)))

    criterion_scores = [
        {"criterion": c.key, "score": int(round(score)),
         "justification": "heuristic offline estimate"}
        for c in rubric.criteria
    ]
    return AutoReview(
        reviewer_id=FALLBACK_REVIEWER_ID,
        overall_score=score,
        passed=False,  # offline reviews are never sufficient for auto-approval
        criterion_scores=criterion_scores,
        flags=["OFFLINE_FALLBACK"],
        summary="Offline heuristic review; submission requires human review before approval.",
    )
