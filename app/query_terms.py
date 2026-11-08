"""
Search terms in German and English, searched in Swedish too.

The notices are in Swedish, the visitors not always: on 4 November 2026
someone searched "Klimadatensatz", "wetterdaten" and "klimadaten", and the
most frequent unclassified searches that month included "sustainability",
"circular business model" and "foresight" — all with nothing found, though
the database holds tenders for each in Swedish. A query word found here is
searched as itself OR its Swedish equivalents (app/search.build_match), so
an English title on TED still matches as before and a Swedish one now too.

Kept small and literal on purpose: words seen in our own search log, and the
everyday words of procurement (cleaning, construction, consultancy, IT,
health, energy). A translation is a guess about meaning, so each entry is
one a Swedish procurement officer would write for the same thing; nothing
is translated that already means something else in Swedish. Add entries as
the log shows them (unmet demand on /analytics).

German compounds are matched by their start, longest entry first, so
"klimadatensatz" is found through "klimadaten" — the same backoff the
search applies to Swedish compounds.
"""
from __future__ import annotations

# Lower case. Values: Swedish terms to search as well; a space makes a phrase.
TERMS: dict[str, tuple[str, ...]] = {
    # --- Weather, climate, environment (seen in the log)
    "wetterdaten": ("väderdata", "väderprognos", "meteorolog"),
    "wetter": ("väder",),
    "klimadaten": ("klimatdata", "klimat"),
    "klima": ("klimat",),
    "klimaanpassung": ("klimatanpassning",),
    "niederschlag": ("nederbörd",),
    "hochwasser": ("högvatten", "översvämning"),
    "überschwemmung": ("översvämning",),
    "flood": ("översvämning", "högvatten"),
    "flooding": ("översvämning",),
    "hydrologie": ("hydrologi",),
    "meteorologie": ("meteorolog",),
    "umwelt": ("miljö",),
    "nachhaltigkeit": ("hållbarhet",),
    "weather": ("väder",),
    "weather data": ("väderdata", "väderprognos", "meteorolog"),
    "climate": ("klimat",),
    "climate data": ("klimatdata", "klimat"),
    "climate adaptation": ("klimatanpassning",),
    "precipitation": ("nederbörd",),
    "hydrology": ("hydrologi",),
    "meteorology": ("meteorolog",),
    "environment": ("miljö",),
    "environmental": ("miljö",),
    "sustainability": ("hållbarhet",),
    "sustainable": ("hållbar",),
    "circular economy": ("cirkulär ekonomi", "cirkulär"),
    "circular business model": ("cirkulär affärsmodell", "cirkulär"),
    "circular": ("cirkulär",),
    "foresight": ("omvärldsanalys", "framtidsscenarier", "framsyn"),
    "innovation programme": ("innovationsprogram",),
    "innovation program": ("innovationsprogram",),
    "accessibility": ("tillgänglighet",),
    # --- Energy, water, waste
    "energie": ("energi",),
    "energy": ("energi",),
    "strom": ("el", "elenergi"),
    "electricity": ("el", "elenergi"),
    "photovoltaik": ("solcell", "solenergi"),
    "solar": ("solcell", "solenergi"),
    "windkraft": ("vindkraft",),
    "wind power": ("vindkraft",),
    "wasser": ("vatten",),
    "water": ("vatten",),
    "abwasser": ("avlopp",),
    "wastewater": ("avlopp",),
    "abfall": ("avfall",),
    "waste": ("avfall",),
    # --- Construction and property
    "bauarbeiten": ("byggarbete", "entreprenad"),
    "bau": ("bygg",),
    "construction": ("bygg", "entreprenad"),
    "straßenbau": ("väg", "anläggning"),
    "road": ("väg",),
    "brücke": ("bro",),
    "bridge": ("bro",),
    "architekt": ("arkitekt",),
    "architect": ("arkitekt",),
    "gebäudereinigung": ("lokalvård", "städ"),
    "reinigung": ("städ", "rengöring"),
    "cleaning": ("städ", "lokalvård"),
    "facility management": ("fastighetsförvaltning", "fastighetsskötsel"),
    "vermessung": ("mätning", "lantmäteri"),
    "surveying": ("mätning", "lantmäteri"),
    "kartierung": ("kartering",),
    "mapping": ("kartering",),
    # --- Services and IT
    "beratung": ("konsult", "rådgivning"),
    "consulting": ("konsult",),
    "consultancy": ("konsult",),
    "ingenieur": ("ingenjör", "teknikkonsult"),
    "engineering": ("ingenjör", "teknikkonsult"),
    "software": ("programvara", "mjukvara"),
    "datenbank": ("databas",),
    "database": ("databas",),
    "datenanalyse": ("dataanalys",),
    "data analysis": ("dataanalys",),
    "cybersicherheit": ("cybersäkerhet", "informationssäkerhet"),
    "cybersecurity": ("cybersäkerhet", "informationssäkerhet"),
    "sicherheit": ("säkerhet",),
    "security": ("säkerhet",),
    "künstliche intelligenz": ("ai", "artificiell intelligens"),
    "artificial intelligence": ("ai", "artificiell intelligens"),
    "machine learning": ("maskininlärning",),
    "computer vision": ("datorseende", "bildanalys"),
    "video analytics": ("videoanalys",),
    "chatbot": ("chattbot",),
    "chattbot": ("chatbot",),
    "user research": ("användarundersökning",),
    "service design": ("tjänstedesign",),
    "customer experience": ("kundupplevelse",),
    "web design": ("webbdesign", "webbplats"),
    "website": ("webbplats",),
    "übersetzung": ("översättning",),
    "translation": ("översättning",),
    "dolmetschen": ("tolk",),
    "interpreting": ("tolk",),
    "schulung": ("utbildning",),
    "weiterbildung": ("utbildning",),
    "training": ("utbildning",),
    "forschung": ("forskning",),
    "research": ("forskning",),
    # --- Health, food, goods, transport
    "gesundheit": ("hälsa", "vård"),
    "healthcare": ("vård", "sjukvård"),
    "krankenhaus": ("sjukhus",),
    "hospital": ("sjukhus",),
    "medizinprodukte": ("medicinteknik",),
    "medical devices": ("medicinteknik",),
    "arzneimittel": ("läkemedel",),
    "pharmaceuticals": ("läkemedel",),
    "lebensmittel": ("livsmedel",),
    "food": ("livsmedel",),
    "möbel": ("möbler",),
    "furniture": ("möbler",),
    "fahrzeug": ("fordon",),
    "vehicle": ("fordon",),
    "vehicles": ("fordon",),
    # --- Procurement itself
    "ausschreibung": ("upphandling",),
    "beschaffung": ("upphandling", "inköp"),
    "tender": ("upphandling",),
    "procurement": ("upphandling",),
    "rahmenvertrag": ("ramavtal",),
    "framework agreement": ("ramavtal",),
    "dienstleistung": ("tjänst",),
}

_MIN_PREFIX = 6   # a compound is matched through an entry at least this long
# Keys folded as queries are ("ß" becomes "ss": "Straßenbau" is "strassenbau").
_FOLDED = {" ".join(k.casefold().split()): v for k, v in TERMS.items()}
_BY_LENGTH = sorted((k for k in _FOLDED if " " not in k and len(k) >= _MIN_PREFIX), key=len, reverse=True)


def swedish_for(text: str) -> tuple[str, ...]:
    """Swedish terms to search as well as `text` (a word or a whole query)."""
    t = " ".join((text or "").casefold().split())
    if t in _FOLDED:
        return _FOLDED[t]
    if " " in t:
        return ()
    for key in _BY_LENGTH:
        if t.startswith(key):
            return _FOLDED[key]
    return ()
