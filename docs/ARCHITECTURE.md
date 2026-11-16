# ARCHITECTURE — "Show Us What You Want" Visual Product Finder (MVP)

Status: v1 — ready for build
Input: `docs/SPEC.md` (source of truth: US-1..US-9, ACs, E1..E15, §6 metrics)
Constraint recap: solo founder, 2–4 weeks, one store, <$50/mo, multi-tenant-ready schema, no microservices / K8s / event bus.

---

## 0. Decision log (one-line rationales)

| # | Decision | Rationale |
|---|----------|-----------|
| D1 | Python 3.12 + FastAPI, ONE deployable | Every provider SDK (Vertex, Titan/boto3, Gemini, OpenAI), image tooling (Pillow, pillow-heif) and the eval harness are Python-first; one process to build, deploy, debug. |
| D2 | Frontend = server-served vanilla JS + Jinja pages (no Node build) | Widget and admin are ≤5 screens; a second JS deployable and toolchain buys nothing for MVP (US-1, US-6..8 still meet ACs). |
| D3 | Postgres 16 + pgvector (managed), HNSW index | pgvector is fixed by spec; HNSW needs no re-training on incremental upserts (E15), unlike IVFFlat. |
| D4 | Embedding: Vertex `multimodalembedding@001` (1408-d), Titan MM G1 (1024-d) as documented fallback | Vertex = strong multimodal quality, $300 GCP credit covers launch; Titan = cheaper migration path if cost ceiling tightens. Vector spaces are model-specific — fallback is a re-embed migration, NOT per-query failover (see §6.1). |
| D5 | Vision LLM: Gemini 3 Flash primary, GPT-5 mini fallback, "no LLM" degrade path | Same API family as embeddings-compatible GCP billing; both cheap Flash-class; degrade to embedding-only ranking per E10. |
| D6 | Postgres-backed job queue (`jobs` table, `FOR UPDATE SKIP LOCKED`) — no Celery/Redis | One less service; queue durability needed for resumable import (AC6.1) and worker restart recovery. |
| D7 | Polling (1.5 s) instead of SSE/WebSockets for query status | Trivial to implement across mobile/iframe environments; E10 "upgrade in place" works with polling. |
| D8 | Query photos + thumbnails in Cloudflare R2 (S3 API), 30-day lifecycle rule | AC9.1 asks for a storage lifecycle rule; free tier covers MVP; no egress fees. |
| D9 | Embeddings keyed by content hash `UNIQUE(tenant_id, image_hash, model)` | Satisfies idempotent upsert (AC6.2), no-re-embed on unchanged images (AC6.1, AC7.1), duplicate-image dedupe (E11); SKU linkage kept in `product_images` so one image shared by 2 SKUs embeds once. |
| D10 | AC3.5 (no out-of-stock #1 while an in-stock item clears HIGH) is an explicit post-sort rule, not a hope that the 0.1 weight resolves it | Deterministic, testable; weights tune relevance, rules enforce promises. |
| D11 | Score band + degraded math centralized in one `scoring.py` module used by pipeline AND eval harness | Prior-project lesson: divergent copies of scoring drift silently; one source module keeps tuning honest. |
| D12 | Similarity label is a server-side enum (`closest_match`, `similar_style`, `same_category` reserved); "exact match" banned and CI-grep-tested | AC3.1 must be grep-testable; LLM never emits UI copy labels, only reason text. |

---

## 1. Stack choice

**API service: Python 3.12 + FastAPI + Uvicorn, packaged as a single Docker image** that serves HTTP, runs the ingestion/query worker pool in-process, and hosts the scheduled jobs (nightly batch, hourly delta, daily photo deletion). Justification: the entire system is IO-bound (provider calls, image fetch, pgvector), FastAPI's async fits it; all provider SDKs are first-class Python; `pillow-heif` solves iOS HEIC server-side (AC2.2) in one wheel; a single process means one deploy, one log stream, one health check — the correct trade for a solo builder. Postgres is the only stateful dependency, reached over `DATABASE_URL`.

**Database: Postgres 16 + pgvector ≥0.7 (Neon free tier to start, eu-central region).** Justification: spec-fixed; HNSW index works with incremental upserts; at ≤5000 SKUs (1408-d float4 ≈ 28 MB of vectors) everything fits the free tier with headroom, and Neon gives PITR backups without ops work.

**Embedding provider: Google Vertex AI `multimodalembedding@001` (1408-d) primary; Amazon Titan Multimodal Embeddings G1 (1024-d) documented fallback.** Justification: single embedding space for image and text, best quality-per-dollar in class (~$0.0001/image, verify at build), $300 GCP credit wipes out launch cost. Titan (~$0.00006–0.00008/image, verify) is the fallback if the $50 ceiling proves hard or GCP onboarding stalls — switching is a one-time tenant re-embed (~15 min for 5k images, <$1), because vector spaces differ per model (D4/schema keeps `model` + `dim` per vector row).

**Vision LLM: Gemini 3 Flash primary; GPT-5 mini fallback; embedding-only degrade path final.** Justification: Flash-class models are the cost/latency sweet spot for "see query + 10 candidate thumbnails, return JSON scores + one-liners" (~$0.002/query, well under the $0.01/query target); two independent vendors remove single-provider risk; if both are down/timing out, the pipeline still renders embedding-only results (E10).

**Frontend: `/find` shopper widget and `/admin` console served as static assets + Jinja templates by the same FastAPI app; vanilla JS (fetch + DOM, no framework, no build step).** Justification: one deployable (D1), no node_modules, camera capture and polling are plain browser APIs; iframe embed keeps cross-origin integration trivial for the store. If the admin grows later, it can be split without touching the API.

---

## 2. Component diagram

```
            Storefront (store's site)                       Shopper mobile browser
   nav/PDP link ──► /find        optional <iframe src=".../find?t=slug">
                         │                                        │
                         ▼                                        ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ ONE DEPLOYABLE — FastAPI app (Docker, 1 process, asyncio)                      │
│                                                                                │
│  ┌────────────────┐  ┌────────────────┐  ┌──────────────────────────────────┐ │
│  │ Search API      │  │ Admin API      │  │ Static/Jinja: /find, /admin      │ │
│  │ POST /search    │  │ imports        │  │ (vanilla JS widget + admin UI)   │ │
│  │ GET  /search/:id│  │ hygiene report │  └──────────────────────────────────┘ │
│  │ POST /refine    │  │ reindex SKU    │                                        │
│  │ POST /leads     │  │ config, health │                                        │
│  └───────┬─────────┘  └───────┬────────┘                                        │
│          │ insert job/query   │ insert job                                      │
│          ▼                    ▼                                                 │
│  ┌──────────────────────────────────────────────────────────────────────────┐  │
│  │ Worker pool (asyncio tasks) + scheduler (in-process, advisory-locked)     │  │
│  │                                                                           │  │
│  │  QUERY pipeline      validate→store→normalize→embed→pgvector top-K→       │  │
│  │                      LLM rerank→score/band→persist payload               │  │
│  │  INGEST pipeline     adapter→validate rows→fetch img→quality gate→        │  │
│  │                      hash→embed(batch)→upsert→enrich tags→hygiene         │  │
│  │  SCHEDULED           nightly full batch · hourly delta scan ·             │  │
│  │                      daily photo retention sweep · daily orphan-vector GC │  │
│  └──────┬───────────────┬────────────────────┬──────────────────┬───────────┘  │
└─────────┼───────────────┼────────────────────┼──────────────────┼──────────────┘
          │               │                    │                  │
          ▼               ▼                    ▼                  ▼
  Postgres+pgvector   Cloudflare R2       Embedding provider   Vision LLM
  Neon (data, jobs,   (query photos 30d  Vertex MM@001       Gemini 3 Flash
  embeddings, config)  lifecycle,        (fallback: Titan    (fallback: GPT-5 mini;
                       thumbnails)        MM G1 via re-embed)  final degrade: none)
```

**Service boundaries — what is separate and why:**

| Component | Runs | Why |
|---|---|---|
| Search API, Admin API, widget pages, ingestion worker, scheduler | **Same process** | ≤10k queries/mo and ≤5k SKUs don't justify network hops; in-process queues remove an entire failure domain. |
| Postgres+pgvector | Managed service | Durability, backups, PITR without ops. |
| R2 bucket | Managed service | Durable object storage with lifecycle rules (AC9.1); used for query photos, catalog thumbnails, mock images. |
| Embedding / LLM providers | External APIs | Behind `EmbeddingProvider` / `VisionProvider` interfaces with timeouts + fallbacks (§6). |

---

## 3. Data model

All tables carry `tenant_id` (multi-tenant-ready, single tenant in MVP). UUID PKs (`gen_random_uuid()`). `jsonb` only where shape is provider/LLM-defined.

### Core catalog

```sql
tenants (
  id uuid PK, slug text UNIQUE, name text, status text DEFAULT 'active',
  config jsonb NOT NULL DEFAULT '{}',          -- weights, thresholds, retention, caps (§8)
  created_at timestamptz
);

products (
  id uuid PK, tenant_id uuid FK NOT NULL, sku text NOT NULL,
  title text, description text, price numeric(10,2), currency char(3),
  stock_status text CHECK (stock_status IN ('in_stock','low_stock','out_of_stock','discontinued')),
  category text, attributes jsonb DEFAULT '{}',   -- feed-provided {type,style,finish,color,material,size_class,dimensions}
  is_active bool DEFAULT true,
  source_updated_at timestamptz, first_seen_at timestamptz, last_seen_at timestamptz,
  created_at timestamptz, updated_at timestamptz,
  UNIQUE (tenant_id, sku)
);
CREATE INDEX ON products (tenant_id, is_active);
CREATE INDEX ON products (tenant_id, category);

image_embeddings (                              -- one vector per unique image content per model space
  id uuid PK, tenant_id uuid FK NOT NULL,
  image_hash char(64) NOT NULL,                 -- sha256 of normalized JPEG bytes
  model text NOT NULL,                          -- 'multimodalembedding@001' | 'titan-mm-g1'
  dim int NOT NULL,                             -- 1408 | 1024
  embedding vector(1408) NOT NULL,              -- column typed for primary model
  created_at timestamptz,
  UNIQUE (tenant_id, image_hash, model)         -- idempotency + dedupe (D9, AC6.2, E11)
);
CREATE INDEX ON image_embeddings
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
-- runtime: SET hnsw.ef_search = 100;  (later multi-tenant scale: pgvector 0.8 iterative scan)

product_images (
  id uuid PK, tenant_id uuid FK NOT NULL, product_id uuid FK NOT NULL,
  embedding_id uuid FK image_embeddings,        -- null if quality gate failed / fetch failed
  source_url text NOT NULL, image_hash char(64),
  thumb_key text,                               -- R2 key of 256px JPEG used for LLM rerank
  width int, height int, bytes int,
  quality_status text CHECK (quality_status IN ('ok','too_small','too_dark','fetch_failed','decode_failed')),
  is_primary bool DEFAULT true, created_at timestamptz,
  UNIQUE (tenant_id, product_id, image_hash)    -- E15: image change = new hash = new row, old row deleted
);
CREATE INDEX ON product_images (embedding_id);
CREATE INDEX ON product_images (tenant_id, image_hash);

product_tags (                                  -- merged feed + enrichment tags, provenance kept
  id uuid PK, tenant_id uuid FK NOT NULL, product_id uuid FK NOT NULL,
  tag_type text NOT NULL CHECK (tag_type IN ('type','style','finish','color','material','size_class')),
  tag_value text NOT NULL,
  source text NOT NULL CHECK (source IN ('feed','llm')),
  confidence real, created_at timestamptz,
  UNIQUE (tenant_id, product_id, tag_type, tag_value, source)
);
CREATE INDEX ON product_tags (tenant_id, tag_type, tag_value);  -- powers filter options (AC4.1)
```

### Query path

```sql
search_queries (
  id uuid PK,                                   -- the query_id returned to widget
  tenant_id uuid FK NOT NULL, session_id text NOT NULL,
  image_key text,                               -- R2 key; nulled after retention sweep
  image_hash char(64), consent_version text NOT NULL,
  status text CHECK (status IN ('pending','embedding','retrieving','reranking','done','degraded','failed')),
  band text CHECK (band IN ('high','medium','low')),
  top_score real, query_attributes jsonb,       -- LLM-extracted {category,confidence,style,finish,...}
  candidates jsonb,                             -- cached top-K [{embedding_id,product_id,sku,visual_sim}] for refine (AC4.4)
  result_payload jsonb,                         -- exact GET /search/:id body (idempotent polling)
  degraded bool DEFAULT false,
  latency_ms int, embedding_ms int, retrieval_ms int, rerank_ms int,
  error_code text,
  keep_for_eval bool DEFAULT false,             -- spec 4.1 step 8 opt-in retention
  photo_deleted_at timestamptz, expires_at timestamptz,  -- created_at + retention_days
  created_at timestamptz
);
CREATE INDEX ON search_queries (tenant_id, created_at DESC);
CREATE INDEX ON search_queries (session_id);
CREATE INDEX ON search_queries (tenant_id, image_hash, created_at DESC);  -- duplicate-photo dedupe

clicks (
  id uuid PK, tenant_id uuid FK, search_query_id uuid FK, session_id text,
  sku text NOT NULL, position int NOT NULL, created_at timestamptz    -- AC3.3 + engagement metric
);
CREATE INDEX ON clicks (tenant_id, created_at DESC);

leads (                                          -- a.k.a. source_requests (AC5.2)
  id uuid PK, tenant_id uuid FK, search_query_id uuid FK NULL, session_id text,
  name text, email text, phone text, note text,
  status text DEFAULT 'new' CHECK (status IN ('new','contacted','sourced','closed')),
  created_at timestamptz
);
CREATE INDEX ON leads (tenant_id, created_at DESC);

consents (                                       -- AC9.3: survives photo deletion, no bytes
  id uuid PK, tenant_id uuid FK, session_id text NOT NULL,
  consent_version text NOT NULL, consented_at timestamptz NOT NULL
);
CREATE INDEX ON consents (tenant_id, session_id);
```

### Ops / admin / cost

```sql
catalog_imports (
  id uuid PK, tenant_id uuid FK, kind text CHECK (kind IN ('full','delta','mock')),
  filename text, status text CHECK (status IN ('validating','running','done','failed')),
  total_rows int, imported int, skipped int, failed int,
  validation_report jsonb,                      -- [{row, reason}] malformed rows, pre-index (AC6.4)
  started_at timestamptz, finished_at timestamptz, created_by text
);

hygiene_findings (                               -- regenerated each run (AC8.3)
  id uuid PK, tenant_id uuid FK, import_id uuid FK catalog_imports,
  sku text NOT NULL,
  finding_type text CHECK (finding_type IN ('no_image','quality_fail','missing_attributes','discontinued','fetch_failed')),
  detail jsonb, created_at timestamptz
);
CREATE INDEX ON hygiene_findings (tenant_id, finding_type, created_at DESC);

jobs (                                           -- Postgres queue, SKIP LOCKED (D6)
  id uuid PK, type text CHECK (type IN ('ingest','delta_scan','reindex_sku','retention_sweep','orphan_gc')),
  tenant_id uuid FK, payload jsonb, status text DEFAULT 'queued'
    CHECK (status IN ('queued','running','done','failed')),
  attempts int DEFAULT 0, max_attempts int DEFAULT 3,
  run_after timestamptz DEFAULT now(), locked_at timestamptz, locked_by text,
  last_error text, result jsonb, created_at timestamptz, updated_at timestamptz
);
CREATE INDEX ON jobs (status, run_after) WHERE status = 'queued';

usage_counters (                                 -- atomic cap check per query (cost guard §6)
  tenant_id uuid, period text,                   -- 'YYYY-MM'
  queries int DEFAULT 0, embed_calls int DEFAULT 0, llm_calls int DEFAULT 0,
  est_cost_usd numeric(10,4) DEFAULT 0,
  PRIMARY KEY (tenant_id, period)
);

provider_calls (                                 -- per-tenant spend ledger (§6 metric)
  id bigserial PK, tenant_id uuid FK,
  kind text CHECK (kind IN ('embedding','llm_rerank','enrichment')),
  provider text, model text, units real, est_cost_usd numeric(10,6),
  search_query_id uuid NULL, import_id uuid NULL, created_at timestamptz
);
CREATE INDEX ON provider_calls (tenant_id, created_at DESC);

eval_queries (                                   -- spec §2.10: 50–100 labeled queries
  id uuid PK, tenant_id uuid FK, image_key text NOT NULL,
  labels jsonb NOT NULL,                         -- {"relevant":["LMP-1042",...],"graded":[{"sku","grade"}]}
  notes text, labeled_by text, created_at timestamptz
);

eval_runs (
  id uuid PK, tenant_id uuid FK, config_snapshot jsonb,   -- weights/thresholds used
  metrics jsonb,                                 -- top1, top3, zero_card_rate, per-category
  created_at timestamptz
);
```

**Idempotency contract (AC6.1/6.2, E15):** ingest computes `image_hash` → `INSERT ... ON CONFLICT (tenant_id, image_hash, model) DO NOTHING RETURNING` (skip embed if row exists) → `INSERT ... ON CONFLICT (tenant_id, product_id, image_hash) DO UPDATE` (relink). Image change ⇒ new hash ⇒ new `product_images` row, old row deleted, orphan GC later. Re-running any import with unchanged data is a no-op.

**Vector-dim note:** `embedding vector(1408)` is typed for the primary model. If Titan (1024) is adopted, either a second table/column or a migration sets the type; the `dim` column makes mismatches detectable at write time (`vector_dims()` check).

---

## 4. API contracts

Base: `https://finder.example.com/api/v1`. JSON everywhere except uploads. `Content-Type: application/json` responses. Admin routes require `Authorization: Bearer $ADMIN_TOKEN`.

**Error envelope (all endpoints):**

```json
{ "error": { "code": "IMAGE_TOO_LARGE", "message": "JPEG, PNG or WebP under 10 MB.", "details": { "max_mb": 10 } } }
```

| Code | HTTP | Meaning |
|---|---|---|
| `UNSUPPORTED_TYPE` | 415 | not JPEG/PNG/WebP/HEIC-safe |
| `IMAGE_TOO_LARGE` | 413 | >10 MB (E12) |
| `IMAGE_TOO_SMALL` / `DECODE_FAILED` | 400 | <200px side / corrupt |
| `CONSENT_REQUIRED` | 400 | missing consent_version (AC9) |
| `QUERY_CAP_REACHED` / `RATE_LIMITED` | 429 | cost guard (§6) |
| `NOT_FOUND` | 404 | unknown query_id / sku |
| `NOT_READY` | 409 | refine before candidates exist |
| `PROVIDER_UNAVAILABLE` | 503 | embedding providers all failed; retryable |
| `INTERNAL` | 500 | bug; logged with request id |

### 4.1 Search

**`POST /search`** — multipart/form-data: `image` (file, required), `session_id` (required, client uuid), `consent_version` (required, e.g. `"2026-10-v1"`), `tenant` (optional slug; defaults to server tenant).

```json
→ 202 { "query_id": "8f1c...", "status": "pending", "poll_after_ms": 1500 }
```
Validation server-side: magic-byte sniff (not extension), size, min side, consent. Same-photo dedupe: if a `done` query with same `(tenant, image_hash, session)` exists <10 min old → return that `query_id` (covers E13 retry + accidental resubmit).

**`GET /search/{query_id}`** — poll target; returns current state; final states carry the full render payload.

```json
200 {
  "query_id": "8f1c...", "status": "done", "band": "high", "degraded": false,
  "latency_ms": 18320,
  "query_attributes": { "category": "floor_lamp", "confidence": 0.86, "style": "mid_century",
                        "finish": "brass", "color": "black", "size_class": "large" },
  "results": [{
      "sku": "LMP-1042", "title": "Arco Floor Lamp, Black/Brass", "price": 249.00, "currency": "USD",
      "stock_status": "in_stock", "image_url": "https://cdn.store/.../lmp-1042.jpg",
      "product_url": "https://store.example/products/lmp-1042",
      "score": 0.812, "visual_sim": 0.78, "attribute_match": 0.90, "boost": 1.0,
      "similarity_label": "closest_match",
      "reason": "Black dome shade, matte finish, ~45 cm — matches the shade shape in your photo.",
      "position": 1
  }],
  "fallback": null,
  "filters": { "finish": ["brass","matte black"], "size_class": ["medium","large"],
               "price_band": { "min": 19, "max": 899 } },
  "poll_after_ms": null
}
```
Non-final statuses (`pending|embedding|retrieving|reranking`) return the same envelope with empty `results` and `poll_after_ms: 1500`. `degraded:true` (E10) may arrive with results and a later `done` upgrades the payload in place; client keeps polling until `done|failed` or 45 s client timeout (then shows last payload + retry). `failed` carries `error`. Reason ≤120 chars enforced server-side. `similarity_label` ∈ {`closest_match`,`similar_style`,`same_category`} — MVP emits the first two; never freeform (D12).

**`POST /search/{query_id}/refine`** — no re-embedding (AC4.4); re-filters/boosts over cached `candidates`.

```json
{ "filters": { "finish": ["brass"], "color": [], "size_class": ["large"],
               "price_min": 100, "price_max": 500, "in_stock_only": true },
  "top_n": 12 }
→ 200 { ...same results shape..., "band": "high", "filters_applied": {...} }
→ 200 { "results": [], "fallback": { "message": "No results with these filters",
         "popular": [<up to 6 cards>] } }          // AC4.3
→ 409 NOT_READY
```

**`GET /popular?category=&limit=6`** — leaderboard from `clicks` (30-day window), falling back to catalog order when sparse (mock/demo). Anonymous, cached 5 min in-process.

**`POST /leads`** — `{ search_query_id?, session_id, name?, email?, phone?, note? }`; requires ≥1 valid contact field (AC5.1).
```json
→ 201 { "lead_id": "...", "message": "We'll contact you if we find a match" }
```

**`POST /events/click`** — `{ session_id, query_id, sku, position }` → `202 {}` (AC3.3).

**`DELETE /search/{query_id}`** (body/query `session_id` must match) — deletes R2 object, sets `photo_deleted_at`, logs; row + consent retained (AC9.4, AC9.3). → `202 { "deleted": true }`.

### 4.2 Admin

**`POST /admin/imports`** — multipart `file` (CSV) OR `{ "adapter": "mock", "count": 100 }`. → `202 { "import_id", "status": "validating" }` (AC6.4 report lands on status endpoint).

**`GET /admin/imports/{import_id}`**
```json
{ "status": "running", "progress_pct": 62, "counts": { "total": 5000, "imported": 3100, "skipped": 5, "failed": 4 },
  "reembedded": 118, "skipped_unchanged": 3105,
  "validation_errors": [ { "row": 17, "reason": "price not numeric" } ] }
```

**`GET /admin/hygiene?format=json|csv`** (AC8.1/8.3)
```json
{ "generated_at": "...", "coverage_pct": 96.4, "indexed": 4820, "active": 5000,
  "findings": { "no_image": [...], "quality_fail": [...], "missing_attributes": [...],
                "discontinued": [...], "fetch_failed": [...] } }
```
Each finding: `{ "sku", "product_url", "detail" }` (AC8.2).

**`POST /admin/skus/{sku}/reindex`** → `202 { "job_id" }` — force re-fetch/re-embed for one SKU (manual fix, E15).

**`GET /admin/config`** / **`PUT /admin/config`** — read/update per-tenant tunables (§8) with no redeploy (§4.2 step 6).

**`GET /health`** (unauthenticated) → `{ "status": "ok", "db": "ok", "version": "0.1.0", "queue_depth": 3,
"last_import_at": "...", "degraded_providers": [] }`.

---

## 5. Sequence flows

### 5a. Index-time (initial batch + incremental)

```
Admin ──upload CSV──► POST /admin/imports
   │                        │ 1. validate rows (cols, types, image URLs)   AC6.4
   │                        │ 2. INSERT catalog_imports(validating) + job(ingest)
   │◄── validation report ──┘
   │
Worker: job(ingest) ──► for each row (resumable; skips rows already linked):
   fetch image (HTTP, 10s timeout, 2 retries, 8 concurrency)
     └─ fail ──► hygiene_findings(fetch_failed)                      AC6.3 / E5
   normalize: HEIC→JPEG (pillow-heif), EXIF strip + auto-rotate, >2048px downscale, 256px thumb→R2
   quality gate: side ≥400px (else too_small), mean-luminance check (too_dark = flagged, still indexed)
   hash = sha256(bytes)
     └─ exists (tenant,hash,model)? ──► SKIP embed (reuse embedding_id)   AC6.1/6.2
     └─ else queue for batch embed (batches ≤32 images/call)
   upsert products ON CONFLICT(tenant_id,sku) UPDATE (price/stock/category/attrs)
   upsert product_images ON CONFLICT(tenant_id,product_id,image_hash)
   update catalog_imports progress (imported/skipped/failed counts)
Enrichment pass: SKUs missing type/style/finish/color/size_class tags
   └─ 1 LLM call per batch of SKUs (metadata + thumbnail) → product_tags(source='llm')  [cut candidate]
Hygiene pass: recompute findings + coverage%; export CSV ready                 AC8
Done: catalog_imports(done), fresh rows visible to search immediately

Incremental (US-7):
• nightly 02:00 — payload mode=full, adapter.fetch_catalog() → diff by image_hash + source_updated_at:
     price/stock only ──► UPDATE products (NO re-embed)                        AC7.3
     new/changed image ──► embed new hash, delete old product_images row       E15
     missing SKU ───────► stock_status='discontinued', is_active=false        AC7.2
• hourly — payload mode=delta, adapter.get_delta(since); same diff path; log re-embedded vs skipped AC7.1
     since = claim time of the tenant's last successful ingest/delta_scan job (None → adapter
     full fetch); adapters own the overlap buffer (Shopify subtracts delta_overlap_s, 5 min)
     absent SKUs are only discontinued when get_delta() is a full snapshot (delta_is_full,
     CSV/mock) — a partial Shopify delta must never mass-discontinue                   AC7.2
• daily 03:30 — retention sweep (R2 objects >30d) + orphan-vector GC (embeddings with no
  product_images links older than 24h)
```

### 5b. Query-time (happy path) + latency budget

```
Widget          API                     Worker                 Providers/DB
  │ POST /search ─►│ sniff/validate, store original→R2 (downscaled ≤2048px, EXIF-stripped)
  │◄─ 202 qid ─────│ INSERT search_queries(pending) + signal worker
  │                │                     │ status=embedding → embed image (Vertex)
  │                │                     │ status=retrieving → pgvector top-50 (tenant-scoped,
  │                │                     │    is_active, quality_status='ok'; <=> cosine)
  │ GET qid (poll) │                     │ status=reranking → ONE LLM call:
  │                │                     │    query image + 10 candidate 256px thumbs (from R2)
  │                │                     │    → {query_attrs + per-sku match 0..1 + reason ≤120c}
  │                │                     │ score = 0.6·sim + 0.3·attr + 0.1·boost   (config §8)
  │                │                     │ rule R1 (AC3.5): out-of-stock never #1 while an
  │                │                     │    in-stock card clears HIGH → promote best in-stock
  │                │                     │ band = high | mid | low
  │                │                     │ persist result_payload, band, timings; status=done
  │◄─ 200 done ────│◄────────────────────┘
  │ render grid (8–12 cards, skeleton until done)
  │ refine / click / lead → §4
```

**Latency budget (p95, target ≤25 s hard 40 s — §6 spec):**

| Step | p95 | Timeout | On timeout |
|---|---|---|---|
| Upload + validate + store (client network bound) | 3.0 s | 15 s | 413/400 |
| Normalize/downscale (+HEIC) | 1.0 s | 3 s | 400 DECODE_FAILED |
| Embed query (primary) | 3.0 s | 8 s ×2 attempts | see §6.1 |
| pgvector top-50 | 0.3 s | 2 s | fail → 503 |
| LLM rerank (query + 10 thumbnails, 1 call) | 12.0 s | **20 s hard** | degrade path (below) |
| Scoring/persist + poll pickup (1.5 s poll) | 1.7 s | — | — |
| Web/transport overhead | 2.0 s | — | — |
| **Total** | **≈23 s** | **40 s wall** | never an error page |

**LLM timeout/degrade path (E10):** on rerank exceeding 20 s or provider error → score with available signals renormalized `score = (0.6·sim + 0.1·boost)/0.7`, `band` capped at `medium`, labels only `similar_style`, `degraded:true` + banner "style matches (still refining)". If the LLM response lands before the query's 40 s wall (retry once), payload is upgraded in place; client's next poll renders the refined grid. If not, degraded result is final for that query. No-LLM mode (`RERANK_MODE=text_only`) is the same mechanism with attribute_match from tag overlap instead of vision — cost valve (§6).

### 5c. No-match fallback branch

```
rerank done → top_score S and category confidence C
 ├─ S < MID (0.55)  OR  C < gate (0.5)     ← non-lamp photos land here (E6, AC5.4)
 │    band='low': message "We couldn't find a close match"
 │    + up to 6 popular items (category-scoped; bestsellers if C low)   US-5
 │    + lead form (rendered below content, never a gate — AC5.3)
 ├─ MID ≤ S < HIGH → "Similar results" grid + refinement prompt chips  AC4.1
 └─ S ≥ HIGH       → "Closest match" (top 1–3) + "Similar style" (next 3–5)
Exactly one result clearing MID → show it honestly under medium framing (no padded grid).
```

---

## 6. Failure modes

### 6.1 Provider failures (embedding)

**Design truth: vector spaces are model-specific.** Vertex and Titan embeddings are not comparable — per-query failover across providers would silently break similarity. Therefore:

| Failure | Detection | Degradation | Spec ref |
|---|---|---|---|
| Embedding timeout (single attempt) | per-call timeout 8 s | retry once (1 s backoff) | — |
| Embedding provider down (both attempts fail) | 2 consecutive timeouts/5xx | if tenant has a synced secondary space (dual-write tenants only) → embed via secondary & search its space; else `status=failed`, code `PROVIDER_UNAVAILABLE`, widget shows retry UI | E13 pattern |
| Sustained Vertex outage (hours) | health `degraded_providers` + alert | founder runs `POST /admin/tenants/{id}/reembed` (job): re-embed catalog with Titan (~15 min / 5k SKUs, <$1), flip `tenant.config.embedding.model`; queries resume in the new space | D4 |
| Embedding moderation flag on query image | provider error surfaced | generic "we couldn't process this photo", no partial results, logged | E14 |

### 6.2 Full failure & edge table

| Failure / edge | Detection | Behavior | Spec ref |
|---|---|---|---|
| LLM rerank timeout (>20 s) or down | per-call timeout; 2 provider failures | embedding-only degrade, band capped medium, banner + in-place upgrade; if mode `text_only`, tag-overlap fallback | E10 |
| LLM cost spike | `provider_calls` daily sum vs cap | admin banner + log; `cost_guard=block` at 2× monthly cap | §6 |
| Postgres/pgvector down | `/health` db check; query 503 | widget retry view; no fake results; UptimeRobot alert | E13 |
| Oversized upload (>10 MB) | client pre-check + server 413 | inline copy "JPEG, PNG or WebP under 10 MB", nothing processed | E12, AC1.1 |
| Absurd dimensions / pixel bomb | Pillow `MAX_IMAGE_PIXELS` guard; downscale to 2048px pre-embed | processed normally, capped | E12 |
| HEIC upload (iOS) | magic bytes + `pillow-heif` decode | convert server-side; decode failure → `DECODE_FAILED` 400 actionable copy | AC2.2 |
| Duplicate photo (resubmit / same session) | `(tenant, image_hash, session)` hit <10 min | return existing `query_id`; zero repeat provider spend | E13 |
| Duplicate catalog image across SKUs | same `image_hash` | embed once (D9); cards collapsed per image_hash at assembly (best-scoring SKU shown) | E11 |
| Rate runaway / cost guard | `usage_counters` atomic increment pre-embedding; per-IP 10/min token bucket | 429 `QUERY_CAP_REACHED` / `RATE_LIMITED` with friendly copy; cap check fail-closed (counter shares the DB search already requires) | prior-learning: limit checks fail closed |
| Corrupt/invalid image | decode error | 400 `DECODE_FAILED`, actionable, nothing stored | AC1.1 |
| Dark/blurry photo | client canvas mean-luminance pre-warn only | warn "photo may be too dark — try another?" but allow submit; likely low band → fallback | E1 |
| Non-lamp photo | LLM category confidence < gate | low band: no-match view + bestsellers + lead form; never force-fit | E6, AC5.4 |
| Room scene | LLM prompt targets most prominent fixture | scene-similar matches allowed but labeled ≤`similar_style`, never `closest_match` | E3 |
| Zero usable images for SKU | quality gate / no fetch | never returned as visual match; hygiene report; may appear via popular fallback only | E5 |
| Feeds with discontinued SKU | SKU absent from latest feed | flagged, excluded same window; strong match backfills | AC7.2, E4 |

---

## 7. Deployment topology

**Smallest viable production setup (single-tenant):**

```
┌─────────────────────────── Hetzner CX22 VPS (~$4.6/mo, eu-central) ────────────┐
│  docker compose:                                                               │
│    app   — finder-api:latest (FastAPI+worker+scheduler, 1 process)             │
│    caddy — TLS termination + reverse proxy (auto Let's Encrypt)                │
└──────────────┬─────────────────────────────┬───────────────────────────────────┘
               │ DATABASE_URL (TLS)          │ S3 API
               ▼                             ▼
      Neon Postgres 16 + pgvector     Cloudflare R2 bucket
      (free tier, eu-central)          （query photos 30d lifecycle,
      PITR backups                     thumbnails, mock images)
               ┌─────────────────────────────┐
               │ external providers: Vertex  │
               │ (embed), Gemini, OpenAI     │
               └─────────────────────────────┘
```

- **One app container**, one uvicorn process (concurrency via asyncio); scheduler jobs guarded by `pg_try_advisory_lock` so restarts can never double-run. Worker pool consumes `jobs` via `SELECT ... FOR UPDATE SKIP LOCKED`.
- Deploy = `docker compose pull && docker compose up -d` (scripted); DB migrations via Alembic on startup (single head, linear).
- Logging: structured JSON to stdout (`docker logs`), request id on every line; errors with stack. Monitoring MVP: UptimeRobot (free) on `/health` + a daily job that logs/emails cost + failed-query counts (fail counts surfaced in health payload).
- Backups: Neon PITR (restore drill at launch, not assumed).

**Env vars (names only; values in server-side `.env`, `chmod 600`, never in repo/widget):**

```
DATABASE_URL, PUBLIC_BASE_URL, DEFAULT_TENANT_SLUG, ADMIN_TOKEN,
R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_PUBLIC_BASE_URL,
GCP_PROJECT_ID, GCP_SA_JSON (Vertex embed), VERTEX_LOCATION, EMBEDDING_MODEL,
GEMINI_API_KEY (primary LLM), OPENAI_API_KEY (fallback LLM),
LLM_PRIMARY, LLM_FALLBACK, RERANK_MODE=vision|text_only,
QUERY_CAP_MONTHLY=10000, RETENTION_DAYS=30, LOG_LEVEL
```

**Secrets handling:** GCP service account scoped to Vertex AI User only; R2 token scoped to the one bucket; admin token rotatable via env change (single store → acceptable); no secret ever reaches the browser bundle; CORS pinned to own origin + storefront domain.

**Cost estimate (steady state, ≤5k SKUs, 10k queries/mo):**

| Item | $/mo |
|---|---|
| VPS CX22 | 4.6 |
| Neon Postgres (free tier; step-up to Launch $19 only if >0.5 GB, unlikely at MVP) | 0 |
| R2 (≤10 GB free; ~5 GB overage worst case) | 0–0.2 |
| Vertex embeddings (~10k query + delta embeds @ ≈$0.0001/img) | 1–2 |
| LLM rerank (~$0.002/query, thumbs-only, verify pricing at build) | ~20 |
| Domain (amortized) | ~1.5 |
| **Total** | **≈$27–28 (headroom to $50)** |

Cost valves if the ceiling tightens: `RERANK_MODE=text_only` (LLM spend → ~$5), rerank_k 10→6, per-tenant cap. Measured cost/query ≈ $0.002–0.003 ≪ $0.01 target (§6 spec).

---

## 8. Config & tunables

**Location:** defaults in repo `config/defaults.yaml` → seeded into `tenants.config` (jsonb) at tenant creation → read at runtime (60 s cache) → editable via `PUT /admin/config` (no redeploy). **One scoring module (`scoring.py`) is imported by both the live pipeline and the eval harness (D11).**

| Key | Default | Notes / tuning source |
|---|---|---|
| `weights.visual / attribute / boost` | 0.60 / 0.30 / 0.10 | grid-searched on eval set |
| `thresholds.high / mid` | 0.75 / 0.55 | grid-searched; recalibrate when eval set changes |
| `category_confidence_gate` | 0.50 | E6 non-lamp rejection; tuned on ≥10 room-scene + non-lamp eval queries (E3) |
| `top_k_retrieve` | 50 | spec K≈50 bounds rerank cost; can drop to 20–40 if eval holds |
| `rerank_k` | 10 | candidates sent to LLM |
| `top_n_cards` | 10 (8–12 range) | AC3.2 |
| `reason_max_chars` | 120 | AC3.4 |
| `business_boost` | in_stock 1.0 / low 0.6 / out 0.2 | only input = stock at MVP; rule R1 (AC3.5) independent |
| `degrade.llm_timeout_s` | 20 | E10 |
| `embed.timeout_s / retries` | 8 / 2 | §6.1 |
| `retention_days` | 30 | AC9.1 (7 = one flag change if founder answers Open Q5) |
| `query_cap_monthly` | 10000 | cost guard; `cost_guard: warn\|degrade\|block` |
| `rate_limit.ip_per_min` | 10 | burst guard |
| `adapters.catalog` | `"csv"` (`"mock"` for demo) | §9 |
| `embedding.model / dim` | `multimodalembedding@001` / 1408 | flip + re-embed job on provider change |

**Tuning loop (§4.2):** labels in `eval_queries` → `eval/run_eval.py` executes pipeline in eval mode → writes `eval_runs.metrics` (top-1 ≥40%, top-3 ≥70%, zero-card ≤15% targets) → grid over weights/thresholds → best config written to `tenants.config` → re-run. **Gate: no tuning before ≥50 labeled queries exist (spec §2.10).**

---

## 9. Adapter interface (catalog sources)

One `Protocol`; CSV + mock + Shopify ship now; Woo implements the same two methods later **without touching core** (spec §8).

```python
class CatalogAdapter(Protocol):
    adapter_type: str                      # 'csv' | 'mock' | 'shopify' | 'woo'
    delta_is_full: bool                    # True when get_delta() is still a full snapshot

    def fetch_catalog(self) -> Iterable[NormalizedProduct]: ...
        # full snapshot; used by initial build + nightly reconcile
    def get_delta(self, since: datetime) -> Iterable[NormalizedProduct]: ...
        # changed rows since timestamp; CSV/mock = same as fetch (diffed downstream by hash);
        # Shopify = updated_at:>=since (delta_is_full=False) -> never mass-discontinues
    def fetch_image(self, image_ref: str) -> bytes: ...
        # returns raw bytes; core owns normalize/gate/hash/embed

@dataclass
class NormalizedProduct:                  # mirror of §3 products/products_images
    tenant_id: str; sku: str; title: str; description: str | None
    price: Decimal; currency: str
    stock_status: Literal['in_stock','low_stock','out_of_stock']
    category: str | None
    attributes: dict                      # {type,style,finish,color,material,size_class,dimensions}
    images: list[ImageRef]                # [{source_url, role: 'primary'|'extra'}]
    source_updated_at: datetime | None
```

- Registry: `adapters.get(tenant.config.adapters.catalog)`; tenant-scoped.
- `CsvAdapter` maps configurable columns (required `sku,title,price,stock,image_url`; optional `description,attributes`); malformed rows → validation report with row numbers (AC6.4), never silently dropped.
- `MockCatalogAdapter`: deterministic seed → ~100 lighting SKUs with procedurally generated lamp images (PIL) uploaded to R2 `mock/` prefix — powers demo + eval before the real feed (E8, AC-cut-candidate #1).
- `ShopifyAdapter` (implemented): Admin GraphQL pinned by `SHOPIFY_API_VERSION` (default 2026-07), credentials from `SHOPIFY_SHOP_DOMAIN` / `SHOPIFY_ADMIN_TOKEN` (`read_products` + `read_inventory`); one row per product (cheapest available variant), `updated_at` deltas (delta_is_full=False), public CDN images fetched without auth, 250/page pagination.
- Core (normalize, quality gate, hash, embed, upsert, enrich, hygiene) is adapter-agnostic; the boundary is exactly where §8 spec draws it.

---

## 10. Open items for the founder (blocking or deadline-bound)

1. **Provider keys/accounts** — GCP project + service account (Vertex embeddings), Gemini API key, OpenAI key (fallback), Cloudflare R2 token, domain for TLS (camera capture requires HTTPS). *Blocking build week 1.*
2. **First store's platform** (spec Open Q1) — Shopify/Woo/custom decides CSV vs feed now, webhook vs hourly later. *Not architecture-blocking; adapter priority only.*
3. **Is <$50/mo a hard or soft ceiling** (spec Open Q2) — if hard, verify Vertex vs Titan pricing at build and consider `RERANK_MODE=text_only` from day one. *Affects provider final pick, not design.*
4. **Demo-first vs productized-first** (spec Open Q3) — design assumes minimal real admin pages (needed anyway for AC6/AC8); fallback: same API with report-only pages. *Scope decision only.*
5. **Who labels the 50–100 eval queries, by when** (spec Open Q4) — blocking tuning, not pipeline build; labeling via CSV import to `eval_queries`. *Deadline: before go-live tuning.*
6. **Retention 30 vs 7 days; lead routing** (spec Open Q5) — one config flag; leads are admin-only in MVP (no CRM push) unless store wants email delivery.
7. **"Not used for training" sign-off** (spec Open Q6) — index pipeline only ever reads catalog image sources; query photos never enter the index (AC9.2, grep-testable); store's legal must accept posture.
8. **Storefront embed surface** — confirm iframe snippet placement (nav banner / PDP link) and store's cooperation for go-live.

---

## 11. Traceability (build checklist)

| Requirement | Where it lives |
|---|---|
| US-1/2 upload+camera, client validation | widget JS (§2), server re-validation (§4.1) |
| US-3 cards, labels, reason, AC3.1 grep, AC3.5 | `scoring.py` + label enum (D12) + rule R1 (D10); CI grep test for "exact match" |
| US-4 refine w/o re-embed | `candidates` cache + `/refine` (§4.1, AC4.4) |
| US-5 fallback + lead | §5c, `/leads` |
| US-6 import pipeline, idempotency, resumable | `jobs`, `catalog_imports`, UNIQUE keys (§3) |
| US-7 incremental | nightly/hourly jobs (§5a), AC7.3 = products-only UPDATE |
| US-8 hygiene + CSV | `hygiene_findings`, `/admin/hygiene?format=csv` |
| US-9 consent/retention/delete | `consents`, R2 lifecycle + `retention_sweep`, `DELETE /search/:id` |
| §6 latency/zero-card/cost | §5b budget, §5c, `usage_counters`+`provider_calls` |
| E1–E15 | §6.2 table |

**Highest-risk items to de-risk first (build order):** (1) LLM rerank p95 in the budget (§5b — prototype day 1 with real Gemini key); (2) idempotent upsert key end-to-end (AC6.2 test on re-run); (3) provider abstraction seam so a Titan migration is a job, not a rewrite; (4) eval set existence before any weight tuning.

*End of ARCHITECTURE v1. Build can start with the mock catalog + Neon + Vertex key; nothing in US-1..US-9, E1..E15, or §6 is unaccounted for above.*
