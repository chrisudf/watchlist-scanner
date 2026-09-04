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
    # 送达凭证 (.sent, 发信成功后才写) 与报告文件 (.md, 发信前就写) 分开
    # 查: .md 在而 .sent 不在 = 扫描跑了但报告没到收件箱 — 这正是
    # "Resend 抖动一次, 整天静默丢报"的洞 (五轮评审)。scanner 会在下一
    # 个 --email fire 自动补发, 这里报的是补发也没救回来的情况
    missing=""
    for m in open close; do
      if [ ! -f "reports/$et_date-$m.md" ]; then
        missing="${missing}${m} "
      elif [ ! -f "reports/$et_date-$m.sent" ]; then
        missing="${missing}${m}(已写盘未送达) "
      fi
    done
    if [ -n "$missing" ]; then
      body=$(mktemp)
      echo "missing: $missing($et_date ET) — cron 未跑成/发信失败/美股假日; 检查 $PWD/$log" > "$body"
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
