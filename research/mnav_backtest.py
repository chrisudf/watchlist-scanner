"""MSTR mNAV 回测: 跌破 0.8/0.7/0.6 的次数、按入场 mNAV 分桶的后续回报、首次破位事件、
BTC 趋势过滤、企业价值平价 (todo.md #11; mnav.py 文档里的数字全部出自这里)。

运行: .venv/bin/python research/mnav_backtest.py            (联网, 全量文件 ~20MB, 约 10 秒)
第六节 = 把 mnav.py 的买入/卖出信号逐日回放, 数近 2 年和全历史各触发几次、结果如何。
      .venv/bin/python research/mnav_backtest.py --cache    (只用 data/mnav_strategytracker.json)

数据: data.strategytracker.com 公开 JSON (= saylortracker / mstr-tracker 同一产品) 的
MSTR historicalData: 日历日的股价 / BTC / 持币 / 基本与稀释股本 / 两个权益 mNAV,
2020-08-11 起。统计只数交易日 (周一到周五, 扣 NYSE 假日表)。

企业价值 mNAV 网站没有, 这里用 SEC XBRL (companyfacts CIK0001050446: LongTermDebt /
TemporaryEquityLiquidationPreference / Cash) + 发行公告日期拼的锚点, 2026-10-16 以官网
首页数字收口。锚点就是下面三张表, 改数字直接改表。误差 ±0.03 (2026 Q2 可转债减少
$1.5B 的确切日期不详, 取 6/1; 取 4/1 或 6/30 只改 <1.0 的天数 9↔10)。

样本只有 2022 和 2026 两个 regime —— 所有"胜率"按两次经验读, 不是统计显著。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mnav                                        # noqa: E402
import scanner as sc                               # noqa: E402  (只借 NYSE_HOLIDAYS)

pd.set_option("display.width", 230)
pd.set_option("display.max_colwidth", 90)

# 可转债面值 $B, 按发行/赎回日期逐段 (面值; 8-K / 10-Q)
DEBT_EVENTS = [
    ("2020-08-01", 0.0), ("2020-12-11", 0.65), ("2021-02-19", 1.70), ("2021-06-14", 2.20),
    ("2022-03-29", 2.405), ("2023-03-24", 2.20), ("2024-03-08", 3.00), ("2024-03-18", 3.60375),
    ("2024-06-17", 4.40375), ("2024-09-19", 5.41375), ("2024-09-26", 4.91375),
    ("2024-11-21", 7.91375), ("2025-01-02", 7.26375), ("2025-02-20", 9.26375),
    ("2025-02-24", 8.21375), ("2026-06-01", 6.71), ("2026-10-16", 6.714)]
# 优先股清算价值 $B, 锚点间线性插值 (ATM 是逐日卖的): 10-Q TemporaryEquityLiquidationPreference
# (2025-12-31 8.03 / 2026-03-31 10.00 / 2026-06-30 15.46), 更早按发行规模, 10/16 官网 Pref
PREF_ANCHORS = [
    ("2020-08-01", 0), ("2025-02-04", 0), ("2025-02-05", 0.73), ("2025-03-24", 0.73),
    ("2025-03-25", 1.58), ("2025-06-09", 1.70), ("2025-06-10", 2.88), ("2025-07-28", 3.20),
    ("2025-07-29", 6.00), ("2025-09-30", 6.70), ("2025-12-31", 8.03), ("2026-03-31", 10.00),
    ("2026-06-30", 15.46), ("2026-10-16", 13.748)]
# USD Reserve + USD Cash $B: 2025 年底前忽略 (<0.1), 之后 10-Q 现金+AFS 与 8-K 公告
CASH_ANCHORS = [
    ("2020-08-01", 0.05), ("2025-09-30", 0.054), ("2025-12-31", 2.30), ("2026-03-31", 2.21),
    ("2026-06-30", 2.45), ("2026-08-09", 2.45), ("2026-08-10", 4.65), ("2026-08-21", 5.10),
    ("2026-09-21", 5.043), ("2026-10-16", 5.518)]
BUCKETS = [0, 0.7, 0.8, 0.9, 1.0, 1.2, 1.5, 2.0, 99]
BUCKET_LABELS = ["<0.7", "0.7-0.8", "0.8-0.9", "0.9-1.0", "1.0-1.2", "1.2-1.5", "1.5-2.0", ">2.0"]


def load(use_cache: bool) -> pd.DataFrame:
    cache = Path(__file__).resolve().parents[1] / "data" / mnav.CACHE_NAME
    if use_cache:
        import json
        data = json.loads(cache.read_text(encoding="utf-8"))
    else:
        data = mnav.fetch_companies(["MSTR"], cache)
    print(f"StrategyTracker 版本 {data.get('version')} 时间 {data.get('timestamp')}"
          + (" (缓存)" if data.get("from_cache") else ""))
    df = mnav.frame(data["companies"]["MSTR"]["historicalData"])
    return mnav.trading_rows(df, sc.NYSE_HOLIDAYS)


def step(events, idx):
    s = pd.Series({pd.Timestamp(a): b for a, b in events})
    return s.reindex(idx.union(s.index)).sort_index().ffill().reindex(idx)


def interp(anchors, idx):
    s = pd.Series({pd.Timestamp(a): float(b) for a, b in anchors})
    return s.reindex(idx.union(s.index)).sort_index().interpolate(method="time").reindex(idx)


def add_ev(t: pd.DataFrame) -> pd.DataFrame:
    t = t.copy()
    t["debt_b"] = step(DEBT_EVENTS, t.index).values
    t["pref_b"] = interp(PREF_ANCHORS, t.index).values
    t["cash_b"] = interp(CASH_ANCHORS, t.index).values
    t["btc_nav_b"] = t.btc_balance * t.btc_prices / 1e9
    t["claims_b"] = t.debt_b + t.pref_b - t.cash_b
    t["m_ev"] = (t.stock_prices * t.total_outstanding_shares / 1e9 + t.claims_b) / t.btc_nav_b
    return t


def clusters(mask, gap=mnav.EPISODE_GAP):
    return len(mnav.episodes(mask, gap))


def forward(t, h):
    P, B = t.stock_prices, t.btc_prices
    f = pd.DataFrame({"rM": P.shift(-h) / P - 1, "rB": B.shift(-h) / B - 1})
    f["rel"] = (1 + f.rM) / (1 + f.rB) - 1
    return f


def mae60(t):
    P = t.stock_prices
    return pd.Series([P.iloc[i + 1:i + 61].min() / P.iloc[i] - 1 if i + 1 < len(P) else np.nan
                      for i in range(len(P))], index=P.index)


def section(title):
    print(f"\n=== {title} ===")


def table_thresholds(t):
    section("一、跌破阈值的交易日数 / 段数 (中间断开 ≤5 个交易日算同一段)")
    rows = []
    for col, label in (("nav_premium_diluted", "稀释权益"), ("nav_premium_basic", "基本权益"),
                       ("m_ev", "企业价值 (复原)")):
        st = mnav.threshold_stats(t[col])
        r = {"口径": label, "最低": f"{t[col].min():.3f} @ {t[col].idxmin().date()}"}
        for thr in mnav.STATS_THRESHOLDS:
            r[f"<{thr:g}"] = f"{st[thr]['days']} 天/{st[thr]['episodes']} 段"
        rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False))
    st = mnav.threshold_stats(t.nav_premium_diluted)
    for thr in (0.8, 0.7, 0.6):
        print(f"  稀释 <{thr:g}: " + "; ".join(f"{a}→{b} ({n}d)" for a, b, n in st[thr]["runs"]))


def table_buckets(t, col, title, era=None):
    section(title)
    cat = pd.cut(t[col], BUCKETS, labels=BUCKET_LABELS)
    fw = {h: forward(t, h) for h in (20, 60)}
    m60 = mae60(t)
    rows = []
    for lab in BUCKET_LABELS:
        mask = cat == lab
        if era:
            mask &= (t.index >= era[0]) & (t.index <= era[1])
        for h in (20, 60):
            f = fw[h][mask].dropna()
            if len(f) < 1:
                continue
            rows.append({"桶": lab, "h": h, "天数": len(f),
                         "段": clusters(mask & fw[h].rM.notna()),
                         "MSTR中位%": f.rM.median() * 100, "胜率%": (f.rM > 0).mean() * 100,
                         "p10%": f.rM.quantile(.1) * 100, "BTC中位%": f.rB.median() * 100,
                         "相对BTC中位%": f.rel.median() * 100,
                         "相对胜率%": (f.rel > 0).mean() * 100,
                         "60d内最大回撤中位%": m60[mask].median() * 100 if h == 60 else np.nan})
    print(pd.DataFrame(rows).round(1).to_string(index=False))


def first_cross_down(s, thr, lookback=20):
    below = s < thr
    return [s.index[i] for i in range(lookback, len(s))
            if below.iloc[i] and not below.iloc[i - lookback:i].any()]


def first_cross_up(s, thr, lookback=5):
    below = s < thr
    return [s.index[i] for i in range(lookback, len(s))
            if (not below.iloc[i]) and below.iloc[i - lookback:i].all()]


def days_until(s, start, cond):
    i = s.index.get_loc(start)
    for j in range(i + 1, len(s)):
        if cond(s.iloc[j]):
            return j - i
    return None


def table_events(t):
    section("二、事件: 首次收盘跌破阈值 (之前 ≥20 个交易日在上方), 稀释口径")
    m, P, B = t.nav_premium_diluted, t.stock_prices, t.btc_prices
    fw = {h: forward(t, h) for h in (20, 40, 60)}
    m60 = mae60(t)
    rows = []
    for thr in (0.9, 0.8, 0.7):
        for d in first_cross_down(m, thr):
            i = m.index.get_loc(d)
            rows.append({"事件": f"破{thr:g}", "日期": d.date(), "稀释": m.iloc[i],
                         "EV": t.m_ev.iloc[i], "MSTR": P.iloc[i], "BTC": B.iloc[i],
                         "20d%": fw[20].rM.iloc[i] * 100, "40d%": fw[40].rM.iloc[i] * 100,
                         "60d%": fw[60].rM.iloc[i] * 100, "BTC60d%": fw[60].rB.iloc[i] * 100,
                         "60d内最大回撤%": m60.iloc[i] * 100,
                         "60d内最低mNAV": m.iloc[i + 1:i + 61].min(),
                         "回到1.0天数": days_until(m, d, lambda x: x >= 1.0)})
    print(pd.DataFrame(rows).round(2).to_string(index=False))
    section("二b、右侧: 从下方重新站上阈值 (之前 ≥5 个交易日在下方)")
    rows = []
    for thr in (0.8, 0.9):
        for d in first_cross_up(m, thr):
            i = m.index.get_loc(d)
            rows.append({"事件": f"站上{thr:g}", "日期": d.date(), "稀释": m.iloc[i],
                         "MSTR": P.iloc[i], "20d%": fw[20].rM.iloc[i] * 100,
                         "60d%": fw[60].rM.iloc[i] * 100, "60d内最大回撤%": m60.iloc[i] * 100})
    print(pd.DataFrame(rows).round(2).to_string(index=False))


def table_btc_filter(t):
    section("三、BTC 趋势过滤 (稀释 <0.85 的日子; 均线按交易日近似)")
    B = t.btc_prices
    low = t.nav_premium_diluted < 0.85
    fw = {h: forward(t, h) for h in (20, 60)}
    for name, flt in (("BTC>20dma", B > B.rolling(20).mean()), ("BTC<20dma", B <= B.rolling(20).mean()),
                      ("BTC>50dma", B > B.rolling(50).mean()), ("BTC<50dma", B <= B.rolling(50).mean())):
        for h in (20, 60):
            f = fw[h][low & flt].dropna()
            if len(f):
                print(f"  {name:10s} h={h:3d} n={len(f):3d} 段={clusters(low & flt & fw[h].rM.notna())} "
                      f"MSTR 中位 {f.rM.median() * 100:6.1f}% 胜率 {(f.rM > 0).mean() * 100:5.1f}% "
                      f"p10 {f.rM.quantile(.1) * 100:6.1f}% | BTC 中位 {f.rB.median() * 100:6.1f}% "
                      f"| 相对 BTC {f.rel.median() * 100:6.1f}%")
    section("三b、基本口径 <0.8 的日子按之后 BTC 涨跌切开")
    low = t.nav_premium_basic < 0.8
    for h in (20, 60):
        f = fw[h][low].dropna()
        up, dn = f[f.rB > 0], f[f.rB <= 0]
        print(f"  h={h}: BTC 涨 (n={len(up)}) MSTR 中位 {up.rM.median() * 100:.1f}% 胜率 "
              f"{(up.rM > 0).mean() * 100:.0f}% | BTC 跌 (n={len(dn)}) MSTR 中位 "
              f"{dn.rM.median() * 100:.1f}% 胜率 {(dn.rM > 0).mean() * 100:.0f}% p10 "
              f"{dn.rM.quantile(.1) * 100:.1f}% | 相对 BTC 中位 {dn.rel.median() * 100:.1f}% "
              f"胜率 {(dn.rel > 0).mean() * 100:.0f}%")


def table_decomp(t):
    section("四、低点到反弹的收益拆分: dlnP = dln mNAV + dln BTC + dln(持币/股本)")
    P, m, B = t.stock_prices, t.nav_premium_diluted, t.btc_prices
    for a, b, lab in (("2022-05-11", "2022-08-15", "2022 低点→8 月反弹顶"),
                      ("2022-05-11", "2022-12-30", "2022 低点→年底 (BTC 继续跌)"),
                      ("2026-06-26", "2026-09-04", "2026 低点→9/4"),
                      ("2026-06-22", "2026-08-18", "2026 首破 0.8→40 天后")):
        if a not in P.index or b not in P.index:
            continue
        dP, dm, dB = np.log(P[b] / P[a]), np.log(m[b] / m[a]), np.log(B[b] / B[a])
        print(f"  {lab}: MSTR {np.exp(dP) - 1:+.1%} = mNAV {np.exp(dm) - 1:+.1%} "
              f"({m[a]:.2f}→{m[b]:.2f}) + BTC {np.exp(dB) - 1:+.1%} + BTC/股 {np.exp(dP - dm - dB) - 1:+.1%}")


def table_ev_floor(t):
    section("五、企业价值平价: 2026 各局部低点与 2022 低点的三口径")
    for d in ("2022-05-11", "2026-06-26", "2026-07-24", "2026-08-18", "2026-08-21"):
        if d in t.index:
            x = t.loc[d]
            print(f"  {d}: 稀释 {x.nav_premium_diluted:.3f} 基本 {x.nav_premium_basic:.3f} "
                  f"EV {x.m_ev:.3f} 净债权/BTC 市值 {x.claims_b / x.btc_nav_b:.0%}")
    last = t.iloc[-1]
    H, Sb, Sd, c = last.btc_balance, last.total_outstanding_shares, last.diluted_shares_outstanding, last.claims_b
    print(f"  今天净债权 ${c:.2f}B; 价格表 (平价 = (持币×BTC − 净债权)/基本股本):")
    rows = []
    for btc in (55000, 60000, 65000, 70000, 75000, last.btc_prices, 90000):
        rows.append({"BTC": f"{btc / 1000:.1f}k", "EV=1.0": mnav.parity_price(H, btc, c, Sb),
                     "EV=0.97": mnav.parity_price(H, btc, c, Sb, 0.97),
                     "稀释0.80": mnav.price_at(0.80, H, Sd, btc), "稀释0.70": mnav.price_at(0.70, H, Sd, btc),
                     "稀释0.63": mnav.price_at(0.63, H, Sd, btc),
                     "平价对应稀释": mnav.parity_price(H, btc, c, Sb) * Sd / (H * btc)})
    print(pd.DataFrame(rows).round(2).to_string(index=False))


def table_signals(t, since=None, label="全历史", cfg=None):
    """把 mnav.py 的买入/卖出信号逐日回放 (与 assess 同一套判据), 数触发次数和结果。
    企业价值线用本脚本的历史锚点 (live 代码用配置里的当前净债权)。
    买入: 现价落进 T1 (稀释 ≤ t1_dil 或 EV ≤ t1_ev) / T2 (稀释 ≤ t2_dil) / T3 (稀释 ≤ t3_dil) 档,
          以及右侧 "从下方重新站上 t1_dil" (之前 ≥ recross_min_below 个交易日在下方)。
    卖出提醒: 稀释 ≥ take_profit[0] 且近 tp_memory_days 个交易日到过 t1_dil 线。
    结果 = 入场后首个 稀释 ≥ 0.90 的收盘价了结。
    "入场" = 当天档位比前一天深 (T1 → T2 算一次 T2 入场); 同档 5 个交易日内重复触发合并。"""
    cfg = cfg or mnav.CFG_DEFAULTS
    t1d, t2d, t3d, t1e = cfg["t1_dil"], cfg["t2_dil"], cfg["t3_dil"], cfg["t1_ev"]
    tp, trap_hi, lb, mb = cfg["take_profit"][0], cfg["trap"][1], cfg["break_lookback"], cfg["recross_min_below"]
    m, ev, P, B = t.nav_premium_diluted, t.m_ev, t.stock_prices, t.btc_prices
    level = pd.Series(0, index=t.index)
    level[(m <= t1d) | (ev <= t1e)] = 1
    level[m <= t2d] = 2
    level[m <= t3d] = 3
    n = len(t)
    brk_trap, brk_t1, rec = [False] * n, [False] * n, [False] * n
    for i in range(n):
        prev = m.iloc[max(0, i - lb):i]
        if len(prev) == lb:
            brk_trap[i] = bool(m.iloc[i] < trap_hi and (prev >= trap_hi).all())
            brk_t1[i] = bool(m.iloc[i] < t1d and (prev >= t1d).all())
        prev5 = m.iloc[max(0, i - mb):i]
        if len(prev5) == mb:
            rec[i] = bool(m.iloc[i] >= t1d and (prev5 < t1d).all())
    recent_t1 = (m <= t1d).shift(1).rolling(cfg["tp_memory_days"], min_periods=1).max().fillna(0) > 0
    flags = pd.DataFrame({"level": level, "brk_trap": brk_trap, "brk_t1": brk_t1, "recross": rec,
                          "tp": (m >= tp) & recent_t1}, index=t.index)
    w = flags if since is None else flags[flags.index >= since]
    tw = t.loc[w.index]
    section(f"六、信号回放 ({label}: {w.index.min().date()} → {w.index.max().date()}, {len(w)} 个交易日)")
    print(f"  信号日数: T1+ {int((w.level >= 1).sum())} 天 (其中 T2+ {int((w.level >= 2).sum())}, T3 {int((w.level >= 3).sum())}) "
          f"| 🔔 止盈提醒 (稀释 ≥ {tp:g} 且近 {cfg['tp_memory_days']} 日到过 {t1d:g}) {int(w.tp.sum())} 天 | ⚠️ 首次破 {trap_hi:g} {int(w.brk_trap.sum())} 次 "
          f"| 首次破 {t1d:g} {int(w.brk_t1.sum())} 次 | 右侧重新站上 {t1d:g} {int(w.recross.sum())} 次")
    rows = []
    prev_level = level.shift(1).fillna(0)
    last_entry = {}
    for i, d in enumerate(w.index):
        gi = t.index.get_loc(d)
        kinds = []
        for k in (1, 2, 3):
            if level.loc[d] >= k and prev_level.loc[d] < k:
                if k in last_entry and gi - last_entry[k] <= mb:
                    continue
                last_entry[k] = gi
                kinds.append(f"T{k}")
        if w.recross.loc[d]:
            kinds.append("右侧站上0.8")
        for kind in kinds:
            fut = m.iloc[gi + 1:]
            hit = fut[fut >= tp]
            if len(hit):
                j = t.index.get_loc(hit.index[0])
                ret, held, exit_d = P.iloc[j] / P.iloc[gi] - 1, j - gi, hit.index[0].date()
                mae = P.iloc[gi + 1:j + 1].min() / P.iloc[gi] - 1
            else:
                j = n - 1
                ret, held, exit_d = P.iloc[j] / P.iloc[gi] - 1, j - gi, "未了结"
                mae = P.iloc[gi + 1:].min() / P.iloc[gi] - 1 if gi + 1 < n else float("nan")
            rows.append({"信号": kind, "日期": d.date(), "MSTR": P.iloc[gi], "稀释": m.iloc[gi], "EV": ev.iloc[gi],
                         "BTC": B.iloc[gi],
                         "20d%": (P.shift(-20) / P - 1).iloc[gi] * 100, "60d%": (P.shift(-60) / P - 1).iloc[gi] * 100,
                         "了结日(稀释≥0.9)": exit_d, "持有天": held, "了结%": ret * 100, "持有期最大回撤%": mae * 100})
    if rows:
        df = pd.DataFrame(rows)
        print(df.round(2).to_string(index=False))
        closed = df[df["了结日(稀释≥0.9)"] != "未了结"]
        if len(closed):
            print(f"  已了结 {len(closed)} 笔: 了结% 中位 {closed['了结%'].median():.1f}, 最差 {closed['了结%'].min():.1f}, "
                  f"胜率 {(closed['了结%'] > 0).mean() * 100:.0f}%, 持有天中位 {closed['持有天'].median():.0f}, "
                  f"持有期最大回撤中位 {closed['持有期最大回撤%'].median():.1f}%")
    else:
        print("  没有任何买入信号")
    bt = w[w.brk_trap]
    if len(bt):
        print("  ⚠️ 首次破 0.9 的日期: " + ", ".join(str(d.date()) for d in bt.index))


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", action="store_true", help="只用本地缓存, 不联网")
    args = ap.parse_args(argv)
    t = add_ev(load(args.cache))
    print(f"交易日 {len(t)} 行, {t.index.min().date()} → {t.index.max().date()}")
    table_thresholds(t)
    table_buckets(t, "nav_premium_diluted", "一b、按入场稀释 mNAV 分桶的后续回报 (全样本)")
    table_buckets(t, "m_ev", "一c、按入场企业价值 mNAV 分桶 (2022 与 2026 结构可比)")
    for era, lab in ((("2021-01-01", "2023-12-31"), "2021-23"), (("2025-01-01", "2026-12-31"), "2025-26")):
        table_buckets(t, "nav_premium_basic", f"一d、基本口径分桶, {lab}", era)
    table_events(t)
    table_btc_filter(t)
    table_decomp(t)
    table_ev_floor(t)
    table_signals(t, since=t.index.max() - pd.DateOffset(years=2), label="近 2 年")
    table_signals(t)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
