# AGENTS.md

Project: Carousell new-listing monitor (Python stdlib, Docker, NocoDB, Telegram).

## What it does

`monitor.py` polls Carousell search URLs (sort_by=3 = recent), extracts listings
from the server-rendered `<script type="application/json">` Redux state
(`SearchListing.listingCards`), dedupes by `product_url` (param-less), archives to a
NocoDB base, and alerts Telegram `"<title>: N new listings"`. Listings whose
`seller_name` is in the `IgnoredSellers` table are archived but never alerted
(`skip_notify=true`). Listings whose **title** contains a keyword listed for their
watch in the `IgnoredKeywords` table (linked to the watch's `Settings` row,
case-insensitive) are also archived but never alerted. Runs 24/7 as a Docker
container on DSM (network `bridge_hoelee`, reaches NocoDB at `http://nocodb:10380`).

## Iron rules

- Secrets NEVER in code or compose — only env vars / `SECRETS.md` (private repo).
  Operational knobs (`enabled` / `notify` / `check_interval_minutes`) live in the
  NocoDB **Settings** table, adjustable from the UI without redeploy.
- Ignored sellers live in the NocoDB **IgnoredSellers** table (one `seller_name`
  per row). When a pending listing's `seller_name` matches an ignored seller, the
  monitor sets `skip_notify=true` + `notified=true` and does NOT send Telegram.
  The list is reloaded every notification cycle, so UI add/remove takes effect
  immediately.
- Ignored keywords live in the NocoDB **IgnoredKeywords** table: `watch` is a real
  **Link column → `Settings`** (pick the watch from a dropdown). A listing is
  silenced when its **title** contains any keyword for its watch, case-insensitive
  substring match. Per-watch, not global. Reloaded every cycle, so UI edits take
  effect immediately. Bootstrap creates the `watch` column and drops any legacy
  `search_url` column.
- Dedupe key is `product_url` (`https://www.carousell.com.my/p/<id>/`), not the raw
  listing id and never the query-string URL.
- First run per watch seeds the archive with **no** Telegram alert (`last_checked_at`
  null == unseeded).
- Docker HEALTHCHECK: container is unhealthy if the last tick is >10 min old or the
  last run had a failure (failed extract / 403 / rate-limit).
- Image thumbnail: `image` Attachment field stores the remote URL (NocoDB hotlinks
  it — media.karousell.com is GCS-backed, `Access-Control-Allow-Origin: *`). The raw
  URL is also kept in `image_url`.

## Verified facts (2026-09-02)

- Carousell search page: 1.6 MB HTML, state blob ~1.27 MB, ~49 `listingCards` per
  load. Card fields: `listingID`, `title`, `price` (e.g. "RM85"), `thumbnailURL`,
  `seller.username`, `aboveFold[time_created].timestampContent.seconds.low`,
  `belowFold` paragraphs where `paragraph[1]` = condition (Like new/Brand new/...).
- `https://www.carousell.com.my/p/<id>/` 301-redirects to the canonical slug URL.
- NocoDB instance: DSM `http://192.168.137.2:10380` (IP drifts; container DNS
  `nocodb:10380` on bridge_hoelee). Base `Carousell` = `poqw1zjw3hnsk37`.
  Workspace token in SECRETS.md. v2 API: records use column *titles* as JSON keys;
  Attachment field accepts a JSON string `[{"path","mimetype","title"}]` and keeps the
  remote URL (does not re-host).

## Build / deploy

Container is managed by **Portainer stack 240** (standalone; compose + build
context live at `/volume1/docker/portainer/compose/240/` on DSM). Deploy flow:

1. Edit code, commit, push to Gitea (`git.hoelee.com/hoelee/carousell-monitor`).
2. Sync the build-context files to `/volume1/docker/portainer/compose/240/`
   (`monitor.py`, `healthcheck.py`, `Dockerfile`, `docker-compose.yml`).
3. If `monitor.py` / `Dockerfile` changed, rebuild the image first (Portainer's
   standalone PUT does **not** rebuild):

   ```bash
   sudo /usr/local/bin/docker build -t carousell-monitor:latest \
     /volume1/docker/portainer/compose/240
   ```

4. Update stack 240 via the Portainer API: `PUT /api/stacks/240?endpointId=2`
   with the repo `docker-compose.yml` as `stackFileContent` and the current env
   array (see the `portainer-api` skill; ⚠ never echo masked `***` values back).

Portainer recreates the container with the new config. Private build on DSM —
NO registry (do not push to Docker Hub).
