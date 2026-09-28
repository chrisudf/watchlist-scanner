"""30 天平值 IV: 旧算法 (Yahoo 列优先) vs 新算法 (mid 优先) vs CBOE, 同一时点对比。

运行: .venv/Scripts/python.exe research/iv30_method_compare.py   (联网, 约 2 分钟)
背景见 lesson.md 2026-09-28 "30 天平值 IV 换算法"。

三个值都用扫描器 atm_iv30 的同一套选法 (7-90 DTE 里夹住 30 天的两个到期, 离现价
最近的 call/put 取均值, 按 DTE 线性插值到 30 天), 只换每腿 IV 的来源:
  old   contract_iv  (Yahoo impliedVolatility 列, 不合理才 mid 反解)  —— 9/25 前的算法
  new   mid_first_iv (bid/ask mid 按扫描器模型反解, 反解不出才 Yahoo 列) —— 9/28 起
  cboe  CBOE 延迟链自己的 IV (独立参照, 模型不同 → 与 new 有常数差)
判据: 自建 IVP 是自己跟自己比, 要的是**稳定**而不是绝对水平 —— 看 (old − cboe) 与
(new − cboe) 去掉中位数后的离散度 (MAD), 越小说明噪声越小。

⚠️ 只能在**有实时盘口**时跑 (美股盘中, 或收盘后不久)。盘外 Yahoo 会清空盘口,
IV 列变成 1e-05, old 也退回反解, 两者恒等 —— 2026-09-28 的第一次离线对比就是这样
作废的。脚本会数一下有盘口的腿, 不到一半就直接判无效。

历史模式 (推荐, 用生产数据):
    .venv/Scripts/python.exe research/iv30_method_compare.py --history data/iv_history.csv
读扫描器每天 15:45 ET 写入的 iv30 (新算法) 与影子列 iv30_ycol (旧算法), 比较两条
序列的日间跳动 —— 真实波动两条序列一起走, 多出来的跳动就是噪声。
"""
import argparse
import json, os, re, sys, urllib.request

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import scanner as sc  # noqa: E402


def cboe_legs(sym):
    req = urllib.request.Request(
        f"https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json",
        headers={"User-Agent": "Mozilla/5.0"})
    d = json.load(urllib.request.urlopen(req, timeout=60))["data"]
    legs = {}
    for o in d["options"]:
        m = re.match(rf"{sym}(\d{{6}})([CP])(\d{{8}})$", o["option"])
        if m and o.get("iv"):
            exp = f"20{m.group(1)[:2]}-{m.group(1)[2:4]}-{m.group(1)[4:]}"
            legs[(exp, m.group(2), int(m.group(3)) / 1000)] = float(o["iv"])
    return legs, d.get("last_trade_time", "?")


QUOTED = {"legs": 0, "live": 0}


def iv30(cc, spot, leg_iv):
    """atm_iv30 的选法, 每腿 IV 由 leg_iv(row, mid, T, is_call, exp) 给。"""
    usable = [(e, d) for e, d in cc.expiries() if 7 <= d <= 90]
    below = [x for x in usable if x[1] <= 30]
    above = [x for x in usable if x[1] > 30]
    picks = ([below[-1]] if below else []) + ([above[0]] if above else [])
    pts = []
    for exp, dte in picks:
        ch = cc.chain(exp)
        T = dte / 365.0
        ivs = []
        for df, is_call in ((ch.calls, True), (ch.puts, False)):
            if df is None or df.empty:
                continue
            row = df.loc[(df["strike"] - spot).abs().idxmin()]
            mid, src = sc._mark(row, sc._stale_cutoff())
            QUOTED["legs"] += 1
            QUOTED["live"] += src == "live"
            iv = leg_iv(row, mid, T, is_call, exp)
            if iv:
                ivs.append(iv)
        if ivs:
            pts.append((dte, sum(ivs) / len(ivs)))
    if not pts:
        return None
    if len(pts) == 1 or pts[0][0] == pts[1][0]:
        return pts[0][1]
    (d1, v1), (d2, v2) = pts
    return v1 + (v2 - v1) * (30 - d1) / (d2 - d1)


def history_mode(path):
    H = pd.read_csv(path)
    if "iv30_ycol" not in H:
        print("iv_history 里还没有影子列 iv30_ycol —— 新代码部署后的收盘扫描才开始写")
        return
    H = H[(H.get("iv_src") == sc.IV30_METHOD) & H["iv30"].notna() & H["iv30_ycol"].notna()]
    if H.empty:
        print("还没有同时带新旧两个值的行")
        return
    H = H.sort_values(["symbol", "date"])
    out = []
    for sym, g in H.groupby("symbol"):
        new, old = g["iv30"] * 100, g["iv30_ycol"] * 100
        out.append(dict(sym=sym, 天数=len(g), 旧减新_均值=(old - new).mean(),
                        旧减新_最大=(old - new).abs().max(),
                        新_日间跳动=new.diff().abs().median(),
                        旧_日间跳动=old.diff().abs().median()))
    R = pd.DataFrame(out).set_index("sym")
    pd.set_option("display.width", 200)
    print(f"{H['date'].min()} ~ {H['date'].max()}, {H['date'].nunique()} 个交易日")
    print(R.round(2).to_string())
    print(f"\n日间跳动中位 (点): 新 {R['新_日间跳动'].median():.2f} / 旧 {R['旧_日间跳动'].median():.2f}; "
          f"旧 > 新 的标的 {(R['旧_日间跳动'] > R['新_日间跳动']).sum()}/{len(R)}")
    print("判读: 旧的日间跳动明显更大 = 新算法确实更稳, 保留; 两者相当 = 换不换无所谓, 保留新的"
          "(与 LEAP/spread 口径一致); 新的反而更大 = 退回 (IV30_METHOD 改回, 旧行重新参与排位)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", help="iv_history.csv 路径 → 用生产影子列做对比")
    a = ap.parse_args()
    if a.history:
        history_mode(a.history)
        sys.exit(0)
    s, tickers = sc.load_config()
    rows, stamps = [], set()
    for sym, cfg in tickers.items():
        if not cfg["options"]:
            continue
        try:
            cc = sc.ChainCache(sym)
            spot = float(cc.tk.history(period="5d")["Close"].iloc[-1])
            legs, stamp = cboe_legs(sym)
            stamps.add(stamp[:16])
            old = iv30(cc, spot, lambda row, mid, T, c, e: sc.contract_iv(row, mid, spot, T, c))
            new = sc.atm_iv30(cc, spot)
            cb = iv30(cc, spot, lambda row, mid, T, c, e:
                      legs.get((e, "C" if c else "P", float(row["strike"]))))
            rows.append(dict(sym=sym, old=old, new=new, cboe=cb))
        except Exception as e:
            print(f"{sym} 失败 {e!r}", file=sys.stderr)
    live_share = QUOTED["live"] / max(QUOTED["legs"], 1)
    if live_share < 0.5:
        print(f"⚠️ 只有 {live_share:.0%} 的腿有实时盘口 —— Yahoo 盘口已清空, old 会退回反解、"
              "与 new 恒等, 本次对比无效。请在美股盘中重跑, 或用 --history 看生产影子列。")
    R = pd.DataFrame(rows).set_index("sym")
    R["new-old"] = (R["new"] - R["old"]) * 100
    R["old-cboe"] = (R["old"] - R["cboe"]) * 100
    R["new-cboe"] = (R["new"] - R["cboe"]) * 100
    pd.set_option("display.width", 200)
    print(f"CBOE 时间戳: {sorted(stamps)}")
    print((R[["old", "new", "cboe"]] * 100).round(1).join(R[["new-old", "old-cboe", "new-cboe"]].round(2)).to_string())

    def mad(x):
        x = x.dropna()
        return float((x - x.median()).abs().median())

    print(f"\n新 − 旧: 中位 {R['new-old'].median():+.2f} 点, 最大 |差| {R['new-old'].abs().max():.2f} 点")
    for k in ("old-cboe", "new-cboe"):
        x = R[k].dropna()
        print(f"{k:9}: 中位 {x.median():+.2f} 点  去掉中位后的离散度 MAD {mad(x):.2f} 点  "
              f"最大偏离 {(x - x.median()).abs().max():.2f} 点  (n={len(x)})")
