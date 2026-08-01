# Quality Manual — Internal Control System

This document describes the control environment for the finance & accounting
human-data factory: control objectives, how each control operates, and the
evidence it produces. It is written in the style of a SOC-oriented control
matrix so client labs can audit the dataset production process.

## 1. Control environment

**Scope.** All activity from expert onboarding through dataset release.
**Actors.** Experts (authors), reviewers, operations admins, the QC engine
(`qc-engine`), the autonomous reviewer (`auto-qc/claude` or
`auto-qc/heuristic-v1`).
**Evidence.** Every control produces (a) a database record (review rows,
status transitions) and (b) a hash-chained audit event. The chain is verified
on demand (`GET /audit/verify`, `hdf verify-audit`) and at every export.

## 2. Control matrix

### C1 — Qualification gating
*Objective:* work in a domain track is performed only by experts who have
demonstrated competence in that track.
*Operation:* an expert must hold a passed qualification exam (score ≥ 85) for
the project's track before task assignment or submission. Enforced in code at
both points; violations return HTTP 403 and are never silently bypassed.
*Evidence:* `qualifications` rows; `expert.exam_recorded` audit events;
rejected attempts surface as API errors.

### C2 — Segregation of duties
*Objective:* no one evaluates their own work; no single person can both
request and approve a release.
*Operation:* human reviews are rejected when reviewer == author, when the
reviewer lacks the reviewer role, or when the reviewer is not in QUALIFIED
status. The review queue endpoint intentionally omits author identity (blind
review). Export approval requires a second person (see C6).
*Evidence:* `reviews.reviewer_id` vs `submissions.expert_id`; audit events.

### C3 — Adaptive human-review sampling
*Objective:* human oversight is concentrated where risk is highest while every
contributor retains a minimum inspection rate.
*Operation:* sampling tiers — new contributors (fewer than 5 approved items):
100%; standard: 35%; trusted (score ≥ 4.2 and ≥ 20 approved): 10% floor. The
decision hashes the submission id into [0,1) and compares to the rate, so any
auditor can re-derive why a given item was or was not sampled. Gray-zone
auto-QC scores and offline-fallback reviews are always routed to human review
independent of sampling.
*Evidence:* `submissions.sampled_for_human_review`; the deterministic function
`controls.is_sampled_for_human_review`.

### C4 — Gold-task calibration
*Objective:* continuously measure contributor accuracy against known answers.
*Operation:* projects seed hidden honeypot tasks (`is_gold=true`). The gold
answer is never exposed through any API. Submissions to gold tasks are scored
by keyword coverage against the hidden answer; a miss applies a 0.75
multiplier to the contributor's quality score, and three misses suspend the
contributor. Gold submissions never enter exported datasets.
*Evidence:* `gold_check` review rows; `submission.gold_checked` audit events;
`expert.gold_pass_count` / `gold_fail_count`.

### C5 — Contributor quality scoring
*Objective:* a single trust metric drives sampling, routing, and suspension.
*Operation:* EWMA (α = 0.3) over review scores on a 0–5 scale, initialized at
3.5. Scores below 2.0 suspend automatically. The score feeds C3 tiers.
*Evidence:* `experts.quality_score` history reconstructible from review rows
and audit events.

### C6 — Dual-control dataset releases
*Objective:* no dataset ships without independent approval and a clean control
report.
*Operation:* an export request is only accepted when the control report is
clean: every shipped record has review evidence (auto-LLM or human PASS), no
tasks are silently dropped without visibility, and the audit chain verifies.
Materialization requires an approver distinct from the requester. The manifest
records counts, per-file SHA-256, the audit-chain head, and both identities.
*Evidence:* `export_batches` rows; `manifest.json` in the release artifact;
`export.requested` / `export.released` audit events.

### C7 — Tamper-evident audit trail
*Objective:* the history of the factory cannot be silently rewritten.
*Operation:* audit events form a SHA-256 hash chain from a genesis value; each
event's hash covers the previous hash and the event body. `verify_chain` walks
the full chain; any modification of a historical record breaks verification.
*Evidence:* `GET /audit/verify`; chain head embedded in every export manifest.

### C8 — Grader calibration & versioning
*Objective:* the autonomous reviewer is continuously measured against human
judgment, and every score is attributable to a pinned grader.
*Operation:* every sampled submission carries both an auto-LLM and a human
verdict; `GET /qc/calibration` computes verdict agreement, false-pass rate
(auto passed, human did not — the direction that would ship a bad record
without sampling), false-hold rate, and exact/adjacent score agreement,
sliced by domain track and grader version. A false-pass rate above 5%
produces a `raise_sampling` recommendation feeding C3; strong agreement
(≥90% on ≥50 pairs) flags the sampling floors for review. Each auto review
is stamped with a `grader_version` (prompt version / rubric id / resolved
model), so score drift can be attributed to grader changes vs contributor
changes. Auto reviews must quote verbatim evidence per criterion; a review
the model cannot ground (`insufficient_evidence`) never auto-approves.
*Evidence:* review `detail` records (evidence quotes, grader version);
calibration endpoint output; audit trail.

## 3. The QC pipeline (gates)

1. **Deterministic validators** — schema completeness and minimum length; PII
   (SSN, email, Luhn-valid card numbers as errors; phone/EIN as warnings);
   placeholder and AI-refusal markers; standards-citation fabrication checks
   (ASC topic set, IFRS 1–19, IAS series); financial tie-outs (balance sheet
   equation, debit/credit equality) on structured `financials` blocks;
   near-duplicate fingerprinting within the project. Errors block; warnings
   travel with the record to downstream reviewers.
2. **Gold check** — see C4.
3. **Autonomous QC review** — the Claude reviewer scores each rubric criterion
   0–5 with justification and raises flags. Overall score is the
   weight-averaged criterion score. Routing: score < 3.0 or any critical flag
   → needs revision; 3.0–4.0, offline fallback, or sampled → human review;
   ≥ 4.0 clean and unsampled → auto-approval (recorded as such).
4. **Human review** — verdicts pass / revise / fail against the same rubric;
   updates the contributor score.
5. **Release** — see C6.

## 4. Rubrics

All reviewers (LLM and human) score the same criteria (0–5, weighted):

- `technical_accuracy` (w3, critical) — figures recomputed, entries balance
- `standards_grounding` (w2, critical) — citations exist and say what is claimed
- `completeness` (w1.5)
- `reasoning_quality` (w1.5)
- `clarity_style` (w1)
- preference sets add `pair_contrast` (w2, critical); eval sets add
  `gradeability` (w2, critical)

A score below 3 on any critical criterion forces revision regardless of the
weighted overall.

## 5. Degraded-mode behavior

If the Anthropic API is unavailable, the pipeline does not stall and does not
loosen: the heuristic fallback reviewer produces a conservative estimate,
flags itself `OFFLINE_FALLBACK`, and the engine routes the submission to human
review. Automation failures therefore increase human oversight rather than
bypassing it.

## 6. Billing controls (v0.2)

- **B1 — Metering integrity.** A billable usage event is emitted only by the
  QC engine on approval of a non-gold task, priced from the firm's rate card
  (default rates otherwise), and is unique per task — revisions can never
  double-bill. Gold/calibration tasks are never billable.
- **B2 — Invoice completeness.** Invoice generation sweeps all uninvoiced
  usage in the period and permanently attaches each event to the invoice;
  the same usage cannot appear on two invoices.
- **B3 — Settlement traceability.** External-mode invoices track settlement
  in-app (`issued_external` → `paid` with the AP reference); Stripe-mode
  invoices settle only via signature-verified webhook events. All transitions
  are audit-chained.
- **B4 — Client transparency.** Firm users can read their own usage ledger,
  billing summary, and invoices at any time — and nothing belonging to any
  other firm.
- **B5 — Self-serve settlement integrity.** Checkout sessions can only be
  opened for unpaid invoices by principals with access to that firm; payment
  state changes only on webhook evidence (signature-verified in production),
  idempotent under replay, with the payment-intent reference recorded.
- **B6 — Reconciliation.** Metered vs invoiced vs paid tie-outs per project,
  per firm, and factory-wide; any leakage surfaces as `clean: false`.

## 7. Known limitations / roadmap

- Gold matching is keyword-coverage; a rubric-based LLM gold grader would
  raise sensitivity.
- Inter-rater reliability (human vs auto-QC agreement, reviewer-pair kappa) is
  reconstructible from review rows but not yet surfaced as a metric endpoint.
- Single-process synchronous QC; move gate 3 to a queue/worker (or the
  Message Batches API) for volume.
