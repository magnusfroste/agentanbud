#!/usr/bin/env python3
"""The watchdog restarts a server that stops answering, and only then.

On 24 October the container hung for nine hours: the process did not exit,
so Docker's restart policy never fired. scripts/watchdog.py runs from cron,
counts failed health checks and kills uvicorn after three in a row; the
container then exits and is restarted. These cases run it against a real
local HTTP server (healthy, failing, hanging) with the kill replaced by a
recorder. The CI image job does the real thing inside the container.

Run: python3 scripts/test_watchdog.py
"""
import http.server
import json
import os
import pathlib
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone

os.environ["ADMIN_API_KEY"] = "k" * 64
os.environ.pop("ALLOW_OPEN_ADMIN", None)
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import watchdog  # noqa: E402
from app import ops  # noqa: E402

failures = 0


def check(label, ok, detail=""):
    global failures
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures += 1


MODE = {"v": "ok"}


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if MODE["v"] == "hang":
            time.sleep(3)
        code, body = (200, {"ok": True}) if MODE["v"] == "ok" else (500, {"ok": False})
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(json.dumps(body).encode())
        except BrokenPipeError:  # the hanging case: the client gave up first
            pass

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{srv.server_address[1]}/api/health"

tmp = pathlib.Path(tempfile.mkdtemp())
state, log = tmp / "fails", tmp / "watchdog.log"

# A fake /proc: the server, an unrelated process, and the watchdog's own pid.
proc = tmp / "proc"
for pid, cmd in ((101, b"/usr/local/bin/python\0/usr/local/bin/uvicorn\0app.main:get_app\0--factory"),
                 (102, b"cron\0"), (os.getpid(), b"python\0uvicorn app.main"),
                 (103, b"/bin/bash\0-c\0cd /app && uvicorn app.main:get_app --port 8080\0"),
                 (104, b"uvicorn\0app.main:get_app\0--factory\0")):
    (proc / str(pid)).mkdir(parents=True)
    (proc / str(pid) / "cmdline").write_bytes(cmd)
(proc / "self").mkdir()

killed = []


def run(url=URL):
    return watchdog.run(url=url, state=state, log=log, proc=proc,
                        kill=lambda pid, sig: killed.append((pid, sig)),
                        check=lambda u: watchdog.healthy(u, timeout=1))


print("Hälsokollen")
MODE["v"] = "ok"
check("200 med ok: true är friskt", watchdog.healthy(URL, 1) == (True, ""))
MODE["v"] = "error"
check("500 är sjukt", watchdog.healthy(URL, 1)[0] is False)
MODE["v"] = "hang"
t0 = time.time()
ok, why = watchdog.healthy(URL, 1)
check("ett svar som dröjer är sjukt, inom tidsgränsen", not ok and time.time() - t0 < 2.5, why)
check("ingen som svarar är sjukt", watchdog.healthy("http://127.0.0.1:1/", 1)[0] is False)

print("\nRäkningen")
check("hittar servern, inte cron, inte sig själv och inte ett skal som nämner den",
      watchdog.uvicorn_pids(proc) == [101, 104], str(watchdog.uvicorn_pids(proc)))
MODE["v"] = "ok"
check("frisk: ingenting görs", run() == "ok" and not killed and not log.exists())
MODE["v"] = "error"
check("första missen räknas", run() == "fail 1/3" and not killed)
MODE["v"] = "ok"
check("ett friskt svar nollställer", run() == "ok" and not state.exists())
MODE["v"] = "error"
r = [run(), run()]
check("två missar i rad: ännu ingen omstart", r == ["fail 1/3", "fail 2/3"] and not killed, str(r))
check("tredje missen: servern får SIGKILL", run() == "restart"
      and killed == [(101, signal.SIGKILL), (104, signal.SIGKILL)], str(killed))
check("räkningen börjar om efteråt", not state.exists())
entry = json.loads(log.read_text().splitlines()[-1])
check("omstarten loggas med orsak", entry["action"] == "restart" and entry["fails"] == 3
      and entry["pids"] == [101, 104] and "500" in entry["reason"], str(entry))

print("\nDriftöversikten")
now = datetime.now(timezone.utc)
old = {"at": (now - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), "action": "restart", "reason": "gammal"}
with log.open("a") as f:
    f.write(json.dumps(old) + "\n")
    f.write("inte json\n")
recent = ops.watchdog_restarts(str(log))
check("senaste dygnets omstarter, inte äldre, och trasiga rader hoppas över",
      len(recent) == 1 and recent[0]["reason"] != "gammal", str(recent))
check("ingen logg: inga omstarter", ops.watchdog_restarts(str(tmp / "finns-inte.log")) == [])

from fastapi.testclient import TestClient  # noqa: E402
import app.main as m  # noqa: E402

db = str(tmp / "application.db")          # the log lives next to the database
c = TestClient(m.create_app(db), base_url="https://testserver")
warn = c.get("/api/ops", headers={"x-admin-key": "k" * 64}).json()["warnings"]
check("ops varnar för omstarten", any("Vakthunden startade om appen 1 gång" in w for w in warn), str(warn))

print("\nStart och schema")
start = (ROOT / "scripts" / "start.sh").read_text()
check("appen startas som barnprocess och signaler skickas vidare",
      "uvicorn app.main:get_app" in start and "&" in start and "trap" in start and 'exit "$STATUS"' in start)
docker = (ROOT / "Dockerfile").read_text()
check("Dockerfile startar via start.sh och schemalägger vakthunden",
      'CMD ["sh", "/app/scripts/start.sh"]' in docker and "*/2 * * * * root cd /app && python scripts/watchdog.py" in docker)

srv.shutdown()
if failures:
    print(f"\n{failures} fall misslyckades")
    sys.exit(1)
print("\nAlla fall passerade")
