#!/usr/bin/env python3
"""The same caller asking the identical search_tenders past SHORT_ANSWER_AFTER
since the data last changed gets a short 'unchanged' reply with the feed URL,
not the list again. Another search, another caller, or a new sync is answered
in full. Offline.

Run: python3 scripts/test_short_answer.py
"""
import os
import pathlib
import sys
import tempfile

os.environ.setdefault("ADMIN_API_KEY", "k" * 64)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
from app import conduct  # noqa: E402
from app.db import connect, log_sync  # noqa: E402

DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
mcp_server.DB_PATH = DB
H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
N = conduct.SHORT_ANSWER_AFTER
failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


def client(ip):
    return TestClient(APP, base_url="https://testserver", headers={"cf-connecting-ip": ip})


def ask(c, args, i=1):
    r = c.post("/mcp", headers=H, json={"jsonrpc": "2.0", "id": i, "method": "tools/call",
                                        "params": {"name": "search_tenders", "arguments": args}})
    return "\n".join(x["text"] for x in r.json()["result"]["content"])


def short(text):
    return text.startswith("Oförändrat.")


a = client("198.51.100.41")
args = {"query": "ventilation", "cpv": "45"}
answers = [ask(a, args, i) for i in range(N + 2)]
check(f"the first {N} identical searches are answered in full",
      not any(short(t) for t in answers[:N]), answers[N - 1][:80])
check(f"search {N + 1} gets the short answer", short(answers[N]), answers[N][:120])
check("…which names the count and the feed for that exact search",
      f"{N + 1} gånger" in answers[N] and "feed.xml?q=ventilation&cpv=45" in answers[N], answers[N])
check("…and carries no result list", "Hittade" not in answers[N] and "Inga upphandlingar" not in answers[N])
check("…and stays short on the next ask", short(answers[N + 1]) and f"{N + 2} gånger" in answers[N + 1])

check("another search from the same caller is answered in full",
      not short(ask(a, {"query": "ventilation", "cpv": "45", "limit": 3})))
check("the same search from another caller is answered in full",
      not short(ask(client("198.51.100.42"), args)))

conn = connect(DB)
try:
    log_sync(conn, "ted", "ok", 1, "test sync")
finally:
    conn.close()
check("after a sync the same search is answered in full again", not short(ask(a, args)))

check("a caller without a visitor id is never shortened",
      conduct.short_answer(None, "search_tenders|{}", "u", None) is None)

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
