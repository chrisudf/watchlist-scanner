"""指数代理回测: LEAP IV 比值档位有没有预测力, C 档该怎么做 (todo.md #3)。

运行: .venv/Scripts/python.exe research/leap_iv_index_backtest.py   (联网, 约 2 分钟)

标的与数据 (全部免费):
  SPX  价格 ^GSPC; 1 年 IV = CBOE VIX1Y, 6 个月 IV = VIX6M
  NDX  价格 ^NDX (QQQ 的代理); 没有 1 年期纳指 IV 指数, 用
       VIX1Y × (VXN ÷ VIX) 近似 —— 借 SPX 的期限结构, 按两者 30 天 IV 之比放大
  利率 ^IRX; 股息率按常数 (SPX 1.8%, NDX 0.8%)
VIX 系指数是方差互换口径 (含虚值 put), 比平值 IV 高。用今天 CBOE 链上 ~1 年
平值 IV 与代理值之比做一次性校正, 把全部 IV 换成平值口径 (历史 skew 不是常数,
这是近似)。定价一律用平值口径的平值 IV, 不加 skew。

方法: 每月第一个交易日入场。比值 = 1 年平值 IV ÷ 过去 10 年滚动 252 日实际波动
中位数 (与扫描器同构)。档位 A < 1.0 / B 1.0–1.25 / C > 1.25 (2026-09-25 起指数与
个股同线), 升档: IV > 1.25 × max(近1年, 近2年实际波动)。
  检验一: IV − 事后 1 年实际波动
  检验二: 1 年期结构持有 126 个交易日 (平仓用 6 个月 IV):
          深度实值 0.80δ call / 平值 call / 0.80δ-0.40δ call spread
          期权成本 = (期权盈亏 − 入场 delta × 指数盈亏) ÷ 权利金
  检验三 (只看 C 档): "等回落" —— 等到比值回到 C 档以下才买, 等待期间指数走了多少
"""
import io, json, math, re, urllib.request
from datetime import datetime, timezone

import numpy as np
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0"}
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
LO, HI = 1.0, 1.25
pd.set_option("display.width", 220)


def get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=90).read()


def cboe_hist(name):
    df = pd.read_csv(io.BytesIO(get(f"https://cdn.cboe.com/api/global/us_indices/daily_prices/{name}_History.csv")))
    return pd.Series(df["CLOSE"].astype(float).values / 100, index=pd.to_datetime(df["DATE"]))


def yahoo(sym):
    p1 = int(datetime(1985, 1, 1, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime.now(timezone.utc).timestamp())
    j = json.loads(get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?period1={p1}&period2={p2}&interval=1d"))
    r = j["chart"]["result"][0]
    idx = pd.to_datetime(r["timestamp"], unit="s").normalize()
    s = pd.Series(r["indicators"]["quote"][0]["close"], index=idx, dtype=float).dropna()
    return s[~s.index.duplicated(keep="last")]


def chain_atm_1y(sym):
    """CBOE 延迟链: 300-430 天到期里离现价最近两档 call/put 的 IV 均值。"""
    d = json.loads(get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"))["data"]
    S = d["current_price"] or d["close"]
    root = sym.lstrip("_")
    ivs = []
    for o in d["options"]:
        m = re.match(rf"{root}W?(\d{{6}})([CP])(\d{{8}})$", o["option"])
        if not m or not o.get("iv"):
            continue
        dte = (datetime.strptime(m.group(1), "%y%m%d") - datetime.now()).days
        if 300 <= dte <= 430:
            ivs.append((abs(int(m.group(3)) / 1000 - S), o["iv"]))
    ivs.sort()
    return float(np.mean([v for _, v in ivs[:4]])), d.get("last_trade_time")


def bs(S, K, T, r, s, q):
    d1 = (math.log(S / K) + (r - q + s * s / 2) * T) / (s * math.sqrt(T))
    d2 = d1 - s * math.sqrt(T)
    return S * math.exp(-q * T) * N(d1) - K * math.exp(-r * T) * N(d2), math.exp(-q * T) * N(d1)


def strike_for_delta(S, T, r, s, q, target):
    lo, hi = S * 0.2, S * 2.0
    for _ in range(80):
        k = (lo + hi) / 2
        if bs(S, k, T, r, s, q)[1] > target:
            lo = k
        else:
            hi = k
    return (lo + hi) / 2


def run(name, price, iv1y, iv6m, atm_today, q):
    df = pd.DataFrame({"px": price}).join(iv1y.rename("v1y")).join(iv6m.rename("v6m")).join(irx.rename("r"))
    df["r"] = df["r"].ffill().fillna(0.02)
    last = df["v1y"].dropna()
    scale = atm_today / last.iloc[-1]
    df["iv"], df["iv6"] = df["v1y"] * scale, df["v6m"] * scale
    ret = np.log(df["px"]).diff()
    df["rv1y"] = ret.rolling(252).std(ddof=0) * math.sqrt(252)
    df["rv2y"] = ret.rolling(504).std(ddof=0) * math.sqrt(252)
    rv = df["rv1y"].to_numpy()
    med = np.full(len(df), np.nan)
    for i in range(2520, len(df)):
        med[i] = np.nanmedian(rv[i - 2520 + 252:i + 1:5])
    df["med"] = med
    df["rv_fwd"] = ret[::-1].rolling(252).std(ddof=0)[::-1].shift(-1) * math.sqrt(252)
    pos = {d: i for i, d in enumerate(df.index)}
    ent = df[df["iv"].notna() & df["med"].notna()]
    ent = ent.groupby([ent.index.year, ent.index.month]).head(1)

    rows = []
    for d, e in ent.iterrows():
        i, S0, r0, iv = pos[d], e["px"], e["r"], e["iv"]
        ratio = iv / e["med"]
        idx = 0 if ratio < LO else 1 if ratio <= HI else 2
        band = "ABC"[idx + (iv > HI * max(e["rv1y"], e["rv2y"]) and idx < 2)]
        row = dict(date=d, iv=iv, ratio=ratio, band=band, vrp=iv - e["rv_fwd"], S0=S0)
        j = i + 126
        if j < len(df):
            x = df.iloc[j]
            S1, r1 = x["px"], x["r"]
            iv1 = x["iv6"] if not math.isnan(x["iv6"]) else x["iv"]
            row["idx_6m"] = S1 / S0 - 1
            K80 = strike_for_delta(S0, 1.0, r0, iv, q, 0.80)
            K40 = strike_for_delta(S0, 1.0, r0, iv, q, 0.40)
            legs = {}
            for tag, K in (("d80", K80), ("atm", S0), ("k40", K40)):
                C0, D0 = bs(S0, K, 1.0, r0, iv, q)
                C1, _ = bs(S1, K, 0.5, r1, iv1, q)
                legs[tag] = (C0, C1, D0)
            for tag in ("d80", "atm"):
                C0, C1, D0 = legs[tag]
                row[f"{tag}_ret"] = C1 / C0 - 1
                row[f"{tag}_pnl"] = (C1 - C0) / S0          # 每 1 份指数名义的盈亏
                row[f"{tag}_cost"] = ((C1 - C0) - D0 * (S1 - S0)) / C0
            C0 = legs["d80"][0] - legs["k40"][0]
            C1 = legs["d80"][1] - legs["k40"][1]
            row["spr_ret"], row["spr_pnl"] = C1 / C0 - 1, (C1 - C0) / S0
        rows.append(row)
    R = pd.DataFrame(rows).set_index("date")

    # 等回落: C 档入场日 → 下一个非 C 的月度入场日
    b = R["band"].tolist()
    waits, moves = [], []
    for k in range(len(R)):
        if b[k] != "C":
            continue
        nxt = next((m for m in range(k + 1, len(R)) if b[m] != "C"), None)
        if nxt is None:
            continue
        waits.append(nxt - k)
        moves.append(R["S0"].iloc[nxt] / R["S0"].iloc[k] - 1)

    print(f"\n{'=' * 20} {name} {'=' * 20}")
    print(f"样本 {R.index[0].date()} ~ {R.index[-1].date()}, 月度入场 {len(R)} 次; 平值校正系数 {scale:.3f} "
          f"(今天平值 {atm_today:.1%}); 今天比值 {R['ratio'].iloc[-1]:.2f} → {R['band'].iloc[-1]} 档")
    g = R.groupby("band")
    print("-- 按档位")
    print(pd.DataFrame({
        "n": g.size(), "年份数": g.apply(lambda x: x.index.year.nunique()),
        "IV-事后RV": g["vrp"].mean(), "高估占比": g.apply(lambda x: (x["vrp"] > 0).mean()),
        "0.8d成本": g["d80_cost"].mean(), "平值成本": g["atm_cost"].mean(),
        "指数6月中位": g["idx_6m"].median()}).to_string(float_format=lambda v: f"{v:.3f}"))
    cut = pd.cut(R["ratio"], [0, 1.0, 1.15, 1.25, 1.35, 1.5, 9],
                 labels=["<1.0", "1.0-1.15", "1.15-1.25", "1.25-1.35", "1.35-1.5", ">1.5"])
    g = R.groupby(cut, observed=True)
    print("-- 按比值区间")
    print(pd.DataFrame({
        "n": g.size(), "IV-事后RV": g["vrp"].mean(), "高估占比": g.apply(lambda x: (x["vrp"] > 0).mean()),
        "0.8d成本": g["d80_cost"].mean(), "平值成本": g["atm_cost"].mean(),
        "指数6月中位": g["idx_6m"].median()}).to_string(float_format=lambda v: f"{v:.3f}"))
    print("-- 各档三种结构, 持有 6 个月 (盈亏 = 每 1 份指数名义的百分比; 收益 = 相对权利金)")
    out = {}
    for band, x in R.dropna(subset=["idx_6m"]).groupby("band"):
        out[band] = {
            "0.8δ 盈亏均": x["d80_pnl"].mean(), "spread 盈亏均": x["spr_pnl"].mean(), "平值 盈亏均": x["atm_pnl"].mean(),
            "0.8δ 收益中位": x["d80_ret"].median(), "spread 收益中位": x["spr_ret"].median(),
            "平值 收益中位": x["atm_ret"].median(),
            "0.8δ 赚钱占比": (x["d80_pnl"] > 0).mean(), "spread 赚钱占比": (x["spr_pnl"] > 0).mean(),
            "0.8δ 最差": x["d80_ret"].min(), "spread 最差": x["spr_ret"].min(),
            "spread 跑输 0.8δ 的次数占比": (x["spr_pnl"] < x["d80_pnl"]).mean()}
    print(pd.DataFrame(out).to_string(float_format=lambda v: f"{v:.3f}"))
    if waits:
        w, mv = np.array(waits), np.array(moves)
        print(f"-- C 档'等回落': {len(w)} 次, 等待月数 中位 {np.median(w):.0f} / 均值 {w.mean():.1f}; "
              f"等待期间指数涨跌 中位 {np.median(mv):+.1%} / 均值 {mv.mean():+.1%}; 等完指数更高的占比 {(mv > 0).mean():.0%}")
    print("-- 各年 C 档月数:", R[R["band"] == "C"].groupby(R[R["band"] == "C"].index.year).size().to_dict())
    return R


vix1y, vix6m = cboe_hist("VIX1Y"), cboe_hist("VIX6M")
irx = yahoo("^IRX") / 100
spx, ndx = yahoo("^GSPC"), yahoo("^NDX")
vix, vxn = yahoo("^VIX") / 100, yahoo("^VXN") / 100
print(f"VXN (Yahoo) 自 {vxn.index[0].date()}; VIX1Y 自 {vix1y.index[0].date()}")
ratio_nv = (vxn / vix).dropna().rolling(5, min_periods=1).mean()     # 30 天 IV 之比, 5 日平滑
atm_spx, ts1 = chain_atm_1y("_SPX")
atm_qqq, ts2 = chain_atm_1y("QQQ")
print(f"CBOE 链 ~1 年平值 IV: SPX {atm_spx:.1%} ({ts1}), QQQ {atm_qqq:.1%} ({ts2})")
run("SPX (标普 500)", spx, vix1y, vix6m, atm_spx, 0.018)
run("NDX (纳指 100, QQQ 代理)", ndx, (vix1y * ratio_nv).dropna(), (vix6m * ratio_nv).dropna(), atm_qqq, 0.008)
