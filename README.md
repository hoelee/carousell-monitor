# carousell-monitor

**Watch Carousell searches, archive every listing into NocoDB, and get a Telegram alert the moment something new appears.**

Self-hosted and free: one docker compose file, no scraping API, no paid service, no account anywhere except your own bot. Built for people who are tired of refreshing search pages all day — sneaker drops, camera gear, used furniture, car parts, uniform lots, whatever you are hunting for.

`Python 3.11 · stdlib only` · `Docker / Portainer` · `NocoDB` · `Telegram` · `MIT`

---

## What it does

- **Watches** any Carousell search URL you give it (sorted by *Recent*), on its own interval.
- **Archives** every listing it sees into NocoDB — title, numeric price, condition, seller, link, and the product photo (grid view shows thumbnails). Deduplicated by product URL, so nothing is stored twice.
- **Alerts** you on Telegram, one photo message per new listing, with the full details and a clickable link.
- **Stays quiet about the past.** The first time it sees a search it archives everything silently, so you are not flooded with 200 messages on setup. Only listings that appear *after* that first pass are alerted.
- **Filters** — mute a seller everywhere, or mute keywords for one specific search (still archived, no alert).
- **Tells you when it breaks.** If the container can no longer fetch, or the loop dies, you get a Telegram failure alert (after N consecutive failures, debounced) and a recovery notice when it works again.

## How it works

```
        ┌──────────────────────── your machine / NAS / VPS ────────────────────────┐
        │                                                                          │
        │   docker network: carousell (private)                                    │
        │                                                                          │
        │   ┌───────────────────────────┐          ┌────────────────────────────┐  │
        │   │ carousell-monitor         │  NocoDB  │ nocodb                     │  │
        │   │ (python, no open ports)   │◄────────►│ (web UI + SQLite, :8080)   │  │
        │   └───────┬───────────┬───────┘  REST    └────────────────────────────┘  │
        │           │           │                                                  │
        └───────────┼───────────┼──────────────────────────────────────────────────┘
                    │           │
        every tick  │           │  new listing  ──►  Telegram alert (photo + details)
                    ▼           ▼
        Carousell search pages   api.telegram.org
```

The monitor is an **outbound-only worker**: no inbound port, no web UI, nothing to expose to the internet. All state lives in NocoDB; you drive it from the NocoDB UI.

Each tick it:

1. reads your watch list from the NocoDB `Settings` table,
2. fetches each search page that is due and extracts the embedded listing JSON,
3. inserts anything it has not seen before into `Listings`,
4. sends one Telegram message per still-unnotified listing and marks it notified.

More detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Requirements

| | |
|---|---|
| **Docker** | Engine 20.10+ with Compose v2 (`docker compose version`) — or Portainer, if you prefer a GUI |
| **Telegram** | a free account (you create the bot in 60 seconds, see below) |
| **Machine** | anything that stays on: NAS, VPS, Raspberry Pi, mini-PC, old laptop |
| **Skills** | being able to copy-paste commands and edit a text file |

Disk: a few hundred MB (NocoDB + the listings database, which grows slowly). RAM: NocoDB wants ~300 MB, the monitor ~40 MB.

---

## Quick start (CLI)

> Prefer clicking? Jump to [Deploy on Portainer](#deploy-on-portainer-gui) — same result, no shell needed.

### Step 1 — get the code and the env file

```bash
git clone https://github.com/hoelee/carousell-monitor.git
cd carousell-monitor
cp .env.example .env
```

### Step 2 — create your Telegram bot

Open Telegram, talk to [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts. You get a token that looks like `8123456789:AAF...`.

Now message [@userinfobot](https://t.me/userinfobot) and it replies with your numeric id.

Put both into `.env`:

```ini
TELEGRAM_BOT_TOKEN=8123456789:AAF...
TELEGRAM_CHAT_ID=123456789
```

Full walkthrough, including how to alert a **group** instead of yourself: [`docs/TELEGRAM-SETUP.md`](docs/TELEGRAM-SETUP.md).

### Step 3 — start NocoDB and create the base

```bash
docker compose -f docker-compose.allinone.yml up -d nocodb
```

Wait ~30 seconds, then open **http://localhost:8080** (replace `localhost` with your server's address if you are deploying remotely).

1. Create your account (the first account is the admin — use a real email and a real password).
2. Create a **Base**, name it e.g. `Carousell`.
3. Copy the **base id** out of the browser URL — the long id after `/nc/base/`:

   ```
   http://localhost:8080/dashboard/#/nc/base/pbase0123456789ab/...
                                            ^^^^^^^^^^^^^^^ copy this
   ```
4. Create an **API token**: click your avatar (bottom-left) → *Account Settings* → *Tokens* → *Create token*. Copy it (it starts with `nc_pat_`). NocoDB only shows it once.

Put both into `.env`:

```ini
NOCODB_BASE_ID=pbase0123456789ab
NOCODB_TOKEN=nc_pat_...
```

While you are there, generate a session secret and put it in `.env` too:

```bash
openssl rand -hex 32     # paste the output into NC_AUTH_JWT_SECRET
```

> The tables themselves are created **by the monitor**, so right now your fresh base is empty and that is expected.

### Step 4 — start the monitor

```bash
docker compose -f docker-compose.allinone.yml up -d
docker compose -f docker-compose.allinone.yml logs --tail 20 carousell-monitor
```

First run builds the image (a minute or so). A healthy start prints one line:

```
ready: listings=... settings=... ignored_sellers=... ignored_keywords=... seen=0
```

It is quiet after that — there is no per-tick logging by design. Progress is visible in NocoDB instead: four tables appear (`Listings`, `Settings`, `IgnoredSellers`, `IgnoredKeywords`), and the container reports `healthy`.

### Step 5 — tell it what to watch

1. Open your NocoDB base → the **`Settings`** table.
2. Add a row:

   | title | url | enabled | notify | check_interval_minutes |
   |---|---|---|---|---|
   | Uniform | *(paste a Carousell search URL)* | ✓ | ✓ | 5 |

3. To get a good URL: search on [carousell.com.my](https://www.carousell.com.my), set the sort to **Recent**, then copy the address bar. Make sure it contains `sort_by=3` — that is what puts the newest listings first:

   ```
   https://www.carousell.com.my/search/uniform?sort_by=3&...
   ```

That is it. The monitor re-reads `Settings` every tick, so no restart is needed after adding, editing or pausing a search.

### Step 6 — prove the alert works

Open the **`Listings`** table: your first pass has already archived the current listings (silently — no messages, that is correct). Then:

- **Send a test alert**: untick `notified` on any row and save. Within a tick the monitor re-sends that listing to Telegram. That is also the fastest way to debug a silent bot.
- **Watch a real one arrive**: the next genuinely new listing appears in `Listings` and lands in Telegram by itself.

Nothing in Telegram? See [Troubleshooting](#troubleshooting).

---

## Deploy on Portainer (GUI)

Portainer runs the same file — it is a normal compose stack. **One catch first:** the monitor's image is built from this repository (`build: .`), and Portainer's *Web editor* / *Upload* options have no build context, so they fail with:

```
compose build operation failed: failed to read dockerfile:
open /volume1/@docker/tmp/…/Dockerfile: no such file or directory
```

So pick one of these two routes — the first needs no shell at all.

### Route 1 — deploy from the Git repository (recommended)

Portainer clones the repo itself, so `build: .` resolves normally.

1. **Stacks → Add stack → Repository**.
2. **Name**: `carousell-monitor`.
3. **Repository URL**: `https://github.com/hoelee/carousell-monitor` · **Reference**: `refs/heads/main` · **Compose path**: `docker-compose.allinone.yml`.
4. **Environment variables** — add these (Portainer lists the ones the file references):

   | Variable | Value |
   |---|---|
   | `NC_AUTH_JWT_SECRET` | output of `openssl rand -hex 32` |
   | `TELEGRAM_BOT_TOKEN` | from @BotFather |
   | `TELEGRAM_CHAT_ID` | from @userinfobot |
   | `NOCODB_PORT` | optional, default `8080` |

   Leave `NOCODB_TOKEN` and `NOCODB_BASE_ID` **empty for now** — you cannot have them until NocoDB is running.
5. **Deploy the stack.** `carousell-nocodb` comes up healthy; `carousell-monitor` will restart in a short loop logging `NOCODB_TOKEN not set`. That is expected — stop that container for now if the noise bothers you.
6. Open `http://<your-host>:8080` and follow [Step 3](#step-3--start-nocodb-and-create-the-base): create the account, create a base, copy the base id, create an API token.
7. Back in Portainer: **Stacks → carousell-monitor → Editor** (or *Environment variables*) and fill in `NOCODB_BASE_ID` + `NOCODB_TOKEN`, then **Update the stack**. The monitor starts its real loop.

   > ⚠️ Portainer **masks secret-looking values that you type into its environment panel** and stores the mask (`***`) with the stack, so a value can silently stop working after a later stack update. If you hit that, put the real values directly in the YAML in the **Editor** tab instead of the panel.
8. Verify: **Containers** shows `carousell-nocodb` and `carousell-monitor` healthy, and the monitor's log shows the `ready: …` line.

### Route 2 — Web editor, with the image built by hand first

Use this if you cannot give Portainer access to your Git host, or you want the stack file to be self-contained.

```bash
# on the Docker host (or anywhere that can reach its daemon)
git clone https://github.com/hoelee/carousell-monitor.git
cd carousell-monitor
docker build -t carousell-monitor:latest .
```

Then **Stacks → Add stack → Web editor**, paste [`docker-compose.allinone.yml`](docker-compose.allinone.yml) and **delete the `build: .` line** from the monitor service — Portainer's web-editor stack directory contains no Dockerfile, so a `build:` entry can only fail there. Fill in the same variables as above, deploy NocoDB first, then add the base id and token and **Update the stack**.

Afterwards, updating the code means: `git pull && docker build -t carousell-monitor:latest .` on the host, then **Update the stack** in Portainer.

### Managing it afterwards

| Task | Where |
|---|---|
| Change a search / filters | NocoDB UI — nothing to redeploy |
| Change credentials or tuning | **Stacks → carousell-monitor → Editor** (or *Environment variables*) → **Update the stack** |
| See logs | **Containers → carousell-monitor → Logs** |
| Restart | **Containers → carousell-monitor → Restart** (state lives in NocoDB — nothing is lost) |
| Back up | **Volumes → nocodb-data** — that volume *is* your data |
| Upgrade NocoDB | change the image tag in the Editor → **Update the stack** (back up the volume first) |

---

## Everyday use (all from the NocoDB UI — no SSH, no restart)

Open your base and work in the tables:

| I want to… | Do this |
|---|---|
| Watch another search | Add a row to `Settings`: `title`, full `url` (with `sort_by=3`), `enabled` ✓, `notify` ✓, interval |
| Pause a search | Untick `enabled` (or delete the row) |
| Keep archiving but stop Telegram for a search | Untick `notify` |
| Check more or less often | Edit `check_interval_minutes` (5 = every 5 minutes) |
| Mute a seller everywhere | Add their username to `IgnoredSellers` |
| Mute keywords for **one** search | Add rows to `IgnoredKeywords`: pick the search in the `watch` dropdown, type the `keyword` (e.g. `nike`) |
| See what is new | `Listings`, sorted by `first_seen_at` (newest first) |
| Browse with pictures | `Listings` → grid view; the `image` column renders thumbnails |
| Hide an archived row from the alert queue | tick `notified` on it (pending rows are `notified = false`) |

Keyword matching is a case-insensitive **substring of the title**, and it only applies to the search you linked it to. Ignored sellers and ignored keywords are still archived — they just do not ring your phone.

### Filter fields at a glance

| Table | What it is | Fields you create |
|---|---|---|
| `Settings` | your watch list | `title`, `url`, `enabled`, `notify`, `check_interval_minutes` (the monitor maintains `last_checked_at`) |
| `IgnoredSellers` | global seller blocklist | `seller_name` |
| `IgnoredKeywords` | per-search title blocklist | `watch` (link → `Settings`), `keyword` |
| `Listings` | the archive — written by the monitor | read-only for you, except `notified` |

Full column reference: [`docs/NOCODB-SETUP.md`](docs/NOCODB-SETUP.md).

### What the alert looks like

```
🛒 Seiko 5 SNK809 automatic watch
💰 RM320
📦 Like new
👤 watchguy88
https://www.carousell.com.my/p/seiko-5-snk809-1234567890/
```

plus the listing photo above the text. The monitor downloads the image and uploads it to Telegram, so the alert still works if Carousell later blocks hotlinking.

---

## Configuration

Everything below goes in `.env` (CLI) or in the stack's environment (Portainer). Only the first five are required. The tuning knobs all have working defaults — ignore them until you have a reason.

| Variable | Default | What it does |
|---|---|---|
| `NOCODB_BASE_ID` | — | **required** — the NocoDB base holding the tables |
| `NOCODB_TOKEN` | — | **required** — NocoDB API token (`nc_pat_…`) |
| `TELEGRAM_BOT_TOKEN` | — | **required** — from @BotFather |
| `TELEGRAM_CHAT_ID` | — | **required** — your numeric id, or a group id (negative) |
| `NOCODB_URL` | `http://nocodb:8080` | where the monitor reaches NocoDB |
| `NC_AUTH_JWT_SECRET` | — | NocoDB session secret (all-in-one stack only); `openssl rand -hex 32` |
| `NOCODB_PORT` | `8080` | host port for the NocoDB web UI |
| `TICK_SECONDS` | `60` | how often the loop wakes up and re-reads the watch list |
| `FETCH_GAP_SECONDS` | `1` | minimum pause between two Carousell requests in one tick — keeps a burst of due searches from looking like an attack. `0` disables |
| `FAILURE_RATIO_THRESHOLD` | `1.0` | fraction of watches that must fail for the tick to count as failed. `1.0` = only if everything failed, so one soft-blocked search does not flip the container to unhealthy. `0` = never fail |
| `ERROR_ALERT_AFTER` | `3` | consecutive failed ticks before the Telegram failure alert (debounce); one recovery notice follows when it clears |
| `HEALTH_STALE_SECONDS` | `600` | a tick older than this marks the container unhealthy |
| `TZ` | `Asia/Kuala_Lumpur` | container clock; timestamps written to NocoDB are UTC on purpose |

Want a different schedule per search? That is `check_interval_minutes` in the `Settings` table, not an env var.

### Already running NocoDB? Use the monitor-only file

[`docker-compose.yml`](docker-compose.yml) deploys just the monitor and joins an **existing** Docker network (edit the network name and `NOCODB_URL` to match your NocoDB). That is the setup this project runs in production, next to a NocoDB used by other apps.

---

## Operating it

```bash
# status + health
docker compose -f docker-compose.allinone.yml ps

# logs (quiet unless something is wrong)
docker compose -f docker-compose.allinone.yml logs --tail 100 carousell-monitor

# restart (state lives in NocoDB — safe, nothing is lost)
docker compose -f docker-compose.allinone.yml restart carousell-monitor

# update to a newer revision of this repo
git pull && docker compose -f docker-compose.allinone.yml up -d --build

# stop everything (data stays in the volumes)
docker compose -f docker-compose.allinone.yml down
```

**Is it actually working?** The container writes `/data/health.json` every tick and the Docker HEALTHCHECK reads it:

```bash
docker exec carousell-monitor cat /data/health.json
# {"last_run_epoch": ..., "ok": true, "error": "", "watch_count": 3, "new_this_tick": 0, "failed_watches": 0}
```

`ok: true` and a recent `last_run_epoch` means the loop is alive. `ok: false` with an `error` means it is not fetching — read the error. Backups are the two volumes: `nocodb-data` (all your data) and `carousell-data` (the health file only).

Deeper: [`docs/OPERATIONS.md`](docs/OPERATIONS.md).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Container is `unhealthy` | last tick failed (Carousell blocked/rate-limited it, or NocoDB was unreachable) | `docker compose logs --tail 50 carousell-monitor` and read the `error`, then `cat /data/health.json` |
| Log says `NOCODB_TOKEN not set` | `.env` empty or the stack never picked it up | fill it in, then `up -d` again (Portainer: **Update the stack**) |
| `add column … failed` / `create table … failed` on startup | wrong `NOCODB_BASE_ID`, token without access to that base, or NocoDB still starting | check the base id, re-create the token in *that* base's workspace, make sure NocoDB is healthy first |
| No Telegram messages at all | token/chat id wrong, `notify` unticked, or the known Docker/IPv6 hang | untick/retick `notified` on a row to force a test send; check `getMe` with `curl https://api.telegram.org/bot<TOKEN>/getMe`; if the send times out, keep the `api.telegram.org` pin in `extra_hosts` (the all-in-one file already has it) |
| No Telegram for *one* search | `notify` unticked on that row, or the listing's seller/keyword is ignored | check the `Settings`, `IgnoredSellers`, `IgnoredKeywords` tables |
| `no application/json state found (blocked/ratelimited?)` | Carousell served a challenge page instead of results | raise `check_interval_minutes`, keep `FETCH_GAP_SECONDS ≥ 1`, and do not watch dozens of searches at once |
| `listingCards null (soft-block/ratelimit?)` | same thing, softer: Carousell answered 200 with empty state | same as above; the tick is retried, this is not a crash |
| Thumbnails missing in NocoDB | `image` column is not an Attachment column (edited?) | the monitor recreates columns on start — restart the container |
| Timestamps look 8 hours off | they are **UTC by design** | set NocoDB's display timezone to your local zone; the stored values stay UTC |
| NocoDB web UI unreachable | port clash or the container is not up | change `NOCODB_PORT`, or `docker compose ps` / read the NocoDB logs |

Still stuck? [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) has the full decision tree, including how to tell "not fetching" from "notifying" failures.

---

## FAQ

**Does this scrape or hammer Carousell?**
It performs one plain HTTP GET per search per interval (default every 5 minutes), with a 1-second gap between searches inside a tick, and it de-duplicates everything. It reads the same public search page your browser reads. Be a good citizen: do not set 30-second intervals on 40 searches.

**Can I get alerts for my own listings to test?**
Yes — post something (or edit an existing listing's title/price: an edit sometimes re-surfaces it), or simply untick `notified` on a row to replay an alert.

**Can I watch a category page or a seller's page instead of a search?**
Any Carousell page that renders the same listing grid works. Search URLs are what is tested.

**Multiple people / multiple searches?**
Everything is one monitor loop, one NocoDB base, one Telegram chat id. Add as many `Settings` rows as you like. For a second Telegram destination, run a second stack with its own bot and base.

**Do I need a reverse proxy / HTTPS?**
No — the monitor has no inbound port. If you want the NocoDB UI reachable from outside your LAN, put it behind your reverse proxy of choice (Synology/nginx/Traefik/Caddy) and keep the token out of the URL.

**Does it work outside Malaysia?**
It is written against `carousell.com.my` (Malaysia); the `.my` endpoints and the `RM` price format are baked in. Other Carousell country sites use the same page structure — change the two URL templates in `monitor.py` (`PRODUCT_URL_TMPL`, `SELLER_URL_TMPL`) and the search URL you paste into `Settings`.

**How do I upgrade NocoDB?**
Change the image tag in `docker-compose.allinone.yml`, `docker compose -f docker-compose.allinone.yml up -d`, and let it migrate. Back up `nocodb-data` first. The monitor only supports the meta API of the versions pinned in that file.

**Tests?**
```bash
python test_pagination.py    # stdlib only, no network; exit 0 = pass
```

---

## Project layout

```
monitor.py                     the whole monitor (stdlib only): bootstrap, fetch, NocoDB IO, Telegram
healthcheck.py                 Docker HEALTHCHECK probe (reads /data/health.json)
Dockerfile                     python:3.11-alpine, no dependencies, ~60 MB
docker-compose.allinone.yml    NocoDB + monitor — start here
docker-compose.yml             monitor only, joining an existing NocoDB on a shared network
.env.example                   every setting, explained
test_pagination.py             stdlib regression tests (run: python test_pagination.py)
docs/                          the long-form guides
AGENTS.md                      notes for AI coding agents working on this repo
SECRETS.example.md             credential template (the real values stay out of git)
```

### Guides

| Doc | Read it when |
|---|---|
| [`docs/QUICKSTART-PORTAINER.md`](docs/QUICKSTART-PORTAINER.md) | you want the click-by-click Portainer version |
| [`docs/TELEGRAM-SETUP.md`](docs/TELEGRAM-SETUP.md) | you need the bot token or a group chat id |
| [`docs/NOCODB-SETUP.md`](docs/NOCODB-SETUP.md) | you want to understand the tables before you fill them |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | you want to modify the code, or understand the compose file line by line |
| [`docs/OPERATIONS.md`](docs/OPERATIONS.md) | something is wrong, or it is time to back up / upgrade |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | you have a symptom and want the decision tree |

---

## Support this project

Built and maintained on my own time, and given away under MIT. If it saved you
money or hours, you can keep it alive:

- **[Sponsor on GitHub](https://github.com/sponsors/hoelee)** — one-off or recurring, through GitHub Sponsors.
- **[Buy me a coffee](https://buymeacoffee.com/hoelee)** — a one-off thank-you.

There are no sponsors-only features and there never will be: everything stays in
this repo, MIT. Starring it and reporting a bug are just as useful.

## Need this set up — or something built?

- **Setup service.** I deploy and configure carousell-monitor for you — NocoDB,
  your Telegram bot, your search list, your filters — on your own server or NAS,
  and hand it over working. Remote, fixed fee, quote on request.
- **Custom development.** Hoelee Enterprise builds websites, web apps, internal
  tools, scrapers/monitors, chat bots and API integrations. Python, PHP,
  Java/Spring, Docker/Linux, and Web3 (Solidity/ethers.js).
- **Ongoing maintenance.** Keep it running, upgrade NocoDB, add new search
  sources or delivery channels.

Tell me what you need and I will come back with a price and a timeline.

**Hoelee Enterprise** · WhatsApp [+60 12-797 2969](https://wa.me/60127972969) ·
[me@hoelee.com](mailto:me@hoelee.com) · [hoelee.com](https://hoelee.com)

---

## Contributing

Issues and pull requests are welcome. Two house rules before you send a patch:

1. **Open an issue first for anything beyond a typo** — this is a small, opinionated tool and the maintainer would rather agree on the shape before you write it.
2. **Do not break the tests.** `python test_pagination.py` must pass, and new behaviour wants a check added to it.

Please never commit credentials, host names or personal URLs. The repo ships no base id, token or chat id — `NOCODB_BASE_ID` and `NOCODB_TOKEN` are required settings and the monitor refuses to start (with a clear message) until they are set.

## Credits

Written and maintained by **Lee Teong Hoe** ([Mr Hoelee](https://hoelee.com)) — Hoelee Enterprise, Malaysia.
Built on the shoulders of [NocoDB](https://nocodb.com) and the [Telegram Bot API](https://core.telegram.org/bots/api).

## License

[MIT](LICENSE) — do what you want, no warranty.
