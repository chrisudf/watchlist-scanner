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
# whether today's US-session reports exist instead of scanning.
bh=$(date +%H)
if (( bh >= 9 && bh <= 12 )); then
  et_date=$(TZ=America/New_York date +%F)   # = the US trading day just ended
  et_dow=$(TZ=America/New_York date +%u)
  if (( et_dow <= 5 )); then
    missing=""
    [[ -f "reports/$et_date-open.md" ]] || missing+="open "
    [[ -f "reports/$et_date-close.md" ]] || missing+="close "
    if [[ -n $missing ]]; then
      osascript -e "display notification \"missed: ${missing}($et_date ET — 电脑睡眠错过或美股假日)\" with title \"Watchlist scan MISSED\"" 2>/dev/null
    fi
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
