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

CONDITIONS = ["Brand new", "Like new", "Lightly used", "Well used", "Heavily used", "Used"]
# Carousell 不同类目对 condition 的写法不一：New ≈ Brand new；Used 是笼统二手。
CONDITION_MAP = {
    "brand new": "Brand new",
    "new": "Brand new",
    "like new": "Like new",
    "lightly used": "Lightly used",
    "well used": "Well used",
    "heavily used": "Heavily used",
    "used": "Used",
}
PRODUCT_URL_TMPL = "https://www.carousell.com.my/p/{listing_id}/"
SELLER_URL_TMPL = "https://www.carousell.com.my/u/{username}/"


def strip_thumbnail_suffix(url):
    """去掉 Carousell 缩略图后缀 _progressive_thumbnail，得到高清原图 URL。

    两种形态都去掉：
      1) ..._progressive_thumbnail.jpg  →  ...jpg
      2) ..._progressive_thumbnail       →  ...（无扩展名，仍是合法 JPEG）
    """
    if not url:
        return url
    return url.replace("_progressive_thumbnail", "")

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
    ("notified", "Checkbox"),
    ("skip_notify", "Checkbox"),
]
IGNORED_SELLERS_COLS = [
    ("seller_name", "SingleLineText"),
]
# 忽略关键词（per-watch）：search_url=Settings 里的 watch URL 原样复制，keyword 单行一条。
# 大小写不敏感，命中该 watch 的「标题」即跳过通知（仍归档）。
IGNORED_KEYWORDS_COLS = [
    ("search_url", "URL"),
    ("keyword", "SingleLineText"),
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


def _download_image(url, timeout=20):
    """下载图片到内存 bytes；失败返回 None。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _send_photo_multipart(chat_id, image_bytes, filename, caption):
    """用 multipart/form-data 上传本地图片字节发 sendPhoto。

    逐字段拼装字节（不用 join，避免破坏二进制图片数据）。
    """
    boundary = "----carousellmonitor" + str(int(time.time() * 1000)) + "boundary"
    CRLF = b"\r\n"
    parts = []

    def field(name, value):
        p = ("--" + boundary).encode("utf-8") + CRLF
        p += ("Content-Disposition: form-data; name=\"" + name + "\"").encode("utf-8") + CRLF
        p += CRLF
        p += value.encode("utf-8") + CRLF
        return p

    def file_field(name, filename, data, content_type):
        p = ("--" + boundary).encode("utf-8") + CRLF
        p += ("Content-Disposition: form-data; name=\"" + name +
              "\"; filename=\"" + filename + "\"").encode("utf-8") + CRLF
        p += ("Content-Type: " + content_type).encode("utf-8") + CRLF
        p += CRLF
        p += data + CRLF
        return p

    body = b""
    body += field("chat_id", str(chat_id))
    body += field("caption", caption)
    body += file_field("photo", filename, image_bytes, "image/jpeg")
    body += ("--" + boundary + "--").encode("utf-8") + CRLF

    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendPhoto"
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": UA,
        "Content-Type": "multipart/form-data; boundary=" + boundary,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
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
    ignored_sellers_tid = _ensure_table("IgnoredSellers", IGNORED_SELLERS_COLS)
    ignored_keywords_tid = _ensure_table("IgnoredKeywords", IGNORED_KEYWORDS_COLS)
    return listings_tid, settings_tid, ignored_sellers_tid, ignored_keywords_tid


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
            # 广告卡片：listingID=0（title 常为 ap_promo_*），跳过不归档
            if lid == 0:
                continue
            # 上架时间优先取 time_created；被顶置(bump)的商品只提供
            # active_bump 时间戳，fallback 到它。两者结构相同(timestampContent)。
            ts = None
            for comp in ("time_created", "active_bump"):
                for item in c.get("aboveFold", []):
                    if item.get("component") == comp:
                        tc = item.get("timestampContent") or {}
                        sec = tc.get("seconds") or {}
                        ts = sec.get("low")
                        break
                if ts:
                    break
            bf = c.get("belowFold", [])
            title = next((i["stringContent"] for i in bf
                          if i.get("component") == "header_1"), "")
            price_raw = next((i["stringContent"] for i in bf
                              if i.get("component") == "header_2"), "")
            paras = [i.get("stringContent", "").strip() for i in bf
                     if i.get("component") == "paragraph"]
            # 从所有 paragraph 里找第一个匹配的 condition 值（忽略 Size: 等噪声）
            cond = ""
            for p in paras:
                key = p.lower()
                if key in CONDITION_MAP:
                    cond = CONDITION_MAP[key]
                    break
            thumb = strip_thumbnail_suffix(c.get("thumbnailURL", ""))
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


def load_ignored_sellers(ignored_sellers_tid):
    """从 IgnoredSellers 表读取被忽略的 seller_name 集合。"""
    ignored = set()
    st, j = nc("GET", f"/api/v2/tables/{ignored_sellers_tid}/records?limit=1000")
    if st != 200:
        raise RuntimeError(f"load ignored sellers failed: {j}")
    for r in j.get("list", []):
        name = (r.get("seller_name") or "").strip()
        if name:
            ignored.add(name)
    return ignored


def load_ignored_keywords(ignored_keywords_tid):
    """从 IgnoredKeywords 表读取忽略关键词，返回 {search_url: {小写关键词}}。

    关键词按 watch（search_url）分组；匹配时大小写不敏感。
    """
    ignored = {}
    st, j = nc("GET", f"/api/v2/tables/{ignored_keywords_tid}/records?limit=1000")
    if st != 200:
        raise RuntimeError(f"load ignored keywords failed: {j}")
    for r in j.get("list", []):
        url = (r.get("search_url") or "").strip()
        kw = (r.get("keyword") or "").strip().lower()
        if not url or not kw:
            continue
        ignored.setdefault(url, set()).add(kw)
    return ignored


def title_matches_keyword(title, ignored_keywords):
    """标题命中任一忽略关键词（大小写不敏感的子串匹配）则返回 True。"""
    if not title or not ignored_keywords:
        return False
    t = title.lower()
    return any(kw in t for kw in ignored_keywords)


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


def build_row(l, watch, notified=False):
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
        "notified": notified,
        "skip_notify": False,
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
def _tg_send(method, payload):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        tg(method, {"chat_id": TELEGRAM_CHAT_ID, **payload})
        return True
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"telegram {method} failed: {e.code} {e.read()[:200]}\n")
        return False


def send_telegram(text):
    _tg_send("sendMessage", {"text": text})


def send_listing_from_record(rec):
    """Send one listing notice from a NocoDB record.

    rec fields: title, price, condition, seller_name, product_url, image_url.
    Prefer photo; fall back to text-only if the image fails.
    """
    title = rec.get("title") or "(no title)"
    price = rec.get("price") or ""
    condition = rec.get("condition") or ""
    seller = rec.get("seller_name") or ""
    product_url = rec.get("product_url") or ""
    caption_lines = [f"🛒 {title}"]
    if price:
        caption_lines.append(f"💰 {price}")
    if condition:
        caption_lines.append(f"📦 {condition}")
    if seller:
        caption_lines.append(f"👤 {seller}")
    if product_url:
        caption_lines.append(product_url)
    caption = "\n".join(caption_lines)
    thumb = rec.get("image_url") or ""
    if thumb:
        # 方案1：下载图后 multipart 上传（最稳，Telegram 无需访问 carousell CDN）
        img_bytes = _download_image(thumb)
        if img_bytes:
            try:
                st, raw = _send_photo_multipart(
                    TELEGRAM_CHAT_ID, img_bytes, "listing.jpg", caption)
                if st == 200:
                    return True
                sys.stderr.write(f"multipart sendPhoto: HTTP {st}\n")
            except Exception as e:
                sys.stderr.write(f"multipart sendPhoto failed: {type(e).__name__}\n")
        # 方案2：退回让 Telegram 直接下载 URL
        if _tg_send("sendPhoto", {"photo": thumb, "caption": caption}):
            return True
    # 方案3：纯文本
    return _tg_send("sendMessage", {"text": caption})


def mark_notified(listings_tid, rec_ids):
    """把已发通知的记录 notified 置 true。"""
    if not rec_ids:
        return
    updates = [{"Id": rid, "notified": True} for rid in rec_ids]
    nc("PATCH", f"/api/v2/tables/{listings_tid}/records", updates)


def send_pending_notifications(listings_tid, settings_tid, ignored_sellers_tid,
                               ignored_keywords_tid):
    """tick 末尾统一发：查 notified=false 的记录，逐条发（间隔 1s），发完置 true。

    仅发「其 watch 仍 notify=true」的记录；watch 已关 notify 的则静默置 true。
    若 seller_name 落在 IgnoredSellers 忽略列表，或 title 命中该 watch
    （search_url）在 IgnoredKeywords 里的关键词（大小写不敏感），则置
    skip_notify=true + notified=true，不发 Telegram。
    """
    # 加载所有 watch 的 notify 开关，key = search_title
    st, j = nc("GET", f"/api/v2/tables/{settings_tid}/records?limit=1000")
    if st != 200:
        return
    notify_by_title = {}
    for w in j.get("list", []):
        notify_by_title[w.get("title")] = bool(w.get("notify"))

    # 每轮重新加载忽略列表，中途增删立即生效
    ignored = load_ignored_sellers(ignored_sellers_tid)
    ignored_kw_by_url = load_ignored_keywords(ignored_keywords_tid)

    # 拉 notified=false 的记录
    st, j = nc("GET", f"/api/v2/tables/{listings_tid}/records"
               f"?limit=1000&fields=Id,title,price,condition,seller_name,"
               f"product_url,image_url,search_title,search_url,notified,skip_notify")
    if st != 200:
        return
    pending = [r for r in j.get("list", []) if not r.get("notified")]

    if not pending:
        return

    for rec in pending:
        seller = (rec.get("seller_name") or "").strip()
        title = (rec.get("title") or "").strip()
        search_url = (rec.get("search_url") or "").strip()
        kw_for_watch = ignored_kw_by_url.get(search_url, set())
        if seller in ignored or title_matches_keyword(title, kw_for_watch):
            # 命中忽略卖家/该 watch 的关键词：标记 skip_notify，静默置 notified，不发
            nc("PATCH", f"/api/v2/tables/{listings_tid}/records",
               [{"Id": rec["Id"], "skip_notify": True, "notified": True}])
            continue
        st_title = rec.get("search_title") or ""
        should_notify = notify_by_title.get(st_title, True)
        if should_notify:
            ok = send_listing_from_record(rec)
        else:
            # 词条关了 notify：静默标记，不发
            ok = True
        # 只有发送成功（或无需发）才标记 notified=true；
        # 失败则保留 false，下个 tick 末尾自动重试。
        if ok:
            mark_notified(listings_tid, [rec["Id"]])
        time.sleep(1)  # 最快 1 秒一条，防限流


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
def run_tick(listings_tid, settings_tid, ignored_sellers_tid, ignored_keywords_tid,
             seen, last_run):
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

        # 首次 seed: 静默归档 (notified=True)；后续新商品: 待发 (notified=False)
        rows = [build_row(l, w, notified=first_seed) for l in fresh]
        if rows:
            insert_listings(listings_tid, rows)
            for l in fresh:
                seen.add(PRODUCT_URL_TMPL.format(listing_id=l["listing_id"]))

        new_total += len(fresh)
        update_checked(settings_tid, wid)
        last_run[wid] = now

    # 归档完成后，统一发送待通知的记录（解耦：归档成功才通知）
    send_pending_notifications(listings_tid, settings_tid, ignored_sellers_tid,
                               ignored_keywords_tid)

    ok = len(failures) == 0
    return ok, ("; ".join(failures) if failures else ""), {
        "watch_count": len(watches), "new_this_tick": new_total}


def main():
    if not NOCODB_TOKEN:
        sys.stderr.write("NOCODB_TOKEN not set\n")
        write_health(False, "NOCODB_TOKEN not set")
        sys.exit(2)

    listings_tid, settings_tid, ignored_sellers_tid, ignored_keywords_tid = bootstrap()
    seen = load_seen(listings_tid)
    last_run = {}

    sys.stderr.write(f"ready: listings={listings_tid} settings={settings_tid} "
                     f"ignored_sellers={ignored_sellers_tid} "
                     f"ignored_keywords={ignored_keywords_tid} seen={len(seen)}\n")

    while True:
        try:
            ok, err, extra = run_tick(listings_tid, settings_tid,
                                      ignored_sellers_tid, ignored_keywords_tid,
                                      seen, last_run)
        except Exception as e:
            ok, err, extra = False, f"tick error: {e}", {}
        write_health(ok, err, extra)
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    main()
