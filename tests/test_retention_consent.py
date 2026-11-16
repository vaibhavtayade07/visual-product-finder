"""US-9/§5a: retention sweep deletes expired photos (rows + consent survive), orphan GC."""
import uuid
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from conftest import make_tenant, make_jpeg, wait_job_done

ONE_HOT = "[" + ",".join(["1"] + ["0"] * 1407) + "]"


def enqueue(sql, job_type: str, tenant_id: str, payload: dict) -> str:
    row = sql.execute(
        """INSERT INTO jobs (type, tenant_id, payload, status, attempts, max_attempts,
                             run_after, created_at, updated_at)
           VALUES (%s, %s, %s, 'queued', 0, 3, now(), now(), now()) RETURNING id::text AS id""",
        (job_type, tenant_id, Jsonb({**payload, "tenant_id": tenant_id})),
    ).fetchone()
    return row["id"]


def tenant_id_of(sql, slug: str) -> str:
    return sql.execute("SELECT id::text AS id FROM tenants WHERE slug=%s", (slug,)).fetchone()["id"]


def test_retention_sweep_deletes_expired_photo_keeps_row_and_consent(client, sql, ctx, make_tenant):
    slug = make_tenant()
    tid = tenant_id_of(sql, slug)

    key = f"query/ret-{uuid.uuid4().hex[:8]}.jpg"
    ctx.storage.put(key, make_jpeg(seed=1))
    assert ctx.storage.exists(key)

    query_id = sql.execute(
        """INSERT INTO search_queries (tenant_id, session_id, image_key, image_hash, consent_version,
                                       status, created_at)
           VALUES (%s, 's-ret', %s, %s, 'v1', 'done', now() - interval '40 days')
           RETURNING id::text AS id""",
        (tid, key, "a" * 64),
    ).fetchone()["id"]
    sql.execute(
        """INSERT INTO consents (tenant_id, session_id, consent_version) VALUES (%s, 's-ret', 'v1')""",
        (tid,),
    )

    job_id = enqueue(sql, "retention_sweep", tid, {})
    job = wait_job_done(sql, job_id)
    assert job["status"] == "done" and job["result"]["deleted"] == 1

    assert not ctx.storage.exists(key), "expired photo object must be deleted"
    row = sql.execute(
        "SELECT image_key, photo_deleted_at FROM search_queries WHERE id=%s", (query_id,)
    ).fetchone()
    assert row["image_key"] is None
    assert row["photo_deleted_at"] is not None
    consent = sql.execute(
        "SELECT COUNT(*) AS n FROM consents WHERE tenant_id=%s AND session_id='s-ret'", (tid,)
    ).fetchone()
    assert consent["n"] == 1, "AC9.3: consent record survives the photo"


def test_retention_sweep_keeps_recent_photos(client, sql, ctx, make_tenant):
    slug = make_tenant()
    tid = tenant_id_of(sql, slug)
    key = f"query/keep-{uuid.uuid4().hex[:8]}.jpg"
    ctx.storage.put(key, make_jpeg(seed=2))
    sql.execute(
        """INSERT INTO search_queries (tenant_id, session_id, image_key, image_hash, consent_version,
                                       status, created_at)
           VALUES (%s, 's-keep', %s, %s, 'v1', 'done', now())""",
        (tid, key, "b" * 64),
    )
    job = wait_job_done(sql, enqueue(sql, "retention_sweep", tid, {}))
    assert job["result"]["deleted"] == 0
    assert ctx.storage.exists(key)


def test_orphan_gc_removes_unreferenced_embeddings_only(client, sql, make_tenant, ctx):
    slug = make_tenant()
    tid = tenant_id_of(sql, slug)
    model = ctx.embed.model

    orphan = sql.execute(
        """INSERT INTO image_embeddings (tenant_id, image_hash, model, dim, embedding, created_at)
           VALUES (%s, %s, %s, 1408, %s::vector, now() - interval '48 hours')
           RETURNING id::text AS id""",
        (tid, "c" * 64, model, ONE_HOT),
    ).fetchone()["id"]
    referenced = sql.execute(
        """INSERT INTO image_embeddings (tenant_id, image_hash, model, dim, embedding, created_at)
           VALUES (%s, %s, %s, 1408, %s::vector, now() - interval '48 hours')
           RETURNING id::text AS id""",
        (tid, "d" * 64, model, ONE_HOT),
    ).fetchone()["id"]
    product = sql.execute(
        """INSERT INTO products (tenant_id, sku, title, price, currency, stock_status, category,
                                 attributes, is_active)
           VALUES (%s, 'LMP-GC', 'GC lamp', 10, 'USD', 'in_stock', 'pendant', '{}', true)
           RETURNING id::text AS id""",
        (tid,),
    ).fetchone()["id"]
    sql.execute(
        """INSERT INTO product_images (tenant_id, product_id, embedding_id, source_url, image_hash,
                                       quality_status)
           VALUES (%s, %s, %s, 'filesystem:x.jpg', %s, 'ok')""",
        (tid, product, referenced, "d" * 64),
    )

    job = wait_job_done(sql, enqueue(sql, "orphan_gc", tid, {"older_than_hours": 24}))
    assert job["status"] == "done" and job["result"]["deleted"] == 1

    remaining = sql.execute(
        "SELECT id::text AS id FROM image_embeddings WHERE tenant_id=%s", (tid,)
    ).fetchall()
    ids = {r["id"] for r in remaining}
    assert orphan not in ids
    assert referenced in ids
