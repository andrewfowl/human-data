import pytest
from fastapi.testclient import TestClient

from factory.api.main import app
from factory.config import settings


@pytest.fixture()
def limited_client(db):
    settings.rate_limit_per_minute = 5
    with TestClient(app) as c:
        yield c
    settings.rate_limit_per_minute = 0


def test_rate_limit_returns_429_with_retry_after(limited_client):
    hdr = {"X-Actor-Id": "hammer"}
    codes = [limited_client.get("/metrics", headers=hdr).status_code for _ in range(8)]
    assert codes[:5] == [200] * 5
    assert 429 in codes[5:]
    r = limited_client.get("/metrics", headers=hdr)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert r.json()["detail"] == "rate limit exceeded"


def test_rate_limit_is_per_identity(limited_client):
    for _ in range(6):
        limited_client.get("/metrics", headers={"X-Actor-Id": "noisy"})
    # a different credential is unaffected
    assert limited_client.get("/metrics", headers={"X-Actor-Id": "quiet"}).status_code == 200


def test_healthz_exempt(limited_client):
    hdr = {"X-Actor-Id": "prober"}
    for _ in range(20):
        assert limited_client.get("/healthz", headers=hdr).status_code == 200


def test_disabled_by_default_in_suite(db):
    settings.rate_limit_per_minute = 0
    with TestClient(app) as c:
        for _ in range(10):
            assert c.get("/metrics", headers={"X-Actor-Id": "x"}).status_code == 200
