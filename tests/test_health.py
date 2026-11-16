"""Health endpoint (architecture §4.2)."""


def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["version"]
    assert isinstance(body["queue_depth"], int)
    assert body["degraded_providers"] == []


def test_root_redirects_to_find(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].endswith("/find")
