"""Mock lighting catalog adapter: deterministic ~N SKUs with generated lamp images.

Images are drawn with Pillow (simple geometric fixtures) — no scraped or bundled
product photos, license-clean by construction. Deterministic seed 42 so tests and
demos are reproducible. Deliberate imperfections for pipeline testing:
  * every 23rd SKU has no image            (hygiene: no_image, E5)
  * every 61st SKU has a 128px image       (quality gate: too_small)
  * a few SKUs share identical image bytes (dedupe by hash, E11)
  * several SKUs miss tags                 (hygiene: missing_attributes + enrichment)
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import random
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw

from .. import lexicon
from .base import ImageRef, NormalizedProduct

log = logging.getLogger(__name__)

SEED = 42
BASE_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
DUPLICATE_PAIRS = [(4, 5), (16, 17), (41, 42)]  # (source_index, duplicate_index)
FULL_SIZE, TINY_SIZE = 512, 128
BACKGROUND = (246, 244, 241)

PRICE_BASE = {
    "pendant": 89, "floor": 139, "table": 69, "wall_sconce": 59, "ceiling": 79,
    "arc": 249, "chandelier": 299, "linear": 159,
}


def sku_for(index: int) -> str:
    return f"LMP-{1000 + index:04d}"


def _mix(rgb: tuple[int, int, int], other: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a + (b - a) * t) for a, b in zip(rgb, other))  # type: ignore[return-value]


def draw_lamp(design: str, lamp_type: str, finish: str, size_px: int, rng: random.Random,
              index: int = 0) -> bytes:
    """Draw one lamp fixture. The bottom-edge bit pattern of `index` guarantees that no two
    SKUs share bytes (only DUPLICATE_PAIRS, which copy the source file, are identical)."""
    s = size_px / 512.0
    img = Image.new("RGB", (size_px, size_px), BACKGROUND)
    d = ImageDraw.Draw(img)
    color = lexicon.FINISH_TO_RGB.get(finish, (150, 150, 150))
    cx = size_px // 2
    top = int(90 * s)
    # per-index jitter: silhouettes must be unique per SKU (only DUPLICATE_PAIRS share bytes)
    r = int((95 + 40 * rng.random()) * s)
    has_pole = lamp_type in ("floor", "arc", "table")
    shade_bottom = top + 2 * r

    d.line([(cx, 0), (cx, max(top, int(10 * s)))], fill=(90, 90, 96), width=max(2, int(4 * s)))

    if design == "dome":
        d.pieslice([cx - r, top, cx + r, top + 2 * r], 180, 360, fill=color)
        d.ellipse([cx - r, top + 2 * r - int(14 * s), cx + r, top + 2 * r + int(14 * s)], fill=color)
    elif design == "cone":
        d.polygon([(cx - r, shade_bottom), (cx + r, shade_bottom), (cx, top)], fill=color)
    elif design == "cylinder":
        d.rectangle([cx - int(0.7 * r), top, cx + int(0.7 * r), shade_bottom], fill=color)
        d.ellipse([cx - int(0.7 * r), top - int(20 * s), cx + int(0.7 * r), top + int(20 * s)], fill=_mix(color, (255, 255, 255), 0.25))
    elif design == "orb":
        d.ellipse([cx - r, top, cx + r, top + 2 * r], fill=color)
    elif design == "drum":
        d.rectangle([cx - r, top + int(20 * s), cx + r, shade_bottom], fill=color)
    else:  # globe
        d.ellipse([cx - r, top + int(10 * s), cx + r, shade_bottom], fill=color)
        d.ellipse([cx - int(0.4 * r), top + int(40 * s), cx + int(0.4 * r), top + int(90 * s)],
                  fill=_mix(color, (255, 255, 255), 0.35))

    if has_pole:
        d.line([(cx, shade_bottom), (cx, int(440 * s))], fill=(80, 80, 86), width=max(2, int(5 * s)))
        d.ellipse([cx - int(60 * s), int(430 * s), cx + int(60 * s), int(465 * s)], fill=(70, 70, 76))

    if finish == "chrome":
        d.ellipse([cx - int(0.5 * r), top + int(20 * s), cx - int(0.1 * r), top + int(70 * s)],
                  fill=_mix(color, (255, 255, 255), 0.7))
    elif finish == "natural_wood":
        for k in range(6):
            y = top + int((k + 1) * (shade_bottom - top) / 7)
            d.line([(cx - r, y), (cx + r, y)], fill=_mix(color, (60, 40, 20), 0.35), width=max(1, int(2 * s)))
    elif finish == "fabric":
        for k in range(40):
            x = rng.randint(cx - r, cx + r)
            y = rng.randint(top, shade_bottom)
            d.point((x, y), fill=_mix(color, (255, 255, 255), 0.3))

    for bit in range(12):  # unique per index: 12 fixed-position bars encoding index bits
        if (index >> bit) & 1:
            x = int((30 + bit * 6) * s)
            d.rectangle([x, int(470 * s), x + max(2, int(3 * s)), int(500 * s)], fill=(210, 208, 205))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class MockCatalogAdapter:
    adapter_type = "mock"
    delta_is_full = True

    def __init__(self, tenant_id: str, image_dir: Path, public_base_url: str,
                 count: int = 300, product_url_template: str | None = None) -> None:
        self.tenant_id = tenant_id
        self.image_dir = Path(image_dir)
        self.public_base_url = public_base_url.rstrip("/")
        self.count = count
        self.product_url_template = product_url_template
        self._gen_lock = threading.Lock()

    # ---- generation ----------------------------------------------------------
    def _product(self, index: int) -> tuple[NormalizedProduct, str | None]:
        """Returns (product, image filename or None)."""
        rng = random.Random(SEED * 1_000_003 + index)
        lamp_type = lexicon.TYPES[rng.randrange(len(lexicon.TYPES))]
        design = lexicon.DESIGNS[rng.randrange(len(lexicon.DESIGNS))]
        style = lexicon.STYLES[rng.randrange(len(lexicon.STYLES))]
        finish = lexicon.FINISHES[rng.randrange(len(lexicon.FINISHES))]
        material = lexicon.MATERIALS[rng.randrange(len(lexicon.MATERIALS))]
        color = finish.split("_")[-1] if finish in ("matte_black", "matte_white", "natural_wood") else rng.choice(lexicon.COLORS)
        size_class = rng.choices(lexicon.SIZE_CLASSES, weights=[0.3, 0.45, 0.25])[0]
        adjective = lexicon.TITLE_ADJECTIVES[rng.randrange(len(lexicon.TITLE_ADJECTIVES))]
        title = lexicon.title_for(design, lamp_type, finish, adjective)
        sku = sku_for(index)

        roll = rng.random()
        stock = "in_stock" if roll < 0.75 else ("low_stock" if roll < 0.9 else "out_of_stock")
        price = Decimal(str(round(PRICE_BASE.get(lamp_type, 99) * rng.uniform(0.7, 3.2), 2)))

        attributes = {"type": lamp_type, "style": style, "finish": finish, "color": color,
                      "material": material, "size_class": size_class}

        # deliberate tag gaps (hygiene + enrichment paths)
        if index % 7 == 0:
            attributes.pop("finish", None)
            attributes.pop("size_class", None)
        if index % 11 == 0:
            attributes.pop("style", None)
        if index % 37 == 0:
            attributes = {}

        has_image = index % 23 != 0
        filename = None
        images: list[ImageRef] = []
        if has_image:
            filename = f"{sku.lower()}.png"
            images = [ImageRef(source_url=f"{self.public_base_url}/mock-images/{filename}",
                               ref=filename)]
            attributes.setdefault("dimensions", f"{rng.randint(18, 60)} cm")

        product = NormalizedProduct(
            tenant_id=self.tenant_id,
            sku=sku,
            title=title,
            description=f"{title} — {style.replace('_', ' ')} {lamp_type.replace('_', ' ')} with a "
                        f"{material} shade in {color}. {size_class.capitalize()} size class.",
            price=price,
            currency="USD",
            stock_status=stock,
            category=lexicon.CATEGORY_BY_TYPE.get(lamp_type),
            attributes=attributes,
            images=images,
            source_updated_at=BASE_TS + timedelta(minutes=index),
        )
        return product, (filename if has_image else None)

    def ensure_images(self) -> None:
        """Redraw the whole catalog so files always match SEED (deterministic, idempotent)."""
        self.image_dir.mkdir(parents=True, exist_ok=True)
        with self._gen_lock:
            plan = {src: dst for src, dst in DUPLICATE_PAIRS}
            for index in range(self.count):
                product, filename = self._product(index)
                if filename is None:
                    continue
                rng = random.Random(SEED * 1_000_003 + index)
                lamp_type = product.attributes.get("type") or "pendant"
                finish = product.attributes.get("finish") or "chrome"
                design = lexicon.DESIGNS[index % len(lexicon.DESIGNS)]
                size = TINY_SIZE if index % 61 == 0 else FULL_SIZE
                data = draw_lamp(design, lamp_type, finish, size, rng, index=index)
                source_index = next((src for src, dst in plan.items() if dst == index), None)
                if source_index is not None:
                    src_product, src_filename = self._product(source_index)
                    src_path = self.image_dir / (src_filename or "")
                    if src_path.exists():
                        data = src_path.read_bytes()  # byte-identical -> same image_hash
                self._write_atomic(self.image_dir / filename, data)

    @staticmethod
    def _write_atomic(path: Path, data: bytes) -> None:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)  # readers never observe a partial file

    # ---- CatalogAdapter ------------------------------------------------------
    def fetch_catalog(self) -> Iterable[NormalizedProduct]:
        self.ensure_images()
        return [self._product(i)[0] for i in range(self.count)]

    def get_delta(self, since: datetime | None) -> Iterable[NormalizedProduct]:
        return self.fetch_catalog()

    def fetch_image(self, image_ref: str) -> bytes:
        path = self.image_dir / image_ref
        if not path.exists():
            self.ensure_images()
        return path.read_bytes()

    def write_csv(self, path: Path) -> Path:
        self.ensure_images()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["sku", "title", "description", "price", "currency", "stock",
                             "image_url", "attributes", "category"])
            for index in range(self.count):
                product, _ = self._product(index)
                writer.writerow([
                    product.sku, product.title, product.description or "", product.price or "",
                    product.currency, product.stock_status,
                    product.images[0].source_url if product.images else "",
                    json.dumps(product.attributes),
                    product.category or "",
                ])
        return path
