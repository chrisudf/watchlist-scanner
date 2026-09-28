"""Yahoo impliedVolatility 列在 CSP / 回踩 spread / 30 天平值上的偏差 (todo.md #5, 2026-09-28)。

运行: .venv/Scripts/python.exe research/yahoo_iv_bias.py   (联网, 约 3 分钟)

同一时点比三个 IV (周末/盘后跑 = 两边都是上个交易日收盘报价):
  y_col  Yahoo 的 impliedVolatility 列        (扫描器 contract_iv 优先用它)
  y_mid  Yahoo bid/ask mid 按扫描器模型反解    (r = RATE, 不计股息)
  c_iv   CBOE 延迟链自己的 IV                  (独立参照, 模型不同)
三类合约 (与扫描器的选法同构):
  CSP     put,  12-31 DTE 取最接近 21 天, 0.05-0.20δ
  SPREAD  call, 80-200 DTE 取最接近 135 天, 0.25-0.65δ
  ATM30   7-90 DTE 里夹住 30 天的两个到期, 离现价最近的 call/put
另算 CSP 的 delta 差: 用 y_col 与 y_mid 各算一次 |delta|, 看会不会把行权价选偏。
"""
import json, os, re, sys, urllib.request

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scanner as sc  # noqa: E402

UA = {"User-Agent": "Mozilla/5.0"}


def cboe(sym):
    req = urllib.request.Request(
        f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json", headers=UA)
    d = json.load(urllib.request.urlopen(req, timeout=60))["data"]
    legs = {}
    for o in d["options"]:
        m = re.match(rf"{sym}(\d{{6}})([CP])(\d{{8}})$", o["option"])
        if m and o.get("iv"):
            exp = f"20{m.group(1)[:2]}-{m.group(1)[2:4]}-{m.group(1)[4:]}"
            legs[(exp, m.group(2), int(m.group(3)) / 1000)] = o["iv"]
    return d, legs


def rows_for(sym):
    d, legs = cboe(sym)
    spot = d.get("current_price") or d.get("close")
    stamp = d.get("last_trade_time", "?")
    cc = sc.ChainCache(sym)
    exps = cc.expiries()
    out = []

    def take(cat, exp, dte, right, df, dlo=None, dhi=None, atm_only=False):
        T = dte / 365.0
        is_call = right == "C"
        if df is None or df.empty:
            return
        if atm_only:
            df = df.loc[[(df["strike"] - spot).abs().idxmin()]]
        for _, row in df.iterrows():
            bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
            if not (0 < bid <= ask):
                continue
            mid = (bid + ask) / 2
            k = float(row["strike"])
            y_mid = sc.implied_vol(mid, spot, k, T, sc.RATE, is_call)
            y_col = row.get("impliedVolatility")
            y_col = float(y_col) if y_col is not None and not pd.isna(y_col) and 0.01 < y_col < 5 else None
            if not y_mid:
                continue
            dm = abs(sc.bs_delta(spot, k, T, sc.RATE, y_mid, is_call))
            if dlo is not None and not (dlo <= dm <= dhi):
                continue
            dc = abs(sc.bs_delta(spot, k, T, sc.RATE, y_col, is_call)) if y_col else None
            out.append(dict(sym=sym, cat=cat, exp=exp, dte=dte, right=right, k=k,
                            spread_pct=(ask - bid) / mid * 100, y_col=y_col, y_mid=y_mid,
                            c_iv=legs.get((exp, right, k)), d_mid=dm, d_col=dc))

    csp = [(e, d) for e, d in exps if 12 <= d <= 31]
    if csp:
        e, dte = min(csp, key=lambda x: abs(x[1] - 21))
        take("CSP", e, dte, "P", cc.chain(e).puts, 0.05, 0.20)
    spr = [(e, d) for e, d in exps if 80 <= d <= 200]
    if spr:
        e, dte = min(spr, key=lambda x: abs(x[1] - 135))
        take("SPREAD", e, dte, "C", cc.chain(e).calls, 0.25, 0.65)
    near = [(e, d) for e, d in exps if 7 <= d <= 90]
    below = [x for x in near if x[1] <= 30]
    above = [x for x in near if x[1] > 30]
    for e, dte in ([below[-1]] if below else []) + ([above[0]] if above else []):
        ch = cc.chain(e)
        take("ATM30", e, dte, "C", ch.calls, atm_only=True)
        take("ATM30", e, dte, "P", ch.puts, atm_only=True)
    return out, stamp


if __name__ == "__main__":
    s, tickers = sc.load_config()
    rows, stamps = [], set()
    for sym, cfg in tickers.items():
        if not cfg["options"]:
            continue
        try:
            r, stamp = rows_for(sym)
            rows += r
            stamps.add(stamp[:16])
            print(f"{sym:5} {len(r)} 张", file=sys.stderr)
        except Exception as e:
            print(f"{sym:5} 失败 {e!r}", file=sys.stderr)
    R = pd.DataFrame(rows)
    R["col-mid"] = (R["y_col"] - R["y_mid"]) * 100
    R["col-cboe"] = (R["y_col"] - R["c_iv"]) * 100
    R["mid-cboe"] = (R["y_mid"] - R["c_iv"]) * 100
    R["delta_diff"] = R["d_col"] - R["d_mid"]
    pd.set_option("display.width", 200)
    print(f"CBOE 时间戳: {sorted(stamps)}")

    def q(x):
        x = x.dropna()
        return (f"{x.median():+.2f} [{x.quantile(.25):+.2f}, {x.quantile(.75):+.2f}]"
                if len(x) else "—")

    out = {}
    for cat, g in R.groupby("cat"):
        out[cat] = {
            "合约数": len(g), "标的数": g["sym"].nunique(),
            "Yahoo列 − mid反解 (点)": q(g["col-mid"]),
            "Yahoo列 − CBOE (点)": q(g["col-cboe"]),
            "mid反解 − CBOE (点)": q(g["mid-cboe"]),
            "|Yahoo列−mid| > 1 点占比": f"{(g['col-mid'].abs() > 1).mean():.0%}",
            "delta 差 (Yahoo列 − mid)": q(g["delta_diff"] * 100 / 100),
        }
    print("\n中位数 [四分位区间]:")
    print(pd.DataFrame(out).to_string())
    print("\n按标的 (CSP, Yahoo列 − mid 的中位, 点):")
    print(R[R["cat"] == "CSP"].groupby("sym")["col-mid"].median().round(2).to_string())
