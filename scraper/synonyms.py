"""
Each night: the week's searches that found nothing, the words Swedish notices
use instead, suggested by a language model and checked by app/learned.py.

Configured entirely by the environment, and off without it:

  LLM_BASE_URL       an OpenAI-compatible API, e.g. https://host/v1 (or the
                     full .../chat/completions URL)
  LLM_API_KEY        sent as "Authorization: Bearer …"; never logged
  LLM_MODEL          the model name the API expects
  LLM_SYNONYMS_MODE  "auto" (default): a suggestion that passes the checks in
                     app/learned.py is searched with at once; an agent reviews
                     the list now and then (MCP list_synonyms/review_synonym,
                     or /ops/synonymer) and rejects what is too broad.
                     "suggest": store suggestions, searched with only once
                     someone approves them.
  LLM_SYNONYMS_PER_NIGHT  how many searches to ask about (default 15)

What is sent: the search word (anonymous — no visitor, no address, no time)
and the names of a few CPV codes that look like it. Nothing else.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

import httpx

from app import learned
from app.db import connect, init_db, log_sync

LOG = logging.getLogger(__name__)
SOURCE = "synonyms"

PROMPT = (
    "Du hjälper en sökmotor för svenska offentliga upphandlingar. Någon sökte på ett ord "
    "som inte gav några träffar, men saken upphandlas troligen under ett annat namn. "
    "Föreslå högst {n} ord eller korta fraser som en svensk upphandlare skulle skriva i "
    "annonsens titel eller beskrivning för samma sak. Inga bredare kategorier, inga "
    "förkortningar under fem bokstäver, inget som också betyder något annat. Ange också "
    "den CPV-huvudgrupp (två siffror) som saken hör till.\n\n"
    "Sökord: {term}\n"
    "CPV-namn som liknar ordet: {cpv}\n\n"
    'Svara bara med JSON: {{"cpv_division": "44", "alternatives": ["ord", "ord"]}}. '
    'Om du inte vet: {{"cpv_division": "", "alternatives": []}}.'
)


def _config() -> Optional[dict]:
    base, key, model = (os.environ.get(k, "").strip() for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))
    if not (base and key and model):
        return None
    url = base.rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"
    mode = os.environ.get("LLM_SYNONYMS_MODE", "auto").strip().lower()
    return {"url": url, "key": key, "model": model, "mode": "suggest" if mode == "suggest" else "auto",
            "per_night": int(os.environ.get("LLM_SYNONYMS_PER_NIGHT", "15") or 15)}


def _cpv_hints(term: str, limit: int = 8) -> list[str]:
    from app.search import _cpv_labels
    stem = term.casefold()[:5]
    if len(stem) < 4:
        return []
    out = [l for l in _cpv_labels().values() if stem in l.casefold()]
    return out[:limit]


def ask(client: httpx.Client, cfg: dict, term: str) -> tuple[str, list[str]]:
    """(cpv division, alternatives) from the model, or ("", []) on any trouble."""
    body = {"model": cfg["model"], "temperature": 0,
            "messages": [{"role": "user", "content": PROMPT.format(
                n=learned.MAX_PER_TERM, term=term, cpv="; ".join(_cpv_hints(term)) or "inga")}]}
    resp = client.post(cfg["url"], json=body, headers={"Authorization": f"Bearer {cfg['key']}"})
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"] or ""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return "", []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return "", []
    alts = [str(a) for a in (data.get("alternatives") or []) if isinstance(a, str)]
    return str(data.get("cpv_division") or ""), alts[: learned.MAX_PER_TERM]


def _unmet(conn, limit: int) -> list[str]:
    """This week's searches with nothing found, most asked first, skipping
    those already handled — in app/query_terms.py or asked about before."""
    from app.insights import usage_summary
    from app.query_terms import TERMS
    static = {" ".join(k.casefold().split()) for k in TERMS}
    asked = {r[0] for r in conn.execute("SELECT DISTINCT term FROM learned_terms")}
    out = []
    for u in usage_summary(conn, days=7, top=limit * 3).get("unmet_demand", []):
        t = " ".join((u["term"] or "").casefold().split())
        if t and t not in static and t not in asked:
            out.append(t)
        if len(out) >= limit:
            break
    return out


def run(db_path: str, client: Optional[httpx.Client] = None) -> int:
    init_db(db_path)
    conn = connect(db_path)
    cfg = _config()
    try:
        if not cfg:
            log_sync(conn, SOURCE, "ok", 0, "av: LLM_BASE_URL, LLM_API_KEY och LLM_MODEL är inte satta")
            return 0
        own = client is None
        client = client or httpx.Client(timeout=httpx.Timeout(60.0))
        status = "active" if cfg["mode"] == "auto" else "suggested"
        woke = learned.wake(conn, status)
        terms = _unmet(conn, cfg["per_night"])
        kept = rejected = failed = dormant = 0
        try:
            for term in terms:
                try:
                    division, alts = ask(client, cfg, term)
                except Exception as exc:
                    # Never the key: only the kind of error and the status code.
                    code = getattr(getattr(exc, "response", None), "status_code", "")
                    LOG.warning("synonym model failed for a term: %s %s", type(exc).__name__, code)
                    failed += 1
                    continue
                if not alts:
                    learned.store(conn, term, "", "rejected", 0, "inga förslag", cfg["model"])
                    rejected += 1
                for alt in alts:
                    ok, hits, why = learned.check(conn, term, alt, division)
                    got = learned.outcome(ok, why, status)
                    learned.store(conn, term, alt, got, hits, f"{why}; CPV {division or '?'}", cfg["model"])
                    kept += ok
                    dormant += got == "dormant"
                    rejected += got == "rejected"
                conn.commit()
        finally:
            if own:
                client.close()
        learned.reload(db_path)
        log_sync(conn, SOURCE, "ok" if not failed or kept else "error", kept + woke,
                 f"{len(terms)} sökord, {kept} synonymer {'aktiva' if status == 'active' else 'föreslagna'}, "
                 f"{dormant} vilande, {rejected} avvisade, {woke} vaknade, {failed} fel mot modellen")
        return kept
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level="INFO")
    print(run(os.environ.get("DB_PATH", "/data/application.db")))
