#!/bin/bash
# CAM serve auto-restart daemon
# Usage: nohup ./cam-serve-daemon.sh &
# Stop:  kill $(cat /tmp/cam-serve-daemon.pid)

PIDFILE="/tmp/cam-serve-daemon.pid"
LOGFILE="/tmp/cam-serve.log"

# Kill any existing daemon
if [ -f "$PIDFILE" ]; then
    oldpid=$(cat "$PIDFILE")
    kill "$oldpid" 2>/dev/null
    sleep 1
fi

# Record our PID
echo $$ > "$PIDFILE"

# Cleanup on exit
trap 'rm -f "$PIDFILE"; exit 0' SIGTERM SIGINT

echo "[$(date)] cam-serve-daemon starting (PID $$)" >> "$LOGFILE"

while true; do
    echo "[$(date)] Starting cam serve..." >> "$LOGFILE"
    /data/venv/bin/cam serve \
        --host 127.0.0.1 \
        --port 8420 \
        --token cam-web-token-2026 \
        --relay ws://localhost:18001 \
        --relay-token camtest123 \
        >> "$LOGFILE" 2>&1

    EXIT_CODE=$?
    echo "[$(date)] cam serve exited with code $EXIT_CODE, restarting in 5s..." >> "$LOGFILE"
    sleep 5
done
