"""Dataset export pipeline with dual-control release (C6).

Flow: request → control report → second-person approval → materialize JSONL
with a manifest (record counts, per-file SHA-256, audit-chain head) so every
released artifact is traceable back to the audit trail state at release time.

Formats:
  sft        {"prompt", "completion", "citations", "metadata"}
  chat       {"messages": [{role, content}...], "metadata"}       (from SFT rows)
  preference {"prompt", "chosen", "rejected", "metadata"}
  eval       {"question", "reference_answer", "grading_notes", "metadata"}
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import audit, controls
from .config import settings
from .models import (
    ExportBatch, Firm, Project, Review, ReviewKind, Submission, SubmissionStatus, Task,
)


class ExportError(Exception):
    pass


def _approved_rows(db: Session, project: Project) -> list[tuple[Task, Submission]]:
    rows = db.execute(
        select(Task, Submission)
        .join(Submission, Submission.task_id == Task.id)
        .where(
            Task.project_id == project.id,
            Task.is_gold.is_(False),                       # calibration items never ship
            Submission.status == SubmissionStatus.APPROVED.value,
        )
        .order_by(Task.created_at)
    ).all()
    # One row per task: keep the latest approved version.
    latest: dict[str, tuple[Task, Submission]] = {}
    for task, sub in rows:
        if task.id not in latest or sub.version > latest[task.id][1].version:
            latest[task.id] = (task, sub)
    return list(latest.values())


def control_report(db: Session, project: Project) -> dict:
    """Pre-release control report — must be clean before approval."""
    tasks = db.execute(
        select(Task).where(Task.project_id == project.id, Task.is_gold.is_(False))
    ).scalars().all()
    approved = _approved_rows(db, project)
    approved_task_ids = {t.id for t, _ in approved}
    pending = [t.id for t in tasks if t.id not in approved_task_ids]

    # Every shipped record must carry an auto-LLM or human review with PASS verdict.
    unreviewed = []
    for _, sub in approved:
        kinds = {r.kind for r in db.execute(
            select(Review).where(Review.submission_id == sub.id)
        ).scalars().all()}
        if not ({ReviewKind.AUTO_LLM.value, ReviewKind.HUMAN.value} & kinds):
            unreviewed.append(sub.id)

    chain_ok, chain_len = audit.verify_chain(db)
    report = {
        "project_id": project.id,
        "total_tasks": len(tasks),
        "approved_records": len(approved),
        "tasks_without_approved_submission": pending,
        "approved_without_review_evidence": unreviewed,
        "audit_chain_intact": chain_ok,
        "audit_chain_length": chain_len,
    }
    report["clean"] = chain_ok and not unreviewed and len(approved) > 0
    return report


def request_export(db: Session, *, project: Project, requested_by: str) -> ExportBatch:
    report = control_report(db, project)
    if not report["clean"]:
        raise ExportError(f"control report is not clean: {report}")
    batch = ExportBatch(project_id=project.id, requested_by=requested_by,
                        manifest={"control_report": report})
    db.add(batch)
    db.flush()
    audit.record(db, actor=requested_by, action="export.requested",
                 entity_type="export_batch", entity_id=batch.id,
                 payload={"project_id": project.id, "records": report["approved_records"]})
    db.commit()
    return batch


def approve_and_materialize(db: Session, *, batch: ExportBatch, approved_by: str,
                            out_dir: str | None = None) -> ExportBatch:
    if batch.status != "pending_approval":
        raise ExportError(f"batch is in status '{batch.status}'")
    controls.assert_export_dual_control(batch.requested_by, approved_by)

    project: Project = db.get(Project, batch.project_id)
    rows = _approved_rows(db, project)
    records = [_format_record(project.task_type, task, sub) for task, sub in rows]

    base = Path(out_dir or settings.exports_dir) / batch.id
    base.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    main_path = base / f"{project.task_type}.jsonl"
    _write_jsonl(main_path, records)
    files[main_path.name] = _sha256(main_path)

    if project.task_type == "sft":
        chat_path = base / "chat.jsonl"
        _write_jsonl(chat_path, [_to_chat(r) for r in records])
        files[chat_path.name] = _sha256(chat_path)

    firm = db.get(Firm, project.firm_id)
    manifest = {
        **batch.manifest,
        "project": {"id": project.id, "name": project.name,
                    "firm": {"id": firm.id, "name": firm.name},
                    "track": project.track, "task_type": project.task_type},
        "record_count": len(records),
        "files": files,
        "audit_chain_head": audit.chain_head(db),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "requested_by": batch.requested_by,
        "approved_by": approved_by,
    }
    (base / "manifest.json").write_text(json.dumps(manifest, indent=2))

    batch.approved_by = approved_by
    batch.path = str(base)
    batch.manifest = manifest
    batch.status = "released"
    audit.record(db, actor=approved_by, action="export.released",
                 entity_type="export_batch", entity_id=batch.id,
                 payload={"record_count": len(records), "files": files})
    db.commit()
    return batch


def _format_record(task_type: str, task: Task, sub: Submission) -> dict:
    meta = {"task_id": task.id, "submission_id": sub.id, "version": sub.version,
            "sampled_human_review": sub.sampled_for_human_review}
    c = sub.content
    if task_type == "sft":
        return {"prompt": task.prompt, "completion": c["response"],
                "citations": c.get("citations", []), "metadata": meta}
    if task_type == "preference":
        return {"prompt": task.prompt, "chosen": c["chosen"], "rejected": c["rejected"],
                "rejection_rationale": c.get("rejection_rationale", ""),
                "citations": c.get("citations", []), "metadata": meta}
    if task_type == "eval":
        return {"question": task.prompt, "reference_answer": c["answer"],
                "grading_notes": c.get("grading_notes", ""),
                "citations": c.get("citations", []), "metadata": meta}
    raise ExportError(f"unknown task type: {task_type}")


def _to_chat(record: dict) -> dict:
    return {
        "messages": [
            {"role": "user", "content": record["prompt"]},
            {"role": "assistant", "content": record["completion"]},
        ],
        "metadata": record["metadata"],
    }


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
