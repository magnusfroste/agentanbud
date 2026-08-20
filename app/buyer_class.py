"""
What kind of buyer is this?

Geography is unusable without it. A buyer's registered town is meaningful for
a municipality — it procures for that municipality — and meaningless for a
national agency, which procures for the country from one head office.
Trafikverket alone made Borlänge, population 42 000, the second-busiest
procurement town in the data.

Name matching on "kommun" cannot separate the two: large cities procure
through companies and administrations — Locum, SISAB, Trafikkontoret — that
are municipal without saying so, so the filter caught 89% of Halmstad's
notices and 13% of Stockholm's.

Returns one of:
    "municipal" — a municipality, its administrations, or a company it owns
    "regional"  — a region/landsting or a company it owns
    "state"     — national agencies, universities, state-owned companies
    "unknown"   — not determined; counted and reported, never guessed

`unknown` is a real answer. A wrong classification silently moves volume
between places, which is the failure this module exists to prevent.
"""
from __future__ import annotations

import re

# Sweden's municipalities. Used to spot companies named after their owner
# ("Borlänge Energi", "Skolfastigheter i Stockholm"), not just "X kommun".
KOMMUNER = set("""
Ale Alingsås Alvesta Aneby Arboga Arjeplog Arvidsjaur Arvika Askersund Avesta
Bengtsfors Berg Bjurholm Bjuv Boden Bollebygd Bollnäs Borgholm Borlänge Borås
Botkyrka Boxholm Bromölla Bräcke Burlöv Båstad Dals-Ed Danderyd Degerfors
Dorotea Eda Ekerö Eksjö Emmaboda Enköping Eskilstuna Eslöv Essunga Fagersta
Falkenberg Falköping Falun Filipstad Finspång Flen Forshaga Färgelanda Gagnef
Gislaved Gnesta Gnosjö Gotland Grums Grästorp Gullspång Gällivare Gävle
Göteborg Götene Habo Hagfors Hallsberg Hallstahammar Halmstad Hammarö Haninge
Haparanda Heby Hedemora Helsingborg Herrljunga Hjo Hofors Huddinge Hudiksvall
Hultsfred Hylte Håbo Hällefors Härjedalen Härnösand Härryda Hässleholm Höganäs
Högsby Hörby Höör Jokkmokk Järfälla Jönköping Kalix Kalmar Karlsborg Karlshamn
Karlskoga Karlskrona Karlstad Katrineholm Kil Kinda Kiruna Klippan Knivsta
Kramfors Kristianstad Kristinehamn Krokom Kumla Kungsbacka Kungsör Kungälv
Kävlinge Köping Laholm Landskrona Laxå Lekeberg Leksand Lerum Lessebo Lidingö
Lidköping Lilla Edet Lindesberg Linköping Ljungby Ljusdal Ljusnarsberg Lomma
Ludvika Luleå Lund Lycksele Lysekil Malmö Malung-Sälen Malå Mariestad Mark
Markaryd Mellerud Mjölby Mora Motala Mullsjö Munkedal Munkfors Mölndal
Mönsterås Mörbylånga Nacka Nora Norberg Nordanstig Nordmaling Norrköping
Norrtälje Norsjö Nybro Nykvarn Nyköping Nynäshamn Nässjö Ockelbo Olofström
Orsa Orust Osby Oskarshamn Ovanåker Oxelösund Pajala Partille Perstorp Piteå
Ragunda Robertsfors Ronneby Rättvik Sala Salem Sandviken Sigtuna Simrishamn
Sjöbo Skara Skellefteå Skinnskatteberg Skurup Skövde Smedjebacken Sollefteå
Sollentuna Solna Sorsele Sotenäs Staffanstorp Stenungsund Stockholm Storfors
Storuman Strängnäs Strömstad Strömsund Sundbyberg Sundsvall Sunne Surahammar
Svalöv Svedala Svenljunga Säffle Säter Sävsjö Söderhamn Söderköping Södertälje
Sölvesborg Tanum Tibro Tidaholm Tierp Timrå Tingsryd Tjörn Tomelilla Torsby
Torsås Tranemo Tranås Trelleborg Trollhättan Trosa Tyresö Täby Töreboda
Uddevalla Ulricehamn Umeå Upplands-Bro Uppsala Uppvidinge Vadstena Vaggeryd
Valdemarsvik Vallentuna Vansbro Vara Varberg Vaxholm Vellinge Vetlanda
Vilhelmina Vimmerby Vindeln Vingåker Vårgårda Vänersborg Vännäs Värmdö Värnamo
Västervik Västerås Växjö Ydre Ystad Åmål Ånge Åre Årjäng Åsele Åstorp Åtvidaberg
Älmhult Älvdalen Älvkarleby Älvsbyn Ängelholm Öckerö Ödeshög Örebro Örkelljunga
Örnsköldsvik Östersund Österåker Östhammar Östra Göinge Överkalix Övertorneå
""".split())

REGIONER = {"Blekinge","Dalarna","Gotland","Gävleborg","Halland","Jämtland",
            "Jönköping","Kalmar","Kronoberg","Norrbotten","Skåne","Stockholm",
            "Sörmland","Södermanland","Uppsala","Värmland","Västerbotten",
            "Västernorrland","Västmanland","Örebro","Östergötland",
            "Västra Götaland"}

# National bodies whose name gives no hint, or whose hint points the wrong way.
# Trafikkontoret and Fastighetskontoret are municipal despite sounding official;
# these are the reverse.
STATE_EXACT = {
    "fmv","försvarets materielverk","försvarsmakten","kammarkollegiet",
    "statens inköpscentral vid kammarkollegiet","regeringskansliet",
    "arbetsförmedlingen","försäkringskassan","skatteverket","kronofogden",
    "migrationsverket","polismyndigheten","kriminalvården","tullverket",
    "riksdagsförvaltningen","riksbanken","pensionsmyndigheten","csn",
    "centrala studiestödsnämnden","lantmäteriet","sida","vinnova",
    "energimyndigheten","naturvårdsverket","socialstyrelsen","folkhälsomyndigheten",
    "läkemedelsverket","skolverket","msb","myndigheten för samhällsskydd och beredskap",
    "svenska kraftnät","affärsverket svenska kraftnät","sjöfartsverket",
    "luftfartsverket","statens fastighetsverk","fortifikationsverket",
    "specialpedagogiska skolmyndigheten","adda inköpscentral ab","adda ab",
    "sveriges riksbank","statistiska centralbyrån","scb","transportstyrelsen",
    "trafikverket","statens servicecenter","upphandlingsmyndigheten",
    "havs- och vattenmyndigheten","jordbruksverket","statens jordbruksverk",
    "säkerhetspolisen","säpo","kungliga operan ab","kungliga dramatiska teatern ab",
    "arbetsförmedlingen varor & tjänster","fra","försvarets radioanstalt",
}
# Municipal organisational forms. A "förvaltning" or "kontor" belongs to the
# municipality it sits in — its name rarely says which, but buyer_city does.
# No leading \b: these are compound endings. "Trafikkontoret" and
# "Stadskontoret" are one word, so \bkontoret\b never matched them — the
# boundary it wants sits inside the compound.
MUNICIPAL_FORM = re.compile(
    r"(förvaltningen|förvaltning|kontoret|nämnden|stadsbyggnad|"
    r"stadsmiljö|exploatering|kretslopp|renhållning|inköpscentral)", re.I)

STATE_PAT = re.compile(
    r"\b(statens|sveriges|riks[a-zåäö]*|universitet|högskola|högskolan|"
    r"universitetet|akademiska hus|specialfastigheter|vasakronan|jernhusen|"
    r"svenska spel|systembolaget|apoteket ab|postnord|sj ab|green cargo|"
    r"lernia|samhall|infranord|swedavia|sveaskog|vattenfall|lkab|"
    r"myndighet|myndigheten|domstol|domstolsverket|länsstyrelsen|institutet|forskningsinstitut|rise research|försvarets)\b", re.I)

# Region-owned companies whose name names no region.
REGIONAL_EXACT = {
    "locum ab","locum","ab storstockholms lokaltrafik","sl","ab transitio",
    "stockholms läns sjukvårdsområde","folktandvården stockholms län ab",
    "västtrafik ab","västtrafik","skånetrafiken","ambulanssjukvården",
    "danderyds sjukhus ab","södersjukhuset ab","karolinska universitetssjukhuset",
    "s:t eriks ögonsjukhus ab","tiohundra ab",
}


def _norm(name: str) -> str:
    n = (name or "").split(",")[0].strip().lower()
    n = re.sub(r"\s+(aktiebolag)\b", " ab", n)
    return re.sub(r"\s+", " ", n).strip()


def classify(authority: str, city: str | None = None) -> str:
    """Classify one buyer. Order matters: the explicit lists win over the
    patterns, because they exist precisely for names the patterns get wrong.

    `city` is the buyer's registered town. It is what rescues municipal
    administrations — "Trafikkontoret", "Stadskontoret", "Serviceförvaltningen"
    name an organisational form and not the municipality that owns it, and are
    the reason a name-only filter caught 13% of Stockholm.
    """
    n = _norm(authority)
    if not n:
        return "unknown"
    if n in STATE_EXACT:
        return "state"
    if n in REGIONAL_EXACT:
        return "regional"

    # "Region Skåne", "Västra Götalandsregionen", "Skåne läns landsting"
    if re.match(r"^region\b", n) or re.search(r"regionen\b", n) or "landsting" in n:
        return "regional"
    # Regional health/transport bodies naming their region
    if re.search(r"\b(regionfastigheter|regionservice|regionteater)\b", n):
        return "regional"

    if STATE_PAT.search(n):
        return "state"

    # "Halmstads kommun", "Stockholms stad", "Telge Inköp" is caught below
    if re.search(r"\b(kommun|kommunen|kommunalförbund|stadsdelsförvaltning)\b", n):
        return "municipal"

    # A company named after the municipality that owns it: "Borlänge Energi",
    # "Skolfastigheter i Stockholm", "AB Stångåstaden" is not caught — the tail
    # of municipal companies without a place name is what "unknown" is for.
    for k in KOMMUNER:
        kl = k.lower()
        # Prefix, not whole word: "Helsingborgshem" and "Stockholmshem" are
        # municipal housing companies named after their owner.
        if re.search(rf"\b{re.escape(kl)}s?", n):
            # Region X already returned above, so a place name here is municipal
            return "municipal"

    if re.search(r"\bstad\b", n):
        return "municipal"

    # Inter-municipal federations and jointly owned utilities. Municipal in
    # nature, but serving several municipalities at once — VA SYD covers Malmö,
    # Lund, Burlöv, Eslöv and Höör — so the buyer's town is not the whole story.
    # Callers building geography should treat these as shared, which is why
    # they are named rather than folded silently into one town.
    if re.search(r"(kommunalförbund|räddningstjänst|va syd|sysav|sydvatten|"
                 r"renova|gryaab|kretslopp och vatten)", n):
        return "municipal"

    # An administration form plus a town we recognise: the town is the owner.
    if city and MUNICIPAL_FORM.search(n):
        c = (city or "").strip().title()
        if c in KOMMUNER:
            return "municipal"
    return "unknown"
