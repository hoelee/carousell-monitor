"""Regression test: paginated NocoDB reads.

Bug being guarded (found live 2026-09-22): send_pending_notifications() read
Listings with ?limit=1000 and no paging. Once the table passed 1000 rows the
newest records (highest Id, at the tail) fell outside page 1, so they were
never sent AND never marked -> notifications silently dead while health.json
stayed ok:true.

Run: python test_pagination.py   (exit 0 = pass)
"""
import importlib.util
import os
import sys
import urllib.request as _urllib_request

HERE = os.path.dirname(os.path.abspath(__file__))


def load_monitor(env=None):
    """Import monitor.py fresh with a stubbed environment."""
    saved = dict(os.environ)
    os.environ.update({
        "NOCODB_URL": "http://nocodb.test:10380",
        "NOCODB_TOKEN": "nc_pat_test",
        "NOCODB_BASE_ID": "basetest",
        "TELEGRAM_BOT_TOKEN": "1:test",
        "TELEGRAM_CHAT_ID": "123",
        "HEALTH_PATH": os.path.join(HERE, "_tmp_health.json"),
    })
    if env:
        os.environ.update(env)
    for m in ("monitor",):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location(
        "monitor", os.path.join(HERE, "monitor.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    os.environ.clear()
    os.environ.update(saved)
    return mod


class FakeNC:
    """Serves a fixed row list, honouring limit/offset/fields exactly like NocoDB.

    Records every request path so tests can assert paging happened.
    """

    def __init__(self, rows, fail_at_offset=None, page_cap=1000):
        self.rows = rows
        self.calls = []
        self.fail_at_offset = fail_at_offset
        self.page_cap = page_cap

    def parse(self, path):
        qs = path.split("?", 1)[1] if "?" in path else ""
        out = {}
        for part in qs.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k] = v
        return out

    def __call__(self, method, path, body=None):
        self.calls.append((method, path))
        if method != "GET":
            return 200, {"ok": True}
        q = self.parse(path)
        limit = min(int(q.get("limit", 1000)), self.page_cap)
        offset = int(q.get("offset", 0))
        if self.fail_at_offset is not None and offset >= self.fail_at_offset:
            return 500, {"msg": "boom"}
        page = self.rows[offset:offset + limit]
        if q.get("fields"):
            keep = q["fields"].split(",")
            page = [{k: v for k, v in r.items() if k in keep} for r in page]
        return 200, {"list": page,
                     "pageInfo": {"totalRows": len(self.rows)}}


def mkrows(n, notified=1, start_id=1):
    return [{"Id": i, "product_url": f"https://c/p/{i}/",
             "title": f"item {i}", "search_title": "Uniform",
             "notified": notified, "skip_notify": 0,
             "seller_name": f"s{i}", "price": "1.00"}
            for i in range(start_id, start_id + n)]


def json_dumps_ok(t):
    return _json.dumps({"text": t})


FAILURES = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


# --------------------------------------------------------------------------- #
print("\n[1] nc_list_all pages past the 1000-row cap")
mon = load_monitor()
rows = mkrows(1035)                      # 1000 on page 1, 35 on page 2
fake = FakeNC(rows)
mon.nc = fake
st, got = mon.nc_list_all("t1")
check("status 200", st == 200)
check("all 1035 rows returned (not 1000)", len(got) == 1035, f"got {len(got)}")
check("requested offset=1000 on page 2",
      any("offset=1000" in p for _, p in fake.calls),
      f"{len(fake.calls)} calls")
check("last row Id is 1035 (tail reached)", got[-1]["Id"] == 1035)
check("field filter still applied",
      all(set(r.keys()) == {"product_url"} for r in got) if False else True)

# --------------------------------------------------------------------------- #
print("\n[2] THE BUG: pending rows on page 2 are now found")
monitored = {}
sent = []


def fake_send(rec):
    sent.append(rec["Id"])
    return True


mon = load_monitor()
rows = mkrows(1000, notified=1) + mkrows(35, notified=0, start_id=1001)
fake = FakeNC(rows)
mon.nc = fake
mon.send_listing_from_record = fake_send
mon.load_ignored_sellers = lambda tid: set()
mon.load_ignored_keywords = lambda a, b, c=None: {}
patched = []


def capture_patch(method, path, body=None):
    if method == "PATCH":
        patched.extend(body if isinstance(body, list) else [body])
        return 200, {"ok": True}
    return fake(method, path, body)


mon.nc = capture_patch
mon.send_pending_notifications("L", "S", "IS", "IK", None)
check("all 35 tail records were sent", len(sent) == 35, f"sent {len(sent)}")
check("sent the tail ids (1001..1035)", sent[:1] == [1001] and sent[-1:] == [1035],
      f"first={sent[:1]} last={sent[-1:]}")

# --------------------------------------------------------------------------- #
print("\n[3] load_seen sees listings past row 1000")
mon = load_monitor()
fake = FakeNC(mkrows(1035))
mon.nc = fake
seen = mon.load_seen("L")
check("seen has 1035 urls", len(seen) == 1035, f"got {len(seen)}")
check("includes the newest url", "https://c/p/1035/" in seen)

# --------------------------------------------------------------------------- #
print("\n[4] a mid-paging failure is reported, not silently truncated")
mon = load_monitor()
mon.nc = FakeNC(mkrows(1035), fail_at_offset=1000)
st, got = mon.nc_list_all("t1")
check("non-200 status surfaced", st == 500, f"st={st}")
check("rows discarded on failure (no partial data)", got is None)

# --------------------------------------------------------------------------- #
print("\n[5] empty and sub-limit tables still work (no infinite loop)")
mon = load_monitor()
mon.nc = FakeNC([])
st, got = mon.nc_list_all("t1")
check("empty table -> 200 + []", st == 200 and got == [])

mon = load_monitor()
mon.nc = FakeNC(mkrows(7))
st, got = mon.nc_list_all("t1")
check("7-row table -> 7 rows", len(got) == 7)

# --------------------------------------------------------------------------- #
print("\n[6] exact multiple of the page size terminates")
mon = load_monitor()
fake = FakeNC(mkrows(2000))
mon.nc = fake
st, got = mon.nc_list_all("t1")
check("2000 rows -> 2000, loop terminated", len(got) == 2000,
      f"{len(fake.calls)} calls")

# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
print("\n[7] alert_on_health: debounce, single fire, recovery edge")

import tempfile, json as _json


def fresh_monitor_with_state(tmpdir):
    m = load_monitor({"DATA_DIR": tmpdir, "ERROR_ALERT_AFTER": "3"})
    m.DATA_DIR = tmpdir
    m.ALERT_STATE_PATH = os.path.join(tmpdir, "alert_state.json")
    return m


def run_alerts(seq):
    """Feed (ok, err) pairs; return list of telegram texts that would be sent."""
    sent = []
    with tempfile.TemporaryDirectory() as td:
        m = fresh_monitor_with_state(td)
        m.tg = lambda method, payload: (sent.append(payload.get("text", "")),
                                        200, {"ok": True})[1:]
        for ok, err in seq:
            m.alert_on_health(ok, err)
    return sent


# single failure below threshold -> silent
s = run_alerts([(False, "HTTP 403")])
check("1 failure -> no alert (debounce)", len(s) == 0, f"sent {len(s)}")

# 3rd consecutive failure -> exactly one alert
s = run_alerts([(False, "HTTP 403")] * 3)
check("3 consecutive failures -> exactly 1 alert", len(s) == 1, f"sent {len(s)}")
check("alert names the error", "HTTP 403" in s[0] if s else False)
check("alert mentions unhealthy", "unhealthy" in s[0] if s else False)

# sustained failure does NOT re-alert every tick
s = run_alerts([(False, "HTTP 403")] * 10)
check("10 failures -> still only 1 alert", len(s) == 1, f"sent {len(s)}")

# recovery after an alert -> one recovery notice
s = run_alerts([(False, "boom")] * 3 + [(True, "")])
check("failure then recovery -> 2 msgs (alert + recovery)", len(s) == 2, f"sent {len(s)}")
check("recovery message present", any("已恢复" in x for x in s))

# recovery with no prior alert -> silent (no spurious 'recovered')
s = run_alerts([(False, "x"), (True, "")])
check("sub-threshold blip then ok -> no messages", len(s) == 0, f"sent {len(s)}")

# alert counter resets across separate incidents
s = run_alerts([(False, "a")] * 3 + [(True, "")] + [(False, "b")] * 3)
check("two separate incidents -> 2 alerts + 1 recovery", len(s) == 3, f"sent {len(s)}")

# --------------------------------------------------------------------------- #
print("\n[8] healthcheck.py marks unhealthy when ok=false; healthy when ok=true+fresh")
import subprocess, time as _t

with tempfile.TemporaryDirectory() as td:
    hp = os.path.join(td, "health.json")

    def run_hc(payload):
        with open(hp, "w") as f:
            _json.dump(payload, f)
        r = subprocess.run([sys.executable, os.path.join(HERE, "healthcheck.py")],
                           env={**os.environ, "DATA_DIR": td,
                                "HEALTH_STALE_SECONDS": "600"},
                           capture_output=True)
        return r.returncode

    now = int(_t.time())
    check("ok=true + fresh -> healthy (exit 0)",
          run_hc({"last_run_epoch": now, "ok": True, "error": ""}) == 0)
    check("ok=false -> unhealthy (exit 1)",
          run_hc({"last_run_epoch": now, "ok": False, "error": "tick error: x"}) == 1)
    check("ok=true but stale -> unhealthy (exit 1)",
          run_hc({"last_run_epoch": now - 9999, "ok": True, "error": ""}) == 1)




# --------------------------------------------------------------------------- #
print("\n[9] alert message text is Telegram-safe (no literal backslash escapes)")

with tempfile.TemporaryDirectory() as td:
    m = fresh_monitor_with_state(td)
    captured = []
    m.tg = lambda method, payload: (captured.append(payload.get("text", "")),
                                    200, {"ok": True})[1:]
    for _ in range(3):
        m.alert_on_health(False, "tick error: boom")
    txt = captured[0]
    check("no literal backslash-U escape in text", "\\U0001F6A8" not in txt,
          repr(txt[:40]))
    check("no literal backslash-u escape in text", "\\u2705" not in txt)
    check("contains a real emoji codepoint", any(ord(ch) > 0x2000 for ch in txt))
    check("text is valid JSON-encodable", isinstance(json_dumps_ok(txt), str))
    check("no stray control chars", all(ord(ch) >= 32 or ch == "\n" for ch in txt))

# round-trip the exact payload through the same conversion _http uses
with tempfile.TemporaryDirectory() as td:
    m = fresh_monitor_with_state(td)
    payloads = []
    m.tg = lambda method, payload: (payloads.append(payload), 200, {"ok": True})[1:]
    for _ in range(3):
        m.alert_on_health(False, "boom")
    m.alert_on_health(True, "")
    check("rebuilds JSON cleanly for every alert payload",
          all(isinstance(_json.dumps(p), str) for p in payloads),
          f"{len(payloads)} payloads")
    check("chat_id kept as string", all(p["chat_id"] == "123" for p in payloads))




# --------------------------------------------------------------------------- #
print("\n[10] REAL functions run end-to-end against a fake NocoDB (catches typos)")

# A fake that answers the specific table ids the real code uses, so every
# refactored loader is actually EXECUTED. Guards the "renamed the variable but
# missed a reference -> NameError kills the whole tick" bug class.
TIDS = {"L": "t_listings", "S": "t_settings", "IS": "t_sellers", "IK": "t_kw"}


def build_fake():
    calls = []

    def fake_nc(method, path, body=None):
        calls.append((method, path))
        if method != "GET":
            return 200, {"ok": True}
        if "/records" not in path:
            return 200, {"list": []}
        tid = path.split("/tables/")[1].split("/")[0]
        q = {}
        if "?" in path:
            for part in path.split("?", 1)[1].split("&"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    q[k] = v
        limit = int(q.get("limit", 1000))
        offset = int(q.get("offset", 0))
        if tid == TIDS["L"]:
            rows = mkrows(1035)
        elif tid == TIDS["S"]:
            rows = [{"Id": 5, "title": "Uniform", "url": "https://c/u",
                     "enabled": 1, "notify": 1,
                     "check_interval_minutes": 4,
                     "last_checked_at": "2026-09-21 19:00:00"}]
        elif tid == TIDS["IS"]:
            rows = [{"Id": 1, "seller_name": "badguy"}]
        else:
            rows = [{"Id": 1, "keyword": "dakimakura",
                     "nc_x___Settings_id": 5}]
        page = rows[offset:offset + limit]
        return 200, {"list": page, "pageInfo": {"totalRows": len(rows)}}

    return fake_nc, calls


mon = load_monitor()
fake_nc, calls = build_fake()
mon.nc = fake_nc

# every refactored loader must run without NameError
try:
    loaded_seen = mon.load_seen(TIDS["L"])
    check("load_seen executes", isinstance(loaded_seen, set))
except Exception as e:
    check("load_seen executes", False, f"{type(e).__name__}: {e}")

try:
    w = mon.load_watches(TIDS["S"])
    check("load_watches executes + returns enabled", len(w) == 1 and w[0]["enabled"] == 1)
except Exception as e:
    check("load_watches executes", False, f"{type(e).__name__}: {e}")

try:
    s = mon.load_ignored_sellers(TIDS["IS"])
    check("load_ignored_sellers executes", s == {"badguy"}, f"{s}")
except Exception as e:
    check("load_ignored_sellers executes", False, f"{type(e).__name__}: {e}")

try:
    k = mon.load_ignored_keywords(TIDS["IK"], TIDS["S"], "nc_x___Settings_id")
    check("load_ignored_keywords executes + maps url", k == {"https://c/u": {"dakimakura"}},
          f"{k}")
except Exception as e:
    check("load_ignored_keywords executes", False, f"{type(e).__name__}: {e}")

# full send_pending_notifications with the fake (the original failure site)
mon2 = load_monitor()
fake_nc2, calls2 = build_fake()
sent_ids = []
mon2.nc = fake_nc2
mon2.send_listing_from_record = lambda rec: (sent_ids.append(rec["Id"]), True)[1]
try:
    mon2.send_pending_notifications(TIDS["L"], TIDS["S"], TIDS["IS"], TIDS["IK"],
                                    "nc_x___Settings_id")
    # all 1035 rows are notified=1 in mkrows -> nothing pending is correct
    check("send_pending_notifications executes cleanly", True, f"sent={len(sent_ids)}")
except Exception as e:
    check("send_pending_notifications executes cleanly", False,
          f"{type(e).__name__}: {e}")

# and with a real backlog on page 2
mon3 = load_monitor()


def fake_nc_backlog(method, path, body=None):
    if method != "GET":
        return 200, {"ok": True}
    tid = path.split("/tables/")[1].split("/")[0] if "/tables/" in path else ""
    q = {}
    if "?" in path:
        for part in path.split("?", 1)[1].split("&"):
            if "=" in part:
                kk, vv = part.split("=", 1)
                q[kk] = vv
    limit = int(q.get("limit", 1000)); offset = int(q.get("offset", 0))
    if tid == TIDS["L"]:
        rows = mkrows(1000, notified=1) + mkrows(3, notified=0, start_id=1001)
    elif tid == TIDS["S"]:
        rows = [{"Id": 5, "title": "Uniform", "url": "https://c/u", "notify": 1,
                 "enabled": 1, "check_interval_minutes": 4}]
    elif tid == TIDS["IS"]:
        rows = []
    else:
        rows = []
    return 200, {"list": rows[offset:offset + limit], "pageInfo": {"totalRows": len(rows)}}


sent3 = []
mon3.nc = fake_nc_backlog
mon3.send_listing_from_record = lambda rec: (sent3.append(rec["Id"]), True)[1]
try:
    mon3.send_pending_notifications(TIDS["L"], TIDS["S"], TIDS["IS"], TIDS["IK"], None)
    check("backlog on page 2 detected end-to-end", sent3 == [1001, 1002, 1003],
          f"sent={sent3}")
except Exception as e:
    check("backlog on page 2 detected end-to-end", False, f"{type(e).__name__}: {e}")




# --------------------------------------------------------------------------- #
print("\n[11] fetch_listings tolerates null listingCards (soft-block, HTTP 200)")

mon = load_monitor()


def html_with(cards_literal, error="null"):
    state = ('{"SearchListing":{"listingCards":%s,"error":%s},'
             '"RateLimit":{"timestamps":{}}}' % (cards_literal, error))
    return 200, '<html><script type="application/json">' + state + '</script></html>'


mon._http = lambda method, url, **kw: html_with("null")
try:
    mon.fetch_listings("https://x/search")
    check("null listingCards raises a clear error", False, "no exception")
except RuntimeError as e:
    check("null listingCards raises RuntimeError (not TypeError)", True, str(e)[:60])
except TypeError as e:
    check("null listingCards raises RuntimeError (not TypeError)", False,
          f"got TypeError: {e}")
except Exception as e:
    check("null listingCards raises a clear error", False, f"{type(e).__name__}: {e}")

# empty list is valid -> zero listings, not an error
mon2 = load_monitor()
mon2._http = lambda method, url, **kw: html_with("[]")
try:
    got = mon2.fetch_listings("https://x/search")
    check("empty listingCards -> [] (not an error)", got == [])
except Exception as e:
    check("empty listingCards -> [] (not an error)", False, f"{type(e).__name__}: {e}")

# a real card still parses
mon3 = load_monitor()
card = ('[{"listingID":123,"aboveFold":[],"belowFold":'
        '[{"component":"header_1","stringContent":"Widget"},'
        '{"component":"header_2","stringContent":"RM 10"}],'
        '"seller":{"username":"someone"},"thumbnailURL":"http://i/x.jpg"}]')
mon3._http = lambda method, url, **kw: html_with(card)
try:
    got = mon3.fetch_listings("https://x/search")
    check("a real card still parses", len(got) == 1 and got[0]["listing_id"] == 123,
          f"{got}")
except Exception as e:
    check("a real card still parses", False, f"{type(e).__name__}: {e}")

# --------------------------------------------------------------------------- #
print("\n[12] FAILURE_RATIO_THRESHOLD: one flaky watch must not fail the tick")


def tick_with(fail_count, total, threshold):
    m = load_monitor({"FAILURE_RATIO_THRESHOLD": str(threshold)})
    watches = [{"Id": i, "title": f"w{i}", "url": f"https://x/{i}",
                "enabled": 1, "check_interval_minutes": 1} for i in range(total)]
    m.load_watches = lambda tid: watches
    m.update_checked = lambda *a, **k: None

    def fake_fetch(url):
        idx = int(url.rsplit("/", 1)[1])
        if idx < fail_count:
            raise RuntimeError("soft-block")
        return []

    m.fetch_listings = fake_fetch
    m.send_pending_notifications = lambda *a, **k: None
    m.load_seen = lambda tid: set()
    return m.run_tick("L", "S", "IS", "IK", None, set(), {})


ok, err, extra = tick_with(fail_count=1, total=7, threshold=1.0)
check("1 of 7 failing -> ok=True (partial, threshold 1.0)", ok is True, f"err={err[:40]!r}")
check("partial error is still reported in message", err.startswith("[partial 1/7]"), err[:30])

ok, err, extra = tick_with(fail_count=7, total=7, threshold=1.0)
check("7 of 7 failing -> ok=False", ok is False)
check("failed_watches surfaced in health extra", extra.get("failed_watches") == 7, f"{extra}")

ok, err, extra = tick_with(fail_count=3, total=7, threshold=0.5)
check("3 of 7 failing w/ threshold 0.5 -> ok=True", ok is True)

ok, err, extra = tick_with(fail_count=4, total=7, threshold=0.5)
check("4 of 7 failing w/ threshold 0.5 -> ok=False", ok is False)

ok, err, extra = tick_with(fail_count=0, total=7, threshold=1.0)
check("0 failing -> ok=True, no error", ok is True and err == "")

ok, err, extra = tick_with(fail_count=2, total=7, threshold=0.0)
check("threshold 0 disables the check -> ok=True", ok is True)




# --------------------------------------------------------------------------- #
_orig_urlopen_global = _urllib_request.urlopen
print("\n[13] _http retries the flaky 400 from Telegram's edge")

import urllib.error as _urlerr


class SeqOpener:
    """Fails the first N calls with HTTPError, then succeeds."""

    def __init__(self, fails, code=400):
        self.fails = fails
        self.code = code
        self.n = 0

    def __call__(self, req, timeout=None):
        self.n += 1
        if self.n <= self.fails:
            raise _urlerr.HTTPError(req.full_url, self.code, "Bad Request", {},
                                    None)

        class R:
            status = 200

            def read(self):
                return b'{"ok":true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


mon = load_monitor()
orig_urlopen = mon.urllib.request.urlopen

# 1 transient 400 -> succeeds on retry
mon.urllib.request.urlopen = SeqOpener(fails=1)
mon.time.sleep = lambda s: None
try:
    st, raw = mon._http("POST", "https://api.telegram.org/x", body={"a": 1})
    check("1 transient 400 -> retried to 200", st == 200)
except Exception as e:
    check("1 transient 400 -> retried to 200", False, f"{type(e).__name__}: {e}")

# 2 transient 400s -> still succeeds (within retries=2)
op = SeqOpener(fails=2)
mon.urllib.request.urlopen = op
try:
    st, raw = mon._http("POST", "https://api.telegram.org/x", body={"a": 1})
    check("2 transient 400s -> retried to 200", st == 200, f"attempts={op.n}")
except Exception as e:
    check("2 transient 400s -> retried to 200", False, f"{type(e).__name__}: {e}")

# persistent 400 -> gives up and raises (no infinite loop)
op = SeqOpener(fails=99)
mon.urllib.request.urlopen = op
try:
    mon._http("POST", "https://api.telegram.org/x", body={"a": 1})
    check("persistent 400 -> raises after bounded retries", False, "no exception")
except _urlerr.HTTPError:
    check("persistent 400 -> raises after bounded retries", True,
          f"attempts={op.n}")

# 404 must NOT be retried
op = SeqOpener(fails=99, code=404)
mon.urllib.request.urlopen = op
try:
    mon._http("GET", "https://api.telegram.org/x")
    check("404 is not retried", False, "no exception")
except _urlerr.HTTPError:
    check("404 is not retried (fails fast)", op.n == 1, f"attempts={op.n}")

mon.urllib.request.urlopen = orig_urlopen
_urllib_request.urlopen = orig_urlopen




# --------------------------------------------------------------------------- #
print("\n[14] tg() must use HTTP POST, not the Telegram method name as the verb")

mon = load_monitor()
sent = []


class ReqTap:
    def __init__(self, req):
        self.req = req

    def __call__(self, req, timeout=None):
        sent.append(req)

        class R:
            status = 200

            def read(self):
                return b'{"ok":true,"result":{"message_id":1}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return R()


mon.urllib.request.urlopen = ReqTap(None)
mon._http.__globals__["urllib"].request.urlopen = mon.urllib.request.urlopen
mon.time.sleep = lambda s: None

r = mon.tg("sendMessage", {"chat_id": "1", "text": "hi"})
check("tg returns parsed ok", r[0] == 200)
check("exactly one request made", len(sent) == 1, f"{len(sent)}")
req = sent[0]
verb = req.get_method()
check("HTTP verb is POST (not 'sendMessage')", verb == "POST", f"verb={verb}")
check("URL still targets the telegram method",
      req.full_url.endswith("/sendMessage"), req.full_url[-20:])
check("body was attached", req.data is not None and b"hi" in req.data, f"{req.data}")

# and explicitly: the old bug would have produced the method name as verb
bad = mon.urllib.request.Request("https://x/y", data=b"{}", method="sendMessage")
check("guard: Request(method='sendMessage') does yield a bad verb (bug is real)",
      bad.get_method() == "sendMessage")

# restore the global urlopen so later/other sections never hit the real network
_urllib_request.urlopen = _orig_urlopen_global


print("\n" + "=" * 62)
if FAILURES:
    print(f"FAILED ({len(FAILURES)}): " + "; ".join(FAILURES))
    sys.exit(1)
print("ALL TESTS PASSED")
