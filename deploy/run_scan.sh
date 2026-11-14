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

# 推荐复盘 (月度): 结算流水账里的 CSP/LEAP, 出 markdown 并邮寄。
# 与扫描互不相干 —— 它只读 data/recommendations.jsonl 与历史日线, 不碰
# state/iv history, 也不受扫描窗口门约束, 任何时候跑都安全。
if [ "${1:-}" = "review" ]; then
  out="reports/review-$(date +%Y-%m).md"
  .venv/bin/python review.py --md "$out" >> "$log" 2>&1
  status=$?
  if [ "$status" -eq 0 ] && [ -s "$out" ]; then
    notify "[watchlist] 推荐复盘 $(date +%Y-%m)" "$out"
  else
    # 复盘失败不静默: 它一个月才跑一次, 挂了没人会注意到
    body=$(mktemp); tail -50 "$log" > "$body"
    notify "[watchlist] 复盘 FAILED $(date +%Y-%m) (exit $status)" "$body"
    rm -f "$body"
  fi
  exit "$status"
fi

# 晚间内部人检查: 美东 22:05-22:59 窗口内查当天新申报的公开市场买入, 有新买入
# 才发邮件; 没有 / 窗口外 = exit 3 静默。取数失败也是 exit 3 (次日开盘报告会
# 重试并写出来); 只有崩溃和发信失败才发 FAILED。
if [ "${1:-}" = "insider" ]; then
  .venv/bin/python scanner.py --mode insider --email >> "$log" 2>&1
  status=$?
  if [ "$status" -ne 0 ] && [ "$status" -ne 3 ]; then
    body=$(mktemp); tail -50 "$log" > "$body"
    notify "[watchlist] insider FAILED $d (exit $status)" "$body"
    rm -f "$body"
  fi
  exit "$status"
fi

# 盘前扫描 (premarket.py, 只读): 布里斯班 22:00 = 07:00 EST / 08:00 EDT, 一条
# cron 两个 DST 都落在 04:00-09:25 ET 窗口内。窗口外/休市/已跑过 = exit 3 静默;
# 崩溃或发信失败才发 FAILED。
if [ "${1:-}" = "premarket" ]; then
  .venv/bin/python premarket.py --email >> "$log" 2>&1
  status=$?
  if [ "$status" -ne 0 ] && [ "$status" -ne 3 ]; then
    body=$(mktemp); tail -50 "$log" > "$body"
    notify "[watchlist] premarket FAILED $d (exit $status)" "$body"
    rm -f "$body"
  fi
  exit "$status"
fi

if [ "${1:-}" = "watchdog" ]; then
  et_date=$(TZ=America/New_York date +%F)   # the US trading day just ended
  # 该交易日到底该不该有报告 —— 问盘面, 不查假日日历 (和扫描器的
  # market_is_live() 同一套判断, 单一事实来源)。假日/周末 = 一份都不该有;
  # 半日市 (13:00 ET 收盘) = 只该有 open, 15:45 的尾盘扫描本就无盘可扫。
  # 原先这里只认星期几不认假日 —— 2026-09-07 劳工节整天正确 skip, 看门狗
  # 照样报 MISSED (见 lesson.md)。
  if expect=$(.venv/bin/python -c '
import sys
from scanner import expected_report_modes
modes, why = expected_report_modes(sys.argv[1])
print(" ".join(modes))
print(why)' "$et_date" 2>>"$log"); then
    modes=$(printf '%s\n' "$expect" | sed -n 1p)
    why=$(printf '%s\n' "$expect" | sed -n 2p)
  else
    # 判定器自己挂了 (venv/import 坏了): 按整日算、照常报警 —— 看门狗宁可
    # 误报, 不可因为自身故障而静默, 那正是它要抓的失效模式
    modes="open close"
    why="expected_report_modes 调用失败 (traceback 见上) — 按整日算"
  fi

  # 送达凭证 (.sent, 发信成功后才写) 与报告文件 (.md, 发信前就写) 分开
  # 查: .md 在而 .sent 不在 = 扫描跑了但报告没到收件箱 — 这正是
  # "Resend 抖动一次, 整天静默丢报"的洞 (五轮评审)。scanner 会在下一
  # 个 --email fire 自动补发, 这里报的是补发也没救回来的情况
  # 开市日 (整日或半日) 也该有盘前报告 —— 它在同一个 ET 日的早上跑
  [ -n "$modes" ] && modes="premarket $modes"
  missing=""
  for m in $modes; do
    if [ ! -f "reports/$et_date-$m.md" ]; then
      missing="${missing}${m} "
    elif [ ! -f "reports/$et_date-$m.sent" ]; then
      missing="${missing}${m}(已写盘未送达) "
    fi
  done
  # 每次都留痕: 告警正文让你查这个日志, 而看门狗过去一个字都不往里写,
  # 打开只看到别的 fire 的记录、看不到它凭什么报警
  echo "watchdog $et_date ET — $why | 应有: ${modes:-无} | 缺: ${missing:-无}" \
    >> "$log"
  if [ -n "$missing" ]; then
    body=$(mktemp)
    { echo "missing: $missing($et_date ET) — cron 未跑成/发信失败; 检查 $PWD/$log"
      echo "看门狗判定: $why"
    } > "$body"
    notify "[watchlist] MISSED $et_date" "$body"
    rm -f "$body"
  fi
  exit 0
fi

# 开盘报告暂停邮寄 (照常扫描写盘): .env 里 SCAN_OPEN_EMAIL_OFF_UNTIL=YYYY-MM-DD,
# 美东日期早于它时, 落在开盘窗口 (09:40-10:50 ET, 同 scanner.OPEN_WINDOW) 的 fire
# 不带 --email; 到期自动恢复, 删掉这行也立即恢复。尾盘/补发/看门狗不受影响。
# 跳过的那份写 .sent 凭证 (内容注明"暂停"), 否则尾盘 fire 的补发会把它补寄出去,
# 看门狗也会报"已写盘未送达"。
email=--email
et_date=$(TZ=America/New_York date +%F)
et_hm=$(TZ=America/New_York date +%H%M)
off_until="${SCAN_OPEN_EMAIL_OFF_UNTIL:-}"
open_paused=""
if [ -n "$off_until" ] && [[ "$et_date" < "$off_until" ]] \
    && [ "$et_hm" -ge 940 ] && [ "$et_hm" -le 1050 ]; then
  email=""
  open_paused=1
fi

.venv/bin/python scanner.py --mode auto $email >> "$log" 2>&1
status=$?

if [ -n "$open_paused" ] && [ "$status" -eq 0 ] \
    && [ -f "reports/$et_date-open.md" ] && [ ! -f "reports/$et_date-open.sent" ]; then
  echo "email paused (SCAN_OPEN_EMAIL_OFF_UNTIL=$off_until) — not sent" \
    > "reports/$et_date-open.sent"
  echo "open 报告未邮寄: 暂停至 $off_until" >> "$log"
fi

if [ "$status" -ne 0 ] && [ "$status" -ne 3 ]; then
  body=$(mktemp)
  tail -50 "$log" > "$body"
  notify "[watchlist] FAILED $d (exit $status)" "$body"
  rm -f "$body"
fi
# 3 = intentional skip (outside window / duplicate / market closed) — silent
exit $status
