# SPEC — "Show Us What You Want" Visual Product Finder (MVP)

Status: Draft v1 — ready for architect
Owner: PM · Builder: solo founder · Target: 2–4 weeks, <$50/mo infra
Decided approach: hybrid retrieval (hosted multimodal embeddings → Postgres + pgvector → vision LLM for caption/attributes/rerank). Treat as fixed unless a fatal gap is found.

---

## 1. Problem & Users

**Problem.** Lighting shoppers often arrive with a *picture* (a lamp seen in a photo, a room scene, a magazine page) but not the vocabulary ("brushed brass arc floor lamp, ~45 cm shade"). Text search fails because lighting catalogs are full of near-identical finish/size variants and shoppers don't know the naming conventions. Result: dead-end searches, bounces, lost sales. Store owners can't justify $350–480/mo hosted visual-search apps.

**Primary user — Shopper.** Non-expert, mostly mobile. Has one photo (screenshot, saved pin, camera shot of a room/object). Job: "find me this, or the closest thing you sell" in <60s, without typing lamp jargon.

**Secondary user — Store owner/admin (single store).** Solo-founder's first customer. Jobs: get the catalog indexed once; keep it fresh; see catalog quality problems (bad/missing images); receive and act on "source this product" leads.

**Long-term context.** MVP ships for ONE store, but the schema is tenant-scoped so the same engine can become a micro-SaaS later. No multi-tenant admin UI in MVP.

---

## 2. Scope

### In (must-have)
1. Photo query: mobile camera capture + file upload (single image per query).
2. Hybrid retrieval: image embedding → pgvector top-K → vision-LLM caption/attribute extraction → rerank `score = 0.6·visual_sim + 0.3·attribute_match + 0.1·business_boost` (in-stock, price band). Weights configurable, not hardcoded.
3. Results grid: 8–12 match cards (image, name, price, stock badge, one-line "why this matches" ≤120 chars, similarity label).
4. Refinement filters: finish/color, size class, price band, in-stock only.
5. Fallback ladder by score band: high → closest + same-style alternatives; medium → similar + refinement prompt; low → "no close match" + lead form + popular items in detected category.
6. Admin catalog onboarding: CSV/feed import → validation → embed → upsert with `tenant_id`; one-time LLM enrichment pass for missing structured tags.
7. Incremental updates: nightly batch + hourly delta scan (webhook adapter later).
8. Hygiene report: SKUs with no/unusable image, missing attributes, stale SKUs.
9. Privacy: upload consent, short retention, deletion job.
10. Eval set: 50–100 labeled real queries must exist before any tuning; scoring/threshold tuning reads it.
11. Mock lighting catalog (~100 SKUs, generated) so the demo works before the first store feed lands.

### Out (explicit non-goals)
- Text-only search / replacing site search. Text description is a later complement.
- Multi-image or video queries; crop/region selection UI (fast-follow candidate).
- Accounts, wishlists, saved searches, history.
- Checkout/cart integration (results link out to product pages only).
- Multi-tenant admin UI, billing, self-serve tenant signup.
- Multi-language, AR/3D, custom-trained embedding models.
- Sub-second synchronous search (async pipeline is accepted).
- Any "exact match" promise in UI copy — banned string.

### Prioritization (ruthless for a solo builder)
- **P0 ship:** upload+camera, embed→pgvector→rerank, result cards with reason, low-band fallback + lead form, CSV import + initial indexing, no-image hygiene list, consent/retention.
- **P1 after first store live:** refinement filters beyond simple post-filter, webhook delta (hourly scan is enough for v1), eval-tuning UI (config file/DB vars are fine).
- **Cut candidates if schedule slips:** mock catalog (only if a real feed exists by week 1); "Same category" third similarity label (two labels suffice); enrichment pass (feed-provided tags preferred).

---

## 3. User Stories & Acceptance Criteria (Given/When/Then)

### US-1 — Shopper uploads a photo (file picker)
Given a shopper on the finder page (mobile or desktop)
When they pick a JPEG/PNG/WebP file ≤10 MB
Then the image is validated client-side (type, size, min 200px side), previewed as thumbnail with retake/change, and a "Find matches" CTA
And after submit they see a progress state (skeleton) and results render per latency targets.

ACs:
- AC1.1 Files >10MB or non-image types are rejected inline; nothing uploads; error is actionable ("JPEG, PNG or WebP under 10 MB").
- AC1.2 Upload works on iOS Safari and Android Chrome (file picker path).
- AC1.3 Query works without login; a random session id is created and used for consent logging and analytics.

### US-2 — Shopper captures a photo (mobile camera)
Given a mobile browser
When they tap "Take a photo"
Then the OS camera opens (capture intent; getUserMedia fallback), the frame is previewed with Retake/Use-photo before submit.

ACs:
- AC2.1 Camera-permission denial falls back to file picker with explanatory copy; no dead end.
- AC2.2 iOS HEIC uploads are accepted (server-side conversion to JPEG).
- AC2.3 Capture flow works in portrait and landscape; preview is not cropped misleadingly (letterboxed, not cover-cropped).

### US-3 — Results grid (match cards)
Given a query photo produces candidate matches
When results render
Then a grid of 8–12 cards shows: product image, name, price + currency, stock badge (In stock / Low stock / Out of stock), one-line "why this matches" (≤120 chars, grounded in extracted attributes), and a similarity label.

ACs:
- AC3.1 Similarity labels come only from: "Closest match", "Similar style", "Same category". The string "exact match" appears nowhere in UI copy (grep-testable).
- AC3.2 At least 1 card renders for any query above the low threshold; otherwise the fallback view renders (never an empty grid).
- AC3.3 Each card links to the product page (new tab) and logs a click event with session id, sku, and card position.
- AC3.4 "Why this matches" mentions ≥1 extracted attribute and ≥1 visual trait (e.g., "black dome shade, matte finish, ~45 cm").
- AC3.5 Reranking uses the score formula in Scope §2 item 2; out-of-stock items can appear but are never ranked #1 while an in-stock item clears the high threshold.

### US-4 — Refinement filters
Given results are shown
When the shopper selects finish/color, size class, price band, or "in-stock only"
Then the result set re-filters (and re-ranks with constraints where supported) without re-uploading.

ACs:
- AC4.1 Filter options derive from values actually present in the tenant catalog (no dead options).
- AC4.2 Selected filters render as removable chips; clearing all restores the original ranking.
- AC4.3 Applying filters that yield zero results shows "no results with these filters" + clear-filters CTA + fallback items — never a silent empty grid.
- AC4.4 Filtering never calls the embedding API again (uses cached candidate set + metadata).

### US-5 — Low-band fallback + lead form
Given the top rerank score is below the low threshold
When the results view renders
Then the shopper sees "We couldn't find a close match", a lead form (name + email/phone + optional note), and up to 6 popular items in the detected category (bestsellers if category confidence is low).

ACs:
- AC5.1 Form submits only with ≥1 valid contact field; invalid input shows inline errors.
- AC5.2 Successful submit shows "We'll contact you if we find a match" and persists a `source_request` record (tenant-scoped) visible in admin.
- AC5.3 Lead form submission is not required to see the fallback content (content first, form second — no gate).
- AC5.4 Non-lighting photo (person, furniture, food) is allowed to reach this path; the system must not force-fit a lamp above the low threshold (category-confidence gate testable via eval set).

### US-6 — Admin: initial catalog indexing
Given admin has a CSV/feed (required: `sku,title,price,stock,image_url`; optional: `description,attributes`)
When admin uploads it and clicks "Build index"
Then the pipeline validates, fetches images, runs the image quality gate, embeds images, upserts into pgvector with `tenant_id`, runs enrichment for missing tags, and produces a per-row status report (imported / skipped / failed + reason).

ACs:
- AC6.1 100-SKU catalog fully indexed ≤15 min; 5000-SKU ≤4 h; job is background, shows progress %, and is resumable after failure without re-embedding already-embedded images (idempotent by `tenant_id + sku + image_hash`).
- AC6.2 Re-running import with unchanged data produces zero duplicate rows (idempotency test).
- AC6.3 Failed image fetches or quality-gate failures are reported per-SKU in the hygiene report — never silently dropped (fail-closed to the report, not to the void).
- AC6.4 Validation report lists malformed rows with row numbers and reasons before indexing starts.

### US-7 — Incremental catalog updates
Given an index exists
When price, stock, image, or availability changes upstream
Then changes are reflected in results within 24 h via nightly batch, and ≤2 h when the hourly delta scan (or later webhook) is enabled.

ACs:
- AC7.1 Delta detection re-embeds only changed/new images (log shows count re-embedded vs skipped).
- AC7.2 SKUs absent from the latest feed are flagged `discontinued` and excluded from results within the same window; a previously strong match backfills with the next candidate.
- AC7.3 Stock/price changes update the business boost without any re-embedding (verifiable: boost changes, vector table unchanged).

### US-8 — Admin: catalog hygiene report
Given at least one indexing run has completed
When admin opens the hygiene report
Then they see, per tenant: SKUs with no image; SKUs failing the quality gate (too small/dark/non-product); SKUs missing critical attributes (type/style/finish/color/size class); discontinued SKUs; and catalog coverage % (indexed ÷ active).

ACs:
- AC8.1 Exportable as CSV.
- AC8.2 Each finding links to the SKU id (and product-page URL where known) for manual fixing.
- AC8.3 Report regenerates on every indexing run; timestamps shown.
- AC8.4 Coverage % matches actual embed count (assertable in a query).

### US-9 — Photo consent & retention
Given a shopper is about to submit a photo
When they submit
Then a consent notice states (plain language): photo is used only to find matching products; stored ≤30 days for quality/debugging; deletable on request; not used for model training — and consent (version + timestamp) is logged against the session.

ACs:
- AC9.1 Query photos live in storage with a 30-day lifecycle rule; a deletion job runs daily and logs deleted object counts.
- AC9.2 No customer photo is copied into the product index or used for training; documented and grep-testable (index pipeline only touches catalog image sources).
- AC9.3 Consent record persists even after the photo is deleted (session id, consent version, timestamp; no photo bytes).
- AC9.4 A "delete my photo" path exists (button on result page or contact link); deletion completes ≤24 h and is logged.

---

## 4. Flows

### 4.1 Shopper flow
1. Land on `/find` (entry points: nav banner, PDP link, marketing link). One-line explainer: "Show us a lamp you like. We'll find the closest matches in our store."
2. Consent notice visible; shopper uploads or captures photo.
3. Client validates (type/size/dimensions); preview with Retake / Find matches.
4. On submit: `POST /search` with image → server stores query image (consented), returns `query_id` immediately; pipeline runs async.
5. Pipeline: embed image → pgvector top-K (K≈50, tenant-scoped) → vision-LLM caption + attribute extraction → rerank (0.6/0.3/0.1) → score-band decision.
6. Client polls/streams until results (<25 s p95, 40 s hard timeout) → renders results grid (high/medium band) or fallback view (low band).
7. Shopper refines (filters), opens product pages (click logged), or submits lead (low band).
8. Query photo auto-deletes at 30 days (unless that SKU was added to the eval set with explicit opt-in — see Open Q4).

### 4.2 Admin onboarding flow
1. Export catalog CSV/feed from the store's platform (or load mock catalog for the demo).
2. Upload in admin console → validation report (missing columns, bad rows, unreachable images).
3. Fix and re-upload, or proceed with warnings.
4. Click "Build index" → background pipeline: parse → fetch images → quality gate → batch embed → upsert `tenant_id + sku` → enrichment for missing tags → hygiene report.
5. Review hygiene report; fix worst offenders (e.g., re-shoot top SKUs with no/bad images).
6. Run eval set (50–100 labeled queries) → read top-1 / top-3 relevancy → tune weights + thresholds (config, no redeploy) → re-run.
7. Go live: link/embed `/find` on the storefront.
8. Ongoing: nightly batch + hourly delta scan; hygiene report refresh; weekly eval-set re-run as catalog grows.

### 4.3 Fallback decision tree
```
top_score S (calibrated on eval set; defaults to tune at go-live)
│
├─ S ≥ HIGH (default 0.75)
│    → "Closest match" (top 1–3) + "Same style alternatives" (next 3–5)
│
├─ MID ≤ S < HIGH (default 0.55–0.75)
│    → "Similar results" grid + refinement prompt ("narrow by finish / size / price?")
│
└─ S < MID  → OR category confidence below gate (non-lighting photo)
     → "We couldn't find a close match"
     → lead form (source request)
     → popular items in detected category; bestsellers if category confidence low
```
Rules: thresholds are per-tenant config; recalibrate whenever the eval set changes; never label anything "exact match"; if exactly one result clears LOW, show it under the honest medium/low framing rather than a padded grid.

---

## 5. Edge Cases

| # | Case | Expected behavior |
|---|------|-------------------|
| E1 | Dark / blurry photo | Client-side pre-warn if brightness/sharpness below threshold ("photo may be too dark — try another?") but allow submit; pipeline proceeds; likely lands low band → fallback. No hard block. |
| E2 | Multiple lamps in one photo | Caption step targets the most prominent object; UI copy: "We matched the main lamp in your photo." Crop UI is out of scope v1 (noted as fast-follow). |
| E3 | Room scene (lamp in context) | Embedding + caption must isolate the fixture; if not, scene-similar matches are acceptable but must not be mislabeled "Closest match". Eval set includes ≥10 room-scene queries to measure this. |
| E4 | Discontinued SKU | Excluded from results once flagged; if it was the top match, next candidate backfills; "Out of stock" is never the #1 card while an in-stock item clears HIGH (AC3.5). |
| E5 | SKU with zero usable images | Can never be returned as a visual match; appears in hygiene report; may surface only via category/popularity fallback. |
| E6 | Non-lamp photo (furniture, person, food) | Category-confidence gate → no-match fallback with bestsellers + lead form. Never force-fit a lamp. |
| E7 | Identical-twin finish variants | Multiple near-identical cards are acceptable; disambiguate via reason line (finish/dimensions) + stock/price badges. Never claim uniqueness; copy says "closest matches". |
| E8 | Catalog of 100 SKUs | Full re-embed is cheap; top-K may equal catalog size; eval set still ≥50 queries. |
| E9 | Catalog of 5000 SKUs | Batched embedding with rate-limit backoff; incremental only after initial build; K=50 to bound rerank LLM cost; nightly reconcile catches drift. |
| E10 | Slow LLM latency | p95 target 25 s, hard timeout 40 s. On timeout: if embedding-only ranking is ready, render it labeled "style matches (still refining)" and upgrade in place if the refined results arrive; otherwise retry UI. Degraded-but-useful beats spinner-forever. |
| E11 | Duplicate image across SKUs | Deduplicate by image hash to avoid cloning cards; nearest-variant collapse is optional, not required, at MVP. |
| E12 | HEIC / oversized upload | HEIC converted server-side; >10 MB rejected with clear error; absurd dimensions downscaled before embedding. |
| E13 | Connection drop mid-query | Retry button; re-submit from the retained client-side thumbnail (no re-picking the photo). |
| E14 | Vision-LLM moderation flag on query image | Generic "we couldn't process this photo" error, logged; no partial results. |
| E15 | Catalog feed with changed image for same SKU | Old vector replaced (upsert by `tenant_id+sku+image_hash`); old vector removed to avoid ghost matches. |

---

## 6. MVP Success Metrics
Measured on the **eval set** (50–100 labeled real queries; must exist before tuning) unless noted:

- Retrieval: top-1 relevant ≥40%, top-3 relevant ≥70%, zero-card rate ≤15% (of eval queries).
- Engagement (live): match-card click rate ≥25% of result sessions; refinement filter usage ≥10% of result sessions.
- Fallback: lead-form submit rate ≥5% of low-band sessions.
- Ops: catalog coverage ≥95% of active SKUs embedded; incremental freshness ≤24 h (≤2 h with scan enabled); hygiene report generated each run.
- Cost: total infra <$50/mo at MVP scale (≤5000 SKUs, ≤10k queries/mo); measured cost per query <$0.01; embedding + LLM spend tracked per tenant.
- Latency: p95 time-to-results ≤25 s; hard timeout 40 s never produces an error page (always a results or fallback view).

---

## 7. Assumptions to Confirm + Open Questions

**Assumptions (confirm with founder):**
1. First store's catalog is exportable as CSV or a feed (platform unknown — Shopify / Woo / custom).
2. Catalog size 100–5000 SKUs; most SKUs have a product image ≥800 px.
3. Traffic is small (<10k queries/mo) — the cost/latency model assumes this.
4. The store sells in at least one region where GDPR-style consent applies, or wants the posture anyway.
5. Demo has value even before the first real store: mock catalog is acceptable for early demos.

**Open questions (blocking → get answers before build week 1):**
1. First store's platform — determines CSV vs feed now and webhook vs hourly scan later. (Affects adapter priority, not architecture.)
2. Is <$50/mo a hard ceiling or soft? Vertex vs Titan free-tier/ingest pricing decides provider; affects E9 cost envelope.
3. Demo-first or productized-first? Decides whether "admin console" is a script + report file (demo) or a minimal real screen (productized).
4. Who labels the 50–100 eval queries (founder? store owner?) and by when? Blocking for tuning, not for pipeline build.
5. Retention 30 days OK, or shorter (7 days) — and does the store want lead-form data pushed to their email/CRM or kept in our admin only?
6. Confirm the "not used for training" posture is acceptable to the store's legal/compliance stance.

---

## 8. Platform Integration Note
- **Pluggable catalog adapter**, one interface: `fetchCatalog() → normalized rows` and `getDelta(since) → changed rows`. Implementations in order: `CsvAdapter` (MVP), `MockCatalogAdapter` (generated ~100-SKU lighting catalog with images for demo), later `ShopifyAdapter`, `WooAdapter`.
- **Normalized product schema** (tenant-scoped): `tenant_id, sku, title, description, price, currency, stock_status, category, attributes{type, style, finish, color, material, size_class, dimensions}, images[], source_updated_at`.
- **Multi-tenant readiness:** every table (`tenants`, `products`, `product_images`, `embeddings`, `queries`, `leads`/`source_requests`, `consents`, `hygiene_findings`) carries `tenant_id`; all reads/writes scoped; per-tenant config for weights, thresholds, retention, adapter.
- **Boundary:** outbound links only to product pages; no cart/checkout integration.

---

*Architect note: this spec is build-ready. Highest-risk items to de-risk first: (1) rerank latency budget (E10), (2) eval set existence before tuning, (3) idempotent upsert key `tenant_id + sku + image_hash`. All three colored by prior project learnings on fail-closed behavior and boundary-first testing.*
