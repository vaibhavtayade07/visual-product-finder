"""US-6/US-7/US-8: CSV import flow with pre-index validation, idempotency, reindex,
hygiene report, config roundtrip."""
import json

from app.services.images import load_image, normalize_jpeg
from app.services.util import sha256_hex
from conftest import make_jpeg, make_tenant, wait_import, wait_job_done

HEADER = "sku,title,price,stock,image_url,attributes\n"


def csv_upload(client, admin_headers, tenant: str, body: str, filename: str = "catalog.csv"):
    return client.post(
        f"/api/v1/admin/imports?tenant={tenant}",
        files={"file": (filename, (HEADER + body).encode("utf-8"), "text/csv")},
        headers=admin_headers,
    )


def products_count(sql, slug: str) -> int:
    row = sql.execute(
        "SELECT COUNT(*) AS n FROM products WHERE tenant_id=(SELECT id FROM tenants WHERE slug=%s)",
        (slug,)).fetchone()
    return int(row["n"])


def test_csv_import_validates_rows_before_indexing(client, admin_headers, sql, make_tenant, tmp_path):
    slug = make_tenant()
    image = tmp_path / "good.jpg"
    image.write_bytes(make_jpeg(seed=7, size=(512, 512)))

    resp = csv_upload(client, admin_headers, slug, (
        f"LMP-A,Good lamp,10.00,in stock,filesystem:{image.as_posix()}\n"
        "LMP-B,Bad price,abc,in stock,\n"
        "LMP-C,,10,in stock,\n"
        "LMP-D,Weird stock,10,definitely,\n"
        ",No sku,10,in stock,\n"
    ))
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert [e["row"] for e in body["validation_errors"]] == [3, 4, 5, 6]

    status = wait_import(client, admin_headers, body["import_id"], tenant=slug)
    assert status["status"] == "done", status
    assert status["counts"] == {"total": 5, "imported": 1, "skipped": 0, "failed": 0}, status
    assert len(status["validation_errors"]) == 4

    assert products_count(sql, slug) == 1, "malformed rows must never become products"
    row = sql.execute(
        """SELECT pi.embedding_id FROM product_images pi
           JOIN products p ON p.id = pi.product_id
           WHERE p.sku='LMP-A' AND p.tenant_id=(SELECT id FROM tenants WHERE slug=%s)""",
        (slug,)).fetchone()
    assert row and row["embedding_id"], "good row was embedded"


def test_reimport_unchanged_skips_and_reuses_embeddings(client, admin_headers, sql, make_tenant, tmp_path):
    slug = make_tenant()
    img_a = tmp_path / "a.jpg"
    img_b = tmp_path / "b.jpg"
    img_a.write_bytes(make_jpeg(seed=1, size=(512, 512)))
    img_b.write_bytes(make_jpeg(seed=2, size=(512, 512)))
    body = (f"LMP-A,Lamp A,10,in stock,filesystem:{img_a.as_posix()}\n"
            f"LMP-B,Lamp B,20,in stock,filesystem:{img_b.as_posix()}\n")

    first = wait_import(client, admin_headers, csv_upload(client, admin_headers, slug, body).json()["import_id"], tenant=slug)
    assert first["counts"] == {"total": 2, "imported": 2, "skipped": 0, "failed": 0}
    assert first["reembedded"] == 2 and first["skipped_unchanged"] == 0

    second = wait_import(client, admin_headers, csv_upload(client, admin_headers, slug, body).json()["import_id"], tenant=slug)
    assert second["counts"]["imported"] == 0
    assert second["counts"]["skipped"] == 2
    assert second["reembedded"] == 0, "unchanged images must not be re-embedded"
    assert second["skipped_unchanged"] == 2

    embeds = sql.execute(
        "SELECT COUNT(*) AS n FROM image_embeddings WHERE tenant_id=(SELECT id FROM tenants WHERE slug=%s)",
        (slug,)).fetchone()
    assert embeds["n"] == 2


def test_changed_image_replaces_old_hash_no_ghosts(client, admin_headers, sql, make_tenant, tmp_path):
    slug = make_tenant()
    img = tmp_path / "a.jpg"
    old_bytes = make_jpeg(seed=1, size=(512, 512))
    new_bytes = make_jpeg(seed=9, size=(512, 512))
    img.write_bytes(old_bytes)
    body = f"LMP-A,Lamp A,10,in stock,filesystem:{img.as_posix()}\n"

    wait_import(client, admin_headers, csv_upload(client, admin_headers, slug, body).json()["import_id"], tenant=slug)
    old_hash = sha256_hex(normalize_jpeg(load_image(old_bytes)))

    img.write_bytes(new_bytes)  # the product image changed on the store side
    status = wait_import(client, admin_headers, csv_upload(client, admin_headers, slug, body).json()["import_id"], tenant=slug)
    assert status["reembedded"] == 1, status
    new_hash = sha256_hex(normalize_jpeg(load_image(new_bytes)))
    assert new_hash != old_hash

    rows = sql.execute(
        """SELECT pi.image_hash FROM product_images pi
           JOIN products p ON p.id = pi.product_id AND p.sku='LMP-A'
           WHERE p.tenant_id=(SELECT id FROM tenants WHERE slug=%s)""",
        (slug,)).fetchall()
    assert len(rows) == 1, "E15: the changed hash replaces the old row"
    assert rows[0]["image_hash"] == new_hash
    assert old_hash not in {r["image_hash"] for r in rows}


def test_reindex_sku_job(client, admin_headers, sql, make_tenant, tmp_path):
    slug = make_tenant()
    img = tmp_path / "a.jpg"
    img.write_bytes(make_jpeg(seed=3, size=(512, 512)))
    body = f"LMP-A,Lamp A,10,in stock,filesystem:{img.as_posix()}\n"
    wait_import(client, admin_headers, csv_upload(client, admin_headers, slug, body).json()["import_id"], tenant=slug)

    resp = client.post(f"/api/v1/admin/skus/LMP-A/reindex?tenant={slug}", headers=admin_headers)
    assert resp.status_code == 202
    job = wait_job_done(sql, resp.json()["job_id"])
    assert job["status"] == "done" and job["result"]["status"] == "unchanged"

    img.write_bytes(make_jpeg(seed=11, size=(512, 512)))
    resp = client.post(f"/api/v1/admin/skus/LMP-A/reindex?tenant={slug}", headers=admin_headers)
    job = wait_job_done(sql, resp.json()["job_id"])
    assert job["result"]["status"] == "reindexed" and job["result"]["reembedded"] == 1

    unknown = client.post(f"/api/v1/admin/skus/NOPE/reindex?tenant={slug}", headers=admin_headers)
    assert unknown.status_code == 404


def test_hygiene_report_and_csv_export(client, admin_headers, make_tenant, tmp_path):
    slug = make_tenant()
    good = tmp_path / "good.jpg"
    tiny = tmp_path / "tiny.jpg"
    good.write_bytes(make_jpeg(seed=5, size=(512, 512)))
    tiny.write_bytes(make_jpeg(seed=6, size=(128, 128)))
    body = (f"LMP-OK,Lamp OK,10,in stock,filesystem:{good.as_posix()}\n"
            "LMP-NOIMG,No image,12,in stock,\n"
            f"LMP-TINY,Tiny image,13,in stock,filesystem:{tiny.as_posix()}\n")
    wait_import(client, admin_headers, csv_upload(client, admin_headers, slug, body).json()["import_id"], tenant=slug)

    report = client.get(f"/api/v1/admin/hygiene?tenant={slug}", headers=admin_headers)
    assert report.status_code == 200
    data = report.json()
    assert data["active"] == 3 and data["indexed"] == 1
    assert data["coverage_pct"] < 100
    assert [f["sku"] for f in data["findings"]["no_image"]] == ["LMP-NOIMG"]
    assert [f["sku"] for f in data["findings"]["quality_fail"]] == ["LMP-TINY"]
    assert data["last_import"]["status"] == "done"

    csv_resp = client.get(f"/api/v1/admin/hygiene?tenant={slug}&format=csv", headers=admin_headers)
    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert "finding_type,sku" in csv_resp.text
    assert "LMP-NOIMG" in csv_resp.text


def test_mock_import_is_idempotent(client, admin_headers, sql, make_tenant):
    slug = make_tenant()
    first = wait_import(client, admin_headers, client.post(
        f"/api/v1/admin/imports?tenant={slug}", json={"adapter": "mock", "count": 25},
        headers=admin_headers).json()["import_id"], tenant=slug)
    # indices 0..24: no image at 0 and 23; duplicate pairs (4,5) and (16,17) -> 21 new embeddings
    assert first["counts"] == {"total": 25, "imported": 25, "skipped": 0, "failed": 0}
    assert first["reembedded"] == 21 and first["skipped_unchanged"] == 2

    second = wait_import(client, admin_headers, client.post(
        f"/api/v1/admin/imports?tenant={slug}", json={"adapter": "mock", "count": 25},
        headers=admin_headers).json()["import_id"], tenant=slug)
    assert second["counts"] == {"total": 25, "imported": 0, "skipped": 25, "failed": 0}
    assert second["reembedded"] == 0 and second["skipped_unchanged"] == 23

    embeds = sql.execute(
        "SELECT COUNT(*) AS n FROM image_embeddings WHERE tenant_id=(SELECT id FROM tenants WHERE slug=%s)",
        (slug,)).fetchone()
    assert embeds["n"] == 21


def test_config_roundtrip_with_defaults_merge(client, admin_headers, make_tenant):
    slug = make_tenant()
    put = client.put(f"/api/v1/admin/config?tenant={slug}",
                     json={"thresholds": {"high": 0.9}}, headers=admin_headers)
    assert put.status_code == 200
    config = put.json()["config"]
    assert config["thresholds"]["high"] == 0.9
    assert config["thresholds"]["mid"] == 0.55, "defaults merge under overrides"
    assert config["weights"]["visual"] == 0.6

    get = client.get(f"/api/v1/admin/config?tenant={slug}", headers=admin_headers)
    assert get.json()["config"]["thresholds"]["high"] == 0.9

    bad = client.put(f"/api/v1/admin/config?tenant={slug}", json=["not", "an", "object"],
                     headers=admin_headers)
    assert bad.status_code == 400


def test_admin_requires_bearer_token(client, seeded):
    assert client.get("/api/v1/admin/hygiene").status_code == 401
    wrong = client.get("/api/v1/admin/hygiene", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401


def test_shopify_import_without_credentials_is_an_actionable_400(
        client, admin_headers, make_tenant, monkeypatch):
    monkeypatch.delenv("SHOPIFY_SHOP_DOMAIN", raising=False)
    monkeypatch.delenv("SHOPIFY_ADMIN_TOKEN", raising=False)
    slug = make_tenant()
    resp = client.post(f"/api/v1/admin/imports?tenant={slug}", json={"adapter": "shopify"},
                       headers=admin_headers)
    assert resp.status_code == 400
    body = resp.json()["error"]
    assert body["code"] == "BAD_REQUEST"
    assert body["details"]["reason"] == "shopify_not_configured"
    assert "SHOPIFY_SHOP_DOMAIN" in body["message"]
    assert "SHOPIFY_ADMIN_TOKEN" in body["message"]


def test_import_rejects_unknown_adapter(client, admin_headers, make_tenant):
    slug = make_tenant()
    resp = client.post(f"/api/v1/admin/imports?tenant={slug}", json={"adapter": "woo"},
                       headers=admin_headers)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "BAD_REQUEST"
