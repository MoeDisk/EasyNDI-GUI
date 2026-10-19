#!/usr/bin/env bash
cd "$(dirname "$0")"
if command -v python3.13 >/dev/null 2>&1; then
  exec python3.13 app.py
fi
if command -v python3 >/dev/null 2>&1; then
  exec python3 app.py
fi
echo "需要 Python 3.13+ 與 pip install -r requirements.txt"
exit 1
