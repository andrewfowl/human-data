"""Deterministic pre-QC validators (control gate 1).

These run before any LLM review and catch objective, machine-checkable defects:
schema violations, PII leakage, unbalanced financials, invalid standards
citations, placeholder/AI-refusal text, and near-duplicate content.

Each finding has a severity: "error" findings block the submission
(AUTO_CHECK_FAILED → needs revision); "warning" findings are attached to the
record and passed to downstream reviewers as context.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .rubrics import MIN_RESPONSE_CHARS, REQUIRED_FIELDS


@dataclass
class Finding:
    code: str
    severity: str  # "error" | "warning"
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message}


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def passed(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"passed": self.passed, "findings": [f.as_dict() for f in self.findings]}


# ---------------------------------------------------------------- PII patterns
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"\b(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_EIN = re.compile(r"\b\d{2}-\d{7}\b")

# Placeholder / refusal / low-effort markers
_PLACEHOLDER = re.compile(
    r"(lorem ipsum|\[insert|\[your |TODO:|TBD\b|xxx+\b|as an ai\b|i cannot assist|"
    r"i'm sorry,? but i can)",
    re.IGNORECASE,
)

# Standards citations. We validate the *form* and, for ASC/IAS, membership in
# the known topic/standard sets — a cheap fabrication check.
_ASC = re.compile(r"\bASC\s+(\d{3})(?:-\d{1,3})*\b")
_IFRS = re.compile(r"\bIFRS\s+(\d{1,2})\b")
_IAS = re.compile(r"\bIAS\s+(\d{1,2})\b")
_IRC = re.compile(r"\b(?:IRC\s+)?[Ss]ection\s+\d{1,4}[A-Z]?\b")

# FASB codification topics in issue (top-level).
VALID_ASC_TOPICS = {
    105, 205, 210, 215, 220, 225, 230, 235, 250, 255, 260, 270, 272, 274, 275, 280,
    305, 310, 320, 321, 323, 325, 326, 330, 340, 350, 360,
    405, 410, 420, 430, 440, 450, 460, 470, 480,
    505, 605, 606, 610, 705, 710, 712, 715, 718, 720, 730, 740,
    805, 808, 810, 815, 820, 825, 830, 832, 835, 840, 842, 845, 848, 850, 852, 853, 855, 860,
    905, 908, 910, 912, 915, 920, 922, 924, 926, 928, 930, 932, 940, 942, 944, 946, 948,
    950, 952, 954, 958, 960, 962, 965, 970, 972, 974, 976, 978, 980, 985, 995,
}
VALID_IFRS = set(range(1, 20))       # IFRS 1–19 issued (incl. IFRS 18/19, 2024)
VALID_IAS = set(range(1, 42))        # IAS numbers in the historical series

_BALANCE_TOL = 0.01


def _text_blob(content: dict) -> str:
    parts: list[str] = []
    for v in content.values():
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.extend(x for x in v if isinstance(x, str))
    return "\n".join(parts)


def check_schema(content: dict, task_type: str) -> list[Finding]:
    out: list[Finding] = []
    required = REQUIRED_FIELDS.get(task_type, ())
    for f in required:
        v = content.get(f)
        if v is None or (isinstance(v, (str, list)) and not v):
            out.append(Finding("SCHEMA_MISSING_FIELD", "error", f"required field '{f}' is missing or empty"))
    main_key = {"sft": "response", "preference": "chosen", "eval": "answer"}.get(task_type)
    if main_key:
        main = content.get(main_key) or ""
        if isinstance(main, str) and 0 < len(main) < MIN_RESPONSE_CHARS:
            out.append(Finding(
                "SCHEMA_TOO_SHORT", "error",
                f"'{main_key}' is {len(main)} chars; minimum is {MIN_RESPONSE_CHARS}",
            ))
    return out


def check_pii(content: dict) -> list[Finding]:
    text = _text_blob(content)
    out: list[Finding] = []
    if _SSN.search(text):
        out.append(Finding("PII_SSN", "error", "text contains an SSN-formatted number"))
    if _EIN.search(text):
        out.append(Finding("PII_EIN", "warning", "text contains an EIN-formatted number; confirm it is fictitious"))
    if _EMAIL.search(text):
        out.append(Finding("PII_EMAIL", "error", "text contains an email address"))
    if _PHONE.search(text):
        out.append(Finding("PII_PHONE", "warning", "text contains a phone-number-like string"))
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            out.append(Finding("PII_CARD", "error", "text contains a Luhn-valid card-like number"))
            break
    return out


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n *= 2
            if n > 9:
                n -= 9
        total += n
        alt = not alt
    return total % 10 == 0


def check_placeholders(content: dict) -> list[Finding]:
    text = _text_blob(content)
    m = _PLACEHOLDER.search(text)
    if m:
        return [Finding("PLACEHOLDER_TEXT", "error",
                        f"placeholder/refusal marker found: {m.group()!r}")]
    return []


def check_citations(content: dict) -> list[Finding]:
    """Validate cited standards against known topic sets (fabrication check)."""
    out: list[Finding] = []
    text = _text_blob(content)
    for m in _ASC.finditer(text):
        topic = int(m.group(1))
        if topic not in VALID_ASC_TOPICS:
            out.append(Finding("CITATION_INVALID_ASC", "error",
                               f"ASC {topic} is not a valid codification topic"))
    for m in _IFRS.finditer(text):
        if int(m.group(1)) not in VALID_IFRS:
            out.append(Finding("CITATION_INVALID_IFRS", "error",
                               f"IFRS {m.group(1)} does not exist"))
    for m in _IAS.finditer(text):
        if int(m.group(1)) not in VALID_IAS:
            out.append(Finding("CITATION_INVALID_IAS", "error",
                               f"IAS {m.group(1)} is not in the IAS series"))
    citations = content.get("citations") or []
    if isinstance(citations, list) and not citations:
        out.append(Finding("CITATION_NONE", "warning",
                           "no authoritative citations provided"))
    return out


def check_financial_consistency(content: dict) -> list[Finding]:
    """Tie-out checks over the optional structured `financials` block."""
    out: list[Finding] = []
    fin = content.get("financials")
    if not isinstance(fin, dict):
        return out

    bs = fin.get("balance_sheet")
    if isinstance(bs, dict):
        try:
            a, l, e = float(bs["assets"]), float(bs["liabilities"]), float(bs["equity"])
            if abs(a - (l + e)) > _BALANCE_TOL:
                out.append(Finding(
                    "FIN_BALANCE_SHEET", "error",
                    f"balance sheet does not balance: assets {a} != liabilities {l} + equity {e}",
                ))
        except (KeyError, TypeError, ValueError):
            out.append(Finding("FIN_BALANCE_SHEET_SHAPE", "error",
                               "balance_sheet must contain numeric assets/liabilities/equity"))

    entries = fin.get("journal_entries")
    if isinstance(entries, list) and entries:
        try:
            debits = sum(float(x.get("debit", 0) or 0) for x in entries)
            credits = sum(float(x.get("credit", 0) or 0) for x in entries)
            if abs(debits - credits) > _BALANCE_TOL:
                out.append(Finding(
                    "FIN_JE_UNBALANCED", "error",
                    f"journal entries do not balance: debits {debits} != credits {credits}",
                ))
        except (TypeError, ValueError, AttributeError):
            out.append(Finding("FIN_JE_SHAPE", "error",
                               "journal_entries must be a list of {account, debit, credit}"))
    return out


def content_fingerprint(content: dict, task_type: str) -> str:
    """Normalized hash of the main text, for near-duplicate detection."""
    main_key = {"sft": "response", "preference": "chosen", "eval": "answer"}.get(task_type, "response")
    text = str(content.get(main_key, ""))
    normalized = re.sub(r"\W+", "", text.lower())
    return hashlib.sha256(normalized.encode()).hexdigest()


def check_duplicate(content: dict, task_type: str, existing_fingerprints: set[str]) -> list[Finding]:
    if content_fingerprint(content, task_type) in existing_fingerprints:
        return [Finding("DUPLICATE_CONTENT", "error",
                        "content duplicates an existing submission in this project")]
    return []


def run_all(content: dict, task_type: str,
            existing_fingerprints: set[str] | None = None) -> ValidationResult:
    result = ValidationResult()
    result.findings += check_schema(content, task_type)
    result.findings += check_pii(content)
    result.findings += check_placeholders(content)
    result.findings += check_citations(content)
    result.findings += check_financial_consistency(content)
    if existing_fingerprints:
        result.findings += check_duplicate(content, task_type, existing_fingerprints)
    return result
