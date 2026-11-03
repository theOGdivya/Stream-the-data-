#!/usr/bin/env bash
# Runs every time the codespace starts: starts the platform in the background (once).
cd "$(dirname "$0")/.."
mkdir -p demo-data
if pgrep -f "demo.py --no-browser --secure" >/dev/null; then
  echo "The platform is already running."
  exit 0
fi
nohup python demo.py --no-browser --secure > demo-data/codespace.log 2>&1 &
echo "Starting the platform (log: demo-data/codespace.log). Your login is in demo-data/login.txt."
