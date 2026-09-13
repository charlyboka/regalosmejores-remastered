# Regalosmejores — Project Blueprint

**Status:** approved for implementation · **Locale:** Spain only (`es-ES`) · **Domain:** regalosmejores.com

---

## 1. Product summary

A Spanish gift-discovery site. A visitor types a natural-language request
("regalos para el día del padre", "qué regalar a unos nuevos padres") and gets a curated,
visually appealing grid of gift cards. Every card exposes several Amazon CTAs
(**Ver precio**, **Ver reseñas**, **Ver detalles**). Revenue comes from Amazon Associates
(tag `zamami07-21`). The goal is maximum organic traffic and maximum outbound click-through.

**Non-goals for v1:** user accounts, saved lists, price display, A/B testing, blog/articles. All must remain *easy to add* — nothing in the design may block them. The site will only serve spain but the idea is that it should be easy to take the same base and apply to a different geography (like uk, de, with its own domain, different infra etc)

### Hard product rules
1. **Never display a price, ever.** No euro amounts, no "desde X€", no price history.
   Only coarse qualitative bands (`Económico` / `Medio` / `Premium`) used for filtering and labels.
   This keeps us clear of the Associates Operating Agreement rule that price data must come
   from the Product Advertising API and be <24h fresh.
2. **Amazon product images are hotlinked** from the Amazon CDN (accepted risk, explicit decision).
   Migrate to PA-API-sourced images once the Associates API is granted. Image URLs are stored,
   never the binaries.
3. **Affiliate disclosure** visible in the page footer. 
4. Every outbound Amazon link is `rel="nofollow sponsored"`, `target="_blank"`, and passes
   through our own click-tracking redirect.

---

## 2. Locked technical decisions

| Area | Decision |
|---|---|
| Language/runtime | Python 3.12 |
| Framework | Django 5.x, server-rendered templates + HTMX (no SPA — SEO is the product) |
| Dependency manager | **uv** (`pyproject.toml` + `uv.lock`) |
| Database | Heroku Postgres `essential-0`, **PostgreSQL 18.3** (already provisioned) |
| Extensions | `vector 0.8.1`, `pg_trgm`, `unaccent`, `btree_gin` — **all already installed** |
| Vector storage | `halfvec(512)` — see §6.2 for why |
| Queue / scheduler | **Custom**: Postgres `JobQueue` + `SELECT … FOR UPDATE SKIP LOCKED`, single worker dyno. No Redis, no Celery, no django-q. Rationale in §7.1 |
| Hosting | Heroku EU, app `regalosmejores`, stack `heroku-24`, dynos `web:1` + `worker:1` (Basic) |
| CDN/DNS | Cloudflare (proxied) in front of Heroku |
| Styling | Tailwind via the **standalone CLI binary** (no Node buildpack) |
| Admin/dashboards | **Django Admin only** — customised ModelAdmins, list filters, admin actions |
| Analytics | First-party `UserQuery` + `ClickEvent` tables + Google Search Console. **No GA4, no cookie banner** in v1 |
| LLM | OpenAI `gpt-5.6-luna` (default, overridable per call) |
| Embeddings | OpenAI `text-embedding-3-small`, `dimensions=512` |
| Keepa | Plan upgraded to **20 tokens/min** (max accumulation around 1000·? — read actual values from API responses) |
| Tests | **None.** No pytest, no unit tests. Validation is manual against production |

### Explicitly deferred (design must not block)
Blog/article pipeline (max 1 post/day, later) · display ads · PA-API

---

## 3. Glossary

Precise vocabulary — these words mean exactly this everywhere in code and docs.

| Term | Meaning |
|---|---|
| **Topic** | A gift idea/theme, e.g. "regalos para el día del padre". Equals a user's natural-language intent. Owns a public landing page. |
| **TopicAlias** | A near-duplicate phrasing that resolves to a Topic ("ideas regalo dia del padre"). Dedup mechanism. |
| **SearchTerm** | An **Amazon keyword** derived from a Topic ("botella térmica ciclismo"). Sent to Keepa. Never shown to users. |
| **Product** | One ASIN in one locale. |
| **ProductFacet** | An LLM-generated synthetic *gift query* that a Product answers, plus its embedding. **The unit of retrieval.** |
| **ProductTopicLink** | A precomputed, ranked Product↔Topic association. The content of a landing page. |
| **UserQuery** | One real visitor search, logged. |
| **QueryDemand** | Daily aggregate of normalised user queries. Drives what we build next. |
| **ClickEvent** | One outbound Amazon click. |

---

## 4. Data model

Django apps and their models. `id` is `BigAutoField` unless stated. All timestamps UTC.

### 4.1 `apps.catalog`

```python
class Product:
    asin                 CharField(10, db_index=True)
    domain_id            SmallIntegerField(default=9)        # Keepa: 9 = amazon.es
    # UNIQUE (asin, domain_id)

    # --- straight from Keepa ---
    title                TextField
    brand                CharField(null=True)
    manufacturer         CharField(null=True)
    model                CharField(null=True)
    parent_asin          CharField(10, null=True, db_index=True)
    product_group        CharField(null=True)
    root_category_id     BigIntegerField(null=True, db_index=True)
    category_ids         ArrayField(BigIntegerField)
    features             ArrayField(TextField)               # bullet points
    description_text     TextField(blank=True)               # HTML stripped
    image_urls           ArrayField(URLField)                # built from imagesCSV
    rating               DecimalField(2,1, null=True)        # csv[16]/10 -> 0.0..5.0
    review_count         IntegerField(null=True)             # csv[17]
    sales_rank           IntegerField(null=True)             # csv[3]
    is_adult             BooleanField(default=False)
    listed_since         DateTimeField(null=True)

    # --- derived, never raw prices ---
    price_band           CharField(choices=ECONOMICO|MEDIO|PREMIUM, null=True)
    price_band_at        DateTimeField(null=True)
    sales_rank_pct       FloatField(null=True)               # 0=best .. 1=worst in root cat
    quality_score        FloatField(default=0)               # §6.4
    ctr_score            FloatField(default=0)               # learned, §6.4
    variation_group_key  CharField(db_index=True)            # parent_asin or asin

    # --- lifecycle ---
    keepa_fetched_at     DateTimeField(db_index=True)        # REQUIRED: last Keepa fetch
    keepa_payload_hash   CharField(64)
    first_seen_at        DateTimeField(auto_now_add=True)
    is_active            BooleanField(default=True)
    availability_note    CharField(blank=True)
    is_blocked           BooleanField(default=False)         # manual kill switch
    block_reason         CharField(blank=True)

    enrichment_status    CharField(PENDING|RUNNING|DONE|FAILED|SKIPPED, db_index=True)
    enriched_at          DateTimeField(null=True)
    enrichment_model     CharField(blank=True)

class ProductFacet:
    product         FK(Product, related_name="facets", on_delete=CASCADE)
    text            TextField                    # synthetic gift query, Spanish
    facet_type      CharField(SYNTHETIC_QUERY|GIFT_ANGLE|SUMMARY)
    embedding       HalfVectorField(512, null=True)
    embedding_model CharField
    embedding_version SmallIntegerField(default=1)
    occasions       ArrayField(CharField)        # controlled vocab
    recipients      ArrayField(CharField)
    interests       ArrayField(CharField)
    weight          FloatField(default=1.0)
    created_at      DateTimeField(auto_now_add=True)
    # HNSW index on embedding (vector_cosine_ops)
    # GIN index on to_tsvector('spanish', text)
```

### 4.2 `apps.topics`

```python
class Topic:
    slug             SlugField(unique=True)          # "regalos-para-el-dia-del-padre"
    title            CharField                       # H1
    meta_title       CharField(blank=True)
    meta_description CharField(blank=True)
    intro_html       TextField(blank=True)           # short, human-reviewed
    canonical_text   TextField                       # normalised query form
    embedding        HalfVectorField(512, null=True)
    embedding_version SmallIntegerField(default=1)

    kind             CharField(OCCASION|RECIPIENT|INTEREST|BUDGET|HYBRID)
    occasions        ArrayField(CharField)
    recipients       ArrayField(CharField)
    interests        ArrayField(CharField)
    price_band       CharField(null=True)

    source           CharField(MANUAL|LLM|MINED_QUERY|MINED_GSC)
    status           CharField(DRAFT|ACTIVE|ARCHIVED, db_index=True)
    priority         SmallIntegerField(default=50)   # pipeline ordering, higher = sooner

    # quality gate (§9.2)
    linked_count     IntegerField(default=0)
    distinct_categories IntegerField(default=0)
    distinct_brands  IntegerField(default=0)
    quality_score    FloatField(default=0)
    is_indexable     BooleanField(default=False)     # computed; controls robots meta + sitemap
    human_reviewed   BooleanField(default=False)

    merged_into      FK("self", null=True)           # near-duplicate resolution
    seasonal_start   DateField(null=True)            # publish/boost window
    seasonal_end     DateField(null=True)
    last_curated_at  DateTimeField(null=True, db_index=True)
    created_at / updated_at

class TopicAlias:
    topic      FK(Topic, related_name="aliases")
    text       TextField
    normalized CharField(db_index=True)
    embedding  HalfVectorField(512, null=True)
    source     CharField(MANUAL|MINED_QUERY|LLM)
    hits       IntegerField(default=0)
    # UNIQUE (normalized)

class SearchTerm:
    topic          FK(Topic, related_name="search_terms")
    text           CharField                          # Amazon keyword
    normalized     CharField(db_index=True)
    status         CharField(PENDING|RUNNING|DONE|FAILED|EXHAUSTED, db_index=True)
    priority       SmallIntegerField(default=50)
    run_count      IntegerField(default=0)
    products_found IntegerField(default=0)
    keepa_tokens   IntegerField(default=0)
    last_run_at    DateTimeField(null=True)
    # UNIQUE (topic, normalized)

class ProductTopicLink:
    topic       FK(Topic, related_name="links")
    product     FK(Product, related_name="topic_links")
    score       FloatField(db_index=True)
    rank        SmallIntegerField
    source      CharField(AUTO|MANUAL|PINNED)
    is_pinned   BooleanField(default=False)     # survives recompute
    is_excluded BooleanField(default=False)     # survives recompute
    computed_at DateTimeField
    # UNIQUE (topic, product)
```

### 4.3 `apps.search`

```python
class UserQuery:
    raw_text         TextField
    normalized       CharField(db_index=True)      # lowercased, unaccented, collapsed
    query_hash       CharField(32, db_index=True)  # md5(normalized + slots)
    embedding        HalfVectorField(512, null=True)
    mode             CharField(SIMPLE|ADVANCED, db_index=True)
    slots            JSONField(default=dict)       # advanced: recipient/occasion/interests/budget/age
    matched_topic    FK(Topic, null=True, on_delete=SET_NULL)
    match_similarity FloatField(null=True)
    result_count     IntegerField(default=0)
    is_zero_result   BooleanField(default=False, db_index=True)
    served_from_cache BooleanField(default=False)
    latency_ms       IntegerField(null=True)
    session_key      CharField(32, db_index=True)  # salted hash, no PII
    referrer_host    CharField(blank=True)
    created_at       DateTimeField(auto_now_add=True, db_index=True)

class QueryDemand:                                  # rolled up nightly
    normalized       CharField(db_index=True)
    day              DateField(db_index=True)
    hits             IntegerField(default=0)
    zero_results     IntegerField(default=0)
    clicks           IntegerField(default=0)
    avg_similarity   FloatField(null=True)
    matched_topic    FK(Topic, null=True)
    promoted_topic   FK(Topic, null=True)           # set when turned into a Topic
    # UNIQUE (normalized, day)

class SearchResultCache:
    query_hash   CharField(32, unique=True)
    payload      JSONField                          # ordered product ids + metadata
    hits         IntegerField(default=0)
    expires_at   DateTimeField(db_index=True)

class RankingConfig:                                # singleton, editable in Admin
    w_semantic, w_lexical, w_quality, w_ctr, w_freshness   FloatField
    topic_match_threshold        FloatField(default=0.90)
    min_facet_similarity         FloatField(default=0.55)
    candidate_pool_size          IntegerField(default=200)
    results_per_page             IntegerField(default=24)
    max_per_brand                SmallIntegerField(default=2)
    max_per_category             SmallIntegerField(default=3)
    cache_ttl_hours              IntegerField(default=24)
```

### 4.4 `apps.tracking`

```python
class ClickEvent:
    product       FK(Product, on_delete=PROTECT)
    topic         FK(Topic, null=True, on_delete=SET_NULL)
    user_query    FK(UserQuery, null=True, on_delete=SET_NULL)
    placement     CharField(SIMPLE_SEARCH|ADVANCED_SEARCH|TOPIC_PAGE|HOME|RELATED|ARTICLE, db_index=True)
    button        CharField(SEE_PRICE|SEE_REVIEWS|DETAILS|IMAGE|TITLE, db_index=True)
    position      SmallIntegerField(null=True)      # rank in the list
    page_path     CharField
    referrer_host CharField(blank=True)
    session_key   CharField(32, db_index=True)
    ascsubtag     CharField(blank=True)             # subtag sent to Amazon
    created_at    DateTimeField(auto_now_add=True, db_index=True)

class PageView:                     # minimal, first-party, cookieless
    path          CharField(db_index=True)
    topic         FK(Topic, null=True, on_delete=SET_NULL)
    session_key   CharField(32, db_index=True)
    referrer_host CharField(blank=True)
    is_bot        BooleanField(default=False)
    created_at    DateTimeField(auto_now_add=True, db_index=True)
```

### 4.5 `apps.pipelines`

```python
class PipelineSchedule:
    pipeline_key  CharField(unique=True)        # "generate_topics"
    enabled       BooleanField(default=True)
    cron          CharField                     # 5-field cron, UTC
    max_per_run   IntegerField(default=10)
    options       JSONField(default=dict)
    next_run_at   DateTimeField(db_index=True)
    last_run_at   DateTimeField(null=True)
    consecutive_failures IntegerField(default=0)

class JobQueue:
    pipeline_key  CharField(db_index=True)
    payload       JSONField(default=dict)
    priority      SmallIntegerField(default=50, db_index=True)  # lower = sooner
    status        CharField(QUEUED|RUNNING|DONE|FAILED|DEFERRED, db_index=True)
    available_at  DateTimeField(db_index=True)
    attempts      SmallIntegerField(default=0)
    max_attempts  SmallIntegerField(default=3)
    locked_by     CharField(blank=True)
    locked_at     DateTimeField(null=True)
    dedupe_key    CharField(blank=True, db_index=True)          # optional uniqueness
    last_error    TextField(blank=True)
    created_at / updated_at

class PipelineRun:
    pipeline_key  CharField(db_index=True)
    job           FK(JobQueue, null=True, on_delete=SET_NULL)
    status        CharField(RUNNING|SUCCESS|FAILED|SKIPPED, db_index=True)
    skip_reason   CharField(blank=True)         # e.g. "keepa_budget_exhausted"
    started_at / finished_at / duration_ms
    items_in / items_created / items_updated / items_failed   IntegerField
    keepa_tokens_used  IntegerField(default=0)
    llm_cost_usd       DecimalField(10,6, default=0)
    error         TextField(blank=True)
    context       JSONField(default=dict)

class PipelineStepRun:
    run        FK(PipelineRun, related_name="steps")
    name       CharField
    status / started_at / finished_at / duration_ms / error / context

class KeepaTokenLedger:                          # reconciled from API responses
    at              DateTimeField(auto_now_add=True, db_index=True)
    endpoint        CharField
    tokens_consumed IntegerField
    tokens_left     IntegerField
    refill_in_ms    IntegerField(null=True)
    refill_rate     IntegerField(null=True)
    pipeline_run    FK(PipelineRun, null=True, on_delete=SET_NULL)

class LLMCall:
    at              DateTimeField(auto_now_add=True, db_index=True)
    purpose         CharField(db_index=True)     # "enrich_product", "decompose_topic", ...
    model           CharField
    prompt_tokens / completion_tokens / total_tokens  IntegerField
    cost_usd        DecimalField(10,6)
    latency_ms      IntegerField
    success         BooleanField(default=True)
    error           TextField(blank=True)
    pipeline_run    FK(PipelineRun, null=True, on_delete=SET_NULL)

class NotificationLog:                           # Telegram throttling
    key        CharField(db_index=True)
    level      CharField(INFO|WARNING|ERROR|CRITICAL)
    message    TextField
    sent_at    DateTimeField(auto_now_add=True, db_index=True)
    suppressed BooleanField(default=False)
```

---

## 5. Clients (`apps.clients`)

All clients are thin, typed, and dependency-injected. No business logic inside them.

### 5.1 `KeepaClient`
- Endpoints used: `product` (hydrate ASINs), `query` (Product Finder), `search` (keyword), `deal` (optional later).
- **Every response carries `tokensLeft`, `tokensConsumed`, `refillIn`, `refillRate`.** The client
  writes a `KeepaTokenLedger` row on every call and updates the in-DB budget state from those
  values. This makes the budget **self-calibrating** — we never hardcode token costs.
- Batches up to 100 ASINs per `product` call (cheapest per-ASIN path).
- Retries with exponential backoff on 429/5xx; raises `KeepaBudgetExhausted` when `tokensLeft` is
  below the configured reserve.
- Maps Keepa's CSV/stat arrays into a normalised `KeepaProduct` dataclass so the rest of the
  codebase never touches raw Keepa index numbers.

### 5.2 `LLMClient`
```python
client.complete(system, user, *, model=None, temperature=None, top_p=None,
                max_tokens=None, json_schema=None, purpose="...") -> LLMResponse
client.embed(texts: list[str], *, model=None, dimensions=512) -> list[list[float]]
```
- Model registry: `{name: (provider, input_$/1M, output_$/1M, supports_temperature, context)}`.
  Unsupported params are silently dropped (some models reject `temperature`).
- Default model `gpt-5.6-luna`, default embedding `text-embedding-3-small` @ 512 dims.

  | Model | Input | Cached input | Output |
  |---|---|---|---|
  | `gpt-6-astra` | $10.00 | $1.00 | $50.00 |
  | `gpt-5.6-sol` | $4.00 | $0.40 | $20.00 |
  | `gpt-5.6-terra` | $2.00 | $0.20 | $12.00 |
  | **`gpt-5.6-luna`** (default) | **$0.20** | **$0.02** | **$1.20** |
  | `text-embedding-3-small` | $0.02 | — | — |

  USD per 1M tokens, short-context tier (our prompts are one product or one topic at a time).
  Long context is roughly 2x across the board.

- Always uses **structured outputs / JSON schema** for pipeline calls — never free-text parsing.
- Writes an `LLMCall` row per call. Enforces a daily spend cap (`LLM_DAILY_BUDGET_USD`);
  raises `LLMBudgetExhausted` past it and fires a Telegram CRITICAL.

### 5.3 `TelegramClient`
```python
notify(key, message, level=INFO, throttle_minutes=60)
```
- Posts to `TELEGRAM_CHANNEL_ID` with `TELEGRAM_BOT_TOKEN`.
- Deduped/throttled per `key` via `NotificationLog` so a failing pipeline can't spam the channel.
- Never raises into calling code — notification failures are logged only.

**Events sent:**

| Level | Event |
|---|---|
| CRITICAL | LLM daily budget exceeded · worker dead-man (no heartbeat >15 min) · DB >80% of plan |
| ERROR | Pipeline failed `max_attempts` · Keepa auth failure |
| WARNING | Zero-result rate >20% over 1h · Keepa tokens starved >30 min · topic dropped out of index |
| INFO | Daily digest 08:00 CET: products added, topics curated, searches, clicks, top 5 queries, spend |

---

## 6. Search architecture

**Principle: all generative work happens offline in pipelines. The online path is
`embed → vector search → cheap ranking`. No chat-completion call ever happens on a user request.**

Embedding a query costs ~€0.0000002 and ~40 ms — that is the only paid operation online.

### 6.1 The core idea: query↔query matching
Embedding a user query against a product *title* matches badly — different semantic spaces.
Instead, the enrichment pipeline makes the LLM generate **6–10 synthetic gift queries** that each
product answers, and we embed *those* (`ProductFacet`). At request time we compare a query to
other queries. This single choice is the biggest quality lever in the system.

### 6.2 Embedding spec
`text-embedding-3-small` with `dimensions=512`, stored as **`halfvec(512)`** (pgvector 0.8.1).

Why: `essential-0` gives 1 GB. A `vector(1536)` is ~6 KB; 50k products × 6 facets = 300k vectors
= **1.8 GB → blows the plan.** `halfvec(512)` is ~1 KB → **300 MB**, comfortable, with ~1–2 %
retrieval quality loss. Every embedding row stores `embedding_model` + `embedding_version` so a
future re-embed is a background migration, not a rewrite.

Index: `CREATE INDEX … USING hnsw (embedding halfvec_cosine_ops) WITH (m=16, ef_construction=64)`.

### 6.3 Request pipeline

```
user query
 │
 ├─ L0  normalise (lowercase, unaccent, collapse whitespace, strip stopwords)
 │      → query_hash → SearchResultCache lookup        HIT: ~10 ms, €0
 │
 ├─ L1  embed query (512 dims)                          ~40 ms
 │
 ├─ L2  nearest Topic / TopicAlias by cosine
 │      sim ≥ topic_match_threshold (0.90)
 │        → SIMPLE mode: 302 redirect to /regalos/<slug>/   (SEO value concentrates there)
 │        → ADVANCED mode: use the Topic's links as the seed pool, then filter by slots
 │
 ├─ L3  hybrid retrieval over ProductFacet → candidate pool (200)
 │        a) HNSW cosine ANN on embedding          (semantic)
 │        b) Postgres FTS, 'spanish' config        (brands, exact nouns)
 │        c) pg_trgm similarity                    (typos)
 │      fused with Reciprocal Rank Fusion (k=60), collapsed to distinct products
 │
 ├─ L4  filter → score → diversify  (pure SQL/Python, no model)
 │
 └─ L5  render + async write-behind: UserQuery row, cache fill,
        enqueue topic-candidate if weak/zero result
```

**Latency budget (p50 target < 200 ms):** cache 10 ms · embed 40 ms · ANN 15 ms · FTS 10 ms ·
rank 10 ms · render 30 ms.

**Cost at 10k searches/day:** ≈ €0.06/month.

### 6.4 Filtering, scoring, diversity

**Hard filters:** `is_active`, `not is_blocked`, `not is_adult`, `enrichment_status=DONE`,
similarity ≥ `min_facet_similarity`, plus slot filters in advanced mode (price band, category).

**Score** (weights live in `RankingConfig`, tunable from Admin without deploy):
```
quality  = 0.5·(rating/5) + 0.3·(ln(1+review_count)/ln(1+5000)) + 0.2·(1 − sales_rank_pct)
freshness= decay on keepa_fetched_at (1.0 fresh → 0.6 at 90 days)
ctr      = smoothed CTR of the product (Bayesian prior, see §8.3)

final = w_semantic·sim + w_lexical·rrf_lex + w_quality·quality
      + w_ctr·ctr + w_freshness·freshness
defaults: .45 / .15 / .20 / .10 / .10
```

**Diversity (this is what makes the grid look good and stops Amazon variation flooding):**
applied greedily in rank order —
1. **max 1 product per `variation_group_key`** (kills the 40-identical-t-shirts problem),
2. max `max_per_brand` (2) per brand,
3. max `max_per_category` (3) per root category,
4. spread price bands — aim for a mix rather than 24 Premium items,
5. MMR penalty on facet-embedding similarity to already-selected items.

### 6.5 Advanced search — still zero LLM calls
Fields (all optional): **Para quién** · **Ocasión** · **Aficiones/intereses** (multi) ·
**Presupuesto** (band) · **Edad** (range).

The first four are **chips/autocomplete backed by a controlled vocabulary** (the same vocabulary
used to tag `ProductFacet`), plus a free-text box. Slots are composed into a synthetic Spanish
sentence, embedded, and run through the same L1–L4 pipeline with slot values as hard filters.
No LLM. Free-text is simply concatenated into the sentence.

If we later find free-text needs slot extraction, it goes behind a slot-hash cache and a
nano-model call (~€0.0002) — but **not in v1**.

### 6.6 Self-healing / remapping
The user asked for a system that stays correct as products and queries grow:

- `curate_topics` re-runs full retrieval for a Topic and **rewrites `ProductTopicLink`**, preserving
  rows where `is_pinned` or `is_excluded`. Triggered when: topic is new, `last_curated_at` older
  than 14 days, ≥50 new products entered a matching category, or weights changed.
- New products therefore enter existing topics automatically; new topics pull from the whole pool.
- Products that go inactive are unlinked, and topics dropping below the quality gate lose
  `is_indexable` automatically (and fire a Telegram WARNING).
- **Near-duplicate topics**: nightly job compares Topic embeddings; pairs with cosine ≥ 0.95 are
  flagged, the lower-priority one gets `merged_into` set, its slug 301-redirects, and its text
  becomes a `TopicAlias`. Manual confirmation required in Admin before merging (avoids cascading
  bad merges).
- **Near-duplicate user queries** are absorbed by `normalized` + embedding match into `TopicAlias`,
  so `QueryDemand` reflects real distinct demand rather than phrasing noise.

---

## 7. Pipelines

### 7.1 Engine

**Why custom rather than Celery/django-q:** concurrency is 1, jobs are short, and we already need
`PipelineRun`/`PipelineStepRun` for the dashboard. Adding a broker would duplicate run tracking,
add a Redis add-on cost, and add failure modes. A ~200-line Postgres queue with
`FOR UPDATE SKIP LOCKED` is resilient, free, fully visible in Django Admin, and trivially
swappable later.

```
apps/pipelines/
  base.py       # Pipeline ABC: key, description, default_cron, requires_keepa, requires_llm,
                #   estimated_keepa_tokens, run(ctx) -> PipelineResult; step() context manager
  registry.py   # decorator-based auto-discovery
  queue.py      # enqueue(), claim_next(), complete(), defer(), fail()
  scheduler.py  # cron -> enqueue due PipelineSchedule rows
  budget.py     # BudgetGuard (§7.3)
  worker.py     # management command `run_worker`
```

`manage.py run_worker` loop (single worker dyno):
1. Write heartbeat.
2. Tick scheduler (enqueue due schedules, idempotent via `dedupe_key`).
3. Claim one job (`ORDER BY priority, available_at` + `FOR UPDATE SKIP LOCKED`).
4. Ask `BudgetGuard.check(pipeline)`. If refused → `DEFERRED` with `available_at = now + backoff`,
   record `PipelineRun(status=SKIPPED, skip_reason=…)`. **No LLM tokens are burned.**
5. Run inside `PipelineRun`; steps recorded as `PipelineStepRun`.
6. On exception: increment attempts, exponential backoff, `FAILED` after `max_attempts`
   → Telegram ERROR.
7. Sleep 2 s if the queue was empty.

`manage.py run_pipeline <key> [--payload json]` runs any pipeline synchronously for manual testing.

### 7.2 Pipeline catalogue

| key | Purpose | Cron (UTC) | Keepa | LLM |
|---|---|---|---|---|
| `seed_products` | **Product Finder** harvest: structured filters → bulk candidate ASINs | `0 */6 * * *` | yes | no |
| `hydrate_products` | Fetch full Keepa data for ASINs with no/stale `keepa_fetched_at`, batched ×100 | `*/5 * * * *` | **yes (main consumer)** | no |
| `enrich_products` | Generate `ProductFacet` synthetic queries + tags, then embed | `*/10 * * * *` | no | yes |
| `generate_topics` | Propose new Topics: gaps vs existing coverage + seasonal calendar | `0 3 * * *` | no | yes |
| `mine_query_demand` | Roll `UserQuery` → `QueryDemand`; promote high-demand unmatched queries to Topic candidates | `30 2 * * *` | no | no |
| `decompose_topic` | Topic → Amazon `SearchTerm`s | on demand / `0 4 * * *` | no | yes |
| `run_search_terms` | Keepa keyword search for pending `SearchTerm`s → queue ASINs for hydration | `*/15 * * * *` | yes | no |
| `curate_topics` | Recompute `ProductTopicLink` + quality gate + `is_indexable` | `0 5 * * *` | no | no |
| `dedupe_topics` | Flag near-duplicate topics for merge | `0 6 * * 1` | no | no |
| `refresh_products` | Re-fetch products older than 30 days; deactivate dead ASINs | `0 */2 * * *` | yes | no |
| `recompute_ctr` | Rebuild `ctr_score` from `ClickEvent` | `0 1 * * *` | no | no |
| `daily_digest` | Telegram summary | `0 6 * * *` | no | no |

**Priority order when the queue is contended** (lower number = runs first):
`hydrate_products (10)` → `enrich_products (20)` → `run_search_terms (30)` →
`curate_topics (40)` → `decompose_topic (50)` → `generate_topics (60)` → housekeeping (70+).

### 7.3 `BudgetGuard`

With 20 tokens/min budget pressure is low, but the gate stays — it prevents wasting LLM money on
work that can't be consumed downstream.

```python
check(pipeline) -> Allow | Defer(reason, retry_after)
```
Refuses when:
- `pipeline.requires_keepa` and `tokens_left − KEEPA_TOKEN_RESERVE < pipeline.estimated_keepa_tokens`
  → defer until `refill_in`.
- `pipeline.requires_llm` **and** the hydration backlog exceeds `MAX_HYDRATION_BACKLOG` (default 5 000)
  → don't generate more work we can't ingest.
- Daily LLM spend ≥ `LLM_DAILY_BUDGET_USD`.
- Global `PIPELINES_ENABLED` flag is off (Admin kill switch).

Token state is read from the last `KeepaTokenLedger` row, i.e. from Keepa's own response fields —
never estimated.

### 7.4 Seeding strategy (quality-filtered, conversion-oriented)

Keyword search is an expensive way to fill a database. **Product Finder** returns thousands of
ASINs for roughly the cost of one keyword search. Strategy: **harvest in bulk, hydrate in a trickle.**

`seed_products` filters (configurable per category, stored in `PipelineSchedule.options`):
- rating ≥ 4.2
- review count ≥ 150 (social proof drives clicks and conversion)
- sales rank within the top ~20 % of its root category
- listed ≥ 6 months ago (filters dropship junk and review manipulation)
- not adult, has images, has a brand
- excluded root categories: groceries, digital/eBooks, software subscriptions, medical, apparel-by-size
- price band distribution targets so we don't end up with only cheap items

The same filters are re-applied as a hard gate at `curate_topics` time, so raising standards later
cleans the site retroactively.

---

## 8. Public site & SEO

### 8.1 URL map

| URL | Content | Indexable |
|---|---|---|
| `/` | Hero + simple search + featured topics + seasonal block | **yes** |
| `/regalos/<slug>/` | **Topic landing page** — the SEO asset | **only if quality gate passes** |
| `/regalos/ocasiones/` `/regalos/para-quien/` `/regalos/aficiones/` `/regalos/presupuesto/` | Hub pages listing topics | **yes** |
| `/buscar/?q=…` | Simple search results | `noindex, follow` |
| `/buscador-avanzado/` | The advanced search **tool** page | **yes** (real utility, targets "buscador de regalos") |
| `/buscador-avanzado/resultados/` | Advanced results (POST/session) | `noindex, nofollow` |
| `/go/<click_id>/` | Affiliate redirect | blocked in `robots.txt` |
| `/aviso-legal/` `/privacidad/` `/cookies/` `/afiliados/` `/contacto/` | Legal | `noindex` except `/afiliados/` |
| `/healthz/` | Deploy health check, no DB access | blocked in `robots.txt` |
| `/sitemap.xml`, `/robots.txt` | Generated | — |

**Answering the AI-detection concern directly:** the risk is *indexing an unbounded long tail of
thin, machine-made pages*. So — the long tail is **never indexed**. `/buscar/` is always `noindex`;
when a simple search matches a Topic strongly it **302s to the curated topic page**, concentrating
all SEO value on a finite, quality-gated, partly human-reviewed set. Advanced search is a tool page,
not a content page, and its results are never indexable. This gives the traffic upside of
query-driven URLs without the scaled-content-abuse exposure.

### 8.2 Topic landing page structure
H1 · 2–4 sentence intro (human-reviewed) · affiliate disclosure · filter chips
(price band / recipient) · **grid of product cards** · "regalos relacionados" internal links to
sibling topics · FAQ block (3 Q&A) when available.

### 8.3 Product card component
One reusable component, variant-driven (`grid`, `list`, `compact`, `featured`) — never copy-pasted.
Contains: image, title (truncated), brand, star rating + review count, price-band pill,
one-line "por qué es buen regalo" (from `ProductFacet`), and the CTA row:

- **Ver precio** → `/go/<id>/?b=see_price`
- **Ver reseñas** → `/go/<id>/?b=see_reviews` → `https://www.amazon.es/product-reviews/<ASIN>/`
- **Ver detalles** → `/go/<id>/?b=details` → `https://www.amazon.es/dp/<ASIN>`

The redirect view writes the `ClickEvent`, builds the affiliate URL
`?tag=zamami07-21&linkCode=ll1&language=es_ES&ascsubtag=rm-<placement>-<topic_id>-<click_id>`
and issues a 302. The `ascsubtag` lets Amazon's Associates report attribute revenue back to a
specific page/placement — this is the feedback loop that later feeds `ctr_score`.

### 8.4 SEO checklist (build into v1)
- Server-rendered HTML, no client-side rendering of content.
- `sitemap.xml` via `django.contrib.sitemaps`, **only `is_indexable` topics**, with `lastmod`.
- `robots.txt`: allow all, `Disallow: /go/`, `Disallow: /buscar/`, `Disallow: /admin/`, sitemap ref.
- Per-page `<title>`, meta description, canonical, OG/Twitter cards.
- JSON-LD: `ItemList` on topic pages (no `Product` markup — we show no price), `WebSite` +
  `SearchAction` on home, `BreadcrumbList` everywhere, `FAQPage` where FAQs exist.
- `hreflang` scaffolding present but single-locale for now.
- **Slugs are immutable once `is_indexable` has been true** — slug changes create a 301 record.
- Internal linking: every topic links to 6 siblings; hubs link to all their topics.
- Core Web Vitals: lazy-load images below the fold, `width`/`height` on all images, preconnect to
  the Amazon image CDN, Tailwind CSS purged, no blocking JS (HTMX only).
- Seasonal topics published/boosted 6–8 weeks before the date via `seasonal_start`.

### 8.5 Pre-blog traffic plan (answers "what brings traffic before articles exist")
1. **150–300 curated topic pages** across four axes: occasion (Navidad, Reyes, San Valentín,
   Día del Padre/Madre, cumpleaños, aniversario, boda, comunión, amigo invisible, jubilación),
   recipient (padre, madre, novio, novia, abuelos, hermano, cuñada, profesor, jefe, compañero,
   adolescente, niños por edad, recién nacidos, nuevos padres), interest (ciclismo, cocina, gaming,
   running, jardinería, café, lectura, viajes, música, fotografía, senderismo, cerveza, mascotas),
   budget (económicos, calidad-precio, especiales).
2. **Hub pages** for internal link equity.
3. **The tool page** (`/buscador-avanzado/`) targets "buscador de regalos", "ideas de regalos",
   "qué regalar".
4. **Amigo invisible / Black Friday / Navidad** are the volume peaks — have those live by October.
5. Product detail pages are **noindex** in v1 (thin + duplicate risk).
6. Register in Google Search Console day one; submit the sitemap; monitor which queries surface and
   feed them back into `generate_topics`.

---

## 9. Quality gates & risk controls

### 9.1 Legal pages (required before launch)
Aviso legal · Política de privacidad (mentions first-party analytics, no third-party cookies) ·
Política de cookies (we set only a session cookie → **no consent banner required**, which is a real
UX and CTR advantage) · Página de afiliados with the exact Amazon disclosure text.

### 9.2 Topic quality gate (`is_indexable = True` requires all)
- ≥ 12 linked products with `score ≥ 0.55`
- ≥ 4 distinct root categories represented
- ≥ 3 distinct brands
- `intro_html` present and non-boilerplate
- `human_reviewed = True` **for the first 100 topics** (mandatory), sampled thereafter
- not `merged_into`

Failing topics stay live for users but are `noindex` and excluded from the sitemap.

### 9.3 Publication rate
No mass publication. Topics move `DRAFT → ACTIVE` in batches of ≤ 10/day, at irregular times.
This is enforced by `curate_topics` (`max_per_run`), not by discipline.

---

## 10. Infrastructure & scalability plan

### Current state (verified 2026-09-13)
App `regalosmejores` (EU, `heroku-24`), `web:1`, Postgres `essential-0` (PG 18.3, pgvector 0.8.1,
DB emptied and ready), Telegram bot configured, no Redis,
no custom domain attached, pipeline `regalosmejores - production`.

### Stage 0 — Foundation (at first deploy, not during local development)
1. `heroku config:set` the missing vars — exact command in §13.A.
2. Add the `worker` dyno type via `Procfile`; `heroku ps:scale web=1:basic worker=1:basic`.
3. `release: python manage.py migrate --noinput` in the `Procfile`.
4. GitHub Actions workflow — already rewritten for Django + uv, lint + `check --deploy` + deploy
   with health check and automatic rollback. **No tests.**
5. Optional: Sentry (free tier) + Heroku Papertrail (free tier) for logs.

### Stage 1 — Go live
Full step-by-step (domain, Cloudflare, Search Console) is in **§13.B**. Canonical host is
`www.regalosmejores.com`; the apex 301-redirects to it at the Cloudflare layer.

### Stage 2 — Growth triggers (act when the metric is hit, not before)
| Trigger | Action |
|---|---|
| DB > 700 MB (70 % of `essential-0`) | Upgrade to `essential-1` (10 GB) |
| Postgres connections near 20 | Reduce `CONN_MAX_AGE`, cap gunicorn workers at 2 |
| > 60k products, ANN p95 > 60 ms | Upgrade to `standard-0` (more RAM → HNSW index stays cached); raise `hnsw.ef_search` |
| Hydration backlog persistently > 10k | Raise Keepa plan above 20 tokens/min |
| > 20k sessions/mo | Evaluate display ads (§12) |
| Worker queue depth > 500 sustained | Add `worker=2` and shard by `pipeline_key` |
| Web p95 > 500 ms | `web=2:standard-1x` |

### Sizing notes
- `essential-0` = 1 GB storage, 20 connections. `halfvec(512)` keeps 50k products × 6 facets at
  ~300 MB — comfortable.
- Gunicorn: `--workers 2 --threads 4 --timeout 60`; `CONN_MAX_AGE=60`.
- `ClickEvent`/`PageView`/`UserQuery` are the fastest-growing tables → monthly pruning job keeps
  raw rows 180 days, aggregates forever.

---

## 11. Configuration

### Environment variables

Vars marked **set me** on Heroku are intentionally deferred — see §13.A for the single
`heroku config:set` command. Vars marked **add** in `.env` are needed for local development and
are created in Phase 0.

| Var | Local `.env` | Heroku | Notes |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | ❌ add | ❌ **set me** | generate 50 chars |
| `DJANGO_DEBUG` | `true` | `false` ✅ (`DEBUG`) | rename to `DJANGO_DEBUG` for clarity |
| `DJANGO_ALLOWED_HOSTS` | ❌ add | ❌ **set me** | `regalosmejores.com,www.regalosmejores.com,regalosmejores-*.herokuapp.com` |
| `SITE_URL` | ❌ add | ❌ **set me** | `https://www.regalosmejores.com` |
| `DATABASE_URL` | ✅ (prod URL) | ✅ | **local dev points at PROD** — accepted decision |
| `OPENAI_API_KEY` | ✅ | ❌ **set me** | |
| `OPENAI_DEFAULT_MODEL` | ✅ `gpt-5.6-luna` | ❌ **set me** | |
| `OPENAI_EMBEDDING_MODEL` | ✅ `text-embedding-3-small` | ❌ **set me** | |
| `OPENAI_EMBEDDING_DIMENSIONS` | ❌ add `512` | ❌ **set me** | |
| `OPENAI_ORGANIZATION` / `OPENAI_PROJECT` | ✅ | optional | |
| `KEEPA_API_KEY` | ✅ | ❌ **set me** | |
| `KEEPA_DOMAIN_ID` | ✅ `9` | ❌ **set me** | 9 = amazon.es |
| `KEEPA_TOKEN_RESERVE` | ✅ `20` | ❌ **set me** | |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHANNEL_ID` | commented out — **uncomment** | ✅ | |
| `AMAZON_AFFILIATE_TAG` | ✅ `zamami07-21` | ✅ | |
| `AMAZON_MARKETPLACE_HOST` | ❌ add `www.amazon.es` | ❌ **set me** | |
| `LLM_DAILY_BUDGET_USD` | ❌ add `5` | ❌ **set me** | |
| `MAX_HYDRATION_BACKLOG` | ❌ add `5000` | ❌ **set me** | |
| `PIPELINES_ENABLED` | ❌ add `true` | ❌ **set me** | kill switch |
| `SESSION_SALT` | ❌ add | ❌ **set me** | salts `session_key` hashes |
| `SENTRY_DSN` | optional | optional | |
| AWS S3 vars | ❌ | ✅ | editorial media only |

### Repository layout
```
manage.py  Procfile  .python-version  pyproject.toml  uv.lock  .env.example  .gitignore
.github/workflows/heroku-deploy.yml
scripts/    build_css.ps1  build_css.sh          # Tailwind v4 standalone CLI (no Node)
config/
  settings/{base,dev,prod,ci}.py   urls.py   wsgi.py
apps/
  clients/    keepa.py  llm.py  telegram.py  exceptions.py
  catalog/    models  admin  services/{ingest,enrich,scoring}.py
  topics/     models  admin  services/{curation,dedupe,quality_gate}.py
  search/     models  admin  services/{normalize,retrieval,ranking,diversity}.py  views.py
  pipelines/  base  registry  queue  scheduler  budget  worker  models  admin  pipelines/*.py
  tracking/   models  admin  views.py (go redirect)  middleware.py
  web/        views  urls  context_processors  templatetags
  seo/        sitemaps.py  robots.py  schema.py
static/
  src/input.css            # Tailwind source (v4 CSS-first config, no tailwind.config.js)
  css/site.css             # compiled, COMMITTED to git, CI fails if stale
  js/htmx.min.js           # self-hosted, no CDN
templates/   base.html  web/  components/
docs/        project_blueprint.md  RUNBOOK.md
```

---

## 12. Monetization beyond Amazon (answering the open question)

| Option | Entry barrier | Notes |
|---|---|---|
| **Google AdSense** | None (needs original content + legal pages; approval days–weeks) | Lowest RPM (~€2–8 for ES gift traffic). Safe baseline. |
| **Ezoic** | No traffic minimum | Higher RPM than AdSense but a heavy script — real Core Web Vitals cost, which hurts SEO while you're young. |
| **Mediavine Journey** | ~10k sessions/mo | Much better RPM, lighter than Ezoic. |
| **Mediavine / Raptive (full)** | 50k / 100k sessions/mo | The real money tier. |
| **Awin / Tradedoubler ES** | Manual approval per advertiser | PcComponentes, El Corte Inglés, Fnac, Decathlon — commissions often 2–4× Amazon's 3–4 %. **Best diversification play.** |

**Recommendation:** no display ads at launch. They cannibalise affiliate CTR (your only revenue
right now), slow the site, and add a consent-banner requirement that we currently avoid entirely.
Revisit at ~20–30k sessions/month, starting with Mediavine Journey or AdSense, and measure against
affiliate revenue per session. Add a second affiliate network (Awin) before adding ads.

---

## 13. Manual actions (owner — not code)

Everything below is deliberately **outside** the implementation phases. Development happens
locally against the production Heroku Postgres; nothing here blocks Phases 0–9.

### Already resolved
Telegram bot is an admin of channel `-1002682055833` ✅ · GitHub secrets `HEROKU_API_KEY`,
`HEROKU_APP_NAME`, `HEROKU_EMAIL` exist ✅ · AWS key rotated ✅ · stale blueprint copies deleted ✅.

### A. Before the first Heroku deploy (end of Phase 0 / whenever we choose to deploy)

Run once — copy-paste ready:

```bash
heroku config:set -a regalosmejores \
  DJANGO_SECRET_KEY="<generate 50 random chars>" \
  DJANGO_DEBUG=false \
  DJANGO_ALLOWED_HOSTS="regalosmejores.com,www.regalosmejores.com,.herokuapp.com" \
  SITE_URL="https://www.regalosmejores.com" \
  OPENAI_API_KEY="<from .env>" \
  OPENAI_DEFAULT_MODEL="gpt-5.6-luna" \
  OPENAI_EMBEDDING_MODEL="text-embedding-3-small" \
  OPENAI_EMBEDDING_DIMENSIONS=512 \
  KEEPA_API_KEY="<from .env>" \
  KEEPA_DOMAIN_ID=9 \
  KEEPA_TOKEN_RESERVE=20 \
  AMAZON_MARKETPLACE_HOST="www.amazon.es" \
  LLM_DAILY_BUDGET_USD=5 \
  MAX_HYDRATION_BACKLOG=5000 \
  PIPELINES_ENABLED=true \
  SESSION_SALT="<generate 32 random chars>"

heroku config:unset -a regalosmejores DEBUG      # replaced by DJANGO_DEBUG
heroku ps:scale -a regalosmejores web=1:basic worker=1:basic
```

Then confirm the Heroku Python buildpack picked up `uv.lock`
(`heroku logs --tail` during build should mention `uv`). If it does not, commit a fallback:
`uv export --no-dev --format requirements-txt > requirements.txt`.

### B. At go-live (Phase 10)

**Canonical host: `www.regalosmejores.com`.** Google treats www and apex as equivalent for
ranking, so this is chosen on operational grounds: Heroku's SSL/DNS targets are `CNAME`-based and
apex `CNAME` is non-standard, and a `www` host keeps cookies off the apex domain (useful if a
subdomain is ever added). The apex 301-redirects to `www`.

1. `heroku domains:add www.regalosmejores.com -a regalosmejores`
   `heroku domains:add regalosmejores.com -a regalosmejores`
   → note the two DNS targets Heroku prints.
2. Create a Cloudflare account, add `regalosmejores.com`, and change the nameservers at Namecheap
   to the two Cloudflare NS records. Propagation is usually under an hour.
3. In Cloudflare DNS: `CNAME www → <heroku www target>` (proxied) and
   `CNAME @ → <heroku apex target>` (proxied, CNAME flattening handles apex).
4. SSL/TLS mode **Full (strict)**. Enable Always Use HTTPS, Brotli, Early Hints.
5. Cache Rules — cache `/`, `/regalos/*`, `/buscador-avanzado`: Edge TTL 1h, Browser TTL 5m.
   Bypass cache on `/go/*`, `/admin/*`, `/buscar*`, `/healthz*`.
6. Redirect Rule: `regalosmejores.com/*` → `https://www.regalosmejores.com/$1`, 301.
7. Google Search Console: add the **domain property**, verify via Cloudflare DNS TXT, submit
   `https://www.regalosmejores.com/sitemap.xml`.
8. Update `SITE_URL` and `DJANGO_ALLOWED_HOSTS` if anything above changed.

### C. Optional / later
Sentry project + `SENTRY_DSN` · Heroku Papertrail add-on (free tier) · Amazon Associates dashboard
check that `ascsubtag` values are appearing in reports · Awin ES application.

---

## 14. Implementation plan

Each phase is independently shippable and verifiable. No unit tests — validation is the **Verify**
line of each phase, run manually. Development runs locally against the production Heroku Postgres
(`DATABASE_URL` in `.env`); deployment to Heroku is deferred until we choose to do it.

### Phase 0 — Project skeleton ✅ DONE
- `pyproject.toml`, `.python-version` (3.12), Django 5.2 project `config`, empty apps
  (`clients`, `catalog`, `topics`, `search`, `pipelines`, `tracking`, `web`, `seo`).
- Dependencies: `django`, `psycopg[binary]`, `pgvector`, `django-environ`, `gunicorn`,
  `whitenoise`, `httpx`, `openai`, `python-slugify`, `croniter`; dev: `ruff`.
  (`dj-database-url` dropped — `django-environ`'s `env.db()` already parses `DATABASE_URL`.)
- `config/settings/{base,dev,prod,ci}.py`. `dev` and `prod` both read `DATABASE_URL`; **local dev
  points at the production Heroku Postgres** (accepted decision). `ci` inherits `prod` (so
  `check --deploy` is meaningful) with a dummy DB URL and never connects.
- `Procfile`:
  ```
  release: python manage.py migrate --noinput
  web: gunicorn config.wsgi --workers 2 --threads 4 --timeout 60 --access-logfile - --error-logfile -
  worker: python manage.py run_worker
  ```
  `run_worker` does not exist until Phase 5 — do not scale the worker dyno before then.
- Tailwind **v4** standalone CLI pinned to `v4.3.3`: `static/src/input.css` → `static/css/site.css`
  (**committed**, since there is no Node buildpack). v4 is CSS-first, so there is no
  `tailwind.config.js`; template paths are declared with `@source` inside `input.css`.
  `scripts/build_css.ps1` + `scripts/build_css.sh` download and run the pinned binary into
  `.tools/` (gitignored). The same version is pinned as `TAILWIND_VERSION` in the CI workflow.
- htmx 2.0.8 self-hosted at `static/js/htmx.min.js` (no CDN, no third-party request).
- `/healthz/` view returning `200 {"status":"ok"}` without touching the DB (used by the deploy
  health check) and exempt from `SECURE_SSL_REDIRECT`.
- GitHub Actions workflow rewritten for this stack — see
  [.github/workflows/heroku-deploy.yml](.github/workflows/heroku-deploy.yml).
- **No Heroku work in this phase.** Config vars and dyno scaling happen at deploy time (§13.A).
- **Verified:** `check` and `check --deploy` clean · `migrate` applied Django's own tables to the
  prod DB · `runserver` served `/healthz/` (`{"status": "ok"}`), `/` and `/admin/login/` with 200 ·
  `ruff check .` and `ruff format --check .` clean · `static/css/site.css` built.
- Operational documentation: [docs/RUNBOOK.md](RUNBOOK.md).

### Phase 1 — Data model ✅ DONE
- All models from §4, with `pgvector` `HalfVectorField(512)`, HNSW (`halfvec_cosine_ops`,
  `m=16, ef_construction=64`) on `ProductFacet`, `Topic` and `TopicAlias`; GIN
  `to_tsvector('spanish', text)` and `gin_trgm_ops` on `ProductFacet.text`; GIN on the
  occasion/recipient/interest arrays.
- `apps/catalog/migrations/0001_extensions.py` creates `vector`, `pg_trgm`, `unaccent` and
  `btree_gin` (idempotent — they already existed on the Heroku DB).
- Django Admin for all 16 models: list displays, filters, search fields, autocomplete,
  read-only computed fields, inlines (facets under a product; aliases / search terms / links under
  a topic; step runs under a pipeline run), and bulk actions (block/unblock products, requeue
  enrichment, pin/exclude links, activate/archive topics, retry/cancel jobs, run schedule now).
  Ledger models (`PipelineRun`, `ClickEvent`, `PageView`, `KeepaTokenLedger`, `LLMCall`,
  `NotificationLog`, `UserQuery`) are read-only in Admin so history cannot be edited.
- `RankingConfig` singleton seeded by `search/0002_seed_rankingconfig.py`.
- Two deviations from §4, both deliberate: optional text fields (`brand`, `manufacturer`,
  `model`, `parent_asin`, `product_group`) use `blank=True, default=""` instead of `null=True`
  per Django convention — `NULL` is kept only where it is semantically distinct (`price_band` =
  "not computed"). `Topic.distinct_brands` was added because the quality gate in §9.2 needs it.
- **Verified:** migrations applied to the prod DB (31 tables, 11 MB); all HNSW/GIN indexes present
  in `pg_indexes`; `check --tag admin` clean; all 36 Admin changelist and add URLs render.

### Phase 2 — Clients ✅ DONE
- `KeepaClient` (`token` / `product` / `query` / `search`), writing a `KeepaTokenLedger` row on
  every call and projecting the current balance from the newest row plus `refillRate`, so the
  budget guard is self-calibrating. Raises `KeepaBudgetExhausted` (retryable, carries
  `retry_after_seconds`) rather than failing, and `KeepaAuthError` on 401/403.
- `LLMClient.complete()` / `.embed()`, model registry with real per-1M pricing, `LLMCall` ledger
  on success *and* failure, daily cap raising `LLMBudgetExhausted` plus a Telegram CRITICAL.
  Unsupported sampling params are detected from the API error and dropped on retry, so the
  registry never has to encode a per-model capability matrix.
- `TelegramClient.notify()` — throttled per key via `NotificationLog`, never raises.
- `manage.py ping_clients [--skip-keepa|--skip-llm|--skip-telegram]`.

**Keepa API facts confirmed against amazon.es (domain 9) — these differ from older docs:**

| Fact | Value |
|---|---|
| Images | `product["images"]` is a list of objects (`{"l","m","variant"}`), **not** `imagesCSV`. MAIN variant is hoisted first. |
| Variations | `product["variations"]` is a list of objects, **not** `variationCSV`. `parentAsin` is preferred for `variation_group_key`. |
| Reviews | `product["reviews"]["ratingCount"]` is more reliable than `stats.current[17]`. |
| Rating | `stats.current[16]` ÷ 10. Price: `stats.current[18]` (buy box) → `[1]` (new) → `[0]` (Amazon); `-1` means no data. |
| Product Finder | `perPage` must be **≥ 50** — smaller values return `invalidParameter`. |
| Token cost | `/token` free · `/query` ≈ 11 · `/product` with `stats+rating+buybox` ≈ 3.5 per ASIN. Account refills at **21/min** (not 20). |

- **Verified:** `ping_clients` green on all three — Product Finder returned 50 ASINs of 4.2M,
  hydration parsed titles/brands/ratings/images/prices, `gpt-5.6-luna` returned valid structured
  JSON for $0.000036, embeddings returned 512 dims, a message landed in the channel, and
  `KeepaTokenLedger` / `LLMCall` / `NotificationLog` rows were all written.

### Phase 3 — Pipeline engine
- `Pipeline` base + registry + `JobQueue` + `scheduler` + `BudgetGuard` + `run_worker`.
- Admin: queue view with retry/cancel actions, run history with duration/cost columns,
  schedule editor, global kill switch.
- `manage.py run_pipeline <key>`.
- **Verify:** a no-op demo pipeline scheduled every minute produces `PipelineRun` rows; killing the
  worker mid-job leaves the job reclaimable; `BudgetGuard` visibly defers when the reserve is faked.

### Phase 4 — Ingestion (get products into the DB)
- `seed_products` (Product Finder + quality filters), `hydrate_products` (batch ×100),
  `refresh_products`.
- Keepa → `Product` mapping incl. `price_band`, `sales_rank_pct`, `variation_group_key`,
  `quality_score`, `keepa_fetched_at`.
- Scale `worker=1:basic` and let it run.
- **Verify:** products accumulate steadily; token ledger matches Keepa's dashboard;
  no duplicate ASINs; variation groups populated.

### Phase 5 — Enrichment & embeddings
- `enrich_products`: structured-output LLM call → 6–10 `ProductFacet` synthetic queries + controlled
  vocabulary tags → batch embed at 512 dims → store as `halfvec`.
- Controlled vocabularies for occasion / recipient / interest defined as Python constants + Admin
  reference page.
- **Verify:** spot-check 20 products in Admin — facets read like real user queries, tags are sane,
  cost per product is within expectation (log it in the digest).

### Phase 6 — Topics & curation
- Manual Topic creation in Admin (must work before any LLM generation).
- `decompose_topic`, `run_search_terms`, `generate_topics`, `curate_topics`, `dedupe_topics`.
- Quality gate + `is_indexable` computation.
- **Seed 30–50 topics by hand** covering the biggest occasions/recipients.
- **Verify:** each seeded topic has ≥12 diverse, genuinely relevant products; `is_indexable`
  flips correctly; pinning/excluding survives a recompute.

### Phase 7 — Search engine (no UI yet)
- `normalize`, `retrieval` (HNSW + FTS + trigram + RRF), `ranking`, `diversity`, result cache.
- `manage.py search "<query>"` printing ranked results with score breakdown.
- **Verify:** run ~40 real-sounding Spanish queries; tune `RankingConfig` weights from Admin until
  results are good; confirm p50 latency < 200 ms.

### Phase 8 — Public site
- Base layout, header with embedded simple-search component, footer, Tailwind design tokens.
- `ProductCard` component with variants + all three CTAs.
- Home, `/regalos/<slug>/`, hub pages, `/buscar/`, `/buscador-avanzado/` (+ results), legal pages.
- HTMX for filter chips and "cargar más".
- **Verify:** every page renders on mobile; Lighthouse performance ≥ 90; simple search redirects to
  a topic when it matches strongly.

### Phase 9 — Tracking
- `/go/<click_id>/` redirect view, affiliate URL builder with `ascsubtag`, `ClickEvent` writing.
- `PageView` middleware with bot filtering and salted session hashing.
- `UserQuery` write-behind + `mine_query_demand` + `recompute_ctr`.
- Admin dashboards: clicks by placement/button/position, top queries, zero-result queries,
  topic performance.
- **Verify:** clicking each CTA writes a correct `ClickEvent` and lands on the right Amazon page
  with the tag present; `ascsubtag` visible in the final URL.

### Phase 10 — SEO & launch
- Sitemaps, `robots.txt`, JSON-LD, canonical/OG tags, 301 slug-change handling.
- Custom domain + Cloudflare per §10 Stage 1.
- Search Console verification + sitemap submission.
- `daily_digest` Telegram pipeline live.
- **Verify:** `curl` the live domain over HTTPS on both hosts; sitemap contains only indexable
  topics; Rich Results Test passes; Search Console reports no coverage errors.

### Phase 11 — Steady state (post-launch, ongoing)
Publish ≤10 topics/day · review `QueryDemand` weekly and promote real demand into Topics ·
tune ranking weights from click data · raise Keepa plan if the backlog grows · then, and only then,
start the article pipeline (max 1/day, irregular times, human-reviewed).

---

**Start here:** Phase 0. Everything needed to begin is in this document; the only external
dependency is the owner checklist in §13, none of which blocks Phases 0–9.
