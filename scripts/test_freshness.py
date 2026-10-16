#!/usr/bin/env python3
"""Every answer says when the data changes next.

Tenders arrive once a day. On 16 October one agent asked the same 177
searches every two minutes, ~28,000 times; every answer after the first of
the day was identical. An agent can only slow down if it is told when the
data changes — so the answers say it, and the tool descriptions say it before
the first call.

Run: python3 scripts/test_freshness.py
"""
import asyncio
import os
import pathlib
import sys
import tempfile

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as m  # noqa: E402
import mcp_server  # noqa: E402
import mcp_shared  # noqa: E402
from app.db import connect, log_sync, upsert_tender  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


DB = tempfile.mktemp(suffix=".db")
APP = m.create_app(DB)
conn = connect(DB)

print("Före första synken")
f = mcp_shared.data_freshness(conn)
check("ingen synk än: updated_at saknas", f["updated_at"] is None)
check("nästa hämtning finns ändå", (f["next_update"] or "").endswith("Z"), f["next_update"])
check("raden säger 'en gång per dygn' utan datum", "en gång per dygn" in mcp_shared.freshness_note(conn))

upsert_tender(conn, {
    "source_system": "mercell", "source_id": "1", "tender_url": "https://example.invalid",
    "title": "Ventilation Testskolan", "authority": "Testkommun", "cpv_codes": ["45331000"],
    "deadline": "2099-01-01T12:00:00Z", "published_at": "2026-10-16", "description": "ventilation",
    "value": None, "procedure": None, "contract_type": None, "document_type": None,
    "region": "Stockholms län", "raw_json": "{}"})
conn.commit()
log_sync(conn, source="mercell", status="ok", count=1, message="")
conn.execute("UPDATE sync_log SET run_at = '2026-10-16 06:03:12' WHERE source = 'mercell'")
log_sync(conn, source="ted", status="error", count=0, message="boom")
log_sync(conn, source="criteria", status="ok", count=5, message="")  # not a tender source
conn.execute("UPDATE sync_log SET run_at = '2026-10-16 09:00:00' WHERE source IN ('ted', 'criteria')")
conn.commit()

print("\nNär datan senast ändrades")
f = mcp_shared.data_freshness(conn)
check("senaste lyckade synk av en upphandlingskälla", f["updated_at"] == "2026-10-16T06:03:12Z", f["updated_at"])
note = mcp_shared.freshness_note(conn)
check("raden har tiden", "senast hämtad 2026-10-16 06:03 UTC" in note, note)
check("raden säger vad agenten ska göra", "en fråga per dygn" in note)

print("\nMCP-svaren")


def text(contents):
    return "\n".join(c.text for c in contents)


r1 = asyncio.run(mcp_server._search_tenders(conn, {"query": "ventilation"}))
r2 = asyncio.run(mcp_server._search_tenders(conn, {"query": "ventilation"}))
check("search_tenders slutar med raden", r1[-1].text == note and "Ventilation Testskolan" in text(r1))
check("…även när svaret kommer ur cachen", r2[-1].text == note and len(r2) == len(r1))
check("cachen lagrar inte raden två gånger",
      text(asyncio.run(mcp_server._search_tenders(conn, {"query": "ventilation"}))).count("Datan uppdateras") == 1)
check("tomt svar har också raden",
      asyncio.run(mcp_server._search_tenders(conn, {"query": "zzzqqq"}))[-1].text == note)
check("match_profile", asyncio.run(mcp_server._match_profile(conn, {"keywords": ["ventilation"]}))[-1].text == note)
check("deadline_calendar", asyncio.run(mcp_server._deadline_calendar(conn, {"days": 30}))[-1].text == note)

print("\nVerktygsbeskrivningarna, före första anropet")
import mcp_http  # noqa: E402
for tools, where in ((mcp_http._tool_list(False), "http"),
                     ([t.model_dump() for t in mcp_server._tool_objects()], "stdio")):
    by = {t["name"]: t["description"] for t in tools}
    check(f"{where}: search_tenders nämner dygnsuppdateringen", "once a day" in by["search_tenders"])
    check(f"{where}: match_profile nämner dygnsuppdateringen", "once a day" in by["match_profile"])

print("\nREST")
c = TestClient(APP, base_url="https://testserver")
r = c.get("/api/tenders?q=ventilation")
j = r.json()
check("fälten i svaret", j.get("data_updated") == "2026-10-16T06:03:12Z" and j.get("next_update"), j)
check("och i huvudena", r.headers.get("x-data-updated") == "2026-10-16T06:03:12Z"
      and r.headers.get("x-next-update") == j["next_update"])
check("llms.txt säger det", "en fråga per" in c.get("/llms.txt").text)

if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
