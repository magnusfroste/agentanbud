#!/usr/bin/env python3
"""REST /api/tenders: the same search, again, before the data can change.

On 26 November four programs searched fixed lists of 100-190 words every 5
to 90 minutes (5,448 calls from one of them), though the data changes once a
day. MCP already answers such a repeat with a short 'unchanged' and the feed
(app/conduct.py). REST now does the same in HTTP: past SHORT_ANSWER_AFTER
identical calls from one visitor since the last sync, a 429 with Retry-After
until the next sync and a body that says why and gives the feed. Never an
empty result list, which a script could read as 'every tender is gone'.

Run: python3 scripts/test_rest_unchanged.py
"""
import os
import pathlib
import sys
import tempfile

os.environ["ADMIN_API_KEY"] = "k" * 64
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app.conduct import SHORT_ANSWER_AFTER  # noqa: E402
from app.db import connect, log_sync, upsert_tender  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


DB = tempfile.mktemp(suffix=".db")
c = TestClient(m.create_app(DB), base_url="https://testserver")
conn = connect(DB)
upsert_tender(conn, {"source_system": "mercell", "source_id": "1", "tender_url": "https://example.invalid",
                     "title": "Tryck av affischer", "authority": "Region Halland", "cpv_codes": ["79810000"],
                     "deadline": "2099-01-01", "published_at": "2026-11-01", "description": "", "value": None,
                     "procedure": None, "contract_type": None, "document_type": None, "region": None,
                     "raw_json": "{}"})
log_sync(conn, "mercell", "ok", 1, "test")
conn.commit()
conn.close()

H = {"user-agent": "node", "cf-connecting-ip": "203.0.113.7"}


def get(q="affischer", headers=H, **params):
    return c.get("/api/tenders", params={"q": q, **params}, headers=headers)


print("The same search, again")
codes = [get().status_code for _ in range(SHORT_ANSWER_AFTER)]
check(f"the first {SHORT_ANSWER_AFTER} are answered in full", codes == [200] * SHORT_ANSWER_AFTER, str(codes))
r = get()
check("the next one is a 429", r.status_code == 429, str(r.status_code))
body = r.json()
check("…saying it is unchanged and giving the feed for that search",
      body.get("unchanged") is True and "feed.xml?q=affischer" in body.get("feed", "")
      and "Oförändrat" in body.get("detail", ""), str(body)[:300])
check("…with Retry-After until after the next sync", int(r.headers.get("retry-after", "0")) >= 60,
      r.headers.get("retry-after"))
check("…and the data headers", "x-next-update" in r.headers)
check("…and never an empty result list", "items" not in body)

print("\nEverything else is answered in full")
check("another search", get("tryck").status_code == 200)
check("the same search with another page size", get(page_size=5).status_code == 200)
check("the same search from another caller",
      get(headers={"user-agent": "node", "cf-connecting-ip": "203.0.113.8"}).status_code == 200)
check("a caller without a visitor id (no address)", get(headers={"user-agent": "node"}).status_code == 200)
check("the plain list with no search", c.get("/api/tenders", headers=H).status_code == 200)
smoke = {"user-agent": "agentanbud-smoke/1.0", "cf-connecting-ip": "203.0.113.9"}
check("our own deploy smoke test, however often it runs",
      all(get(headers=smoke).status_code == 200 for _ in range(SHORT_ANSWER_AFTER + 3)))

conn = connect(DB)
log_sync(conn, "ted", "ok", 5, "next sync")
conn.execute("UPDATE sync_log SET run_at = datetime('now', '+1 minute') WHERE source = 'ted'")
conn.commit()
conn.close()
r = get()
check("the same search after the next sync", r.status_code == 200 and r.json()["total"] == 1, str(r.status_code))

conn = connect(DB)
logged = conn.execute("SELECT COUNT(*) FROM usage_log WHERE channel = 'api' "
                      "AND json_extract(meta, '$.short') = 1").fetchone()[0]
conn.close()
check("a short answer is still logged, marked short", logged == 1, str(logged))

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
