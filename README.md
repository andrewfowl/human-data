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

## Multi-tenancy, auth, and billing (v0.2)

**Firms.** Every project belongs to a `Firm` — the billable client
organization. Firm-side users get read-only, firm-scoped access to their
projects, control reports, usage, and invoices.

**Auth.** Bearer API keys (`Authorization: Bearer hdf_...`); only the SHA-256
of a key is stored, and keys are shown once at issuance. Roles: `admin`
(everything), `ops` (production + billing operations), `reviewer` (review
queue, may only review as themselves), `expert` (may only submit as
themselves), `client` (firm-scoped read access). Cold start: `POST /bootstrap`
creates the first admin + key while the user table is empty. Set
`HDF_AUTH_DISABLED=1` for local dev / tests (header-based identity).

**Billing — two modes per firm:**

- `external` — the firm is invoiced *outside* the system (their AP process,
  wire, netting). The factory still meters every billable unit, generates
  numbered invoice records with line items, and tracks settlement in-app
  (`issued_external` → `paid` via `POST /invoices/{id}/external-payment` with
  the AP reference), so finance has full visibility even though money moves
  elsewhere.
- `stripe` — embedded Stripe billing: the firm becomes a Stripe customer,
  invoice generation pushes line items and a `send_invoice` Stripe invoice
  (hosted invoice URL returned), and `POST /billing/stripe/webhook`
  (signature-verified) flips invoices to `paid`/`void` on Stripe events.
  Without `STRIPE_SECRET_KEY` a deterministic fake gateway runs, so the whole
  flow works offline.

**Metering.** One usage event per approved non-gold task — a task revised and
re-approved is never double-billed, and gold/calibration items are always
free. Prices come from the firm's rate card (`POST /firms/{id}/rate-cards`),
falling back to configured defaults (`HDF_RATE_SFT_CENTS` etc.). Every usage
event and invoice transition is written to the hash-chained audit trail.

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
- `POST /bootstrap`, `POST /users`, `POST /users/{id}/revoke` — identity
- `POST /firms`, `POST /firms/{id}/rate-cards`, `GET /firms/{id}/billing`,
  `GET /firms/{id}/usage`, `POST /firms/{id}/invoices`,
  `POST /invoices/{id}/external-payment`, `POST /billing/stripe/webhook` — billing

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

## Deploying on Vercel

The repo deploys as-is (`vercel deploy` or the Vercel MCP/CLI): `api/index.py`
exposes the FastAPI app, `vercel.json` rewrites all routes to it. After the
first deploy:

1. **Database** — set `DATABASE_URL` (or `POSTGRES_URL` / `HDF_DATABASE_URL`)
   to a hosted Postgres (Neon / Supabase / Vercel Postgres) in the project's
   environment variables. Without it the app runs in **ephemeral demo mode**
   (SQLite in `/tmp`, reset on each cold start).
2. **Bootstrap** — `POST /bootstrap` with `{name, email}` to get the first
   admin API key.
3. **QC reviews** — set `ANTHROPIC_API_KEY` to enable the autonomous reviewer
   (otherwise the conservative offline fallback routes everything to human
   review).
4. **Stripe (optional)** — set `STRIPE_SECRET_KEY` and
   `STRIPE_WEBHOOK_SECRET`, and point a Stripe webhook (events
   `invoice.paid`, `invoice.voided`) at `/billing/stripe/webhook`.

5. **Durable exports (recommended)** — set `HDF_EXPORT_S3_BUCKET` (plus
   `HDF_EXPORT_S3_ENDPOINT` for Cloudflare R2/MinIO, `HDF_EXPORT_S3_PREFIX`,
   and standard AWS credentials). Released batches — data files first, then
   the manifest — are uploaded to object storage and the manifest records the
   bucket/keys. Without a bucket, exports stay on the local filesystem
   (`/tmp` on Vercel — ephemeral).

**Migrations** — the schema ships as an Alembic baseline
(`migrations/versions/`). On a fresh production database run
`alembic upgrade head` (it resolves the same `DATABASE_URL` the app uses);
future schema changes are added as revisions. `init_db`/`create_all` remains
for local dev and the ephemeral demo mode.

**Self-serve payment (Stripe Checkout)** — `POST /invoices/{id}/checkout`
(callable by the firm's own client users) returns a Stripe Checkout URL for
any unpaid invoice — including *external*-mode invoices, giving firms a
pay-by-card alternative to their AP process. Completion arrives via the
`checkout.session.completed` webhook, which marks the invoice paid
idempotently and records the payment-intent reference.

**Rate limiting** — fixed-window per credential (or per IP when anonymous),
`HDF_RATE_LIMIT_PER_MINUTE` (default 120, `0` disables); 429 with
`Retry-After` on breach, `/healthz` exempt. State is per instance — front it
with an edge/WAF limiter for hard global guarantees.

**Reconciliation** — `GET /firms/{id}/reconciliation` (firm-scoped) and
`GET /billing/reconciliation` (ops-wide) tie out metered usage vs invoiced vs
paid per project and per firm, flagging any leakage between production,
invoicing, and cash (`clean: false`).

See [docs/QUALITY_MANUAL.md](docs/QUALITY_MANUAL.md) for the control
objectives, rubric definitions, and operating procedures.
