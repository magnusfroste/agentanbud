#!/usr/bin/env python3
"""Hand the container's environment to cron.

cron starts its jobs with an empty environment (PATH only), so the nightly
sync never saw what Easypanel sets: SCRAPE_* toggles fell back to their
defaults, and LLM_BASE_URL / LLM_API_KEY / LLM_MODEL were missing, so the
synonym job reported itself off on 20 November although they were set.

scripts/start.sh runs this before starting cron. It writes the variables the
jobs read — and only those — to ENV_FILE as `export NAME='value'` lines,
readable by root alone because LLM_API_KEY is among them. The cron line
sources the file before running the orchestrator.
"""
from __future__ import annotations

import os
import shlex
import sys

ENV_FILE = "/etc/agentanbud.env"
PREFIXES = ("SCRAPE_", "LLM_", "TED_")
NAMES = ("DB_PATH", "USER_AGENT", "LOG_LEVEL")


def wanted(name: str) -> bool:
    return name in NAMES or name.startswith(PREFIXES)


def render(env: dict) -> str:
    lines = [f"export {k}={shlex.quote(v)}" for k, v in sorted(env.items())
             if wanted(k) and k.isidentifier()]
    return "\n".join(lines) + ("\n" if lines else "")


def write(env: dict, path: str = ENV_FILE) -> int:
    """Write the file with mode 0600 from the start; returns how many variables."""
    text = render(env)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(path, 0o600)
    return text.count("\n")


if __name__ == "__main__":
    n = write(dict(os.environ), sys.argv[1] if len(sys.argv) > 1 else ENV_FILE)
    # The count only — never a name's value.
    print(f"cron_env: {n} variables for cron")
