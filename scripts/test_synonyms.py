#!/usr/bin/env python3
"""Synonyms learned at night: a model suggests what the notices call a search
that found nothing, app/learned.py checks every suggestion against the data,
and only active ones are searched with. The model is a mock: no network, and
no key leaves the test.

Run: python3 scripts/test_synonyms.py
"""
import asyncio
import json
import os
import pathlib
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ.setdefault("ADMIN_API_KEY", "k" * 64)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
from app import learned  # noqa: E402
from app.db import connect, upsert_tender  # noqa: E402
from app.query_terms import swedish_for  # noqa: E402
from scraper import synonyms  # noqa: E402

failures = 0
KEY = os.environ["ADMIN_API_KEY"]
LLM_KEY = "test-llm-key-not-real"


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
mcp_server.DB_PATH = DB
conn = connect(DB)
soon = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
ROWS = [("1", "Lastpallar till förråd", ["44423330"])]
ROWS += [(str(10 + i), f"Specialiserad palliativ vård {i}", ["85100000"]) for i in range(4)]
for sid, title, cpv in ROWS:
    upsert_tender(conn, {"source_system": "mercell", "source_id": sid, "tender_url": "https://example.invalid",
                         "title": title, "authority": "Region Halland", "cpv_codes": cpv, "deadline": soon,
                         "published_at": "2026-10-01", "description": "", "value": None, "procedure": None,
                         "contract_type": None, "document_type": None, "region": None, "raw_json": "{}"})
conn.commit()
conn.close()

sent = []
answers = {
    "godspallar": {"cpv_division": "44", "alternatives": ["lastpall", "pall", "palliativ", "godspallar"]},
    "nödkraft": {"cpv_division": "31", "alternatives": ["dieselaggregat"]},
}


def handler(request):
    body = json.loads(request.content)
    sent.append({"auth": request.headers.get("authorization"), "url": str(request.url), "body": body})
    term = re.search(r"Sökord: (.+)", body["messages"][0]["content"]).group(1).strip()
    if term == "trasig":
        return httpx.Response(500, json={"error": "boom"})
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answers.get(term, {}))}}]})


client = httpx.Client(transport=httpx.MockTransport(handler))
synonyms._unmet = lambda conn, limit: ["godspallar", "nödkraft", "trasig"]


def last_sync():
    c = connect(DB)
    try:
        return c.execute("SELECT status, count, message FROM sync_log WHERE source = 'synonyms' "
                         "ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        c.close()


print("Off without configuration")
for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_SYNONYMS_MODE"):
    os.environ.pop(k, None)
check("nothing is asked", synonyms.run(DB, client=client) == 0 and not sent)
check("…and the sync log says why", "inte satta" in last_sync()["message"], str(dict(last_sync())))

print("\nSuggest mode")
os.environ.update({"LLM_BASE_URL": "https://llm.example.invalid/v1/", "LLM_API_KEY": LLM_KEY,
                   "LLM_MODEL": "test-model", "LLM_SYNONYMS_MODE": "suggest"})
n = synonyms.run(DB, client=client)
check("one suggestion passes", n == 1, str(n))
check("the URL gets /chat/completions after /v1", sent[0]["url"] == "https://llm.example.invalid/v1/chat/completions",
      sent[0]["url"])
check("the key goes in the Authorization header", sent[0]["auth"] == f"Bearer {LLM_KEY}")
prompt = sent[0]["body"]["messages"][0]["content"]
check("only the search word and CPV names are sent", "godspallar" in prompt and "Region Halland" not in prompt)
c = connect(DB)
rows = {(r["term"], r["alt"]): dict(r) for r in c.execute("SELECT * FROM learned_terms")}
c.close()
check("lastpall is suggested, not active", rows[("godspallar", "lastpall")]["status"] == "suggested", str(rows))
check("pall is rejected: too short", rows[("godspallar", "pall")]["status"] == "rejected"
      and "kort" in rows[("godspallar", "pall")]["reason"])
check("palliativ is rejected: wrong CPV division", rows[("godspallar", "palliativ")]["status"] == "rejected"
      and "fel bransch" in rows[("godspallar", "palliativ")]["reason"], rows[("godspallar", "palliativ")]["reason"])
check("the search itself is rejected", rows[("godspallar", "godspallar")]["status"] == "rejected")
check("a word not in the data waits as dormant, to be checked again", rows[("nödkraft", "dieselaggregat")]["status"] == "dormant",
      rows[("nödkraft", "dieselaggregat")]["status"])
s = last_sync()
check("a failing model call is counted, the key is not in the log",
      "1 fel" in s["message"] and LLM_KEY not in s["message"], s["message"])
learned.reload(DB)
check("a suggestion is not searched with", swedish_for("godspallar") == ())

print("\nReview")
api = TestClient(APP, base_url="https://testserver")
check("the list needs the admin key", api.get("/api/ops/synonyms").status_code in (401, 403))
listing = api.get("/api/ops/synonyms", headers={"X-Admin-Key": KEY}).json()["synonyms"]
check("…and with it shows every suggestion", len(listing) >= 5, str(len(listing)))
pub = api.get("/api/synonyms")
check("the read-only list is open, without the key", pub.status_code == 200, str(pub.status_code))
pj = pub.json()
check("…with each suggestion, its status and reason, and the counts",
      any(r["alt"] == "lastpall" and r["status"] == "suggested" and r["reason"] for r in pj["synonyms"])
      and pj["counts"]["suggested"] == 1, str(pj)[:300])
check("…but not the model name", all("model" not in r for r in pj["synonyms"]))
check("changing a status still needs the key", api.post(
    "/api/ops/synonyms", params={"term": "godspallar", "alt": "lastpall", "status": "active"}).status_code in (401, 403))
r = api.post("/api/ops/synonyms", params={"term": "godspallar", "alt": "lastpall", "status": "active"},
             headers={"X-Admin-Key": KEY})
check("activating one", r.status_code == 200, r.text)
check("…makes it a Swedish equivalent at once", swedish_for("godspallar") == ("lastpall",), str(swedish_for("godspallar")))
c = connect(DB)
text = asyncio.run(mcp_server._search_tenders_uncached(c, {"query": "godspallar"}))[0].text
c.close()
titles = re.findall(r"\*\*(.+?)\*\*", text)
check("…and search_tenders finds the pallets with it, not the palliative care",
      titles == ["Lastpallar till förråd"], str(titles))
r = api.post("/api/ops/synonyms", params={"term": "godspallar", "alt": "lastpall", "status": "rejected"},
             headers={"X-Admin-Key": KEY})
check("rejecting takes it away again", r.status_code == 200 and swedish_for("godspallar") == ())

print("\nThe buttons on /ops")
ops_c = TestClient(APP, base_url="https://testserver")
same = {"origin": "https://testserver"}
ops_c.post("/ops/login", data={"key": KEY}, headers=same, follow_redirects=False)
page = ops_c.get("/ops/synonymer")
check("/ops/synonymer lists the suggestion with its buttons",
      page.status_code == 200 and 'data-alt="lastpall"' in page.text and 'data-status="active"' in page.text
      and "/api/ops/synonyms?" in page.text, f"HTTP {page.status_code}")
check("…and the side panel counts what waits", 'href="/ops/synonymer"' in page.text and KEY not in page.text)
check("a button press from another site is refused", ops_c.post(
    "/api/ops/synonyms", params={"term": "godspallar", "alt": "lastpall", "status": "active"},
    headers={"origin": "https://evil.example"}).status_code == 403)
r = ops_c.post("/api/ops/synonyms", params={"term": "godspallar", "alt": "lastpall", "status": "active"}, headers=same)
check("a button press from /ops activates it with the session", r.status_code == 200 and
      swedish_for("godspallar") == ("lastpall",), r.text)
page = ops_c.get("/ops/synonymer").text
check("…and the page then lists it as searched with", "Inga förslag väntar" in page and "Ta bort" in page)
ops_c.post("/api/ops/synonyms", params={"term": "godspallar", "alt": "lastpall", "status": "rejected"}, headers=same)

print("\nOver MCP")
H = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


def mcp(name, args, headers=None):
    r = api.post("/mcp", headers={**H, **(headers or {})}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    body = r.json()
    return "\n".join(x["text"] for x in body["result"]["content"]) if "result" in body else str(body)


def listed(headers=None):
    r = api.post("/mcp", headers={**H, **(headers or {})}, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {t["name"] for t in r.json()["result"]["tools"]}


check("list_synonyms is open to every agent", "list_synonyms" in listed())
check("review_synonym is listed only with the key", "review_synonym" not in listed()
      and "review_synonym" in listed({"x-admin-key": KEY}))
api.post("/api/ops/synonyms", params={"term": "godspallar", "alt": "lastpall", "status": "suggested"},
         headers={"X-Admin-Key": KEY})
text = mcp("list_synonyms", {})
check("list_synonyms shows what waits, with hits and the check's verdict, and how to review",
      "`godspallar` → `lastpall`" in text and "öppna träffar" in text and "review_synonym" in text, text[:400])
refused = mcp("review_synonym", {"term": "godspallar", "alt": "lastpall", "decision": "active"})
check("review_synonym without the key changes nothing", swedish_for("godspallar") == () and "✅" not in refused,
      refused[:200])
done = mcp("review_synonym", {"term": "godspallar", "alt": "lastpall", "decision": "active"}, {"x-admin-key": KEY})
check("review_synonym with the key activates it", "✅" in done and swedish_for("godspallar") == ("lastpall",), done)
check("…and list_synonyms then shows it as active", "`godspallar` → `lastpall`" in mcp("list_synonyms", {"status": "active"}))
mcp("review_synonym", {"term": "godspallar", "alt": "lastpall", "decision": "rejected"}, {"x-admin-key": KEY})
check("…and rejecting it takes it away again", swedish_for("godspallar") == ())
check("an unknown pair is said to be unknown", "Hittade inget" in mcp(
    "review_synonym", {"term": "x", "alt": "y", "decision": "active"}, {"x-admin-key": KEY}))

print("\nCounted the way search uses it")
c = connect(DB)
ok, hits, why = learned.check(c, "godspall", "lastpallsystem", "44")
check("a word found only by shortening it is not in the data", not ok and why == "finns inte i datan", f"{ok} {hits} {why}")
ok, hits, why = learned.check(c, "godspall", "lastpall", "44")
check("…while a word that starts a word in a title is", ok and hits == 1, f"{ok} {hits} {why}")
before = asyncio.run(mcp_server._search_tenders(c, {"query": "godspallar"}))[0].text
c.close()
api.post("/api/ops/synonyms", params={"term": "godspallar", "alt": "lastpall", "status": "active"},
         headers={"X-Admin-Key": KEY})
c = connect(DB)
after = asyncio.run(mcp_server._search_tenders(c, {"query": "godspallar"}))[0].text
c.close()
check("an approved synonym reaches a search already answered from the cache",
      "Lastpallar till förråd" not in before and "Lastpallar till förråd" in after, after[:200])
api.post("/api/ops/synonyms", params={"term": "godspallar", "alt": "lastpall", "status": "rejected"},
         headers={"X-Admin-Key": KEY})

print("\nDormant words wake when a notice uses them")
c = connect(DB)
c.execute("INSERT INTO learned_terms (term, alt, status, open_hits, reason, model) VALUES "
          "('laddskåp', 'laddstation', 'rejected', 0, 'inga öppna upphandlingar; CPV 31', 'm')")
c.execute("INSERT INTO learned_terms (term, alt, status, open_hits, reason, model, reviewed) VALUES "
          "('kickoff', 'seminarier', 'rejected', 0, 'inga öppna upphandlingar; CPV 79', 'm', 1)")
c.commit()
check("nothing wakes while no open notice uses the word", learned.wake(c, "active") == 0)
st = dict(c.execute("SELECT alt, status FROM learned_terms WHERE term IN ('laddskåp', 'kickoff')").fetchall())
check("an old 'nothing open' rejection becomes dormant", st["laddstation"] == "dormant", str(st))
check("…but one rejected by a reviewer stays rejected", st["seminarier"] == "rejected", str(st))
c.close()
for sid, title, cpv in [("90", "Laddstation för elbilar vid kommunhuset", ["31158100"]),
                        ("91", "Seminarier om ledarskap", ["79952000"])]:
    c = connect(DB)
    upsert_tender(c, {"source_system": "mercell", "source_id": sid, "tender_url": "https://example.invalid",
                      "title": title, "authority": "Region Halland", "cpv_codes": cpv, "deadline": soon,
                      "published_at": "2026-10-01", "description": "", "value": None, "procedure": None,
                      "contract_type": None, "document_type": None, "region": None, "raw_json": "{}"})
    c.commit()
    c.close()
c = connect(DB)
woke = learned.wake(c, "active")
st = dict(c.execute("SELECT alt, status FROM learned_terms WHERE term IN ('laddskåp', 'kickoff')").fetchall())
c.close()
learned.reload(DB)
check("the night a notice uses it, the dormant word wakes and is searched with",
      woke == 1 and st["laddstation"] == "active" and swedish_for("laddskåp") == ("laddstation",), f"{woke} {st}")
check("…and the reviewer's rejection is still left alone", st["seminarier"] == "rejected")

print("\nWhat the checks found harmful cannot be approved")
r = api.post("/api/ops/synonyms", params={"term": "godspallar", "alt": "palliativ", "status": "active"},
             headers={"X-Admin-Key": KEY})
check("activating one in the wrong CPV division is refused with 409 and the reason",
      r.status_code == 409 and "fel bransch" in r.text and swedish_for("godspallar") == (), r.text)
text = mcp("review_synonym", {"term": "godspallar", "alt": "palliativ", "decision": "active"}, {"x-admin-key": KEY})
check("…and over MCP", "Spärrad" in text and swedish_for("godspallar") == (), text[:200])
check("…and /ops offers no button for it", "spärrad" in ops_c.get("/ops/synonymer").text)
r = api.post("/api/ops/synonyms", params={"term": "nödkraft", "alt": "dieselaggregat", "status": "active"},
             headers={"X-Admin-Key": KEY})
check("a word that only finds nothing open yet may still be approved", r.status_code == 200, r.text)
check("rejecting is never refused", api.post(
    "/api/ops/synonyms", params={"term": "nödkraft", "alt": "dieselaggregat", "status": "rejected"},
    headers={"X-Admin-Key": KEY}).status_code == 200)

print("\nAuto mode, and nothing asked twice")
sent.clear()
os.environ.pop("LLM_SYNONYMS_MODE")   # auto is the default
synonyms._unmet = lambda conn, limit: ["lastpalls"]
answers["lastpalls"] = {"cpv_division": "44", "alternatives": ["lastpall"]}
check("by default a checked suggestion is active at once", synonyms.run(DB, client=client) == 1
      and swedish_for("lastpalls") == ("lastpall",), str(swedish_for("lastpalls")))
print("\nThe real list of searches to ask about")
import importlib  # noqa: E402
importlib.reload(synonyms)
c = connect(DB)
asked = synonyms._unmet(c, 10)
c.close()
check("a search already asked about is not asked again", "godspallar" not in asked and "lastpalls" not in asked,
      str(asked))

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
