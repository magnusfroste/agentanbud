# Agentanbud

> **Svenska upphandlingar — fri tillgång, en SQLite, en container.**
> Open by design. Data är dina medborgares rättighet.

Agentanbud samlar in offentlig upphandlingsdata från Mercell och TED EU,
lagrar allt i en lokal SQLite, och serverar den via webb-dashboard, öppet
JSON-API och en **MCP-server för AI-agenter** — allt från en FastAPI-app.
Ovanpå datan finns marknadsintelligens (`get_winner_history` — vem brukar
vinna), en agent-författad blogg och en cookie-fri analytics-sida. Allt-i-ett-
container, deployas till Easypanel via en `docker-compose.yml`. Inga
molntjänster, inga API-nycklar för läsning, inga paywalls.

**Varför det här finns:** Svenska myndigheter måste enligt lag publicera
upphandlingar enligt offentlighetsprincipen, men det finns ingen samlad
publik tjänst som gör datan lätt att upptäcka, jämföra och bevaka. Vi
tycker att medborgare, småföretagare och ideella organisationer förtjänar
samma tillgång som de stora konsultbolagen.

---

## 🇸🇪 Agentanbuds syfte

Svenska myndigheter måste enligt lag publicera upphandlingar enligt **offentlighetsprincipen** — men det finns ingen samlad öppen plats där datan är lätt att hitta, jämföra och bevaka. Stora konsultbolag har råd att betala plattformar, byta källa, skriva anbud. Småföretag gör det inte.

**Agentanbuds syfte:** göra det enklare för svenska företag — särskilt småföretag — att hitta och offerera offentliga upphandlingar. Öppna data, öppen kod, öppen matchning. Inte för att konkurrera med plattformarna, utan för att hjälpa marknaden fungera bättre.

Konkret: vi speglar publik upphandlingsdata, gör den sökbar, och ger AI-agenter direktåtkomst via MCP — så att småföretag har samma möjlighet att **hitta** anbud som de stora har.

> "Sharing is caring" — men vi tar det längre: vi delar **hela ekosystemet** (kod, data, utbildning) så att fler kan bygga bättre verktyg ovanpå.

**Respekt före aggresion:** vi respekterar `robots.txt`. Vi kontaktar plattformar istället för att kringgå dem. Vi speglar bara det som plattformarna själva publicerat publikt. Om en plattform säger nej, säger vi också nej (e-Avrop är vårt första exempel — se `/providers`).

## 🎯 Tre principer

1. **Fri tillgång** — koden är MIT-licensierad, datan är publik, API:t är
   fritt. Inga betalväggar, inga konton, inga kontaktuppgifter för att
   "få tillgång".
2. **Enkel att driva** — en container, en SQLite-fil, en cron-rad. Kan köras
   på en Hetzner-VPS för €4/mån eller på din laptop. Backup = `cp`.
3. **Lokal först** — vi skickar inte data till molnplattformar vi inte äger.
   All data bor i din egen SQLite, under din egen kontroll.

---

## Arkitektur

```
┌─────────────────────────────────────────────┐
│  Easypanel service: agentanbud              │
│  (single container via docker-compose)      │
│                                             │
│  ┌──────────────┐  ┌──────────────────────┐ │
│  │ cron 06:00   │  │ FastAPI (uvicorn)    │ │
│  │  ↓           │  │  /api/*  (REST)      │ │
│  │  scraper     │  │  /mcp    (agenter)   │ │
│  │   • mercell  │  │  /api/winners        │ │
│  │   • ted ×3   │  │  /   (dashboard)     │ │
│  │   • lov      │──│  reads ────→ SQLite  │ │
│  │   • kunskap  │  │                      │ │
│  └──────────────┘  └──────────────────────┘ │
│                  ↳ /data/application.db       │
└─────────────────────────────────────────────┘
          ↕ HTTPS
       Traefik (Easypanel)
```

**Inga Supabase-konton, inga externa databaser, inga buildpacks, inga
hemligheter att hantera.** Allt-i-ett, läs SQLite direkt om du vill.

---

## 🚀 Deploy till Easypanel (3 minuter)

1. Easypanel → **Create Service → Docker Compose**
2. **Source:** `https://github.com/magnusfroste/agentanbud`
3. **Domain:** t.ex. `upphandling.dindomän.se` (HTTPS auto)
4. **Deploy**

Klart. Första sync körs kl 06:00 (eller ställ in egen `CRON_SCHEDULE`).

## 🐳 Lokal test

```bash
git clone https://github.com/magnusfroste/agentanbud.git
cd agentanbud
docker compose up --build
# → http://localhost:8080
```

För att köra en synk manuellt (utan att vänta på cron):

```bash
docker compose exec app python -m scraper.orchestrator
```

## ✅ Rök-test

Kör efter varje deploy — 42 kontroller, inga beroenden utöver Python:

```bash
python3 scripts/smoke_test.py                 # mot produktion
python3 scripts/smoke_test.py --wait 180      # vänta in omstarten först
python3 scripts/smoke_test.py --base http://localhost:8080
```

Avslutar med kod 0 (allt grönt) eller 1 (något fel), så det kan gate:a en
deploy. Det kontrollerar bl.a. saker som faktiskt gått sönder i drift:

- **att rätt kodversion är ute** — ett misslyckat bygge kan lämna gamla
  imagen igång, så sajten ser frisk ut medan fixen saknas
- **att varje filter faktiskt filtrerar** — ett filter som returnerar
  *hela totalen* är lika trasigt som ett som ger noll, och inget felar
- **att detaljsidan för en avslutad upphandling renderar** (den grenen
  gav 500 i veckor — vanlig klickning når den aldrig)
- **att skrivendpoints avvisar anrop utan nyckel** — hoppas över med
  varning om instansen saknar `ADMIN_API_KEY`, eftersom sonderna då
  skulle utlösa en riktig synk
- **att MCP-verktygen svarar med innehåll** — för anslutna agenter är
  det produkten

## ⚙️ Konfiguration

Alla inställningar är miljövariabler — se [`.env.example`](.env.example) för
hela listan. De viktigaste för självhostande:

| Variabel | Standard | Vad |
|---|---|---|
| `DB_PATH` | `/data/application.db` | SQLite-sökväg (Docker-volym) |
| `CRON_SCHEDULE` | `0 6 * * *` | När daglig synk körs |
| `ADMIN_API_KEY` | *(tom)* | Skydd för skriv-/adminåtgärder — **sätt den i produktion** |
| `ALLOW_OPEN_ADMIN` | *(tom)* | Öppnar skrivning utan nyckel — **endast lokal dev** |
| `SCRAPE_MERCELL`, `SCRAPE_TED`, … | `true` | Slå av/på enskilda källor |
| `USER_AGENT` | `agentanbud/0.1 …` | Skickas till Mercell/TED (artig, med kontakt) |

> ⚠️ **Läsning är alltid öppen** (REST GET + `/mcp`). Skrivning (`/api/sync`,
> blogg m.m.) kräver `X-Admin-Key`. Saknas `ADMIN_API_KEY` **stängs
> skriv-endpoints helt** (403) i stället för att stå öppna — så en glömd
> eller felstavad nyckel i produktion märks direkt i stället för att tyst
> lämna dem obevakade. Vill du ha dem öppna lokalt utan nyckel: sätt
> `ALLOW_OPEN_ADMIN=true`.

---

## 📊 API

Alla **läs**-endpoints är öppna — ingen auth, inga tokens.

| Endpoint | Beskrivning |
|---|---|
| `GET /` | Dashboard (vanilla HTML, ingen build) |
| `GET /api/health` | Hälsa + senaste sync |
| `GET /api/stats` | KPI:er + senaste 20 syncs + top-15 upphandlare |
| `GET /api/tenders?source=&q=&authority=&cpv=&page=&page_size=` | Paginerad lista, max 200/sida |
| `GET /api/tenders/{id}` | Enskild upphandling (inkl. `raw_json`) |
| `GET /api/winners?authority=&cpv=&top=` | **Vem brukar vinna?** — leverantörer rankade per köpare/CPV med totalt värde |
| `GET /api/knowledge?q=&source=` | Kunskapsbas (hållbarhetskriterier + Q&A) |
| `POST /mcp` | MCP-server (Streamable HTTP) för AI-agenter — se nedan |
| `GET /docs` | Swagger UI (auto-genererad av FastAPI) |

**Skriv-/adminåtgärder** (`POST /api/sync`, `/api/backfill`, `/api/reset-ted`,
`/api/repair-links`, `/api/admin/query`, `POST/PUT /api/blog`) kräver headern
`X-Admin-Key` när `ADMIN_API_KEY` är satt. Datan synkas ändå automatiskt varje
dag — du behöver normalt aldrig trigga något.

**Upptäckbarhet (SEO/AEO):** `GET /robots.txt` (välkomnar AI-crawlers),
`GET /llms.txt` (kort beskrivning för LLM:er) och `GET /sitemap.xml` (alla
sidor + upphandlingar) genereras dynamiskt. Statiska assets cache-bustas med
en innehålls-hash (`?v=…`) så design-ändringar slår igenom direkt.

### Sidor (webb — vanilla HTML, ingen JS-build)

| Sida | Innehåll |
|---|---|
| `/` | Startsida — hero, live-KPI:er, senaste upphandlingar |
| `/browse` | Sök & filtrera (fritext, källa, upphandlare, CPV, status) |
| `/tenders/{id}` | Enskild upphandling |
| `/dashboard` | Insikter — toppköpare, CPV-fördelning, "Vem vinner i Sverige" |
| `/providers` | Datakällor, metod och policy (transparens) |
| `/agenter` | Så kopplar du in en agent (MCP + prompt + REST) |
| `/blogg` | Agent-författad blogg om offentlig upphandling |
| `/kunskap` | Kunskapsbank (LOU/LOV, hållbarhetskriterier) |
| `/analytics` | Cookie-fri användningsstatistik (människor / agenter / crawlers) |
| `/system` | Driftstatus + synk-loggar |

**Exempel:**

```bash
# Hämta alla IT-upphandlingar
curl 'http://localhost:8080/api/tenders?q=it&page=1'

# Vem vinner byggupphandlingar hos Trafikverket?
curl 'http://localhost:8080/api/winners?authority=Trafikverket&cpv=45'

# Hämta en specifik upphandling (inkl. hela raw_json)
curl 'http://localhost:8080/api/tenders/42'
```

Svaret är rent JSON. Bygg din egen frontend, integrera i ditt CRM, eller
använd det från en Jupyter notebook — your call.

---

## 🤖 MCP — för AI-agenter

Agentanbud exponerar sina läsdata via [Model Context Protocol](https://modelcontextprotocol.io/)
på `POST /mcp` (Streamable HTTP). Anslut Claude Code, Claude Cowork, Cursor,
Kilo Code m.fl. med bara en URL — inga nycklar, verktygen listas automatiskt.

```bash
claude mcp add --transport http agentanbud https://www.agentanbud.se/mcp
```

**16 öppna läsverktyg:** `search_tenders`, `get_tender`, `similar_tenders`,
`deadline_calendar`, `match_profile`, `get_winner_history`, `get_authority`,
`get_stats`, `list_providers`, `list_regions`, `list_cpv_top`,
`search_knowledge`, `get_knowledge`, `list_posts`, `get_post`, `get_post_stats`.
Plus 2 nyckel-skyddade skrivverktyg (`create_post`, `update_post`) för den
agent-författade bloggen på `/blogg` — se [`MCP.md`](MCP.md).

`get_winner_history` är marknadsintelligensen: fråga *"vem brukar vinna
byggupphandlingar hos Trafikverket?"* och få leverantörer rankade efter
antal vinster och totalt tilldelat värde — beslutsunderlaget ett litet
företag behöver för att avgöra om det är värt att lägga anbud. Full
klientdokumentation i [`MCP.md`](MCP.md).

---

## 🌍 Datakällor

### Live upphandlingar (tenders)

| Källa | Typ | Status |
|---|---|---|
| **Mercell** | Publik JSON-API | ✅ Live (~320 SE records) |
| **TED EU** (Contract Notices) | Publik JSON-API (POST), `notice-type` cn-* | ✅ Live (~7 300, öppna upphandlingar) |
| **TED EU Awards** | Samma API, `notice-type` can-* | ✅ Live (~5 200, ~88% med vinnare) |
| **TED EU PIN** | Samma API, `notice-type` pin-* | ✅ Live (~295, förhandsinfo) |
| **Upphandlingsmyndigheten LOV** | Publik JSON-API | ✅ Live (~429 st) |
| Tendsign / MeForm | Inget öppet API | 🔴 Kräver Selenium (PRs välkomna!) |
| e-Avrop | robots.txt Disallow: / | 🔴 Respekterat — ingen scraping |
| Kommersannons | Inget öppet API | 🔴 Vanilla HTTP-scrape möjligt |
| Clira / Esource | Sanctum-skyddat | 🔴 Kräver konto/headless browser |

> **TED-filtrering:** vi filtrerar på `notice-type` (inte legacy `notice-subtype`,
> som TED:s expert-search tyst tolkar som `cn-standard`). Öppna upphandlingar
> (`cn-*`), tilldelningar (`can-*`) och förhandsinfo (`pin-*`) hålls därför i
> separata källor utan dubbletter.

### Kunskapsbas (knowledge)

Separata från upphandlingar — referensmaterial från Upphandlingsmyndigheten.

| Källa | Vad | Status |
|---|---|---|
| **Hållbarhetskriterier** | Miljökrav per bransch — IT, transport, livsmedel etc. | ✅ Live (~743 st) |
| **Frågeportalen** | Juridisk Q&A om LOU, LOV, tröskelvärden etc. | ✅ Live (~150 unika) |

Kunskapsbasen exponeras via `/kunskap` (HTML) + `/api/knowledge` (JSON)
+ MCP-tools `search_knowledge` / `get_knowledge`. Sökbart via webben och
direkt från AI-agenter. Användbart för att svara på frågor som "vilka
miljökrav gäller typiskt vid IT-upphandling?" eller "vad är LOU?".

**Mercell ensamt täcker 65–70% av svensk upphandlingsvolym** (vi verifierade
det genom att jämföra deras `sourceId`-lista med kända aggregatorers
`source_url`-domäner — Mercell speglar MeForm, e-Avrop, Kommersannons).

**Vill du lägga till en datakälla?** Öppna en PR med en ny `scraper/*.py`
som implementerar `run(db_path) -> int`. Registrera den i
`scraper/orchestrator.py:_registry()`. Klart.

---

## 🗄️ Schema

`tenders` speglar de publika fälten — schema är public-only, inga PII.

```sql
CREATE TABLE tenders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system TEXT NOT NULL,         -- 'mercell' | 'ted' | ...
    source_id TEXT NOT NULL,             -- unik ID inom källan
    tender_url TEXT,                     -- kanonisk deeplänk
    title TEXT,
    authority TEXT,                      -- upphandlande myndighet
    cpv_codes TEXT,                      -- JSON-lista med CPV-koder
    deadline TEXT,                       -- ISO8601
    published_at TEXT,                   -- ISO8601
    description TEXT,
    value REAL,
    procedure TEXT,                      -- t.ex. "Open procedure"
    contract_type TEXT,
    document_type TEXT,
    region TEXT,
    winner_name TEXT,                    -- JSON-lista med tilldelade leverantörer (ted_awards)
    raw_json TEXT,                       -- hela källposten (för debugging)
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_system, source_id)     -- idempotenta synkar
);
```

Övriga tabeller (se [`app/schema.sql`](app/schema.sql)):

- `sync_log` — varje scraper-körning (för dashboardens "senaste syncs").
- `knowledge` — kunskapsbanken (hållbarhetskriterier + Q&A).
- `posts` / `post_events` — den agent-författade bloggen och dess
  läs-statistik (visningar/lästa-hela; ingen IP, inga cookies).
- `usage_log` — driver `/analytics`. Kanal (webb/MCP/API), sökterm, en
  bot-flagga och — för MCP — en anonym sessions-hash för att räkna unika
  agenter. Ingen IP, ingen User-Agent, inget kopplat till en person.

**Inspektera direkt med `sqlite3`-CLI:**

```bash
sqlite3 data/application.db
sqlite> .schema
sqlite> SELECT source_system, COUNT(*) FROM tenders GROUP BY source_system;
sqlite> SELECT title, authority FROM tenders WHERE cpv_codes LIKE '%72%' LIMIT 5;
```

---

## 🤝 Bidra

Vi vill ha bidrag. Speciellt:

- **Nya datakällor** — Tendsign, Kommersannons, kommuners egna
  upphandlingssidor (kräver ofta Selenium/Playwright). Varje scraper är
  en ~150-rad fil som implementerar `run(db_path) -> int`. Tänk på
  dubbletter: kolla om annonsen redan finns via Mercell/TED innan du
  lägger till en källa.
- **CPV-mappning** — `cpv_codes` lagras råa. En `cpv_labels` JOIN-tabell
  med svenska etiketter (via `cpv-eu`-biblioteket) skulle ge oss sökbar
  kategorisering.
- **Notifieringar** — e-post/RSS när nya upphandlingar matchar en query.
- **Fler MCP-verktyg** — t.ex. `similar_authorities` eller anbuds-mallar.
  Se önskelistan i [`MCP.md`](MCP.md).

Inget bidrag är för litet. Öppna en issue först om du vill diskutera
innan du kodar.

---

## 📜 Licens

**MIT** — gör vad du vill med koden.

**Data:** Varje källas egna villkor gäller för den underliggande datan.
Vi speglar den inte — vi pekar bara vidare via `tender_url` till
originalkällan. Om en myndighet tar bort en annons försvinner den från
vår databas vid nästa sync, men den fysiska posten finns kvar i
`raw_json` om du har en lokal kopia.

---

## 🙏 Inspiration

- `magnusfroste/openjobs-api` — samma mikroservice-pattern, för jobb
- Magto/upphandling-matcher — Mercell-scraper vi portade
- `isakskogstad/Upphandlingsdata-MCP` — MCP-server för samma datakällor
- Den publika `cpv-cache.wizflow.ai/cpv/nested` — CPV-trädet

Tack till alla som byggt verktyg vi kunde stå på.
