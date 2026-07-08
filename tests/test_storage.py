"""Export upload to durable object storage (via a fake storage backend)."""

from pathlib import Path

from factory import export
from factory.qc import engine
from tests.conftest import GOOD_SFT


class FakeStorage:
    def __init__(self):
        self.uploads: list[tuple[str, str]] = []  # (batch_id, filename)

    def upload_batch(self, local_dir: Path, batch_id: str, only=None) -> dict:
        keys = {}
        for path in sorted(local_dir.iterdir()):
            if not path.is_file() or (only is not None and path.name not in only):
                continue
            self.uploads.append((batch_id, path.name))
            keys[path.name] = f"exports/{batch_id}/{path.name}"
        return {"backend": "s3", "bucket": "fake-bucket", "endpoint": None, "keys": keys}


def test_export_uploads_to_object_storage(db, project, task, qualified_expert,
                                          reviewer, tmp_path):
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    engine.human_review(db, submission=sub, reviewer=reviewer, verdict="pass")

    storage = FakeStorage()
    batch = export.request_export(db, project=project, requested_by="ops")
    batch = export.approve_and_materialize(db, batch=batch, approved_by=reviewer.id,
                                           out_dir=str(tmp_path), storage=storage)

    uploaded_names = {name for _, name in storage.uploads}
    assert uploaded_names == {"sft.jsonl", "chat.jsonl", "manifest.json"}
    # data files were uploaded before the manifest
    order = [name for _, name in storage.uploads]
    assert order.index("manifest.json") > order.index("sft.jsonl")

    block = batch.manifest["storage"]
    assert block["backend"] == "s3"
    assert block["keys"]["sft.jsonl"] == f"exports/{batch.id}/sft.jsonl"
    assert "manifest.json" in block["keys"]


def test_export_without_storage_stays_local(db, project, task, qualified_expert,
                                            reviewer, tmp_path):
    sub = engine.submit(db, task=task, expert=qualified_expert, content=GOOD_SFT)
    engine.human_review(db, submission=sub, reviewer=reviewer, verdict="pass")
    batch = export.request_export(db, project=project, requested_by="ops")
    batch = export.approve_and_materialize(db, batch=batch, approved_by=reviewer.id,
                                           out_dir=str(tmp_path), storage=None)
    assert "storage" not in batch.manifest
    assert (Path(batch.path) / "sft.jsonl").exists()
