# Quick start on Portainer (click-by-click)

For a fresh machine, no shell needed. Portainer runs the same compose file as the CLI flow — see the [README](../README.md#quick-start-cli) for that version.

**Before you start:** Portainer must already be up and connected to a Docker endpoint, and its container needs access to the Docker socket (the standard install does). NocoDB needs one free host port — `8080` by default.

**One thing to know up front:** the monitor's image is built from this repository (`build: .`). Portainer's *Web editor* and *Upload* options have no build context, so they cannot build it:

```
compose build operation failed: failed to solve: failed to read dockerfile:
open /volume1/@docker/tmp/buildkit-mount…/Dockerfile: no such file or directory
```

That leaves two routes. **Route 1 needs no shell.**

---

## Route 1 — deploy from the Git repository (recommended)

1. **Stacks → Add stack**
2. **Name:** `carousell-monitor`
3. **Build method:** *Repository*
4. **Repository URL:** `https://github.com/hoelee/carousell-monitor`
   **Reference:** `refs/heads/main`
   **Compose path:** `docker-compose.allinone.yml`
5. **Environment variables** — add:

   | Variable | Value |
   |---|---|
   | `NC_AUTH_JWT_SECRET` | output of `openssl rand -hex 32` |
   | `TELEGRAM_BOT_TOKEN` | from @BotFather |
   | `TELEGRAM_CHAT_ID` | from @userinfobot |
   | `NOCODB_PORT` | optional, default `8080` |

   Add `NOCODB_TOKEN` and `NOCODB_BASE_ID` too if you already have them (e.g. you are reusing an existing NocoDB). On a fresh install you cannot: they do not exist yet. Leave them empty.
6. **Deploy the stack.** `carousell-nocodb` comes up healthy. `carousell-monitor` will restart in a short loop logging `NOCODB_TOKEN not set` — expected, it needs the values from step 8. Stop that container for now if you prefer a clean list.

## 2. Create the base and collect the two values

Open `http://<your-host>:8080`:

1. Create the admin account.
2. Create a base (`+ New Base`) — call it `Carousell`.
3. Copy the **base id** from the URL (`…/nc/base/<BASE_ID>/…`).
4. Avatar (bottom-left) → **Account Settings → Tokens → Create token**, copy the `nc_pat_…` value. It is shown once.
5. Do not create tables by hand — the monitor does that on start.

## 3. Hand the values to the monitor

1. **Stacks → carousell-monitor → Editor** (or *Environment variables*).
2. Fill in `NOCODB_BASE_ID` and `NOCODB_TOKEN`, then **Update the stack**.
3. Portainer recreates the monitor. **Containers → carousell-monitor → Logs** should now end with one line:

   ```
   ready: listings=… settings=… ignored_sellers=… ignored_keywords=… seen=0
   ```

   and the container status should be `healthy` within a couple of minutes.

   > ⚠️ **Portainer masks secret-looking values typed into its environment panel** and stores the mask (`***`) with the stack — on a later stack update the container receives `***` and authentication silently fails. If that happens, put the real values directly in the YAML in the **Editor** tab instead of the panel.

## 4. Start using it

In NocoDB, open the base — four tables now exist (`Listings`, `Settings`, `IgnoredSellers`, `IgnoredKeywords`). Add your first watch to **`Settings`** ([details](../docs/NOCODB-SETUP.md#3-the-tables-the-monitor-creates)), give it a minute, then untick `notified` on any `Listings` row to force a test alert into Telegram.

---

## Route 2 — Web editor, with the image built by hand first

Use this when Portainer cannot reach your Git host, or you want a self-contained stack file.

```bash
# on the Docker host (or anywhere with access to its daemon)
git clone https://github.com/hoelee/carousell-monitor.git
cd carousell-monitor
docker build -t carousell-monitor:latest .
```

Then:

1. **Stacks → Add stack → Web editor**, name it `carousell-monitor`.
2. Paste [`docker-compose.allinone.yml`](../docker-compose.allinone.yml) and **remove the `build: .` line** from the `carousell-monitor` service — a `build:` entry in a web-editor stack can only fail (no Dockerfile in Portainer's stack directory).
3. Fill in `NC_AUTH_JWT_SECRET`, `NOCODB_PORT`, and the Telegram values. Deploy, then follow §2 and §3 above to add the base id and token.

Updating the code later:

```bash
git pull && docker build -t carousell-monitor:latest .
```

then **Update the stack** in Portainer.

---

## Everyday maintenance in Portainer

| Task | Where |
|---|---|
| Change a search / filter | NocoDB UI — nothing to redeploy |
| Change credentials or tuning | **Stacks → carousell-monitor → Editor** → **Update the stack** |
| Deploy a code change | Route 1: **Update the stack** (Portainer re-clones). Route 2: `git pull && docker build …`, then **Update the stack** |
| See why it is unhealthy | **Containers → carousell-monitor → Logs**, then `Listings` / `Settings` in NocoDB for the real progress |
| Restart | **Containers → carousell-monitor → Restart** — state is in NocoDB, nothing is lost |
| Back up | **Volumes → nocodb-data** (all data), or NocoDB's own base export |
| Upgrade NocoDB | edit the image tag in the Editor → **Update the stack**; back the volume up first |

## Notes and limits

- A **Repository** stack re-clones the repo on every deploy, so the update is always a full fetch of `main`. That is fine here (the repo is small) and it means you get fixes by pressing one button.
- Portainer deploys standalone compose stacks for this — no Swarm involved.
- GitOps updates (auto-redeploy when the repo changes) work too: enable them in the stack settings after the first deploy.
