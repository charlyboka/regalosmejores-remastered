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
| `enrich_products` | Generate `ProductFacet` synthetic queries + tags, then embed | `*/15 * * * *` | no | yes |
| `generate_topics` | Propose new Topics: gaps vs existing coverage + seasonal calendar | `0 3 * * *` | no | yes |
| `mine_query_demand` | Roll `UserQuery` → `QueryDemand`; promote high-demand unmatched queries to Topic candidates | `30 2 * * *` | no | no |
| `decompose_topic` | Topic → Amazon `SearchTerm`s | on demand / `0 4 * * *` | no | yes |
| `run_search_terms` | Keepa keyword search for pending `SearchTerm`s → queue ASINs for hydration | `*/15 * * * *` | yes | no |
| `curate_topics` | Recompute `ProductTopicLink` + quality gate + `is_indexable` | `0 5 * * *` | no | no |
| `dedupe_topics` | Flag near-duplicate topics for merge | `0 6 * * 1` | no | no |
| `refresh_products` | Re-fetch products older than 30 days; deactivate dead ASINs | `0 */2 * * *` | yes | no |
| `recompute_derived` | Re-derive bands / `sales_rank_pct` / `quality_score` and re-apply the quality gate | `20 1 * * *` | no | no |
| `prune_catalog` | Delete products that will never earn their storage back (§7.5) | `0 2 * * 0` | no | no |
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

### 7.5 Retention — what leaves the catalogue, and why

The catalogue must not grow forever. Counter-intuitively **the binding constraint is disk, not
Keepa tokens**, and that shapes the whole policy:

- **Refreshing is cheap.** A 20k catalogue refreshed every 90 days is 222 products/day × 4 tokens
  = ~900 tokens/day, about 3 % of the 30 240 that refill daily. Even a 30-day cycle is only ~9 %.
  There is no token argument for refreshing less often; the cadence should be set by how fast
  Amazon prices and ranks actually drift, not by budget.
- **Storing is expensive.** An enriched product costs ~20 KB — mostly its 8 facet vectors and
  their HNSW index entries (§14 Phase 5 capacity note). That is what fills a 1 GB plan at
  ~20–25k products.

So the lever is eviction, not throttling. `prune_catalog` runs weekly and **deletes** (not
deactivates) products that will never earn their storage back:

1. `is_active = False` for > 60 days — Keepa has not returned the ASIN in two months; it is gone.
2. `SKIPPED` by the quality gate for > 90 days — it failed the standards and nothing has changed.
3. `FAILED` enrichment for > 90 days — the model could not describe it twice; a human has not
   intervened.
4. `DONE` but zero clicks **and** zero impressions for > 180 days, *and* below the median
   `quality_score` — it occupies index space and has never once been useful.

Rules 1–3 are safe to automate. **Rule 4 is not**: it needs the impression data from Phase 8, and
a product with no impressions may simply never have been *shown* rather than never wanted. It
stays behind a `dry_run` option that reports what it would delete, until we have enough traffic to
trust it.

Deletion cascades to `ProductFacet`, which is where the space actually is. It is blocked by
`ClickEvent`'s `PROTECT`, which is the desired behaviour: **a product anybody ever clicked is never
deleted**, because that would destroy the click history the CTR score is built from. `prune_catalog`
therefore excludes anything with clicks and reports the count it skipped for that reason.

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

### Phase 3 — Pipeline engine ✅ DONE
- `Pipeline` base + registry + `JobQueue` + `scheduler` + `BudgetGuard` + `run_worker`.
- Admin: queue view with retry/cancel actions, run history with duration/cost columns,
  schedule editor, global kill switch.
- `manage.py run_pipeline <key>`.
- **Verify:** a no-op demo pipeline scheduled every minute produces `PipelineRun` rows; killing the
  worker mid-job leaves the job reclaimable; `BudgetGuard` visibly defers when the reserve is faked.

**Delivered.** `apps/pipelines/`: `base.py` (`Pipeline` ABC, `PipelineContext` with lazy
`keepa`/`llm` clients and a `step()` context manager, `PipelineResult`), `registry.py`
(`@register` + `pkgutil` auto-discovery of `apps.pipelines.pipelines.*`), `queue.py`
(`enqueue`/`claim_next`/`complete`/`defer`/`fail`/`release_stale_jobs`), `scheduler.py`
(`sync_schedules`, `tick`), `budget.py` (`BudgetGuard` with four gates), `worker.py`
(`execute()` + `Worker` loop). Commands: `run_worker [--once --sleep --name]`,
`run_pipeline <key> [--list --payload --max-items --ignore-budget]`, and `dev` (in `apps.web`)
which runs the web server and the worker together for local development.

**Deviations and additions beyond §4.5 / §7.1:**
- **`WorkerHeartbeat` model added** (migration `pipelines/0002`). Without it there is no way to
  answer "is the worker alive?" — a silent worker and an empty queue look identical in Admin.
  Keyed by `DYNO` (or hostname), so restarts reuse one row instead of accumulating dead ones.
  `is_stale` is true after 15 minutes without a beat.
- **`defer()` decrements `attempts`.** Deferral means conditions were wrong (no tokens, budget
  spent, kill switch), not that the job is broken, so it must not consume a retry. Otherwise a
  weekend of token starvation would permanently FAIL every scheduled job.
- **The kill switch defers rather than refusing to claim.** `PIPELINES_ENABLED=false` is a
  `BudgetGuard` gate, which means paused work is visible in Admin as DEFERRED jobs with a reason,
  and resumes automatically when the flag flips back.
- **Three permanent diagnostic pipelines** (`demo_noop`, `demo_fail`, `demo_budget`) instead of a
  throwaway one, so the engine can always be verified without spending tokens or LLM budget.
  None declares a `default_cron`; they are on-demand only.
- **`execute()` is shared** by the worker and `run_pipeline`, so a manual run is not a second code
  path — it produces identical run, step, token and cost records.
- **Budget gates use real data, not estimates:** Keepa headroom is projected from the newest
  `KeepaTokenLedger` row (Keepa's own `tokensLeft` + `refillRate`), ignoring readings older than
  6 hours; LLM spend comes from summing today's `LLMCall` rows.

**Verified live:** `run_pipeline demo_noop` → SUCCESS with two step rows; `demo_budget` → SKIPPED
(`keepa_budget_exhausted`, "~1475 tokens available, need 10000020") and the job returned to
DEFERRED with `attempts` back at 0; `demo_fail` → FAILED run, job requeued with 60 s backoff;
duplicate `dedupe_key` returned `None`; a job forced to RUNNING with a 2-hour-old lock was
reclaimed to QUEUED; `manage.py dev` started both processes and stopped both cleanly.

### Phase 4 — Ingestion (get products into the DB) ✅ DONE
- `seed_products` (Product Finder + quality filters), `hydrate_products` (batch ×100),
  `refresh_products`.
- Keepa → `Product` mapping incl. `price_band`, `sales_rank_pct`, `variation_group_key`,
  `quality_score`, `keepa_fetched_at`.
- Scale `worker=1:basic` and let it run.
- **Verify:** products accumulate steadily; token ledger matches Keepa's dashboard;
  no duplicate ASINs; variation groups populated.

**Delivered.** `apps/catalog/categories.py` (frozen Keepa category tree for domain 9, 36 roots,
15 flagged gift-suitable, plus type/binding blocklists), `apps/catalog/services.py`
(`QualityGate`, `compute_sales_rank_pct`, `compute_quality_score`, `compute_price_band`,
`apply_keepa_product`, `recompute_derived`, `apply_gate`) and
`apps/pipelines/pipelines/ingest.py` with four pipelines: `seed_products` (50 / 6 h),
`hydrate_products` (25 / 5 min), `refresh_products` (25 / 2 h), `recompute_derived` (daily).

**Deviations from the plan above, and why:**
1. **`Product.price_cents` added** (migration `catalog/0003`). Internal-only — never rendered in a
   template or feed, because Amazon Associates terms only permit displaying PA-API prices. It
   exists to compute bands, to spread prices in the diversity pass, and to re-band without
   re-fetching from Keepa.
2. **Band thresholds moved to `RankingConfig`** (`price_band_economico_max_cents` = 2500,
   `price_band_medio_max_cents` = 7500, migration `search/0003`) so they are Admin-editable
   without a deploy. They are deliberately *absolute* euros: a shopper's wallet is absolute, so
   "económico" must mean cheap, not "cheap for a camera". Category-relative price spread is the
   diversity pass's job (Phase 7), not the band's.
3. **A fourth pipeline, `recompute_derived`**, beyond the three listed. It re-applies the bands,
   `sales_rank_pct`, `quality_score` *and the quality gate* over the whole catalogue for zero
   Keepa tokens, which is what makes the Admin-editable thresholds meaningful — raising standards
   cleans the site retroactively without re-fetching anything.
4. **`sales_rank_pct` uses real per-category product counts** from the frozen category tree rather
   than a fixed denominator, so a rank of 5 000 means something different in Hogar y cocina
   (51 M products) than in Videojuegos (457 K).
5. **Out-of-scope products get `enrichment_status = SKIPPED`, not `is_active = False`.**
   `is_active` stays reserved for "Keepa no longer returns this ASIN". Both are excluded by the
   §6.4 hard filters, but the distinction keeps the audit trail honest. `refresh_products` skips
   `SKIPPED` rows so we never spend tokens re-fetching something we will never serve.
6. **`sales_rank_min = 150` and `min_price_cents = 1500` added to the Finder selection**, after the
   first sample seed returned AA batteries, Kindles and Echo Dots. The top ~100 ranks of every
   Amazon category are consumables, not gifts. The €15 floor is also enforced in `QualityGate`, so
   it applies at serving time and not only at harvest time.
7. **Keepa's `productGroup` is dead** — it is `null` on every response now. The field that carries
   the same meaning is `type` (`BATTERY`, `TOY_BUILDING_BLOCK`, `PHYSICAL_MOVIE`, …), and it is far
   more specific than `productGroup` ever was. `KeepaClient` now maps `type` onto
   `Product.product_group`, and also stores `binding` (`blu_ray`, `tapa_blanda`, …) as a backstop.
   `BLOCKED_PRODUCT_GROUPS` / `BLOCKED_BINDINGS` filter media and consumables that slip through the
   category allowlist — a Blu-ray really is filed under Electrónica.
8. **`Keepa referralFeePercent` deliberately not stored.** It is the fee Amazon charges the *seller*,
   not the Associates commission. Commission rate is a function of the Associates category table
   and will be derived from `root_category_id` in Phase 7.
9. **Amazon first-party hardware is blocked** (`AMAZON_BOOK_READER`, `DIGITAL_DEVICE_3/4` — Kindle,
   Echo, Fire TV). It passes every quality filter, but it earns 0% Associates commission in Spain
   and has no discovery value: nobody needs this site to learn that a Kindle exists.

**Verified live** on a deliberately small sample (the intended pace is a queue, never saturation):
two `seed_products` runs created 100 stubs for 11 Keepa tokens each; three `hydrate_products` runs
hydrated 75 products at exactly 4 tokens/ASIN. `recompute_derived` then parked 48 (38 `BATTERY`,
9 Amazon devices, 1 Blu-ray) and left **27 genuine gifts** — LEGO, board games, Montessori sets,
plush, chess, Tapo cameras — spread across all three price bands, quality 0.836–0.973, collapsing
to a small set of `variation_group_key` values. Tightening the standards three times over the
sample cost **zero Keepa tokens**, which is the whole point of `recompute_derived`. All four
schedules registered with their crons.


### Phase 5 — Enrichment & embeddings ✅ DONE
- `enrich_products`: structured-output LLM call → 6–10 `ProductFacet` synthetic queries + controlled
  vocabulary tags → batch embed at 512 dims → store as `halfvec`.
- Controlled vocabularies for occasion / recipient / interest defined as Python constants + Admin
  reference page.
- **Verify:** spot-check 20 products in Admin — facets read like real user queries, tags are sane,
  cost per product is within expectation (log it in the digest).

**Delivered.** `apps/catalog/vocabularies.py` (19 occasions, 26 recipients, 70 interests, all
ASCII-slug keys with Spanish labels), `apps/catalog/enrichment.py` (prompt, strict JSON schema,
validation) and `apps/pipelines/pipelines/enrich.py` (`enrich_products`, `*/15 * * * *`, 10/run).

**Deviations, and why:**
1. **Two facet types, not three.** `SYNTHETIC_QUERY` (6–8) plus one `SUMMARY` at weight 0.6.
   `GIFT_ANGLE` stays in the enum but is unused: every extra facet is another halfvec row *and*
   another HNSW index entry, and the storage maths below does not leave room for a third type.
2. **The vocabulary legend is in the prompt.** The schema's `enum` constrains the model to valid
   keys, but a key is a slug — nothing in `nino` says "6–11 años". Without the legend the model
   tagged a 4-year-old LEGO set as `nino` instead of `nino-pequeno`. Sending ~1 400 tokens of
   `clave = significado` fixed it and raised cost per product from $0.0011 to $0.0014. Worth it:
   these tags are the hard filters behind advanced search, so a wrong tag is a wrong result.
3. **Life stages live in `RECIPIENTS`.** Keepa gives no reliable per-product age, so the "Edad"
   slot in §6.5 resolves to recipient keys (`bebe`, `nino-pequeno`, `nino`, `adolescente`, …)
   rather than a numeric range.
4. **All-or-nothing per product.** If the model returns fewer than `min_queries` usable queries,
   the product is marked `FAILED` and *no* facets are written. A half-enriched product would rank
   badly forever and look like a search bug rather than an enrichment bug.
5. **`FAILED` is not retried automatically.** It needs `--payload '{"retry_failed": true}'`.
   Retrying a product the model cannot describe, every 15 minutes, forever, is an unbounded bill.
6. **Enrichment failures never touch `availability_note`.** That field belongs to the quality gate
   and `recompute_derived` rewrites it; the failure reason goes in the `PipelineStepRun` context.
7. **`reenrich` option** re-runs products that are already `DONE` — the switch to pull after
   changing the prompt or the vocabularies. Facets are replaced, not merged: the LLM returns no
   stable identity across runs, so there is nothing to match old rows against.

**Verified live** on all 27 servible products: **218 facets, 0 without an embedding**, 8.1 facets
per product, **$0.0247 total — $0.00095 per product**, zero failures. Spot-checked output reads
like real queries ("regalo de reyes para una niña que inventa historias"), not restated titles.
A live cosine search over the facets returned sensible products for three unseen queries at
similarity 0.61–0.80, confirming the query↔query premise of §6.1 end to end.

> **Capacity note, for Phase 10.** At 8.1 facets/product a `halfvec(512)` row plus its HNSW index
> entry costs roughly 2.5 KB. That puts the practical ceiling of Heroku `essential-0` (1 GB) at
> **~20–25k enriched products**, not the 50k §6.2 assumed — §6.2 counted vector data but not the
> index. Options when we approach it: drop to 6 facets, cap the catalogue, or move to
> `standard-0` (4 GB). Not urgent, but it is the first limit we will hit.

### Phase 6 — Search engine (no UI yet) ✅ DONE

> **Swapped with Topics.** This was Phase 7. §6.6 defines `curate_topics` as "re-runs full
> retrieval for a Topic", so Topics depends on the retrieval stack. Building Topics first would
> have meant a throwaway retrieval or a stubbed `curate_topics`. The two phases are built back to
> back, retrieval first.

Built: `apps/search/normalize.py`, `retrieval.py`, `ranking.py`, `engine.py`, plus
`manage.py search` (single query, score breakdown) and `manage.py search_report` (40-query batch
with a quality summary). Diversity lives in `ranking.py` rather than a separate `diversity.py` —
it shares the `Result` dataclass and is 60 lines; a separate module would have been indirection
for its own sake.

**Deviations from the spec, and why:**

1. **Two normalisations, not one.** §6.3 L0 says "lowercase, unaccent, collapse, strip stopwords".
   Stripping stopwords before embedding would destroy the signal the facets were written to match
   — "regalo para mi madre" and "regalo madre" are not the same sentence to an embedding model.
   So `normalize()` (accent- and punctuation-free, word order intact) is what gets embedded, and
   `cache_key()` applies stopword stripping on top, purely so phrasings of the same intent share
   one cache entry and one `QueryDemand` row.
2. **A Spanish *unaccented* FTS configuration was required.** The index built in Phase 1 used the
   stock `spanish` config, which preserves accents, while queries arrive accent-free. The lexical
   arm therefore matched nothing on any query containing an accented word — which in Spanish is
   most of them — and only the trigram arm was firing, by accident. Catalog migration `0005`
   creates `spanish_unaccent` (`unaccent` + `spanish_stem`) and rebuilds `facet_text_fts_idx`
   against it. `retrieval.FTS_CONFIG` must always equal the index expression or Postgres silently
   sequential-scans.
3. **A relevance floor was added, and it is the most important line in the engine.**
   `min_facet_similarity` originally only bounded the semantic arm, so a product that tripped only
   the trigram arm entered the pool with `similarity = 0` and was then ranked on quality and
   freshness alone. In practice that meant the highest-rated product in the catalogue answered
   every query it had no business answering — a security camera was the fourth result for
   "regalo para alguien que le encanta cocinar". `retrieval._is_relevant` now requires either a
   real semantic match or a full-text hit (`websearch` requires every term, so it is trustworthy
   on its own). Trigram can no longer promote a candidate by itself. An empty result page is
   recoverable; a confident, irrelevant one is not.
4. **Trigram threshold raised 0.2 → 0.45.** Spanish gift queries share heavy boilerplate
   ("regalo para … que le gusta …"), so a loose threshold makes every facet resemble every query.
5. **The category cap scales with page size**, `max(config.max_per_category, ceil(limit/3))`.
   The absolute 3 was written for a 24-result page, where it would have answered "regalo para un
   niño" with 3 toys and 21 unrelated products — the category usually *is* the query. Brand stays
   absolute at 2, because two of the same brand is two too many at any page size. The Admin value
   now acts as a floor, so small pages keep the tighter spread.
6. **RRF contributions are capped at one per product per list.** A product with eight facets would
   otherwise accumulate eight contributions and outrank a better product that matched on one.
7. **The result cache stores ordering, not products.** Product rows are re-read live and the hard
   filters re-applied on read, so a product deactivated or blocked after caching is never served.
   Caching rendered products would have served it for up to `cache_ttl_hours`.
8. **One shared `LLMClient`, warmed at startup.** Opening the TLS connection measured **6.9 s cold
   versus 375 ms warm** — a per-process cost, not a per-call one. `SearchConfig.ready()` warms it
   in a daemon thread for `gunicorn`/`runserver` only (`DISABLE_SEARCH_WARMUP` opts out), costing
   one embedding call per dyno boot.

**Verified live** against production: 40 real-sounding Spanish queries (accents, typos, vague
intent) via `manage.py search_report`. **Zero duplicate variation families across all 40** — the
11-product FUNNYB&G craft-kit family collapses to one result every time. 39 of 40 returned
results; the one that did not ("regalo para mi jefe") correctly returns nothing, because a
27-product toy catalogue genuinely contains no boss-appropriate gift. Before the relevance floor
the same batch averaged 11.3 results per query; after, 6.5 — the difference was entirely junk.

**On the < 200 ms p50 target:** measured p50 is 1 062 ms locally, but that is not the engine.
A single database round trip from this machine to Heroku Postgres EU measures **79 ms**, and a
search makes six; embedding adds ~375 ms warm. On a dyno sitting next to the database those six
round trips cost single-digit milliseconds, putting an uncached search at roughly **400 ms,
dominated entirely by the OpenAI embedding call**, and a cached one in single digits. The < 200 ms
target is therefore only achievable on cache hits. This is acceptable — the cache absorbs repeat
traffic and no chat-completion call ever touches the request path — but it should be re-measured
on the dyno in Phase 10 rather than assumed.

**Still open, deliberately:** ranking weights are untuned. With 27 enriched products there is not
enough signal to tune them honestly, and `ctr_score` is 0.000 for every product until Phase 9
collects clicks. Re-run `manage.py search_report` and tune from Admin once the catalogue is in the
thousands.

### Phase 7 — Topics & curation ✅ DONE (verification deferred, see below)

Built: `apps/topics/services.py` (canonical text, curation, §9.2 gate),
`apps/pipelines/pipelines/topics.py` (`curate_topics`, `decompose_topic`, `dedupe_topics`),
`run_search_terms` in `ingest.py`, and `manage.py seed_topics`.

**Deviations from the spec, and why:**

1. **`generate_topics` was not built.** It proposes new topics from coverage gaps and demand, but
   `QueryDemand` is empty until Phase 9 collects searches, so today it would have to invent topics
   from nothing — which is exactly how a site ends up with 500 thin pages. The 12 hand-written
   seed topics are the reference set; `generate_topics` lands once there is real demand data to
   mine. §9.3 caps publication at 10/day regardless.
2. **`run_search_terms` uses Product Finder with a `title` filter, not Keepa's `search` endpoint.**
   Finder returns bare ASINs at ~11 tokens per call and hydration fills in details at 4 tokens
   each; `search` returns full product objects at a much worse rate for ASINs that may be
   discarded at the quality gate anyway. Measured: **3 terms → 50 new products for 31 tokens**,
   roughly 0.6 tokens per product discovered, against ~4 for category seeding. It reuses the
   identical quality thresholds, so topics cannot become a back door for junk.
3. **Canonical text skips facets already named in the title.** Appending them produced
   *"Regalos para madres para madre"* — repetition that moves the vector without adding meaning.
   A five-character prefix match handles Spanish inflection ("madre"/"madres",
   "cocina"/"cocinar"). Nothing is lost: the facets still apply as hard filters via `topic_slots`.
4. **Curation calls `engine.search` with `use_cache=False`.** A cached ordering would let a topic
   page drift behind live search, which is the exact disagreement this phase exists to prevent.
5. **Pinned links are renumbered to the top slots** rather than keeping their old rank, so an
   editor's choices actually lead the page.
6. **`dedupe_topics` never merges**, per §6.6 — it records a `TopicAlias` and fires a Telegram
   warning for an editor to confirm. It also refuses to propose demoting an indexed page in
   favour of one that is not.

**Verified live** against production:

- `seed_topics --embed` created 12 topics with clean canonical text and embeddings.
- `curate_topics` ran all 12 in one pass, **0 Keepa tokens, $0 LLM** (embeddings already existed).
- The §9.2 gate behaves correctly on real data: `regalos-de-cumpleanos` linked **15 products
  across 13 brands but only 2 categories**, so it correctly stayed `is_indexable = False` on the
  ≥4-categories rule. Topics needing adult recipients (`madres`, `padres`, `abuelos`) linked
  **0 products** — correct, because the 27-product sample is almost entirely toys.
- **Pinning and exclusion survive a recompute**, confirmed directly: a pinned link moved from rank
  15 to rank 1 and stayed pinned; an excluded link was dropped from the page and from
  `linked_count` (15 → 14) while remaining in the table.
- `decompose_topic` produced genuine catalogue keywords rather than gift phrases — *"set
  jardinería principiantes"*, *"masajeador de cuello"*, *"joyero organizador"* — 25 terms across
  3 topics for **$0.000661**.
- `run_search_terms` then turned 3 of those terms into **50 new candidate products for 31 tokens**.

**Verification deliberately deferred:** the "≥12 diverse, genuinely relevant products per topic"
check cannot be judged against 27 enriched products, nearly all toys. The gate is demonstrably
working — it is rejecting everything it should — but whether curation picks *good* products at
scale is unanswerable until the catalogue is in the thousands. Re-run `curate_topics --force` and
re-read the table then.

**Throughput was raised to get there.** Enrichment was the bottleneck at 960 products/day against
a hydration capacity of 7 200, and only enriched products are searchable. Now: `seed_products`
200/hour, `enrich_products` 40 per 15 min (~3 800/day, ~$3.60/day). Steady-state Keepa use is
roughly **71% of the 30 240 token daily budget**, leaving headroom for `refresh_products` and
`run_search_terms`. Throttle back down once the catalogue is large enough to tune against.

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

**`prune_catalog` (§7.5) belongs here, not earlier.** Rules 1–3 (dead ASINs, long-parked, long-
failed) can be enabled as soon as there is anything old enough to match. Rule 4 — evicting
enriched products with no clicks and no impressions — needs Phase 9 tracking data and enough
traffic to distinguish "nobody wanted it" from "nobody was ever shown it", so it stays in `dry_run`
until then. Watch DB size in the daily digest: at ~20 KB per enriched product, `essential-0` fills
at ~20–25k products and that, not the Keepa budget, is what caps the catalogue.

---

**Start here:** Phase 0. Everything needed to begin is in this document; the only external
dependency is the owner checklist in §13, none of which blocks Phases 0–9.
