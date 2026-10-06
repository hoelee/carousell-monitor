# Troubleshooting

A symptom, a cause, a fix — plus the decision tree to run when the symptom is the vague one: *"it stopped alerting me"*.

---

## 1. The decision tree for "no alert"

Four different failures look identical from the outside. Find which stage breaks before changing anything.

**Stage 0 — is it running at all?**

```bash
docker compose -f docker-compose.allinone.yml ps
docker exec carousell-monitor cat /data/health.json
docker exec carousell-monitor cat /data/alert_state.json     # fail_streak, alerted
```

- `ok: false` → the fetch loop is dead. Read `error` and jump to §2.
- `last_run_epoch` older than a couple of ticks → the loop is stuck; check the logs for a traceback.
- `No such file` → the container never completed a tick (bad credentials most likely — see `NOCODB_TOKEN not set`, §2).

**Stage 1 — is it fetching?**

Open `Settings` and check `last_checked_at` on your watch. It should advance every `check_interval_minutes`. If it never advances, the watch is disabled (`enabled` unticked), the interval is huge, or every fetch is failing.

**Stage 2 — is it archiving?**

Open `Listings`, sort by `first_seen_at` descending. New rows appearing means fetch + parse + NocoDB writes all work. If `last_checked_at` advances but `Listings` stays empty, you are the victim of an over-narrow search, not a bug — nothing new has appeared.

**Stage 3 — is it notifying?**

This is where the archive can look healthy while Telegram is silent. Sort `Listings` by `notified`:

- Rows with `notified = false` **and** `skip_notify = false` that stay false → the send is failing (see §2 transport rows) or the monitor is stuck.
- Rows with `skip_notify = true` → a filter matched, by design. Check `IgnoredSellers` / `IgnoredKeywords` and the watch's `notify` checkbox.

**Stage 4 — is the transport sane?**

From inside the container, prove the credentials and egress in one shot:

```bash
docker exec carousell-monitor sh -c '
  wget -qO- "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getMe"'
```

`{"ok":true,...}` means the token is good and the container can reach Telegram. Then send a real message:

```bash
docker exec carousell-monitor sh -c '
  wget -qO- --post-data="chat_id=$TELEGRAM_CHAT_ID&text=test" \
    "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage"'
```

If `getMe` works but the send does not, it is the chat id (a bot cannot open a conversation with you before you have messaged it — see [TELEGRAM-SETUP.md](TELEGRAM-SETUP.md)).

**Stage 5 — force a notification.**

In NocoDB, untick `notified` on any listing row. Within one tick the monitor re-sends it and flips the flag back to `true`. The flip **is** the proof of delivery: it only happens after Telegram returns HTTP 200.

---

## 2. Symptom → cause → fix

| Symptom | Likely cause | Fix |
|---|---|---|
| `NOCODB_TOKEN not set` in the logs, container exits | `.env` missing the token, or the stack did not pick it up | fill it in and re-create the container (`up -d`, or Portainer *Update the stack*) |
| `list tables failed: HTTP 401/403` | token wrong, expired, or from another workspace | create a token inside the workspace that owns the base |
| `list tables failed: HTTP 404` | wrong `NOCODB_BASE_ID` | re-copy the id from the base URL |
| `create table … failed` / `add column … failed` | the token can read but not write, or the base was deleted | verify with a write test (create a scratch table in the UI), check the base exists |
| `connection refused` to `nocodb` | the monitor is not on the same Docker network, or the host name is wrong | both services must share the network in the compose file; `NOCODB_URL` must use the service/container name, not `localhost` |
| `no application/json state found (blocked/ratelimited?)` | Carousell returned a challenge or error page | raise `check_interval_minutes`, keep `FETCH_GAP_SECONDS ≥ 1`, reduce the number of watches; the watch retries next interval |
| `listingCards null (soft-block/ratelimit?)` | Carousell answered 200 with an empty state blob | same as above — this is rate limiting, not a bug |
| `tick error: …` and the container goes unhealthy | an exception escaped the tick (NocoDB error, unexpected page shape) | read the message; the loop keeps running and retries |
| `telegram sendMessage failed: 400` in the logs | the request was malformed — historically a code bug where the Telegram **method name** was used as the HTTP verb. Fixed; keep `test_pagination.py` passing | run the test suite |
| Telegram sends hang and time out | Docker DNS handing back an IPv6 (AAAA) address on a network with no IPv6 | keep/refresh the `extra_hosts` pin (`dig +short api.telegram.org`) |
| `chat not found` (400) | you never started the bot; or the group id is wrong | message the bot once; re-read the id from `getUpdates` |
| Archive filling up, Telegram silent | see the tree in §1 — usually the `notify` checkbox, a filter match, or a failed send that is being retried |
| Every listing alerted twice | you reset `notified`, or two monitors point at the same base | untick only once; run one monitor per base |
| Hundreds of messages right after setup | the watch's `last_checked_at` was not empty (it was seeded before) | expected on a re-seed; delete `last_checked_at` only when you *want* a silent re-seed |
| Thumbnails broken in the NocoDB grid | the `image` column is not an Attachment column | restart the monitor — the bootstrap recreates missing columns |
| Timestamps 8 hours off | stored as UTC by design | change the display timezone in NocoDB |
| Container healthy but nothing happens | no enabled watches, or all of them fall outside their interval | add/enable a row in `Settings`, lower `check_interval_minutes` |

---

## 3. Useful commands

```bash
# status
docker compose -f docker-compose.allinone.yml ps

# follow the logs
docker compose -f docker-compose.allinone.yml logs -f --tail 100 carousell-monitor

# health + alert state as the container sees them
docker exec carousell-monitor cat /data/health.json
docker exec carousell-monitor cat /data/alert_state.json

# what credentials did it actually receive?
docker exec carousell-monitor sh -c 'env | grep -E "NOCODB|TELEGRAM|TICK|FAILURE"'

# is NocoDB reachable from the monitor's network?
docker exec carousell-monitor sh -c 'wget -qO- http://nocodb:8080/api/v1/health'

# restart / rebuild / stop
docker compose -f docker-compose.allinone.yml restart carousell-monitor
docker compose -f docker-compose.allinone.yml up -d --build
docker compose -f docker-compose.allinone.yml down
```

---

## 4. Reporting a bug

Include: the `health.json` contents, the last ~50 log lines, the compose file with every credential replaced by `***`, and your NocoDB version (`…/api/v1/version`). Do not paste tokens, chat ids or your base id.
