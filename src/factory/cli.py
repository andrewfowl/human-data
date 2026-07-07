"""Operator CLI.

    hdf init-db                        create tables
    hdf seed-demo                      seed a demo project and run submissions end-to-end
    hdf control-report <project_id>    print the pre-release control report
    hdf export <project_id> --requested-by A --approved-by B
    hdf verify-audit                   verify the hash-chained audit trail
    hdf serve                          run the HTTP API
"""

from __future__ import annotations

import json

import typer

from . import audit as audit_mod, auth as auth_mod, db as database, export as export_mod
from .billing import service as billing_mod
from .models import (
    BillingMode, DomainTrack, Expert, ExpertStatus, Firm, Project, Qualification,
    Role, Task, TaskType, User,
)
from .qc import engine
from .rubrics import default_rubric_for

app = typer.Typer(help="Human Data Factory — finance & accounting datasets with embedded controls.")


@app.command()
def init_db():
    """Create database tables."""
    database.init_db()
    typer.echo("database initialized")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000):
    """Run the HTTP API."""
    import uvicorn

    uvicorn.run("factory.api.main:app", host=host, port=port)


@app.command()
def verify_audit():
    """Verify the hash-chained audit trail."""
    db = database.session()
    try:
        ok, n = audit_mod.verify_chain(db)
        typer.echo(f"audit chain intact={ok} events={n}")
        raise typer.Exit(0 if ok else 1)
    finally:
        db.close()


@app.command()
def control_report(project_id: str):
    """Print the pre-release control report for a project."""
    db = database.session()
    try:
        project = db.get(Project, project_id)
        if project is None:
            typer.echo("project not found", err=True)
            raise typer.Exit(1)
        typer.echo(json.dumps(export_mod.control_report(db, project), indent=2))
    finally:
        db.close()


@app.command()
def export(project_id: str, requested_by: str = typer.Option(...),
           approved_by: str = typer.Option(...), out_dir: str = typer.Option(None)):
    """Request + dual-approve + materialize a dataset export."""
    db = database.session()
    try:
        project = db.get(Project, project_id)
        if project is None:
            typer.echo("project not found", err=True)
            raise typer.Exit(1)
        batch = export_mod.request_export(db, project=project, requested_by=requested_by)
        batch = export_mod.approve_and_materialize(db, batch=batch, approved_by=approved_by,
                                                   out_dir=out_dir)
        typer.echo(json.dumps({"batch_id": batch.id, "path": batch.path,
                               "records": batch.manifest["record_count"]}, indent=2))
    finally:
        db.close()


@app.command()
def bootstrap_admin(name: str = typer.Option(...), email: str = typer.Option(...)):
    """Create the first admin user and print their API key (once)."""
    database.init_db()
    db = database.session()
    try:
        from sqlalchemy import func, select
        if db.execute(select(func.count()).select_from(User)).scalar():
            typer.echo("users already exist; use the API to add more", err=True)
            raise typer.Exit(1)
        user = User(name=name, email=email, role=Role.ADMIN.value)
        db.add(user)
        db.flush()
        key = auth_mod.issue_key(db, user)
        audit_mod.record(db, actor=user.id, action="user.bootstrapped",
                         entity_type="user", entity_id=user.id, payload={"email": email})
        db.commit()
        typer.echo(json.dumps({"user_id": user.id, "api_key": key,
                               "note": "store this key now; it is not retrievable later"},
                              indent=2))
    finally:
        db.close()


@app.command()
def invoice(firm_id: str, period_start: str = typer.Option(..., help="ISO date, inclusive"),
            period_end: str = typer.Option(..., help="ISO date, exclusive"),
            issued_by: str = typer.Option("ops-admin")):
    """Generate an invoice for a firm's uninvoiced usage in a period."""
    from datetime import datetime, timezone

    db = database.session()
    try:
        firm = db.get(Firm, firm_id)
        if firm is None:
            typer.echo("firm not found", err=True)
            raise typer.Exit(1)
        start = datetime.fromisoformat(period_start).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(period_end).replace(tzinfo=timezone.utc)
        inv = billing_mod.generate_invoice(db, firm=firm, period_start=start,
                                           period_end=end, issued_by=issued_by)
        typer.echo(json.dumps({"invoice": inv.number, "status": inv.status,
                               "subtotal_cents": inv.subtotal_cents,
                               "line_items": inv.line_items,
                               "hosted_invoice_url": inv.hosted_invoice_url},
                              indent=2, default=str))
    finally:
        db.close()


@app.command()
def seed_demo():
    """Seed a demo SFT project and drive three submissions through the pipeline."""
    database.init_db()
    db = database.session()
    try:
        ops = "ops-admin"

        alice = Expert(name="Alice Turner", email="alice@example-experts.com",
                       credentials=["CPA"], status=ExpertStatus.QUALIFIED.value)
        bob = Expert(name="Bob Reyes", email="bob@example-experts.com",
                     credentials=["CPA", "CIA"], status=ExpertStatus.QUALIFIED.value,
                     is_reviewer=True)
        db.add_all([alice, bob])
        db.flush()
        db.add_all([
            Qualification(expert_id=alice.id, track=DomainTrack.FINANCIAL_ACCOUNTING.value,
                          exam_score=92, passed=True),
            Qualification(expert_id=bob.id, track=DomainTrack.FINANCIAL_ACCOUNTING.value,
                          exam_score=95, passed=True),
        ])
        audit_mod.record(db, actor=ops, action="expert.created", entity_type="expert",
                         entity_id=alice.id, payload={"seed": True})
        audit_mod.record(db, actor=ops, action="expert.created", entity_type="expert",
                         entity_id=bob.id, payload={"seed": True})

        firm = Firm(name="Demo Lab", billing_email="ap@demo-lab.example.com",
                    billing_mode=BillingMode.EXTERNAL.value,
                    external_reference="PO-2026-0042")
        db.add(firm)
        db.flush()
        audit_mod.record(db, actor=ops, action="firm.created", entity_type="firm",
                         entity_id=firm.id, payload={"seed": True})

        project = Project(
            name="ASC 606 revenue recognition SFT set",
            firm_id=firm.id,
            track=DomainTrack.FINANCIAL_ACCOUNTING.value,
            task_type=TaskType.SFT.value,
            guidelines="Write responses a senior technical accountant would sign off on.",
            rubric_id=default_rubric_for("sft"),
            created_by=ops,
        )
        db.add(project)
        db.flush()
        audit_mod.record(db, actor=ops, action="project.created", entity_type="project",
                         entity_id=project.id, payload={"seed": True})

        t1 = Task(project_id=project.id,
                  prompt="A SaaS company sells a 3-year license bundled with implementation "
                         "services and annual support. Walk through the five-step ASC 606 "
                         "model to determine how revenue should be recognized.")
        t2 = Task(project_id=project.id,
                  prompt="Prepare the journal entries to record a $120,000 annual SaaS "
                         "subscription invoiced upfront on Jan 1, recognized monthly. Show "
                         "the entry at invoicing and the monthly recognition entry, and "
                         "provide the figures in a structured financials block.")
        t3 = Task(project_id=project.id,
                  prompt="Under ASC 842, does a 10-month equipment rental with no purchase "
                         "option require balance-sheet recognition? Explain the short-term "
                         "lease exemption.",
                  is_gold=True,
                  gold_answer={"must_include": ["short-term", "12 months", "exemption"],
                               "must_not_include": ["finance lease required"]})
        db.add_all([t1, t2, t3])
        db.commit()

        good_response = (
            "Under ASC 606, the arrangement is analyzed with the five-step model. "
            "Step 1 — identify the contract: the signed order form creates enforceable "
            "rights and obligations. Step 2 — identify performance obligations: the "
            "3-year license, implementation services, and annual support are assessed "
            "for distinctness; implementation that significantly customizes the software "
            "is combined with the license, while standard setup is distinct. Step 3 — "
            "determine the transaction price, including any variable consideration "
            "subject to the constraint. Step 4 — allocate the price to each performance "
            "obligation on a relative standalone selling price basis. Step 5 — recognize "
            "revenue as each obligation is satisfied: the license/SaaS obligation over "
            "the 3-year term (a stand-ready obligation satisfied over time), distinct "
            "implementation at the point service is completed or over the service period, "
            "and support ratably over each annual period. Judgment areas — SSP estimation "
            "and the distinctness of implementation — should be documented in the revenue "
            "memo."
        )
        s1 = engine.submit(db, task=t1, expert=alice, content={
            "response": good_response,
            "citations": ["ASC 606"],
            "assumptions": ["Implementation does not significantly modify the software."],
        })

        s2 = engine.submit(db, task=t2, expert=alice, content={
            "response": (
                "At invoicing on Jan 1, the company records the receivable and a contract "
                "liability because payment precedes performance: debit Accounts Receivable "
                "$120,000, credit Deferred Revenue $120,000. Each month, as the entity "
                "satisfies its stand-ready performance obligation ratably under ASC 606, "
                "it recognizes one-twelfth: debit Deferred Revenue $10,000, credit "
                "Subscription Revenue $10,000. By Dec 31 the contract liability is fully "
                "amortized and $120,000 of revenue has been recognized."
            ),
            "citations": ["ASC 606"],
            "financials": {"journal_entries": [
                {"account": "Accounts Receivable", "debit": 120000, "credit": 0},
                {"account": "Deferred Revenue", "debit": 0, "credit": 120000},
                {"account": "Deferred Revenue (monthly)", "debit": 10000, "credit": 0},
                {"account": "Subscription Revenue (monthly)", "debit": 0, "credit": 10000},
            ]},
        })

        s3 = engine.submit(db, task=t3, expert=alice, content={
            "response": (
                "No balance-sheet recognition is required if the lessee elects the "
                "short-term lease exemption. Under ASC 842, a lease with a term of "
                "12 months or less at commencement, and no purchase option the lessee is "
                "reasonably certain to exercise, qualifies for the short-term exemption; "
                "the 10-month rental qualifies. The lessee recognizes lease expense on a "
                "straight-line basis instead of a right-of-use asset and lease liability, "
                "and discloses short-term lease cost."
            ),
            "citations": ["ASC 842"],
        })

        typer.echo(json.dumps({
            "firm_id": firm.id,
            "project_id": project.id,
            "experts": {"author": alice.id, "reviewer": bob.id},
            "submissions": [
                {"id": s.id, "task": s.task_id, "status": s.status} for s in (s1, s2, s3)
            ],
            "next": [
                f"hdf control-report {project.id}",
                "POST /submissions/<id>/human-review for any items in human_review",
                f"hdf export {project.id} --requested-by ops-admin --approved-by {bob.id}",
                f"hdf invoice {firm.id} --period-start 2026-01-01 --period-end 2027-01-01",
            ],
        }, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    app()
