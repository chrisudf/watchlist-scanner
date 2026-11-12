#!/usr/bin/env python3
"""盘前扫描 (~07:45 ET) — scanner.py 开盘扫描的盘前版, 独立脚本, 不改 scanner.py。

为什么另起一个: scanner.py 的 open 模式在盘前跑不起来, 也跑不对 ——
  - OPEN_WINDOW (09:40-10:50) 和 market_is_live() (SPY 正常时段 1m bar)
    两道门在盘前恒判 skip;
  - 日线最后一根是昨天, gap_pct/change_pct 算出来是"昨天的开盘 gap"。
这里改用 yfinance 1m prepost=True 取盘前最新价, 对比昨收算隔夜 gap,
价值区/20 日线也按盘前价判断 (盘前流动性薄, 报告里标出盘前成交量)。

只读: 不写 state.json / iv_history / 推荐流水账, 不调 insider.mark_seen
(新申报留给正式的开盘/尾盘报告去"报过")。只写 reports/{date}-premarket.md
和 latest-premarket.json。

门控: 美东工作日 04:00-09:25 且 NYSE 当天开市 (scanner.expected_report_modes
的交易所日历) 才跑, 否则 exit 3。--force 跳过门控, 报告名加 -manual。

cron (droplet 时钟 = 布里斯班, 无夏令时): 一条就够 ——
  0 22 * * 1-5  ...  # = 07:00 EST (美国冬令时) / 08:00 EDT (美国夏令时)
两个时点都落在 04:00-09:25 窗口内; 布里斯班周一至五 22:00 = 美东同一天,
星期过滤直接对齐。08:00 EDT 时盘前已有 4 小时成交, 07:00 EST 有 3 小时;
美东 06:00-08:00 发布的盘前财报 (BMO) 冬令时可能有一部分还没出。
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime

import pandas as pd
import yfinance as yf

import scanner as sc

PREMARKET_WINDOW = ((4, 0), (9, 25))


def load_dotenv(path=sc.BASE / ".env") -> None:
    """本地跑时读 .env (SEC_EMAIL 等); 已有的环境变量优先 (droplet 的
    run_scan.sh 自己会 source .env)。"""
    import os
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def premarket_quotes(symbols: list[str]) -> dict[str, dict | None]:
    """今天 (ET) 盘前 04:00-09:30 的 1m bar -> {last, high, low, volume, ts, bars}。
    没有盘前成交 (指数/冷门票) = None。"""
    df = yf.download(symbols, period="2d", interval="1m", prepost=True,
                     group_by="ticker", progress=False, threads=True,
                     auto_adjust=False)
    today = datetime.now(sc.ET).date()
    out: dict[str, dict | None] = {}
    for sym in symbols:
        try:
            sub = df[sym].dropna(subset=["Close"])
        except KeyError:
            out[sym] = None
            continue
        if sub.empty:
            out[sym] = None
            continue
        idx = sub.index.tz_convert(sc.ET) if sub.index.tz is not None \
            else sub.index.tz_localize(sc.ET)
        sub = sub.set_axis(idx)
        mins = idx.hour * 60 + idx.minute
        pm = sub[(idx.date == today) & (mins < 9 * 60 + 30)]
        if pm.empty:
            out[sym] = None
            continue
        out[sym] = {
            "last": float(pm["Close"].iloc[-1]),
            "high": float(pm["High"].max()),
            "low": float(pm["Low"].min()),
            "volume": int(pm["Volume"].sum()),
            "ts": pm.index[-1].to_pydatetime(),
            "bars": len(pm),
        }
    return out


def prev_close(hist: pd.DataFrame | None, today: date) -> tuple[float, str] | None:
    """最后一根**早于今天**的日线收盘 (Yahoo 盘前有时已预建今天的空行)。"""
    if hist is None:
        return None
    h = hist.dropna(subset=["Close"])
    h = h[h.index.date < today]
    if h.empty:
        return None
    return float(h["Close"].iloc[-1]), str(h.index[-1].date())


def fmt_vol(v: int) -> str:
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K" if v >= 1e3 else str(v)


def live_btc_price() -> float | None:
    """BTC-USD 最新 1m 价 (24/7, 盘前也是实时的)。取不到 = None, 模块退回日线。"""
    try:
        b = yf.Ticker("BTC-USD").history(period="1d", interval="1m")
        return float(b["Close"].dropna().iloc[-1]) if not b.empty else None
    except Exception:       # noqa: BLE001
        return None


def btc_metas(settings: dict, results, today: date):
    """mNAV + BTC 比值, 股价用**盘前价** (scanner 的 attach_* 用的是日线收盘, 盘前
    那是昨收), BTC 用实时价 —— 两边同一时点。只取数, 不写 *_history.csv
    (快照只归尾盘定时扫描); 模块自己的数据缓存照常更新。"""
    live = {r["symbol"]: r["pm"]["last"] for r in results if r.get("pm")}
    btc = live_btc_price()
    out = []
    for key, mod, kw in (
            ("mnav", sc.mnav, lambda c: dict(cache_path=sc.DATA / sc.mnav.CACHE_NAME,
                                             holidays=sc.NYSE_HOLIDAYS)),
            ("btcratio", sc.btcratio, lambda c: dict(cache_dir=sc.DATA))):
        cfgs = settings.get(key) or {}
        if not cfgs:
            out.append(None)
            continue
        try:
            out.append(mod.run(cfgs, today=today, live_btc=btc,
                               live_px={s_: live[s_] for s_ in cfgs if s_ in live},
                               **kw(cfgs)))
        except Exception as e:      # noqa: BLE001
            out.append({"by_symbol": {s_: {"error": f"{key} 模块异常: "
                                                     f"{type(e).__name__}: {e}"}
                                      for s_ in cfgs},
                        "cfgs": cfgs, "version": None, "timestamp": None,
                        "warnings": [], "from_cache": False, "fetched": None})
    return out[0], out[1]


def render(results, regime, now_et, s, insider_meta, shortside_meta=None,
           mnav_meta=None, btcratio_meta=None) -> str:
    d = now_et.strftime("%Y-%m-%d")
    lines = [f"# 盘前异动 — {d} ({now_et:%H:%M} ET)", "",
             "> 盘前价 = yfinance 1m prepost 最新成交 (Yahoo 盘前成交量恒为 0, 用有成交的分钟数衡量流动性); "
             "盘前流动性薄, gap 以开盘后为准。"
             "VIX 期限结构在盘前多半是昨日结算值。", ""]
    lines += sc.regime_block(regime)
    lines += ["", "## 盘前异动", ""]
    alerts = []
    for r in results:
        sym, pm, pc = r["symbol"], r.get("pm"), r.get("pc")
        t = r["tech"]
        if t is None or pc is None:
            continue
        px = pm["last"] if pm else None
        if pm:
            gap = (px / pc[0] - 1) * 100
            r["pm_gap_pct"] = gap
            if abs(gap) >= s["gap_alert_pct"]:
                alerts.append(f"- **{sym}** 盘前 {gap:+.1f}% (盘前 {px:.2f} vs 昨收 "
                              f"{pc[0]:.2f}, 盘前 {pm['bars']} 分钟有成交, "
                              f"区间 {pm['low']:.2f}-{pm['high']:.2f})")
        ref = px if px is not None else pc[0]
        ref_tag = "盘前" if px is not None else "昨收"
        zone = r["cfg"]["value_zone"]
        if r.get("zone_invalid"):
            alerts.append(f"- **{sym}** ⛔ {r['zone_invalid']} — value_zone 已作废, "
                          "重锚 (并更新 zone_asof) 后恢复")
        elif zone and ref < zone[0]:
            alerts.append(f"- **{sym}** {ref_tag}已在价值区下沿之下 ({zone[0]:g}-{zone[1]:g}, "
                          f"{ref_tag} {ref:.2f}) — 剧本: 检查论点是否失效, 不是按旧档继续摊")
        elif zone and ref <= zone[1]:
            alerts.append(f"- **{sym}** {ref_tag}在价值区内 ({zone[0]:g}-{zone[1]:g}, "
                          f"{ref_tag} {ref:.2f}) — 尾盘前核对 CSP 挂单/接货档位")
        if r.get("prev_state") in ("CONFIRMED", "TREND") and ref < t["sma20"]:
            alerts.append(f"- **{sym}** {ref_tag}在20日线下 ({t['sma20']:.2f}) — "
                          "右侧移动止损以**收盘**为准, 尾盘扫描确认")
        if r["earnings"]:
            days = (date.fromisoformat(r["earnings"]) - now_et.date()).days
            if 0 <= days <= s["earnings_alert_days"]:
                when = "今天" if days == 0 else f"{days} 天内"
                alerts.append(f"- **{sym}** 财报 {when} ({r['earnings']}) — "
                              "short option 不跨财报; LEAP 等财报后")
        elif r["earnings"] is None:
            alerts.append(f"- **{sym}** ⚠️ 财报日期取不到 — 出 short option 前手查")
    # 内部人新申报/失败行都在下面的内部人段里, 这里不重复
    lines += alerts or ["- 无异动 (盘前 gap/价值区/财报/内部人买入 均未触发)"]
    if insider_meta is not None:
        lines += [""] + sc.insider_block(results, insider_meta)
    ss = sc.shortside_block(results, shortside_meta, now_et.date())
    if ss:
        lines += [""] + ss
    for blk in (sc.mnav_block(mnav_meta), sc.btcratio_block(btcratio_meta)):
        if blk:
            # 模块把 live_px 标成"扫描器收盘"; 这里喂的是盘前价
            lines += [""] + [l.replace("(扫描器收盘)", "(盘前价)") for l in blk]

    lines += ["", "## 盘前一览", "",
              "| 标的 | 昨收 | 盘前 | 盘前% | 成交分钟 | 20日线 | 价值区 | 状态 |",
              "|---|---:|---:|---:|---:|---:|---|---|"]
    rows = sorted(results, key=lambda r: -abs(r.get("pm_gap_pct") or 0))
    for r in rows:
        t, pm, pc = r["tech"], r.get("pm"), r.get("pc")
        if t is None or pc is None:
            lines.append(f"| {r['symbol']} | — | — | — | — | — | — | {r['error'] or '无数据'} |")
            continue
        zone = r["cfg"]["value_zone"]
        lines.append(
            f"| {r['symbol']} | {pc[0]:.2f} | "
            + (f"{pm['last']:.2f} | {r['pm_gap_pct']:+.1f}% | {pm['bars']}"
               if pm else "— | — | —")
            + f" | {t['sma20']:.2f} | "
            + (f"{zone[0]:g}-{zone[1]:g}" if zone else "—")
            + f" | {r.get('prev_state') or '—'} |")
    no_pm = [r["symbol"] for r in results if r["tech"] is not None and not r.get("pm")]
    lines += ["",
              f"无盘前成交 ({len(no_pm)}): {', '.join(no_pm) or '无'} — 指数/冷门票盘前本就无价",
              "", "---",
              "盘前报告只做提醒, 不出期权票 (盘前期权无盘口)。信号与票据以尾盘扫描为准。非投资建议。"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="盘前扫描 (只读)")
    ap.add_argument("--force", action="store_true", help="跳过时间/开市门控")
    ap.add_argument("--tickers", help="逗号分隔子集 (测试)")
    ap.add_argument("--no-insider", action="store_true")
    ap.add_argument("--no-shortside", action="store_true",
                    help="跳过 FINRA 空头仓位 + IBKR 借券费")
    ap.add_argument("--no-btc", action="store_true",
                    help="跳过 mNAV ([mnav.*]) 与 BTC 比值 ([btcratio.*])")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()
    load_dotenv()

    now_et = datetime.now(sc.ET)
    d = now_et.strftime("%Y-%m-%d")
    manual = args.force or bool(args.tickers)
    if not args.force:
        if now_et.weekday() >= 5 or not sc.in_window(now_et, PREMARKET_WINDOW):
            print(f"outside premarket window ({now_et:%a %H:%M} ET) — skip")
            return sc.SKIP
        modes, why = sc.expected_report_modes(d)
        if not modes:
            print(f"NYSE 今天不开市 ({why}) — skip")
            return sc.SKIP
    report_path = sc.REPORTS / f"{d}-premarket{'-manual' if manual else ''}.md"
    if report_path.exists() and not args.force:
        print(f"{report_path.name} already exists — skip")
        return sc.SKIP

    settings, tickers = sc.load_config()
    if args.tickers:
        keep = {t.strip().upper() for t in args.tickers.split(",")}
        tickers = {k: v for k, v in tickers.items() if k in keep}
    symbols = list(tickers)
    print(f"premarket {d} {now_et:%H:%M} ET — {len(symbols)} tickers")

    regime = sc.fetch_regime(settings)
    hist = sc.batch_history(symbols)
    pms = premarket_quotes(symbols)
    state = sc.load_state()   # 只读

    results_by = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(sc.analyze_ticker, sym, cfg, hist.get(sym),
                            state.get(sym, {}), regime, settings, "open", False): sym
                for sym, cfg in tickers.items()}
        for fut in as_completed(futs):
            r = fut.result()
            results_by[r["symbol"]] = r
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(sc.next_earnings, yf.Ticker(sym)): sym for sym in symbols}
        for fut in as_completed(futs):
            sym = futs[fut]
            e = fut.result()
            if e is None and tickers[sym]["kind"] in ("etf", "index"):
                e = ""
            results_by[sym]["earnings"] = e
    results = [results_by[s_] for s_ in symbols]
    for r in results:
        r["pm"] = pms.get(r["symbol"])
        r["pc"] = prev_close(hist.get(r["symbol"]), now_et.date())
        # analyze_ticker 用的 prev_state 存在 state 里, 这里补回来做 20 日线提醒
        r["prev_state"] = state.get(r["symbol"], {}).get("state")

    insider_meta = None
    if not args.no_insider:
        insider_meta = sc.attach_insider(results, tickers, hist, now_et.date())
        # 刻意不调 insider.mark_seen: 盘前只预告, "已报过"留给正式报告
    shortside_meta = None
    if not args.no_shortside:
        # 只取数不落快照 (snapshot_shortside 只归尾盘定时扫描)
        shortside_meta = sc.attach_shortside(results, settings, now_et.date())

    mnav_meta = btcratio_meta = None
    if not args.no_btc:
        mnav_meta, btcratio_meta = btc_metas(settings, results, now_et.date())

    report = render(results, regime, now_et, settings, insider_meta, shortside_meta,
                    mnav_meta, btcratio_meta)
    sc.REPORTS.mkdir(exist_ok=True)
    report_path.write_text(report + "\n", encoding="utf-8")
    (sc.REPORTS / f"latest-premarket{'-manual' if manual else ''}.json").write_text(
        json.dumps({"generated_et": now_et.isoformat(), "regime": regime,
                    "results": [{k: v for k, v in r.items() if k != "cfg"}
                                for r in results]},
                   indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print()
    print(report)
    print(f"\nREPORT {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
