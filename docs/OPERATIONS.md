# Operations

Running it, watching it, backing it up, upgrading it.

---

## Health at a glance

The container writes `/data/health.json` at the end of every tick, and the Docker HEALTHCHECK (`healthcheck.py`) reads it every 60 s:

```bash
docker exec carousell-monitor cat /data/health.json
```

```json
{
  "last_run_epoch": 1791275045,
  "ok": true,
  "error": "",
  "watch_count": 3,
  "new_this_tick": 0,
  "failed_watches": 0
}
```

| Field | Meaning |
|---|---|
| `ok` | the last tick completed without exceeding the failure threshold |
| `error` | empty, or which watches failed and why (`[partial 1/3] …` when the ratio threshold absorbed it) |
| `last_run_epoch` | when the tick ended (Unix seconds, UTC) |
| `watch_count` | how many enabled watches were read from `Settings` |
| `new_this_tick` | listings fetched this tick that were not already known — **not** how many alerts were sent |

Two traps worth internalising:

- **`ok: true` only proves the fetch loop ran.** Notification failures are retried on the next tick rather than reported, so an archive that fills up happily can still be silent in Telegram. To check the notify path, untick `notified` on a row and watch it flip back to `true` (that flip only happens after Telegram answered 200).
- **`new_this_tick: 0` is not evidence of anything** — it counts fetched rows, not delivered messages.

The container's own health is the other half:

```bash
docker compose -f docker-compose.allinone.yml ps          # State / Health column
docker inspect --format '{{.State.Health.Status}}' carousell-monitor
```

`unhealthy` = the last tick failed or is older than `HEALTH_STALE_SECONDS` (600 s).

## Logs

```bash
docker compose -f docker-compose.allinone.yml logs -f --tail 100 carousell-monitor
```

The loop is deliberately quiet: one `ready:` line at startup, then nothing unless something fails. Per-watch failures are logged to stderr, and hard failures appear in `health.json`. If you are debugging "why no alert", logs are the wrong place — use the decision tree in [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## Alerting on the monitor itself

`ERROR_ALERT_AFTER` (default 3) consecutive failed ticks trigger one Telegram message:

```
🚨 carousell-monitor 故障
连续失败 3 次
错误: Uniform: carousell fetch HTTP 403
容器将标记为 unhealthy
```

and one recovery message when the next good tick arrives. The debounce state lives in `/data/alert_state.json`, so a container restart does not re-fire an alert you already saw.

The alert strings are Chinese in the current code (`monitor.py` → `alert_on_health()`); change the two `msg = (…)` literals if you want English.

## Backups

| What | Where | How |
|---|---|---|
| All listings, settings and filters | volume `nocodb-data` | stop the stack, tar the volume; or use NocoDB's own **Export base** |
| Health + alert state | volume `carousell-data` | disposable — do not bother |
| This repo's config | `.env` / the Portainer stack file | keep a copy in your password manager |

Nothing else is stateful. NocoDB is the single source of truth.

## Upgrading

**The monitor** (code change in this repo):

```bash
git pull
docker compose -f docker-compose.allinone.yml up -d --build
```

The rebuild re-creates the container. The schema bootstrap and the seen-set are idempotent, so nothing is duplicated.

**NocoDB** — change the image tag in the compose file and:

```bash
docker compose -f docker-compose.allinone.yml up -d
```

NocoDB migrates its own database on start. Back up `nocodb-data` first. The monitor's schema bootstrap talks to NocoDB's **meta API**, which does change between majors: the tag in `docker-compose.allinone.yml` is the version range this code is verified against, so upgrade it deliberately (and check the table columns in the UI afterwards).

**Portainer users:** same thing through the UI — edit the stack file, *Update the stack*. Be careful with `Re-pull image` on a stack whose env values were entered through Portainer's panel (see the README's masking warning).

## Tests

```bash
python test_pagination.py     # stdlib only, no network, exit 0 = pass
```

Covers the things that have actually broken: NocoDB paging past 1000 rows, the Telegram HTTP verb bug, the failure-ratio threshold maths, and the fetch-gap timing.

## Tuning notes

| Goal | Change |
|---|---|
| More search coverage | add rows to `Settings`, keep `check_interval_minutes` ≥ 5 |
| React faster | lower `check_interval_minutes` (not `TICK_SECONDS` below ~30 s) |
| Be gentler on Carousell | raise `check_interval_minutes`, keep `FETCH_GAP_SECONDS` ≥ 1 |
| Alert inbox too noisy | set `notify = false` on a watch, or add entries to `IgnoredKeywords` / `IgnoredSellers` |
| Fewer failure alerts | raise `ERROR_ALERT_AFTER`, or let `FAILURE_RATIO_THRESHOLD` stay at `1.0` |

## Data safety rules

- `.env` and your NocoDB token are credentials. Never commit them, never paste them into an issue.
- The NocoDB token is scoped to a workspace: regenerate it in NocoDB and update the stack if it leaks.
- The monitor never deletes or modifies archived listings, apart from the `notified` / `skip_notify` flags. Cleaning up the archive is your job — NocoDB's grid view deletes rows fine.
