"""Scoring unit tests (D10/D11): one source of truth for score, band, labels, rule R1."""
from app.scoring import (apply_stock_rule, assign_labels, build_reason, cap_band,
                         clamp_reason, compute_score, decide_band, finalize,
                         score_candidate, weights)


def cfg(**overrides) -> dict:
    base = {
        "weights": {"visual": 0.6, "attribute": 0.3, "boost": 0.1},
        "thresholds": {"high": 0.75, "mid": 0.55},
        "category_confidence_gate": 0.5,
        "business_boost": {"in_stock": 1.0, "low_stock": 0.6, "out_of_stock": 0.2},
        "top_n_cards": 10,
    }
    base.update(overrides)
    return base


def test_weights_defaults():
    assert weights({}) == (0.60, 0.30, 0.10)


def test_compute_score_normal_and_degraded():
    c = cfg()
    assert abs(compute_score(1.0, 1.0, 1.0, c) - 1.0) < 1e-9
    assert abs(compute_score(0.5, 0.5, 0.5, c) - 0.5) < 1e-9
    # no LLM verdict for this candidate -> no attribute credit, no renormalization
    assert abs(compute_score(1.0, None, 1.0, c) - 0.7) < 1e-9
    # degraded whole query renormalizes over visual+boost only
    assert abs(compute_score(1.0, None, 1.0, c, degraded=True) - 1.0) < 1e-9
    assert abs(compute_score(0.7, None, 0.2, c, degraded=True) - (0.6 * 0.7 + 0.1 * 0.2) / 0.7) < 1e-9
    assert abs(compute_score(2.0, 2.0, 2.0, c) - 1.0) < 1e-9  # clamped
    assert compute_score(-1.0, -1.0, -1.0, c) == 0.0


def test_unscored_tail_never_outranks_vision_scored_candidate():
    """Regression: None attribute_match used to renormalize (+0.99) and outrank the
    image that matched at visual_sim 1.0 with a real (if mediocre) attribute verdict."""
    c = cfg()
    tail = compute_score(0.999, None, 1.0, c)            # no LLM verdict
    vision = compute_score(1.0, 0.7, 1.0, c)             # sim 1.0 + attr verdict
    assert vision > tail


def test_cap_band():
    assert cap_band("high", "medium") == "medium"
    assert cap_band("medium", "high") == "medium"
    assert cap_band("low", "medium") == "low"


def test_decide_band_thresholds_gate_and_degrade():
    c = cfg()
    assert decide_band(0.9, 0.9, c) == "high"
    assert decide_band(0.6, 0.9, c) == "medium"
    assert decide_band(0.2, 0.9, c) == "low"
    # E6: low category confidence forces low even with a top score
    assert decide_band(0.95, 0.2, c) == "low"
    # E10: degraded can never be high
    assert decide_band(0.95, 0.9, c, degraded=True) == "medium"
    assert decide_band(0.2, 0.9, c, degraded=True) == "low"


def test_finalize_sorts_labels_and_positions():
    results = [
        {"sku": "A", "score": 0.60, "stock_status": "in_stock"},
        {"sku": "B", "score": 0.90, "stock_status": "in_stock"},
        {"sku": "C", "score": 0.70, "stock_status": "in_stock"},
        {"sku": "D", "score": 0.76, "stock_status": "in_stock"},
    ]
    cards, band = finalize(results, 0.90, 0.9, cfg())
    assert band == "high"
    assert [c["sku"] for c in cards] == ["B", "D", "C", "A"]
    assert [c["position"] for c in cards] == [1, 2, 3, 4]
    assert [c["similarity_label"] for c in cards] == [
        "closest_match", "closest_match", "closest_match", "similar_style"]


def test_finalize_low_band_returns_no_cards():
    cards, band = finalize([{"sku": "A", "score": 0.3, "stock_status": "in_stock"}], 0.3, 0.9, cfg())
    assert cards == [] and band == "low"


def test_finalize_medium_band_labels_everything_similar_style():
    cards, band = finalize([{"sku": "A", "score": 0.6, "stock_status": "in_stock"}], 0.6, 0.9, cfg())
    assert band == "medium"
    assert cards[0]["similarity_label"] == "similar_style"


def test_assign_labels_degraded_never_closest_match():
    results = [{"sku": "A"}, {"sku": "B"}]
    assign_labels(results, "high", degraded=True)
    assert [r["similarity_label"] for r in results] == ["similar_style", "similar_style"]


def test_rule_r1_promotes_available_item():
    results = [
        {"sku": "OOS", "score": 0.95, "stock_status": "out_of_stock"},
        {"sku": "OK-low", "score": 0.60, "stock_status": "in_stock"},
        {"sku": "OK-high", "score": 0.80, "stock_status": "in_stock"},
    ]
    promoted = apply_stock_rule(list(results), cfg())
    assert promoted[0]["sku"] == "OK-high"


def test_rule_r1_keeps_out_of_stock_when_nothing_clears_high():
    results = [
        {"sku": "OOS", "score": 0.95, "stock_status": "out_of_stock"},
        {"sku": "OK", "score": 0.70, "stock_status": "in_stock"},
    ]
    assert apply_stock_rule(list(results), cfg())[0]["sku"] == "OOS"


def test_out_of_stock_scores_below_available_at_equal_similarity():
    oos = score_candidate({"visual_sim": 0.8, "attribute_match": 0.8, "stock_status": "out_of_stock"}, cfg())
    ok = score_candidate({"visual_sim": 0.8, "attribute_match": 0.8, "stock_status": "in_stock"}, cfg())
    assert ok["score"] > oos["score"]
    assert oos["boost"] == 0.2 and ok["boost"] == 1.0


def test_clamp_reason_and_build_reason():
    long = "word " * 60
    clamped = clamp_reason(long, 120)
    assert len(clamped) <= 120 and clamped.endswith("\u2026")
    assert clamp_reason("  a   b\n c ", 120) == "a b c"
    reason = build_reason("chrome finish", "matches the dome silhouette in your photo", 120)
    assert len(reason) <= 120
    assert "chrome" in reason and "silhouette" in reason
