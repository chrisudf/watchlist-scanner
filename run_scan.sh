#!/bin/zsh
# Watchlist left/right-side scan. Scheduled via
# ~/Library/LaunchAgents/com.zoez.watchlist-scanner.plist at fixed Brisbane
# times; scanner.py --mode auto keeps whichever fires land inside a
# US-session window and exits 3 for the rest (survives US DST shifts and
# US weekends).

cd "$(dirname "$0")" || exit 1
mkdir -p reports data
d=$(date +%F)
log="reports/run_$d.log"

# 09:00-12:59 Brisbane can never be an in-window scan under either US DST
# regime (in-window Brisbane times sit in 23:40-01:50 and 05:30-07:05 only).
# Fires here are the 10:30 watchdog or sleep-coalesced missed fires: check
# whether the reports that day was entitled to exist, instead of scanning.
bh=$(date +%H)
if (( bh >= 9 && bh <= 12 )); then
  et_date=$(TZ=America/New_York date +%F)   # = the US trading day just ended
  # 该交易日该有哪几份报告 —— 与 droplet 看门狗同一个判据 (scanner.py 的
  # NYSE 日历): 休市 0 份、半日市 1 份、整日 2 份。此前这里只查 et_dow<=5,
  # 于是每个美股假日都误报一次 (见 lesson.md 2026-09-08)。
  if expect=$(.venv/bin/python -c '
import sys
from scanner import expected_report_modes
modes, why = expected_report_modes(sys.argv[1])
print(" ".join(modes))
print(why)' "$et_date" 2>>"$log"); then
    modes=${expect%%$'\n'*}
    why=${expect#*$'\n'}
  else
    # 判定器自己挂了: 按整日算、照常报警 —— 看门狗宁可误报, 不可静默
    modes="open close"
    why="expected_report_modes 调用失败 (traceback 见上) — 按整日算"
  fi
  missing=""
  for m in ${=modes}; do
    [[ -f "reports/$et_date-$m.md" ]] || missing+="$m "
  done
  echo "watchdog $et_date ET — $why | 应有: ${modes:-无} | 缺: ${missing:-无}" >> "$log"
  if [[ -n $missing ]]; then
    osascript -e "display notification \"missed: ${missing}($et_date ET — 电脑睡眠错过触发时点或扫描失败)\" with title \"Watchlist scan MISSED\"" 2>/dev/null
  fi
  exit 0
fi

.venv/bin/python scanner.py --mode auto >> "$log" 2>&1
status=$?

if [[ $status -eq 0 ]]; then
  report=$(grep '^REPORT ' "$log" | tail -1 | cut -d' ' -f2)
  osascript -e "display notification \"${report:t}\" with title \"Watchlist scan done\"" 2>/dev/null
elif [[ $status -ne 3 ]]; then
  osascript -e "display notification \"Check $PWD/$log\" with title \"Watchlist scan FAILED\"" 2>/dev/null
fi
# status 3 = intentional skip (outside window / duplicate / market closed) — silent
exit $status
