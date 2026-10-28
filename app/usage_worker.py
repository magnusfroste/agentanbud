"""
Compute one usage summary and print it as JSON — run as its own process.

app/usage_cache.py starts this with `python -m app.usage_worker DB DAYS` so
that the scan of usage_log (mostly Python looping over rows) runs outside
the web server's process and cannot take its turn on the interpreter. See
usage_cache._run_worker for why.
"""
import json
import sys

from app.db import connect
from app.insights import usage_summary


def main(argv: list[str]) -> None:
    db_path, days = argv[1], (int(argv[2]) if len(argv) > 2 and argv[2] != "all" else None)
    conn = connect(db_path)
    try:
        json.dump(usage_summary(conn, days=days), sys.stdout, ensure_ascii=False)
    finally:
        conn.close()


if __name__ == "__main__":
    main(sys.argv)
