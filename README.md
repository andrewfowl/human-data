# Human Data Factory — Finance & Accounting

A boutique human-data factory for LLM training: vetted finance and accounting
experts (CPA / CFA / auditors) produce SFT, preference, and evaluation
datasets, with **internal controls embedded directly in the workflow** and an
**autonomous LLM quality-control reviewer** in the pipeline. Think Mercor or
Micro1, but specialized in one vertical and built around an auditable control
environment rather than throughput alone.

## What it does

```
Expert onboarding ─▶ Qualification exam (per domain track)
                          │
Client project ─▶ Tasks (incl. hidden gold/honeypot tasks)
                          │
                     Submission
                          │
   Gate 1  Deterministic validators   schema, PII, unbalanced journal entries /
                          │           balance sheets, fabricated ASC/IFRS/IAS
                          │           citations, placeholders, near-duplicates
   Gate 2  Gold calibration           honeypots score the contributor, never ship
                          │
   Gate 3  Autonomous QC review       Claude scores against the project rubric
                          │           (structured JSON verdict: per-criterion
                          │           scores, flags, pass/fail)
   Gate 4  Human review               sampled adaptively by contributor trust,
                          │           mandatory in the gray zone; blind & duty-
                          │           segregated
                     APPROVED
                          │
   Gate 5  Dual-control export        clean control report + second approver →
                                      JSONL + manifest (checksums, audit head)
```

## Internal controls

| # | Control | Enforcement |
|---|---------|-------------|
| C1 | **Qualification gating** — only experts with a passed exam in the project's domain track can be assigned or submit | `controls.assert_qualified`, checked at assignment and submission |
| C2 | **Segregation of duties** — authors never review their own work; the review queue is blind (no author identity) | `controls.assert_reviewer_independent` |
| C3 | **Adaptive sampling** — human-review rate scales with demonstrated quality (100% for new contributors → 10% floor for trusted ones); the sampling decision is derived deterministically from the submission id so it is reproducible in an audit | `controls.is_sampled_for_human_review` |
| C4 | **Gold-task calibration** — honeypot tasks with hidden answers measure contributor accuracy; misses cut the trust score, repeated misses suspend | `controls.register_gold_result` |
| C5 | **Contributor scoring** — EWMA quality score from review outcomes drives sampling tier and automatic suspension | `controls.update_quality_score` |
| C6 | **Dual-control releases** — an export needs a clean control report *and* a second approver distinct from the requester | `export.request_export` / `approve_and_materialize` |
| — | **Hash-chained audit trail** — every state transition is an append-only event whose hash covers its predecessor; tampering is detectable | `audit.verify_chain`, `GET /audit/verify` |

## Autonomous QC reviewer

`factory/qc/auto_reviewer.py` sends each submission plus the project rubric to
a Claude model (default `claude-opus-4-8`, override with `HDF_QC_MODEL`) with a
JSON-schema-constrained output, so the verdict — per-criterion scores 0–5,
flags, summary — is machine-parseable by construction. Critical flags
(`CALCULATION_ERROR`, `FABRICATED_CITATION`, `HALLUCINATION_RISK`, `PII`) or a
sub-3 score on a critical rubric criterion force revision regardless of the
overall score.

Without an Anthropic credential (or with `HDF_QC_OFFLINE=1`) a deterministic
heuristic reviewer runs instead. Fallback reviews are flagged
`OFFLINE_FALLBACK` and **can never auto-approve** — they always route to a
human reviewer, so degraded automation degrades to more human oversight, not
less.

## Quickstart

```bash
pip install -e ".[dev]"

# Run everything offline (no API key needed)
export HDF_QC_OFFLINE=1

hdf seed-demo                     # demo project: 2 SFT tasks + 1 gold task
hdf control-report <project_id>
hdf serve                         # API at http://localhost:8000/docs
hdf export <project_id> --requested-by ops-admin --approved-by <reviewer_id>
hdf verify-audit
```

With `ANTHROPIC_API_KEY` set (and `HDF_QC_OFFLINE` unset), gate 3 runs real
autonomous reviews.

```bash
pytest        # 45 tests, fully offline
```

## API surface

- `POST /experts`, `POST /experts/{id}/exams` — onboarding & qualification
- `POST /projects`, `POST /projects/{id}/tasks`, `POST /tasks/{id}/assign/{expert}`
- `POST /tasks/{id}/submissions` — runs gates 1–3 synchronously
- `GET /review-queue`, `POST /submissions/{id}/human-review` — gate 4
- `GET /projects/{id}/control-report`, `POST /projects/{id}/exports`,
  `POST /exports/{id}/approve` — gate 5
- `GET /audit/verify`, `GET /metrics`

Mutating endpoints take an `X-Actor-Id` header for audit attribution — swap in
real authentication before production.

## Dataset formats

Approved, non-gold records export as JSONL with a `manifest.json` carrying
record counts, per-file SHA-256 digests, the audit-chain head at release time,
and both approver identities:

- **sft**: `{prompt, completion, citations, metadata}` (+ a `chat.jsonl` in
  messages format)
- **preference**: `{prompt, chosen, rejected, rejection_rationale, citations, metadata}`
- **eval**: `{question, reference_answer, grading_notes, citations, metadata}`

## Layout

```
src/factory/
  models.py        ORM: experts, qualifications, projects, tasks, submissions,
                   reviews, audit events, export batches
  rubrics.py       finance/accounting grading rubrics (shared by LLM + human reviewers)
  validators.py    gate 1: deterministic checks
  controls.py      C1–C6 control policies
  audit.py         hash-chained audit trail
  qc/auto_reviewer.py   gate 3: Claude reviewer + offline fallback
  qc/engine.py     pipeline state machine
  export.py        gate 5: control report + dual-control JSONL release
  api/main.py      FastAPI app
  cli.py           `hdf` operator CLI
docs/QUALITY_MANUAL.md   control objectives and operating procedures
tests/                   45 offline tests
```

See [docs/QUALITY_MANUAL.md](docs/QUALITY_MANUAL.md) for the control
objectives, rubric definitions, and operating procedures.
