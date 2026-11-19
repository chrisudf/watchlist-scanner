#!/usr/bin/env python3
"""Qullamaggie 突破 — 全市场组合回测 + 参数敏感性 (breakout.py 的依据)。

和“挑几只票各回测一遍”的区别:
  * 每天只用当天及以前的数据对全市场排名、筛形态 —— 没有事后挑票
  * 一个账户一份钱: 信号多于现金就买不了, 按 3 月涨幅优先
规则 (与 breakout.py / TradingView Pine 版同口径):
  信号日收盘 → 次日在整理高点挂 buy-stop (跳空高开按开盘价成交, +$0.02 滑点, 0.05% 佣金)
  止损 = 进场日最低点, 不宽于 1×ADR (进场当天不触发, 与 Pine 一致)
  持有满 3 天次日开盘卖 33% 并上移保本; 之后收盘跌破 10MA 次日开盘清仓
  单笔风险 1% 权益, 单票 ≤ 30%, 不加杠杆
仍有的偏差: 股票池是“今天还在上市”的普通股, 期间退市/被收购的缺失 (幸存者偏差),
  免费数据去不掉, 结果偏乐观。

2026-11-18 结果 (样本内 2016-2021 / 样本外 2022-11-17, QQQ 同期 25.0% / 11.3%):
  基准 (大盘 10>20 过滤) 年化 20.1% / -5.5%, 13 个单参数变体样本外没有一个跑赢 QQQ;
  只按样本内选的最优组合 (20MA 止损 + 前段 ≥50%) 样本内 26.7% → 样本外 -5.4%, 典型过拟合。
  样本外改善的只有三项: 流动性 ≥$10/$30M、突破须放量 1.5 倍、严格大盘 —— breakout.py 默认用这三项。

需要 ~3GB 内存 (11 年 × ~5300 只), droplet 跑不动, 只在本机跑:
  python research/breakout_backtest.py                 # 基准回测, 出净值图/年度表/交易明细
  python research/breakout_backtest.py --sensitivity   # 13 个单参数变体 + 2 个组合
首次下载约 15 分钟, 缓存在 data/breakout/hist_panel.pkl。
"""
from __future__ import annotations

import argparse
import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import breakout as bk                              # noqa: E402

HIST_START = "2015-01-01"
CACHE = bk.CACHE / "hist_panel.pkl"
OUT = bk.CACHE / "backtest"
IS_END, OOS_START = "2021-12-31", "2022-01-01"
# 回测基准用当初的宽松流动性门槛, 让“提高流动性”能作为变体比较
BASE_P = {**bk.PATTERN, "min_price": 5.0, "min_dolvol": 10.0, "risk_pct": 1.0, "max_pos_pct": 30.0}


# ───────── 数据 ─────────
def load_hist(refresh: bool = False) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    if CACHE.exists() and not refresh:
        with open(CACHE, "rb") as f:
            return pickle.load(f)
    syms = bk.load_universe().symbol.tolist()
    parts: dict[str, list] = {k: [] for k in ("Open", "High", "Low", "Close", "Volume")}
    for i in range(0, len(syms), 200):
        print(f"  下载 {i + 1}-{min(i + 200, len(syms))} / {len(syms)}", flush=True)
        df = yf.download(syms[i:i + 200], start=HIST_START, auto_adjust=True, group_by="column",
                         threads=True, progress=False)
        for k in parts:
            parts[k].append(df[k].astype("float32"))
    pn = {k: pd.concat(v, axis=1) for k, v in parts.items()}
    keep = pn["Close"].notna().sum() >= 130
    pn = {k: v.loc[:, keep] for k, v in pn.items()}
    pn["bench"] = yf.download(["QQQ", "SPY"], start=HIST_START, auto_adjust=True,
                              progress=False)["Close"]
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE, "wb") as f:
        pickle.dump(pn, f)
    return pn


# ───────── 信号 (向量化, 只用当天及以前) ─────────
def build_signals(pn: dict, top: float, p: dict) -> dict:
    H, L, C, V = (pn[k].astype("float64") for k in ("High", "Low", "Close", "Volume"))
    adr = 100 * ((H / L).rolling(20).mean() - 1)
    dolvol = (C * V).rolling(20).mean() / 1e6
    ma10, ma20 = C.rolling(10).mean(), C.rolling(20).mean()
    bl, pb = p["base_len"], p["pause_bars"]
    base_high, base_low = H.rolling(bl).max(), L.rolling(bl).min()
    pause = H.shift(pb).rolling(bl - pb).max() > H.rolling(pb).max()   # = hi_ago >= pause_bars
    depth = 100 * (base_high - base_low) / base_high
    range5 = 100 * (H.rolling(5).max() - L.rolling(5).min()) / C
    prior = 100 * (base_high / L.rolling(p["prior_look"]).min() - 1)
    liq = (C >= p["min_price"]) & (dolvol >= p["min_dolvol"])
    strong = pd.DataFrame(False, index=C.index, columns=C.columns)
    for n in bk.WINDOWS.values():
        strong |= (C / C.shift(n) - 1).where(liq).rank(axis=1, pct=True) >= 1 - top / 100
    setup = (strong & liq & (prior >= p["min_prior"]) & (adr >= p["min_adr"]) & pause
             & (depth <= p["depth_k"] * adr) & (range5 <= p["tight_k"] * adr)
             & (C >= ma20) & (ma20 > ma20.shift(5)) & (C < base_high))
    return dict(setup=setup, base_high=base_high, adr=adr, ma10=ma10, ma20=ma20,
                ret3m=C / C.shift(63) - 1, avgvol=V.rolling(50).mean())


# ───────── 组合模拟 ─────────
def simulate(pn, sig, start="2016-01-01", risk_pct=1.0, mkt="ma", trail="ma10", vol_k=None,
             partial_pct=33.0, partial_days=3, max_pos_pct=30.0, capital=100_000.0,
             comm=0.0005, slip=0.02, max_orders=20):
    """mkt: none | ma (QQQ 10MA>20MA) | strict (再加 QQQ>50MA); trail: ma10 | ma20;
    vol_k: 进场日成交量 < vol_k×50 日均量则当天收盘卖出 (突破无量)。"""
    O, H, L, C, V = (pn[k].to_numpy("float64") for k in ("Open", "High", "Low", "Close", "Volume"))
    syms, dates = pn["Close"].columns.to_numpy(), pn["Close"].index
    SU, BH, AD, R3, AV = (sig[k].to_numpy() for k in ("setup", "base_high", "adr", "ret3m", "avgvol"))
    MA = sig[trail].to_numpy()
    q = pn["bench"]["QQQ"].reindex(dates).ffill()
    mkt_ok = np.ones(len(dates), bool)
    if mkt != "none":
        mkt_ok = (q.rolling(10).mean() > q.rolling(20).mean()).to_numpy()
        if mkt == "strict":
            mkt_ok = mkt_ok & (q > q.rolling(50).mean()).to_numpy()

    cash, pos, orders, trades, curve = capital, {}, [], [], []
    last_px = np.full(len(syms), np.nan)

    def sell(j, p, n, px, d, why):
        nonlocal cash
        amt = n * (px - slip) * (1 - comm)
        cash += amt
        p["proceeds"] += amt
        p["shares"] -= n
        if p["shares"] == 0:
            pnl = p["proceeds"] - p["cost"]
            trades.append(dict(symbol=syms[j], entry_date=p["d0"].date(), entry=round(p["fill"], 2),
                               stop0=round(p["stop0"], 2), shares=p["shares0"], exit_date=d.date(),
                               exit_reason=why, pnl=round(pnl, 2), days=(d - p["d0"]).days,
                               R=round(pnl / p["risk_usd"], 2) if p["risk_usd"] > 0 else np.nan))

    for i in range(int(np.searchsorted(dates, pd.Timestamp(start))), len(dates)):
        d, o, h, l, c = dates[i], O[i], H[i], L[i], C[i]
        last_px[~np.isnan(c)] = c[~np.isnan(c)]
        eq_prev = curve[-1][1] if curve else capital
        # 1) 昨收决定的开盘动作
        for j, p in list(pos.items()):
            if np.isnan(o[j]):
                continue
            if p["exit_next"]:
                sell(j, p, p["shares"], o[j], d, "跌破均线")
                del pos[j]
            elif p["partial_next"]:
                n = int(p["shares"] * partial_pct / 100)
                if n:
                    sell(j, p, n, o[j], d, "")
                p.update(partial_next=False, partial=True, stop=max(p["stop"], p["fill"]))
        # 2) 止损 (进场当天不查)
        for j, p in list(pos.items()):
            if np.isnan(l[j]):
                p["nan_days"] += 1
                if p["nan_days"] >= 5:
                    sell(j, p, p["shares"], last_px[j] + slip, d, "数据中断")
                    del pos[j]
                continue
            p["nan_days"] = 0
            tag = "保本止损" if p["partial"] else "止损"
            if o[j] <= p["stop"]:
                sell(j, p, p["shares"], o[j], d, tag + "(跳空)")
                del pos[j]
            elif l[j] <= p["stop"]:
                sell(j, p, p["shares"], p["stop"], d, tag)
                del pos[j]
        # 3) 昨天挂的 buy-stop
        for (j, bs, a) in orders:
            if j in pos or np.isnan(h[j]) or h[j] < bs:
                continue
            fill = max(o[j], bs) + slip
            sh = min(eq_prev * risk_pct / 100 / (bs * a / 100), eq_prev * max_pos_pct / 100 / fill)
            sh = int(min(sh, cash / (fill * (1 + comm))))
            if sh < 1:
                continue
            cost = sh * fill * (1 + comm)
            cash -= cost
            stop0 = max(l[j], fill * (1 - a / 100))
            pos[j] = dict(shares=sh, shares0=sh, fill=fill, stop=stop0, stop0=stop0, i0=i, d0=d,
                          cost=cost, proceeds=0.0, risk_usd=sh * (fill - stop0), partial=False,
                          partial_next=False, exit_next=False, nan_days=0)
        # 4) 收盘后决定
        if vol_k:
            for j, p in list(pos.items()):
                if p["i0"] == i and not np.isnan(c[j]) and V[i, j] < vol_k * AV[i - 1, j]:
                    sell(j, p, p["shares"], c[j], d, "突破无量")
                    del pos[j]
        for j, p in pos.items():
            if np.isnan(c[j]):
                continue
            if i - p["i0"] >= partial_days and not p["partial"] and not p["partial_next"]:
                p["partial_next"] = True
            if (p["partial"] or p["partial_next"]) and c[j] < MA[i, j]:
                p.update(exit_next=True, partial_next=False)
        # 5) 记账
        mv = sum(p["shares"] * last_px[j] for j, p in pos.items())
        curve.append((d, cash + mv, mv, len(pos)))
        # 6) 明天的挂单
        orders = []
        if mkt_ok[i]:
            js = [j for j in np.flatnonzero(SU[i]) if j not in pos]
            js.sort(key=lambda j: -np.nan_to_num(R3[i, j]))
            orders = [(j, BH[i, j] + 0.01, AD[i, j]) for j in js[:max_orders]]

    eq = pd.DataFrame(curve, columns=["date", "equity", "invested", "n_pos"]).set_index("date")
    return eq, pd.DataFrame(trades)


# ───────── 统计 ─────────
def period_stats(eq: pd.Series, tr: pd.DataFrame | None, a: str, b: str | None) -> dict:
    e = eq.loc[a:b]
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    r = e.pct_change().dropna()
    out = dict(CAGR=(e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1, DD=(e / e.cummax() - 1).min(),
               Sharpe=r.mean() / r.std() * np.sqrt(252) if r.std() > 0 else np.nan)
    if tr is not None:
        x = pd.to_datetime(tr.exit_date)
        t = tr[(x >= a) & ((x <= b) if b else True)]
        gl = -t.pnl[t.pnl <= 0].sum()
        out.update(PF=t.pnl[t.pnl > 0].sum() / gl if gl > 0 else np.nan, N=len(t))
    return out


def report(eq, tr, bench, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tr.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")
    eq.to_csv(out / "equity.csv", encoding="utf-8-sig")
    b = bench.reindex(eq.index).ffill()
    s = pd.DataFrame({"策略": period_stats(eq.equity, tr, eq.index[0], None),
                      "QQQ": period_stats(b.QQQ, None, eq.index[0], None),
                      "SPY": period_stats(b.SPY, None, eq.index[0], None)}).T
    win, loss = tr[tr.pnl > 0], tr[tr.pnl <= 0]
    big = tr.nlargest(max(len(tr) // 20, 1), "pnl")
    lines = [s[["CAGR", "DD", "Sharpe"]].to_string(float_format=lambda v: f"{v:.3f}"), "",
             f"交易 {len(tr)} 笔 ｜ 胜率 {len(win) / len(tr):.1%} ｜ PF {win.pnl.sum() / -loss.pnl.sum():.2f}",
             f"亏损单持有中位 {loss.days.median():.0f} 天 ｜ 盈利单 {win.days.median():.0f} 天 ｜ "
             f"前 5% 大赚单 {big.days.median():.0f} 天, 占总盈利 {big.pnl.sum() / win.pnl.sum():.0%}",
             f"平均仓位占用 {(eq.invested / eq.equity).mean():.0%} ｜ 平均持仓 {eq.n_pos.mean():.1f} 只", "",
             "按离场原因:", tr.groupby("exit_reason").pnl.agg(["size", "sum"]).round(0).to_string(), "",
             "年度收益 %:"]
    yr = pd.DataFrame({k: v.resample("YE").last() for k, v in
                       {"策略": eq.equity, "QQQ": b.QQQ, "SPY": b.SPY}.items()})
    yr = pd.concat([pd.DataFrame([[eq.equity.iloc[0], b.QQQ.iloc[0], b.SPY.iloc[0]]],
                                 columns=yr.columns, index=[eq.index[0]]), yr]).pct_change().iloc[1:]
    yr.index = yr.index.year
    lines.append((yr * 100).round(1).to_string())
    text = "\n".join(lines)
    (out / "summary.txt").write_text(text, encoding="utf-8")
    print(text)
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "DejaVu Sans"]
        fig, (a1, a2) = plt.subplots(2, 1, figsize=(11, 7), sharex=True, height_ratios=[3, 1])
        for k, ser, lw in (("突破组合", eq.equity, 1.6), ("QQQ", b.QQQ, 1), ("SPY", b.SPY, 1)):
            a1.plot(ser / ser.iloc[0] * 100, label=k, lw=lw)
        a1.set_yscale("log"); a1.legend(); a1.grid(alpha=.3)
        a2.fill_between(eq.index, (eq.equity / eq.equity.cummax() - 1) * 100, 0, alpha=.5)
        a2.set_ylabel("回撤 %"); a2.grid(alpha=.3)
        fig.tight_layout(); fig.savefig(out / "equity.png", dpi=120)
    except ImportError:
        pass
    print(f"\n输出: {out}")


# ───────── 敏感性 ─────────
BASE = dict(top=2.0, p={}, sim=dict(mkt="ma", trail="ma10", risk_pct=1.0))
VARIANTS = {
    "基准(大盘10>20)":           {},
    "不加大盘过滤":               dict(sim=dict(mkt="none")),
    "严格大盘(+QQQ>50MA)":        dict(sim=dict(mkt="strict")),
    "强度前1%":                   dict(top=1.0),
    "强度前5%":                   dict(top=5.0),
    "突破须放量1.5倍":            dict(sim=dict(vol_k=1.5)),
    "移动止损20MA":               dict(sim=dict(trail="ma20")),
    "不做部分止盈":               dict(sim=dict(partial_pct=0.0)),
    "单笔风险0.5%":               dict(sim=dict(risk_pct=0.5)),
    "流动性($10,$30M)":           dict(p=dict(min_price=10.0, min_dolvol=30.0)),
    "整理更紧(2/1.5×ADR)":        dict(p=dict(depth_k=2.0, tight_k=1.5)),
    "ADR门槛3.5%":                dict(p=dict(min_adr=3.5)),
    "前段涨幅≥50%":               dict(p=dict(min_prior=50.0)),
    # 组合 A 只按样本内选 (诚实); B 是看了样本外才挑的, 只当上限参考
    "A 样本内最优(20MA+前段50%)": dict(p=dict(min_prior=50.0), sim=dict(trail="ma20")),
    "B 偷看样本外(放量+流动性+严格大盘)": dict(p=dict(min_price=10.0, min_dolvol=30.0),
                                             sim=dict(vol_k=1.5, mkt="strict")),
}


def sensitivity(pn) -> pd.DataFrame:
    cache, rows = {}, []
    for name, v in VARIANTS.items():
        top, p = v.get("top", BASE["top"]), {**BASE_P, **v.get("p", {})}
        sim = {**BASE["sim"], **v.get("sim", {})}
        key = f"{top}|{sorted(p.items())}"
        t0 = time.time()
        if key not in cache:
            cache[key] = build_signals(pn, top, p)
        eq, tr = simulate(pn, cache[key], **sim)
        si, so = (period_stats(eq.equity, tr, "2016-01-01", IS_END),
                  period_stats(eq.equity, tr, OOS_START, None))
        rows.append({"变体": name, **{f"IS_{k}": v_ for k, v_ in si.items()},
                     **{f"OOS_{k}": v_ for k, v_ in so.items()}})
        print(f"  {name:<28} ({time.time() - t0:3.0f}s)  IS {si['CAGR']:6.1%}  OOS {so['CAGR']:6.1%}",
              flush=True)
    b = pn["bench"]
    for k in ("QQQ", "SPY"):
        si, so = period_stats(b[k], None, "2016-01-01", IS_END), period_stats(b[k], None, OOS_START, None)
        rows.append({"变体": f"{k} 买入持有", **{f"IS_{x}": y for x, y in si.items()},
                     **{f"OOS_{x}": y for x, y in so.items()}})
    return pd.DataFrame(rows).set_index("变体")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sensitivity", action="store_true")
    ap.add_argument("--mkt", default="ma", choices=["none", "ma", "strict"])
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    pn = load_hist(a.refresh)
    print(f"{pn['Close'].shape[1]} 只, {pn['Close'].index[0].date()} ~ {pn['Close'].index[-1].date()}")
    if a.sensitivity:
        res = sensitivity(pn)
        OUT.mkdir(parents=True, exist_ok=True)
        res.to_csv(OUT / "sensitivity.csv", encoding="utf-8-sig")
        with pd.option_context("display.width", 250, "display.max_columns", 20):
            print(res.to_string(float_format=lambda v: f"{v:.3f}"))
        return
    eq, tr = simulate(pn, build_signals(pn, BASE["top"], BASE_P), mkt=a.mkt)
    report(eq, tr, pn["bench"], OUT / f"base_{a.mkt}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
