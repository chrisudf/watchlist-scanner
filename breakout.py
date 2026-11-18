#!/usr/bin/env python3
"""Qullamaggie 突破扫描 (收盘后, 全市场) — 独立脚本, 不改 scanner.py。

方法来源: qullamaggie.com《3 TIMELESS setups》的 Breakout:
  前段 1-3 月涨 30%+ → 有序整理 (收窄、沿 10/20MA) → 放量突破整理高点。
本脚本只管**缩小范围**: 每天收盘后对全美股 (~5500 只普通股) 做强度排名 + 形态
筛选, 出四个清单 + 自选股的强度排位, 邮寄一份日报。不进状态机/门控/票据。

为什么只做提示 (2026-11-18 组合回测, research/breakout_backtest.py):
  全市场逐日选股、单账户、无事后挑票, 规则机械执行 2016-2021 年化 ~20%, 2022-
  至今样本外 -5.5% (QQQ +11.3%)。13 个单参数变体无一在样本外跑赢 QQQ; 只有
  “提高流动性 / 突破须放量 / 严格大盘”三项在样本外改善 —— 本脚本默认就用这三项。
  详见 lesson.md「Qullamaggie 突破」。

清单:
  ① 今日放量突破: 昨天成形 + 今天收盘站上昨天的整理高点 + 量比 ≥ 1.5
     → 次日开盘可买, 止损 = 今天最低点 (不宽于 1×ADR)。给不能盯开盘的人用。
  ② 就绪: 成形且离整理高点 ≤ 1×ADR → 收盘后挂次日有效的 buy-stop。
  ③ 观察: 成形但离突破还远。
  ④ 差一条: 强势股里只差一个条件的。
  ⑤ 自选股强度排位: watchlist.toml 里的个股在全市场 1/3/6 月涨幅中的分位。

门控: 美东工作日 16:15-23:59 且 NYSE 当天开市 (含半日市) 才跑; 行情最后一根
不是今天 (Yahoo 还没更新日线) = exit 3, 留给下一次 cron 重试。--force 跳过门控,
报告名加 -manual, 日期取行情的最后交易日。

cron (droplet 时钟 = 布里斯班, 无夏令时), 两条做重试:
  10 7 * * 2-6  ... breakout   # = 16:10 EST (窗口外, skip) / 17:10 EDT
  40 7 * * 2-6  ... breakout   # = 16:40 EST / 17:40 EDT
布里斯班周二至六早上 = 美东周一至五傍晚, 所以是 2-6。
资源: 1 核 1GB 的 droplet 上峰值内存约 300MB; run_scan.sh 用 nice 19 跑。
"""

from __future__ import annotations

import argparse
import io
import os
import pickle
import sys
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

import scanner as sc

WINDOW = ((16, 15), (23, 59))
CACHE = sc.DATA / "breakout"
KEEP_CACHE_DAYS = 3

# 形态参数 (与 research/breakout_backtest.py、TradingView Pine 版同口径)
PATTERN = dict(
    prior_look=63,      # 前段涨幅回看 (日)
    min_prior=30.0,     # 前段最小涨幅 %
    base_len=10,        # 整理窗口 (日)
    pause_bars=2,       # 整理高点至少在 N 根之前 (有停顿)
    depth_k=3.0,        # 整理最大深度 ×ADR
    tight_k=2.0,        # 最近 5 日振幅上限 ×ADR
    min_adr=5.0,        # 最小 ADR %
)
# 选股门槛: 流动性与放量取回测样本外改善的那一档
SCREEN = dict(min_price=10.0, min_dolvol=30.0, vol_k=1.5, top=2.0,
              risk_pct=1.0, max_pos_pct=30.0)
WINDOWS = {"ret_1m": 21, "ret_3m": 63, "ret_6m": 126}
MIN_BARS = 130
# nasdaqtrader 交易所代码 → TradingView 前缀 (Arca 上市股在 TV 用 AMEX)
TV_EXCH = {"N": "NYSE", "A": "AMEX", "P": "AMEX", "Z": "CBOE", "V": "NYSE"}
NASDAQ_TRADER = "https://www.nasdaqtrader.com/dynamic/SymDir/"


# --------------------------------------------------------------------------
# 取数
# --------------------------------------------------------------------------

def parse_universe(nasdaq_txt: str, other_txt: str) -> pd.DataFrame:
    """nasdaqlisted.txt + otherlisted.txt → [symbol, name, tv_exch] 普通股。
    剔除 ETF、测试代码、权证/优先股/单位/权利/票据 (代码含 $.^ 或名称含关键词)。"""
    nas = pd.read_csv(io.StringIO(nasdaq_txt), sep="|", dtype=str)
    oth = pd.read_csv(io.StringIO(other_txt), sep="|", dtype=str)
    nas = nas[(nas["Test Issue"] == "N") & (nas["ETF"] == "N")]
    oth = oth[(oth["Test Issue"] == "N") & (oth["ETF"] == "N")]
    u = pd.concat([
        pd.DataFrame({"symbol": nas["Symbol"], "name": nas["Security Name"], "tv_exch": "NASDAQ"}),
        pd.DataFrame({"symbol": oth["ACT Symbol"], "name": oth["Security Name"],
                      "tv_exch": oth["Exchange"].map(TV_EXCH).fillna("NYSE")}),
    ]).dropna(subset=["symbol"])
    bad_name = r"Warrant|Preferred|Right|Unit|Depositary Shares|Notes? due|Debenture|% "
    u = u[~u["symbol"].str.contains(r"[\$\.\^ ]", regex=True)]
    u = u[~u["name"].fillna("").str.contains(bad_name, case=False, regex=True)]
    u = u[~u["symbol"].str.startswith("File Creation")]
    return u.drop_duplicates("symbol").reset_index(drop=True)


def load_universe() -> pd.DataFrame:
    import requests
    hdr = {"User-Agent": "Mozilla/5.0 (watchlist-scanner breakout)"}
    get = lambda f: requests.get(NASDAQ_TRADER + f, headers=hdr, timeout=30).text  # noqa: E731
    return parse_universe(get("nasdaqlisted.txt"), get("otherlisted.txt"))


def download_prices(symbols: list[str], cache: Path, chunk: int = 250) -> dict[str, pd.DataFrame]:
    """一年日线 (复权), 按 ET 日期缓存。缺得多 (Yahoo 限流) 就降速补一轮。"""
    import yfinance as yf
    if cache.exists():
        with open(cache, "rb") as f:
            return pickle.load(f)

    def fetch(batch):
        df = yf.download(batch, period="1y", interval="1d", auto_adjust=True,
                         group_by="ticker", threads=True, progress=False)
        got = {}
        for s in batch:
            try:
                d = df[s].dropna(subset=["Close"])
            except KeyError:
                continue
            if len(d) >= MIN_BARS:
                got[s] = d[["Open", "High", "Low", "Close", "Volume"]].astype("float32")
        return got

    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk):
        print(f"  下载 {i + 1}-{min(i + chunk, len(symbols))} / {len(symbols)}", flush=True)
        out.update(fetch(symbols[i:i + chunk]))
    missing = [s for s in symbols if s not in out]
    # 正常有 ~200 只新股/停牌拿不满 130 根; 缺上千只多半是被限流, 慢一点再补
    if len(missing) > 0.25 * len(symbols):
        print(f"  缺 {len(missing)} 只, 疑似限流 — 60s 后小批量补一轮", flush=True)
        time.sleep(60)
        for i in range(0, len(missing), 100):
            out.update(fetch(missing[i:i + 100]))
            time.sleep(2)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache, "wb") as f:
        pickle.dump(out, f)
    for old in sorted(cache.parent.glob("prices_*.pkl"))[:-KEEP_CACHE_DAYS]:
        old.unlink(missing_ok=True)
    return out


# --------------------------------------------------------------------------
# 纯计算 (test_signals.py 覆盖)
# --------------------------------------------------------------------------

def metrics(d: pd.DataFrame, p: dict = PATTERN) -> dict | None:
    """单只股票截至最后一根 K 线的指标。不足 MIN_BARS 根返回 None。"""
    if len(d) < MIN_BARS:
        return None
    h, l, c, v = (d[k].astype("float64") for k in ("High", "Low", "Close", "Volume"))
    last = float(c.iloc[-1])
    adr = 100 * ((h / l).rolling(20).mean().iloc[-1] - 1)
    bl = p["base_len"]
    win_h = h.iloc[-bl:].to_numpy()
    base_high, base_low = float(win_h.max()), float(l.iloc[-bl:].min())
    ma20 = c.rolling(20).mean()
    avg_v = float(v.iloc[-51:-1].mean())
    m = dict(
        date=d.index[-1].date(), close=round(last, 2), low=round(float(l.iloc[-1]), 2),
        adr=round(float(adr), 2),
        dolvol_m=round(float((c * v).rolling(20).mean().iloc[-1]) / 1e6, 1),
        base_high=round(base_high, 2),
        hi_ago=int(np.argmax(win_h[::-1])),            # 整理高点距今几根, 同值取最近
        depth=round(100 * (base_high - base_low) / base_high, 1),
        range5=round(100 * float(h.iloc[-5:].max() - l.iloc[-5:].min()) / last, 1),
        prior_gain=round(100 * (base_high / float(l.iloc[-p["prior_look"]:].min()) - 1), 1),
        ma10=round(float(c.rolling(10).mean().iloc[-1]), 2),
        ma20=round(float(ma20.iloc[-1]), 2),
        ma20_rising=bool(ma20.iloc[-1] > ma20.iloc[-6]),
        dist_to_bo=round(100 * (base_high / last - 1), 2),
        vol_ratio=round(float(v.iloc[-1]) / avg_v, 2) if avg_v > 0 else float("nan"),
    )
    for k, n in WINDOWS.items():
        m[k] = round(100 * (last / float(c.iloc[-1 - n]) - 1), 1)
    return m


def check_setup(r, p: dict = PATTERN) -> list[str]:
    """未通过的形态条件; 空 = 成形。"""
    fail = []
    if r["prior_gain"] < p["min_prior"]:
        fail.append("前段涨幅不足")
    if r["adr"] < p["min_adr"]:
        fail.append("ADR偏低")
    if r["hi_ago"] < p["pause_bars"]:
        fail.append("刚创新高未整理")
    if r["depth"] > p["depth_k"] * r["adr"]:
        fail.append("整理太深")
    if r["range5"] > p["tight_k"] * r["adr"]:
        fail.append("近5日未收窄")
    if not (r["close"] >= r["ma20"] and r["ma20_rising"]):
        fail.append("不在上升20MA之上")
    if r["close"] >= r["base_high"]:
        fail.append("已突破")
    return fail


def rank_universe(px: dict[str, pd.DataFrame], top: float = SCREEN["top"]) -> pd.DataFrame:
    """截面: 指标 + 流动性 + 强度 (1/3/6 月涨幅任一进入流动性股票的前 top%)。
    只保留最后一根落在众数日期上的 (剔除停牌/数据过期)。"""
    rows = []
    for s, d in px.items():
        m = metrics(d)
        if m:
            rows.append({"symbol": s, **m})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df[df["date"] == df["date"].mode().iloc[0]].copy()
    df["liquid"] = (df.close >= SCREEN["min_price"]) & (df.dolvol_m >= SCREEN["min_dolvol"])
    df["strong"] = False
    for k in WINDOWS:
        pct = df[k].where(df.liquid).rank(pct=True) * 100
        df[k + "_pct"] = pct.round(1)
        df["strong"] |= df.liquid & (pct >= 100 - top)
    fails = df.apply(check_setup, axis=1)
    df["fails"] = fails.map(lambda f: "、".join(f))
    df["n_fail"] = fails.map(len)
    df["setup"] = df.strong & (df.n_fail == 0)
    df["ready"] = df.setup & (df.dist_to_bo <= df.adr)
    return df.reset_index(drop=True)


def breakouts(today: pd.DataFrame, prev: pd.DataFrame, vol_k: float = SCREEN["vol_k"]) -> pd.DataFrame:
    """昨天成形 (prev.setup) 且今天收盘站上昨天的整理高点、量比 ≥ vol_k。"""
    if today.empty or prev.empty:
        return today.iloc[0:0].assign(bo_px=[])
    p = prev.loc[prev.setup, ["symbol", "base_high"]].rename(columns={"base_high": "bo_px"})
    bo = today.merge(p, on="symbol")
    return bo[(bo.close > bo.bo_px) & (bo.vol_ratio >= vol_k)].copy()


def plan(df: pd.DataFrame, entry: pd.Series, stop: pd.Series, account: float) -> pd.DataFrame:
    """1% 风险定股数, 单票 ≤ 30% 账户。"""
    df = df.copy()
    df["entry_px"] = entry.round(2).to_numpy()
    df["stop_px"] = stop.round(2).to_numpy()
    risk = (df.entry_px - df.stop_px).clip(lower=0.01)
    sh = np.minimum(account * SCREEN["risk_pct"] / 100 / risk,
                    account * SCREEN["max_pos_pct"] / 100 / df.entry_px)
    df["shares"] = np.floor(sh).astype(int)
    df["pos_pct"] = (df.shares * df.entry_px / account * 100).round(1)
    df["stop_pct"] = (100 * risk / df.entry_px).round(1)
    return df


def market_light(qqq: pd.Series) -> tuple[str, str]:
    """QQQ 收盘序列 → (灯, 说明)。10MA>20MA 且收盘>50MA 绿; 只满足一条黄; 都不满足红。"""
    last = float(qqq.iloc[-1])
    m10, m20, m50 = (float(qqq.rolling(n).mean().iloc[-1]) for n in (10, 20, 50))
    trend, above = m10 > m20, last > m50
    light = "🟢 绿灯" if trend and above else ("🟡 黄灯" if trend or above else "🔴 红灯")
    text = (f"QQQ {last:.2f} ｜ 10MA {m10:.2f} {'>' if trend else '<'} 20MA {m20:.2f} ｜ "
            f"{'高于' if above else '低于'} 50MA {m50:.2f}")
    return light, text


def watchlist_ranks(df: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """自选个股在流动性股票里的 1/3/6 月涨幅分位 (不流动的也按同一分布定位)。"""
    liq = df[df.liquid]
    rows = []
    for s in symbols:
        hit = df[df.symbol == s]
        if hit.empty:
            continue
        r = hit.iloc[0]
        row = {"symbol": s, "close": r.close, "ret_3m": r.ret_3m}
        for k in WINDOWS:
            row[k + "_pct"] = round(float((liq[k] < r[k]).mean() * 100), 0)
        # 自选多是大票, 逐条列不过的条件是噪声; 只说成没成形、差几条 (强势股才列具体哪条)
        row["setup"] = ("✅ 成形" if r.n_fail == 0 else
                        f"差 1 条: {r.fails}" if r.n_fail == 1 else f"差 {r.n_fail} 条")
        if r.strong:
            row["setup"] = "⭐ 前 2% ｜ " + row["setup"]
        rows.append(row)
    return pd.DataFrame(rows).sort_values("ret_3m_pct", ascending=False) if rows else pd.DataFrame()


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def md_table(df: pd.DataFrame, cols: dict[str, str]) -> str:
    d = df[list(cols)].rename(columns=cols)
    head = "| " + " | ".join(d.columns) + " |\n|" + "---|" * len(d.columns) + "\n"
    return head + "\n".join("| " + " | ".join(str(x) for x in row) + " |"
                            for row in d.itertuples(index=False)) + "\n"


LIGHT_HINT = {
    "🟢": "趋势健康: 可以按计划开仓。",
    "🟡": "趋势一般: 少开仓, 只做形态最好且放量的 —— 回测里这种时候假突破明显增多。",
    "🔴": "趋势走弱: Qullamaggie 此时基本空仓观望; 清单只作观察, 不建议开新仓。",
}


def render(asof: date, light: str, mtext: str, funnel: dict, df: pd.DataFrame,
           bo: pd.DataFrame, wl: pd.DataFrame, account: float) -> str:
    setups = df[df.setup].sort_values("dist_to_bo")
    ready, watch = setups[setups.ready], setups[~setups.ready]
    near = df[df.strong & (df.n_fail == 1)].sort_values("ret_3m", ascending=False)
    L = [f"# Qullamaggie 突破扫描 · {asof}", "",
         f"## 大盘 {light}", "", mtext, "", LIGHT_HINT[light[0]], "",
         f"股票池 {funnel['universe']} → 有行情 {funnel['priced']} → 流动性 (≥${SCREEN['min_price']:.0f}、"
         f"日均成交额 ≥${SCREEN['min_dolvol']:.0f}M) {funnel['liquid']} → 强度前 {SCREEN['top']:g}% "
         f"{funnel['strong']} → 成形 {len(setups)} (就绪 {len(ready)})", "",
         f"股数按账户 ${account:,.0f}、单笔风险 {SCREEN['risk_pct']:g}%、单票 ≤{SCREEN['max_pos_pct']:g}% 计算。", ""]

    L += ["## ① 今日放量突破 (收盘确认)", "",
          f"昨天成形、今天收盘站上整理高点、量比 ≥ {SCREEN['vol_k']:g}。次日开盘可买; "
          "止损 = 今天最低点 (不宽于 1×ADR)。", ""]
    if len(bo):
        bo = plan(bo, bo.close, np.maximum(bo.low, bo.close * (1 - bo.adr / 100)), account)
        L.append(md_table(bo.sort_values("vol_ratio", ascending=False),
                          {"symbol": "代码", "close": "收盘", "bo_px": "突破价", "vol_ratio": "量比",
                           "stop_px": "止损", "stop_pct": "止损距离%", "shares": "股数",
                           "pos_pct": "仓位%", "adr": "ADR%", "ret_3m": "3月%"}))
    else:
        L.append("今天没有。\n")

    L += ["## ② 就绪: 明天可挂突破买单", "",
          "离整理高点 ≤ 1×ADR。挂次日有效的 buy-stop 于突破价并带止损; "
          f"突破当天收盘量比 < {SCREEN['vol_k']:g} 的, 回测显示应收盘前卖出。", ""]
    if len(ready):
        r = plan(ready, ready.base_high + 0.01, (ready.base_high + 0.01) * (1 - ready.adr / 100), account)
        L.append(md_table(r, {"symbol": "代码", "close": "收盘", "entry_px": "买入止损价",
                              "stop_px": "止损(最宽)", "stop_pct": "止损距离%", "shares": "股数",
                              "pos_pct": "仓位%", "dist_to_bo": "离突破%", "adr": "ADR%",
                              "ret_3m": "3月%", "depth": "整理深度%"}))
    else:
        L.append("今天没有。\n")

    L += ["## ③ 观察: 成形但离突破还远", ""]
    L.append(md_table(watch, {"symbol": "代码", "close": "收盘", "base_high": "整理高点",
                              "dist_to_bo": "离突破%", "adr": "ADR%", "ret_3m": "3月%",
                              "depth": "整理深度%"}) if len(watch) else "今天没有。\n")

    L += ["## ④ 差一条就成形的强势股", ""]
    L.append(md_table(near.head(20), {"symbol": "代码", "close": "收盘", "ret_3m": "3月%",
                                      "adr": "ADR%", "fails": "差在哪"}) if len(near) else "没有。\n")

    L += ["## ⑤ 自选个股的强度排位", "",
          "在流动性股票里的涨幅分位 (100 = 最强)。≥98 即进入强度前 2%。", ""]
    L.append(md_table(wl, {"symbol": "代码", "close": "收盘", "ret_1m_pct": "1月分位",
                           "ret_3m_pct": "3月分位", "ret_6m_pct": "6月分位", "setup": "形态"})
             if len(wl) else "(未读到 watchlist.toml 个股)\n")

    L += ["---", "*只用于缩小范围: 规则机械执行在 2022 年后样本外跑输 QQQ (research/"
          "breakout_backtest.py, lesson.md)。形态质量、是否交易需自己在图上判断。*"]
    return "\n".join(L)


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Qullamaggie 突破扫描 (收盘后, 全市场)")
    ap.add_argument("--force", action="store_true", help="跳过时间/开市/数据日期门控")
    ap.add_argument("--email", action="store_true", help="写盘后邮寄 (同 scanner 的 env)")
    ap.add_argument("--account", type=float, default=100_000, help="账户规模 $, 只用于算股数")
    ap.add_argument("--limit", type=int, help="只取前 N 只股票 (测试)")
    args = ap.parse_args()

    now_et = datetime.now(sc.ET)
    d = now_et.strftime("%Y-%m-%d")
    if not args.force:
        if now_et.weekday() >= 5 or not sc.in_window(now_et, WINDOW):
            print(f"outside breakout window ({now_et:%a %H:%M} ET) — skip")
            return sc.SKIP
        modes, why = sc.expected_report_modes(d)
        if not modes:
            print(f"NYSE 今天不开市 ({why}) — skip")
            return sc.SKIP
        rp = sc.REPORTS / f"{d}-breakout.md"
        if rp.exists():
            if args.email and not sc._sent_marker(rp).exists():
                return send(rp, f"[watchlist] {d} breakout (补发)", now_et)
            print(f"{rp.name} already exists — skip")
            return sc.SKIP

    print("1/3 股票池 ...", flush=True)
    u = load_universe()
    syms = u.symbol.tolist()[: args.limit] if args.limit else u.symbol.tolist()
    print(f"    普通股 {len(syms)} 只", flush=True)
    print("2/3 一年日线 ...", flush=True)
    px = download_prices(syms, CACHE / f"prices_{d}{'-limit' if args.limit else ''}.pkl")
    print(f"    有效行情 {len(px)} 只", flush=True)

    print("3/3 排名与形态 ...", flush=True)
    df = rank_universe(px).merge(u, on="symbol", how="left")
    if df.empty:
        print("没有可用行情", file=sys.stderr)
        return 1
    asof = df.date.iloc[0]
    if not args.force and str(asof) != d:
        print(f"行情最后一根是 {asof}, 不是今天 {d} (Yahoo 日线还没更新) — skip, 等下一次 cron")
        return sc.SKIP
    prev = rank_universe({s: x.iloc[:-1] for s, x in px.items()})
    bo = breakouts(df, prev)

    import yfinance as yf
    q = yf.download("QQQ", period="1y", auto_adjust=True, progress=False)["Close"].squeeze()
    light, mtext = market_light(q.loc[:pd.Timestamp(asof)])

    wl_syms = []
    try:
        _, tickers = sc.load_config()
        wl_syms = [s for s, c in tickers.items() if c.get("kind") == "stock"]
    except Exception as e:        # noqa: BLE001 — 自选只是附加段, 读不到不影响主报告
        print(f"watchlist.toml 读取失败, 跳过 ⑤: {e}", file=sys.stderr)
    wl = watchlist_ranks(df, wl_syms)

    funnel = dict(universe=len(syms), priced=len(df), liquid=int(df.liquid.sum()),
                  strong=int(df.strong.sum()))
    report = render(asof, light, mtext, funnel, df, bo, wl, args.account)

    manual = args.force or bool(args.limit)
    stem = f"{asof}-breakout{'-manual' if manual else ''}"
    sc.REPORTS.mkdir(exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    rp = sc.REPORTS / f"{stem}.md"
    rp.write_text(report + "\n", encoding="utf-8")
    keep = ["symbol", "name", "tv_exch", "close", "base_high", "dist_to_bo", "vol_ratio", "adr",
            "dolvol_m", "ret_1m", "ret_3m", "ret_6m", "ret_3m_pct", "prior_gain", "depth",
            "range5", "hi_ago", "setup", "ready", "fails"]
    df.loc[df.strong, keep].sort_values(["setup", "ret_3m"], ascending=False).to_csv(
        CACHE / f"{stem}.csv", index=False, encoding="utf-8-sig")
    tv = list(dict.fromkeys((bo.tv_exch + ":" + bo.symbol).tolist()
                            + (df[df.setup].tv_exch + ":" + df[df.setup].symbol).tolist()))
    (CACHE / f"{stem}-tradingview.txt").write_text(",".join(tv), encoding="utf-8")

    print()
    print(report)
    print(f"\nREPORT {rp}")
    if args.email:
        subject = (f"[watchlist] {asof} breakout{' manual' if manual else ''} — {light} "
                   f"突破 {len(bo)} / 就绪 {int(df.ready.sum())}")
        return send(rp, subject, now_et)
    return 0


def send(rp: Path, subject: str, now_et: datetime) -> int:
    try:
        sc.send_email_report(rp, subject)
        sc._sent_marker(rp).write_text(now_et.isoformat(), encoding="utf-8")
        print(f"email sent to {os.environ.get('SCAN_EMAIL_TO')}")
        return 0
    except Exception as e:        # noqa: BLE001
        print(f"EMAIL FAILED: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
