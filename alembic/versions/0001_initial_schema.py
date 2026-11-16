"""Initial schema: 16 tables, HNSW index, Postgres job queue (architecture §3).

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-18
"""
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

STATEMENTS = [
    "CREATE EXTENSION IF NOT EXISTS vector",

    # ---- core catalog -------------------------------------------------------
    """
    CREATE TABLE tenants (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      slug text UNIQUE NOT NULL,
      name text,
      status text NOT NULL DEFAULT 'active',
      config jsonb NOT NULL DEFAULT '{}',
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE products (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants(id),
      sku text NOT NULL,
      title text,
      description text,
      price numeric(10,2),
      currency char(3),
      stock_status text CHECK (stock_status IN ('in_stock','low_stock','out_of_stock','discontinued')),
      category text,
      attributes jsonb NOT NULL DEFAULT '{}',
      is_active boolean NOT NULL DEFAULT true,
      source_updated_at timestamptz,
      first_seen_at timestamptz NOT NULL DEFAULT now(),
      last_seen_at timestamptz NOT NULL DEFAULT now(),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, sku)
    )
    """,
    "CREATE INDEX ix_products_tenant_active ON products (tenant_id, is_active)",
    "CREATE INDEX ix_products_tenant_category ON products (tenant_id, category)",
    """
    CREATE TABLE image_embeddings (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants(id),
      image_hash char(64) NOT NULL,
      model text NOT NULL,
      dim int NOT NULL,
      embedding vector(1408) NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, image_hash, model)
    )
    """,
    """
    CREATE INDEX ix_image_embeddings_hnsw ON image_embeddings
      USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """,
    """
    CREATE TABLE product_images (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants(id),
      product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      embedding_id uuid REFERENCES image_embeddings(id),
      source_url text NOT NULL,
      image_hash char(64),
      thumb_key text,
      width int,
      height int,
      bytes int,
      quality_status text CHECK (quality_status IN ('ok','too_small','too_dark','fetch_failed','decode_failed')),
      is_primary boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, product_id, image_hash)
    )
    """,
    "CREATE INDEX ix_product_images_embedding ON product_images (embedding_id)",
    "CREATE INDEX ix_product_images_hash ON product_images (tenant_id, image_hash)",
    """
    CREATE TABLE product_tags (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants(id),
      product_id uuid NOT NULL REFERENCES products(id) ON DELETE CASCADE,
      tag_type text NOT NULL CHECK (tag_type IN ('type','style','finish','color','material','size_class')),
      tag_value text NOT NULL,
      source text NOT NULL CHECK (source IN ('feed','llm')),
      confidence real,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, product_id, tag_type, tag_value, source)
    )
    """,
    "CREATE INDEX ix_product_tags_lookup ON product_tags (tenant_id, tag_type, tag_value)",

    # ---- query path ----------------------------------------------------------
    """
    CREATE TABLE search_queries (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid NOT NULL REFERENCES tenants(id),
      session_id text NOT NULL,
      image_key text,
      image_hash char(64),
      consent_version text NOT NULL,
      status text NOT NULL CHECK (status IN ('pending','embedding','retrieving','reranking','done','degraded','failed')),
      band text CHECK (band IN ('high','medium','low')),
      top_score real,
      query_attributes jsonb,
      candidates jsonb,
      result_payload jsonb,
      degraded boolean NOT NULL DEFAULT false,
      latency_ms int,
      embedding_ms int,
      retrieval_ms int,
      rerank_ms int,
      error_code text,
      keep_for_eval boolean NOT NULL DEFAULT false,
      photo_deleted_at timestamptz,
      expires_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_search_queries_tenant_created ON search_queries (tenant_id, created_at DESC)",
    "CREATE INDEX ix_search_queries_session ON search_queries (session_id)",
    "CREATE INDEX ix_search_queries_dedupe ON search_queries (tenant_id, image_hash, created_at DESC)",
    """
    CREATE TABLE clicks (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid REFERENCES tenants(id),
      search_query_id uuid REFERENCES search_queries(id),
      session_id text,
      sku text NOT NULL,
      position int NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_clicks_tenant_created ON clicks (tenant_id, created_at DESC)",
    """
    CREATE TABLE leads (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid REFERENCES tenants(id),
      search_query_id uuid REFERENCES search_queries(id),
      session_id text,
      name text,
      email text,
      phone text,
      note text,
      status text NOT NULL DEFAULT 'new' CHECK (status IN ('new','contacted','sourced','closed')),
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_leads_tenant_created ON leads (tenant_id, created_at DESC)",
    """
    CREATE TABLE consents (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid REFERENCES tenants(id),
      session_id text NOT NULL,
      consent_version text NOT NULL,
      consented_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_consents_tenant_session ON consents (tenant_id, session_id)",

    # ---- ops / admin / cost ---------------------------------------------------
    """
    CREATE TABLE catalog_imports (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid REFERENCES tenants(id),
      kind text CHECK (kind IN ('full','delta','mock')),
      filename text,
      status text CHECK (status IN ('validating','running','done','failed')),
      total_rows int,
      imported int DEFAULT 0,
      skipped int DEFAULT 0,
      failed int DEFAULT 0,
      validation_report jsonb DEFAULT '[]',
      stats jsonb NOT NULL DEFAULT '{}',
      started_at timestamptz,
      finished_at timestamptz,
      created_by text
    )
    """,
    """
    CREATE TABLE hygiene_findings (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid REFERENCES tenants(id),
      import_id uuid REFERENCES catalog_imports(id),
      sku text NOT NULL,
      finding_type text CHECK (finding_type IN ('no_image','quality_fail','missing_attributes','discontinued','fetch_failed')),
      detail jsonb,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_hygiene_findings_lookup ON hygiene_findings (tenant_id, finding_type, created_at DESC)",
    """
    CREATE TABLE jobs (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      type text NOT NULL CHECK (type IN ('ingest','delta_scan','reindex_sku','retention_sweep','orphan_gc')),
      tenant_id uuid REFERENCES tenants(id),
      payload jsonb NOT NULL DEFAULT '{}',
      status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','done','failed')),
      attempts int NOT NULL DEFAULT 0,
      max_attempts int NOT NULL DEFAULT 3,
      run_after timestamptz NOT NULL DEFAULT now(),
      locked_at timestamptz,
      locked_by text,
      last_error text,
      result jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_jobs_queued ON jobs (status, run_after) WHERE status = 'queued'",
    """
    CREATE TABLE usage_counters (
      tenant_id uuid NOT NULL,
      period text NOT NULL,
      queries int NOT NULL DEFAULT 0,
      embed_calls int NOT NULL DEFAULT 0,
      llm_calls int NOT NULL DEFAULT 0,
      est_cost_usd numeric(10,4) NOT NULL DEFAULT 0,
      PRIMARY KEY (tenant_id, period)
    )
    """,
    """
    CREATE TABLE provider_calls (
      id bigserial PRIMARY KEY,
      tenant_id uuid REFERENCES tenants(id),
      kind text CHECK (kind IN ('embedding','llm_rerank','enrichment')),
      provider text,
      model text,
      units real,
      est_cost_usd numeric(10,6),
      search_query_id uuid REFERENCES search_queries(id),
      import_id uuid REFERENCES catalog_imports(id),
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_provider_calls_tenant_created ON provider_calls (tenant_id, created_at DESC)",
    """
    CREATE TABLE eval_queries (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid REFERENCES tenants(id),
      image_key text NOT NULL,
      labels jsonb NOT NULL,
      notes text,
      labeled_by text,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE eval_runs (
      id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      tenant_id uuid REFERENCES tenants(id),
      config_snapshot jsonb,
      metrics jsonb,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
]

TABLES = ["eval_runs", "eval_queries", "provider_calls", "usage_counters", "jobs",
          "hygiene_findings", "catalog_imports", "consents", "leads", "clicks",
          "search_queries", "product_tags", "product_images", "image_embeddings",
          "products", "tenants"]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("SET client_min_messages TO WARNING")
    for table in TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
