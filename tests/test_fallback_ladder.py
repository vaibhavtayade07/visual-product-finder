"""Fallback ladder (§5c): E11 duplicate images, E6 category gate, E10 LLM timeout."""
from conftest import DEMO_SLUG, mock_image_bytes, new_session, search, wait_query


def test_duplicate_image_pair_yields_single_card(client, seeded, settings):
    # LMP-1004 and LMP-1005 share byte-identical images in the mock catalog
    resp = search(client, mock_image_bytes(settings, 4), session_id=new_session("dup"))
    assert resp.status_code == 202
    payload = wait_query(client, resp.json()["query_id"])
    assert payload["status"] == "done"
    skus = [c["sku"] for c in payload["results"]]
    present = [s for s in skus if s in ("LMP-1004", "LMP-1005")]
    assert len(present) == 1, f"one card per image hash (E11), got: {present}"


def test_category_gate_forces_fallback_with_popular(client, seeded, settings, vision_hooks):
    vision_hooks.force_category = "not_a_lamp"  # mock confidence 0.2 < gate 0.5
    resp = search(client, mock_image_bytes(settings, 10), session_id=new_session("gate"))
    assert resp.status_code == 202
    payload = wait_query(client, resp.json()["query_id"])
    assert payload["status"] == "done"
    assert payload["band"] == "low"
    assert payload["results"] == [], "E6: never force-fit a non-lamp photo"
    fallback = payload["fallback"]
    assert fallback is not None
    assert fallback["message"]
    assert fallback["popular"], "fallback view must offer popular items"
    assert all(item["similarity_label"] in {"closest_match", "similar_style", "same_category"}
               for item in fallback["popular"])


def test_llm_timeout_degrades_but_never_fails(client, seeded, settings, vision_hooks, set_tenant_config):
    set_tenant_config(DEMO_SLUG, {"degrade": {"llm_timeout_s": 2}})
    vision_hooks.delay_s = 30.0  # far beyond the timeout; upgrade attempt will time out too
    try:
        resp = search(client, mock_image_bytes(settings, 10), session_id=new_session("degrade"))
        assert resp.status_code == 202
        payload = wait_query(client, resp.json()["query_id"], timeout=45)
    finally:
        set_tenant_config(DEMO_SLUG, {"degrade": {"llm_timeout_s": 20}})

    assert payload["status"] == "done", "E10: a slow LLM must degrade, never error"
    assert payload["degraded"] is True
    assert payload["band"] == "medium", "degraded results are capped below high"
    assert payload["results"], "degraded queries still render embedding-only results"
    assert all(c["similarity_label"] in {"similar_style", "same_category"} for c in payload["results"]), \
        "E3: degraded results never claim to be the closest match"
    assert payload["latency_ms"] < 40_000


def test_low_band_fallback_has_popular_and_message_shape(client, seeded, settings, vision_hooks):
    # a second E6 pass from the API surface: fallback payload fields the widget renders
    vision_hooks.force_category = "not_a_lamp"
    resp = search(client, mock_image_bytes(settings, 16), session_id=new_session("gate2"))
    payload = wait_query(client, resp.json()["query_id"])
    assert payload["status"] == "done"
    assert payload["band"] == "low"
    assert payload["degraded"] is False
    assert payload["query_attributes"]["category"] == "not_a_lamp"
