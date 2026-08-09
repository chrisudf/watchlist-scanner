#!/usr/bin/env bash
# Droplet wrapper (Linux, headless): source .env, run the scan with --email,
# email failures and missed-scan watchdog alerts through the same SMTP env.
# Scheduled via deploy/crontab.example (UTC clock).
set -u
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
mkdir -p reports data
d=$(date +%F)
log="reports/run_$d.log"

notify() {  # notify <subject> <file-with-body>
  .venv/bin/python -c "import sys; from pathlib import Path; \
from scanner import send_email_report; \
send_email_report(Path(sys.argv[2]), sys.argv[1])" "$1" "$2" \
    >> "$log" 2>&1 || true
}

if [ "${1:-}" = "watchdog" ]; then
  et_date=$(TZ=America/New_York date +%F)   # the US trading day just ended
  et_dow=$(TZ=America/New_York date +%u)
  if [ "$et_dow" -le 5 ]; then
    missing=""
    [ -f "reports/$et_date-open.md" ] || missing="open "
    [ -f "reports/$et_date-close.md" ] || missing="${missing}close"
    if [ -n "$missing" ]; then
      body=$(mktemp)
      echo "missing: $missing ($et_date ET) — cron 未跑成或美股假日; 检查 $PWD/$log" > "$body"
      notify "[watchlist] MISSED $et_date" "$body"
      rm -f "$body"
    fi
  fi
  exit 0
fi

.venv/bin/python scanner.py --mode auto --email >> "$log" 2>&1
status=$?

if [ "$status" -ne 0 ] && [ "$status" -ne 3 ]; then
  body=$(mktemp)
  tail -50 "$log" > "$body"
  notify "[watchlist] FAILED $d (exit $status)" "$body"
  rm -f "$body"
fi
# 3 = intentional skip (outside window / duplicate / market closed) — silent
exit $status
