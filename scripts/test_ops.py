#!/usr/bin/env python3
"""The logged-in operations page (/ops) must stay closed to everyone else.

Offline: a temporary database, FastAPI's test client, no network. Each case is
something that would leak the machine's state, a secret, or a write button if
it regressed.

Run: python3 scripts/test_ops.py
"""
import os
import pathlib
import sys
import tempfile

KEY = "k" * 64
os.environ["ADMIN_API_KEY"] = KEY          # read at import time
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import ops  # noqa: E402
from app.db import connect  # noqa: E402

DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
BASE = "https://testserver"
SAME = {"origin": BASE}
FOREIGN = {"origin": "https://evil.example"}

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


def client(ip):
    # A distinct CF-Connecting-IP per case keeps the failed-login counter of
    # one case from locking out the next.
    return TestClient(APP, base_url=BASE, headers={"cf-connecting-ip": ip})


def login(c, key=KEY, headers=SAME):
    return c.post("/ops/login", data={"key": key}, headers=headers, follow_redirects=False)


print("Logged out")
c = client("198.51.100.1")
r = c.get("/ops")
check("/ops without a session shows the login form", r.status_code == 200 and 'action="/ops/login"' in r.text)
check("…and nothing from the dashboard", "Behöver uppmärksamhet" not in r.text and "Ops Central" not in r.text)
check("/api/ops without a session is refused", c.get("/api/ops").status_code == 401)
r2 = c.get("/ops/maskinen", follow_redirects=False)
check("an ops page without a session sends you to the login", r2.status_code == 303 and r2.headers["location"] == "/ops")
check("/ops is never cached", r.headers.get("cache-control") == "no-store")
check("/ops is not indexed", "noindex" in (r.headers.get("x-robots-tag") or ""))
check("robots.txt keeps crawlers off /ops", "Disallow: /ops" in c.get("/robots.txt").text)

print("Logging in")
r = login(c, key="wrong")
check("a wrong key is refused", r.status_code == 303 and "fel=nyckel" in r.headers["location"])
check("…and sets no session", ops.COOKIE not in r.cookies)
r = login(c, headers={})
check("a login posted without Origin is refused", r.status_code == 403)
r = login(c, headers=FOREIGN)
check("a login posted from another site is refused", r.status_code == 403)
r = login(c)
set_cookie = r.headers.get("set-cookie", "")
check("the right key logs in", r.status_code == 303 and r.headers["location"] == "/ops")
check("the session cookie is HttpOnly, Secure and SameSite=Strict",
      all(x in set_cookie.lower() for x in ("httponly", "secure", "samesite=strict")), set_cookie)
check("the cookie does not contain the key", KEY not in set_cookie)

print("Logged in")
r = c.get("/ops")
check("/ops shows the overview", r.status_code == 200 and "Behöver uppmärksamhet" in r.text and "Ops Central" in r.text)
check("the key appears nowhere on the page", KEY not in r.text)
SECTIONS = ["maskinen", "trafik", "fel", "synk", "agenter", "inloggningar", "versioner", "konfiguration"]
check("the side panel links every page", all(f'href="/ops/{x}"' in r.text for x in SECTIONS))
MARK = {"maskinen": "Rader per tabell", "trafik": "Anrop per timme", "fel": "sedan start", "synk": "Kör synk nu",
        "agenter": "Senaste verktygsanropen", "inloggningar": "Alla försök", "versioner": "Driftsättningar",
        "konfiguration": "Admin-nyckel"}
for x in SECTIONS:
    rx = c.get(f"/ops/{x}")
    check(f"/ops/{x} renders its page", rx.status_code == 200 and MARK[x] in rx.text and 'class="active"' in rx.text, f"HTTP {rx.status_code}")
    check(f"/ops/{x} does not contain the key", KEY not in rx.text)
check("an unknown ops page is a 404", c.get("/ops/nope").status_code == 404)
r = c.get("/api/ops")
check("/api/ops answers", r.status_code == 200)
body = r.text
check("the key appears nowhere in /api/ops", KEY not in body)
check("…which says only that a key is defined", r.json()["konfiguration"]["config"]["admin_key_defined"] is True)
check("the deployment history has this start", len(r.json()["versioner"]["deploys"]) >= 1)
check("…and every page is in it", all(x in r.json() for x in SECTIONS + ["overview", "warnings"]))

print("Writes on the session")
q = {"sql": "SELECT 1"}
check("a same-origin write is allowed", c.post("/api/admin/query", json=q, headers=SAME).status_code == 200)
check("a write from another site is refused", c.post("/api/admin/query", json=q, headers=FOREIGN).status_code == 403)
check("a write without Origin is refused", c.post("/api/admin/query", json=q).status_code == 403)

print("The header path agents and scripts use")
h = client("198.51.100.2")
check("X-Admin-Key still opens /api/ops", h.get("/api/ops", headers={"x-admin-key": KEY}).status_code == 200)
check("a wrong X-Admin-Key is refused", h.get("/api/ops", headers={"x-admin-key": "nope"}).status_code == 401)
check("…even with a valid session beside it",
      c.get("/api/ops", headers={"x-admin-key": "nope"}).status_code == 401)

print("Forged and expired sessions")
f = client("198.51.100.3")
f.cookies.set(ops.COOKIE, "9999999999.abcd.deadbeef")
check("a forged session is ignored", f.get("/api/ops").status_code == 401)
old = ops.make_session(KEY, now=1_000_000)
check("an expired session is rejected", not ops.session_valid(old, KEY))
check("a session signed for another key is rejected", not ops.session_valid(ops.make_session("x" * 64), KEY))

print("Lockout")
lk = client("198.51.100.4")
for _ in range(ops.LOGIN_MAX_FAILS):
    login(lk, key="wrong")
r = login(lk)
check(f"after {ops.LOGIN_MAX_FAILS} wrong keys even the right one waits",
      r.status_code == 303 and "fel=last" in r.headers["location"])
check("…and only that client", login(client("198.51.100.5")).headers["location"] == "/ops")

print("No key configured")
saved = m.ADMIN_API_KEY
m.ADMIN_API_KEY = ""
try:
    n = client("198.51.100.6")
    r = login(n, key="")
    check("login is refused when no key is configured", "fel=ingen-nyckel" in r.headers["location"])
    check("…and the form says so", "ingen admin-nyckel" in n.get("/ops").text)
    check("/api/ops stays closed", n.get("/api/ops").status_code == 403)
finally:
    m.ADMIN_API_KEY = saved

print("The login log")
lg = c.get("/ops/inloggningar").text
check("failed and successful logins are listed", "fel nyckel" in lg and "inloggad" in lg)
check("…the side panel counts the failures", 'class="count warning"' in c.get("/ops").text)
conn = connect(DB)
try:
    dump = "\n".join(str(tuple(row)) for row in conn.execute("SELECT * FROM ops_logins"))
finally:
    conn.close()
check("no address is stored in the login log", "198.51.100." not in dump, dump[:200])
check("a lockout is logged as one", "spärrad" in dump)
lo = TestClient(APP, base_url=BASE, headers={"cf-connecting-ip": "198.51.100.9", "cf-ipcountry": "se"})
login(lo)
lo.post("/ops/logout", follow_redirects=False)
check("logout is logged too, with Cloudflare's country", "utloggad" in c.get("/ops/inloggningar").text and ">SE<" in c.get("/ops/inloggningar").text)

print("Counting")
conn = connect(DB)
try:
    ops.STATS.flush(conn)
    n_req = conn.execute("SELECT COALESCE(SUM(n), 0) FROM request_hourly").fetchone()[0]
    views = conn.execute("SELECT COUNT(*) FROM usage_log WHERE query LIKE '/ops%'").fetchone()[0]
finally:
    conn.close()
check("requests are counted per hour", n_req > 0, f"{n_req}")
check("/ops visits are not counted as pageviews", views == 0, f"{views}")

print()
if failures:
    print(f"FAIL — {failures} case(s)")
    sys.exit(1)
print("PASS")
