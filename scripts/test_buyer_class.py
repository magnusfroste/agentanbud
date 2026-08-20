#!/usr/bin/env python3
"""Cases the buyer classifier must get right.

Each one is a name that broke a real analysis. Run: python3 scripts/test_buyer_class.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from app.buyer_class import classify

CASES = [
    # (name, city, expected, why this case exists)
    ("Halmstads kommun", "Halmstad", "municipal", "the easy one"),
    ("Stockholms stad", "Stockholm", "municipal", "stad, not kommun"),
    ("Trafikkontoret", "Stockholm", "municipal",
     "compound ending: \\bkontoret\\b never matches inside 'Trafikkontoret'"),
    ("Stadskontoret", "Malmö", "municipal", "same compound trap"),
    ("Serviceförvaltningen", "Stockholm", "municipal", "form named, owner not"),
    ("Helsingborgshem AB", "Helsingborg", "municipal",
     "housing company named after its owner, no word boundary after the town"),
    ("VA SYD", "Malmö", "municipal", "inter-municipal utility"),
    ("Region Skåne", "Malmö", "regional", "prefix form"),
    ("Västra Götalandsregionen", "Vänersborg", "regional",
     "suffix form — ^region\\b misses it, and it is 313 notices"),
    ("Locum AB", "Stockholm", "regional", "region-owned, names no region"),
    ("Stockholms läns sjukvårdsområde", "Stockholm", "regional", "regional health body"),
    ("Trafikverket", "Borlänge", "state",
     "the reason geography needs this module at all"),
    ("Statens fastighetsverk", "Stockholm", "state", "state, sits in a city"),
    ("FMV", "Stockholm", "state", "acronym, no hint in the name"),
    ("Kammarkollegiet", "Stockholm", "state", "no -verket, still state"),
    ("Karolinska Institutet", "Stockholm", "state", "university"),
    ("Säkerhetspolisen, SÄPO", "Stockholm", "state", "comma-suffixed name"),
    ("Ellevio AB", "Stockholm", "unknown",
     "private company — must not be counted as public procurement"),
]

def main() -> int:
    bad = []
    for name, city, want, why in CASES:
        got = classify(name, city)
        mark = "PASS" if got == want else "FAIL"
        print(f"  {mark}  {got:<10} {name[:34]:<36} {why}")
        if got != want:
            bad.append(f"{name}: fick {got}, väntade {want}")
    print()
    if bad:
        print(f"❌ {len(bad)} av {len(CASES)} fel:")
        for b in bad:
            print("   •", b)
        return 1
    print(f"✅ Alla {len(CASES)} fall korrekta")
    return 0

if __name__ == "__main__":
    sys.exit(main())
