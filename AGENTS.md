# AGENTS.md

Project: Carousell new-listing monitor (Python stdlib, Docker, NocoDB, Telegram).

## What it does

`monitor.py` polls Carousell search URLs (sort_by=3 = recent), extracts listings
from the server-rendered `<script type="application/json">` Redux state
(`SearchListing.listingCards`), dedupes by `product_url` (param-less), archives to a
NocoDB base, and alerts Telegram `"<title>: N new listings"`. Runs 24/7 as a Docker
container on DSM (network `bridge_hoelee`, reaches NocoDB at `http://nocodb:10380`).

## Iron rules

- Secrets NEVER in code or compose — only env vars / `SECRETS.md` (private repo).
  Operational knobs (`enabled` / `notify` / `check_interval_minutes`) live in the
  NocoDB **Settings** table, adjustable from the UI without redeploy.
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

Private build on DSM — NO registry (do not push to Docker Hub):

```bash
# on DSM (repo cloned to /volume1/docker/carousell-monitor)
cd /volume1/docker/carousell-monitor
sudo docker compose up -d --build
```
