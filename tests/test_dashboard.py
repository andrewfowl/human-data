import pytest
from fastapi.testclient import TestClient

from factory.api.main import app


@pytest.fixture()
def client(db):
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("path", ["/app", "/app/"])
def test_dashboard_shell_served_without_redirect(client, path):
    # Proxies that strip trailing slashes must not loop against a slash-adding redirect.
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert 'src="/app/app.js"' in r.text
    assert 'href="/app/app.css"' in r.text


@pytest.mark.parametrize("asset,ctype", [("/app/app.js", "javascript"), ("/app/app.css", "text/css")])
def test_dashboard_assets_served(client, asset, ctype):
    r = client.get(asset, follow_redirects=False)
    assert r.status_code == 200
    assert ctype in r.headers["content-type"]


def test_landing_links_to_slashless_dashboard(client):
    assert 'href="/app"' in client.get("/").text
