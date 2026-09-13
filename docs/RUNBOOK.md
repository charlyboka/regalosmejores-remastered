# Runbook

Day-to-day operations for **Regalos Mejores**. The architecture and the build plan live in
[project_blueprint.md](project_blueprint.md) — this file is only "how do I run it".

> ⚠️ **Local development talks to the production database.** `DATABASE_URL` in `.env` points at the
> Heroku Postgres. There is no local Postgres. Anything you delete locally is deleted for real.

---

## 1. One-time setup

```powershell
uv sync                                              # creates .venv, installs everything
Copy-Item .env.example .env                          # then fill in the values
powershell -NoProfile -File scripts/build_css.ps1    # downloads Tailwind CLI, builds site.css
uv run python manage.py migrate
uv run python manage.py createsuperuser              # type the password directly in the terminal
```

`uv sync` is the only install step — never `pip install`. If you add a dependency:

```powershell
uv add <package>          # runtime
uv add --dev <package>    # tooling only
```

Both update `pyproject.toml` **and** `uv.lock`. Commit both; CI runs `uv sync --locked` and fails
if the lock file is out of date.

---

## 2. Start and stop the app locally

### Everything at once (the normal way)

```powershell
uv run python manage.py dev
```

Starts the web server **and** the pipeline worker in one console. **Stop both with `Ctrl+C`.**
If either process dies, the other is shut down too, so you never end up with half a stack.

| Flag | Effect |
|---|---|
| `--port 8001` | Serve on another port |
| `--no-worker` | Web server only |
| `--no-web` | Worker only |

If `Ctrl+C` ever leaves something behind (force-closed window, crashed console):

```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'runserver|run_worker' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
```

A job the worker was holding when it died is **not** lost: the next worker reclaims it after 30
minutes (`release_stale_jobs`), or immediately if you use the *Reintentar ahora* action in Admin.

### Web server on its own

```powershell
uv run python manage.py runserver          # http://127.0.0.1:8000/
```

Stop with `Ctrl+C`. If a stray server is holding port 8000:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

Run on another port with `runserver 8001`.

### Background worker on its own

```powershell
uv run python manage.py run_worker
uv run python manage.py run_worker --once     # one iteration then exit, for debugging
```

Stop with `Ctrl+C` — it finishes the job in flight first. Only ever run **one** worker: the job
queue is designed for concurrency 1.

### Production-like server

```powershell
$env:DJANGO_SETTINGS_MODULE="config.settings.prod"
uv run gunicorn config.wsgi --workers 2 --threads 4 --timeout 60
```

Rarely needed; `runserver` is the normal path.

### CSS

```powershell
powershell -NoProfile -File scripts/build_css.ps1            # one-off
powershell -NoProfile -File scripts/build_css.ps1 -Watch     # rebuild while editing templates
```

`static/css/site.css` is **committed to git** because Heroku has no Node buildpack. CI rebuilds it
and fails the build if the committed copy is stale, so **rebuild and commit whenever you touch
template classes**. The Tailwind version is pinned in three places that must stay in sync:
`scripts/build_css.ps1`, `scripts/build_css.sh`, and `TAILWIND_VERSION` in the CI workflow.

---

## 3. Before every push

```powershell
uv run ruff format .
uv run ruff check . --fix
powershell -NoProfile -File scripts/build_css.ps1
uv run python manage.py makemigrations --check --dry-run
$env:DJANGO_SETTINGS_MODULE="config.settings.ci"; uv run python manage.py check --deploy; Remove-Item Env:DJANGO_SETTINGS_MODULE
```

That is exactly what CI runs. There are no unit tests by design — validation is manual.

---

## 4. Local URLs

| URL | What it is |
|---|---|
| http://127.0.0.1:8000/ | Home page |
| http://127.0.0.1:8000/healthz/ | Liveness probe — returns `{"status": "ok"}`, never touches the DB |
| http://127.0.0.1:8000/admin/ | Django Admin — the entire operations console |

---

## 5. Monitoring the pipelines

Everything is observed through Django Admin. Replace the host with the production domain when
looking at the live site.

| Admin URL | Use it to |
|---|---|
| `/admin/pipelines/pipelinerun/` | Every run: status, duration, counters, error, plus its steps inline. **Start here when something looks wrong.** |
| `/admin/pipelines/jobqueue/` | Pending / running / failed jobs, with retry and cancel actions. A growing backlog means the worker is down or throttled |
| `/admin/pipelines/pipelineschedule/` | Cron expressions and per-pipeline enable/disable switches |
| `/admin/pipelines/keepatokenledger/` | Keepa token balance over time — the hard constraint on ingestion speed |
| `/admin/pipelines/llmcall/` | LLM spend, per call and per day, against `LLM_DAILY_BUDGET_USD` |
| `/admin/pipelines/notificationlog/` | What was sent to Telegram and whether it succeeded |
| `/admin/pipelines/workerheartbeat/` | **Is the worker alive?** The *Vivo* column goes red if there has been no heartbeat for 15 minutes |
| `/admin/search/userquery/` | Raw incoming searches |
| `/admin/search/querydemand/` | Clustered demand — the input to new topic creation |
| `/admin/topics/topic/` | Topic status, quality score, indexability |
| `/admin/tracking/clickevent/` | Outbound affiliate clicks |

These Admin views are created in Phase 1 and filled in as later phases land.

Alerts arrive in the Telegram channel (`TELEGRAM_CHANNEL_ID`). Telegram is the push channel; Admin
is the pull channel. If Telegram goes quiet, check `notificationlog` before assuming all is well.

### Kill switch

Set `PIPELINES_ENABLED=false` and restart the worker. The worker keeps running and keeps claiming
jobs, but every one of them is deferred instead of executed, so nothing is lost — they all resume
when you switch it back on. Individual pipelines can be disabled instead via
`/admin/pipelines/pipelineschedule/`.

### Running a pipeline by hand

```powershell
uv run python manage.py run_pipeline --list                    # what exists
uv run python manage.py run_pipeline demo_noop                 # engine health check, costs nothing
uv run python manage.py run_pipeline <key> --max-items 5
uv run python manage.py run_pipeline <key> --payload '{"asin": "B0XXXX"}'
uv run python manage.py run_pipeline <key> --ignore-budget     # spends real money, be deliberate
```

This runs **synchronously, in your console, bypassing the queue**, but through the same executor
the worker uses — so it produces the same `PipelineRun`, step, token and cost records. It prints
the outcome and per-step timings when it finishes.

Three diagnostic pipelines exist permanently and touch no external API:

| Key | Proves |
|---|---|
| `demo_noop` | Claiming, running, step tracking and run records all work |
| `demo_fail` | Retry backoff, and the Telegram alert once attempts are exhausted |
| `demo_budget` | `BudgetGuard` defers instead of executing when tokens are short |

### Checking the external APIs

```powershell
uv run python manage.py ping_clients
uv run python manage.py ping_clients --skip-telegram   # don't post to the channel
```

Makes one live call to Keepa, OpenAI and Telegram and prints tokens left, cost and latency. Run it
first whenever a pipeline starts failing — it separates "our bug" from "their API". It costs about
20 Keepa tokens and a few thousandths of a cent, and it posts a message to the channel unless you
skip it.

---

## 5b. The catalogue (ingestion)

Four pipelines fill and maintain `catalog_product`. Normally the worker runs them on their cron and
you never touch them; these are the manual equivalents.

| Pipeline | Cron | Per run | What it does |
|---|---|---|---|
| `seed_products` | `0 */6 * * *` | 50 | Keepa Product Finder on the least-covered gift category → creates empty `Product` stubs. ~11 tokens. |
| `hydrate_products` | `*/5 * * * *` | 25 | Fetches full data for stubs that have never been fetched. **Exactly 4 tokens per ASIN.** |
| `refresh_products` | `0 */2 * * *` | 25 | Re-fetches anything older than 30 days. Skips parked products. 4 tokens per ASIN. |
| `recompute_derived` | `20 1 * * *` | 5000 | Re-derives bands, `sales_rank_pct`, `quality_score` and re-applies the quality gate. **Zero tokens.** |
| `enrich_products` | `*/15 * * * *` | 10 | LLM writes 6–8 synthetic gift queries per product, then embeds them. **~$0.001 per product.** |

```powershell
uv run python manage.py run_pipeline seed_products
uv run python manage.py run_pipeline hydrate_products --max-items 25
uv run python manage.py run_pipeline refresh_products --payload '{\"refresh_after_days\": 0}'
uv run python manage.py run_pipeline recompute_derived
```

```powershell
# Enrichment. Costs money, so these are the ones to be deliberate about.
uv run python manage.py run_pipeline enrich_products --max-items 10
uv run python manage.py run_pipeline enrich_products --payload '{\"retry_failed\": true}'
uv run python manage.py run_pipeline enrich_products --payload '{\"reenrich\": true}' --max-items 20
```

**Pace.** Keepa refills 21 tokens/minute ≈ 1 260/hour, and hydration costs 4 tokens per product, so
the hard ceiling is ~300 products/hour. The crons above are deliberately well under it: we want a
queue that never empties, not a saturated token budget.

### Changing the standards

Three places control what gets into the catalogue. **After changing any of them, run
`recompute_derived`** — it re-applies the gate to the whole catalogue for free, so raising standards
cleans the site retroactively and lowering them un-parks products without re-fetching from Keepa.

1. **Which categories** — `GIFT_SUITABLE_IDS` in `apps/catalog/categories.py`. 15 of Amazon's 36
   Spanish roots are flagged gift-suitable. Libros, Moda, Alimentación and the digital storefronts
   are deliberately excluded.
2. **What counts as a gift** — `BLOCKED_PRODUCT_GROUPS` and `BLOCKED_BINDINGS` in the same file.
   These match Keepa's `type` and `binding` fields (`BATTERY`, `PHYSICAL_MOVIE`, `blu_ray`, …) and
   catch media and consumables that are filed under an allowed category. If junk appears in the
   catalogue, find its `product_group` in Admin and add it here.
3. **Quality floors** — `QualityGate` in `apps/catalog/services.py`: rating ≥ 4.2, ≥ 150 reviews,
   ≥ €15, must have images and a brand. Per-run overrides are available via
   `--payload '{\"min_rating\": 4.5}'`.

**Price bands** are the exception: they live in Admin under **Search → Configuración de ranking**
(`price_band_economico_max_cents`, `price_band_medio_max_cents`) so they can be changed without a
deploy. Run `recompute_derived` afterwards.

### Changing what products are tagged with

`apps/catalog/vocabularies.py` holds the three controlled vocabularies — occasions, recipients,
interests. They are the shared language of the site: the enrichment LLM may only tag with these
keys, advanced search offers exactly these as chips, and topics are tagged with them too.

**Never rename a key** once products are tagged with it — add a new one instead. Renaming silently
orphans every product carrying the old key. After adding keys or editing the prompt in
`apps/catalog/enrichment.py`, re-run enrichment with `{"reenrich": true}`; unlike `recompute_derived`
this one costs money (~$0.001 per product), so do it in batches with `--max-items`.

### Reading the result

`Product.enrichment_status` tells you why a product is or is not served:

- `PENDING` — passed the gate, waiting for enrichment. This is the servible catalogue.
- `SKIPPED` — parked by the gate. `availability_note` says why, in Spanish. Never served, never
  refreshed, never deleted.
- `DONE` — enriched and embedded. **Only these can be returned by search.**
- `FAILED` — the model could not write usable queries for it. Not retried automatically; the reason
  is in the pipeline run's `generate` step context.
- `is_active = False` — different thing entirely: Keepa no longer returns this ASIN. Rows are never
  deleted because `ClickEvent` protects them.

Prices are stored in `price_cents` for banding and diversity only. **Never render a price
anywhere** — Amazon Associates terms only permit displaying prices obtained through PA-API.

---

## 5c. Search

### Trying a query

```powershell
uv run python manage.py search "regalo para mi madre que le gusta la jardineria"
uv run python manage.py search "juguete creativo" --limit 5 --explain --no-cache
```

`--explain` prints the facet that matched, which is usually the fastest way to understand why a
product appeared. `--no-cache` bypasses `SearchResultCache`; use it whenever you are tuning,
otherwise you are grading yesterday's ordering.

Each result shows its full score breakdown:

```
sim=0.873 lex=1.000 qual=0.836 ctr=0.000 fresh=1.000 -> 0.8102  [fuzzy+lexical+semantic]
```

- `sim` — cosine similarity to the best matching facet. The honest relevance number.
- `lex` — the product's fused rank score, rescaled against the best in the pool.
- `qual` — `quality_score`, from rating, review count and sales rank.
- `ctr` — click-through performance. **0.000 for everything until Phase 9 ships tracking.**
- `fresh` — decays from 1.0 to a 0.6 floor over ~90 days since the last Keepa fetch.
- The bracket lists which of the three retrieval arms found it.

### Tuning the ranking

```powershell
uv run python manage.py search_report              # 40 queries, full output
uv run python manage.py search_report --quiet      # just the summary
```

Change a weight in Admin → *Búsqueda* → *Configuración de ranking*, re-run, compare. The summary
reports the three things that actually indicate breakage: **duplicated variation families** (must
always be zero), **zero-result queries**, and **latency**. Nothing needs a deploy or a restart —
`RankingConfig` is read on every search.

Weights are currently at their defaults and deliberately untuned: with a catalogue this small
there is not enough signal to tune them honestly. Revisit once there are thousands of enriched
products and real click data.

### Two things that will bite you

**The FTS config must match the index.** `retrieval.FTS_CONFIG` is `spanish_unaccent`, created in
catalog migration `0005`. If you change one and not the index on `ProductFacet`, Postgres will not
error — it will quietly stop using the index and sequential-scan every facet.

**The relevance floor is load-bearing.** `retrieval._is_relevant` drops any candidate that has
neither a semantic match above `min_facet_similarity` nor a full-text hit. Remove it and the
highest-quality products in the catalogue start answering every unrelated query, because without
a similarity score they are ranked on quality alone. If results suddenly look plausible-but-wrong,
check this first.

### Latency

An uncached search is dominated by the OpenAI embedding call (~375 ms warm, ~7 s on the very first
call of a process — which is why the client is shared and warmed at startup). Locally, add ~79 ms
per database round trip and there are six; on a dyno next to the database that part is negligible.
Cached searches skip the embedding entirely.

---

## 5d. Topics

A Topic is a landing page: a gift intent (`regalos para madres`), its facets, and a precomputed,
ranked list of products.

### Creating topics

```powershell
uv run python manage.py seed_topics --embed      # the 12 hand-written starters, idempotent
```

Everything else is Admin → *Temas*. A topic needs a `title`, a `slug`, and at least one facet
(`occasions` / `recipients` / `interests`). Facets are **hard filters**, so adding one narrows the
page — use the vocabulary keys from `apps/catalog/vocabularies.py`, never free text.

`canonical_text` and `embedding` are computed; don't hand-edit them. A topic without an embedding
cannot be matched by a user search.

### Filling and publishing a topic

```powershell
uv run python manage.py run_pipeline curate_topics                       # stale topics only
uv run python manage.py run_pipeline curate_topics --payload '{\"force\": true}'
uv run python manage.py run_pipeline decompose_topic --max-items 5       # topic -> Amazon keywords
uv run python manage.py run_pipeline run_search_terms --max-items 10     # keywords -> products
```

The loop is: **curate** → see which topics are starved → **decompose** them into keywords →
**run the terms** to ingest matching products → wait for hydration and enrichment → **curate
again**. That is how you fix a thin page, and it is far more targeted than seeding a whole
category.

`curate_topics` costs no Keepa tokens and no LLM money once topic embeddings exist.

### Reading the quality gate

`is_indexable` controls `noindex` and the sitemap. It requires **all** of: ≥12 linked products
scoring ≥0.55, ≥4 distinct root categories, ≥3 distinct brands, a non-empty intro, a human review,
and not being merged away. A failing topic **stays live for users** — it just isn't indexed.

```powershell
uv run python manage.py run_pipeline curate_topics --payload '{\"force\": true}'
```

then check `linked_count`, `distinct_categories`, `distinct_brands` in Admin. The usual failure is
too few categories, which means the catalogue is too narrow, not that the topic is bad.

A topic that *loses* `is_indexable` fires a Telegram warning — that is a live page leaving Google,
and it should never pass unnoticed.

### Pinning and excluding

In Admin → *Productos en tema*, `is_pinned` forces a product to the top and `is_excluded` removes
it. **Both survive every recompute** — a recompute updates the machine's opinion, never the
editor's. Excluded products stay in the table but are dropped from the page and from
`linked_count`.

### Duplicates

`dedupe_topics` flags pairs with cosine ≥0.95 and sends a Telegram warning. It **never merges on
its own**; set `merged_into` in Admin after checking, because a wrong merge cascades into
redirects that are painful to unwind.

---

## 5e. The public site

### Pages and who may index them

| Route | Page | Robots |
|---|---|---|
| `/` | Home | index |
| `/regalos/ocasiones|para-quien|aficiones|presupuesto/` | Hubs | index |
| `/regalos/<slug>/` | Topic | index **only if** `topic.is_indexable` |
| `/buscar/` | Simple search | `noindex, follow` |
| `/buscador-avanzado/` | The filter tool | index |
| `/buscador-avanzado/resultados/` | Advanced results | `noindex, nofollow` |
| `/afiliados/` | Affiliate disclosure | index |
| `/aviso-legal/`, `/privacidad/`, `/cookies/`, `/contacto/` | Legal | `noindex, follow` |
| `/go/<product_id>/` | Outbound redirect | blocked in `robots.txt` |

The rule behind the table: **SEO value stays on a finite, quality-gated set of pages.** Search
results are machine-made and unbounded, so they never enter the index. If a query matches a
curated topic strongly, `/buscar/` 302s to that topic instead of serving a thin copy of it.

### Why the home page and hubs can look empty

Home, the hubs and the zero-result suggestions all filter on
`status=ACTIVE, is_indexable=True, merged_into__isnull=True`. Freshly seeded topics are `DRAFT`
and fail the §9.2 gate, so **empty is the correct output, not a bug.** A topic page itself renders
regardless — go to `/regalos/<slug>/` directly.

To see a populated home page before the gate passes naturally, promote one by hand in Admin (or
the shell): set `status=ACTIVE` and `is_indexable=True`. Do this knowingly — an indexable topic
with fewer than 12 diverse products is exactly the thin page the gate exists to prevent.

**A hand-promoted topic does not stay promoted.** `curate_topics` runs nightly at 05:00 UTC and
re-applies the gate, so anything still failing it drops back to `is_indexable = False` (and fires a
"topic dropped out of index" Telegram warning). That is the gate working, not a bug. Re-run the
promotion, or disable the `curate_topics` schedule in Admin while you are demoing.

```powershell
@'
import django, os, logging
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()
logging.disable(logging.CRITICAL)
from apps.topics.models import Topic, TopicStatus
for t in Topic.objects.filter(linked_count__gte=3):
    t.status = TopicStatus.ACTIVE
    t.human_reviewed = True
    t.is_indexable = True
    t.save(update_fields=["status", "human_reviewed", "is_indexable", "updated_at"])
    print("ACTIVE", t.slug, t.linked_count)
'@ | uv run python - 2>$null
```

### Outbound links

Every Amazon link on the site — image, title, and all three CTAs — goes through
`/go/<product_id>/?b=<button>&p=<placement>&pos=<position>&t=<topic_id>`, carries
`rel="nofollow sponsored"` and opens in a new tab. The view writes a `ClickEvent`, then 302s to
the tagged URL with `ascsubtag=rm-<placement>-<topic>-<click>`, which is what lets Amazon's report
be joined back to a placement.

To check tracking is alive:

```powershell
uv run python manage.py shell -c "from apps.tracking.models import ClickEvent; print(ClickEvent.objects.count())"
```

**Never display a price anywhere**, including `price_cents`. Amazon's affiliate terms forbid
showing a cached price, and ours go stale within minutes. Cards show a band (económico / medio /
premium) and nothing more.

### After changing any template

`static/css/site.css` is committed and CI fails if it is stale, so any new Tailwind class needs a
rebuild:

```powershell
.\scripts\build_css.ps1
```

Tailwind writes its progress banner to stderr, so PowerShell reports "exited with code 1" on a
successful build. Confirm with `Get-Item static\css\site.css` instead of trusting the exit code.

### Smoke-testing every route

```powershell
@'
import django, os, logging
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()
logging.disable(logging.CRITICAL)
from django.conf import settings
settings.ALLOWED_HOSTS = ["*"]
from django.test import Client
c = Client()
for u in ["/", "/regalos/ocasiones/", "/buscar/", "/buscador-avanzado/", "/afiliados/",
          "/robots.txt", "/sitemap.xml", "/healthz/", "/no-existe/"]:
    r = c.get(u)
    print(r.status_code, u)
'@ | uv run python - 2>$null
```

`ALLOWED_HOSTS` must be widened in the snippet or every request returns **400**, not 404 — the
test client uses the host `testserver`. And if you want to inspect `response.context` rather than
the HTML, call `django.test.utils.setup_test_environment()` first; without it `response.context` is
`None`, because the signal that captures it is only connected under the test runner.

### Before launch

The legal templates contain `[PENDIENTE: …]` markers for the operator's name, NIF, address and
contact email. These are required by art. 10 LSSI-CE and **must be filled in before the site is
public.**

---

## 6. Database access

No `psql` is installed. Use an ephemeral psycopg session:

```powershell
@'
import os, psycopg
url = [l.split("=",1)[1].strip() for l in open(".env") if l.startswith("DATABASE_URL=")][0]
with psycopg.connect(url, sslmode="require", connect_timeout=20) as conn, conn.cursor() as cur:
    cur.execute("select pg_size_pretty(pg_database_size(current_database()))")
    print("DB SIZE:", cur.fetchone()[0])
'@ | uv run python -
```

Or through Django, which is usually easier:

```powershell
uv run python manage.py shell
uv run python manage.py dbshell      # only works if psql is on PATH
```

**Watch the size.** The Heroku `essential-0` plan caps at **1 GB** and 20 connections. Embeddings
are `halfvec(512)` specifically to fit; if the database approaches ~800 MB, see blueprint §10 for
the upgrade path.

---

## 7. Heroku

The app is `regalosmejores` (EU region). Nothing is deployed from a laptop — pushes to `main` go
through GitHub Actions, which runs the health check and rolls back automatically if
`/healthz/` does not return `ok`.

```powershell
heroku logs --tail --app regalosmejores
heroku ps --app regalosmejores
heroku releases --app regalosmejores
heroku config --app regalosmejores
heroku pg:info --app regalosmejores
```

Before the first deploy, the config vars listed in blueprint §13.A must be set. Migrations run
automatically in the Procfile `release` phase; a failing migration aborts the release.

---

## 8. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `django.core.exceptions.ImproperlyConfigured: Set the DJANGO_SECRET_KEY` | `.env` missing or incomplete — compare against `.env.example` |
| CI fails on "Tailwind CSS is up to date" | Rebuild `static/css/site.css` and commit it |
| CI fails on `makemigrations --check` | You changed a model without generating the migration |
| `uv sync --locked` fails in CI | `uv.lock` was not committed after a dependency change |
| Tailwind binary "not a valid application" | The download was truncated — delete `.tools/` and rebuild (the script uses `curl.exe` for this reason) |
| Job queue backlog grows | Worker is down (`heroku ps`), or `PIPELINES_ENABLED=false`, or Keepa tokens are exhausted |
| Deploy rolled back | The health check failed — read `heroku logs` for the release phase; a failed migration is the usual cause |
