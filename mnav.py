#!/usr/bin/env python3
"""BTC 财库公司 mNAV (StrategyTracker) — 取数 / 三个口径 / 入场规则 / 报告段.

只做提示, 不进状态机/门控/票据 (同 insider / revisions)。标的默认只有 MSTR
(watchlist.toml 的 [mnav.MSTR]); 不配置这张表整段不出。

三个口径 (2026-10-17 研究, 全部逐日核对过):
  - 稀释权益 mNAV = 收盘 × 稀释股本 ÷ (持币 × BTC)  —— saylortracker / mstr-tracker /
    strategytracker (三个域名同一产品) 默认那条线
  - 基本权益 mNAV = 收盘 × 基本股本 ÷ (持币 × BTC)
  - 企业价值 mNAV = (基本市值 + 可转债面值 + 优先股面值 − 美元储备/现金) ÷ (持币 × BTC)
    —— 分子就是 strategy.com 首页 "Enterprise Value" 的定义 (术语表: Market Cap of Basic
    Shares + Debt + Pref − USD Assets; Debt 自 2026-08-31 起只含可转债), 分母是它的 BTC
    Reserve。注意官网首页显示的 "mNAV" 自 2026-07-23 起换了定义: 股价 ÷ 每股净 BTC (全
    稀释股本; 价外转债面值 + 优先股面值 − 美元资产 先从持币里扣), 函数形式不同, 只在
    1.0 附近与本口径重合 (10/16 官网 1.08 vs 本口径 1.05)。两种历史序列网站都没有,
    净债权三个数字手工维护在配置里 (debt_b / pref_b / usd_b, 来源 = 官网首页 Debt /
    Pref / USD Reserve / USD Cash, 或每周 8-K), 超过 claims_stale_days 天在报告里提醒。

回测结论 (2020-08-11 → 2026-10-16, 1,554 个交易日; research/mnav_backtest.py 可复现,
样本本质只有 2022 和 2026 两个 regime, 胜率要按"两次经验"读):
  - 稀释 0.8–0.9 是最坏的桶: 之后 20 天中位 −8% / 胜率 44%, 入场后 60 天内最大回撤
    中位 −30%。它不是便宜区, 是跌向 0.7 的路上。
  - 首次收盘跌破 0.9 (之前 ≥20 天在上方) 的 4 次, 之后 60 天内都再跌 31–35% 才见底。
  - 0.7–0.8 桶: 20 天中位 +22% / 75%, 60 天中位 +61% / 100% (3 段); <0.7 只有 5 天。
  - 2026 的三个底稀释 mNAV 各不相同 (0.63 / 0.68 / 0.72), 企业价值 mNAV 全在
    0.96–0.99; 全样本没有一天低于 0.96 —— 企业价值平价是底 (买下整个资本结构比
    买 BTC 便宜, 公司也在这里回购优先股)。净债权占 BTC 市值的比例变了, 平价对应
    的稀释 mNAV 就变: 2026-06 (38%) 是 0.63, 2026-10 (22%) 是 0.84。
  - BTC 必须参考: 基本口径 <0.8 的日子里, 之后 20 天 BTC 涨 → MSTR 中位 +32% / 90%,
    BTC 跌 → −11% / 15% (相对 BTC 仍 +7%); BTC 在 20 日均线上时 20 天胜率 79% vs 59%。
    2022-05-03 基本 0.80 入场, BTC 之后 60 天 −37%, MSTR 最大回撤 −56%。
  - 2026-07/08 公司在稀释 0.65–0.80 就通过 ATM 卖普通股 (8-K: 八周约 $3.2B, 股本
    371.6M → 420.5M, BTC/股 −14%), mNAV 修复只回到 0.95–0.98 —— 止盈放 0.90–0.95。

规则 (可在配置里改阈值):
  T1 = min(企业价值 ≤ t1_ev 的价格, 稀释 ≤ t1_dil 的价格), T2 = 稀释 ≤ t2_dil,
  T3 = 稀释 ≤ t3_dil (2026 低点, 只在创新低时买); 全部按当时 BTC 实时换算成价格,
  同时给 BTC −10% / −20% 的价位。BTC 在 20 日均线下只下半仓。

数据: data.strategytracker.com 的公开 JSON (latest.json 给版本号和全量文件名, 全量
约 20MB, 版本号没变就用缓存 data/mnav_strategytracker.json, 缓存只留我们要的公司
的几列)。取数失败退回缓存并出一行 ⚠️; 没有缓存就整段降级成一行错误。
historicalData 是日历天 (周末沿用周五股价、BTC 继续动), 统计一律只数交易日。
股本按 StrategyTracker 的 total_outstanding_shares (= A+B 类, 与官网市值一致;
processedMetrics.sharesOutstanding 只有 A 类, 别用); 它按月跳变, ATM 密集的月份
网站读数会偏低几个百分点 —— 报告印股本上次变动日期。

命令行 (手工看一眼; 不写历史):
    .venv/bin/python mnav.py [MSTR]
"""
from __future__ import annotations

import csv
import json
import math
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

LATEST_URL = "https://data.strategytracker.com/latest.json"
FILE_URL = "https://data.strategytracker.com/{}"
UA = {"User-Agent": "Mozilla/5.0 (watchlist-scanner mnav)"}
TIMEOUT = 60
CACHE_NAME = "mnav_strategytracker.json"
HISTORY_NAME = "mnav_history.csv"
HIST_COLS = ("dates", "stock_prices", "btc_prices", "btc_balance",
             "total_outstanding_shares", "diluted_shares_outstanding",
             "nav_premium_basic", "nav_premium_diluted")
STATS_THRESHOLDS = (1.0, 0.9, 0.8, 0.7, 0.6)
# NYSE 全休市日 2020-08 → 2025 (序列从 2020-08-11 起; 2026 起由 scanner.NYSE_HOLIDAYS 传入,
# trading_rows 把两份并起来)。网站在休市日照样出一行 (股价沿用前一日), 不剔会多算 ~50 行
PAST_HOLIDAYS = frozenset(map(date.fromisoformat, (
    "2020-09-07", "2020-11-26", "2020-12-25",
    "2021-01-01", "2021-01-18", "2021-02-15", "2021-04-02", "2021-05-31", "2021-07-05",
    "2021-09-06", "2021-11-25", "2021-12-24",
    "2022-01-17", "2022-02-21", "2022-04-15", "2022-05-30", "2022-06-20", "2022-07-04",
    "2022-09-05", "2022-11-24", "2022-12-26",
    "2023-01-02", "2023-01-16", "2023-02-20", "2023-04-07", "2023-05-29", "2023-06-19",
    "2023-07-04", "2023-09-04", "2023-11-23", "2023-12-25",
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19",
    "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
)))
EPISODE_GAP = 5                 # 中间断开 ≤ 这么多交易日算同一段

# 回测文案 (2026-10-17 快照, 稀释口径; 数字与 research/mnav_backtest.py 同源)
BUCKET_EDGES = ((0.7, "<0.7"), (0.8, "0.7-0.8"), (0.9, "0.8-0.9"),
                (1.0, "0.9-1.0"), (1.2, "1.0-1.2"), (math.inf, "≥1.2"))
BUCKET_NOTE = {
    "<0.7": "历史只有 5 天 (2 段), 20/60 天全部为正 — 深档区",
    "0.7-0.8": "20 天中位 +22%/胜率 75%, 60 天中位 +61%/100% (3 段) — 有正期望的入场区",
    "0.8-0.9": "20 天中位 −8%/胜率 44%, 入场后 60 天内最大回撤中位 −30% — 历史最坏的桶, 不入场",
    "0.9-1.0": "20 天中位 −2%/胜率 49%, 60 天中位 −12% — 不便宜",
    "1.0-1.2": "溢价区, 与 mNAV 规则无关",
    "≥1.2": "溢价区, 与 mNAV 规则无关",
}
BREAK09_NOTE = ("历史 4 次同类事件 (2022-05-09 / 2025-12-30 / 2026-04-01 / 2026-06-03) "
                "之后 60 天内都再跌 31–35% 才见底")
BTC_FILTER_NOTE = "稀释 <0.85 的日子: BTC 在 20 日均线上 20 天胜率 79%, 线下 59%"

CFG_DEFAULTS = {
    "st_ticker": None,          # StrategyTracker 代码, 默认 = 表名
    "debt_b": None,             # 可转债面值 $B (官网 Debt)
    "pref_b": None,             # 优先股清算价值 $B (官网 Pref)
    "usd_b": 0.0,               # USD Reserve + USD Cash $B
    "claims_asof": None,        # 上面三个数字的日期
    "claims_stale_days": 45,
    "t1_ev": 0.97, "t1_dil": 0.80, "t2_dil": 0.70, "t3_dil": 0.63,
    "trap": [0.80, 0.90],       # 最坏桶
    "take_profit": [0.90, 0.95],  # 到下沿就提醒 (含上方), 带只是文案
    "break_lookback": 20,       # "首次跌破": 之前这么多交易日都在阈值上方
    "recross_min_below": 5,     # "重新站上": 之前这么多交易日都在阈值下方
    "tp_memory_days": 60,       # 止盈提醒只在近这么多交易日内到过 t1_dil 线之后才出
    "notes": "",
}
HISTORY_FIELDS = ("date", "symbol", "px", "btc", "holdings", "shares_basic",
                  "shares_dil", "m_basic", "m_dil", "m_ev", "debt_b", "pref_b",
                  "usd_b", "bucket", "flags", "px_src", "st_version")


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

def _finite(x) -> bool:
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def _iso(v, what: str):
    if isinstance(v, datetime):
        v = v.date()
    if not isinstance(v, date):
        try:
            v = date.fromisoformat(str(v))
        except (ValueError, TypeError):
            raise ValueError(f"{what} 必须是日期 (YYYY-MM-DD), 得到 {v!r}") from None
    return v


def validate_config(raw: dict | None, today: date) -> dict:
    """[mnav] 表 -> {SYMBOL: cfg}。同 load_config 的脾气: 未知键 / 类型错 / 顺序错
    在装载时就炸。表缺省 = {} (整段不出)。"""
    out = {}
    for sym, tcfg in (raw or {}).items():
        if not isinstance(tcfg, dict):
            raise ValueError(f"[mnav.{sym}] 必须是一张表 (debt_b/pref_b/...)")
        unknown = set(tcfg) - set(CFG_DEFAULTS)
        if unknown:
            raise ValueError(f"[mnav.{sym}] 未知键 (typo?): {', '.join(sorted(unknown))}")
        cfg = {**CFG_DEFAULTS, **tcfg}
        for k in ("debt_b", "pref_b", "usd_b"):
            if not _finite(cfg[k]) or cfg[k] < 0:
                raise ValueError(f"[mnav.{sym}] {k} 必须是非负数 ($B), 得到 {cfg[k]!r}")
        asof = cfg["claims_asof"]
        if asof is None:
            raise ValueError(f"[mnav.{sym}] 缺 claims_asof — 债权数字要带日期才能提醒过期")
        asof = _iso(asof, f"[mnav.{sym}] claims_asof")
        if asof > today:
            raise ValueError(f"[mnav.{sym}] claims_asof {asof} 在未来")
        cfg["claims_asof"] = asof.isoformat()
        for k in ("t1_ev", "t1_dil", "t2_dil", "t3_dil"):
            if not _finite(cfg[k]) or cfg[k] <= 0:
                raise ValueError(f"[mnav.{sym}] {k} 必须是正数, 得到 {cfg[k]!r}")
        if not (cfg["t3_dil"] < cfg["t2_dil"] < cfg["t1_dil"]):
            raise ValueError(f"[mnav.{sym}] 阶梯顺序错: 要 t3_dil < t2_dil < t1_dil")
        for k in ("trap", "take_profit"):
            v = cfg[k]
            if not (isinstance(v, (list, tuple)) and len(v) == 2
                    and all(_finite(x) and x > 0 for x in v) and v[0] < v[1]):
                raise ValueError(f"[mnav.{sym}] {k} 必须是 [低, 高] 两个正数, 得到 {v!r}")
            cfg[k] = [float(v[0]), float(v[1])]
        for k in ("claims_stale_days", "break_lookback", "recross_min_below", "tp_memory_days"):
            if not (isinstance(cfg[k], int) and not isinstance(cfg[k], bool)
                    and cfg[k] > 0):
                raise ValueError(f"[mnav.{sym}] {k} 必须是正整数, 得到 {cfg[k]!r}")
        if cfg["st_ticker"] is not None and not isinstance(cfg["st_ticker"], str):
            raise ValueError(f"[mnav.{sym}] st_ticker 必须是字符串")
        if not isinstance(cfg["notes"], str):
            raise ValueError(f"[mnav.{sym}] notes 必须是字符串")
        key = sym.upper()
        if key in out:
            raise ValueError(f"[mnav] 重复条目 (含大小写差异): {key}")
        cfg["st_ticker"] = (cfg["st_ticker"] or key).upper()
        out[key] = cfg
    return out


# --------------------------------------------------------------------------
# Fetch + cache
# --------------------------------------------------------------------------

def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def _subset(company: dict) -> dict:
    """全量文件里一家公司 -> 只留我们要的 (缓存小到几百 KB)。"""
    hist = company.get("historicalData") or {}
    pm = company.get("processedMetrics") or {}
    return {"historicalData": {k: hist.get(k) for k in HIST_COLS},
            "stockPriceDate": pm.get("stockPriceDate"),
            "companyName": pm.get("companyName")}


def fetch_companies(symbols: list[str], cache_path: Path, get=_get) -> dict:
    """-> {"version", "timestamp", "companies": {ST代码: subset}, "warnings": [..],
    "from_cache": bool}。版本号没变就用缓存; 取数失败退回缓存 (带 ⚠️); 没缓存就抛。"""
    cached = None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    want = [s.upper() for s in symbols]
    try:
        latest = json.loads(_get_text(get, LATEST_URL))
        version = str(latest["version"])
        if (cached and cached.get("version") == version
                and all(s in (cached.get("companies") or {}) for s in want)):
            return {**cached, "warnings": [], "from_cache": True}
        full = json.loads(_get_text(get, FILE_URL.format(latest["files"]["full"])))
        comps = full.get("companies") or {}
        missing = [s for s in want if s not in comps]
        if missing:
            raise KeyError(f"StrategyTracker 没有 {', '.join(missing)}")
        out = {"version": version, "timestamp": full.get("timestamp"),
               "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "companies": {s: _subset(comps[s]) for s in want}}
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, cache_path)
        return {**out, "warnings": [], "from_cache": False}
    except Exception as e:          # noqa: BLE001
        why = f"{type(e).__name__}: {str(e).splitlines()[0][:80]}" if str(e) else type(e).__name__
        if cached and all(s in (cached.get("companies") or {}) for s in want):
            return {**cached, "from_cache": True,
                    "warnings": [f"StrategyTracker 取数失败 ({why}), 用的是 "
                                 f"{str(cached.get('timestamp'))[:16]} 的缓存"]}
        raise RuntimeError(f"StrategyTracker 取数失败且无缓存 ({why})") from e


def _get_text(get, url: str) -> str:
    raw = get(url)
    return raw.decode("utf-8") if isinstance(raw, bytes) else raw


# --------------------------------------------------------------------------
# Frame
# --------------------------------------------------------------------------

def frame(hist: dict) -> pd.DataFrame:
    """historicalData -> DataFrame (index = 日历日)。缺持币/股本的行 (采用前) 丢掉。"""
    dates = hist.get("dates") or []
    cols = {k: hist.get(k) for k in HIST_COLS if k != "dates"}
    n = len(dates)
    for k, v in cols.items():
        if not isinstance(v, list) or len(v) != n:
            raise ValueError(f"historicalData.{k} 长度与 dates 不符")
    df = pd.DataFrame(cols, index=pd.to_datetime(dates))
    df = df.apply(pd.to_numeric, errors="coerce")
    df = df[(df.btc_balance > 0) & df.total_outstanding_shares.notna()
            & df.diluted_shares_outstanding.notna() & (df.stock_prices > 0)
            & (df.btc_prices > 0)]
    return df.sort_index()


def trading_rows(df: pd.DataFrame, holidays=frozenset()) -> pd.DataFrame:
    """只留交易日: 周一到周五且不在假日表 (传入的 + PAST_HOLIDAYS)。网站是日历天
    (周末/休市日沿用前一日股价, BTC 继续动), 那些行的"新低"不能算。"""
    hol = frozenset(holidays) | PAST_HOLIDAYS
    keep = [(d.weekday() < 5) and (d.date() not in hol) for d in df.index]
    return df[keep]


# --------------------------------------------------------------------------
# Pure math
# --------------------------------------------------------------------------

def bucket(m: float) -> str:
    for edge, label in BUCKET_EDGES:
        if m < edge:
            return label
    return BUCKET_EDGES[-1][1]


def mnav(price: float, shares: float, holdings: float, btc: float) -> float:
    return price * shares / (holdings * btc)


def ev_mnav(price: float, shares_basic: float, claims_b: float,
            holdings: float, btc: float) -> float:
    """(基本市值 + 净债权) ÷ BTC 市值。claims_b = debt + pref − usd ($B)。"""
    return (price * shares_basic + claims_b * 1e9) / (holdings * btc)


def price_at(m: float, holdings: float, shares: float, btc: float) -> float:
    """稀释 (或基本) mNAV = m 时的股价。"""
    return m * holdings * btc / shares


def parity_price(holdings: float, btc: float, claims_b: float,
                 shares_basic: float, ev: float = 1.0) -> float:
    """企业价值 mNAV = ev 时的股价 (基本股本口径的市值)。"""
    return (ev * holdings * btc - claims_b * 1e9) / shares_basic


def ladder(holdings, shares_basic, shares_dil, btc, claims_b, cfg) -> dict:
    """三档价格, 按当前 BTC 和 BTC −10% / −20% 各给一组。T1 取企业价值线与稀释线
    中更低的价 (哪条更低记在 t1_bind)。"""
    out = {"btc_grid": [1.0, 0.9, 0.8], "t1": [], "t1_ev_px": [], "t1_dil_px": [],
           "t2": [], "t3": [], "t1_bind": []}
    for f in out["btc_grid"]:
        b = btc * f
        p_ev = parity_price(holdings, b, claims_b, shares_basic, cfg["t1_ev"])
        p_dil = price_at(cfg["t1_dil"], holdings, shares_dil, b)
        out["t1_ev_px"].append(p_ev)
        out["t1_dil_px"].append(p_dil)
        out["t1"].append(min(p_ev, p_dil))
        out["t1_bind"].append("EV" if p_ev <= p_dil else "稀释")
        out["t2"].append(price_at(cfg["t2_dil"], holdings, shares_dil, b))
        out["t3"].append(price_at(cfg["t3_dil"], holdings, shares_dil, b))
    return out


def episodes(mask: pd.Series, gap: int = EPISODE_GAP) -> list[tuple]:
    """连续为真的区间 [(起, 止, 天数)], 中间断开 ≤ gap 行算同一段。"""
    ix = np.flatnonzero(mask.to_numpy(dtype=bool))
    if len(ix) == 0:
        return []
    runs, start, prev = [], ix[0], ix[0]
    for i in ix[1:]:
        if i - prev > gap:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    idx = mask.index
    return [(idx[a].date(), idx[b].date(), int(mask.iloc[a:b + 1].sum()))
            for a, b in runs]


def threshold_stats(m: pd.Series, thresholds=STATS_THRESHOLDS,
                    gap: int = EPISODE_GAP) -> dict:
    """{阈值: {"days", "episodes", "runs"}} —— 交易日口径。"""
    s = m.dropna()
    out = {}
    for thr in thresholds:
        runs = episodes(s < thr, gap)
        out[thr] = {"days": int((s < thr).sum()), "episodes": len(runs), "runs": runs}
    return out


def first_break_below(m: pd.Series, thr: float, lookback: int) -> bool:
    """最后一个值 < thr, 且之前 lookback 个值都 ≥ thr (样本不足 lookback 不算)。"""
    s = m.dropna()
    if len(s) < lookback + 1:
        return False
    return bool(s.iloc[-1] < thr and (s.iloc[-lookback - 1:-1] >= thr).all())


def recross_above(m: pd.Series, thr: float, min_below: int) -> bool:
    """最后一个值 ≥ thr, 且之前 min_below 个值都 < thr。"""
    s = m.dropna()
    if len(s) < min_below + 1:
        return False
    return bool(s.iloc[-1] >= thr and (s.iloc[-min_below - 1:-1] < thr).all())


def btc_trend(btc: pd.Series) -> dict:
    """BTC 日历日序列 (BTC 周末也交易) -> 20/50 日均线位置。样本不足 = None。"""
    s = btc.dropna()
    px = float(s.iloc[-1])
    sma20 = float(s.iloc[-20:].mean()) if len(s) >= 20 else None
    sma50 = float(s.iloc[-50:].mean()) if len(s) >= 50 else None
    return {"px": px, "sma20": sma20, "sma50": sma50,
            "above20": (px > sma20) if sma20 is not None else None,
            "above50": (px > sma50) if sma50 is not None else None}


def dilution_pct(df_td: pd.DataFrame, days: int = 60) -> float | None:
    """BTC/稀释股 近 days 个交易日变化 % (负 = ATM 稀释快过买币)。"""
    if len(df_td) < days + 1:
        return None
    bps = df_td.btc_balance / df_td.diluted_shares_outstanding
    return float((bps.iloc[-1] / bps.iloc[-days - 1] - 1) * 100)


def last_change_date(s: pd.Series):
    """序列最后一次变动的日期 (股本按月跳变, 报告印出来提醒读数可能偏低)。"""
    s = s.dropna()
    if len(s) < 2:
        return None
    ch = s[s.diff().fillna(0) != 0]
    return ch.index[-1].date() if len(ch) else None


# --------------------------------------------------------------------------
# Assess (one symbol)
# --------------------------------------------------------------------------

def assess(df: pd.DataFrame, cfg: dict, *, today: date, holidays=frozenset(),
           live_px: float | None = None, live_btc: float | None = None) -> dict:
    """一家公司 -> 今天的读数 + 旗标 + 阶梯 + 历史统计。

    今天的点: 网站最后一行 (持币/股本/BTC), 股价可被扫描器自己的收盘 (live_px)
    覆盖 —— 网站股价是延迟的, 标的在 watchlist 里时用我们自己的。旗标用的序列 =
    今天之前的交易日 + 今天这一个点。"""
    if df.empty:
        raise ValueError("historicalData 为空")
    last = df.iloc[-1]
    holdings = float(last.btc_balance)
    sb, sd = float(last.total_outstanding_shares), float(last.diluted_shares_outstanding)
    px = float(live_px) if live_px else float(last.stock_prices)
    btc = float(live_btc) if live_btc else float(last.btc_prices)
    claims_b = float(cfg["debt_b"] + cfg["pref_b"] - cfg["usd_b"])
    m_b, m_d = mnav(px, sb, holdings, btc), mnav(px, sd, holdings, btc)
    m_e = ev_mnav(px, sb, claims_b, holdings, btc)

    td = trading_rows(df, holidays)
    prior = td[td.index.date < today] if len(td) else td
    s = prior.nav_premium_diluted.copy()
    s.loc[pd.Timestamp(today)] = m_d
    s = s.sort_index()
    trap_lo, trap_hi = cfg["trap"]
    flags = {
        "trap": trap_lo <= m_d < trap_hi,
        "break_trap": first_break_below(s, trap_hi, cfg["break_lookback"]),
        "break_t1": first_break_below(s, cfg["t1_dil"], cfg["break_lookback"]),
        "recross_t1": recross_above(s, cfg["t1_dil"], cfg["recross_min_below"]),
        # 止盈提醒: 到了带的下沿就提醒, 带的上方也提醒 (跳空越过 0.95 不能漏), 但只在
        # 近 tp_memory_days 个交易日内到过 T1 线 (= 可能持有 mNAV 多头) 之后才出 —— 否则
        # 溢价年份天天提醒。卖出本身自己管理, 这里只负责说"到了"
        "take_profit": bool(m_d >= cfg["take_profit"][0]
                            and (s.iloc[-cfg["tp_memory_days"] - 1:-1] <= cfg["t1_dil"]).any()),
    }
    lad = ladder(holdings, sb, sd, btc, claims_b, cfg)
    tier = None
    if px <= lad["t3"][0]:
        tier = "T3"
    elif px <= lad["t2"][0]:
        tier = "T2"
    elif px <= lad["t1"][0]:
        tier = "T1"
    flags["tier"] = tier
    trend = btc_trend(df.btc_prices)
    claims_age = (today - date.fromisoformat(cfg["claims_asof"])).days
    warnings = []
    if claims_age > cfg["claims_stale_days"]:
        warnings.append(f"净债权数字已 {claims_age} 天没更新 (claims_asof "
                        f"{cfg['claims_asof']}) — 企业价值 mNAV 和 T1 的 EV 线按旧数算, "
                        "去 strategy.com 首页抄 Debt / Pref / USD Reserve / USD Cash")
    asof = last.name.date()
    if (today - asof).days > 4:
        warnings.append(f"StrategyTracker 最后一行是 {asof}, 落后 {(today - asof).days} 天")
    return {
        "asof": asof.isoformat(), "px": px, "px_src": "live" if live_px else "st",
        "btc": btc, "holdings": holdings, "shares_basic": sb, "shares_dil": sd,
        "m_basic": m_b, "m_dil": m_d, "m_ev": m_e, "claims_b": claims_b,
        "claims_pct": claims_b * 1e9 / (holdings * btc) * 100,
        "claims_age": claims_age, "bucket": bucket(m_d), "flags": flags,
        "ladder": lad, "btc_trend": trend,
        "take_profit_px": [price_at(x, holdings, sd, btc) for x in cfg["take_profit"]],
        "dilution_60d": dilution_pct(td, 60),
        "shares_changed": (lambda d_: d_.isoformat() if d_ else None)(
            last_change_date(df.total_outstanding_shares)),
        "stats": threshold_stats(s), "warnings": warnings,
    }


def run(cfgs: dict, *, cache_path: Path, today: date, holidays=frozenset(),
        live_px: dict | None = None, live_btc: float | None = None,
        get=_get) -> dict:
    """{SYMBOL: cfg} -> {"by_symbol": {SYMBOL: assess | {"error"}}, "version",
    "timestamp", "warnings", "from_cache"}。取数整体失败 -> 每只都是 error。"""
    live_px = live_px or {}
    try:
        data = fetch_companies([c["st_ticker"] for c in cfgs.values()], cache_path, get)
    except Exception as e:          # noqa: BLE001
        return {"by_symbol": {s_: {"error": str(e)} for s_ in cfgs}, "cfgs": cfgs,
                "version": None, "timestamp": None, "warnings": [], "from_cache": False}
    by = {}
    for sym, cfg in cfgs.items():
        try:
            comp = data["companies"][cfg["st_ticker"]]
            df = frame(comp["historicalData"])
            by[sym] = assess(df, cfg, today=today, holidays=holidays,
                             live_px=live_px.get(sym), live_btc=live_btc)
            by[sym]["st_version"] = data.get("version")
        except Exception as e:      # noqa: BLE001
            by[sym] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"by_symbol": by, "cfgs": cfgs, "version": data.get("version"),
            "timestamp": data.get("timestamp"), "warnings": data.get("warnings", []),
            "from_cache": data.get("from_cache", False)}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

def _k(v: float) -> str:
    return f"{v:,.0f}"


def _grid(vals: list[float]) -> str:
    return " / ".join(f"${v:,.0f}" for v in vals)


def symbol_lines(sym: str, a: dict, cfg: dict) -> list[str]:
    f = a["flags"]
    tr = a["btc_trend"]
    out = [f"- **{sym}** 收 {a['px']:.2f}{' (扫描器收盘)' if a['px_src'] == 'live' else ''} "
           f"/ BTC {_k(a['btc'])} / 持币 {_k(a['holdings'])} → 稀释 mNAV **{a['m_dil']:.3f}** · "
           f"基本 {a['m_basic']:.3f} · 企业价值 {a['m_ev']:.2f} "
           f"(债 {cfg['debt_b']:.2f} + 优先股 {cfg['pref_b']:.2f} − 美元 {cfg['usd_b']:.2f} "
           f"= 净债权 ${a['claims_b']:.1f}B, 占 BTC 市值 {a['claims_pct']:.0f}%, "
           f"债权数据 {cfg['claims_asof']})"]
    out.append(f"  - 桶 {a['bucket']}: {BUCKET_NOTE[a['bucket']]}")
    if f["break_trap"]:
        out.append(f"  - ⚠️ 今天首次收盘跌破 {cfg['trap'][1]:g} (之前 {cfg['break_lookback']} "
                   f"个交易日都在上方) — {BREAK09_NOTE}")
    if f["break_t1"]:
        out.append(f"  - 首次收盘跌破 {cfg['t1_dil']:g} — 2022-05-11 之后几乎直接反弹 "
                   "(最大回撤 −10%), 2026-06-22 之后再跌 25% 到 0.63; 分档, 别一次买完")
    if f["recross_t1"]:
        out.append(f"  - 🟢 右侧: 从下方重新站上 {cfg['t1_dil']:g}"
                   + (" 且 BTC 在 20 日均线上" if tr["above20"] else
                      " 但 BTC 在 20 日均线下 (样本里那次 2026-08-25 是线上: 20 天 +28%)"))
    if f["tier"]:
        out.append(f"  - 🟢 现价已在 {f['tier']} 档内")
    if f["take_profit"]:
        tp_lo, tp_hi = cfg["take_profit"]
        where = (f"在止盈带 {tp_lo:g}–{tp_hi:g} 内" if a["m_dil"] < tp_hi
                 else f"已高于止盈带上沿 {tp_hi:g}")
        out.append(f"  - 🔔 {where} (近 {cfg['tp_memory_days']} 个交易日到过 {cfg['t1_dil']:g} 线): "
                   "持有 mNAV 多头的话该了结了 — 公司在 0.65–0.80 就 ATM 卖股, "
                   "2026 修复只回到 0.95–0.98, 不等 1.0")
    lad = a["ladder"]
    out.append(f"  - 阶梯 (按当前 BTC / −10% / −20% 换算): "
               f"T1 {_grid(lad['t1'])} [min(EV≤{cfg['t1_ev']:g} {_grid(lad['t1_ev_px'])}, "
               f"稀释≤{cfg['t1_dil']:g} {_grid(lad['t1_dil_px'])}), 现由{lad['t1_bind'][0]}线定] · "
               f"T2 稀释≤{cfg['t2_dil']:g} {_grid(lad['t2'])} · "
               f"T3 稀释≤{cfg['t3_dil']:g} {_grid(lad['t3'])} · "
               f"止盈 {cfg['take_profit'][0]:g}–{cfg['take_profit'][1]:g} = "
               f"${a['take_profit_px'][0]:,.0f}–{a['take_profit_px'][1]:,.0f}")
    if tr["sma20"] is not None:
        pos20 = "上" if tr["above20"] else "下"
        pos50 = ("" if tr["sma50"] is None else
                 f", 50 日线 {_k(tr['sma50'])} {'上' if tr['above50'] else '下'}")
        out.append(f"  - BTC {_k(tr['px'])} 在 20 日线 {_k(tr['sma20'])} {pos20}方{pos50} — "
                   f"{BTC_FILTER_NOTE}; 线下只下半仓" if not tr["above20"] else
                   f"  - BTC {_k(tr['px'])} 在 20 日线 {_k(tr['sma20'])} 上方{pos50} — "
                   f"{BTC_FILTER_NOTE}")
    tail = []
    if a["dilution_60d"] is not None:
        tail.append(f"BTC/稀释股 60 个交易日 {a['dilution_60d']:+.1f}%"
                    + (" (ATM 稀释快过买币)" if a["dilution_60d"] < -3 else ""))
    if a["shares_changed"]:
        tail.append(f"股本上次变动 {a['shares_changed']} (网站按月跳, ATM 密集月读数偏低)")
    st = a["stats"]
    tail.append("历史 (稀释, 交易日): " + " · ".join(
        f"<{thr:g} {st[thr]['days']} 天/{st[thr]['episodes']} 段" for thr in (0.8, 0.7, 0.6)))
    out.append("  - " + " · ".join(tail))
    for w in a["warnings"]:
        out.append(f"  - ⚠️ {w}")
    if cfg.get("notes"):
        out.append(f"  - 备注: {cfg['notes']}")
    return out


def report_block(meta: dict | None) -> list[str]:
    """报告段。meta=None (--no-mnav / 未配置) 整段不出。cfgs 随 meta 走 (run 放进去)。"""
    cfgs = (meta or {}).get("cfgs")
    if meta is None or not cfgs:
        return []
    lines = ["## BTC 财库 mNAV (StrategyTracker)", ""]
    for sym, cfg in cfgs.items():
        a = (meta.get("by_symbol") or {}).get(sym)
        if a is None:
            continue
        if a.get("error"):
            lines.append(f"- ⚠️ **{sym}** mNAV 取不到: {a['error']} — 只缺这一段, 下次扫描重试")
            continue
        lines += symbol_lines(sym, a, cfg)
    for w in meta.get("warnings") or []:
        lines.append(f"- ⚠️ {w}")
    src = meta.get("timestamp")
    lines += ["",
              "> 稀释 mNAV = 收盘 × 稀释股本 ÷ (持币 × BTC), 与 saylortracker / strategytracker "
              "的 Diluted 线同口径; 企业价值 mNAV = (基本市值 + 可转债面值 + 优先股面值 − 美元储备) "
              "÷ BTC 市值, 分子即 strategy.com 首页的 Enterprise Value (官网 2026-07-23 起显示的 "
              "mNAV 改为股价 ÷ 每股净 BTC, 只在 1.0 附近与此重合), 2026 的三个底都在 0.96–0.99 "
              "(全样本没低于 0.96)。"
              "T1 取企业价值线与稀释线中更低的价; 三档都按当时 BTC 实时换算, 别记固定价。"
              "回测只有 2022 / 2026 两个 regime, 胜率按两次经验读 (research/mnav_backtest.py)。"
              "只做提示, 不进状态机/门控/票据"
              + (f"; 数据 StrategyTracker {str(src)[:16]}Z" if src else "")
              + (" (缓存)" if meta.get("from_cache") else "") + "。",
              ""]
    return lines


# --------------------------------------------------------------------------
# History (每天一行, 只有定时尾盘扫描写)
# --------------------------------------------------------------------------

def history_rows(meta: dict, day: str) -> list[dict]:
    rows = []
    cfgs = meta.get("cfgs") or {}
    for sym, a in (meta.get("by_symbol") or {}).items():
        if not a or a.get("error"):
            continue
        cfg = cfgs[sym]
        f = a["flags"]
        flags = ",".join(k for k, v in f.items() if v is True) + (f",{f['tier']}" if f["tier"] else "")
        rows.append({"date": day, "symbol": sym, "px": round(a["px"], 4),
                     "btc": round(a["btc"], 2), "holdings": a["holdings"],
                     "shares_basic": a["shares_basic"], "shares_dil": a["shares_dil"],
                     "m_basic": round(a["m_basic"], 5), "m_dil": round(a["m_dil"], 5),
                     "m_ev": round(a["m_ev"], 5), "debt_b": cfg["debt_b"],
                     "pref_b": cfg["pref_b"], "usd_b": cfg["usd_b"],
                     "bucket": a["bucket"], "flags": flags.strip(","),
                     "px_src": a["px_src"], "st_version": a.get("st_version")})
    return rows


def append_history(rows: list[dict], path: Path) -> int:
    """追加 -> 新写入行数; (date, symbol) 已有的跳过 (同 iv_history)。"""
    have = set()
    header_ok = False
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            header_ok = rd.fieldnames == list(HISTORY_FIELDS)
            if rd.fieldnames and "date" in rd.fieldnames and "symbol" in rd.fieldnames:
                have = {(r["date"], r["symbol"]) for r in rd}
    new = [r for r in rows if (r["date"], r["symbol"]) not in have]
    if not new:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    write_header = not path.exists() or not header_ok
    if path.exists() and not header_ok:
        # 表头变了 (以后加列): 按新表头重写, 旧行缺的列留空
        with path.open(newline="", encoding="utf-8") as f:
            old = list(csv.DictReader(f))
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, HISTORY_FIELDS, lineterminator="\n", extrasaction="ignore")
            w.writeheader()
            w.writerows(old)
            w.writerows(new)
        os.replace(tmp, path)
        return len(new)
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, HISTORY_FIELDS, lineterminator="\n")
        if write_header:
            w.writeheader()
        w.writerows(new)
    return len(new)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    import tomllib
    from zoneinfo import ZoneInfo
    base = Path(__file__).resolve().parent
    raw = tomllib.loads((base / "watchlist.toml").read_text(encoding="utf-8"))
    today = datetime.now(ZoneInfo("America/New_York")).date()
    cfgs = validate_config(raw.get("mnav"), today)
    if argv:
        keep = {a.upper() for a in argv}
        cfgs = {k: v for k, v in cfgs.items() if k in keep}
    if not cfgs:
        print("watchlist.toml 没有 [mnav.*] 配置 (或过滤后为空)")
        return 1
    meta = run(cfgs, cache_path=base / "data" / CACHE_NAME, today=today)
    print("\n".join(report_block(meta)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
