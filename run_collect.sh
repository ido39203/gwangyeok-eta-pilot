#!/usr/bin/env bash
# 맥·리눅스용: 수집기가 꺼지면 30초 뒤 다시 켠다. 멈추려면 터미널을 닫거나 Ctrl+C 두 번.
cd "$(dirname "$0")"
if [ -f .venv/bin/activate ]; then source .venv/bin/activate; fi
while true; do
  python collect.py
  echo "collector stopped. restarting in 30 seconds (close this window to stop)"
  sleep 30
done
