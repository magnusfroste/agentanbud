"""
Orchestrator — runs every enabled scraper in sequence.

CLI: `python -m scraper.orchestrator`. The container's cron entry invokes
this once per day. Each scraper logs its own outcome to `sync_log`; the
dashboard reads that table to show recent runs.

Enable/disable individual sources via env vars (see docker-compose.yml):
  SCRAPE_MERCELL=true
  SCRAPE_TED=true
  SCRAPE_LOV=true
  SCRAPE_CRITERIA=true
  SCRAPE_QUESTIONS=true
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

from . import mercell, ted, ted_awards, ted_pin, lov, criteria, questions
from app.db import connect, prune_logs

LOG = logging.getLogger(__name__)


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _registry() -> list[tuple[str, bool, Callable[[str], int]]]:
    """Return [(name, enabled, fn), ...] in scrape order."""
    return [
        ("mercell",     _truthy(os.environ.get("SCRAPE_MERCELL", "true")), mercell.run),
        ("ted",         _truthy(os.environ.get("SCRAPE_TED", "true")),     ted.run),
        ("ted_awards",  _truthy(os.environ.get("SCRAPE_TED_AWARDS", "true")),  ted_awards.run),
        ("ted_pin",     _truthy(os.environ.get("SCRAPE_TED_PIN", "true")),     ted_pin.run),
        ("lov",         _truthy(os.environ.get("SCRAPE_LOV", "true")),         lov.run),
        ("criteria",    _truthy(os.environ.get("SCRAPE_CRITERIA", "true")),    criteria.run),
        ("questions",   _truthy(os.environ.get("SCRAPE_QUESTIONS", "true")),   questions.run),
    ]


def run_all(db_path: str) -> dict:
    """Run each enabled scraper. Returns per-source counts + total."""
    results: dict[str, int] = {}
    t0 = time.time()
    for name, enabled, fn in _registry():
        if not enabled:
            LOG.info("skipping %s (disabled via env)", name)
            continue
        try:
            if name in ("ted", "ted_awards", "ted_pin"):
                lookback = int(os.environ.get(
                    "TED_LOOKBACK_DAYS" if name == "ted" else
                    f"TED_{name.split('_')[1].upper()}_LOOKBACK_DAYS",
                    "30" if name == "ted" else "90",
                ))
                n = fn(db_path, lookback_days=lookback)
            else:
                n = fn(db_path)
            results[name] = n
        except Exception as exc:
            LOG.exception("scraper %s crashed", name)
            results[name] = -1
    # Housekeeping: cap the append-only log tables once a day, here rather
    # than at app startup — a DELETE+VACUUM can hold the write lock long
    # enough to fail the container health check and crash-loop the app.
    # Runs after the scrapers so the write lock is already ours.
    try:
        conn = connect(db_path)
        try:
            prune_logs(conn)
        finally:
            conn.close()
    except Exception:
        LOG.exception("prune_logs failed")

    results["_elapsed_s"] = int(time.time() - t0)
    LOG.info("scrape complete: %s", results)
    return results


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db_path = os.environ.get("DB_PATH", "/data/application.db")
    run_all(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
