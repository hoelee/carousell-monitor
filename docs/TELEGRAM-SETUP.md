# Telegram setup

Two values are needed: a **bot token** (who sends) and a **chat id** (where it sends). Both are free and take about a minute.

---

## 1. Create the bot

1. Open Telegram and search for **@BotFather** (the one with a blue checkmark).
2. Send `/newbot`.
3. It asks for a **name** — anything, e.g. `My Carousell Alerts`.
4. It asks for a **username** — must be unique and end in `bot`, e.g. `mycarousell_alerts_bot`.
5. It replies with a token like:

   ```
   Use this token to access the HTTP API:
   8123456789:AAF7xK3nQw8_your_token_here_9dZ
   ```

   That whole string is `TELEGRAM_BOT_TOKEN`. Treat it like a password — anyone holding it can send and read messages as your bot.

Optional, but nice: `/setdescription` and `/setuserpic` to make the alert messages look intentional.

## 2. Get your chat id (alert yourself)

1. **Send your new bot a message** — click the link BotFather gave you and say `hi`. This step matters: until you have messaged the bot, it is not allowed to message you, and sends fail with `chat not found`.
2. Message **@userinfobot**. It replies with your id:

   ```
   Id: 123456789
   ```

   That number is `TELEGRAM_CHAT_ID`.

## 3. Or alert a group

1. Create the group (or use an existing one) and **add your bot** to it as a member.
2. Send a message in the group (any text).
3. Read the group's chat id:

   ```bash
   curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
   ```

   Look for `"chat":{"id":-1001234567890,"title":"..."}`. Group ids are **negative** and usually start with `-100`. Use that number as `TELEGRAM_CHAT_ID`.

If `getUpdates` returns `{"ok":true,"result":[]}`, the bot has not seen any message yet — send another one in the group and retry.

## 4. Verify before you blame the monitor

```bash
# who am I?
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getMe"

# send a test message
curl -s -X POST "https://api.telegram.org/bot<YOUR_TOKEN>/sendMessage" \
     -d chat_id=<YOUR_CHAT_ID> -d text="hello from carousell-monitor"
```

Expected: `{"ok":true,...}` and a message on your phone. If this works, the monitor's credentials are right and anything missing is a monitor-side issue (filters, `notify` checkbox, pending queue).

| Error | Meaning |
|---|---|
| `401 Unauthorized` | token is wrong, or it was revoked in BotFather |
| `400 chat not found` | wrong chat id, or you never messaged the bot / the bot is not in the group |
| `403 bot was blocked by the user` | you blocked the bot — unblock it |
| the `curl` hangs forever | DNS/IPv6 trouble. The compose files pin `api.telegram.org` to its IPv4 address in `extra_hosts`; refresh that IP with `dig +short api.telegram.org` |

## 5. Rotate it later

If the token leaks: @BotFather → `/revoke` → pick the bot → you get a new token. Update `.env` (or the Portainer stack environment) and restart the monitor.
