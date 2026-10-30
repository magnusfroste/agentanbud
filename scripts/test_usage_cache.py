#!/usr/bin/env python3
"""Usage figures: one scan at a time, never a reader waiting on a stale one.

A scan of usage_log takes tens of seconds at a month of data. The old cache
let every request that arrived after expiry start its own scan; they piled up
on the worker threads until the server stopped answering (24 October, nine
hours). These cases put twenty readers on a slow computation at once and
check that it runs once, that a stale answer comes back at once while a
single background refresh runs, and that ?days= cannot fan out.

Run: python3 scripts/test_usage_cache.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile
import threading
import time

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app import usage_cache  # noqa: E402
from app.usage_cache import UsageCache, period  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


print("Perioder")
check("1, 7, 30 och allt", [period(d) for d in (1, 7, 30, None)] == [1, 7, 30, None])
check("andra värden avrundas uppåt", [period(d) for d in (0, 2, 6, 8, 29)] == [1, 7, 7, 30, 30])
check("över 30 är allt (loggen sparar 30 dygn)", period(31) is None and period(365) is None)
check("skräp är allt", period("x") is None)

print("\nTjugo samtidiga läsare, en beräkning")
running, peak, calls = [0], [0], []
lock = threading.Lock()


def slow(days):
    with lock:
        running[0] += 1
        peak[0] = max(peak[0], running[0])
        calls.append(days)
    time.sleep(0.4)
    with lock:
        running[0] -= 1
    return {"days": days, "n": len(calls)}


cache = UsageCache(slow)
results = []
threads = [threading.Thread(target=lambda: results.append(cache.get(7))) for _ in range(20)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("beräknad en gång", calls == [7], str(calls))
check("alla fick samma svar", len(results) == 20 and all(r is results[0] for r in results))

calls.clear()
threads = [threading.Thread(target=cache.get, args=(d,)) for d in (1, 30, None, 1, 30, None)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("olika perioder: en beräkning åt gången", peak[0] == 1 and sorted(calls, key=str) == sorted([1, 30, None], key=str),
      f"peak={peak[0]} calls={calls}")

print("\nGammalt svar direkt, en uppdatering i bakgrunden")
now = [1000.0]
calls.clear()
c2 = UsageCache(slow, fresh_s=600, clock=lambda: now[0])
first = c2.get(7)
now[0] += 601
t0 = time.perf_counter()
stale = [c2.get(7) for _ in range(10)]
waited = time.perf_counter() - t0
check("läsaren väntar inte på beräkningen", waited < 0.1 and all(s is first for s in stale), f"{waited:.3f}s")
for _ in range(50):
    if len(calls) == 2 and not c2._refreshing:
        break
    time.sleep(0.05)
check("en enda uppdatering startades", calls == [7, 7], str(calls))
check("nästa läsare får det nya svaret", c2.get(7) is not first)


def boom(days):
    raise RuntimeError("database is locked")


c3 = UsageCache(lambda d: {"v": 1}, fresh_s=1, clock=lambda: now[0])
old = c3.get(1)
c3._compute = boom
now[0] += 5
c3.get(1)
for _ in range(50):
    if not c3._refreshing:
        break
    time.sleep(0.02)
check("en misslyckad uppdatering behåller det gamla svaret", not c3._refreshing and c3._items[1][1] is old)
check("…och försöker igen nästa gång", c3.get(1) is old and 1 in c3._refreshing)

calls.clear()
c4 = UsageCache(slow)
c4.warm(periods=(7, None), delay_s=0).join()
check("förvärmning räknar perioderna i förväg", calls == [7, None] and c4.get(7) and calls == [7, None], str(calls))

calls.clear()
c5 = UsageCache(lambda d: (calls.append(d), {"n": len(calls)})[1])
c5.warm(periods=(1, 30), delay_s=0, every_s=0.2)
import time as _t  # noqa: E402
for _ in range(100):
    if len(calls) >= 6:
        break
    _t.sleep(0.05)
check("hålls färska utan läsare: alla perioder räknas om med jämna mellanrum",
      calls.count(1) >= 3 and calls.count(30) >= 3, str(calls))
check("…och läsaren får det senaste", c5.get(30)["n"] >= 4, str(c5.get(30)))

print("\nSajten och MCP delar cachen")
from fastapi.testclient import TestClient  # noqa: E402
import app.main as m  # noqa: E402
import mcp_server  # noqa: E402

DB = tempfile.mktemp(suffix=".db")
c = TestClient(m.create_app(DB), base_url="https://testserver")
mcp_server.DB_PATH = DB
shared = usage_cache.for_db(DB)
before = shared.computations
j = c.get("/api/analytics?days=3").json()
check("?days=3 räknas som en vecka", j["period_days"] == 7, str(j["period_days"]))
c.get("/api/analytics?days=5")
c.get("/api/analytics?days=7")
check("tre olika ?days, en beräkning", shared.computations == before + 1, f"{shared.computations - before}")
from app.db import connect  # noqa: E402
conn = connect(DB)
asyncio.run(mcp_server._get_usage_stats(conn, {"days": 6}))
check("MCP-verktyget använder samma svar", shared.computations == before + 1)
check("analyssidan renderas", c.get("/analytics").status_code == 200)

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
