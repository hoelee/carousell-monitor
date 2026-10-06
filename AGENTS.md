# AGENTS.md

Entry point for AI coding agents working on **carousell-monitor**: a Python-stdlib
monitor that polls Carousell search pages, archives every listing to NocoDB, and
alerts Telegram.

Read this file, then the doc that matches your task. Keep it updated when the
layout or the deployment story changes.

## What it does

`monitor.py` polls Carousell search URLs (`sort_by=3` = recent), extracts listings
from the server-rendered `<script type="application/json">` Redux state
(`SearchListing.listingCards`), dedupes by `product_url` (param-less), archives to
a NocoDB base, and alerts Telegram `"<title>: N new listings"`.

Silencing: a listing whose `seller_name` is in `IgnoredSellers` is archived but
never alerted (`skip_notify=true`); likewise a listing whose **title** contains a
keyword listed for its watch in `IgnoredKeywords` (a real Link column → `Settings`,
case-insensitive substring).

## Iron rules

- Secrets NEVER in code or compose — env vars (`.env`, gitignored) or a stack's
  environment. `SECRETS.example.md` is the tracked template; the real inventory is
  the gitignored `SECRETS.md`. No default base id, token or chat id may be shipped.
- Operational knobs (`enabled` / `notify` / `check_interval_minutes`) live in the
  NocoDB **Settings** table, adjustable from the UI without a redeploy. Only
  credentials and tuning belong in the environment.
- Dedupe key is `product_url` (`https://www.carousell.com.my/p/<id>/`), never the
  raw listing id and never the query-string URL.
- First run per watch seeds the archive with **no** Telegram alert
  (`last_checked_at` null == unseeded).
- Docker HEALTHCHECK: unhealthy when the last tick is older than
  `HEALTH_STALE_SECONDS` or the last run failed.
- `image` is an Attachment column storing the remote URL (NocoDB hotlinks it; the
  raw URL is also kept in `image_url`).
- Never change `docker-compose.yml` casually: it is the production variant that
  joins an existing NocoDB network. The beginner/fresh-install path is
  `docker-compose.allinone.yml`.

## Layout

| File | Role |
|---|---|
| `monitor.py` | the whole loop (stdlib only) |
| `healthcheck.py` | Docker HEALTHCHECK probe over `/data/health.json` |
| `Dockerfile` | python:3.11-alpine, no dependencies |
| `docker-compose.allinone.yml` | NocoDB + monitor (fresh install) |
| `docker-compose.yml` | monitor only, existing NocoDB network (production) |
| `test_pagination.py` | stdlib regression suite — must pass before any commit |
| `docs/` | long-form guides: QUICKSTART-PORTAINER, TELEGRAM-SETUP, NOCODB-SETUP, ARCHITECTURE, OPERATIONS, TROUBLESHOOTING |
| `OPS-INTERNAL.md` | gitignored: this deployment's hosts, paths, stack ids (may not exist) |

## Verified facts (Carousell page shape, 2026-09)

- Search page: ~1.6 MB HTML, state blob ~1.27 MB, ~49 `listingCards` per load.
  Card fields: `listingID`, `title`, `price` (e.g. "RM85"), `thumbnailURL`,
  `seller.username`, `aboveFold[time_created].timestampContent.seconds.low`,
  `belowFold` paragraphs where `paragraph[1]` = condition.
- `https://www.carousell.com.my/p/<id>/` 301-redirects to the canonical slug URL.
- Soft rate limiting returns **HTTP 200 with `listingCards: null`** — treat as a
  retryable error, never as "no results".
- NocoDB v2 API: records use column *titles* as JSON keys; an Attachment field
  accepts a JSON string `[{"path","mimetype","title"}]` and keeps the remote URL.

## Working on the code

- Stdlib only. Do not add dependencies without a very good reason.
- Run `python test_pagination.py` (exit 0 = pass) and
  `python -m pyflakes monitor.py` before committing. pyflakes currently reports one
  cosmetic finding (`_send_photo_multipart` has a dead `parts = []` local) — anything
  beyond that is yours.
- New env knobs must be added to `monitor.py`, both compose files, `.env.example`
  and the tables in `README.md` + `docs/ARCHITECTURE.md` in the same commit.
- Tests must execute the real loader functions, not mocks of them, and must be
  mutation-checked (reintroduce the bug, watch the check fail, restore).
- `monitor.py` is checked out with CRLF on Windows; prefer a Python
  `read → replace → write(newline='')` over fuzzy patch tools on big blocks.
- Grepping pitfall: a `NameError`/arity error inside a tick does **not** fail at
  import. The container keeps running and the only symptom is
  `/data/health.json` reporting `ok:false` with a `tick error: …` string.

## Deploying

Generic:

```bash
git pull
docker compose -f docker-compose.allinone.yml up -d --build
```

This project's own deployment (host paths, Portainer stack id, NocoDB instance,
tunnel) is deliberately kept out of the repo: see the gitignored `OPS-INTERNAL.md`
or the operator's `carousell-monitor` skill. Two rules that were learned the hard
way:

1. The **build context is a plain directory** on the host, not a git checkout:
   the running container is built from the files copied next to the compose file.
   Sync `monitor.py` (and `docker-compose*.yml` / `Dockerfile` when they change)
   and then rebuild — a stack PUT alone does not run `--build`.
2. **Compare the live container's environment with the file you are about to
   deploy before rebuilding.** Env drift between the compose file, the stack's
   stored env and the running container is the single most common cause of a
   "fixed" deploy that changes nothing.
