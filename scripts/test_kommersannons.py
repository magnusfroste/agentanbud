#!/usr/bin/env python3
"""Cases the Kommersannons parser must get right. Offline — no network.

Each fixture is cut from a real page. The detail page comes in at least three
layouts, and the first version of the parser only knew one: it lost the buyer
on 78 of 137 notices, which also blinded the duplicate check that needs it.

Run: python3 scripts/test_kommersannons.py
"""
import pathlib
import sqlite3
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from scraper import kommersannons as k  # noqa: E402

LIST_PAGE = """
<div class="row mt-4"> <div class="col-md-8 text-break"> <p class="mb-0 h4">
<a href="NoticeOverview.aspx?ProcurementId=73447"><span class="text-uppercase">RAB-2026-00363</span></a>
 - Upphandling av Byggentreprenad &amp; process <small class="text-muted">Meddelande om upphandling</small>
</p> <small> Sista anbudsdag är 2026-11-16, annonsen visas mellan 2026-10-06 och 2026-11-16. Utförandeort Västra Götalands län. </small></div></div>
<div class="row mt-4"> <div class="col-md-8 text-break"> <p class="mb-0 h4">
<a href="NoticeOverview.aspx?ProcurementId=65758"><span class="text-uppercase">TN 2025/863</span></a>
 - Rörinspektion <small class="text-muted">Efterannons</small>
</p> <small> Sista anbudsdagen är 2025-05-30, annonsen visas mellan 2026-10-06 och 2026-11-05. Utförandeort Skåne län. </small></div></div>
<div class="col-2 fw-bold text-center"> 3 / 20 </div>
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="abc&amp;123" />
"""

# Call for tender, long label: "Ansvarig upphandlande organisation för den här upphandlingen är"
DETAIL_CALL = """
<dt>Upphandlingen genomförs med förfarande</dt> <dd>Annat förfarande i ett steg</dd>
<dt>Ansvarig upphandlande organisation för den här upphandlingen är</dt>
<dd><a id="ctl00_x_hlProcuringEntity" href="../Info/ProcuringEntity.aspx?ProcuringEntityId=268">Renova Aktiebolag</a></dd>
<p>Publik annons: Denna upphandling är annonserad publikt. </p> <p> Byggdels entreprenad gällande Ny process i Marieholm. </p>
<script>setDeadline('2026-11-16T23:59:50', '2026-10-06T20:32:51');</script>
<dt>CPV</dt> <dd><span>45000000-7</span> Anläggningsarbete</dd>
<dt>CPV</dt> <dd><span>45210000-2</span> Byggnadsanläggning</dd>
"""

# Dynamic purchasing system, short label: "Ansvarig upphandlande organisation"
DETAIL_DIS = """
<dt>Upphandlingen genomförs med förfarande</dt> <dd>Dynamiskt inköpssystem</dd>
<dt>Ansvarig upphandlande organisation</dt>
<dd><a id="ctl00_y_hlProcuringEntity" href="../Info/ProcuringEntity.aspx?ProcuringEntityId=91">MKB Fastighets AB</a></dd>
<script>setDeadline('2033-02-24T23:59:50', '2026-10-06T20:32:51');</script>
"""

# RFI: "Ansvarig organisation", no procedure, no deadline script
DETAIL_RFI = """
<dt>Typ av förfrågan</dt> <dd>Informationsförfrågan</dd>
<dt class="col-md-6"> Ansvarig organisation </dt> <dd class="col-md-6">
<a id="ctl00_z_hlProcuringEntity" href="../Info/ProcuringEntity.aspx?ProcuringEntityId=445">Danderyds kommun</a> </dd>
"""

failures = []


def check(name, got, want):
    ok = got == want
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f" — got {got!r}, want {want!r}"))
    if not ok:
        failures.append(name)


print("Listsidan")
rows = k.parse_list(LIST_PAGE)
check("två rader", len(rows), 2)
check("titel, entiteter avkodade", rows[0]["title"], "Upphandling av Byggentreprenad & process")
check("annonstyp", rows[0]["kind"], "Meddelande om upphandling")
check("deadline-datum", rows[0]["deadline_date"], "2026-11-16")
check("'Sista anbudsdagen' (efterannons) läses också", rows[1]["deadline_date"], "2025-05-30")
check("publicerad = visas från", rows[0]["published"], "2026-10-06")
check("region utan avslutande punkt", rows[0]["region"], "Västra Götalands län")
check("sidposition", k.page_position(LIST_PAGE), (3, 20))
check("viewstate avkodad för postback", k.hidden_fields(LIST_PAGE)["__VIEWSTATE"], "abc&123")
check("efterannonser behålls inte", [r["kind"] in k.KEEP_KINDS for r in rows], [True, False])

print("\nDetaljsidan — tre layouter")
d = k.parse_detail(DETAIL_CALL)
check("köpare (lång etikett)", d.get("authority"), "Renova Aktiebolag")
check("förfarande", d.get("procedure"), "Annat förfarande i ett steg")
check("beskrivning", d.get("description"), "Byggdels entreprenad gällande Ny process i Marieholm.")
check("CPV utan kontrollsiffra", d.get("cpv_codes"), ["45000000", "45210000"])
check("exakt deadline, vintertid → UTC", d.get("deadline"), "2026-11-16T22:59:50Z")
check("köpare (kort etikett, DIS)", k.parse_detail(DETAIL_DIS).get("authority"), "MKB Fastighets AB")
rfi = k.parse_detail(DETAIL_RFI)
check("köpare (RFI: 'Ansvarig organisation')", rfi.get("authority"), "Danderyds kommun")
check("RFI utan deadline-skript", rfi.get("deadline"), None)

print("\nSvensk tid → UTC")
for local, want in [
    ("2026-07-01T12:00:00", "2026-07-01T10:00:00Z"),   # sommartid
    ("2026-01-15T12:00:00", "2026-01-15T11:00:00Z"),   # vintertid
    ("2026-03-29T01:59:00", "2026-03-29T00:59:00Z"),   # sista minuten vintertid
    ("2026-03-29T03:00:00", "2026-03-29T01:00:00Z"),   # första minuten sommartid
    ("2026-10-25T03:00:00", "2026-10-25T02:00:00Z"),   # tillbaka till vintertid
]:
    check(local, k._iso_utc(datetime.fromisoformat(local)), want)

print("\nRad till databasen")
rec = k.map_record(rows[0], {})
check("utan detaljsida: deadline = dagens slut, svensk tid", rec["deadline"], "2026-11-16T22:59:00Z")
check("källa", rec["source_system"], "kommersannons")
check("länk till detaljsidan", rec["tender_url"].endswith("ProcurementId=73447"), True)

print("\nDubbletter mot andra källor")
db = tempfile.mktemp(suffix=".db")
conn = sqlite3.connect(db)
conn.execute("CREATE TABLE tenders (source_system TEXT, title TEXT, authority TEXT)")
conn.execute("INSERT INTO tenders VALUES ('mercell', 'Manuella rullstolar', 'KRISTIANSTADS KOMMUN, Kristianstad')")
conn.execute("INSERT INTO tenders VALUES ('kommersannons', 'Egen annons', 'Ale kommun')")
known = k.known_elsewhere(conn)
check("samma annons, annan stavning av köparen",
      (k._norm("Manuella rullstolar"), k._norm("Kristianstads kommun")[:10]) in known, True)
check("samma titel, annan köpare räknas inte som dubblett",
      (k._norm("Manuella rullstolar"), k._norm("Ale kommun")[:10]) in known, False)
check("egna rader räknas inte", (k._norm("Egen annons"), k._norm("Ale kommun")[:10]) in known, False)

if failures:
    print(f"\n{len(failures)} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
