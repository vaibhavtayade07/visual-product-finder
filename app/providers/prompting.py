"""Shared prompt building / response parsing for real vision-LLM providers.

The LLM returns JSON only; it never emits UI copy labels (D12) — just attribute
extraction, a 0..1 match estimate and a grounded reason line.
"""
from __future__ import annotations

import json
import logging
import re

from .base import RerankCandidate

log = logging.getLogger(__name__)

RERANK_SYSTEM = (
    "You are a lighting catalog matching assistant. You will receive one query photo and "
    "a numbered list of candidate product images with metadata. "
    "1) Extract the main lighting fixture's attributes from the QUERY photo: "
    "category (one of pendant_lamp, floor_lamp, table_lamp, wall_light, ceiling_light, "
    "arc_lamp, chandelier, linear_pendant, or not_a_lamp), confidence (0..1, how sure you "
    "are the photo contains a lighting fixture), style, finish, color, size_class. "
    "Target the most prominent fixture only. "
    "2) For each candidate, estimate attribute_match (0..1) against the query attributes "
    "and write one reason line (<=120 chars) mentioning at least one extracted attribute "
    "and one visual trait. "
    'Return strict JSON: {"query_attributes": {"category": str, "confidence": float, '
    '"style": str, "finish": str, "color": str, "size_class": str}, '
    '"matches": {"<SKU>": {"attribute_match": float, "reason": str}}}'
)

ENRICH_SYSTEM = (
    "You label lighting products for a catalog. Given product metadata (and optionally a "
    "thumbnail), return strict JSON with values only for the requested missing attributes: "
    '{"type": "pendant|floor|table|wall_sconce|ceiling|arc|chandelier|linear", '
    '"style": str, "finish": str, "color": str, "size_class": "small|medium|large"}. '
    "Only include keys you are confident about."
)


def rerank_user_text(candidates: list[RerankCandidate]) -> str:
    lines = ["Candidates:"]
    for i, c in enumerate(candidates, start=1):
        attrs = ", ".join(f"{k}={v}" for k, v in sorted((c.attributes or {}).items()))
        lines.append(f"{i}. SKU={c.sku} title={c.title!r} price={c.price} stock={c.stock_status} attrs=[{attrs}]")
    return "\n".join(lines)


def extract_json(text: str) -> dict:
    """LLMs occasionally wrap JSON in prose/fences; extract the first JSON object."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def parse_rerank_response(text: str, candidates: list[RerankCandidate]) -> tuple[dict, dict[str, dict]]:
    data = extract_json(text)
    query_attrs = data.get("query_attributes") or {}
    raw_matches = data.get("matches") or {}
    matches: dict[str, dict] = {}
    for cand in candidates:
        entry = raw_matches.get(cand.sku) or {}
        try:
            score = float(entry.get("attribute_match", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        reason = str(entry.get("reason", "")).strip()
        matches[cand.sku] = {"attribute_match": max(0.0, min(1.0, score)), "reason": reason}
    return query_attrs, matches
