"""SPX 指数代理回测: LEAP IV 比值档位有没有预测力 (todo.md #3, 2026-09-25)。

运行: .venv/Scripts/python.exe research/leap_iv_vix1y_backtest.py   (联网, ~1 分钟)
结论与局限见 todo.md #3 "指数代理回测"。

数据 (全部免费):
  - VIX1Y / VIX6M: CBOE 日收盘 (VIX 方法论 = 方差互换口径, 含虚值 put, 系统性高于平值 IV)
  - SPX: Yahoo ^GSPC 日收盘 (价格指数, 不含股息)
  - 利率: Yahoo ^IRX (13 周国库券)
方法:
  每月第一个交易日入场。比值 = VIX1Y ÷ SPX 过去 10 年滚动 252 日实际波动的中位数
  (与 scanner 同构: 窗口 = 期权期限 1 年, 每 5 个交易日取一个窗口)。按指数口径分档:
  A < 1.0 / B 1.0–1.35 / C > 1.35; 升档: IV > 1.35 × max(近1年, 近2年实际波动)。
  检验一: 比值 vs 事后 1 年实际波动 (IV 有没有高估未来波动)。
  检验二: 买 1 年期 0.80δ / 平值 call, 持有 126 个交易日, 用 VIX6M 定价平仓。
          期权特有成本 = (期权盈亏 − 入场 delta × 指数盈亏) ÷ 入场权利金
          —— 剥离方向后剩下的 theta + vega + gamma。
"""
import io, json, math, os, sys, urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0"}
Q = 0.018            # SPX 股息率近似 (常数)
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()


def cboe_hist(name):
    df = pd.read_csv(io.BytesIO(get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv")))
    df["DATE"] = pd.to_datetime(df["DATE"])
    return df.set_index("DATE")["CLOSE"].astype(float) / 100


def yahoo(sym):
    p1 = int(datetime(1990, 1, 1, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    j = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d"))
    r = j["chart"]["result"][0]
    idx = pd.to_datetime(r["timestamp"], unit="s").normalize()
    return pd.Series(r["indicators"]["quote"][0]["close"], index=idx, dtype=float).dropna()


def bs(S, K, T, r, s, q=Q):
    d1 = (math.log(S / K) + (r - q + s * s / 2) * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    return S * math.exp(-q * T) * N(d1) - K * math.exp(-r * T) * N(d2), math.exp(-q * T) * N(d1)


def strike_for_delta(S, T, r, s, target):
    lo, hi = S * 0.3, S * 1.5
    for _ in range(80):
        k = (lo + hi) / 2
        if bs(S, k, T, r, s)[1] > target:
            lo = k
        else:
            hi = k
    return (lo + hi) / 2


vix1y, vix6m = cboe_hist("VIX1Y"), cboe_hist("VIX6M")
spx = yahoo("^GSPC")
irx = yahoo("^IRX") / 100
df = pd.DataFrame({"spx": spx}).join(vix1y.rename("v1y")).join(vix6m.rename("v6m")).join(irx.rename("r"))
df["r"] = df["r"].ffill().fillna(0.02)
ret = np.log(df["spx"]).diff()
df["rv1y"] = ret.rolling(252).std(ddof=0) * math.sqrt(252)
df["rv2y"] = ret.rolling(504).std(ddof=0) * math.sqrt(252)
# 10 年里每 5 个交易日一个 252 日窗口的中位数 (窗口完全落在 t 之前)
rv = df["rv1y"].to_numpy()
med = np.full(len(df), np.nan)
for i in range(2520, len(df)):
    med[i] = np.nanmedian(rv[i - 2520 + 252:i + 1:5])
df["med"] = med
# 事后 1 年实际波动 (t+1 .. t+252)
df["rv_fwd"] = ret[::-1].rolling(252).std(ddof=0)[::-1].shift(-1) * math.sqrt(252)

pos = {d: i for i, d in enumerate(df.index)}
entries = df[df["v1y"].notna() & df["med"].notna()]
entries = entries.groupby([entries.index.year, entries.index.month]).head(1)

rows = []
for d, e in entries.iterrows():
    i = pos[d]
    iv, S0, r0 = e["v1y"], e["spx"], e["r"]
    ratio = iv / e["med"]
    idx = 0 if ratio < 1.0 else 1 if ratio <= 1.35 else 2
    bumped = iv > 1.35 * max(e["rv1y"], e["rv2y"]) and idx < 2
    band = "ABC"[idx + bumped]
    row = dict(date=d, iv=iv, med=e["med"], ratio=ratio, band=band, rv_fwd=e["rv_fwd"],
               rv1y=e["rv1y"], rv2y=e["rv2y"])
    j = i + 126
    if j < len(df):
        x = df.iloc[j]
        S1, r1 = x["spx"], x["r"]
        iv1 = x["v6m"] if not math.isnan(x["v6m"]) else x["v1y"]
        row["spx_6m"] = S1 / S0 - 1
        for tag, tgt in (("d80", 0.80), ("atm", None)):
            K = strike_for_delta(S0, 1.0, r0, iv, tgt) if tgt else S0
            C0, D0 = bs(S0, K, 1.0, r0, iv)
            C1, _ = bs(S1, K, 0.5, r1, iv1)
            row[f"{tag}_ret"] = C1 / C0 - 1
            row[f"{tag}_cost"] = ((C1 - C0) - D0 * (S1 - S0)) / C0
            # 同一段路径下, 若入场 IV 等于该标的"平时"水平 (中位数) 会怎样
            C0f, _ = bs(S0, K, 1.0, r0, e["med"])
            row[f"{tag}_overpay"] = C0 / C0f - 1
    rows.append(row)
R = pd.DataFrame(rows).set_index("date")

# ---------------------------------------------------------------- 输出
pd.set_option("display.width", 200)
print(f"样本: {R.index[0].date()} ~ {R.index[-1].date()}, 月度入场 {len(R)} 次 "
      f"(有 6 个月结果 {R['spx_6m'].notna().sum()} 次, 有 1 年事后波动 {R['rv_fwd'].notna().sum()} 次)")
print(f"VIX1Y 与 10 年中位数: 今天比值 {R['ratio'].iloc[-1]:.2f} (VIX1Y {R['iv'].iloc[-1]:.1%}, 中位数 {R['med'].iloc[-1]:.1%})")


def summarize(g):
    out = pd.Series({
        "n": len(g),
        "年份数": g.index.year.nunique(),
        "比值中位": g["ratio"].median(),
        "IV−事后RV(均)": (g["iv"] - g["rv_fwd"]).mean(),
        "IV>事后RV占比": (g["iv"] > g["rv_fwd"]).mean(),
        "0.8δ 期权成本(均)": g["d80_cost"].mean(),
        "平值 期权成本(均)": g["atm_cost"].mean(),
        "0.8δ 6月收益(中位)": g["d80_ret"].median(),
        "SPX 6月(中位)": g["spx_6m"].median(),
        "0.8δ 多付(均)": g["d80_overpay"].mean(),
    })
    return out


print("\n== 按档位 (指数口径 A<1.0 / B 1.0–1.35 / C>1.35, 含升档) ==")
print(R.groupby("band").apply(summarize).T.to_string(float_format=lambda v: f"{v:.3f}"))
R["q"] = pd.qcut(R["ratio"], 5, labels=["Q1 低", "Q2", "Q3", "Q4", "Q5 高"])
print("\n== 按比值五分位 (不依赖阈值) ==")
print(R.groupby("q", observed=True).apply(summarize).T.to_string(float_format=lambda v: f"{v:.3f}"))
print("\n== 各年档位分布 ==")
print(pd.crosstab(R.index.year, R["band"]).T.to_string())

# 偏差检查: 今天 VIX1Y vs SPX 1 年期平值 IV
try:
    q = json.loads(get("https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX1Y.json"))["data"]
    ch = json.loads(get("https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json"))["data"]
    S = ch["current_price"] or ch["close"]
    import re
    best = {}
    for o in ch["options"]:
        m = re.match(r"SPXW?(\d{6})([CP])(\d{8})$", o["option"])
        if not m or not o.get("iv"):
            continue
        exp = datetime.strptime(m.group(1), "%y%m%d")
        dte = (exp - datetime.now()).days
        k = int(m.group(3)) / 1000
        if 300 <= dte <= 430:
            best.setdefault(m.group(1), []).append((abs(k - S), o["iv"], dte))
    lines = []
    for exp, v in sorted(best.items()):
        v.sort()
        atm = np.mean([x[1] for x in v[:2]])
        lines.append((v[0][2], atm))
    print(f"\n== 偏差检查 ({ch.get('last_trade_time', '?')}) ==")
    print(f"VIX1Y 当前 {q.get('current_price')}  |  SPX ~1 年期平值 IV: "
          + ", ".join(f"{dte}天 {atm:.1%}" for dte, atm in lines))
except Exception as e:
    print("偏差检查失败:", repr(e))


# ---------------------------------------------------------------- 平值口径校正
# VIX1Y 含虚值 put (方差互换口径), 系统性高于平值 IV。用今天的比值做一次性校正:
# 2026-09-24 VIX1Y 21.8% vs SPX ~1 年平值 17.6%。历史 skew 不是常数, 校正是近似。
ATM_TODAY, V1Y_TODAY = 0.176, 0.2182


def reband(iv, r):
    ratio = iv / r["med"]
    idx = 0 if ratio < 1.0 else 1 if ratio <= 1.35 else 2
    return "ABC"[idx + (iv > 1.35 * max(r["rv1y"], r["rv2y"]) and idx < 2)], ratio


for name, f in (("比例校正", lambda v: v * ATM_TODAY / V1Y_TODAY),
                ("相减校正", lambda v: v - (V1Y_TODAY - ATM_TODAY))):
    iv = f(R["iv"])
    t = [reband(v, r) for v, (_, r) in zip(iv, R.iterrows())]
    b, ratio = pd.Series([x[0] for x in t], index=R.index), iv / R["med"]
    print()
    print(f"==== 平值口径 ({name}); 今天 {b.iloc[-1]} ({ratio.iloc[-1]:.2f}) ====")
    cut = pd.cut(ratio, [0, 1.0, 1.15, 1.25, 1.35, 1.5, 9],
                 labels=["<1.0", "1.0-1.15", "1.15-1.25", "1.25-1.35", "1.35-1.5", ">1.5"])
    for key, lab in ((b, "按档位"), (cut, "按比值区间")):
        g = pd.DataFrame({"k": key, "vrp": iv - R["rv_fwd"], "over": iv > R["rv_fwd"],
                          "d80": R["d80_cost"], "atm": R["atm_cost"], "spx": R["spx_6m"],
                          "yr": R.index.year}).groupby("k", observed=True)
        print(f"-- {lab}")
        print(pd.DataFrame({"n": g.size(), "年份数": g["yr"].nunique(), "IV-事后RV": g["vrp"].mean(),
                            "高估占比": g["over"].mean(), "0.8d成本": g["d80"].mean(),
                            "平值成本": g["atm"].mean(), "SPX6月中位": g["spx"].median()}
                           ).to_string(float_format=lambda v: f"{v:.3f}"))
