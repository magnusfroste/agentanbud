"""FastAPI app — multi-page dashboard + JSON API over the SQLite store."""
from __future__ import annotations

import hmac
import json
import logging
import math
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Set up logging early — used by the mcp_http import try/except below
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
LOG = logging.getLogger(__name__)

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

from .cron import get_schedule, next_run
from .db import connect, init_db, log_usage
from .insights import (SEGMENT_OTHER, looks_like_bot as _looks_like_bot,
                       segment_for as _segment_for, usage_summary)

# MCP-over-HTTP is optional — only loaded if mcp_http module is present
# (it's a separate file so the stdio MCP server stays independent).
# Catch Exception, not just ImportError: an incompatible mcp release makes
# mcp_server raise AttributeError at import, and that must not take the whole
# site down — the /mcp endpoint is meant to be optional.
try:
    from mcp_http import mcp_router
    MCP_HTTP_AVAILABLE = True
except Exception as exc:
    MCP_HTTP_AVAILABLE = False
    LOG.warning("mcp_http not importable (%s: %s), /mcp endpoint disabled",
                type(exc).__name__, exc)

DB_PATH = os.environ.get("DB_PATH", "/data/application.db")
# Public origin, used for canonical URLs, OG tags, sitemap and robots.txt.
# Override when running on another domain so those never point at the wrong host.
SITE_URL = os.environ.get("SITE_URL", "https://www.agentanbud.se").rstrip("/")
TEMPLATE_DIR = Path(__file__).parent.parent / "web" / "templates"
STATIC_DIR = Path(__file__).parent.parent / "web" / "static"

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200

# Admin auth — when set, mutating endpoints require the X-Admin-Key header
# and /api/admin/query becomes available. When unset (local dev), mutating
# endpoints stay open and the query endpoint is disabled.
ADMIN_API_KEY = os.environ.get("ADMIN_API_KEY", "")
ADMIN_QUERY_MAX_ROWS = 500

# Sync state — single concurrent run only
_sync_lock = threading.Lock()
_sync_running = False


def _require_admin(request: Request) -> None:
    if not ADMIN_API_KEY:
        return
    supplied = request.headers.get("x-admin-key", "")
    if not hmac.compare_digest(supplied, ADMIN_API_KEY):
        raise HTTPException(status_code=401, detail="missing or invalid X-Admin-Key")


def _log_usage_safe(conn, channel: str, action: str, query: Optional[str] = None, meta: Optional[dict] = None) -> None:
    """Fire-and-forget usage logging — never let analytics break a request."""
    try:
        log_usage(conn, channel, action, query=query, meta=meta)
    except Exception:
        LOG.exception("log_usage failed (channel=%s action=%s)", channel, action)


def _num(n) -> str:
    """Format int with thin-space thousands separator (Swedish style)."""
    return f"{int(n):,}".replace(",", " ")


import re as _re

def _parse_days_until(deadline: str | None, now: datetime) -> int | None:
    """Parse a deadline string into days remaining, robust to TED's date formats.

    TED returns dates like '2026-09-18+02:00' (date + offset, no time),
    '2026-06-04Z' (date + Z), or '2026-09-18T23:59:00Z'.
    Standard datetime.fromisoformat() chokes on the first two in Python < 3.11.
    """
    if not deadline:
        return None
    s = str(deadline).strip()[:25]
    # Normalise: strip the timezone offset to parse as naive date, then
    # compare date-only (we don't need hour-level precision for "Xd kvar")
    m = _re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if not m:
        return None
    try:
        d = datetime.strptime(m.group(1), "%Y-%m-%d")
        return (d - now.replace(tzinfo=None)).days
    except Exception:
        return None


def create_app(db_path: Optional[str] = None) -> FastAPI:
    db = db_path or DB_PATH
    try:
        init_db(db)
    except Exception as exc:
        LOG.warning("init_db failed: %s", exc)

    app = FastAPI(title="Agentanbud", version="0.2.0")
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)), autoescape=True)
    env.filters["format_num"] = _num
    env.globals["SITE_URL"] = SITE_URL

    def render(template: str, **ctx) -> str:
        tpl = env.get_template(template)
        return tpl.render(request=ctx.pop("request", None), **ctx)

    # ---- Static ----
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # ---- Pageview logging (visits) ----
    # Counts every page load so /analytics can show reach — how many visits,
    # and how many are human vs automated (bots/crawlers/agents). We store
    # ONLY a coarse bot flag derived from the User-Agent, never the UA string
    # or IP, so this stays cookie-free and non-identifying.
    _PAGEVIEW_SKIP_PREFIXES = ("/static", "/api", "/mcp", "/openapi", "/docs", "/redoc", "/favicon")

    @app.middleware("http")
    async def _log_pageviews(request: Request, call_next):
        response = await call_next(request)
        try:
            path = request.url.path
            if (request.method == "GET"
                    and response.status_code < 400
                    and not path.startswith(_PAGEVIEW_SKIP_PREFIXES)):
                is_bot = _looks_like_bot(request.headers.get("user-agent", ""))
                conn = connect(db)
                try:
                    _log_usage_safe(conn, "browser", "view", query=path,
                                     meta={"bot": is_bot})
                finally:
                    conn.close()
        except Exception:
            LOG.exception("pageview logging failed")
        return response

    # ---- MCP over HTTP (Streamable HTTP transport) ----
    # Lets remote clients (Claude Code, Cursor, Windsurf, future MCP-aware
    # web assistants) connect with just a URL — no local install.
    if MCP_HTTP_AVAILABLE:
        app.include_router(mcp_router)
        LOG.info("MCP HTTP endpoint mounted at /mcp")
    else:
        LOG.warning("MCP HTTP endpoint NOT mounted (mcp_http module missing)")

    # ---- Pages ----
    @app.get("/", include_in_schema=False)
    def landing(request: Request):
        """Public landing page — first impression for new visitors."""
        conn = connect(db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
            open_count = conn.execute(
                "SELECT COUNT(*) FROM tenders WHERE deadline IS NULL OR deadline > ?",
                (datetime.now().isoformat(timespec="seconds"),),
            ).fetchone()[0]
            sources = conn.execute(
                "SELECT source_system, COUNT(*) AS n FROM tenders GROUP BY source_system"
            ).fetchall()
            regions = conn.execute(
                "SELECT COUNT(DISTINCT region) AS n FROM tenders WHERE region IS NOT NULL AND region != ''"
            ).fetchone()
            region_count = regions["n"] if regions else 0
            # Latest 6 open tenders with deadlines
            rows = conn.execute(
                """
                SELECT id, source_system, title, authority, region, deadline,
                       published_at, value, cpv_codes
                FROM tenders
                WHERE deadline IS NULL OR deadline > ?
                ORDER BY published_at DESC NULLS LAST, id DESC
                LIMIT 6
                """,
                (datetime.now().isoformat(timespec="seconds"),),
            ).fetchall()
            recent = []
            now = datetime.now()
            for r in rows:
                t = dict(r)
                # parse cpv_codes
                if t.get("cpv_codes"):
                    try:
                        t["cpv_codes"] = json.loads(t["cpv_codes"])
                    except Exception:
                        t["cpv_codes"] = []
                # days_until
                t["days_until"] = _parse_days_until(t.get("deadline"), now)
                recent.append(t)
            # Top 5 authorities for mini chart
            top_auth = conn.execute(
                "SELECT authority, COUNT(*) AS n FROM tenders "
                "WHERE authority IS NOT NULL AND authority != '' "
                "GROUP BY authority ORDER BY n DESC LIMIT 5"
            ).fetchall()
            max_n = top_auth[0]["n"] if top_auth else 1
            top_authorities = [
                {"authority": r["authority"], "n": r["n"], "pct": int(r["n"] / max_n * 100)}
                for r in top_auth
            ]

            return HTMLResponse(render("landing.html",
                total=total,
                open_count=open_count,
                source_count=len(sources),
                region_count=region_count,
                recent_tenders=recent,
                top_authorities=top_authorities,
            ))
        finally:
            conn.close()

    @app.get("/dashboard", include_in_schema=False)
    def dashboard(request: Request):
        conn = connect(db)
        try:
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat(timespec="seconds")

            # Basic counts
            total = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
            open_count = conn.execute(
                "SELECT COUNT(*) FROM tenders WHERE deadline IS NULL OR deadline > ?",
                (now_iso,),
            ).fetchone()[0]

            # Total value
            total_value = conn.execute(
                "SELECT COALESCE(SUM(value), 0) FROM tenders WHERE value IS NOT NULL AND value > 0"
            ).fetchone()[0] or 0

            # Biggest tender
            biggest_row = conn.execute(
                "SELECT value, authority, title FROM tenders WHERE value IS NOT NULL AND value > 0 ORDER BY value DESC LIMIT 1"
            ).fetchone()
            biggest = dict(biggest_row) if biggest_row else None

            # Closing soon (nearest deadline in the future)
            closing_row = conn.execute(
                "SELECT title, deadline FROM tenders WHERE deadline > ? ORDER BY deadline ASC LIMIT 1",
                (now_iso,),
            ).fetchone()
            closing_soon = None
            if closing_row:
                c = dict(closing_row)
                c["days"] = max(0, _parse_days_until(c.get("deadline"), now) or 0)
                closing_soon = c

            # Authority count
            authority_count = conn.execute(
                "SELECT COUNT(DISTINCT authority) FROM tenders WHERE authority IS NOT NULL AND authority != ''"
            ).fetchone()[0]

            # Top 10 authorities with percentage
            top_auth_rows = conn.execute(
                "SELECT authority, COUNT(*) AS n FROM tenders "
                "WHERE authority IS NOT NULL AND authority != '' "
                "GROUP BY authority ORDER BY n DESC LIMIT 10"
            ).fetchall()
            max_n = top_auth_rows[0]["n"] if top_auth_rows else 1
            top_authorities = [
                {"authority": r["authority"], "n": r["n"], "pct": int(r["n"] / max_n * 100)}
                for r in top_auth_rows
            ]

            # CPV top categories (first 2 digits = division)
            cpv_rows = conn.execute(
                "SELECT cpv_codes FROM tenders WHERE cpv_codes IS NOT NULL AND cpv_codes != ''"
            ).fetchall()
            from collections import Counter
            cpv_counter = Counter()
            cpv_names = {
                "45": "Bygg", "71": "Ingenjörstjänster", "72": "IT", "73": "Forskning",
                "48": "Mjukvara", "50": "Reparation", "51": "Transport (rörlig)",
                "55": "Hotell/restaurang", "60": "Transport", "63": "Resor",
                "64": "Post/telecom", "66": "Finansiella tjänster", "79": "Affärstjänster",
                "80": "Utbildning", "85": "Hälso- och sjukvård", "90": "Miljö/sanering",
                "92": "Fritid/kultur", "03": "Jordbruk", "09": "Petroleum",
                "15": "Livsmedel", "18": "Kläder", "19": "Bränsle",
                "22": "Trycksaker", "24": "Kemikalier", "30": "Kontor",
                "31": "Möbler", "32": "Elektronik", "33": "Medicinsk utrustning",
                "34": "Transportmedel", "35": "Säkerhet", "37": "Ljud/ljus",
                "38": "Mätinstrument", "39": "Maskiner", "41": "Vatten",
                "42": "Industriella maskiner", "43": "Anläggningsmaskiner",
                "44": "Byggmaterial", "46": "Maskiner (industri)",
                "47": "Petroleumprodukter", "49": "Kläder/skydd",
                "52": "Engineering", "53": "Militär utrustning",
                "54": "Finansiella system", "56": "Kundtjänst",
                "57": "IT-tjänster", "58": "Publicering",
                "59": "Radio/TV", "61": "Telekom",
                "62": "Mjukvarutjänster", "65": "Försäkring",
                "67": "Affärstjänster (finansiella)", "68": "Fastigheter",
                "69": "Juridiska tjänster", "70": "Fastighetstjänster",
                "74": "Standardisering", "75": "Distribution",
                "76": "Relaterade tjänster", "77": "Miljöteknik",
                "78": "Personal", "81": "Facility management",
                "82": "Administrativa tjänster", "83": "Offentlig förvaltning",
                "84": "Försvar", "86": "Sjukhusutrustning",
                "87": "Skönhetsvård", "88": "Socialtjänst",
                "91": "Religiösa tjänster", "93": "Sport",
                "94": "Rekreation", "95": "Familjetjänster",
                "96": "Social skydd", "98": "Övrigt",
            }
            colors = ["#2563eb", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444", "#06b6d4", "#ec4899", "#84cc16"]
            for r in cpv_rows:
                try:
                    cpvs = json.loads(r["cpv_codes"])
                    for c in cpvs:
                        prefix = str(c)[:2]
                        cpv_counter[prefix] += 1
                except Exception:
                    pass
            cpv_total = sum(cpv_counter.values()) or 1
            cpv_top = []
            for i, (prefix, n) in enumerate(cpv_counter.most_common(8)):
                cpv_top.append({
                    "code": prefix,
                    "name": cpv_names.get(prefix, f"CPV {prefix}"),
                    "n": n,
                    "pct": int(n / cpv_total * 100),
                    "color": colors[i % len(colors)],
                })

            # Deadline weekday distribution
            weekday_names = ["Mån", "Tis", "Ons", "Tor", "Fre", "Lör", "Sön"]
            weekday_counts = [0] * 7
            dl_rows = conn.execute(
                "SELECT deadline FROM tenders WHERE deadline IS NOT NULL AND deadline != ''"
            ).fetchall()
            for r in dl_rows:
                d = _parse_days_until(str(r["deadline"]), now)
                if d is not None:
                    # Parse the actual date to get weekday
                    m = _re.match(r"(\d{4}-\d{2}-\d{2})", str(r["deadline"]))
                    if m:
                        try:
                            dt = datetime.strptime(m.group(1), "%Y-%m-%d")
                            weekday_counts[dt.weekday()] += 1
                        except Exception:
                            pass
            max_wd = max(weekday_counts) or 1
            deadline_weekday = [
                {"day": weekday_names[i], "n": weekday_counts[i], "pct": int(weekday_counts[i] / max_wd * 100)}
                for i in range(7)
            ]

            # Recent tenders (5)
            recent_rows = conn.execute(
                "SELECT id, source_system, title, authority, deadline FROM tenders "
                "ORDER BY published_at DESC LIMIT 5"
            ).fetchall()
            recent_tenders = []
            for r in recent_rows:
                t = dict(r)
                t["days_until"] = _parse_days_until(t.get("deadline"), now)
                recent_tenders.append(t)

            # Sync logs
            recent_syncs = [dict(r) for r in conn.execute(
                "SELECT source, run_at, count, status FROM sync_log ORDER BY run_at DESC LIMIT 10"
            ).fetchall()]

            nr = next_run()

            def format_money(v):
                if v >= 1_000_000_000:
                    return f"{v/1_000_000_000:.1f} mdr SEK"
                elif v >= 1_000_000:
                    return f"{v/1_000_000:.0f} mln SEK"
                elif v >= 1_000:
                    return f"{v/1_000:.0f}k SEK"
                return f"{v:.0f} SEK"

            return HTMLResponse(render("dashboard.html",
                total=total, open_count=open_count, total_value=total_value,
                biggest=biggest, closing_soon=closing_soon, authority_count=authority_count,
                top_authorities=top_authorities, cpv_top=cpv_top,
                deadline_weekday=deadline_weekday, recent_tenders=recent_tenders,
                recent_syncs=recent_syncs, schedule=get_schedule(),
                next_run_iso=nr.strftime("%Y-%m-%d %H:%M UTC") if nr else "—",
                format_money=format_money,
            ))
        finally:
            conn.close()

    @app.get("/browse", include_in_schema=False)
    def browse(
        request: Request,
        q: str = "",
        source: str = "",
        authority: str = "",
        cpv: str = "",
        status: str = "open",
        sort: str = "deadline",
        page: int = 1,
    ):
        page = max(1, page)
        conn = connect(db)
        try:
            where = []
            args: list = []
            if source:
                where.append("source_system = ?")
                args.append(source)
            if authority:
                where.append("authority LIKE ?")
                args.append(f"%{authority}%")
            if cpv:
                where.append("cpv_codes LIKE ?")
                args.append(f'%"{cpv}')
            if q:
                where.append("(title LIKE ? OR description LIKE ?)")
                args.extend([f"%{q}%", f"%{q}%"])

            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if status == "open":
                where.append("(deadline IS NULL OR deadline > ?)")
                args.append(now_iso)
            elif status == "closing":
                where.append("deadline > ?")
                args.append(now_iso)
                from datetime import timedelta
                soon = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec="seconds")
                where.append("deadline <= ?")
                args.append(soon)

            where_sql = ("WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute(
                f"SELECT COUNT(*) FROM tenders {where_sql}", args
            ).fetchone()[0]

            if page == 1 and (q or source or authority or cpv):
                _log_usage_safe(conn, "browser", "search", query=q or None,
                                 meta={"source": source, "authority": authority,
                                       "cpv": cpv, "status": status,
                                       "segment": _segment_for(q, cpv), "results": total})

            sort_map = {
                "deadline": "CASE WHEN deadline IS NULL THEN 1 ELSE 0 END, deadline ASC",
                "newest": "published_at DESC",
                "value": "value DESC NULLS LAST",
                "title": "title ASC",
            }
            order_by = sort_map.get(sort, sort_map["deadline"])

            page_size = 20
            pages = max(1, (total + page_size - 1) // page_size)
            offset = (page - 1) * page_size

            rows = conn.execute(
                f"""
                SELECT id, source_system, title, authority, region, deadline,
                       published_at, value, cpv_codes, procedure, tender_url,
                       document_type
                FROM tenders {where_sql}
                ORDER BY {order_by}
                LIMIT ? OFFSET ?
                """,
                args + [page_size, offset],
            ).fetchall()
            items = [dict(r) for r in rows]
            now = datetime.now(timezone.utc)
            for item in items:
                if item.get("cpv_codes"):
                    try:
                        item["cpv_codes_list"] = json.loads(item["cpv_codes"])
                    except Exception:
                        item["cpv_codes_list"] = []
                item["days_until"] = _parse_days_until(item.get("deadline"), now)

            sources = conn.execute(
                "SELECT source_system, COUNT(*) as count FROM tenders GROUP BY source_system ORDER BY count DESC"
            ).fetchall()

            from urllib.parse import urlencode
            qs_base = {k: v for k, v in {"q": q, "source": source, "authority": authority,
                                        "cpv": cpv, "status": status, "sort": sort}.items() if v}
            qs_prev = urlencode({**qs_base, "page": page - 1})
            qs_next = urlencode({**qs_base, "page": page + 1})

            return HTMLResponse(render("browse.html", q=q, source=source,
                                       authority=authority, cpv=cpv, status=status, sort=sort,
                                       total=total, tenders=items, page=page, pages=pages,
                                       sources=[dict(r) for r in sources],
                                       qs_prev=qs_prev, qs_next=qs_next))
        finally:
            conn.close()


    @app.get("/tenders/{tid}", include_in_schema=False)
    def tender_detail(tid: int, request: Request):
        conn = connect(db)
        try:
            row = conn.execute("SELECT * FROM tenders WHERE id = ?", (tid,)).fetchone()
            if not row:
                raise HTTPException(404, "tender not found")
            d = dict(row)
            # Parse cpv_codes
            if d.get("cpv_codes"):
                try:
                    d["cpv_codes_list"] = json.loads(d["cpv_codes"])
                except Exception:
                    d["cpv_codes_list"] = []
            else:
                d["cpv_codes_list"] = []
            # Days until deadline
            d["days_until"] = _parse_days_until(d.get("deadline"), datetime.now(timezone.utc))
            # Pretty raw_json — raw may be a JSON string OR already-parsed dict/list
            # (depends on driver). Handle both. Fall back to raw on any error.
            raw = d.get("raw_json", "")
            try:
                if isinstance(raw, (dict, list)):
                    d["raw_json_pretty"] = json.dumps(raw, indent=2, ensure_ascii=False)
                elif raw:
                    d["raw_json_pretty"] = json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
                else:
                    d["raw_json_pretty"] = ""
            except Exception:
                d["raw_json_pretty"] = str(raw) if raw else ""
            return HTMLResponse(render("detail.html", t=d))
        except HTTPException:
            raise
        except Exception as exc:
            LOG.exception("tender_detail crashed for id=%s", tid)
            return HTMLResponse(
                f"<h1>Internal Server Error</h1><p>Kunde inte visa tender #{tid}.</p><pre>{type(exc).__name__}: {exc}</pre>",
                status_code=500,
            )
        finally:
            conn.close()


    @app.get("/system", include_in_schema=False)
    def system(request: Request):
        conn = connect(db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
            syncs = [dict(r) for r in conn.execute(
                "SELECT source, run_at, count, status, message FROM sync_log ORDER BY run_at DESC LIMIT 20"
            ).fetchall()]
            sources = [dict(r) for r in conn.execute(
                "SELECT source_system, COUNT(*) as count, MAX((SELECT run_at FROM sync_log sl WHERE sl.source = t.source_system ORDER BY run_at DESC LIMIT 1)) as last_sync "
                "FROM tenders t GROUP BY source_system ORDER BY count DESC"
            ).fetchall()]
            nr = next_run()
            health = {
                "tenders_total": total,
                "cron_schedule": get_schedule(),
                "next_run": nr.strftime("%Y-%m-%d %H:%M UTC") if nr else "—",
            }
            return HTMLResponse(render("system.html", health=health, syncs=syncs, sources=sources))
        finally:
            conn.close()

    @app.get("/analytics", include_in_schema=False)
    def analytics(request: Request):
        """Usage insights — transparency page and sponsor deck in one.
        The figures come from app.insights so /api/analytics and the MCP
        get_usage_stats tool report exactly the same numbers."""
        conn = connect(db)
        try:
            s = usage_summary(conn, days=None)

            # Decorate the raw figures with presentation-only bits.
            colors = ["#2563eb", "#8b5cf6", "#10b981", "#f59e0b", "#ef4444",
                      "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#14b8a6",
                      "#a855f7", "#64748b"]
            top_seg = s["segments"][0]["n"] if s["segments"] else 1
            segments = [
                {**seg, "bar": int(seg["n"] / top_seg * 100), "color": colors[i % len(colors)]}
                for i, seg in enumerate(s["segments"])
            ]

            def bars(items, key="n"):
                mx = items[0][key] if items else 1
                return [{**it, "pct": int(it[key] / mx * 100)} for it in items]

            v = s["visits"]
            kpis = {"views": v["total"], "human_views": v["human"], "bot_views": v["bot"],
                    "human_pct": v["human_pct"], "searches": s["searches"],
                    "agent_calls": s["agent_calls"],
                    "operator_calls": s["operator_calls"]}

            usage_types = [
                {"label": "Sidvisningar", "n": v["total"], "color": "#2563eb"},
                {"label": "Webbsökningar", "n": s["searches"], "color": "#8b5cf6"},
                {"label": "MCP-agentverktyg", "n": s["agent_calls"], "color": "#10b981"},
            ]
            ut_total = sum(u["n"] for u in usage_types) or 1
            usage_types = [{**u, "pct": int(u["n"] / ut_total * 100)}
                           for u in usage_types if u["n"] > 0]

            max_day = max((d["total"] for d in s["daily_last_30"]), default=0) or 1
            daily = [{"day": d["date"][8:10] + "/" + d["date"][5:7], "n": d["total"],
                      "views": d["views"], "searches": d["searches"],
                      "agents": d["agent_calls"],
                      "pct": int(d["total"] / max_day * 100)}
                     for d in s["daily_last_30"]]

            recent = [dict(r) for r in conn.execute(
                "SELECT channel, action, query, created_at FROM usage_log "
                "WHERE action != 'view' ORDER BY created_at DESC LIMIT 25"
            ).fetchall()]

            return HTMLResponse(render("analytics.html",
                total=s["events_total"], kpis=kpis, segments=segments,
                unmet=bars(s["unmet_demand"]), unclassified=bars(s["unclassified"]),
                other_share=s["other_share_pct"], top_terms=bars(s["top_terms"]),
                usage_types=usage_types,
                top_tools=bars([{"tool": t["tool"], "n": t["n"]} for t in s["mcp_tools"]]),
                daily=daily, recent=recent,
            ))
        finally:
            conn.close()

    @app.get("/api/analytics")
    def api_analytics(days: Optional[int] = Query(default=None, ge=1, le=365)):
        """Public usage figures as JSON — same numbers as /analytics.

        Exists so an operator agent (or any script) can report on traffic
        without scraping the HTML page. Aggregate only: no IP addresses,
        no visitor ids, nothing per-person. Free to republish.
        """
        conn = connect(db)
        try:
            return usage_summary(conn, days=days)
        finally:
            conn.close()


    @app.get("/agenter", include_in_schema=False)
    def agents(request: Request):
        return HTMLResponse(render("agents.html"))

    # ---- Crawler / answer-engine entry points --------------------------
    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        """Allow everything (this is open civic data) and point at the sitemap.

        AI crawlers are explicitly welcome: being cited by an answer engine
        is the whole point of publishing procurement data openly.
        """
        body = (
            "User-agent: *\n"
            "Allow: /\n"
            # Search/filter permutations are infinite and low-value to index;
            # the canonical tender pages carry the content.
            "Disallow: /api/\n"
            "Disallow: /mcp\n"
            "Disallow: /browse?\n"
            "\n"
            f"Sitemap: {SITE_URL}/sitemap.xml\n"
        )
        return Response(content=body, media_type="text/plain")

    @app.get("/llms.txt", include_in_schema=False)
    def llms_txt():
        """llms.txt — a plain-text brief for LLMs and agents that land here.

        Tells an agent what this site is and, crucially, that there's an MCP
        endpoint and a JSON API so it can stop scraping HTML and use those.
        """
        conn = connect(db)
        try:
            total = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
            knowledge = conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        except Exception:
            total = knowledge = 0
        finally:
            conn.close()
        body = f"""# Agentanbud

> Öppen spegel av svenska offentliga upphandlingar. Fri att läsa — inga
> konton, inga API-nycklar, inga betalväggar. {total} upphandlingar och
> {knowledge} kunskapsobjekt, synkade dagligen.

Agentanbud samlar upphandlingsdata från Mercell, TED EU (annonser,
tilldelningar och förhandsinformation) och Upphandlingsmyndigheten (LOV,
hållbarhetskriterier, juridisk Q&A) och gör den sökbar för både människor
och AI-agenter.

## För AI-agenter: använd MCP eller API:t, inte HTML-skrapning

- MCP (rekommenderat): {SITE_URL}/mcp — transport streamable-http, 11 verktyg
  (search_tenders, get_tender, match_profile, get_authority, get_stats,
  list_providers, list_regions, list_cpv_top, search_knowledge, get_knowledge,
  get_usage_stats).
- REST API: {SITE_URL}/api/tenders?q=&source=&authority=&cpv=&page=
  Enskild post: {SITE_URL}/api/tenders/{{id}}
  Statistik: {SITE_URL}/api/stats · OpenAPI: {SITE_URL}/openapi.json

## Är du satt som operator och ska rapportera om användningen?

Använd MCP-verktyget get_usage_stats(days=1) för ett dygn (days=7 för en
vecka), eller GET {SITE_URL}/api/analytics?days=1 utan MCP. Du får besök
(mänskliga vs botar), sökningar, agentanrop, efterfrågan per bransch, mest
sökta termer och sökningar utan träff. Siffrorna är aggregerade och
innehåller inga personuppgifter — fria att publicera och citera.

Alla GET-endpoints är öppna. Skrivåtgärder kräver adminnyckel.

## Sidor

- {SITE_URL}/browse — sök och filtrera upphandlingar
- {SITE_URL}/tenders/{{id}} — en upphandling med deadline, värde, CPV och länk till originalannonsen
- {SITE_URL}/kunskap — hållbarhetskriterier och juridisk Q&A (LOU, LOV, tröskelvärden)
- {SITE_URL}/dashboard — statistik över svensk upphandling
- {SITE_URL}/providers — datakällor och hur de hämtas
- {SITE_URL}/agenter — hur du kopplar in en agent
- {SITE_URL}/analytics — öppen användningsstatistik (inga cookies)

## Viktigt om data vs ansökan

Vi speglar publik data. Att faktiskt lämna anbud sker hos originalplattformen
och kräver ofta konto där (t.ex. Mercell). TED-annonser är helt publika.
Följ alltid tender_url till originalkällan för dokument och inlämning.

## Licens

Kod: MIT ({SITE_URL} · https://github.com/magnusfroste/agentanbud).
Data: respektive källas villkor gäller; vi länkar alltid till originalet.
"""
        return Response(content=body, media_type="text/plain; charset=utf-8")

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap():
        """Sitemap covering the static pages plus every tender and knowledge
        item. Without this the thousands of detail pages — the long-tail that
        actually brings in search traffic — are only reachable by crawling
        paginated /browse, which engines do slowly and incompletely."""
        from xml.sax.saxutils import escape

        conn = connect(db)
        try:
            static_pages = [
                ("/", "daily", "1.0"),
                ("/browse", "daily", "0.9"),
                ("/dashboard", "daily", "0.7"),
                ("/kunskap", "weekly", "0.7"),
                ("/agenter", "monthly", "0.8"),
                ("/providers", "monthly", "0.6"),
                ("/analytics", "weekly", "0.5"),
            ]
            parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                     '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
            for path, freq, prio in static_pages:
                parts.append(
                    f"<url><loc>{escape(SITE_URL + path)}</loc>"
                    f"<changefreq>{freq}</changefreq><priority>{prio}</priority></url>"
                )

            # Open tenders first (most valuable), then recent closed ones.
            now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
            rows = conn.execute(
                """
                SELECT id, fetched_at,
                       CASE WHEN deadline IS NULL OR deadline > ? THEN 1 ELSE 0 END AS is_open
                FROM tenders
                ORDER BY is_open DESC, published_at DESC NULLS LAST
                LIMIT 40000
                """,
                (now_iso,),
            ).fetchall()
            for r in rows:
                lastmod = (r["fetched_at"] or "")[:10]
                parts.append(
                    f"<url><loc>{escape(SITE_URL)}/tenders/{r['id']}</loc>"
                    + (f"<lastmod>{lastmod}</lastmod>" if lastmod else "")
                    + f"<changefreq>{'daily' if r['is_open'] else 'monthly'}</changefreq>"
                    + f"<priority>{'0.8' if r['is_open'] else '0.3'}</priority></url>"
                )

            parts.append("</urlset>")
            return Response(content="\n".join(parts), media_type="application/xml")
        finally:
            conn.close()

    @app.get("/providers", include_in_schema=False)
    def providers(request: Request):
        conn = connect(db)
        try:
            # Count per source for live display
            counts = dict(conn.execute(
                "SELECT source_system, COUNT(*) FROM tenders GROUP BY source_system"
            ).fetchall())

            def make(**kw):
                kw["count"] = counts.get(kw["id"], 0)
                return kw

            providers = [
                make(
                    id="mercell",
                    name="Mercell (public search API)",
                    status="live",
                    description="Svensk upphandlingsplattform som speglar Tendsign, e-Avrop, "
                                "Kommersannons, TED och andra. Levererar ~65-70% av svensk volym "
                                "via ett öppet, oautentiserat JSON-API.",
                    method="REST GET",
                    method_note="(oautentiserat, polite user-agent)",
                    url_pattern="https://search-service-api.discover.app.mercell.com/public/api/v1/search",
                    requires_auth="Nej",
                    technical="""GET /public/api/v1/search?page=N&pageSize=100
Returns paginated JSON. Filter syntax is lossy — we walk pages
and dedupe on (source_system, source_id). ~525 SE records / 100 pages / 80s.
Headers: User-Agent (polite), Accept: application/json.
No API key required.""",
                ),
                make(
                    id="ted",
                    name="TED EU — Contract Notices",
                    status="live",
                    description="EU-kommissionens officiella databas för upphandlingar "
                                "över EU-tröskelvärden. Vi filtrerar på Sverige (buyer-country=SWE) "
                                "och hämtar öppna upphandlingar (notice-subtype 7, 29). "
                                "Svarar på frågan: \"Vad kan jag lägga anbud på?\"",
                    method="REST POST",
                    method_note="(JSON body med query + fields + filters)",
                    url_pattern="https://api.ted.europa.eu/v3/notices/search",
                    requires_auth="Nej",
                    technical="""POST /v3/notices/search
Body: {"query": "buyer-country = SWE AND publication-date >= 20260101",
       "fields": [...], "limit": 100, "page": 1}
Returns notice metadata. Only covers EU-threshold procurements,
not all Swedish tenders. Polite User-Agent required.""",
                ),
                make(
                    id="ted_awards",
                    name="TED EU — Contract Awards",
                    status="live",
                    description="Tilldelningsbeslut från TED — visar VILKA kontrakt som "
                                "redan har tilldelats, till vem och till vilket värde. "
                                "Marknadsintelligence för småföretag: \"Vem vann senast?\" "
                                "notice-subtypes 16–19 (standard, sectoral, concessions, defence).",
                    method="REST POST",
                    method_note="(samma API som ted, annan subtype-filter)",
                    url_pattern="https://api.ted.europa.eu/v3/notices/search",
                    requires_auth="Nej",
                    technical="""POST /v3/notices/search
Body: {"query": "buyer-country = SWE AND notice-subtype = \\"16\\" OR \\"17\\" ...",
       "fields": ["winner-name", "result-value-lot", ...]}
Winner fields are requested but often empty in search results —
full data lives in the notice XML body. ~18k SWE awards/year.""",
                ),
                make(
                    id="ted_pin",
                    name="TED EU — Prior Information Notices",
                    status="live",
                    description="Förhandsinformation om kommande upphandlingar. "
                                "Myndigheter meddelar att de PLANERAR att upphandla — "
                                "innan formell annons publiceras. Tidigast möjliga signal "
                                "för småföretag att förbereda sig. notice-subtypes 4, 5, 25, 26.",
                    method="REST POST",
                    method_note="(samma API, subtype-filter för PIN)",
                    url_pattern="https://api.ted.europa.eu/v3/notices/search",
                    requires_auth="Nej",
                    technical="""POST /v3/notices/search
Body: {"query": "buyer-country = SWE AND notice-subtype = \\"4\\" OR \\"5\\" ...",
       "fields": ["estimated-value-lot", "future-notice", ...]}
~1k SWE PINs/year. Low volume but high strategic value.""",
                ),
            ]

            return HTMLResponse(render("providers.html", providers=providers))
        finally:
            conn.close()

    # ---- JSON API ----
    @app.get("/api/health")
    def health() -> dict:
        conn = connect(db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM tenders").fetchone()[0]
            last = conn.execute(
                "SELECT source, run_at, count, status FROM sync_log ORDER BY run_at DESC LIMIT 1"
            ).fetchone()
            return {
                "ok": True,
                "tenders_total": n,
                "last_sync": dict(last) if last else None,
                "cron_schedule": get_schedule(),
                "next_run_utc": next_run().strftime("%Y-%m-%dT%H:%M:%SZ") if next_run() else None,
            }
        finally:
            conn.close()

    @app.get("/api/stats")
    def stats() -> dict:
        conn = connect(db)
        try:
            by_source = conn.execute(
                "SELECT source_system, COUNT(*) AS n FROM tenders GROUP BY source_system ORDER BY n DESC"
            ).fetchall()
            top_auth = conn.execute(
                "SELECT authority, COUNT(*) AS n FROM tenders "
                "WHERE authority IS NOT NULL AND authority != '' "
                "GROUP BY authority ORDER BY n DESC LIMIT 15"
            ).fetchall()
            recent = conn.execute(
                "SELECT source, run_at, count, status, message FROM sync_log "
                "ORDER BY run_at DESC LIMIT 20"
            ).fetchall()
            return {
                "by_source": [dict(r) for r in by_source],
                "top_authorities": [dict(r) for r in top_auth],
                "recent_syncs": [dict(r) for r in recent],
            }
        finally:
            conn.close()

    @app.get("/api/tenders")
    def list_tenders(
        source: Optional[str] = Query(default=None),
        authority: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    ) -> dict:
        conn = connect(db)
        try:
            where = []
            args: list = []
            if source:
                where.append("source_system = ?")
                args.append(source)
            if authority:
                where.append("authority LIKE ?")
                args.append(f"%{authority}%")
            if q:
                where.append("(title LIKE ? OR description LIKE ?)")
                args.extend([f"%{q}%", f"%{q}%"])
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute(
                f"SELECT COUNT(*) FROM tenders {where_sql}", args
            ).fetchone()[0]
            if page == 1 and (q or source or authority):
                _log_usage_safe(conn, "api", "search", query=q,
                                 meta={"source": source, "authority": authority,
                                       "segment": _segment_for(q, None), "results": total})
            rows = conn.execute(
                f"""
                SELECT id, source_system, source_id, tender_url, title, authority,
                       cpv_codes, deadline, published_at, value, procedure, region
                FROM tenders {where_sql}
                ORDER BY published_at DESC NULLS LAST, id DESC
                LIMIT ? OFFSET ?
                """,
                args + [page_size, (page - 1) * page_size],
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                if d.get("cpv_codes"):
                    try:
                        d["cpv_codes"] = json.loads(d["cpv_codes"])
                    except Exception:
                        d["cpv_codes"] = []
                items.append(d)
            return {"items": items, "page": page, "page_size": page_size, "total": total}
        finally:
            conn.close()

    @app.get("/api/tenders/{tid}")
    def get_tender(tid: int) -> dict:
        conn = connect(db)
        try:
            row = conn.execute("SELECT * FROM tenders WHERE id = ?", (tid,)).fetchone()
            if not row:
                raise HTTPException(404, "tender not found")
            d = dict(row)
            for k in ("cpv_codes", "raw_json"):
                if d.get(k):
                    try:
                        d[k] = json.loads(d[k])
                    except Exception:
                        pass
            return d
        finally:
            conn.close()

    @app.post("/api/sync")
    def trigger_sync(request: Request) -> JSONResponse:
        """Fire-and-forget: spawn the orchestrator in the background."""
        _require_admin(request)
        global _sync_running
        if not _sync_lock.acquire(blocking=False):
            return JSONResponse(
                {"ok": False, "error": "sync already running"},
                status_code=409,
            )
        _sync_running = True

        def run():
            global _sync_running
            try:
                subprocess.run(
                    ["python", "-m", "scraper.orchestrator"],
                    cwd="/app",
                    timeout=600,
                    capture_output=True,
                )
            except Exception as exc:
                LOG.exception("background sync failed: %s", exc)
            finally:
                _sync_running = False
                _sync_lock.release()

        threading.Thread(target=run, daemon=True).start()
        return JSONResponse(
            {"ok": True, "started_at": datetime.now(timezone.utc).isoformat(),
             "note": "poll /api/health in ~60-90s to confirm completion"},
            status_code=202,
        )

    @app.post("/api/backfill")
    def backfill(request: Request, days: int = Query(default=90, ge=1, le=365)):
        """Trigger a backfill with a longer lookback for TED EU.
        Default: 90 days. Max: 365 days.
        TED has ~6500 SWE notices per 90 days."""
        _require_admin(request)
        env = dict(os.environ)
        env["TED_LOOKBACK_DAYS"] = str(days)
        try:
            proc = subprocess.Popen(
                ["python", "-m", "scraper.orchestrator"],
                cwd="/app",
                stdout=open("/var/log/agentanbud.log", "a"),
                stderr=subprocess.STDOUT,
                env=env,
            )
            return JSONResponse(
                {"ok": True, "days": days, "started_at": datetime.now(timezone.utc).isoformat(),
                 "note": f"backfilling {days}d of TED EU — check /api/stats in 2-5 min"},
                status_code=202,
            )
        except FileNotFoundError:
            return JSONResponse(
                {"ok": False, "error": "not running in Docker — run manually"},
                status_code=500,
            )

    @app.post("/api/repair-links")
    def repair_links(request: Request):
        """Repair tender links (2026-07 URL audit).

        1. ted / ted_awards / ted_pin: rebuild as /en/notice/-/detail/{nr}
           — the only form that renders in a browser. Both /en/notice/{nr}
           and /en/notice/{nr}/html bounce users to the TED homepage.
        2. mercell: delete all rows and re-sync. Old rows are keyed on
           unstable search-index ids (duplicates after Mercell
           re-indexes) and point at the dead /sv-SE/m/tender/ route.
           The scraper now keys on repsNoticeId, links to /tender/{id}
           and skips Mercell's TED mirrors.
        """
        _require_admin(request)
        conn = connect(db)
        try:
            ted_fixed = conn.execute(
                "UPDATE tenders SET tender_url = 'https://ted.europa.eu/en/notice/-/detail/' || source_id "
                "WHERE source_system IN ('ted', 'ted_awards', 'ted_pin') "
                "AND tender_url != 'https://ted.europa.eu/en/notice/-/detail/' || source_id"
            ).rowcount
            mercell_deleted = conn.execute(
                "DELETE FROM tenders WHERE source_system = 'mercell'"
            ).rowcount
            conn.commit()
        finally:
            conn.close()

        def resync():
            try:
                import scraper.mercell as mercell_mod
                mercell_mod.run(db)
            except Exception:
                LOG.exception("mercell resync after repair failed")

        threading.Thread(target=resync, daemon=True).start()
        return JSONResponse({
            "ok": True,
            "ted_urls_fixed": ted_fixed,
            "mercell_rows_deleted": mercell_deleted,
            "note": "mercell re-sync started — check /api/stats in ~2 min",
        })

    @app.post("/api/admin/query")
    async def admin_query(request: Request):
        """Read-only SQL access for maintenance and diagnostics.

        Requires ADMIN_API_KEY to be configured AND supplied — unlike the
        mutating endpoints this one fails closed when no key is set.
        SELECT/WITH single statements only; the connection is opened with
        PRAGMA query_only so writes are rejected at the SQLite level too.

        Body: {"sql": "SELECT ...", "params": [...]}
        Returns: {"columns": [...], "rows": [[...], ...], "truncated": bool}
        """
        if not ADMIN_API_KEY:
            raise HTTPException(status_code=403, detail="ADMIN_API_KEY not configured")
        _require_admin(request)
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        sql = str(body.get("sql") or "").strip().rstrip(";").strip()
        params = body.get("params") or []
        if not sql:
            raise HTTPException(status_code=400, detail="missing sql")
        if ";" in sql:
            raise HTTPException(status_code=400, detail="single statement only")
        if sql.split(None, 1)[0].lower() not in ("select", "with"):
            raise HTTPException(status_code=400, detail="SELECT/WITH statements only")
        conn = connect(db)
        try:
            conn.execute("PRAGMA query_only = ON")
            try:
                cur = conn.execute(sql, params)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"query failed: {exc}")
            columns = [c[0] for c in cur.description or []]
            rows = cur.fetchmany(ADMIN_QUERY_MAX_ROWS + 1)
            truncated = len(rows) > ADMIN_QUERY_MAX_ROWS
            return JSONResponse({
                "columns": columns,
                "rows": [list(r) for r in rows[:ADMIN_QUERY_MAX_ROWS]],
                "truncated": truncated,
            })
        finally:
            conn.close()

    # ---- Knowledge base (criteria + questions) ----
    @app.get("/api/knowledge")
    def list_knowledge(
        request: Request,
        source: Optional[str] = Query(default=None, description="criteria | questions"),
        category: Optional[str] = Query(default=None),
        q: Optional[str] = Query(default=None),
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    ) -> dict:
        conn = connect(db)
        try:
            where = []
            args: list = []
            if source:
                where.append("source_system = ?")
                args.append(source)
            if category:
                where.append("(category = ? OR subcategory = ?)")
                args.extend([category, category])
            if q:
                where.append("(title LIKE ? OR excerpt LIKE ? OR tags LIKE ?)")
                args.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""
            total = conn.execute(
                f"SELECT COUNT(*) FROM knowledge {where_sql}", args
            ).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT id, source_system, source_id, url, title, category, subcategory,
                       tags, excerpt, body, fetched_at
                FROM knowledge {where_sql}
                ORDER BY source_system, id
                LIMIT ? OFFSET ?
                """,
                args + [page_size, (page - 1) * page_size],
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                if d.get("tags"):
                    try:
                        d["tags"] = json.loads(d["tags"])
                    except Exception:
                        d["tags"] = []
                items.append(d)
            return {"items": items, "page": page, "page_size": page_size, "total": total}
        finally:
            conn.close()

    @app.get("/api/knowledge/stats")
    def knowledge_stats() -> dict:
        """Counts per source_system, and per top category."""
        conn = connect(db)
        try:
            by_source = conn.execute(
                "SELECT source_system, COUNT(*) as n FROM knowledge GROUP BY source_system"
            ).fetchall()
            by_category = conn.execute(
                "SELECT source_system, category, COUNT(*) as n FROM knowledge "
                "WHERE category IS NOT NULL AND category != '' "
                "GROUP BY source_system, category ORDER BY n DESC LIMIT 20"
            ).fetchall()
            return {
                "by_source": [dict(r) for r in by_source],
                "top_categories": [dict(r) for r in by_category],
            }
        finally:
            conn.close()

    @app.get("/kunskap", include_in_schema=False)
    def kunskap(
        request: Request,
        source: str = "",
        category: str = "",
        q: str = "",
        page: int = 1,
    ):
        """Browse the knowledge base — criteria and Q&A from UHM."""
        page = max(1, page)
        conn = connect(db)
        try:
            where = []
            args: list = []
            if source:
                where.append("source_system = ?")
                args.append(source)
            if category:
                where.append("(category = ? OR subcategory = ?)")
                args.extend([category, category])
            if q:
                where.append("(title LIKE ? OR excerpt LIKE ? OR tags LIKE ?)")
                args.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
            where_sql = ("WHERE " + " AND ".join(where)) if where else ""

            page_size = 20
            total = conn.execute(
                f"SELECT COUNT(*) FROM knowledge {where_sql}", args
            ).fetchone()[0]
            pages = max(1, (total + page_size - 1) // page_size)
            rows = conn.execute(
                f"""
                SELECT id, source_system, title, category, subcategory, tags, excerpt, url
                FROM knowledge {where_sql}
                ORDER BY source_system, category, id
                LIMIT ? OFFSET ?
                """,
                args + [page_size, (page - 1) * page_size],
            ).fetchall()
            items = []
            for r in rows:
                d = dict(r)
                if d.get("tags"):
                    try:
                        d["tags_list"] = json.loads(d["tags"])
                    except Exception:
                        d["tags_list"] = []
                items.append(d)

            # Top categories for filter sidebar
            top_categories = conn.execute(
                "SELECT source_system, category, COUNT(*) as n FROM knowledge "
                "WHERE category IS NOT NULL AND category != '' "
                "GROUP BY source_system, category ORDER BY n DESC LIMIT 15"
            ).fetchall()

            # Total counts per source
            source_counts = conn.execute(
                "SELECT source_system, COUNT(*) as n FROM knowledge GROUP BY source_system"
            ).fetchall()
            sc = {r["source_system"]: r["n"] for r in source_counts}

            return HTMLResponse(render("kunskap.html",
                q=q, source=source, category=category, page=page, pages=pages,
                total=total, items=items,
                top_categories=[dict(r) for r in top_categories],
                source_counts=sc,
            ))
        finally:
            conn.close()

    return app


# Lazy app creation so importing this module doesn't require DB
_app = None
def get_app():
    global _app
    if _app is None:
        _app = create_app()
    return _app


def HTMLResponse(html: str, status_code: int = 200):
    from fastapi.responses import HTMLResponse as _HR
    return _HR(html, status_code=status_code)
