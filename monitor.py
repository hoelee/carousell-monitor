#!/usr/bin/env python3
"""Carousell new-listing monitor -> NocoDB archive + Telegram alerts.

Self-bootstrapping: on startup it ensures the NocoDB schema (Listings + Settings
tables) exists, then loops forever. Each tick it reads the watch list from the
Settings table, polls each enabled watch's Carousell search URL on its own
interval, dedupes by product_url (param-less listing URL), archives every listing
to NocoDB (title, numeric price, condition, image URL + thumbnail attachment,
seller, link, timestamps), and — after the first seed — alerts Telegram with
"<title>: N new listing(s)".

Secrets via env vars; operational knobs (enabled / notify / interval) live in the
NocoDB Settings table so they're adjustable from the UI without a redeploy.

Writes /data/health.json every tick; the Docker HEALTHCHECK flags the container
unhealthy on any failed extract / rate-limit / crash.

Stdlib only.
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- #
# Config (env)
# --------------------------------------------------------------------------- #
NOCODB_URL = os.environ.get("NOCODB_URL", "http://nocodb:10380").rstrip("/")
NOCODB_TOKEN = os.environ.get("NOCODB_TOKEN", "")
NOCODB_BASE_ID = os.environ.get("NOCODB_BASE_ID", "poqw1zjw3hnsk37")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
DATA_DIR = os.environ.get("DATA_DIR", "/data")
TICK_SECONDS = int(os.environ.get("TICK_SECONDS", "60"))
HEALTH_STALE_SECONDS = int(os.environ.get("HEALTH_STALE_SECONDS", "600"))
DEFAULT_INTERVAL_MIN = int(os.environ.get("DEFAULT_INTERVAL_MIN", "5"))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

CONDITIONS = ["Brand new", "Like new", "Lightly used", "Well used", "Heavily used"]
PRODUCT_URL_TMPL = "https://www.carousell.com.my/p/{listing_id}/"
SELLER_URL_TMPL = "https://www.carousell.com.my/u/{username}/"

# Column definitions: table title -> list of (title, uidt)
LISTINGS_COLS = [
    ("product_url", "URL"),
    ("title", "SingleLineText"),
    ("price", "Decimal"),
    ("condition", "SingleSelect"),   # options set via 2-pass below
    ("image_url", "URL"),
    ("image", "Attachment"),
    ("seller_name", "SingleLineText"),
    ("seller_url", "URL"),
    ("search_title", "SingleLineText"),
    ("search_url", "URL"),
    ("listed_at", "DateTime"),
    ("first_seen_at", "DateTime"),
]
SETTINGS_COLS = [
    ("title", "SingleLineText"),
    ("url", "URL"),
    ("enabled", "Checkbox"),
    ("notify", "Checkbox"),
    ("check_interval_minutes", "Number"),
    ("last_checked_at", "DateTime"),
]


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _http(method, url, body=None, headers=None, timeout=30):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="ignore")
        return r.status, raw


def _json(status_raw):
    status, raw = status_raw
    return status, (json.loads(raw) if raw else None)


def nc(method, path, body=None):
    return _json(_http(method, f"{NOCODB_URL}{path}", body=body,
                       headers={"xc-token": NOCODB_TOKEN}))


def tg(method, payload):
    return _json(_http(
        method, f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        body=payload))


# --------------------------------------------------------------------------- #
# Schema bootstrap (idempotent)
# --------------------------------------------------------------------------- #
def _ensure_table(title, cols):
    st, tables = nc("GET", f"/api/v2/meta/bases/{NOCODB_BASE_ID}/tables")
    if st != 200:
        raise RuntimeError(f"list tables failed: {tables}")
    tid = None
    for t in tables.get("list", []):
        if t.get("title") == title:
            tid = t["id"]
    if tid is None:
        # NocoDB requires >=1 column inline on create; seed with the first.
        st, out = nc("POST", f"/api/v2/meta/bases/{NOCODB_BASE_ID}/tables",
                     {"title": title, "table_name": title, "type": "table",
                      "columns": [{"title": cols[0][0], "uidt": cols[0][1]}]})
        if st != 200:
            raise RuntimeError(f"create table {title} failed: {out}")
        tid = out["id"]

    def _columns_by_title():
        st, meta = nc("GET", f"/api/v2/meta/tables/{tid}")
        return {c.get("title"): c for c in meta.get("columns", [])}

    existing = _columns_by_title()
    for (ctitle, uidt) in cols:
        if ctitle in existing:
            continue
        st, out = nc("POST", f"/api/v2/meta/tables/{tid}/columns",
                     {"title": ctitle, "uidt": uidt})
        if st != 200:
            raise RuntimeError(f"add column {ctitle} failed: {out}")

    # SingleSelect options. Re-read meta for authoritative column ids: POST
    # /columns returns the *table* object, not the column id.
    existing = _columns_by_title()
    for (ctitle, uidt) in cols:
        if uidt != "SingleSelect" or ctitle not in existing:
            continue
        col = existing[ctitle]
        opts = col.get("colOptions", {}).get("options") or []
        if len(opts) >= len(CONDITIONS):
            continue
        opt_list = [{"title": c, "order": i + 1, "color": None}
                    for i, c in enumerate(CONDITIONS)]
        st, out = nc("PATCH", f"/api/v2/meta/columns/{col['id']}",
                     {"colOptions": {"options": opt_list},
                      "dtxp": ",".join(CONDITIONS)})
        if st != 200:
            raise RuntimeError(f"patch SingleSelect {ctitle} failed: {out}")

    return tid


def bootstrap():
    listings_tid = _ensure_table("Listings", LISTINGS_COLS)
    settings_tid = _ensure_table("Settings", SETTINGS_COLS)
    return listings_tid, settings_tid


# --------------------------------------------------------------------------- #
# Carousell extraction
# --------------------------------------------------------------------------- #
def fetch_listings(search_url):
    st, html = _http("GET", search_url)
    if st != 200:
        raise RuntimeError(f"carousell fetch HTTP {st}")
    blobs = re.findall(r'<script type="application/json">(.*?)</script>',
                       html, re.S)
    if not blobs:
        raise RuntimeError("no application/json state found (blocked/ratelimited?)")
    state = json.loads(max(blobs, key=len))
    cards = state["SearchListing"]["listingCards"]
    out = []
    for c in cards:
        try:
            lid = int(c["listingID"])
            ts = None
            for item in c.get("aboveFold", []):
                if item.get("component") == "time_created":
                    ts = item["timestampContent"]["seconds"]["low"]
                    break
            bf = c.get("belowFold", [])
            title = next((i["stringContent"] for i in bf
                          if i.get("component") == "header_1"), "")
            price_raw = next((i["stringContent"] for i in bf
                              if i.get("component") == "header_2"), "")
            paras = [i.get("stringContent", "") for i in bf
                     if i.get("component") == "paragraph"]
            cond = paras[1].strip() if len(paras) > 1 else ""
            if cond not in CONDITIONS:
                cond = ""
            thumb = c.get("thumbnailURL", "")
            seller = (c.get("seller") or {}).get("username", "")
            out.append({
                "listing_id": lid,
                "title": title,
                "price": price_raw,
                "condition": cond,
                "thumbnail": thumb,
                "seller": seller,
                "ts": ts,
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out


def parse_price(s):
    if not s:
        return None
    t = re.sub(r"[^0-9.]", "", s)
    if not t:
        return None
    try:
        return round(float(t), 2)
    except ValueError:
        return None


def mimetype_for(url):
    p = url.lower()
    if ".png" in p:
        return "image/png"
    if ".webp" in p:
        return "image/webp"
    if ".gif" in p:
        return "image/gif"
    return "image/jpeg"


def iso_now():
    # UTC: NocoDB parses naive datetimes as UTC.
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def iso_from_epoch(epoch):
    if not epoch:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


# --------------------------------------------------------------------------- #
# NocoDB row IO
# --------------------------------------------------------------------------- #
def load_seen(listings_tid):
    seen = set()
    limit, offset = 1000, 0
    while True:
        st, j = nc("GET", f"/api/v2/tables/{listings_tid}/records"
                   f"?limit={limit}&offset={offset}")
        if st != 200:
            raise RuntimeError(f"load seen failed: {j}")
        lst = j.get("list", [])
        for r in lst:
            if r.get("product_url"):
                seen.add(r["product_url"])
        if len(lst) < limit:
            break
        offset += limit
    return seen


def load_watches(settings_tid):
    st, j = nc("GET", f"/api/v2/tables/{settings_tid}/records?limit=1000")
    if st != 200:
        raise RuntimeError(f"load watches failed: {j}")
    watches = []
    for r in j.get("list", []):
        if r.get("enabled"):
            watches.append(r)
    return watches


def insert_listings(listings_tid, rows):
    if not rows:
        return
    st, out = nc("POST", f"/api/v2/tables/{listings_tid}/records", rows)
    if st != 200:
        raise RuntimeError(f"insert listings failed: {out}")


def update_checked(settings_tid, watch_id):
    nc("PATCH", f"/api/v2/tables/{settings_tid}/records",
       [{"Id": watch_id, "last_checked_at": iso_now()}])


def build_row(l, watch):
    row = {
        "product_url": PRODUCT_URL_TMPL.format(listing_id=l["listing_id"]),
        "title": l["title"],
        "image_url": l["thumbnail"],
        "image": json.dumps([{"path": l["thumbnail"],
                              "mimetype": mimetype_for(l["thumbnail"]),
                              "title": f"{l['listing_id']}.jpg"}]),
        "seller_name": l["seller"],
        "seller_url": SELLER_URL_TMPL.format(username=l["seller"]),
        "search_title": watch.get("title", ""),
        "search_url": watch.get("url", ""),
        "first_seen_at": iso_now(),
    }
    price = parse_price(l["price"])
    if price is not None:
        row["price"] = price
    if l["condition"]:
        row["condition"] = l["condition"]
    listed = iso_from_epoch(l["ts"])
    if listed:
        row["listed_at"] = listed
    return row


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        tg("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": text})
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"telegram send failed: {e.code} {e.read()[:200]}\n")


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def write_health(ok, error, extra=None):
    os.makedirs(DATA_DIR, exist_ok=True)
    h = {"last_run_epoch": int(time.time()), "ok": ok, "error": error or ""}
    if extra:
        h.update(extra)
    tmp = os.path.join(DATA_DIR, "health.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f)
    os.replace(tmp, os.path.join(DATA_DIR, "health.json"))


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def run_tick(listings_tid, settings_tid, seen, last_run):
    failures = []
    new_total = 0
    watches = load_watches(settings_tid)
    now = time.time()

    for w in watches:
        wid = w.get("Id")
        interval_min = w.get("check_interval_minutes") or DEFAULT_INTERVAL_MIN
        interval_sec = max(int(interval_min), 1) * 60
        if wid in last_run and (now - last_run[wid]) < interval_sec:
            continue

        try:
            listings = fetch_listings(w.get("url", ""))
        except Exception as e:
            failures.append(f"{w.get('title')}: {e}")
            # still advance so a hard-failing watch doesn't hammer every tick
            last_run[wid] = now
            continue

        first_seed = not w.get("last_checked_at")
        fresh = [l for l in listings
                 if PRODUCT_URL_TMPL.format(listing_id=l["listing_id"]) not in seen]

        rows = [build_row(l, w) for l in fresh]
        if rows:
            insert_listings(listings_tid, rows)
            for l in fresh:
                seen.add(PRODUCT_URL_TMPL.format(listing_id=l["listing_id"]))

        if fresh and not first_seed and w.get("notify", True):
            send_telegram(f"{w.get('title')}: {plural(len(fresh), 'new listing')}")

        new_total += len(fresh)
        update_checked(settings_tid, wid)
        last_run[wid] = now

    ok = len(failures) == 0
    return ok, ("; ".join(failures) if failures else ""), {
        "watch_count": len(watches), "new_this_tick": new_total}


def main():
    if not NOCODB_TOKEN:
        sys.stderr.write("NOCODB_TOKEN not set\n")
        write_health(False, "NOCODB_TOKEN not set")
        sys.exit(2)

    listings_tid, settings_tid = bootstrap()
    seen = load_seen(listings_tid)
    last_run = {}

    sys.stderr.write(f"ready: listings={listings_tid} settings={settings_tid} "
                     f"seen={len(seen)}\n")

    while True:
        try:
            ok, err, extra = run_tick(listings_tid, settings_tid, seen, last_run)
        except Exception as e:
            ok, err, extra = False, f"tick error: {e}", {}
        write_health(ok, err, extra)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
