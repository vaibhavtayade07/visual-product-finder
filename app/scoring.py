"""Central scoring module (architecture D10/D11).

ONE source of truth for score math, band decisions, similarity labels and the
out-of-stock rule R1 — imported by the live pipeline AND the eval harness so tuning
and production never drift. Pure functions, no IO.
"""
from __future__ import annotations

from typing import Any, Literal

Band = Literal["high", "medium", "low"]
Label = Literal["closest_match", "similar_style", "same_category"]

AVAILABLE_STOCK = ("in_stock", "low_stock")


def weights(cfg: dict) -> tuple[float, float, float]:
    w = cfg.get("weights", {})
    return (
        float(w.get("visual", 0.60)),
        float(w.get("attribute", 0.30)),
        float(w.get("boost", 0.10)),
    )


def boost_for(stock_status: str | None, cfg: dict) -> float:
    table = cfg.get("business_boost", {})
    return float(table.get(stock_status or "out_of_stock", 0.2))


def compute_score(visual_sim: float, attribute_match: float | None, boost: float,
                  cfg: dict, degraded: bool = False) -> float:
    """Normal path: 0.6*sim + 0.3*attr + 0.1*boost (weights from config).

    Degraded path (E10, whole query without LLM): renormalized (0.6*sim + 0.1*boost) / 0.7.
    `attribute_match=None` means "no LLM verdict for this candidate" (e.g. beyond
    rerank_k): it earns no attribute credit; it must NOT trigger the degraded
    renormalization or tail candidates would outrank vision-scored ones.
    """
    wv, wa, wb = weights(cfg)
    if degraded:
        denom = wv + wb
        if denom <= 0:
            return 0.0
        return max(0.0, min(1.0, (wv * visual_sim + wb * boost) / denom))
    attr = 0.0 if attribute_match is None else float(attribute_match)
    return max(0.0, min(1.0, wv * visual_sim + wa * attr + wb * boost))


def cap_band(band: Band, cap: Band) -> Band:
    order = {"low": 0, "medium": 1, "high": 2}
    return band if order[band] <= order[cap] else cap


def decide_band(top_score: float, category_confidence: float | None, cfg: dict,
                degraded: bool = False) -> Band:
    """Fallback ladder (§5c) + category-confidence gate (E6).

    S >= high -> high; mid <= S < high -> medium; else low.
    A category confidence below the gate forces low (non-lamp photo; never force-fit).
    Degraded results can never be labeled high (E10).
    """
    high = float(cfg.get("thresholds", {}).get("high", 0.75))
    mid = float(cfg.get("thresholds", {}).get("mid", 0.55))
    gate = float(cfg.get("category_confidence_gate", 0.50))

    if category_confidence is not None and category_confidence < gate:
        band: Band = "low"
    elif top_score >= high:
        band = "high"
    elif top_score >= mid:
        band = "medium"
    else:
        band = "low"
    return cap_band(band, "medium") if degraded else band


def apply_stock_rule(results: list[dict], cfg: dict) -> list[dict]:
    """Rule R1 (AC3.5/D10): out-of-stock is never #1 while an in-stock item clears HIGH."""
    if len(results) < 2:
        return results
    high = float(cfg.get("thresholds", {}).get("high", 0.75))
    first = results[0]
    if first.get("stock_status") != "out_of_stock":
        return results
    best_idx = None
    for i, r in enumerate(results[1:], start=1):
        if r.get("stock_status") in AVAILABLE_STOCK and float(r.get("score", 0)) >= high:
            best_idx = i
            break
    if best_idx is None:
        return results
    promoted = results.pop(best_idx)
    results.insert(0, promoted)
    return results


def assign_labels(results: list[dict], band: Band, degraded: bool = False) -> list[dict]:
    """Similarity labels are a server-side enum (D12); never freeform.

    High band: top 1-3 cards are `closest_match`, the rest `similar_style`.
    Medium/degraded: everything is `similar_style` (E3: room scenes must never be
    mislabeled as the closest match).
    """
    for i, r in enumerate(results):
        if band == "high" and not degraded and i < 3:
            r["similarity_label"] = "closest_match"
        else:
            r["similarity_label"] = "similar_style"
    return results


def finalize(results: list[dict], top_score: float | None, category_confidence: float | None,
             cfg: dict, degraded: bool = False) -> tuple[list[dict], Band]:
    """Sort -> rule R1 -> labels -> positions. Returns (cards, band)."""
    results.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    band = decide_band(top_score if top_score is not None else (
        results[0]["score"] if results else 0.0), category_confidence, cfg, degraded=degraded)
    if not results:
        return [], "low"
    if band == "low":
        # Below the low threshold nothing is a visual match (AC3.2: fallback view).
        return [], band
    results = apply_stock_rule(results, cfg)
    results = assign_labels(results[: int(cfg.get("top_n_cards", 10))], band, degraded)
    for i, r in enumerate(results, start=1):
        r["position"] = i
    return results, band


def build_reason(attribute_text: str, visual_text: str, max_chars: int = 120) -> str:
    """AC3.4: reason mentions >=1 extracted attribute and >=1 visual trait, <=120 chars."""
    text = f"{attribute_text} — {visual_text}"
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "\u2026"


def clamp_reason(text: str, max_chars: int = 120) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "\u2026"


def score_candidate(cand: dict[str, Any], cfg: dict, degraded: bool = False) -> dict[str, Any]:
    """Attach score to an internal candidate dict using the centralized math."""
    stock = cand.get("stock_status")
    boost = boost_for(stock, cfg)
    cand["boost"] = boost
    cand["score"] = round(
        compute_score(float(cand.get("visual_sim", 0.0)),
                      cand.get("attribute_match"), boost, cfg, degraded=degraded), 6)
    return cand
