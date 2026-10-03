#!/usr/bin/env bash
# Job Scout for macOS / Linux. Run: ./start.sh  (first run sets everything up)
set -e
cd "$(dirname "$0")"

PY="$(command -v python3 || command -v python || true)"
if [ -z "$PY" ]; then
  echo "Python 3 is not installed. Get it from https://www.python.org/downloads/ and run this again."
  exit 1
fi

if [ ! -x ".venv/bin/python" ]; then
  echo "Setting up Job Scout for the first time..."
  "$PY" -m venv .venv
fi

if ! cmp -s requirements.txt .venv/installed.txt; then
  echo "Installing dependencies..."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
  cp requirements.txt .venv/installed.txt
fi

[ -f config.yaml ] || cp config.example.yaml config.yaml

echo
echo "  Job Scout is starting at http://127.0.0.1:5000 - press Ctrl+C to stop it."
echo
( sleep 4; (command -v open >/dev/null && open http://127.0.0.1:5000) \
  || (command -v xdg-open >/dev/null && xdg-open http://127.0.0.1:5000) || true ) &
exec .venv/bin/python app.py
