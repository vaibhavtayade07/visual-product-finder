"""Deterministic mock providers (keyless mode).

MockEmbeddingProvider: bytes-derived, deterministic, structurally cheap features
(downscaled luminance + colour cells + hash bits) scattered into a pseudo-random but
fixed projection. Visually similar images land closer together, identical bytes land
at cosine 1.0 — enough to exercise the pipeline, NOT to imitate real semantic quality.

MockVisionProvider: deterministic rerank/enrich stand-in. Query attributes are derived
from the best visual candidate (with small deterministic noise) so results are sane in
demos; `delay_s` / `force_category` / `force_confidence` are mock-mode test hooks for
E10/E6 paths.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import math
import random
from functools import lru_cache

from PIL import Image

from .. import lexicon
from ..scoring import build_reason
from .base import RerankCandidate, RerankResult

MOCK_EMBEDDING_MODEL = "mock-embedding-v1"
MOCK_VISION_MODEL = "mock-vision-v1"
VISUAL_TRAITS = lexicon.DESIGNS

SCHEMA_DIM = 1408


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


@lru_cache(maxsize=8)
def _scatter_map(n_feat: int, dim: int) -> tuple[tuple[int, float], ...]:
    """Fixed per-slot projection: feature j spreads into 16 dims with fixed signs."""
    mapping: list[tuple[int, float]] = []
    for j in range(n_feat):
        for k in range(16):
            h = hashlib.sha256(f"scatter:{j}:{k}".encode()).digest()
            idx = int.from_bytes(h[:4], "big") % dim
            sign = 1.0 if h[4] % 2 == 0 else -1.0
            mapping.append((idx, sign))
    return tuple(mapping)


def _scatter(feats: list[float], dim: int) -> list[float]:
    vec = [0.0] * dim
    mapping = _scatter_map(len(feats), dim)
    i = 0
    for value in feats:
        for _ in range(16):
            idx, sign = mapping[i]
            vec[idx] += value * sign
            i += 1
    return _l2_normalize(vec)


def image_features(data: bytes) -> list[float]:
    """97 deterministic features: 8x8 centre luminance + 3x3 colour cells + hash bits."""
    img = Image.open(io.BytesIO(data)).convert("RGB")
    w, h = img.size
    cw, ch = max(1, int(w * 0.7)), max(1, int(h * 0.7))
    box = ((w - cw) // 2, (h - ch) // 2, (w + cw) // 2, (h + ch) // 2)
    gray = img.crop(box).convert("L").resize((8, 8), Image.BILINEAR)
    feats = [v / 255.0 for v in gray.tobytes()]
    small = img.resize((3, 3), Image.BILINEAR)
    feats += [c / 255.0 for px in small.tobytes() for c in (px,)]
    if len(feats) != 8 * 8 + 3 * 3 * 3:  # tobytes -> single channel; keep shape fixed
        feats = feats[: 8 * 8 + 27]
    digest = hashlib.sha256(data).digest()
    feats += [digest[i] / 255.0 for i in range(6)]
    return feats


class MockEmbeddingProvider:
    est_cost_per_call = 0.0

    def __init__(self, dim: int = SCHEMA_DIM, model: str = MOCK_EMBEDDING_MODEL) -> None:
        self.dim = dim
        self.model = model

    async def embed_image(self, image_bytes: bytes) -> list[float]:
        try:
            feats = image_features(image_bytes)
        except Exception:
            # Deterministic fallback for undecodable bytes (should not happen: uploads
            # are validated before embedding).
            feats = [b / 255.0 for b in hashlib.sha256(image_bytes).digest()[:32]]
        return _scatter(feats, self.dim)

    async def embed_text(self, text: str) -> list[float]:
        tokens = (text or "").lower().split()[:24]
        feats: list[float] = []
        for tok in tokens:
            d = hashlib.sha256(tok.encode()).digest()
            feats += [d[0] / 255.0, d[1] / 255.0]
        if not feats:
            feats = [0.0]
        return _scatter(feats, self.dim)


def _attribute_overlap(query_attrs: dict, cand_attrs: dict) -> float:
    keys = ["type", "style", "finish", "color", "size_class", "material"]
    present = [k for k in keys if query_attrs.get(k)]
    if not present:
        return 0.0
    matched = sum(1 for k in present if str(cand_attrs.get(k, "")).lower() == str(query_attrs[k]).lower())
    return round(matched / len(present), 4)


class MockVisionProvider:
    """Deterministic stand-in vision LLM. No network, no keys, no cost."""

    name = "mock"
    model = MOCK_VISION_MODEL
    est_cost_per_call = 0.0

    def __init__(self, delay_s: float = 0.0, force_category: str = "",
                 force_confidence: float | None = None) -> None:
        self.delay_s = delay_s
        self.force_category = force_category
        self.force_confidence = force_confidence

    async def rerank(self, query_image: bytes, candidates: list[RerankCandidate]) -> RerankResult:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        seed = int.from_bytes(hashlib.sha256(query_image).digest()[:8], "big")
        rng = random.Random(seed)

        top = candidates[0] if candidates else None
        attrs: dict = dict(top.attributes) if top and top.attributes else {}
        # deterministic noise so attribute_match is not a constant 1.0
        if attrs:
            keys = [k for k in attrs if k in lexicon.CRITICAL_TAG_TYPES + ["material"]]
            if keys and rng.random() < 0.35:
                attrs.pop(rng.choice(keys), None)
            if rng.random() < 0.25:
                for key in ("finish", "color"):
                    if key in attrs:
                        attrs[key] = rng.choice(lexicon.FINISHES if key == "finish" else lexicon.COLORS)
                        break

        category = self.force_category or lexicon.CATEGORY_BY_TYPE.get(attrs.get("type", ""), "lamp")
        if self.force_confidence is not None:
            confidence = self.force_confidence
        elif self.force_category:
            confidence = 0.2 if self.force_category == "not_a_lamp" else 0.9
        else:
            confidence = round(0.55 + 0.44 * rng.random(), 3)
        attrs["category"] = category
        attrs["confidence"] = confidence

        matches: dict[str, dict] = {}
        for cand in candidates:
            overlap = _attribute_overlap(attrs, cand.attributes)
            trait_rng = random.Random(seed ^ (hash(cand.sku) & 0xFFFFFFFF))
            trait = VISUAL_TRAITS[trait_rng.randrange(len(VISUAL_TRAITS))]
            finish = str(cand.attributes.get("finish", "")).replace("_", " ")
            color = str(cand.attributes.get("color", "")).replace("_", " ")
            lamp = str(cand.attributes.get("type", "lamp")).replace("_", " ")
            attribute_text = ", ".join(x for x in [finish and f"{finish} finish", color and f"{color} {lamp}"] if x)
            if not attribute_text:
                attribute_text = cand.title[:60]
            reason = build_reason(attribute_text, f"matches the {trait} shape in your photo", 120)
            matches[cand.sku] = {"attribute_match": overlap, "reason": reason}
        return RerankResult(query_attributes=attrs, matches=matches, est_cost_usd=0.0)

    async def enrich(self, product: dict, thumb: bytes | None) -> dict[str, str]:
        """Fill missing critical tags deterministically (stand-in for the LLM pass)."""
        rng = random.Random(int.from_bytes(hashlib.sha256(str(product.get("sku", "")).encode()).digest()[:8], "big"))
        existing = product.get("attributes") or {}
        out: dict[str, str] = {}
        if not existing.get("type") and product.get("category"):
            for t, cat in lexicon.CATEGORY_BY_TYPE.items():
                if cat == product["category"]:
                    out["type"] = t
                    break
        if not existing.get("style"):
            out["style"] = lexicon.STYLES[rng.randrange(len(lexicon.STYLES))]
        if not existing.get("finish"):
            out["finish"] = lexicon.FINISHES[rng.randrange(len(lexicon.FINISHES))]
        if not existing.get("color"):
            out["color"] = lexicon.COLORS[rng.randrange(len(lexicon.COLORS))]
        if not existing.get("size_class"):
            out["size_class"] = lexicon.SIZE_CLASSES[rng.randrange(len(lexicon.SIZE_CLASSES))]
        return out
