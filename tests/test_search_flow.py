"""US-1/US-3/US-4/US-5/US-9: upload -> poll -> cards; dedupe; click; lead; delete."""
from conftest import DEMO_SLUG, error_code, mock_image_bytes, new_session, search, wait_query


def upload_and_wait(client, settings, index: int, session: str | None = None) -> tuple[str, dict]:
    session = session or new_session("flow")
    resp = search(client, mock_image_bytes(settings, index), session_id=session)
    assert resp.status_code == 202, resp.text
    query_id = resp.json()["query_id"]
    payload = wait_query(client, query_id)
    return session, payload


def test_exact_catalog_image_returns_source_sku_top3(client, seeded, settings):
    session, payload = upload_and_wait(client, settings, 10)  # LMP-1010
    assert payload["status"] == "done"
    assert payload["band"] == "high"
    cards = payload["results"]
    assert cards, "an exact catalog image must return matches"
    skus = [c["sku"] for c in cards]
    assert "LMP-1010" in skus[:3], skus
    card = cards[skus.index("LMP-1010")]
    assert card["similarity_label"] == "closest_match"
    assert 1 <= card["position"] <= len(cards)
    assert card["product_url"].startswith("http")
    assert card["price"] is not None
    if card["reason"]:
        assert len(card["reason"]) <= 120
    assert all(c["similarity_label"] in {"closest_match", "similar_style", "same_category"}
               for c in cards)
    assert payload["filters"] is not None  # refine chips data
    assert payload["latency_ms"] > 0


def test_poll_envelope_while_pending(client, seeded, settings):
    resp = search(client, mock_image_bytes(settings, 12), session_id=new_session())
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    assert body["poll_after_ms"] > 0
    first = client.get(f"/api/v1/search/{body['query_id']}").json()
    assert first["query_id"] == body["query_id"]
    # even pending polls carry the renderable envelope
    assert first["status"] in {"pending", "embedding", "retrieving", "reranking", "done"}
    assert "results" in first


def test_same_photo_same_session_dedupes(client, seeded, settings):
    session = new_session("dedupe")
    image = mock_image_bytes(settings, 10)
    first = search(client, image, session_id=session)
    second = search(client, image, session_id=session)
    assert first.status_code == 202 and second.status_code == 202
    assert second.json()["deduplicated"] is True
    assert second.json()["query_id"] == first.json()["query_id"]
    payload = wait_query(client, first.json()["query_id"])
    assert payload["status"] == "done" and payload["results"]


def test_different_photo_same_session_is_not_deduped(client, seeded, settings):
    session = new_session("dedupe2")
    first = search(client, mock_image_bytes(settings, 10), session_id=session)
    second = search(client, mock_image_bytes(settings, 14), session_id=session)
    assert first.json()["query_id"] != second.json()["query_id"]
    assert "deduplicated" not in second.json()


def test_click_event_logged(client, seeded, sql, settings):
    session, payload = upload_and_wait(client, settings, 10)
    resp = client.post("/api/v1/events/click", json={
        "session_id": session, "query_id": payload["query_id"], "sku": "LMP-1010", "position": 1,
    })
    assert resp.status_code == 202
    row = sql.execute("SELECT COUNT(*) AS n FROM clicks WHERE sku=%s AND position=1", ("LMP-1010",)).fetchone()
    assert row["n"] == 1
    bad = client.post("/api/v1/events/click", json={"session_id": "", "sku": ""})
    assert bad.status_code == 400
    assert error_code(bad) == "BAD_REQUEST"


def test_lead_validation_and_creation(client, seeded, sql):
    missing = client.post("/api/v1/leads", json={"session_id": "s-lead"})
    assert missing.status_code == 400
    assert "contact" in missing.json()["error"]["details"]["fields"]

    bad_email = client.post("/api/v1/leads", json={"session_id": "s-lead", "email": "nope"})
    assert bad_email.status_code == 400
    assert "email" in bad_email.json()["error"]["details"]["fields"]

    ok = client.post("/api/v1/leads", json={
        "session_id": "s-lead", "phone": "+1 555 010 1010", "note": "looking for a floor lamp",
    })
    assert ok.status_code == 201
    assert ok.json()["lead_id"]
    row = sql.execute("SELECT COUNT(*) AS n FROM leads WHERE session_id='s-lead'").fetchone()
    assert row["n"] == 1


def test_delete_my_photo_removes_object_keeps_row(client, seeded, sql, ctx, settings):
    session, payload = upload_and_wait(client, settings, 10)
    query_id = payload["query_id"]
    key = f"query/{query_id}.jpg"
    assert ctx.storage.exists(key)

    wrong = client.delete(f"/api/v1/search/{query_id}", params={"session_id": "not-mine"})
    assert wrong.status_code == 404

    resp = client.delete(f"/api/v1/search/{query_id}", params={"session_id": session})
    assert resp.status_code == 202 and resp.json()["deleted"] is True
    assert not ctx.storage.exists(key)
    row = sql.execute("SELECT image_key, photo_deleted_at, status FROM search_queries WHERE id=%s",
                      (query_id,)).fetchone()
    assert row["image_key"] is None
    assert row["photo_deleted_at"] is not None
    assert row["status"] == "done", "AC9.3: the result row survives the photo"


def test_popular_endpoint_serves_seeded_catalog(client, seeded):
    resp = client.get("/api/v1/popular", params={"limit": 6})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 6
    assert [i["position"] for i in items] == [1, 2, 3, 4, 5, 6]
    assert all(i["sku"] and i["image_url"] for i in items)


def test_unknown_query_id_is_404(client, seeded):
    resp = client.get("/api/v1/search/11111111-2222-3333-4444-555555555555")
    assert resp.status_code == 404
    assert error_code(resp) == "NOT_FOUND"


def test_refine_filters_cached_candidates(client, seeded, settings):
    _, payload = upload_and_wait(client, settings, 10)
    query_id = payload["query_id"]

    empty = client.post(f"/api/v1/search/{query_id}/refine", json={"filters": {}, "top_n": 6})
    assert empty.status_code == 200
    assert 0 < len(empty.json()["results"]) <= 6

    impossible = client.post(f"/api/v1/search/{query_id}/refine", json={
        "filters": {"finish": ["definitely_not_a_finish"]}, "top_n": 6,
    })
    assert impossible.status_code == 200
    body = impossible.json()
    assert body["results"] == []
    assert body["fallback"]["message"] == "No results with these filters"
    assert body["fallback"]["popular"], "refine fallback must offer popular items"


def test_tenant_param_unknown_is_404(client, seeded):
    resp = client.get("/api/v1/popular", params={"tenant": "no-such-tenant"})
    assert resp.status_code == 404


def test_search_on_default_tenant_uses_form_slug(client, seeded, settings):
    # explicit good slug in the form field works too
    resp = search(client, mock_image_bytes(settings, 10), tenant=DEMO_SLUG, session_id=new_session())
    assert resp.status_code == 202
    wait_query(client, resp.json()["query_id"])
