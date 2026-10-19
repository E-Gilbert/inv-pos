#!/usr/bin/env bash
# Start CFC POS on Linux / Mac. First time: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
echo "CFC POS running. On other devices open: http://$(hostname -I 2>/dev/null | awk '{print $1}'):8000"
export CFC_LICENCE_MODE="${CFC_LICENCE_MODE:-development}"
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --no-server-header
