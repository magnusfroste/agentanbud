#!/usr/bin/env python3
"""cron starts its jobs with no environment, so scripts/cron_env.py hands it
the variables the sync reads. On 20 November the synonym job said it was off
although LLM_* were set in Easypanel: cron never saw them. Offline.

Run: python3 scripts/test_cron_env.py
"""
import os
import pathlib
import stat
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import cron_env  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures += 1


env = {"LLM_BASE_URL": "https://llm.example.invalid/v1", "LLM_API_KEY": "abc'; rm -rf / #",
       "LLM_MODEL": "model name with spaces", "SCRAPE_TED": "false", "DB_PATH": "/data/application.db",
       "TED_LOOKBACK_DAYS": "30", "HOME": "/root", "HOSTNAME": "abc123", "PATH": "/usr/bin",
       "SECRET_OTHER": "not for cron"}
path = tempfile.mktemp()
n = cron_env.write(env, path)
check("only the variables the jobs read", n == 6, str(n))
text = open(path).read()
check("nothing else is written", "HOSTNAME" not in text and "SECRET_OTHER" not in text and "HOME" not in text)
check("readable by its owner alone", stat.S_IMODE(os.stat(path).st_mode) == 0o600,
      oct(stat.S_IMODE(os.stat(path).st_mode)))
out = subprocess.run(["sh", "-c", f'. "{path}"; printf "%s|%s|%s" "$LLM_API_KEY" "$LLM_MODEL" "$SCRAPE_TED"'],
                     capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"}).stdout
check("sourcing it in sh gives the values back exactly, quotes and all",
      out == "abc'; rm -rf / #|model name with spaces|false", out)

dockerfile = (pathlib.Path(__file__).resolve().parent.parent / "Dockerfile").read_text()
check("the cron line sources the file", ". /etc/agentanbud.env" in dockerfile)
start = (pathlib.Path(__file__).resolve().parent / "start.sh").read_text()
check("start.sh writes it before starting cron", start.index("cron_env.py") < start.index("\ncron\n"))
compose = (pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text()
check("docker-compose passes the LLM variables into the container",
      all(f"- {k}=" in compose for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")))

print("\nOK" if not failures else f"\n{failures} FAILED")
sys.exit(1 if failures else 0)
