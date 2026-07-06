import pytest
from fastapi.testclient import TestClient

from factory.api.main import app
from tests.conftest import GOOD_SFT

HDR = {"X-Actor-Id": "ops-admin"}


@pytest.fixture()
def client(db):
    # `db` fixture has already pointed the engine at a fresh temp database.
    with TestClient(app) as c:
        yield c


def _onboard_expert(client, email, is_reviewer=False):
    r = client.post("/experts", json={"name": "E", "email": email,
                                      "credentials": ["CPA"], "is_reviewer": is_reviewer},
                    headers=HDR)
    assert r.status_code == 201, r.text
    expert_id = r.json()["id"]
    r = client.post(f"/experts/{expert_id}/exams",
                    json={"track": "financial_accounting", "exam_score": 92}, headers=HDR)
    assert r.json()["passed"] is True
    return expert_id


def test_full_workflow_over_http(client, tmp_path):
    author = _onboard_expert(client, "author@example.com")
    reviewer = _onboard_expert(client, "reviewer@example.com", is_reviewer=True)

    r = client.post("/projects", json={"name": "P", "client": "lab",
                                       "track": "financial_accounting", "task_type": "sft"},
                    headers=HDR)
    project_id = r.json()["id"]

    r = client.post(f"/projects/{project_id}/tasks",
                    json={"prompt": "Explain ASC 606 allocation."}, headers=HDR)
    task_id = r.json()["id"]

    r = client.post(f"/tasks/{task_id}/assign/{author}", headers=HDR)
    assert r.status_code == 200

    r = client.post(f"/tasks/{task_id}/submissions",
                    json={"expert_id": author, "content": GOOD_SFT})
    assert r.status_code == 201, r.text
    sub = r.json()
    assert sub["status"] == "human_review"  # offline fallback always routes to human

    # queue is blind — no author id exposed
    queue = client.get("/review-queue").json()
    assert queue and "expert_id" not in queue[0]

    # self-review must be blocked (C2)
    r = client.post(f"/submissions/{sub['id']}/human-review",
                    json={"reviewer_id": author, "verdict": "pass"})
    assert r.status_code == 403

    r = client.post(f"/submissions/{sub['id']}/human-review",
                    json={"reviewer_id": reviewer, "verdict": "pass", "comments": "good"})
    assert r.status_code == 201
    assert r.json()["submission_status"] == "approved"

    report = client.get(f"/projects/{project_id}/control-report").json()
    assert report["clean"] is True

    r = client.post(f"/projects/{project_id}/exports", headers=HDR)
    batch_id = r.json()["batch_id"]

    # same-person approval blocked (C6)
    r = client.post(f"/exports/{batch_id}/approve", json={"approved_by": "ops-admin"})
    assert r.status_code == 409

    r = client.post(f"/exports/{batch_id}/approve", json={"approved_by": reviewer})
    assert r.status_code == 200
    assert r.json()["status"] == "released"
    assert r.json()["manifest"]["record_count"] == 1

    assert client.get("/audit/verify").json()["intact"] is True
    metrics = client.get("/metrics").json()
    assert metrics["submissions_by_status"]["approved"] == 1


def test_unqualified_assignment_rejected(client):
    r = client.post("/experts", json={"name": "N", "email": "new@example.com",
                                      "credentials": []}, headers=HDR)
    novice = r.json()["id"]
    r = client.post("/projects", json={"name": "P", "client": "lab",
                                       "track": "financial_accounting", "task_type": "sft"},
                    headers=HDR)
    project_id = r.json()["id"]
    r = client.post(f"/projects/{project_id}/tasks", json={"prompt": "x"}, headers=HDR)
    task_id = r.json()["id"]
    r = client.post(f"/tasks/{task_id}/assign/{novice}", headers=HDR)
    assert r.status_code == 403
    assert "C1" in r.json()["detail"]


def test_gold_answer_hidden_from_task_endpoint(client):
    r = client.post("/projects", json={"name": "P", "client": "lab",
                                       "track": "financial_accounting", "task_type": "sft"},
                    headers=HDR)
    project_id = r.json()["id"]
    r = client.post(f"/projects/{project_id}/tasks",
                    json={"prompt": "gold q", "is_gold": True,
                          "gold_answer": {"must_include": ["secret"]}}, headers=HDR)
    task_id = r.json()["id"]
    body = client.get(f"/tasks/{task_id}").json()
    assert "gold_answer" not in body
