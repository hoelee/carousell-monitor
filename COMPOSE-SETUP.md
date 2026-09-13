# COMPOSE-SETUP.md — Carousell Monitor Docker Stack, Explained

Line-by-line anatomy of the compose stack, how it connects to the rest of the
homelab, and the two ways to deploy it. Complementary to `DOCUMENTATION.md`
(ops) and `AGENTS.md` (agent entry); this file is the *stack reference*.

---

## 1. Topology

```
                    ┌────────────────────────────────────────────────┐
                    │ DSM host (Synology)                            │
                    │                                                │
 Internet ─ 443 ──► │ DSM nginx reverse proxy                        │
                    │      │ (not involved for this container —      │
                    │      │  it makes outbound calls only)          │
                    │      ▼                                         │
                    │  Docker network  bridge_hoelee  (external)     │
                    │      │                                         │
                    │      ├── carousell-monitor  (this stack)       │
                    │      │      │                                   │
                    │      │      ├─► NocoDB    http://nocodb:10380  │
                    │      │      │     (same bridge_hoelee network) │
                    │      │      ├─► Carousell www.carousell.com.my │
                    │      │      │     (public internet, GET search)│
                    │      │      └─► Telegram api.telegram.org      │
                    │      │            (pinned IPv4 via extra_hosts)│
                    │      │                                          │
                    │      └── nocodb container (named "nocodb")      │
                    └────────────────────────────────────────────────┘
```

The monitor is an **outbound-only** worker: it has no inbound port, no web UI,
and is never reached through the reverse proxy. All state lives in NocoDB
(Listings / Settings / IgnoredSellers / IgnoredKeywords), all alerts go out via
Telegram.

---

## 2. docker-compose.yml, block by block

### Top-level services

```yaml
services:
  carousell-monitor:
    build: .
    image: carousell-monitor:latest
    container_name: carousell-monitor
    restart: unless-stopped
```

| Key | Meaning |
|---|---|
| `build: .` | Image is built **locally from this directory** (Dockerfile in repo) — nothing is pulled from a registry |
| `image: carousell-monitor:latest` | Local tag for the built image; docker compose will rebuild + retag on `up --build` |
| `container_name` | Fixed name = stable DNS name on the network, predictable for health checks and logs |
| `restart: unless-stopped` | Survive daemon restarts and DSM reboots; stop it manually to keep it down |

### Naming vs. images

The image is **private**: built on DSM, never pushed to Docker Hub or any
registry. `image:` is just a local convenience tag (`docker images` shows it; the
build context is the repo directory).

### extra_hosts — the Telegram IPv4 pin

```yaml
    extra_hosts:
      - "api.telegram.org:149.154.166.110"
```

**Why this exists (root cause, do not delete):**

- The container runs inside `bridge_hoelee`, which has **no IPv6**.
- Docker's embedded DNS at `127.0.0.11` may return an **IPv6 AAAA record** for
  `api.telegram.org`; with no IPv6 route, the connection hangs and Telegram
  sends fail silently.
- Pinning the correct IPv4 (as of 2026-09) in `extra_hosts` short-circuits DNS
  and makes `api.telegram.org` resolve straight to the IPv4.
- ⚠ If Telegram calls start timing out again, **re-verify the current IP**:
  `nslookup api.telegram.org` / `dig +short api.telegram.org` — Telegram rotates
  IPs. Update the pin, then redeploy.
- Note: the **live** stack pins the **same** IP as the repo (synced 2026-09-13).
  Treat the repo value as canonical; re-verify on any timeout.

### environment

```yaml
    environment:
      NOCODB_URL: ${NOCODB_URL:-http://nocodb:10380}
      NOCODB_TOKEN: ${NOCODB_TOKEN}
      NOCODB_BASE_ID: ${NOCODB_BASE_ID:-poqw1zjw3hnsk37}
      TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN}
      TELEGRAM_CHAT_ID: ${TELEGRAM_CHAT_ID}
      TICK_SECONDS: ${TICK_SECONDS:-60}
      FETCH_GAP_SECONDS: ${FETCH_GAP_SECONDS:-1}
      HEALTH_STALE_SECONDS: ${HEALTH_STALE_SECONDS:-600}
      TZ: Asia/Kuala_Lumpur
```

| Var | Default | Meaning |
|---|---|---|
| `NOCODB_URL` | `http://nocodb:10380` | NocoDB REST endpoint. `nocodb` = the NocoDB container's name on `bridge_hoelee` (container DNS). Never use a public hostname here. |
| `NOCODB_TOKEN` | *(required)* | Workspace-scoped NocoDB PAT (xref `SECRETS.md`). Never hard-code; comes from the stack env/.env. |
| `NOCODB_BASE_ID` | `poqw1zjw3hnsk37` | Base "Carousell" — all four tables live under it. |
| `TELEGRAM_BOT_TOKEN` | *(required)* | @carousellFoundBot token (xref `SECRETS.md`). |
| `TELEGRAM_CHAT_ID` | *(required)* | `5648309582` — @MrFullStackDev. |
| `TICK_SECONDS` | `60` | Scheduler granularity: heartbeat + watch-list reload interval. |
| `FETCH_GAP_SECONDS` | `1` | Minimum pause (s) between watch URL fetches within one tick — prevents request bursts (default 1; set `0` to disable). |
| `HEALTH_STALE_SECONDS` | `600` | Docker healthcheck tolerance: if last tick older than this → unhealthy. |
| `TZ` | `Asia/Kuala_Lumpur` | Container clock (mostly cosmetic; timestamps are written in UTC deliberately for NocoDB). |

`${VAR:-default}` syntax: compose substitutes the value from the environment /
`.env` file, falling back to the literal default when unset. `${NOCODB_TOKEN}`
with **no** default means it's mandatory — compose errors if missing.

### volumes

```yaml
    volumes:
      - carousell-data:/data
```

Named volume `carousell-data` mounted at `/data`. Inside the container that's:

- `monitor.py` → `DATA_DIR` default → `/data/health.json` (written each tick, read by the healthcheck)
- Nothing else is stored there — NocoDB holds all real state.

Why a volume and not a bind mount: survives container recreation, no host-path
permission issues on DSM ACLs, and it's private to the stack (not exposed to the
host filesystem).

### networks

```yaml
    networks:
      - bridge_hoelee

networks:
  bridge_hoelee:
    external: true
```

`external: true` = the network is **pre-existing** (created by the NocoDB stack,
typically). Compose does not create it, just joins it. This is what lets the
monitor reach `http://nocodb:10380` by container name instead of an IP that
drifts (DSM IP history: 192.168.137.2 → 192.168.1.1 → …).

---

## 3. Dockerfile

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

- `python:3.11-alpine` — tiny, stdlib-only code needs no pip deps → fast builds, small image.
- `COPY` bakes the script into the image → a rebuild is how code ships (there is no bind-mount of the repo).
- `HEALTHCHECK` runs `healthcheck.py` every 60s: exits 0 iff `/data/health.json` exists, is newer than `HEALTH_STALE_SECONDS`, and `ok == true`.
- `python -u` — unbuffered stdout so `docker logs` shows ticks in real time.

### healthcheck.py logic

```python
age = time.time() - int(h.get("last_run_epoch", 0))
if age <= STALE and h.get("ok") is True:
    sys.exit(0)     # healthy
sys.exit(1)         # unhealthy
```

One failed tick (Carousell 403/429/parse error, NocoDB down) → `ok:false` →
container turns **unhealthy** until a fully successful tick. That's the tripwire:
Portainer shows it red, `docker inspect` reports it, and you can alert on it.

---

## 4. Deployment paths

### A. On DSM via Portainer stack (current production)

Container is owned by **Portainer stack 240** (standalone). Deploy:

1. Edit code in the repo (`D:\dev\carousell-monitor`), commit, push to Gitea
   (`git.hoelee.com/hoelee/carousell-monitor`).
2. Sync the build context — Portainer's project dir holds a full manual copy
   (not a git clone): copy changed runtime files to
   `/volume1/docker/portainer/compose/240/` (`monitor.py`, `healthcheck.py`,
   `Dockerfile`, `docker-compose.yml`).
3. If `monitor.py` / `Dockerfile` changed, rebuild the image — a standalone
   stack PUT does **not** run `--build`:
   ```bash
   sudo /usr/local/bin/docker build -t carousell-monitor:latest \
     /volume1/docker/portainer/compose/240
   ```
4. Update the stack via the Portainer API:
   `PUT /api/stacks/240?endpointId=2` with the repo `docker-compose.yml` as
   `stackFileContent` and the **current env array** (8 entries, incl.
   `FETCH_GAP_SECONDS=1`; see the `portainer-api` skill — ⚠ Portainer masks
   secret env values, so never echo the masked `***` strings back, and keep the
   real Telegram token in `SECRETS.md`). Portainer recreates the container.

The old `/volume1/docker/carousell-monitor` dir is **legacy — do not
`compose up` there anymore**; it only kept around for reference (its `.env` is
masked and stale).

### B. Local dev (Windows, against LAN NocoDB)

```bash
NOCODB_URL=http://192.168.1.1:10380 \
NOCODB_TOKEN=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
python monitor.py
```

Runs the same loop outside Docker — useful for testing code changes before
shipping (uses LAN IP instead of container DNS).
⚠ `192.168.1.1` drifts; check `:10380` on the current DSM IP first.

---

## 5. What to check when something breaks

| Symptom | Check |
|---|---|
| Container `unhealthy` | `sudo docker logs carousell-monitor --tail 100` — read the `error` line |
|"No application/json state" | Carousell rate-limited/blocked; raise `check_interval_minutes` in Settings |
| No Telegram | `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` correct? `notify` box on the watch? Telegram IP pin in `extra_hosts` still valid? |
| "NOCODB_TOKEN not set" | Stack env missing the token; fix in Portainer stack env, redeploy |
| NocoDB unreachable | Is `nocodb` container on `bridge_hoelee`? `sudo docker network inspect bridge_hoelee` |
| Timestamps 8h off | They are **UTC by design**; NocoDB display TZ → Asia/Kuala_Lumpur |

---

## 6. Files in the repo

| File | Role |
|---|---|
| `docker-compose.yml` | Stack definition (this doc) |
| `Dockerfile` | Image build + healthcheck |
| `monitor.py` | The whole monitor (stdlib only) |
| `healthcheck.py` | Docker HEALTHCHECK probe |
| `.env.example` | Template for local runs (secrets go in `.env`, gitignored) |
| `SECRETS.md` | Credential inventory (private repo) |
| `DOCUMENTATION.md` | Deployment & operations manual |
| `AGENTS.md` | AI-agent entry point |
| `COMPOSE-SETUP.md` | This file |

---

*Last reviewed: 2026-09-13 (NocoDB 2026.08.1, Docker Compose v2.20.1 on DSM).*