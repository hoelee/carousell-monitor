# NocoDB setup

The monitor stores everything in [NocoDB](https://nocodb.com) — an open-source Airtable alternative. It gives you the archive (with thumbnails), the watch-list UI, and the filter tables, and it is the only interface you need day to day.

The **base** and the **API token** are created by you; the **four tables** are created by the monitor on its first start.

---

## 1. First run of NocoDB

Start it (all-in-one stack):

```bash
docker compose -f docker-compose.allinone.yml up -d nocodb
```

Open `http://<host>:8080`, then:

1. **Create the admin account.** First click wins — pick a real password, there is no password reset without mail configured.
2. **Create a Base** (`+ New Base`), named e.g. `Carousell`. A base is a database; everything this project needs lives inside it.
3. Do not bother creating tables by hand. The monitor does that.

## 2. Collect the two values

**Base id** — open the base and read the browser URL:

```
http://localhost:8080/dashboard/#/nc/base/poqw1zjw3hnsk37/...
                                         ^^^^^^^^^^^^^^^ NOCODB_BASE_ID
```

**API token** — click your avatar (bottom-left) → **Account Settings** → **Tokens** → **Create token**.

- Give it a name (`carousell-monitor`), no expiry (or an expiry you will remember to renew).
- Copy the value immediately — it starts with `nc_pat_` and NocoDB will not show it again. That is `NOCODB_TOKEN`.

If your NocoDB is multi-workspace, create the token inside the workspace that owns the base.

## 3. The tables the monitor creates

Restart the monitor after filling in the token; `Listings`, `Settings`, `IgnoredSellers` and `IgnoredKeywords` appear in the base. Creating them is idempotent — restarting never duplicates a table or a row.

### `Settings` — your watch list (you edit this)

| Column | Type | Notes |
|---|---|---|
| `title` | text | label shown in alerts and used to link filters |
| `url` | URL | the full Carousell search URL, **must contain `sort_by=3`** |
| `enabled` | checkbox | untick to pause a search |
| `notify` | checkbox | untick to archive without alerting |
| `check_interval_minutes` | number | how often this search is polled (default 5) |
| `last_checked_at` | datetime | maintained by the monitor; empty = never seeded yet |

### `Listings` — the archive (written by the monitor)

| Column | Type | Notes |
|---|---|---|
| `product_url` | URL | dedupe key, `https://www.carousell.com.my/p/<id>/`, no query string |
| `title` | text | listing title |
| `price` | decimal | numeric, `RM` stripped (`85.00`) so you can sort and filter |
| `condition` | select | Brand new / Like new / Lightly used / Well used / Heavily used / Used |
| `image_url` | URL | the full-size photo URL |
| `image` | attachment | same photo as an attachment — renders as a thumbnail in grid view |
| `seller_name` / `seller_url` | text / URL | who is selling |
| `search_title` / `search_url` | text / URL | which watch found it |
| `listed_at` | datetime (UTC) | when the seller posted it |
| `first_seen_at` | datetime (UTC) | when the monitor first saw it |
| `notified` | checkbox | `false` = still queued for an alert; `true` = sent or deliberately silenced |
| `skip_notify` | checkbox | set when a filter matched (see below) |

### `IgnoredSellers` — mute a seller everywhere

One row per seller, column `seller_name` = the Carousell username. Their listings stay in the archive (`skip_notify = true`) but never reach Telegram.

### `IgnoredKeywords` — mute words for one search

| Column | Type | Notes |
|---|---|---|
| `watch` | link → `Settings` | **pick the search from the dropdown**; keywords only apply to it |
| `keyword` | text | case-insensitive substring match against the listing **title** |

Both filter tables are reloaded every tick, so edits take effect within a minute without a restart.

## 4. Getting a good search URL

1. Search on [carousell.com.my](https://www.carousell.com.my).
2. Set the sort to **Recent** (the newest listing must come first — the monitor only sees what the first page shows).
3. Copy the address bar into the `url` column. It should look like:

```
https://www.carousell.com.my/search/mechanical-keyboard?addRecent=true&canChangeKeyword=true&includeSuggestions=true&sort_by=3&t-search_query_source=direct_search
```

Tips: keep searches specific (a specific model, a narrow category). Each search returns roughly the first page of results; a very broad search whose first page turns over slowly will miss the churn below the fold.

## 5. Quality-of-life

- **Images**: switch `Listings` to **grid view** (`Fields` → include `image`) for a thumbnail wall of everything found.
- **Timezones**: stored timestamps are UTC on purpose. Change your display timezone in NocoDB's account settings if you want local times in the UI.
- **Filters/sorts**: `price` is numeric, so `price < 200 AND condition = 'Like new'` works.
- **Backups**: everything is in the `nocodb-data` volume. NocoDB also has its own export (base → `…` → Export), which is the friendlier thing to keep off-box.
