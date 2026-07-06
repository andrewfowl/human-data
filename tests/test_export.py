import hashlib
import json
from pathlib import Path

import pytest

from factory import export
from factory.models import Task
from factory.qc import engine
from tests.conftest import GOOD_SFT


def _approve_one(db, task, author, reviewer):
    sub = engine.submit(db, task=task, expert=author, content=GOOD_SFT)
    engine.human_review(db, submission=sub, reviewer=reviewer, verdict="pass")
    return sub


def test_control_report_flags_pending_tasks(db, project, task, qualified_expert):
    report = export.control_report(db, project)
    assert not report["clean"]
    assert task.id in report["tasks_without_approved_submission"]


def test_export_requires_clean_report(db, project, task):
    with pytest.raises(export.ExportError):
        export.request_export(db, project=project, requested_by="ops")


def test_dual_control_enforced(db, project, task, qualified_expert, reviewer, tmp_path):
    _approve_one(db, task, qualified_expert, reviewer)
    batch = export.request_export(db, project=project, requested_by="ops")
    from factory.controls import ControlViolation
    with pytest.raises(ControlViolation):
        export.approve_and_materialize(db, batch=batch, approved_by="ops",
                                       out_dir=str(tmp_path))


def test_export_materializes_with_manifest(db, project, task, qualified_expert, reviewer, tmp_path):
    _approve_one(db, task, qualified_expert, reviewer)
    batch = export.request_export(db, project=project, requested_by="ops")
    batch = export.approve_and_materialize(db, batch=batch, approved_by=reviewer.id,
                                           out_dir=str(tmp_path))
    base = Path(batch.path)
    assert (base / "sft.jsonl").exists()
    assert (base / "chat.jsonl").exists()
    manifest = json.loads((base / "manifest.json").read_text())
    assert manifest["record_count"] == 1
    assert manifest["approved_by"] == reviewer.id
    # checksum in manifest matches the file on disk
    digest = hashlib.sha256((base / "sft.jsonl").read_bytes()).hexdigest()
    assert manifest["files"]["sft.jsonl"] == digest

    row = json.loads((base / "sft.jsonl").read_text().splitlines()[0])
    assert row["completion"] == GOOD_SFT["response"]
    chat = json.loads((base / "chat.jsonl").read_text().splitlines()[0])
    assert chat["messages"][1]["role"] == "assistant"


def test_gold_tasks_never_ship(db, project, task, qualified_expert, reviewer, tmp_path):
    _approve_one(db, task, qualified_expert, reviewer)
    gold = Task(project_id=project.id, prompt="gold", is_gold=True,
                gold_answer={"must_include": ["allocation"]})
    db.add(gold)
    db.commit()
    gold_content = {
        "response": "The allocation of the transaction price follows the relative "
                    "standalone selling price method under the standard. " + "More. " * 30,
        "citations": ["ASC 606"],
    }
    engine.submit(db, task=gold, expert=qualified_expert, content=gold_content)

    batch = export.request_export(db, project=project, requested_by="ops")
    batch = export.approve_and_materialize(db, batch=batch, approved_by=reviewer.id,
                                           out_dir=str(tmp_path))
    assert batch.manifest["record_count"] == 1  # only the non-gold record
