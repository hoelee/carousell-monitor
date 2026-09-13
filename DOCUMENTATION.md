# carousell-monitor — Deployment & Operations Documentation

Carousell new-listing monitor: watches Carousell search pages (sorted by *recent*),
archives every listing into a NocoDB base (with image thumbnail), and alerts Telegram
when a genuinely new listing appears. Runs 24/7 as a Docker container on the DSM.

---

## 1. Architecture

```
Docker container (carousell-monitor, DSM, network bridge_hoelee)
  ├─ every 60 s (TICK_SECONDS) ─ reads watch list from NocoDB Settings table
  ├─ per watch (on its own check_interval_minutes) ─ GET the Carousell search URL
  │     ├─ parse embedded JSON state → SearchListing.listingCards[]
  │     ├─ dedupe by product_url (param-less listing URL)
  │     ├─ INSERT new rows into NocoDB Listings table
  │     └─ archive w/ notified flag (first seed = silent; else pending-notify)
  ├─ end of each tick ─ send ALL pending (notified=false) listings, 1s apart
  │     └─ each notice: photo + title/price/condition/seller/url
  ├─ writes /data/health.json each tick → Docker HEALTHCHECK
  └─ reaches: NocoDB http://nocodb:10380 (container DNS, bridge_hoelee)
              Carousell www.carousell.com.my (public internet)
              Telegram api.telegram.org (public internet)
```

Nothing is pushed to any image registry — the image is built **privately on DSM**
(`build: .`). Secrets come from the `.env` file next to the compose file.

---

## 2. Files in this folder

| File | Purpose |
|---|---|
| `docker-compose.yml` | Stack definition (build, env, volume, network) |
| `Dockerfile` | python:3.11-alpine + monitor.py + healthcheck.py, HEALTHCHECK |
| `monitor.py` | Main loop: schema bootstrap, fetch/parse, NocoDB IO, Telegram |
| `healthcheck.py` | HEALTHCHECK probe (reads `/data/health.json`) |
| `.env` | Secrets + tunables (gitignored, NOT in the repo) |
| `DOCUMENTATION.md` | This file |

Source of truth for the code is the **private Gitea repo**
`git.hoelee.com/hoelee/carousell-monitor` (local checkout `D:\dev\carousell-monitor`).
Credentials are documented in `SECRETS.md` there.

---

## 3. Environment variables (`.env`)

| Var | Value / default | Notes |
|---|---|---|
| `NOCODB_URL` | `http://nocodb:10380` | container DNS on `bridge_hoelee`; LAN form `http://192.168.137.2:10380` |
| `NOCODB_TOKEN` | *(secret)* | workspace-scoped NocoDB PAT |
| `NOCODB_BASE_ID` | `poqw1zjw3hnsk37` | base "Carousell" |
| `TELEGRAM_BOT_TOKEN` | *(secret)* | @carousellFoundBot |
| `TELEGRAM_CHAT_ID` | `5648309582` | alert destination |
| `TICK_SECONDS` | `60` | scheduler granularity |
| `HEALTH_STALE_SECONDS` | `600` | healthcheck staleness window |

**Secrets = env vars (`.env`). Operational knobs = NocoDB Settings table.**
Speed, enable/disable, and notify on/off are all changed from the NocoDB UI — no
redeploy, no `.env` edit.

---

## 4. NocoDB schema (base `Carousell`, id `poqw1zjw3hnsk37`)

The container bootstraps both tables on startup if they are missing (idempotent —
deleting a table is safe; it is recreated on the next start).

### `Listings` (archive — every seen listing)

| Column | Type | Purpose |
|---|---|---|
| `Id` | auto | primary key |
| `product_url` | URL | **unique dedupe key** — `https://www.carousell.com.my/p/<id>/`, no query params |
| `title` | SingleLineText | listing title |
| `price` | Decimal | numeric price, "RM" stripped (e.g. `85.00`) — filterable/sortable |
| `condition` | SingleSelect | Brand new / Like new / Lightly used / Well used / Heavily used / Used (归一化自 Carousell 的 New/Used 等写法) |
| `image_url` | URL | 高清图 URL（已去 `_progressive_thumbnail` 后缀） |
| `image` | Attachment | 高清图 — NocoDB hotlinks the URL; renders in grid view |
| `seller_name` | SingleLineText | seller username |
| `seller_url` | URL | `https://www.carousell.com.my/u/<username>/` |
| `search_title` | SingleLineText | which watch found it (denormalized) |
| `search_url` | URL | the watch's search URL |
| `listed_at` | DateTime (UTC) | 上架时间（优先 `time_created`，被顶置商品 fallback `active_bump`） |
| `first_seen_at` | DateTime (UTC) | when the monitor first captured it |
| `notified` | Checkbox | false = 待发通知；发完/静默归档后置 true（防重复通知） |

### `IgnoredSellers` (global seller blocklist)

| Column | Type | Purpose |
|---|---|---|
| `seller_name` | SingleLineText | seller username to silence globally (any watch) |

### `IgnoredKeywords` (per-watch title blocklist)

| Column | Type | Purpose |
|---|---|---|
| `search_url` | URL | **copy the `url` from a `Settings` row** — which watch this keyword applies to |
| `keyword` | SingleLineText | if the listing **title** contains this (case-insensitive substring), skip the Telegram alert (still archived) |

One keyword per row; add multiple rows for multiple keywords. A keyword only
silences listings found by the watch whose `Settings.url` matches `search_url` —
the same keyword never applies to other watches. Empty rows are ignored.

### `Settings` (the watch list — you manage this)

| Column | Type | Purpose |
|---|---|---|
| `Id` | auto | primary key |
| `title` | SingleLineText | human label, e.g. "Uniform" |
| `url` | URL | full Carousell search URL (with `sort_by=3`) |
| `enabled` | Checkbox | false = paused (skipped entirely) |
| `notify` | Checkbox | false = archive only, no Telegram ping |
| `check_interval_minutes` | Number | per-watch poll interval (default 5) |
| `last_checked_at` | DateTime | null = not yet seeded (first run archives silently) |

---

## 5. Runtime behaviour

- **Tick loop**: every `TICK_SECONDS` the container reloads the Settings table and
  polls each *enabled* watch whose interval has elapsed.
- **Dedupe**: `product_url` is the identity. On startup the container loads every
  existing `product_url` from NocoDB into an in-memory set; a listing is "new" only
  if its URL is not in that set.
- **First run per watch** (`last_checked_at` is null): seeds the current listings as an
  archive with `notified=true` (silent, no Telegram). Prevents a flood on setup.
- **Afterwards**: new listings are inserted with `notified=false` (pending). At the end
  of each tick the monitor sends every `notified=false` listing **one message each**
  (photo + title/price/condition/seller/url), 1 second apart, then sets `notified=true`.
- **Silencing**: a pending listing is marked `skip_notify=true` + `notified=true`
  (no Telegram) if its `seller_name` is in `IgnoredSellers` (global), or if its
  **title** contains any keyword whose `IgnoredKeywords.search_url` matches the
  listing's own `search_url` (per-watch, case-insensitive).
- **Failure handling**: a fetch/parse error on any watch marks that tick failed; the
  container becomes **unhealthy** until the next fully-successful tick. `last_checked_at`
  is only advanced on success, and a hard-failing watch is rate-limited to one attempt
  per tick so it doesn't hammer the site.

---

## 6. Operations (all from the NocoDB UI — no SSH, no redeploy)

| Want to… | Do this |
|---|---|
| Add a keyword | Add a row to `Settings`: `title`, full `url` (open Carousell → search → sort "Recent" → copy URL), `enabled` ✓, `notify` ✓, interval |
| Remove / pause a watch | Set `enabled` = false (or delete the row) |
| Stop Telegram pings but keep archiving | Set `notify` = false |
| Ignore a seller everywhere | Add `seller_name` row in `IgnoredSellers` |
| Ignore certain keywords for **one watch** | Add row(s) in `IgnoredKeywords`: `search_url` = copy of that watch's `Settings.url`, `keyword` = e.g. `nike` (case-insensitive, matches inside the title) |
| Change how often it checks | Edit `check_interval_minutes` (5 = every 5 min) |
| See what's new | Open `Listings`, sort by `first_seen_at` desc |
| Browse with images | `Listings` grid view — the `image` column renders thumbnails |

The URL for a watch must keep `sort_by=3` (recent) so new listings sort first, e.g.:

```
https://www.carousell.com.my/search/uniform?addRecent=true&canChangeKeyword=true&includeSuggestions=true&sort_by=3&t-search_query_source=direct_search
```

---

## 7. Updating the code

The image is built **on DSM** from this folder. To ship a code change:

```bash
# 1) edit monitor.py / Dockerfile / compose in the repo (D:\dev\carousell-monitor)
# 2) copy the changed file(s) to this folder, then:
cd /volume1/docker/carousell-monitor
sudo /usr/local/bin/docker compose up -d --build
```

`--build` rebuilds the image and recreates the container; the NocoDB schema bootstrap
and dedupe seeding are idempotent, so a rebuild never duplicates rows.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Container shows **unhealthy** | last tick failed (403/429/parse) or >10 min stale | `sudo /usr/local/bin/docker logs carousell-monitor --tail 50` — read the `error` |
| "no application/json state found" | Carousell rate-limited / blocked the request | wait; consider raising `check_interval_minutes` |
| No Telegram messages | token/chat wrong, or `notify` off | verify `.env` `TELEGRAM_BOT_TOKEN`; check `notify` checkbox |
| Thumbnails missing in NocoDB | `image` column isn't Attachment, or Carousell blocked hotlink | confirm `image` is Attachment type; `image_url` (URL) always works as a link |
| NocoDB unreachable | DSM IP drifted, or not on `bridge_hoelee` | from DSM: `sudo /usr/local/bin/docker network inspect bridge_hoelee`; NocoDB container must be named `nocodb` |
| `listed_at`/`first_seen_at` look 8 h off | they are **UTC** (NocoDB parses naive datetimes as UTC) | expected — set your NocoDB display timezone to MYT (Asia/Kuala_Lumpur) if you want local times |

### Handy commands (run on DSM)

```bash
D="sudo /usr/local/bin/docker"
$D ps --filter name=carousell-monitor          # status + health
$D logs carousell-monitor --tail 100           # last log lines
$D inspect carousell-monitor --format '{{.State.Health.Status}}'   # health
$D restart carousell-monitor                   # restart (state is in NocoDB, safe)
```

Health file lives at `/data/health.json` inside the container:
`{"last_run_epoch": ..., "ok": true, "error": "", "watch_count": N, "new_this_tick": N}`.

---

## 9. Key identifiers

| Item | Value |
|---|---|
| Repo (private) | `git.hoelee.com/hoelee/carousell-monitor` |
| Local checkout | `D:\dev\carousell-monitor` |
| DSM deploy dir | `/volume1/docker/carousell-monitor` |
| Container | `carousell-monitor` (network `bridge_hoelee`) |
| NocoDB base | `Carousell` = `poqw1zjw3hnsk37` (workspace `wal4hatt`) |
| Tables | `Listings` + `Settings` (bootstrap finds by title) |
| Telegram | `@carousellFoundBot` → chat `5648309582` (@MrFullStackDev) |

---

## 10. Security notes

- `.env` contains secrets and is **gitignored** — it exists only on this DSM and is
  never committed. Credential inventory: `SECRETS.md` in the repo.
- The image is private (built on DSM, never pushed to Docker Hub).
- NocoDB token is workspace-scoped; regenerate in NocoDB if it leaks and update `.env`.

---

## 11. Changelog

- **2026-09-13** 新增 per-watch 忽略关键词：`IgnoredKeywords` 表（`search_url` +
  `keyword`）。标题命中该 watch 关键词（大小写不敏感子串）时静默归档、不发
  Telegram（`skip_notify=true`）。`search_url` 需与 `Settings.url` 一致，实现按
  watch 隔离。
- **2026-09-08** 通知重构：每商品一条图文消息（title/price/condition/seller/url），归档与通知解耦（`notified` 列 + tick 末尾统一发 + 1s 间隔）。图片改用高清 URL（去 `_progressive_thumbnail`）。condition 归一化（New→Brand new、Used→Used，加第 6 档）。listed_at 加 `active_bump` fallback。修复 Telegram IPv6/DNS 问题（compose `extra_hosts` 钉 IPv4）。bot 换 `@carousellFoundBot`。
