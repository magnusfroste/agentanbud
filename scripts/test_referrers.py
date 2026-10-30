#!/usr/bin/env python3
"""Where visitors come from: the linking site's domain, and nothing more.

A post on LinkedIn (30 October 2026) raised the question "did it bring
anyone?", and the log could not say. A human pageview now carries the
referring domain — "linkedin.com", never the path, which can name a profile,
a post or a search. These cases check what is kept, what is dropped, and the
figure on /analytics.

Run: python3 scripts/test_referrers.py
"""
import json
import os
import pathlib
import sys
import tempfile

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import usage_cache  # noqa: E402
from app.db import connect  # noqa: E402
from app.insights import referrer_domain, usage_summary, format_usage_markdown  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


print("Domänen, inget mer")
cases = {
    "https://www.linkedin.com/feed/update/urn:li:activity:123/": "linkedin.com",
    "https://lnkd.in/dAbC123": "linkedin.com",
    "android-app://com.linkedin.android/": "linkedin.com",
    "https://l.facebook.com/l.php?u=https%3A%2F%2Fagentanbud.se": "facebook.com",
    "https://t.co/xyz": "x.com",
    "https://www.google.se/": "google.se",
    "https://chatgpt.com/": "chatgpt.com",
    "https://WWW.Example.SE:8443/a?b=c": "example.se",
}
for ref, want in cases.items():
    got = referrer_domain(ref, "www.agentanbud.se")
    check(f"{ref[:40]} → {want}", got == want, str(got))
for ref in ("https://www.agentanbud.se/browse", "https://agentanbud.se/", "https://api.agentanbud.se/x",
            "", None, "inte en adress", "https://localhost/", "https://user@host_med_konstigt.se/"):
    check(f"ingen domän: {ref!r}", referrer_domain(ref, "www.agentanbud.se") is None)
check("egen värd i test", referrer_domain("http://testserver/x", "testserver") is None
      and referrer_domain("https://testserver/x", "testserver:443") is None)

print("\nLoggen och siffran")
DB = tempfile.mktemp(suffix=".db")
c = TestClient(m.create_app(DB), base_url="https://testserver")
ua = "Mozilla/5.0 (Macintosh) Safari/605"
li = {"user-agent": ua, "referer": "https://www.linkedin.com/feed/update/urn:li:activity:7/"}
c.get("/", headers={**li, "cf-connecting-ip": "10.0.0.1"})
c.get("/kopare", headers={**li, "cf-connecting-ip": "10.0.0.1"})
c.get("/", headers={**li, "cf-connecting-ip": "10.0.0.2"})
c.get("/", headers={"user-agent": ua, "referer": "https://www.google.se/", "cf-connecting-ip": "10.0.0.3"})
c.get("/", headers={"user-agent": ua, "cf-connecting-ip": "10.0.0.4"})
c.get("/kopare", headers={"user-agent": ua, "referer": "https://testserver/", "cf-connecting-ip": "10.0.0.4"})
c.get("/", headers={"user-agent": "Googlebot/2.1 (+http://www.google.com/bot.html)",
                    "referer": "https://www.linkedin.com/"})
conn = connect(DB)
metas = [json.loads(r[0]) for r in conn.execute(
    "SELECT meta FROM usage_log WHERE action = 'view' ORDER BY id").fetchall()]
check("sparat: bara domänen", [x.get("ref") for x in metas]
      == ["linkedin.com", "linkedin.com", "linkedin.com", "google.se", None, None], str(metas))
check("adressen till inlägget finns ingenstans", not conn.execute(
    "SELECT COUNT(*) FROM usage_log WHERE meta LIKE '%activity%' OR query LIKE '%activity%'").fetchone()[0])
s = usage_summary(conn, days=1)
check("per besökare och dag", s["referrers"][:2] == [
    {"domain": "linkedin.com", "visitors": 2, "views": 3},
    {"domain": "google.se", "visitors": 1, "views": 1}], str(s["referrers"]))
check("med i rapporten till agenter", "linkedin.com: 2 besökare, 3 sidvisningar" in format_usage_markdown(s))
usage_cache._CACHES.pop(DB, None)
h = c.get("/analytics").text
check("analyssidan visar varifrån", "Varifrån kommer besökarna?" in h and "linkedin.com" in h
      and "2 besökare · 3 sidor" in h)
check("och säger vad som sparas", "aldrig resten av adressen" in h)

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
