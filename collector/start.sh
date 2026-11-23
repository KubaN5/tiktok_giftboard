#!/usr/bin/env bash
# Spuštění sběrače na Linuxu / Raspberry Pi bez Dockeru: ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "Vytvořen .env – doplň SUPABASE_SECRET_KEY (nebo COLLECTOR_TOKEN od vlastníka) a spusť znovu."; exit 1
fi
[ -x .venv/bin/python ] || python3 -m venv .venv
export PIP_CONSTRAINT="$PWD/constraints.txt"
.venv/bin/python -m pip install -q --disable-pip-version-check -U pip setuptools wheel
.venv/bin/python -m pip install -q --disable-pip-version-check --no-build-isolation protobuf3-to-dict
.venv/bin/python -m pip install -q --disable-pip-version-check -U -r requirements.txt
exec .venv/bin/python tracker.py
