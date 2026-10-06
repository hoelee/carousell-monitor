# Architecture

How the pieces fit together, and what every line of the compose file and Dockerfile is doing. Read this before changing the code.

---

## 1. Runtime

```
carousell-monitor container (python:3.11-alpine, no ports, no web UI)
  │
  ├─ once at start ─ bootstrap(): create/fix the NocoDB tables, load every known
  │                  product_url into an in-memory seen-set
  │
  └─ every TICK_SECONDS (default 60s):
       ├─ read the Settings table (watch list)
       ├─ per watch whose check_interval_minutes has elapsed:
       │     GET the Carousell search page
       │     parse the embedded JSON state → SearchListing.listingCards[]
       │     keep only URLs not in seen
       │     INSERT them into Listings
       │        first pass for a watch (last_checked_at empty) → notified = true (silent seed)
       │        afterwards                                    → notified = false (queued)
       │     advance last_checked_at, then sleep FETCH_GAP_SECONDS
       ├─ send every queued listing (notified = false), one Telegram message each,
       │   applying the ignore filters, then mark it notified
       └─ write /data/health.json  →  the Docker HEALTHCHECK reads it
```

External calls: **Carousell** (search pages, and listing photos when alerting), **NocoDB** (REST on the private network), **Telegram** (Bot API). Nothing calls in.

## 2. The files

| File | Role |
|---|---|
| `monitor.py` | everything: config, HTTP, NocoDB schema bootstrap, extraction, IO, Telegram, health, alerting |
| `healthcheck.py` | Docker HEALTHCHECK probe — exits 0 only if `health.json` is fresh **and** `ok: true` |
| `Dockerfile` | `python:3.11-alpine`, copies the two scripts, declares the healthcheck |
| `docker-compose.allinone.yml` | NocoDB + monitor, private `carousell` network |
| `docker-compose.yml` | monitor only, joins an existing NocoDB network (production variant) |
| `test_pagination.py` | regression suite (stdlib, no network): NocoDB paging, the Telegram HTTP verb, failure thresholds |
| `.env.example` | the settings, documented |

## 3. Why it is built this way

**Two containers minimum.** NocoDB holds all state; the monitor is disposable. You can delete the monitor container, rebuild it, or change `monitor.py` and nothing is lost — the seen-set is rebuilt from `Listings` on start.

**Archive and notify are decoupled.** A listing is inserted with `notified = false`; a later pass sends it and flips the flag. So a Telegram outage or a wrong token never loses a listing — the queue just drains later. It also means "no alert" and "no data" are distinguishable failures.

**Filters are data, not config.** Ignored sellers and keywords live in NocoDB tables that are re-read every cycle, so muting something is a UI action, not a redeploy. Operational knobs (which searches, how often, whether to notify) are in `Settings`; only credentials and tuning live in the environment.

**First pass is silent.** `last_checked_at` empty means "never seeded": the current listings are archived with `notified = true`. Otherwise the first start would fire hundreds of messages.

**Dedupe on the canonical product URL**, `https://www.carousell.com.my/p/<id>/` — never the search URL and never the raw listing id alone. The search page's own links carry tracking parameters that change between loads.

**The failure threshold matters.** `FAILURE_RATIO_THRESHOLD` (default `1.0`) means a tick only counts as failed if *every* watch failed. Carousell soft-blocks individual searches from time to time; without this, one flaky search would mark the container unhealthy and fire a failure alert every tick.

**Telegram failures are alerted on their own edge.** `ERROR_ALERT_AFTER` consecutive failed ticks send one alert, and clearing the failure state sends one recovery notice — a state machine debounce, not a per-tick message.

## 4. The compose file, block by block

### `docker-compose.allinone.yml`

```yaml
services:
  nocodb:
    image: nocodb/nocodb:2026.09.0     # pinned on purpose; the monitor targets this meta API
    volumes:
      - nocodb-data:/usr/app/data      # SQLite db + attachments
    ports:
      - "${NOCODB_PORT:-8080}:8080"    # only the web UI is published
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:8080/api/v1/health >/dev/null 2>&1 || exit 1"]

  carousell-monitor:
    build: .                           # built locally from this repo — no registry involved
    depends_on:
      nocodb:
        condition: service_healthy     # do not start before the database answers
    extra_hosts:
      - "api.telegram.org:149.154.166.110"
    volumes:
      - carousell-data:/data           # health.json, alert_state.json
    networks: [carousell]
```

| Key | Why |
|---|---|
| `build: .` | the image is built from this directory; nothing is pulled from a registry |
| `depends_on: service_healthy` | the monitor bootstraps the schema at startup, so NocoDB must be up first |
| `extra_hosts` | pins `api.telegram.org` to its IPv4. On a Docker network without IPv6, the embedded DNS can hand back an AAAA record and the send hangs with no error — the alert is lost silently. Refresh the IP with `dig +short api.telegram.org` if sends start timing out; delete the two lines if your host has working IPv6 |
| named volumes | survive container recreation and `down`; no host-path/ACL problems |
| private network | only `nocodb` publishes a port, and the monitor resolves it as `http://nocodb:8080` — no IPs anywhere |

### `docker-compose.yml` (existing NocoDB)

The same monitor service, minus NocoDB, joining a network that already exists:

```yaml
    networks:
      - bridge_hoelee        # ← your existing network name

networks:
  bridge_hoelee:
    external: true           # created by the NocoDB stack, not by this one
```

Point `NOCODB_URL` at the existing container's name on that network (e.g. `http://nocodb:10380` if it listens on a non-default port).

## 5. The Dockerfile

```dockerfile
FROM python:3.11-alpine
WORKDIR /app
COPY monitor.py healthcheck.py /app/
RUN mkdir -p /data
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=15s --start-period=120s --retries=3 \
  CMD python /app/healthcheck.py
CMD ["python", "-u", "/app/monitor.py"]
```

- **stdlib only** — no `pip install`, so the image is ~60 MB and builds in seconds.
- **Code is baked in** — shipping a change means rebuilding (`docker compose up -d --build`), not restarting.
- **`python -u`** — unbuffered output so logs appear immediately.
- **`start-period=120s`** — the first tick includes the schema bootstrap; give it room before Docker starts reporting health.

## 6. Extraction details (why it is fragile)

Carousell server-renders the search results into a `<script type="application/json">` blob; the monitor parses the largest one and reads `SearchListing.listingCards[]`. Each card carries `listingID`, `title`, `thumbnailURL`, `seller.username`, the condition as a paragraph, and timestamps under `aboveFold` (`time_created`, or `active_bump` for bumped listings).

Consequences worth knowing before you patch it:

- **HTTP 200 does not mean success.** Under soft rate limiting Carousell returns 200 with `listingCards: null`. That is a retryable condition, not an empty result set — the code raises a named error for it.
- **No `application/json` blob at all** means a challenge/blocked page. Same treatment.
- **Ad cards exist** (`listingID = 0`) and are skipped rather than archived.
- **Thumbnail URLs carry a `_progressive_thumbnail` suffix** that is stripped to get the full-size image.

## 7. Scaling and limits

One process polls everything. That is comfortable for tens of searches at 5-minute intervals. If you need more, raise `check_interval_minutes` rather than lowering `TICK_SECONDS`, and keep `FETCH_GAP_SECONDS ≥ 1` — the point of the gap is that your traffic never looks like a burst.
