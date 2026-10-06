# Quick start on Portainer (click-by-click)

For a fresh machine, no shell needed. Portainer runs the same compose file as the CLI flow — see the [README](../README.md) for that version.

**Before you start:** Portainer must already be up and connected to a Docker endpoint, and Portainer's own container needs access to the Docker socket (the standard install does). NocoDB will need one free host port — `8080` by default.

---

## 1. Create the NocoDB container first

Because you cannot paste the base id and token until NocoDB exists, do this in two deploys.

1. **Stacks → Add stack**
2. **Name:** `carousell-monitor`
3. **Build method:** *Web editor*
4. Paste the **first service only** for now — the `nocodb:` block from [`docker-compose.allinone.yml`](../docker-compose.allinone.yml) plus the closing `volumes:` / `networks:` sections:

   ```yaml
   services:
     nocodb:
       image: nocodb/nocodb:2026.09.0
       container_name: carousell-nocodb
       restart: unless-stopped
       environment:
         PORT: "8080"
         NC_AUTH_JWT_SECRET: <paste output of: openssl rand -hex 32>
         NC_DISABLE_TELE: "true"
         NC_ALLOW_LOCAL_HOOKS: "false"
       volumes:
         - nocodb-data:/usr/app/data
       ports:
         - "8080:8080"
       healthcheck:
         test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:8080/api/v1/health >/dev/null 2>&1 || exit 1"]
         interval: 30s
         timeout: 10s
         retries: 5
         start_period: 60s
       networks:
         - carousell

   volumes:
     nocodb-data:

   networks:
     carousell:
   ```

5. **Deploy the stack.** Wait for `carousell-nocodb` to show as running/healthy.

## 2. Create the base and collect the two values

Open `http://<your-host>:8080`:

1. Create the admin account.
2. Create a base (`+ New Base`) — call it `Carousell`.
3. Copy the **base id** from the URL (`…/nc/base/<BASE_ID>/…`).
4. Avatar (bottom-left) → **Account Settings → Tokens → Create token**, and copy the `nc_pat_…` value. It is shown once.

## 3. Add the monitor to the same stack

1. Get the Telegram values first ([docs/TELEGRAM-SETUP.md](TELEGRAM-SETUP.md)) — bot token from @BotFather, chat id from @userinfobot.
2. Back in Portainer: **Stacks → carousell-monitor → Editor** tab.
3. Replace the whole file with the content of [`docker-compose.allinone.yml`](../docker-compose.allinone.yml), **with the real values written in directly** instead of `${...}` placeholders:

   ```yaml
       environment:
         NOCODB_URL: http://nocodb:8080
         NOCODB_TOKEN: nc_pat_your_token_here
         NOCODB_BASE_ID: your_base_id_here
         TELEGRAM_BOT_TOKEN: 8123456789:AAF...
         TELEGRAM_CHAT_ID: 123456789
   ```

   and the same for `NC_AUTH_JWT_SECRET` / `NC_DISABLE_TELE` in the `nocodb` service.

   > **Why inline values instead of Portainer's environment panel?** Portainer CE masks secret-looking values that are entered through the UI and stores the mask with the stack. On the next stack update the container receives `***` and silently stops authenticating. Editing the YAML keeps the real value on your Portainer host, which is where a stack file's secrets live anyway.
4. **Update the stack.** Portainer recreates the services that changed and adds `carousell-monitor`.
5. **Containers → carousell-monitor → Logs**: expect one line starting with `ready: listings=… settings=… seen=0`.

## 4. Start using it

In NocoDB, open the base — four tables are now present. Add your first watch to **`Settings`** (see [docs/NOCODB-SETUP.md](NOCODB-SETUP.md#3-the-tables-the-monitor-creates)) and give it a minute. Then untick `notified` on any `Listings` row to force a test alert.

## 5. Everyday maintenance in Portainer

| Task | Where |
|---|---|
| Change a search / filters | NocoDB UI — nothing to redeploy |
| Change credentials or tuning | **Stacks → carousell-monitor → Editor** → edit YAML → **Update the stack** |
| See why it is unhealthy | **Containers → carousell-monitor → Logs**, then the `Listings`/`Settings` tables for real progress |
| Restart | **Containers → carousell-monitor → Restart** (state is in NocoDB, nothing is lost) |
| Update this project | pull the new code on the host, then **Editor** → *Update the stack* with `Re-pull image`/rebuild enabled, or `docker compose up -d --build` on the CLI |
| Back up | **Volumes → nocodb-data** (all data) — or NocoDB's own base export |

## 6. Optional: deploy from the repository instead

**Stacks → Add stack → Repository**, with:

| Field | Value |
|---|---|
| Repository URL | `https://github.com/hoelee/carousell-monitor` |
| Reference | `refs/heads/main` |
| Compose path | `docker-compose.allinone.yml` |

Set `NOCODB_BASE_ID` / `NOCODB_TOKEN` / `TELEGRAM_*` / `NC_AUTH_JWT_SECRET` in the environment panel for this one (they are not in git). Be aware of the masking caveat above, and that Portainer clones the repository on every deploy.
