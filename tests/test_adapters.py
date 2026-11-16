"""Adapter tests: CSV validation with row numbers (AC6.4), mock catalog determinism."""
from pathlib import Path

import pytest
from PIL import Image

from app.adapters.csv_adapter import CsvAdapter
from app.adapters.mock_adapter import DUPLICATE_PAIRS, MockCatalogAdapter, sku_for

HEADER = "sku,title,price,stock,image_url\n"
HEADER_WITH_ATTRS = "sku,title,price,stock,image_url,attributes\n"


def write_csv(tmp_path: Path, body: str, header: str = HEADER) -> Path:
    path = tmp_path / "catalog.csv"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_csv_missing_required_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("sku,title\nLMP-1,x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing required columns"):
        CsvAdapter("t", path)._parse()  # noqa: SLF001


def test_csv_validation_errors_carry_row_numbers(tmp_path):
    path = write_csv(tmp_path, (
        "LMP-1,Good lamp,10,in stock,\n"          # row 2 ok
        "LMP-2,Bad price,abc,in stock,\n"         # row 3 bad price
        "LMP-3,,10,in stock,\n"                   # row 4 missing title
        "LMP-4,Confusing stock,10,maybe,\n"       # row 5 bad stock
        ",No sku,10,in stock,\n"                  # row 6 empty sku
    ))
    rows, errors = CsvAdapter("t", path)._parse()  # noqa: SLF001
    assert [r.sku for r in rows] == ["LMP-1"]
    assert [e["row"] for e in errors] == [3, 4, 5, 6], errors
    reasons = " | ".join(e["reason"] for e in errors)
    assert "price" in reasons and "title" in reasons and "stock" in reasons and "sku" in reasons


def test_csv_stock_aliases_and_attribute_forms(tmp_path):
    path = write_csv(tmp_path, (
        "LMP-1,Pendant,89.90,In Stock,,{}\n"
        "LMP-2,Table,49,low stock,,finish=chrome;color=black\n"
        "LMP-3,Floor,,0,,\n"
        "LMP-4,Arc,,available,,\n"
    ), header=HEADER_WITH_ATTRS)
    rows, errors = CsvAdapter("t", path)._parse()  # noqa: SLF001
    assert errors == []
    by_sku = {r.sku: r for r in rows}
    assert by_sku["LMP-1"].stock_status == "in_stock"
    assert by_sku["LMP-2"].stock_status == "low_stock"
    assert by_sku["LMP-2"].attributes == {"finish": "chrome", "color": "black"}
    assert by_sku["LMP-3"].stock_status == "out_of_stock"
    assert by_sku["LMP-4"].stock_status == "in_stock"
    assert by_sku["LMP-3"].price is None
    assert str(by_sku["LMP-1"].price) == "89.90"


def test_csv_bad_attributes_json_is_row_error(tmp_path):
    path = write_csv(tmp_path, "LMP-1,Lamp,10,in stock,,{not json}\n", header=HEADER_WITH_ATTRS)
    rows, errors = CsvAdapter("t", path)._parse()  # noqa: SLF001
    assert rows == []
    assert errors[0]["row"] == 2 and "JSON" in errors[0]["reason"]


def test_csv_extra_columns_are_row_errors_not_crashes(tmp_path):
    path = write_csv(tmp_path, "LMP-1,Lamp,10,in stock,,surprise,extra\n")
    rows, errors = CsvAdapter("t", path)._parse()  # noqa: SLF001
    assert rows == []
    assert errors[0]["row"] == 2
    assert "more columns" in errors[0]["reason"]
    assert errors[0]["sku"] == "LMP-1"


def test_csv_fetch_image_filesystem_ref(tmp_path):
    img_path = tmp_path / "one.jpg"
    img_path.write_bytes(b"\xff\xd8\xff-fake-jpeg")
    adapter = CsvAdapter("t", write_csv(tmp_path, "LMP-1,L,1,in stock,\n"))
    assert adapter.fetch_image(f"filesystem:{img_path.as_posix()}") == b"\xff\xd8\xff-fake-jpeg"


def test_mock_adapter_deterministic_with_cadence(tmp_path):
    a = MockCatalogAdapter("t", tmp_path, "http://base", count=70)
    b = MockCatalogAdapter("t", tmp_path / "b", "http://base", count=70)
    a.ensure_images()

    p0, p0b = a._product(0)[0], b._product(0)[0]  # noqa: SLF001
    assert p0.sku == p0b.sku == "LMP-1000"
    assert p0.title == p0b.title and p0.price == p0b.price
    assert p0.images == [], "every 23rd SKU (index 0) has no image"
    assert a._product(23)[0].images == []  # noqa: SLF001

    tiny = tmp_path / "lmp-1061.png"
    full = tmp_path / "lmp-1010.png"
    assert tiny.exists() and Image.open(tiny).size == (128, 128)
    assert full.exists() and Image.open(full).size == (512, 512)

    for src, dup in DUPLICATE_PAIRS:
        src_bytes = (tmp_path / f"{sku_for(src).lower()}.png").read_bytes()
        dup_bytes = (tmp_path / f"{sku_for(dup).lower()}.png").read_bytes()
        assert src_bytes == dup_bytes, f"{src}->{dup} must share identical image bytes"


def test_mock_adapter_write_csv_round_trips_clean(tmp_path):
    a = MockCatalogAdapter("t", tmp_path, "http://base", count=30)
    csv_path = a.write_csv(tmp_path / "catalog.csv")
    rows, errors = CsvAdapter("t", csv_path)._parse()  # noqa: SLF001
    assert errors == []
    assert len(rows) == 30
    assert rows[0].sku == "LMP-1000"
    assert rows[0].images == []


def test_mock_adapter_out_of_stock_ratio_is_bounded(tmp_path):
    a = MockCatalogAdapter("t", tmp_path, "http://base", count=60)
    products = [a._product(i)[0] for i in range(60)]  # noqa: SLF001
    out = [p for p in products if p.stock_status == "out_of_stock"]
    assert 0 < len(out) < len(products) / 2, "some out-of-stock items, but not the majority"
