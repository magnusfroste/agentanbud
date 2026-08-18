#!/usr/bin/env python3
"""Post-deploy smoke test for agentanbud.

Run it after every deploy:

    python3 scripts/smoke_test.py --expect-sha $(git rev-parse --short HEAD)
    python3 scripts/smoke_test.py --base http://localhost:8080 --wait 0
    python3 scripts/smoke_test.py                 # against production, as-is

Exit code 0 = everything passed, 1 = something is wrong.

Why this exists — every check here maps to a bug that actually shipped:

* **Filters that silently match everything.** `/api/tenders?cpv=72` returned all
  15 240 rows because the endpoint had no cpv parameter; FastAPI dropped the
  unknown query param without a word. A filter returning *the unfiltered total*
  is as broken as one returning zero, and neither raises an error.
* **A deploy that didn't land.** A failed build left the previous image running,
  so the site looked healthy while the fix was missing. We assert the running
  code exposes the parameters we expect, instead of trusting the deploy.
* **500s only on edge rows.** Closed tenders crashed for weeks (Jinja has no
  `abs()`), because normal browsing never opens one.
* **Write endpoints must stay shut.** They are the only destructive surface.
* **The MCP tools are the product** for agents — a tool that returns an empty
  string is a silent outage for every connected agent.

Stdlib only, so it runs anywhere with no install step.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

UA = "agentanbud-smoke/1.0"
DEFAULT_BASE = "https://www.agentanbud.se"

# Query parameters the running code must expose. Guards against a deploy that
# silently rolled back to an older image.
EXPECTED_TENDER_PARAMS = {"source", "authority", "q", "cpv", "page", "page_size"}

failures: list[str] = []
checks_run = 0


def check(name: str, ok: bool, detail: str = "") -> bool:
    global checks_run
    checks_run += 1
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not ok:
        failures.append(f"{name}{': ' + detail if detail else ''}")
    return ok


def get(base: str, path: str, timeout: int = 30, headers: dict | None = None):
    """GET -> (status, body_bytes, headers). Never raises.

    A 4xx/5xx is a normal answer here — several checks assert on one. Status 0
    means we never got an answer at all (refused, DNS, TLS, timeout): that is
    the case this script exists for, so it has to report it rather than crash.
    Only HTTPError was caught before, and a refused connection raises URLError,
    so pointing the test at a host that was actually down ended in a traceback.
    """
    req = urllib.request.Request(base + path)
    req.add_header("User-Agent", UA)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)
    except Exception as e:  # URLError, socket.timeout, ssl errors, …
        return 0, str(e).encode(), {}


def get_json(base: str, path: str, timeout: int = 30):
    status, body, _ = get(base, path, timeout)
    if status != 200:
        return status, None
    try:
        return status, json.loads(body)
    except Exception:
        return status, None


def post_json(base: str, path: str, payload: dict, key: str | None = None):
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    if key:
        req.add_header("X-Admin-Key", key)
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def sha_matches(running: str, want: str) -> bool:
    """True when both strings name the same commit.

    Either side may be abbreviated, so we compare over the shorter length.
    An empty or too-short commit matches nothing: the earlier form used
    `want.startswith(running[:7])`, and every string starts with "", so a
    build reporting no commit at all satisfied every --expect-sha — passing
    the one check whose whole job is to catch a deploy that did not land.
    """
    a, b = (running or "").strip().lower(), (want or "").strip().lower()
    if len(a) < 7 or len(b) < 7:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def wait_for_health(base: str, seconds: int, expect_sha: str | None = None) -> bool:
    """Poll /api/health until the build we are testing is the one serving.

    Waiting for a 200 is not enough during a deploy: the *previous* container
    keeps answering 200 the whole time the new image builds, so health alone
    is satisfied on the first attempt and every later check then runs against
    the old code. With --expect-sha we wait for that commit to actually be
    live, which is the condition the caller means by "wait for the deploy".

    Without --expect-sha there is nothing to compare against, so we fall back
    to plain reachability and the caller accepts that weaker guarantee.
    """
    deadline = time.time() + seconds
    attempt = 0
    last_seen = None
    while True:
        attempt += 1
        status, health = get_json(base, "/api/health", timeout=15)
        if status == 200:
            if not expect_sha:
                if attempt > 1:
                    print(f"  (uppe efter {attempt} försök)")
                return True
            running = ((health or {}).get("build") or {}).get("commit") or ""
            if sha_matches(running, expect_sha):
                if attempt > 1:
                    print(f"  (rätt bygge live efter {attempt} försök)")
                return True
            if running[:7] != last_seen:
                last_seen = running[:7]
                print(f"  …uppe, men kör {last_seen or '?'} — väntar på {expect_sha[:7]}")
        if time.time() >= deadline:
            return False
        time.sleep(10)


# The SHA comparison is the check that decides whether we are testing the build
# we think we are — every other check is scoped by its verdict. It shipped once
# with `want.startswith(running[:7])`, which is true for an empty commit because
# every string starts with "", so a build reporting no version data satisfied any
# --expect-sha. These cases pin that behaviour: an unknown build must fail closed.
SHA_CASES = [
    ("98678e4ccf4dd50f965a271a34d2bdcff3c6a8b2", "98678e4",      True,  "full commit vs kort --expect-sha"),
    ("98678e4",                                  "98678e4ccf4d", True,  "kort commit vs full --expect-sha"),
    ("98678E4CCF4D",                             "98678e4",      True,  "versaler"),
    ("5ee70b1ccf4d",                             "98678e4",      False, "ett annat bygge"),
    ("",                                         "98678e4",      False, "bygget rapporterar ingen commit"),
    ("unknown",                                  "98678e4",      False, "commit = unknown"),
    ("98678e4",                                  "",             False, "tom --expect-sha"),
    ("98678e4",                                  "986",          False, "för kort --expect-sha"),
]


def self_test() -> bool:
    """Check the instrument before trusting its readings. Runs offline, so it
    goes first: if this is broken there is no point waiting for a deploy."""
    bad = [label for running, want, expected, label in SHA_CASES
           if sha_matches(running, want) != expected]
    return check("SHA-jämförelsen är korrekt",
                 not bad,
                 "; ".join(bad) if bad else f"{len(SHA_CASES)} fall")


def main() -> int:
    ap = argparse.ArgumentParser(description="Post-deploy smoke test for agentanbud")
    ap.add_argument("--base", default=DEFAULT_BASE, help="base URL to test")
    ap.add_argument("--wait", type=int, default=420,
                    help="seconds to wait for the deploy before testing; 0 to test immediately. "
                         "With --expect-sha it waits for that build to be live, not just for a 200")
    ap.add_argument("--expect-sha", default=None,
                    help="commit you just deployed; fails if another build is serving")
    ap.add_argument("--key", default=None,
                    help="admin key; only used to confirm write endpoints reject requests without it")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print(f"Rökt-test mot {base}\n")

    print("0. Testet självt")
    if not self_test():
        print("  (avbryter — testets egen SHA-jämförelse är trasig)")
        return 1
    print()

    if args.wait:
        if args.expect_sha:
            print(f"Väntar upp till {args.wait}s på att {args.expect_sha[:7]} går live…")
        else:
            print(f"Väntar upp till {args.wait}s på att appen svarar…")
        if not wait_for_health(base, args.wait, args.expect_sha):
            # Not fatal on its own: the checks below say *what* is wrong with
            # whatever is serving, and the SHA check names the build. Bailing
            # here would report a timeout and hide a broken deploy behind it.
            print("  (tidsgränsen nåddes — testar det som faktiskt kör)")

    # ---- 1. Drift -----------------------------------------------------------
    print("1. Drift")
    status, health = get_json(base, "/api/health")
    if not check("hälsokontroll svarar 200", status == 200,
                 f"{base} svarar inte alls" if status == 0 else f"fick {status}"):
        return 1  # nothing else can be trusted
    total_db = health.get("tenders_total", 0)
    check("databasen har data", total_db > 1000, f"{total_db} upphandlingar")
    last = (health.get("last_sync") or {})
    check("senaste synk lyckades", last.get("status") == "ok",
          f"{last.get('source')} @ {last.get('run_at')} ({last.get('status')})")

    # ---- 2. Rätt kodversion är ute -----------------------------------------
    # A failed build can leave the old image running while the site looks fine.
    print("\n2. Deployad kodversion")
    # The image is stamped with the commit it was built from, so this is a
    # fact rather than an inference. Without it a failed build leaves the old
    # image serving happily and every other check still passes.
    build = health.get("build") or {}
    running = build.get("commit") or ""
    if check("bygget rapporterar en commit", bool(running) and running != "unknown",
             f"build={build or 'saknas'}"):
        print(f"        kör {build.get('version')}+{build.get('commit_short')} "
              f"(byggd {build.get('built_at')})")
    if args.expect_sha:
        want = args.expect_sha.strip()
        check("den deployade committen är den som kör",
              sha_matches(running, want),
              f"väntade {want[:7]}, kör {running[:7] or 'ingen commit alls'}")

    status, spec = get_json(base, "/openapi.json")
    if check("openapi.json tillgänglig", status == 200 and spec is not None, f"fick {status}"):
        try:
            params = {p["name"] for p in spec["paths"]["/api/tenders"]["get"]["parameters"]}
            missing = EXPECTED_TENDER_PARAMS - params
            check("/api/tenders exponerar väntade parametrar",
                  not missing, f"saknas: {sorted(missing)}" if missing else f"{len(params)} st")
        except Exception as e:
            check("/api/tenders finns i schemat", False, str(e))

    # ---- 3. Filtren filtrerar faktiskt --------------------------------------
    # The cpv bug: a filter that returns the unfiltered total is broken, and
    # nothing errors. Compare every filter against the baseline.
    print("\n3. Filtren filtrerar (inte noll, inte allt)")
    status, base_page = get_json(base, "/api/tenders?page_size=1")
    baseline = (base_page or {}).get("total", 0)
    check("ofiltrerad lista svarar", status == 200 and baseline > 0, f"{baseline} träffar")

    for path, label in [
        ("q=konsult", "fritext q"),
        ("source=mercell", "källa"),
        ("source=ted_awards", "tilldelningar"),
        ("authority=Trafikverket", "upphandlare"),
        ("cpv=72", "CPV 72 (IT)"),
        ("cpv=45", "CPV 45 (bygg)"),
    ]:
        st, d = get_json(base, f"/api/tenders?{path}&page_size=1")
        n = (d or {}).get("total", -1)
        if st != 200 or n < 0:
            check(f"filter {label}", False, f"HTTP {st}")
        elif n == 0:
            check(f"filter {label}", False, "0 träffar — matchar inget")
        elif baseline and n == baseline:
            check(f"filter {label}", False, f"{n} = hela totalen — filtret ignoreras")
        else:
            check(f"filter {label}", True, f"{n} träffar")

    # A filter that cannot match must return zero — proves it is applied at all.
    st, d = get_json(base, "/api/tenders?cpv=999999&page_size=1")
    check("orimligt filter ger 0", st == 200 and (d or {}).get("total") == 0,
          f"{(d or {}).get('total')}")

    # ---- 4. Sidorna renderar ------------------------------------------------
    # Includes a closed tender: that branch 500'd for weeks because normal
    # browsing never reaches it.
    print("\n4. Sidor renderar")
    for path in ["/", "/browse", "/dashboard", "/agenter", "/blogg", "/analytics",
                 "/providers", "/kunskap", "/robots.txt", "/llms.txt", "/sitemap.xml"]:
        st, _, _ = get(base, path)
        check(f"GET {path}", st == 200, f"HTTP {st}")

    st, d = get_json(base, "/api/tenders?status=all&sort=oldest&page_size=1")
    st2, closed = get_json(base, "/api/tenders?source=ted_awards&page_size=1")
    items = (closed or {}).get("items") or []
    if items:
        tid = items[0]["id"]
        st3, _, _ = get(base, f"/tenders/{tid}")
        check(f"detaljsida för avslutad upphandling (#{tid})", st3 == 200, f"HTTP {st3}")

    # ---- 5. Skrivytan är stängd --------------------------------------------
    # Careful: probing these unauthenticated is only safe if the instance
    # actually requires a key. On an instance with ADMIN_API_KEY unset the
    # mutating endpoints are open by design — POSTing to /api/sync or
    # /api/reset-ted there would really start a sync or wipe the TED tables.
    # So we first ask /api/admin/query, which fails CLOSED (403) when no key is
    # configured, and only probe the destructive ones once a key is proven.
    print("\n5. Skrivendpoints kräver nyckel")
    st, body = post_json(base, "/api/admin/query", {"sql": "SELECT 1"})
    key_configured = st == 401
    check("POST /api/admin/query utan nyckel avvisas", st in (401, 403), f"HTTP {st}")

    if key_configured:
        for path in ["/api/sync", "/api/reset-ted", "/api/repair-links", "/api/blog"]:
            st, _ = post_json(base, path, {})
            check(f"POST {path} utan nyckel avvisas", st in (401, 403), f"HTTP {st}")
    else:
        print("  SKIP  destruktiva endpoints — ingen ADMIN_API_KEY är satt på den här")
        print("        instansen, så de är öppna med flit (lokal dev). Att sonda dem")
        print("        skulle starta en riktig sync/rensning. Sätt nyckeln för att testa.")

    # ---- 6. MCP-ytan levererar ---------------------------------------------
    # For connected agents this IS the product; an empty tool result is an
    # outage they'd hit silently.
    print("\n6. MCP-ytan")
    st, info = get_json(base, "/mcp")
    tools_count = (info or {}).get("tools_count", 0)
    check("GET /mcp svarar", st == 200, f"HTTP {st}")
    check("verktyg exponeras", tools_count >= 10, f"{tools_count} verktyg")

    def rpc(payload, session=None):
        req = urllib.request.Request(base + "/mcp", data=json.dumps(payload).encode(),
                                     method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("User-Agent", UA)
        if session:
            req.add_header("Mcp-Session-Id", session)
        r = urllib.request.urlopen(req, timeout=30)
        body = r.read().decode()
        sid = r.headers.get("Mcp-Session-Id")
        for line in body.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip()), sid
        return json.loads(body), sid

    try:
        init, sess = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                     "clientInfo": {"name": "smoke", "version": "1"}}})
        check("MCP initialize", bool(init.get("result", {}).get("serverInfo")),
              str(init.get("result", {}).get("serverInfo")))
        listed, _ = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sess)
        n_tools = len(listed.get("result", {}).get("tools", []))
        check("tools/list", n_tools >= 10, f"{n_tools} verktyg")

        # Each tool must return actual content, not an empty string.
        for tool, tool_args in [
            ("get_stats", {}),
            ("search_tenders", {"query": "IT", "limit": 1}),
            ("get_winner_history", {"cpv": "45", "top": 1}),
            ("deadline_calendar", {"days": 30, "limit": 1}),
        ]:
            res, _ = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": tool, "arguments": tool_args}}, sess)
            text = (res.get("result", {}).get("content") or [{}])[0].get("text", "")
            check(f"tools/call {tool}", len(text) > 30, f"{len(text)} tecken")
    except Exception as e:
        check("MCP-anrop", False, f"{type(e).__name__}: {e}")

    # ---- 7. Bloggen lovar inget vi inte har ---------------------------------
    # A post once told readers to "skapa en profil på Agentanbud så matchas du
    # mot öppna upphandlingar" — there is no profile and no login; not needing
    # one is the point. An agent had written what most procurement services
    # offer, and it contradicted the product rather than merely being wrong.
    # These patterns are imperatives aimed at the reader. Describing what OTHER
    # platforms require ("flera kräver konto hos upphandlingsverktyget") is
    # accurate and must not trip this, which is why "kräver konto" is absent
    # and the create/register verbs carry their object.
    print("\n7. Bloggen lovar inget vi inte har")
    FORBIDDEN = [
        (r"skapa\s+(?:ett\s+|en\s+)?(?:konto|profil|inloggning)", "uppmanar till konto/profil"),
        (r"registrera\s+dig\b", "uppmanar till registrering"),
        (r"prenumerera\b", "lovar prenumeration"),
        (r"logga\s+in\s+(?:på|hos)\s+(?:agentanbud|oss)", "lovar inloggning"),
        (r"mejlbevakning|e-postbevakning|bevakningstjänst", "lovar bevakningstjänst"),
    ]
    status, body, _ = get(base, "/blogg")
    slugs = sorted(set(re.findall(r'href="/blogg/([^"/]+)"', body.decode("utf-8", "replace"))))
    if check("bloggen listar inlägg", bool(slugs), f"{len(slugs)} st"):
        offenders = []
        for slug in slugs:
            st, pb, _ = get(base, f"/blogg/{slug}")
            if st != 200:
                offenders.append(f"{slug[:28]}: HTTP {st}")
                continue
            # Only the post's own text. The page chrome legitimately says
            # "behöver du skapa konto hos plattformen (Mercell, Tendsign)",
            # which is true of those platforms and not a promise about us —
            # scoping to <article> is what separates the two.
            html = pb.decode("utf-8", "replace")
            art = re.search(r"<article[^>]*class=\"blog-article\".*?</article>", html, re.S)
            if not art:
                offenders.append(f"{slug[:28]}: hittade ingen artikeltext")
                continue
            text = re.sub(r"<[^>]+>", " ", art.group(0)).lower()
            for pat, label in FORBIDDEN:
                if re.search(pat, text):
                    offenders.append(f"{slug[:28]}: {label}")
        check("inga påhittade funktioner i publicerade inlägg",
              not offenders, "; ".join(offenders) if offenders else f"{len(slugs)} inlägg rena")

    # ---- 8. Säkerhetsheaders ------------------------------------------------
    print("\n8. Säkerhetsheaders")
    _, _, headers = get(base, "/")
    lower = {k.lower(): v for k, v in headers.items()}
    for h in ["content-security-policy", "x-frame-options",
              "x-content-type-options", "strict-transport-security"]:
        check(f"{h}", h in lower, lower.get(h, "saknas")[:40])

    # ---- Summering ----------------------------------------------------------
    print("\n" + "─" * 60)
    if failures:
        print(f"❌ {len(failures)} av {checks_run} kontroller MISSLYCKADES:")
        for f in failures:
            print(f"   • {f}")
        return 1
    print(f"✅ Alla {checks_run} kontroller passerade")
    return 0


if __name__ == "__main__":
    sys.exit(main())
