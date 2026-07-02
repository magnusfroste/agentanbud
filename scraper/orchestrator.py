"""
Orchestrator — runs every enabled scraper in sequence.

CLI: `python -m scraper.orchestrator`. The container's cron entry invokes
this once per day. Each scraper logs its own outcome to `sync_log`; the
dashboard reads that table to show recent runs.

Enable/disable individual sources via env vars (see docker-compose.yml):
  SCRAPE_MERCELL=true
  SCRAPE_TED=true
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

from . import mercell, ted, ted_awards, ted_pin

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
    results["_elapsed_s"] = int(time.time() - t0)
    LOG.info("scrape complete: %s", results)
    return results


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    db_path = os.environ.get("DB_PATH", "/data/agentanbud.db")
    run_all(db_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
