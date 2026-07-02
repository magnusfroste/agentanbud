# Agentanbud — MCP server (för Claude Code, Kilo Code, Cline m.fl.)

Agentanbud exponerar sina data via [Model Context Protocol](https://modelcontextprotocol.io/) så att AI-agenter (Claude Code, Kilo Code, Cline, OpenAI Assistants m.fl.) kan söka och läsa svenska offentliga upphandlingar direkt i sina arbetsflöden.

## Verktyg

| Tool | Beskrivning |
|---|---|
| `search_tenders` | Sök upphandlingar (fritext, källa, upphandlare, CPV, öppen/stängd) |
| `get_tender` | Hämta en specifik upphandling (full beskrivning) |
| `get_stats` | Databasöversikt + senaste sync |
| `list_providers` | Datakällor + om ansökan kräver konto |
| `list_regions` | Län med upphandlingar |
| `sync_now` | Trigga scraping i bakgrunden |

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

### "Uppdatera datat nu"

```python
sync_now()
# Vänta 60-90 sekunder...
get_stats()                           # verifiera nya counts
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

**Dokument och anbudsformulär** (PDF:er, kravspecifikationer) finns hos plattformarna — vi speglar dem inte.

---

## Designval

### Varför dispatcher-pattern (FlowWink-stil)?

FlowWink har 200+ skills och använder två dispatcher-tools (`search_skills` + `execute_skill`) för att inte slösa context-fönstret på 200 tool-definitioner. Vi har bara **6 verktyg, alla relaterade till samma domän** så vi registrerar dem rakt — enklare för LLM:en att lära sig.

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

## Installation (Claude Desktop)

Lägg till i `~/Library/Application Support/Claude/claude_desktop_config.json` (Mac) eller motsvarande:

```json
{
  "mcpServers": {
    "agentanbud": {
      "command": "python",
      "args": ["-m", "mcp_server"],
      "cwd": "/path/to/agentanbud",
      "env": {"DB_PATH": "/data/agentanbud.db"}
    }
  }
}
```

Starta om Claude Desktop — Agentanbud-verktygen dyker upp i verktygslistan.

## Installation (Kilo Code / Cline / Continue)

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

MCP-servern är **read-only** mot databasen. Den kan:
- ✅ Söka och läsa upphandlingar
- ✅ Lista providers/regions/stats
- ✅ Trigga scraping (`sync_now`)

Den kan **INTE**:
- ❌ Modifiera databasen direkt
- ❌ Läsa filer utanför /data
- ❌ Köra godtyckliga shell-kommandon (bara `scraper.orchestrator`)

## Bidra

Vi vill ha fler tools! Speciellt:
- `match_keywords(profile)` — hitta upphandlingar som matchar en användares profil
- `list_cpv_top(n)` — top CPV-kategorier i databasen
- `get_authority(name)` — alla upphandlingar från en specifik organisation
- `get_stats_by_cpv(prefix)` — statistik uppdelat per CPV-grupp

Öppna en PR.
