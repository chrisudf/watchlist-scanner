"""空头面回测 (todo.md #12; shortside.py 的档位阈值和提示里的数字全部出自这里)。

一、全市场: S&P 1500 (维基百科上的现成分股) 每只每期的 FINRA 空头, 按 结算日 + 8 个交易日
    (约等于 FINRA 公布那天) 的收盘入场, 看之后 6 / 12 个月: 相对 S&P 1500 等权的超额收益、之后
    半年的已实现波动、半年内再跌 20% 以上的比例、跑赢 / 跑输 30% 以上的比例。分组: 空头占流通股
    档位 (<5 / 5-10 / 10-20 / >=20%)、"创一年新高"、"较上期大涨"、回补天数, 再逐年看一遍。
二、watchlist 回放: 用 shortside 自己的解析 / 基准 / 档位函数把过去 24 期逐期重放, 数旧规则
    (占比 >=15%、一年新高、回补 >=5 天、较上期 >=+30% 任一条) 和新规则 (进档 ⚠️ / 单期大跳 ℹ️)
    各亮几次。

运行: .venv/bin/python research/shortside_backtest.py              (联网: FINRA 约 1,500 次请求
      ~7 分钟, yfinance 1,500 只日线 ~2 分钟, TradingView 流通股 1 次)
      .venv/bin/python research/shortside_backtest.py --cache      (只用 data/shortside_backtest/)
      .venv/bin/python research/shortside_backtest.py --watchlist  (只跑第二节, 联网, 十几秒)

口径和偏差 (读数字前先看):
  - 成分股是现在的: 期间被剔除的 (多半是暴跌或被收购) 不在样本里 —— 幸存者偏差, 对占比高的
    几档偏乐观。
  - 分母是现在的流通股 (TradingView 扫描器 float_shares_outstanding, 缺的用 Yahoo floatShares),
    套到全部历史; 一年里大量增发 / 回购的票有偏差, 只拿来分档。
  - 代码会被重复使用 (META 2022-06 以前是一只 ETF): 某期空头比此前 24 期中位数大 50 倍以上的
    当脏数据扔掉。FINRA 这份数据 2021-07 起, 要先攒 24 期, 所以入场从 2022-06 开始。
  - 同一只票相邻几期的持有期重叠: t 值先按结算日做横截面平均, 再 Newey-West (12 期) 调整。
  - 2022-06~2026-05 只有四年、大体上涨的一段; 只写"各档之间谁高谁低", 而且逐年都成立的才进
    shortside.py。2026-11-14 跑的结果见 README「空头面」和 lesson.md 同日那条。
"""
import argparse
import concurrent.futures as cf
import json
import sys
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import shortside as ss                             # noqa: E402

CACHE = ROOT / "data" / "shortside_backtest"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124 Safari/537.36"}
WIKI = ["https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
        "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies"]
PUBLISH_LAG = 8          # 结算日 -> 公布日, 交易日
H6, H12 = 126, 252
OLD_RULE = {"pct": 15.0, "dtc": 5.0, "chg": 30.0}    # 2026-10-28 上线时的旧阈值 (四条任一就亮)

pd.set_option("display.width", 200)


# ---------------------------------------------------------------- 取数

def wiki_symbols(url: str) -> list[str]:
    from bs4 import BeautifulSoup
    html = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60).read()
    for t in BeautifulSoup(html, "html.parser").find_all("table", class_="wikitable"):
        rows = t.find_all("tr")
        head = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])] if rows else []
        if "Symbol" in head and len(rows) > 90:
            j = head.index("Symbol")
            out = [r.find_all(["td", "th"])[j].get_text(strip=True) for r in rows[1:]
                   if len(r.find_all(["td", "th"])) > j]
            return [s for s in out if s]
    raise ValueError(f"{url}: 没找到带 Symbol 列的成分股表")


def finra_hist(sym: str, start: str = "2021-06-01") -> tuple[str, list | None]:
    body = {"compareFilters": [{"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": sym}],
            "dateRangeFilters": [{"fieldName": "settlementDate", "startDate": start,
                                  "endDate": date.today().isoformat()}],
            "limit": 500,
            "fields": ["settlementDate", "currentShortPositionQuantity",
                       "previousShortPositionQuantity", "averageDailyVolumeQuantity",
                       "daysToCoverQuantity", "stockSplitFlag"]}
    for a in range(5):
        try:
            return sym, ss._post_json(ss.FINRA_URL, body, 60)
        except Exception:          # noqa: BLE001
            time.sleep(1.5 * (a + 1))
    return sym, None


def tv_floats() -> dict[str, float]:
    body = {"filter": [{"left": "exchange", "operation": "in_range",
                        "right": ["NYSE", "NASDAQ", "AMEX"]}],
            "columns": ["name", "float_shares_outstanding", "total_shares_outstanding"],
            "range": [0, 20000]}
    req = urllib.request.Request("https://scanner.tradingview.com/america/scan",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **UA})
    out = {}
    for row in json.load(urllib.request.urlopen(req, timeout=60)).get("data", []):
        name, flt, tot = row["d"]
        if flt or tot:
            out[str(name).replace("/", ".")] = float(flt or tot)
    return out


def load(use_cache: bool):
    CACHE.mkdir(parents=True, exist_ok=True)
    f_syms, f_si, f_px, f_fl = (CACHE / n for n in ("universe.json", "si.json", "px.pkl", "float.json"))
    if use_cache:
        return (json.loads(f_syms.read_text()), json.loads(f_si.read_text()),
                pd.read_pickle(f_px), json.loads(f_fl.read_text()))
    syms = sorted({s for u in WIKI for s in wiki_symbols(u)})
    f_syms.write_text(json.dumps(syms))
    print(f"成分股 {len(syms)} 只", flush=True)
    si, t0 = {}, time.time()
    with cf.ThreadPoolExecutor(6) as ex:
        for i, (s, rows) in enumerate(ex.map(finra_hist, syms)):
            if rows is not None:
                si[s] = rows
            if i % 250 == 0:
                print(f"  FINRA {i}/{len(syms)} {time.time() - t0:.0f}s", flush=True)
    f_si.write_text(json.dumps(si))
    import yfinance as yf
    parts = []
    for i in range(0, len(syms), 250):
        chunk = [s.replace(".", "-") for s in syms[i:i + 250]]
        parts.append(yf.download(chunk, start="2021-05-01", auto_adjust=True, progress=False,
                                 threads=True)["Close"])
    px = pd.concat(parts, axis=1)
    px.columns = [c.replace("-", ".") for c in px.columns]
    px.to_pickle(f_px)
    fl = tv_floats()
    fl = {s: fl[s] for s in syms if s in fl}
    for s in (s for s in syms if s not in fl):
        try:
            v = yf.Ticker(s.replace(".", "-")).info.get("floatShares")
            if v:
                fl[s] = float(v)
        except Exception:          # noqa: BLE001
            pass
    f_fl.write_text(json.dumps(fl))
    return syms, si, px, fl


# ---------------------------------------------------------------- 一、全市场

def panel(si: dict, px: pd.DataFrame, fl: dict) -> pd.DataFrame:
    px = px.sort_index().dropna(axis=1, how="all")
    idx, vals = px.index, px.values
    fwd6 = (px.shift(-H6) / px - 1).values
    fwd12 = (px.shift(-H12) / px - 1).values
    b6, b12 = np.nanmean(fwd6, axis=1), np.nanmean(fwd12, axis=1)       # S&P 1500 等权
    vol6 = (np.log(px).diff().rolling(H6, min_periods=100).std() * np.sqrt(252)).shift(-H6).values
    hi1y = px.rolling(252, min_periods=200).max().values
    recs = []
    for s, rows in si.items():
        if not rows or s not in px.columns or not fl.get(s):
            continue
        df = pd.DataFrame(rows).dropna(subset=["currentShortPositionQuantity"])
        df["d"] = pd.to_datetime(df["settlementDate"])
        df = df.sort_values("d").drop_duplicates("d")
        q = df["currentShortPositionQuantity"].astype(float).values
        adv = df["averageDailyVolumeQuantity"].astype(float).values
        j = px.columns.get_loc(s)
        for n in range(24, len(df)):
            prev = q[n - 24:n]
            if prev.min() <= 0 or q[n] / np.median(prev) > 50:
                continue
            p = idx.searchsorted(df["d"].iloc[n]) + PUBLISH_LAG
            if p >= len(idx) or np.isnan(vals[p, j]):
                continue
            fut = vals[p + 1:p + 1 + H6, j]
            mdd = (np.nanmin(fut) / vals[p, j] - 1) if len(fut) == H6 and not np.isnan(fut).all() else np.nan
            recs.append({
                "sym": s, "settle": df["d"].iloc[n], "year": idx[p].year,
                "pct": q[n] / fl[s] * 100, "new_high": q[n] > prev.max(),
                "chg": (q[n] / q[n - 1] - 1) * 100 if q[n - 1] > 0 else np.nan,
                "dtc": q[n] / adv[n] if adv[n] else np.nan,
                "dd1y": vals[p, j] / hi1y[p, j] - 1,
                "xs6": fwd6[p, j] - b6[p], "xs12": fwd12[p, j] - b12[p],
                "vol6": vol6[p, j], "mdd6": mdd})
    ev = pd.DataFrame(recs)
    return ev[ev.pct < 100]


def nw_t(x, lags: int = 12) -> float:
    x = np.asarray(x, float)
    x = x[~np.isnan(x)]
    n = len(x)
    if n < 10:
        return float("nan")
    e = x - x.mean()
    s = e @ e / n
    for k in range(1, min(lags, n - 1) + 1):
        s += 2 * (1 - k / (lags + 1)) * (e[k:] @ e[:-k]) / n
    return x.mean() / np.sqrt(s / n)


def table(groups: dict) -> pd.DataFrame:
    out = {}
    for name, d in groups.items():
        d6 = d.dropna(subset=["xs6", "mdd6"])
        if len(d6) < 30:
            continue
        fm = d6.groupby("settle").xs6.mean()
        out[name] = {
            "样本": len(d6), "股票": d6.sym.nunique(),
            "半年波动": d6.vol6.median() * 100,
            "再跌≥20%": (d6.mdd6 <= -0.20).mean() * 100,
            "涨≥30%": (d6.xs6 >= 0.30).mean() * 100, "跌≥30%": (d6.xs6 <= -0.30).mean() * 100,
            "6月超额中位": d6.xs6.median() * 100, "6月超额均值": d6.xs6.mean() * 100,
            "t(均值)": nw_t(fm.values),
            "12月超额中位": d.xs12.dropna().median() * 100}
    return pd.DataFrame(out).T.round(1)


def market(ev: pd.DataFrame) -> None:
    p = ev.pct
    tiers = {"占比 <5%": ev[p < 5], "5-10%": ev[(p >= 5) & (p < 10)],
             "10-20%": ev[(p >= 10) & (p < 20)], ">=20%": ev[p >= 20]}
    print(f"\n一、全市场: {len(ev):,} 个 (票, 期), {ev.sym.nunique()} 只, "
          f"入场 {ev.settle.min().date()} ~ {ev.settle.max().date()} (满 6 个月的才进表)")
    print("\n1. 按空头占流通股分档 (超额 = 相对 S&P 1500 等权, %)")
    print(table(tiers).to_string())
    print("\n2. 旧规则那几条单独看 (占比低的时候)")
    print(table({
        "一年新高 & 占比 <5%": ev[ev.new_high & (p < 5)],
        "较上期 >=+50% & 占比 <5%": ev[(ev.chg >= 50) & (p < 5)],
        "回补 >=5 天 & 占比 <10%": ev[(ev.dtc >= 5) & (p < 10)],
        "一年新高 & 占比 >=10%": ev[ev.new_high & (p >= 10)],
        "占比 >=10% & 跌离一年高点 30%+": ev[(p >= 10) & (ev.dd1y <= -0.30)],
        "占比 <10% & 跌离一年高点 30%+": ev[(p < 10) & (ev.dd1y <= -0.30)]}).to_string())
    print("\n3. 逐年 (入场年份): 半年波动中位 / 半年内再跌 >=20% 的比例")
    d6 = ev.dropna(subset=["xs6", "mdd6"]).copy()
    d6["档"] = pd.cut(d6.pct, [0, 5, 10, 20, 100], right=False,
                     labels=["<5%", "5-10%", "10-20%", ">=20%"])
    vol = d6.pivot_table(index="档", columns="year", values="vol6", aggfunc="median", observed=True) * 100
    dd = d6.assign(f=(d6.mdd6 <= -0.2) * 100.0).pivot_table(
        index="档", columns="year", values="f", aggfunc="mean", observed=True)
    out = pd.DataFrame({c: ["—" if pd.isna(vol.loc[i, c]) else f"{vol.loc[i, c]:.0f}% / {dd.loc[i, c]:.0f}%"
                            for i in vol.index] for c in vol.columns}, index=vol.index)
    print(out.to_string())


# ---------------------------------------------------------------- 二、watchlist 回放

def watchlist_replay() -> None:
    import scanner as sc
    settings, tickers = sc.load_config()
    stocks = [s for s, c in tickers.items() if c.get("kind", "stock") == "stock"]
    today = date.today()
    body = ss.finra_request(stocks, today, lookback=800)
    body["limit"] = 5000
    recs = ss._post_json(ss.FINRA_URL, body, 90)
    shares = ss.fetch_shares(stocks)
    dates = sorted({r["settlementDate"] for r in recs})[-24:]
    old = OLD_RULE
    n_old, n_new, n_jump, why_old, lines = 0, 0, 0, {}, []
    for d in dates:
        d0 = (date.fromisoformat(d) - timedelta(days=400)).isoformat()
        for s in stocks:
            sl = [r for r in recs if r["symbolCode"] == s and d0 <= r["settlementDate"] <= d]
            sh = dict(shares.get(s) or {})
            if not sl or max(r["settlementDate"] for r in sl) != d or not sh:
                continue
            if sh.get("since") and d < sh["since"]:
                continue
            if sh.get("split") and sh["split"]["date"] > d:      # 那时还没拆, 用拆前股本
                sh["shares"] /= sh["split"]["ratio"]
                sh["split"] = None
            v = ss.parse_finra(sl)[s]
            info = {"si": v, "shares": sh, "borrow": None}       # 借券费没有历史: 不判逼空
            pct, base = ss.si_pct(info), ss.si_baseline(info)
            hit = [k for k, ok in (
                ("占比", pct is not None and pct >= old["pct"]),
                ("一年新高", bool(base and base["new_high"])),
                ("回补", v.get("dtc") is not None and v["dtc"] >= old["dtc"]),
                ("较上期", v.get("chg") is not None and v["chg"] >= old["chg"])) if ok]
            if hit:
                n_old += 1
                key = "+".join(hit)
                why_old[key] = why_old.get(key, 0) + 1
            if flag := ss.si_flag(info, settings):
                n_new += 1
                lines.append(f"  ⚠️ {d} {s} {flag[:40]}…")
            if jump := ss.si_jump(info, settings):
                n_jump += 1
                lines.append(f"  ℹ️ {d} {s} {jump[:40]}…")
    print(f"\n二、watchlist 回放: {len(stocks)} 只个股 × {len(dates)} 期 ({dates[0]} ~ {dates[-1]})")
    print(f"  旧规则亮 {n_old} 次; 按条件: " + ", ".join(f"{k} {v}" for k, v in
                                                   sorted(why_old.items(), key=lambda x: -x[1])))
    print(f"  新规则 ⚠️ {n_new} 次, ℹ️ {n_jump} 次 (借券费没有历史, 逼空那条没算)")
    print("\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", action="store_true", help="只用 data/shortside_backtest/ 的缓存")
    ap.add_argument("--watchlist", action="store_true", help="只跑第二节")
    a = ap.parse_args()
    if not a.watchlist:
        _, si, px, fl = load(a.cache)
        market(panel(si, px, fl))
    watchlist_replay()
    return 0


if __name__ == "__main__":
    sys.exit(main())
