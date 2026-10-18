#!/usr/bin/env python3
"""Ops sees a traffic storm, says what it was made of, and who is calling.

On 12 October MCP requests ran at ~31,000 an hour for four hours — a client
reconnecting in a loop — and the ops overview said "Allt ser bra ut". The
traffic page filed it all under "MCP", and the agents page showed a search
loop with a different session id on every row. These cases are that day.

Run: python3 scripts/test_ops_traffic.py
"""
import json
import os
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta, timezone

KEY = "k" * 64
os.environ["ADMIN_API_KEY"] = KEY
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
from app import ops  # noqa: E402
from app.db import connect  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


def hour(h_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h_ago)).strftime("%Y-%m-%dT%H")


def ts(h_ago: int, minute: int = 0) -> str:
    t = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=h_ago)
    return (t + timedelta(minutes=minute)).strftime("%Y-%m-%d %H:%M:%S")


print("Vad en MCP-förfrågan är")
check("GET /mcp med Accept: text/event-stream är en ström",
      ops.request_kind("/mcp", "GET", "text/event-stream") == "mcp_stream")
check("…även i en blandad Accept",
      ops.request_kind("/mcp", "GET", "application/json, text/event-stream") == "mcp_stream")
check("POST med samma Accept är vanlig MCP",
      ops.request_kind("/mcp", "POST", "application/json, text/event-stream") == "mcp")
check("vanlig GET /mcp (en person) är vanlig MCP", ops.request_kind("/mcp", "GET", "text/html") == "mcp")
check("utan metod: som förut", ops.request_kind("/mcp") == "mcp" and ops.request_kind("/api/x") == "api")

DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
conn = connect(DB)

print("\nRäknarna")
stats = ops.RequestStats()
for _ in range(10):
    stats.record("/mcp", 405, 1.0, "GET", "text/event-stream")
for _ in range(4):
    stats.record("/mcp", 200, 5.0, "POST", "application/json")
stats.flush(conn)
row = [r for r in ops.requests_by_hour(conn, 2) if r["hour"] == hour(0)][0]
check("ström räknas både för sig och som MCP", row["mcp_stream"] == 10 and row["mcp"] == 14,
      f"mcp={row['mcp']} stream={row['mcp_stream']}")
check("totalen räknar varje förfrågan en gång", row["total"] == 14)

print("\nVad MCP-förfrågningarna var, per timme")
conn.execute("DELETE FROM request_hourly")
# The storm hour, counted before streams had a kind: 30,000 "mcp", 30 tool calls.
conn.execute("INSERT INTO request_hourly (hour, kind, status, n, ms_total) VALUES (?, 'mcp', 2, 30000, 90000)",
             (hour(5),))
conn.executemany("INSERT INTO usage_log (channel, action, query, meta, created_at) VALUES ('mcp', ?, ?, ?, ?)",
                 [("tool:search_tenders", "bygg", "{}", ts(5, i)) for i in range(30)]
                 + [("mcp:connect", None, "{}", ts(5, i)) for i in range(5)])
conn.commit()
rows = ops.with_mcp_breakdown(conn, ops.requests_by_hour(conn, 48))
storm = [r for r in rows if r["hour"] == hour(5)][0]
check("verktygsanrop och handskakningar hämtas ur loggen",
      storm["mcp_tools"] == 30 and storm["mcp_handshakes"] == 5)
check("resten blir 'övrigt' — det tomma i stormen syns", storm["mcp_other"] == 29965, f"{storm['mcp_other']}")

print("\nToppar")
conn.execute("DELETE FROM request_hourly")
normal = [(hour(h), "page", 2, 1000, 5000) for h in range(1, 48)]
spike = [(hour(h), "mcp", 2, 31000, 93000) for h in (9, 10)]
conn.executemany("INSERT INTO request_hourly (hour, kind, status, n, ms_total) VALUES (?, ?, ?, ?, ?)",
                 [r for r in normal if r[0] not in {hour(9), hour(10)}] + spike)
conn.commit()
slots = ops.hour_slots(ops.with_mcp_breakdown(conn, ops.requests_by_hour(conn, 48)), 48)
spikes = ops.traffic_spikes(slots)
check("två stormtimmar flaggas", len(spikes) == 2, f"{[s['hour'] for s in spikes]}")
check("med faktor mot medianen", spikes and spikes[0]["factor"] == 31, f"{spikes and spikes[0]['factor']}")
w = ops.spike_warning(spikes) or ""
check("varningen säger vad det var", "nästan bara MCP utan verktygsanrop" in w and "2 timmar" in w, w)

quiet = [{"hour": hour(h), "total": t} for h, t in ((3, 100), (2, 100), (1, 100), (0, 600))]
check("för lite historik: ingen varning", ops.traffic_spikes(quiet) == [])
low = [{"hour": hour(h), "total": 100} for h in range(47, 0, -1)] + [{"hour": hour(0), "total": 900}]
check("9× en lugn natt men under golvet: ingen varning", ops.traffic_spikes(low) == [])
api = ops.spike_warning([{"hour": hour(1), "total": 9000, "api": 8800, "mcp": 0, "factor": 9}]) or ""
check("en API-topp beskrivs som API", "mest API" in api, api)
check("ingen topp, ingen varning", ops.spike_warning([]) is None)

print("\nVarningen når översikten")
c = TestClient(APP, base_url="https://testserver")
snap = c.get("/api/ops", headers={"x-admin-key": KEY}).json()
check("/api/ops varnar för stormen", any("Ovanligt mycket trafik" in x for x in snap["warnings"]), snap["warnings"])
check("trafiksidan listar topparna", len(snap["trafik"]["spikes"]) == 2)

print("\nVem som anropar")
conn.execute("DELETE FROM usage_log")
loop_terms = ["digital transformation", "tjänstedesign", "varumärkesstrategi", "kundupplevelse",
              "digital design", "strategisk kommunikation", "webb intranät", "systeminnovation"]
rows = []
for burst in range(6):                     # every 5 minutes, eight questions
    for i, q in enumerate(loop_terms):
        rows.append(("mcp", "tool:search_tenders", q,
                     json.dumps({"v": "loopvisitor1", "_sid": f"s{burst}{i}"}), ts(2, burst * 5)))
rows += [("api", "search", "klimatdata", json.dumps({"v": "human000001", "bot": 0}), ts(1, m_)) for m_ in (1, 9, 30)]
rows.append(("mcp", "mcp:connect", None, json.dumps({"v": "loopvisitor1", "client": "ziwei-ore"}), ts(2)))
conn.executemany("INSERT INTO usage_log (channel, action, query, meta, created_at) VALUES (?, ?, ?, ?, ?)", rows)
conn.commit()
tc = ops.top_callers(conn, 24)
first = tc["callers"][0]
check("loopen blir en rad, överst", first["v"] == "loopvisitor1" and first["n"] == 48, f"{first['v']} {first['n']}")
check("8 unika frågor av 48", first["distinct"] == 8)
check("rytmen hittas: var 5:e minut", first["rhythm_min"] == 5, f"{first['rhythm_min']}")
check("klientnamnet från handskakningen", first["client"] == "ziwei-ore")
check("andel av all aktivitet", first["share_pct"] == 94, f"{first['share_pct']}")
check("människan syns också", any(x["v"] == "human000001" and x["n"] == 3 for x in tc["callers"]))
recent = ops.recent_mcp_calls(conn, 3)
check("senaste anropen visar besöks-id, inte bara session",
      all(r["visitor"] == "loopvisitor1" for r in recent))

print("\nSökningar utan sökord visar sina filter")
check("sökord vinner", ops.describe_call("klimatdata", {"cpv": "72"}) == "klimatdata")
check("filter i hakparentes", ops.describe_call(None, {"cpv": "45", "source": "mercell"}) == "[källa mercell, cpv 45]",
      ops.describe_call(None, {"cpv": "45", "source": "mercell"}))
check("ingenting alls", ops.describe_call(None, {"bot": 0}) == "[inga filter]")
check("MCP-argument med samma namn", ops.describe_call("", {"authority": "Trafikverket", "_sid": "x"}) == "[köpare Trafikverket]")
conn.execute("DELETE FROM usage_log")
conn.executemany("INSERT INTO usage_log (channel, action, query, meta, created_at) VALUES (?, ?, ?, ?, ?)",
                 [("api", "search", None, json.dumps({"v": "watcher00001", "cpv": "45", "source": "mercell", "bot": 0}),
                   ts(3, 5 * i)) for i in range(12)])
conn.commit()
w = ops.top_callers(conn, 24)["callers"][0]
check("bevakaren utan sökord visar vad den följer",
      w["top"] and w["top"][0]["q"] == "[källa mercell, cpv 45]" and w["distinct"] == 1, f"{w['top']}")
c.get("/api/tenders?buyer_type=municipal", headers={"cf-connecting-ip": "203.0.113.9"})
logged = conn.execute("SELECT meta FROM usage_log WHERE channel = 'api' ORDER BY id DESC LIMIT 1").fetchone()
check("API-loggen sparar köpartyp", logged and json.loads(logged[0]).get("buyer_type") == "municipal",
      logged and logged[0])

print("\nSidorna renderas")
c.post("/ops/login", data={"key": KEY}, headers={"origin": "https://testserver"}, follow_redirects=False)
t = c.get("/ops/trafik")
check("/ops/trafik", t.status_code == 200 and "Toppar senaste dygnet" in t.text and "· övrigt" in t.text)
a = c.get("/ops/agenter")
check("/ops/agenter", a.status_code == 200 and "Största anropare" in a.text and "var 5 min" in a.text)
o = c.get("/ops")
check("/ops översikt visar varningen", o.status_code == 200 and "Ovanligt mycket trafik" in o.text)

print("\nREST-anropare har ett programnamn, webbläsare inget")
from app.insights import program_name  # noqa: E402
check("MyAgents med kontakt: bara namnet", program_name("MyAgents/1.0 (info@myagents.se)") == "MyAgents/1.0")
check("python-requests", program_name("python-requests/2.31.0") == "python-requests/2.31.0")
check("webbläsare: ingenting", program_name(
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 Safari/605.1.15") is None)
check("crawler som låtsas vara webbläsare: ingenting",
      program_name("Mozilla/5.0 (compatible; GPTBot/1.0; +https://openai.com/gptbot)") is None)
check("tomt och skräp: ingenting", program_name("") is None and program_name(None) is None
      and program_name("12345 (x)") is None)
check("tecken som inte hör till ett namn tas bort och längden begränsas",
      program_name("<script>alert(1)</script>/" + "x" * 80) == "scriptalert1/script/" + "x" * 20)
conn.execute("DELETE FROM usage_log")
conn.commit()
c.get("/api/tenders?q=ventilation", headers={"cf-connecting-ip": "203.0.113.20",
                                              "user-agent": "MyAgents/1.0 (info@myagents.se)"})
c.get("/api/tenders?q=ventilation", headers={"cf-connecting-ip": "203.0.113.21",
                                              "user-agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0"})
metas = [json.loads(r[0]) for r in conn.execute(
    "SELECT meta FROM usage_log WHERE channel = 'api' ORDER BY id").fetchall()]
check("API-loggen sparar programnamnet", metas[0].get("program") == "MyAgents/1.0", metas[0])
check("…men inte webbläsarens", metas[1].get("program") is None and "Firefox" not in json.dumps(metas[1]))
named = [x for x in ops.top_callers(conn, 24)["callers"] if x["client"]]
check("tabellen visar programmet som klient", [x["client"] for x in named] == ["MyAgents/1.0"], named)

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
