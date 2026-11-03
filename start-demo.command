#!/bin/bash
# Double-click this file to try the AQ test platform on this Mac.
# (First time: right-click it and choose Open, because it was downloaded from the internet.)
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed. Install Python 3.12 or newer from https://www.python.org/downloads/"
  read -r -p "Then double-click this file again. Press Enter to close."
  exit 1
fi
python3 demo.py "$@"
read -r -p "The demo has stopped. Press Enter to close."
