#!/bin/sh
# Container entrypoint: cron in the background, the app in the foreground.
#
# The app runs as a child, not as PID 1, so that scripts/watchdog.py can kill
# a hung server: the kernel ignores SIGKILL sent to PID 1 from inside its own
# container. When uvicorn exits — stopped, crashed or killed — this script
# exits with it, the container stops, and `restart: unless-stopped` starts a
# new one. SIGTERM from `docker stop` is passed on, so a deploy still shuts
# the app down cleanly.
cron
uvicorn app.main:get_app --factory --host 0.0.0.0 --port 8080 &
APP=$!
trap 'kill -TERM "$APP" 2>/dev/null' TERM INT
wait "$APP"
STATUS=$?
# A trapped signal ends the first wait early; wait for the app to finish.
if kill -0 "$APP" 2>/dev/null; then
  wait "$APP"
  STATUS=$?
fi
exit "$STATUS"
