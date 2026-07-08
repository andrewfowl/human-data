import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Tests always run the offline heuristic reviewer — deterministic, no network —
# and header-based dev auth (auth-enabled behavior is tested explicitly).
os.environ["HDF_QC_OFFLINE"] = "1"
os.environ["HDF_AUTH_DISABLED"] = "1"
os.environ["HDF_RATE_LIMIT_PER_MINUTE"] = "0"  # rate limiting tested explicitly

from factory import db as database  # noqa: E402
from factory.models import (  # noqa: E402
    BillingMode, DomainTrack, Expert, ExpertStatus, Firm, Project, Qualification,
    Task, TaskType,
)
from factory.rubrics import default_rubric_for  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    database.reset_for_tests(f"sqlite:///{tmp_path}/test.db")
    s = database.session()
    yield s
    s.close()


@pytest.fixture()
def qualified_expert(db):
    ex = Expert(name="Alice", email="alice@x.test", credentials=["CPA"],
                status=ExpertStatus.QUALIFIED.value)
    db.add(ex)
    db.flush()
    db.add(Qualification(expert_id=ex.id, track=DomainTrack.FINANCIAL_ACCOUNTING.value,
                         exam_score=92, passed=True))
    db.commit()
    return ex


@pytest.fixture()
def reviewer(db):
    ex = Expert(name="Bob", email="bob@x.test", credentials=["CPA"],
                status=ExpertStatus.QUALIFIED.value, is_reviewer=True)
    db.add(ex)
    db.flush()
    db.add(Qualification(expert_id=ex.id, track=DomainTrack.FINANCIAL_ACCOUNTING.value,
                         exam_score=95, passed=True))
    db.commit()
    return ex


@pytest.fixture()
def firm(db):
    f = Firm(name="Test Lab", billing_email="ap@lab.example.com",
             billing_mode=BillingMode.EXTERNAL.value)
    db.add(f)
    db.commit()
    return f


@pytest.fixture()
def project(db, firm):
    p = Project(name="Test project", firm_id=firm.id,
                track=DomainTrack.FINANCIAL_ACCOUNTING.value,
                task_type=TaskType.SFT.value, rubric_id=default_rubric_for("sft"),
                created_by="ops")
    db.add(p)
    db.commit()
    return p


@pytest.fixture()
def task(db, project):
    t = Task(project_id=project.id, prompt="Explain ASC 606 step 4 allocation.")
    db.add(t)
    db.commit()
    return t


GOOD_SFT = {
    "response": (
        "Under ASC 606 step 4, the transaction price is allocated to each distinct "
        "performance obligation on a relative standalone selling price (SSP) basis. "
        "When SSP is not directly observable, the entity estimates it using the "
        "adjusted market assessment, expected cost plus margin, or (in limited "
        "circumstances) the residual approach. Discounts are generally allocated "
        "proportionately unless criteria for allocating entirely to one or more "
        "specific obligations are met."
    ),
    "citations": ["ASC 606"],
}
