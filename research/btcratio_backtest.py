"""COIN/BTC 比值回测: 低于 1.8/1.5/1.2 的天数、按入场比值分桶的后续回报 (绝对与相对 BTC)、
首次触及 <2.0 的事件表 (含最大回撤)、BTC 趋势过滤、BTC 上涨中的比值新低 (背离)、市值比值
(btcratio.py 文档和 BUCKET_NOTE 里的数字全部出自这里; todo.md #13)。

运行: .venv/bin/python research/btcratio_backtest.py            (联网, yfinance 两只全量日线, 几秒)
      .venv/bin/python research/btcratio_backtest.py --cache    (只用 data/btcratio_COIN.csv)
      .venv/bin/python research/btcratio_backtest.py --asof 2026-10-30   (截到某天, 复现文档数字)

数据: yfinance COIN (2021-04-14 上市起) + BTC-USD, 对齐到 COIN 的交易日 (BTC 取同一天 UTC 收盘)。
比值 = 收盘 ÷ BTC × 1000 (‰)。前瞻收益按交易日数 (20/60/120/250), 有重叠; "首次触及"表才是独立样本。
股本锚点 (市值比值) 是 10-Q 封面页近似值, 见 btcratio.SHARES_ANCHORS。

样本本质只有三组 (2022-05 / 2022-12→2023-06 / 2025-04→05) + 2026-10 进行中 —— 所有"胜率"按
三次经验读, 不是统计显著。
"""
import argparse
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import btcratio as br                              # noqa: E402

pd.set_option("display.width", 230)
pd.set_option("display.max_colwidth", 90)

BINS = [0, 1.9, 2.0, 2.2, 2.4, 2.6, 3.0, 3.5, 4.5, 99]
LABELS = ["<1.9", "1.9-2.0", "2.0-2.2", "2.2-2.4", "2.4-2.6", "2.6-3.0", "3.0-3.5", "3.5-4.5", "≥4.5"]
HORIZONS = (20, 60, 120, 250)
THRESHOLDS = (2.6, 2.4, 2.2, 2.0, 1.9, 1.85, 1.8, 1.7, 1.6, 1.5, 1.2)


def load(use_cache: bool, asof: date) -> pd.DataFrame:
    base = Path(__file__).resolve().parents[1]
    cache = base / "data" / br.CACHE_NAME.format("COIN")
    if use_cache:
        df = br._read_cache(cache)
        if df is None:
            sys.exit(f"没有缓存 {cache} — 先不带 --cache 跑一次")
    else:
        df = br.fetch_pair("COIN", cache, asof)["df"]
    df = df[df.index.date <= asof].copy()
    df["ratio"] = br.ratio(df.close, df.btc)
    df["mcap"] = br.mcap_ratio_series(df)
    for h in HORIZONS:
        df[f"f{h}"] = df.close.shift(-h) / df.close - 1
        df[f"fb{h}"] = df.btc.shift(-h) / df.btc - 1
        df[f"fr{h}"] = df[f"f{h}"] - df[f"fb{h}"]
    df["btc50"] = df.btc.rolling(50).mean()
    return df


def strict_runs(mask: pd.Series) -> int:
    m = mask.astype(int)
    return int(((m.diff() == 1) | ((m == 1) & m.shift(1).isna())).sum())


def section(title: str) -> None:
    print(f"\n{'=' * 8} {title} {'=' * 8}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", action="store_true")
    ap.add_argument("--asof", default=None, help="YYYY-MM-DD, 默认今天")
    a = ap.parse_args()
    asof = date.fromisoformat(a.asof) if a.asof else date.today()
    df = load(a.cache, asof)
    r = df.ratio

    section("1. 分布")
    print(f"交易日 {len(df)} ({df.index[0].date()} → {df.index[-1].date()}); 现值 {r.iloc[-1]:.3f} "
          f"= {br.percentile(r.iloc[:-1], r.iloc[-1]):.1f} 分位; 最低 {r.min():.3f} @{r.idxmin().date()}, "
          f"最高 {r.max():.3f} @{r.idxmax().date()}")
    print("分位:", r.quantile([0, .01, .05, .1, .25, .5, .75, .9, 1]).round(2).to_dict())
    print(r.groupby(r.index.year).agg(["min", "median", "max"]).round(2).to_string())
    print(f"市值比值 (%): 现值 {df.mcap.iloc[-1]:.2f}, 最低 {df.mcap.min():.2f} @{df.mcap.idxmin().date()}, "
          f"现值分位 {br.percentile(df.mcap.iloc[:-1], df.mcap.iloc[-1]):.1f}; "
          f"现价回到历史最低市值比值 = ${df.close.iloc[-1] * df.mcap.min() / df.mcap.iloc[-1]:.0f}")

    section("2. 低于阈值的天数 (交易日)")
    print(f"{'阈值':>6} {'天数':>5} {'段(严格连续)':>12} {'段(断开≤5合并)':>14}  首次         末次")
    for th in THRESHOLDS:
        m = r < th
        runs = br.episodes(m)
        first = m[m].index.min().date() if m.any() else None
        last = m[m].index.max().date() if m.any() else None
        print(f"{th:>6} {int(m.sum()):>5} {strict_runs(m):>12} {len(runs):>14}  {first}  {last}")

    section("3. 按入场比值分桶的前瞻收益 (全部交易日, 有重叠)")
    df["bk"] = pd.cut(r, BINS, labels=LABELS, right=False)
    for h in HORIZONS:
        g = df.groupby("bk", observed=True)
        out = pd.DataFrame({
            "n": g[f"f{h}"].count(),
            "win%": g[f"f{h}"].apply(lambda s: (s.dropna() > 0).mean() * 100).round(0),
            "med%": (g[f"f{h}"].median() * 100).round(1),
            "p25%": (g[f"f{h}"].quantile(.25) * 100).round(1),
            "rel_win%": g[f"fr{h}"].apply(lambda s: (s.dropna() > 0).mean() * 100).round(0),
            "rel_med%": (g[f"fr{h}"].median() * 100).round(1)})
        print(f"\n-- {h} 个交易日"); print(out.to_string())

    section("4. 低于阈值就买 (有重叠)")
    rows = []
    for th in (2.0, 2.2, 2.4, 2.6, 3.0):
        m = r < th
        for h in HORIZONS:
            s = df.loc[m, f"f{h}"].dropna(); sr = df.loc[m, f"fr{h}"].dropna()
            if len(s):
                rows.append(dict(th=th, h=h, n=len(s), win=round((s > 0).mean() * 100),
                                 med=round(s.median() * 100, 1), p10=round(s.quantile(.1) * 100, 1),
                                 rel_win=round((sr > 0).mean() * 100), rel_med=round(sr.median() * 100, 1)))
    print(pd.DataFrame(rows).to_string(index=False))

    section("5. 首次触及 <2.0 的独立样本 (严格连续段的首日入场; 最大回撤 = 之后 120 日最低收盘)")
    m = (r < 2.0).astype(int); grp = (m.diff() != 0).cumsum()
    print(f"{'入场日':>10} {'入场价':>7} {'最低比值':>8} {'MAE120':>7} {'120日':>6} {'250日':>6} {'BTC120':>7}")
    for _, g in df[m == 1].groupby(grp[m == 1]):
        i0 = g.index[0]; pos = df.index.get_loc(i0); p0 = df.close.iloc[pos]
        win = df.iloc[pos + 1:pos + 121]
        if win.empty:
            print(f"{i0.date()} {p0:>7.1f} {g.ratio.min():>8.3f}  (无前瞻数据)"); continue
        mae = win.close.min() / p0 - 1
        r120 = df[f"f120"].iloc[pos]; r250 = df[f"f250"].iloc[pos]; b120 = df[f"fb120"].iloc[pos]
        fmt = lambda x: f"{x * 100:>6.0f}%" if pd.notna(x) else "    na"
        print(f"{i0.date()} {p0:>7.1f} {g.ratio.min():>8.3f} {mae * 100:>6.0f}% {fmt(r120)} {fmt(r250)} {fmt(b120)}")

    section("6. BTC 趋势过滤 (比值 <2.2 的日子, 按 BTC 是否在 50 日线上)")
    for lbl, mm in (("BTC>50dma", (r < 2.2) & (df.btc > df.btc50)), ("BTC<50dma", (r < 2.2) & (df.btc < df.btc50))):
        for h in (60, 120):
            s = df.loc[mm, f"f{h}"].dropna()
            if len(s):
                print(f"{lbl} {h}d n={len(s)} win={round((s > 0).mean() * 100)}% med={s.median() * 100:.1f}%")

    section("7. 背离: 比值创 250 日新低而 BTC 高于 120 日前")
    low250 = r == r.rolling(250).min()
    btc_up = df.btc > df.btc.shift(120)
    both = low250 & btc_up
    print(f"比值 250 日新低 {int(low250.sum())} 天, 其中 BTC 高于 120 日前 {int(both.sum())} 天:")
    print(df.loc[both, ["close", "btc", "ratio"]].round(2).to_string())
    return 0


if __name__ == "__main__":
    sys.exit(main())
