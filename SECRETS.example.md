# SECRETS.example.md — credential template

Copy to `SECRETS.md` (gitignored) or keep the values in your own secret store.
**Never commit real credentials** — the tracked repo carries only this template.

| Var | Value / source |
|---|---|
| `NOCODB_URL` | `http://nocodb:8080` (all-in-one) or your existing NocoDB URL |
| `NOCODB_BASE_ID` | NocoDB → open the base → copy the id from the URL |
| `NOCODB_TOKEN` | NocoDB → Account Settings → Tokens → Create token (`nc_pat_…`) |
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` → token |
| `TELEGRAM_CHAT_ID` | @userinfobot, or `getUpdates` for a group |
| `NC_AUTH_JWT_SECRET` | `openssl rand -hex 32` (all-in-one stack only) |

## Where the values are used

| Store | Used by | Committed? |
|---|---|---|
| `.env` (repo root) | `docker compose` local runs | no (gitignored) |
| Portainer stack environment / stack file | Portainer deploys | no (lives on the Portainer host) |
| `SECRETS.md` | human inventory | no (gitignored) |

If a token ever leaks, regenerate it: NocoDB → Account Settings → Tokens
(revoke + create), Telegram → @BotFather → `/revoke`.
