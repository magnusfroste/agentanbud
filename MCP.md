# Agentanbud — MCP server (för Claude Code, Kilo Code, Cline m.fl.)

Agentanbud exponerar sina data via [Model Context Protocol](https://modelcontextprotocol.io/) så att AI-agenter (Claude Code, Kilo Code, Cline, OpenAI Assistants m.fl.) kan söka och läsa svenska offentliga upphandlingar direkt i sina arbetsflöden.

## Verktyg

| Tool | Beskrivning |
|---|---|
| `search_tenders` | Sök upphandlingar (fritext, källa, upphandlare, CPV, öppen/stängd) |
| `get_tender` | Hämta en specifik upphandling (full beskrivning + länk till originalannons) |
| `match_profile` | Matcha upphandlingar mot en profil — nyckelord, CPV-prefix, regioner |
| `get_authority` | Alla upphandlingar från en organisation, t.ex. Trafikverket |
| `get_stats` | Databasöversikt + senaste sync |
| `list_providers` | Datakällor + om ansökan kräver konto |
| `list_regions` | Län med upphandlingar |
| `list_cpv_top` | Vanligaste CPV-kategorierna i databasen |
| `search_knowledge` | Sök i kunskapsbanken (hållbarhetskriterier + Q&A) |
| `get_knowledge` | Hämta ett kunskapsobjekt i detalj |

Den publika MCP-endpointen är **read-only** — skriv-/adminåtgärder ligger bakom
nyckel i REST-API:t. (Stdio-servern för lokal körning har även `sync_now`.)

---

## Cookbook — vanliga mönster

Det här är **working examples** agenten ska kunna hantera direkt. Exempelfrasen kommer från användaren, agenten väljer rätt verktyg och parametrar.

### "Vad har ni för data?"

```
get_stats()
list_providers()
list_regions()
```

### "Hitta IT-upphandlingar i Stockholm som är öppna"

```python
search_tenders(
    query="IT",
    authority="Stockholm",
    open_only=True,
    limit=10
)
```

### "Hitta konstruktionsupphandlingar i hela landet (även stängda)"

```python
search_tenders(
    cpv="45",          # CPV 45 = construction
    open_only=False,
    limit=20
)
```

### "Vad har Uppsala län för upphandlingar?"

```python
list_regions()                        # bekräfta att Uppsala finns
search_tenders(authority="Uppsala")
```

### "Detaljerna på upphandling #142"

```python
get_tender(id=142)
```

### "Bevaka upphandlingar som matchar vår profil"

```python
match_profile(
    keywords=["bredband", "IoT", "fiber"],
    cpv_prefixes=["32", "72"],
    regions=["Stockholms län"]
)
# Vid träff: get_tender(id=...) → följ länken → hämta underlag → skriv anbudsutkast
```

### "Vad har Trafikverket öppet just nu?"

```python
get_authority(name="Trafikverket")
```

### "Vilka organisationer har flest upphandlingar?"

```
get_stats()                           # visar per-källa
# För top upphandlare: använd browse-sidan https://<host>/browse
```

---

## Viktigt: data vs ansökan

Agentanbud speglar **publik data** (titlar, beskrivningar, deadlines, CPV-koder). **Att ansöka** kräver ofta ett konto hos plattformen:

| Källa | Data (läs) | Att ansöka |
|---|---|---|
| TED EU | ✅ öppet, ingen inloggning | ✅ via eu.europa.eu |
| Mercell | ✅ via vårt API | ❌ Mercell-konto krävs |
| Tendsign (Visma) | 🔴 inte i MVP | ❌ konto krävs |
| e-Avrop | 🔴 inte i MVP | ❌ konto krävs |
| Kommersannons | 🔴 inte i MVP | ❌ konto krävs |
| Clira (Esource) | 🔴 inte i MVP | ❌ betal-SaaS, konto krävs |

**Dokument och anbudsformulär** (PDF:er, kravspecifikationer) finns hos plattformarna — vi speglar dem inte. Så här hämtar en agent dem:

- **TED**: öppna `tender_url` — helt publikt. Upphandlingsdokumenten ligger hos upphandlarens plattform; leta efter *"Address of the procurement documents"* i annonsen och följ den länken.
- **Mercell**: `tender_url` visar annonsen publikt. Bilagor och anbudsinlämning kräver inloggat Mercell-konto — har din användare ett: logga in, öppna länken och hämta bilagorna under **Documents**.

---

## Designval

### Varför dispatcher-pattern (FlowWink-stil)?

FlowWink har 200+ skills och använder två dispatcher-tools (`search_skills` + `execute_skill`) för att inte slösa context-fönstret på 200 tool-definitioner. Vi har bara **10 verktyg, alla relaterade till samma domän** så vi registrerar dem rakt — enklare för LLM:en att lära sig.

### Varför stdio-transport?

- Enkelt: ingen HTTP-server, ingen auth
- Lokal: agent-processen startar MCP-processen som child
- Säkert: ingen publik endpoint
- Stödjs av alla MCP-klienter

När vi behöver fjärråtkomst (t.ex. för hostad version) kan vi lägga till SSE/HTTP-transport.

### Best practices vi följer

1. **Korta descriptions, konkreta exempel** — 1 mening + "Examples: 'IT-konsult', 'vägbyggnation'..."
2. **Enums med explicita värden** — `["mercell", "ted"]` istället för "data source"
3. **Säkra defaults** — `open_only=true` så agenten inte får stängda upphandlingar som default
4. **Markdown-formaterad output** — lätt för LLM att extrahera
5. **Paywall-info i output** — agenten vet om konto krävs
6. **Errors med nästa steg** — "Sync kunde inte startas — kör: python -m scraper.orchestrator"
7. **Names som är självförklarande** — `search_tenders`, inte `tender_query_v1`

---

## Installation

### Alt 1: Remote (Streamable HTTP) — inget att installera

Den publika servern exponerar MCP på `https://www.agentanbud.se/mcp`.
Bara att peka klienten på URL:en, ingen lokal kod behövs.

**Claude Code** (ett kommando):

```bash
claude mcp add --transport http agentanbud https://www.agentanbud.se/mcp
```

**Claude Cowork**: Inställningar → Connectors → Add custom connector →
URL `https://www.agentanbud.se/mcp`.

**Övriga klienter** (Cursor, Windsurf, Kilo Code, Cline m.fl.):

```json
{
  "mcpServers": {
    "agentanbud": {
      "url": "https://www.agentanbud.se/mcp",
      "transport": "streamable-http"
    }
  }
}
```

Klienten listar tools automatiskt vid connect.

### Alt 2: Lokal (stdio) — för utvecklare

Lägg till i `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) eller motsvarande:

```json
{
  "mcpServers": {
    "agentanbud": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/agentanbud",
      "env": {"DB_PATH": "/data/application.db"}
    }
  }
}
```

Starta om Claude Desktop — Agentanbud-verktygen dyker upp i verktygslistan.

### Installation (Kilo Code / Cline / Continue)

Samma princip — ange `command` och `args` i din klient-konfiguration.

**Kilo Code:** Inställningar → MCP Servers → "Add Server" → typ = `stdio`, command = `python -m mcp_server`, cwd = din sökväg.

## Testa

```bash
# Från repot:
python -m mcp_server

# Eller med mcp-inspector (visuell test):
npx @modelcontextprotocol/inspector python -m mcp_server
```

## Säkerhet

Den publika MCP-endpointen (`/mcp`) är **read-only**. Den kan:
- ✅ Söka och läsa upphandlingar och kunskapsbank
- ✅ Lista providers/regions/stats

Den kan **INTE**:
- ❌ Modifiera databasen
- ❌ Trigga scraping — synk sker automatiskt dagligen; manuella
  skrivåtgärder ligger bakom `X-Admin-Key` i REST-API:t
- ❌ Köra shell-kommandon

Stdio-servern (lokal körning mot egen databas) har även `sync_now`,
eftersom den som kör den redan har lokal åtkomst.

## Bidra

Vi vill ha fler tools! Idéer:
- `get_stats_by_cpv(prefix)` — statistik uppdelat per CPV-grupp
- `similar_tenders(id)` — hitta liknande upphandlingar (samma CPV/upphandlare)
- `deadline_calendar(days)` — kommande deadlines som kalenderöversikt

Öppna en PR.
