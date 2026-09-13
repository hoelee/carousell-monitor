# carousell-monitor

Watches Carousell search pages (sorted by *recent*) for new listings, archives every
listing to a NocoDB base (with image URL + thumbnail), and alerts Telegram.

## How it works

- `monitor.py` runs in a Docker container on DSM, self-bootstrapping its NocoDB
  schema (`Listings` + `Settings` + `IgnoredSellers` + `IgnoredKeywords` tables)
  and looping forever.
- Every `TICK_SECONDS` it reads the watch list from the **Settings** table and polls
  each enabled watch's URL on its own `check_interval_minutes`.
- Dedupe key = `product_url` (param-less listing URL). First run per watch = seed
  archive only (no Telegram). After that, new listings are archived and alerted as
  `"<title>: N new listings"`.
- The container marks itself **unhealthy** (Docker healthcheck) if a tick fails to
  extract / gets rate-limited / crashes.

## Schema

**Listings** — `product_url` (unique), `title`, `price` (numeric), `condition`
(SingleSelect), `image_url`, `image` (Attachment → thumbnail), `seller_name`,
`seller_url`, `search_title`, `search_url`, `listed_at`, `first_seen_at`,
`notified`, `skip_notify`.

**Settings** — `title`, `url`, `enabled`, `notify`, `check_interval_minutes`,
`last_checked_at`. Add/remove watches here from the NocoDB UI; no redeploy needed.

**IgnoredSellers** — `seller_name`. Add/remove sellers here to suppress Telegram
alerts for their listings (still archived, marked `skip_notify=true`).

**IgnoredKeywords** — `watch` (Link → Settings) + `keyword`. Per-watch title
blocklist: pick the watch from a dropdown, add one keyword per row. A keyword only
applies to listings from the linked watch; case-insensitive substring match against
the title. Still archived.

## Run

```bash
# local (against LAN NocoDB)
NOCODB_URL=http://192.168.137.2:10380 \
NOCODB_TOKEN=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
python monitor.py

# docker
docker build -t hoelee/carousell-monitor:latest .
docker compose up -d
```

## Deploy (DSM via Portainer stack 240)

Private build — no registry. The container is owned by **Portainer stack 240**
(standalone; compose + build context at `/volume1/docker/portainer/compose/240/`
on DSM), deployed with secrets passed as stack environment variables.

```bash
# on DSM — rebuild the image only when monitor.py / Dockerfile changed
sudo /usr/local/bin/docker build -t carousell-monitor:latest \
  /volume1/docker/portainer/compose/240
```

Then update stack 240 via the Portainer API (`PUT /api/stacks/240?endpointId=2`,
repo compose as `stackFileContent` + current env array — see `portainer-api`
skill; ⚠ never echo masked `***` env values back). The old
`/volume1/docker/carousell-monitor` dir is legacy — do not `compose up` there.

## Files

- `monitor.py` — main loop, schema bootstrap, fetch/parse, NocoDB IO, Telegram.
- `healthcheck.py` — Docker HEALTHCHECK probe (`/data/health.json`).
- `Dockerfile`, `docker-compose.yml`, `.env.example`.
- `COMPOSE-SETUP.md` — stack anatomy: compose file, Dockerfile, networking, deploy paths.
- `AGENTS.md` — AI-agent entry. `SECRETS.md` — credentials (private repo).
