#!/usr/bin/env python3
"""BTC 平台股 / BTC 比值 (COIN 为默认) — 取数 / 比值 / 入场阶梯 / 报告段.

只做提示, 不进状态机/门控/票据 (同 insider / revisions / mnav)。标的默认只有 COIN
(watchlist.toml 的 [btcratio.COIN]); 不配置这张表整段不出。**只考虑正股和 2x ETF**:
不出 CSP / LEAP 票 (COIN 在 [tickers] 里 options = false), 2x 只在右侧趋势中段作为
工具出现 (走 [tickers.COIN] 的 two_x 提示), 比值阶梯里不用 2x。

比值 (2026-11-01 研究, 2021-04-14 上市 → 2026-10-30 共 1,395 个交易日):
  - 价格比值 = 收盘 ÷ BTC × 1000 (‰, 即 COIN 价格占 BTC 价格的千分之几)。10/30 收
    138.99 / BTC 76,600 = 1.814, 全历史 0.5 分位; 历史最低 1.672 (2023-05-03, COIN 48.5
    / BTC 29.0k); 分年中位 2021 5.5 / 2022 3.3 / 2023 2.6 / 2024 3.3 / 2025 2.8 / 2026 2.5。
  - 市值比值 = COIN 市值 ÷ BTC 市值 (%)。上市以来股本 +26%、BTC 供给只 +7%, 价格比值
    低估了 2023 的便宜程度: 市值比值 10/30 是 2.40%, 历史最低 2.04% (同一天), 还差 15%。
    股本历史用 10-Q 封面页锚点线性插值 (SHARES_ANCHORS, 近似 ±3%), 今天的股本抄配置
    shares_m; 不配 shares_m 市值比值那几句不出。

回测结论 (research/btcratio_backtest.py 可复现; 样本本质只有 2022-05 / 2022-12→2023-06 /
2025-04→05 三组 + 2026-10 进行中, 胜率按"三次经验"读):
  - 低于阈值的天数 (交易日, 到 10/30; 段 = 中间断开 ≤5 个交易日算同一段, 与报告同口径):
    <2.2 118 天/12 段 · <2.0 29 天/9 段 · <1.8 4 天/1 段 (2023-05-01→05-04) · <1.7 1 天 ·
    <1.5 / <1.2 从未出现。严格连续口径是 <2.2 117/28、<2.0 28/12。
  - 按入场比值分桶 (只数有前瞻数据的日子): ≤2.0 两桶 60 天胜率 100%/中位 +85 ~ +88%,
    120 天 100%; 2.0–2.2 桶 60 天 99%/+63%; 2.2–2.4 桶 60 天 81%/+23%; 2.4–2.6 桶 60 天
    64%/+12% (一年中位附近, 优势消失); 3.5–4.5 桶 60 天 14%/−27% 是最坏的桶; ≥4.5 (2021
    上市初) 250 天 0%/−73%。
  - 低于 2.0 的 10 次首次触及 (严格口径, 合并后 8 段): 120 天全部为正 (最差 +18%), 60 天中位
    +85%, 入场后 120 天内最大回撤 (收盘) 最坏 −12% (2022-05-11, 之后 BTC 再跌 29%; 盘中低点
    口径 −24%)、次坏 −11% (2023-04-24); 2025 年三段 250 天只有 +1 ~ +21% (COIN 7 月见顶后
    跌回) — 比值说的是"相对 BTC 便宜", 不是绝对顶部。
  - 相对收益 (COIN − BTC) 同样单调: ≤2.0 桶 60 天相对胜率 100%、中位 +57 ~ +82%;
    >3.5 桶 120 天相对胜率 2–5%。比值的均值回归主要靠 COIN 涨得比 BTC 快。
  - BTC 趋势过滤在这里**不**起作用 (与 MSTR 不同): 比值 <2.2 的日子 BTC 在 50 日线上
    60 天胜率 100%, 线下 98%。2022-05 入场 BTC 之后 120 天 −29%, COIN 仍 +18%。
  - 2026-10 的背离: 6/30 以来 BTC +31%、比值从 2.49 跌到 1.81 (−27%)。比值创 250 日新低
    且 BTC 高于 120 日前的日子共 18 天, 先例两组: 2023-04/05 (SEC 起诉前夜, 之后 120 天
    +34%, 收盘最大回撤 −11%) 和 2025-03/04 (之后 +95%, 有 Deribit 并购 / 入标普 500 /
    GENIUS 法案催化)。两次都是 COIN 自己的坏消息压出来的, 都修复了; 这次的坏消息是现货
    份额流失 + Q4 费用上台阶, 是慢变量不是事件。

规则 (阈值可在配置里改):
  关注 = 比值 ≤ watch (2.2); T1 = 比值 ≤ t1 (2.0, 第一批 1/3, 接受 −25% 回撤);
  T2 = 比值 ≤ t2 (1.67 = 历史最低); T3 = 比值 ≤ t3 (1.5, 从未出现 ≈ 市值比值地板);
  另给市值比值 ≤ mcap_floor_pct (2.01%) 的价; 全部按当时 BTC 实时换算成价格, 同时给
  BTC −10% / −20% 的价位。止盈带 take_profit (2.4–2.6 = 一年中位附近, 60 天胜率跌到
  六成), 只在近 tp_memory_days 个交易日内到过 T1 线之后才提醒。
  右侧 (日收 > 前低平台 / 20 日线) 不在这里出 —— COIN 在 [tickers] 里, 走扫描器自己的
  三选二; 这里只管"相对 BTC 便不便宜"。

数据: yfinance 日线 (标的 + BTC-USD, 上市起全量, 约 1,400 行, 几秒), 缓存
data/btcratio_<SYM>.csv; 缓存已含今天就不重下; 取数失败退回缓存并出 ⚠️; 无缓存整段
降级成一行。BTC-USD 是 UTC 日, 今天那行是实时的; 标的在 [tickers] 里时今天的收盘用
扫描器自己的 (陈旧日线不用)。

命令行 (手工看一眼, 不写历史):
    .venv/bin/python btcratio.py [COIN ...]
"""
from __future__ import annotations

import csv
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

CACHE_NAME = "btcratio_{}.csv"
HISTORY_NAME = "btcratio_history.csv"
BTC_TICKER = "BTC-USD"
STATS_THRESHOLDS = (2.2, 2.0, 1.8, 1.5, 1.2)
EPISODE_GAP = 5                 # 中间断开 ≤ 这么多交易日算同一段
STALE_DAYS = 4                  # 缓存最后一行落后超过这么多天报告提醒

# 股本锚点 (百万股, A+B 类, 10-Q 封面页; 锚点间线性插值, 近似 ±3%) — 只用于市值比值的
# 历史分位。今天的股本抄配置 shares_m。
SHARES_ANCHORS = (
    ("2021-04-14", 210.0), ("2021-07-01", 212.0), ("2022-01-01", 220.0), ("2022-07-01", 226.0),
    ("2023-01-01", 232.0), ("2023-07-01", 238.0), ("2024-01-01", 244.0), ("2024-07-01", 250.0),
    ("2025-01-01", 253.0), ("2025-07-01", 256.0), ("2025-10-01", 268.0), ("2026-01-01", 266.0),
    ("2026-09-30", 264.8))
# BTC 流通量锚点 (百万枚)
BTC_SUPPLY_ANCHORS = (
    ("2021-04-14", 18.69), ("2022-01-01", 18.92), ("2023-01-01", 19.25), ("2024-01-01", 19.58),
    ("2024-04-20", 19.69), ("2025-01-01", 19.80), ("2026-01-01", 19.95), ("2026-10-30", 20.00))

# 回测文案 (2026-11-01 快照, 价格比值; 数字与 research/btcratio_backtest.py 同源)
BUCKET_EDGES = ((1.9, "<1.9"), (2.0, "1.9-2.0"), (2.2, "2.0-2.2"), (2.4, "2.2-2.4"),
                (2.6, "2.4-2.6"), (3.0, "2.6-3.0"), (3.5, "3.0-3.5"), (4.5, "3.5-4.5"),
                (math.inf, "≥4.5"))
BUCKET_NOTE = {
    "<1.9": "60 天胜率 100%/中位 +85%, 120 天 100%/+49% (n=9, 只有 2023-05 和 2025-04 两组) — 历史最低区, 分档买",
    "1.9-2.0": "60 天胜率 100%/中位 +88%, 120 天 100%/+104% (n=15) — 入场区",
    "2.0-2.2": "60 天胜率 99%/中位 +63%, 120 天 93%/+73% (n=73) — 关注区, 60 天看多",
    "2.2-2.4": "60 天胜率 81%/中位 +23%, 120 天 70%/+16% — 中性偏多",
    "2.4-2.6": "60 天胜率 64%/中位 +12%, 120 天 62%/+12% — 一年中位附近, 优势消失, 不加仓",
    "2.6-3.0": "60 天胜率 46%/中位 −5%, 120 天 52%/+6% — 无优势",
    "3.0-3.5": "60 天胜率 39%/中位 −8%, 120 天相对 BTC 胜率 27% — 相对 BTC 贵",
    "3.5-4.5": "60 天胜率 14%/中位 −27%, 120 天 34%/−19% — 历史最坏的桶, 不追",
    "≥4.5": "2021 上市初期, 250 天胜率 0%/中位 −73% — 泡沫区",
}
EPISODE_NOTE = ("历史 <2.0 的 10 次首次触及 120 天全部为正 (最差 +18%), 60 天中位 +85%, "
                "入场后 120 天内最大回撤 (收盘) 最坏 −12% (2022-05, 之后 BTC 再跌 29%; 盘中低点 −24%); "
                "2025 三段 250 天只有 +1 ~ +21% — 分档, 别一次买完")
DIVERGENCE_NOTE = ("先例两组: 2023-04/05 (SEC 起诉前夜, 之后 120 天 +34%, 收盘最大回撤 −11%) 和 "
                   "2025-03/04 (之后 +95%, 有 Deribit 并购 / 入标普 500 / GENIUS 法案催化) — "
                   "都是 COIN 自己的坏消息压出来的、都修复了; 这次的坏消息是现货份额流失 + Q4 费用上台阶, "
                   "是慢变量不是事件")
BTC_FILTER_NOTE = ("比值 <2.2 的日子: BTC 在 50 日线上 60 天胜率 100%, 线下 98% — "
                   "历史上不影响 (与 MSTR 不同), 但样本只有 3 组")
TWO_X_NOTE = ("只在右侧趋势中段用 (走 [tickers] 的 2x 提示), 比值阶梯 (左侧) 不用: "
              "日内重置损耗 ≈ 日波动率², 底部区域日波动 5–8%")

CFG_DEFAULTS = {
    "yf_ticker": None,          # Yahoo 代码, 默认 = 表名
    "shares_m": None,           # 今天的股本 (百万股, A+B 类, 10-Q 封面页); 不填不出市值比值
    "shares_asof": None,        # 股本日期 (填了 shares_m 就必须填)
    "shares_stale_days": 120,   # 一个季度多一点: 下一份 10-Q 出了该换
    "watch": 2.2, "t1": 2.0, "t2": 1.67, "t3": 1.5,
    "mcap_floor_pct": 2.04,     # 市值比值历史最低 (2023-05-03, 锚点插值口径) %
    "take_profit": [2.4, 2.6],  # 到下沿就提醒 (含上方)
    "break_lookback": 20,       # "首次跌破": 之前这么多交易日都在阈值上方
    "tp_memory_days": 120,      # 止盈提醒只在近这么多交易日内到过 t1 线之后才出
    "div_lookback": 250,        # 背离: 比值创这么多交易日新低
    "div_btc_days": 120,        # 背离: 且 BTC 高于这么多交易日前
    "two_x": None,              # 2x ETF 代码 (CONL), 只用于文案
    "notes": "",
}
HISTORY_FIELDS = ("date", "symbol", "px", "btc", "ratio", "pct", "mcap_pct", "shares_m",
                  "bucket", "flags", "streak_t1", "px_src", "data_asof")


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
    """[btcratio] 表 -> {SYMBOL: cfg}。同 load_config / mnav 的脾气: 未知键 / 类型错 /
    顺序错在装载时就炸。表缺省 = {} (整段不出)。"""
    out = {}
    for sym, tcfg in (raw or {}).items():
        if not isinstance(tcfg, dict):
            raise ValueError(f"[btcratio.{sym}] 必须是一张表 (shares_m/t1/...)")
        unknown = set(tcfg) - set(CFG_DEFAULTS)
        if unknown:
            raise ValueError(f"[btcratio.{sym}] 未知键 (typo?): {', '.join(sorted(unknown))}")
        cfg = {**CFG_DEFAULTS, **tcfg}
        if cfg["shares_m"] is not None:
            if not _finite(cfg["shares_m"]) or cfg["shares_m"] <= 0:
                raise ValueError(f"[btcratio.{sym}] shares_m 必须是正数 (百万股), 得到 {cfg['shares_m']!r}")
            if cfg["shares_asof"] is None:
                raise ValueError(f"[btcratio.{sym}] 填了 shares_m 就要填 shares_asof — 股本要带日期才能提醒过期")
            asof = _iso(cfg["shares_asof"], f"[btcratio.{sym}] shares_asof")
            if asof > today:
                raise ValueError(f"[btcratio.{sym}] shares_asof {asof} 在未来")
            cfg["shares_asof"] = asof.isoformat()
        elif cfg["shares_asof"] is not None:
            raise ValueError(f"[btcratio.{sym}] 没填 shares_m 就不要填 shares_asof")
        for k in ("watch", "t1", "t2", "t3", "mcap_floor_pct"):
            if not _finite(cfg[k]) or cfg[k] <= 0:
                raise ValueError(f"[btcratio.{sym}] {k} 必须是正数, 得到 {cfg[k]!r}")
        if not (cfg["t3"] < cfg["t2"] < cfg["t1"] <= cfg["watch"]):
            raise ValueError(f"[btcratio.{sym}] 阶梯顺序错: 要 t3 < t2 < t1 ≤ watch")
        v = cfg["take_profit"]
        if not (isinstance(v, (list, tuple)) and len(v) == 2
                and all(_finite(x) and x > 0 for x in v) and v[0] < v[1]):
            raise ValueError(f"[btcratio.{sym}] take_profit 必须是 [低, 高] 两个正数, 得到 {v!r}")
        cfg["take_profit"] = [float(v[0]), float(v[1])]
        if cfg["take_profit"][0] <= cfg["t1"]:
            raise ValueError(f"[btcratio.{sym}] take_profit 下沿要高于 t1")
        for k in ("shares_stale_days", "break_lookback", "tp_memory_days",
                  "div_lookback", "div_btc_days"):
            if not (isinstance(cfg[k], int) and not isinstance(cfg[k], bool)
                    and cfg[k] > 0):
                raise ValueError(f"[btcratio.{sym}] {k} 必须是正整数, 得到 {cfg[k]!r}")
        for k in ("yf_ticker", "two_x"):
            if cfg[k] is not None and not isinstance(cfg[k], str):
                raise ValueError(f"[btcratio.{sym}] {k} 必须是字符串")
        if not isinstance(cfg["notes"], str):
            raise ValueError(f"[btcratio.{sym}] notes 必须是字符串")
        key = sym.upper()
        if key in out:
            raise ValueError(f"[btcratio] 重复条目 (含大小写差异): {key}")
        cfg["yf_ticker"] = (cfg["yf_ticker"] or key).upper()
        cfg["two_x"] = cfg["two_x"].upper() if cfg["two_x"] else None
        out[key] = cfg
    return out


# --------------------------------------------------------------------------
# Fetch + cache
# --------------------------------------------------------------------------

def _download(tickers: list[str]) -> pd.DataFrame:
    """yfinance 日线 (全量) -> 列 = 代码, 值 = 收盘。惰性 import: 测试和缓存命中都不碰网络。"""
    import yfinance as yf
    df = yf.download(tickers, period="max", interval="1d", auto_adjust=True,
                     progress=False, threads=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance 返回空")
    close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]]
    if not isinstance(df.columns, pd.MultiIndex):
        close = close.rename(columns={"Close": tickers[0]})
    close.index = pd.to_datetime(close.index)
    if getattr(close.index, "tz", None) is not None:
        close.index = close.index.tz_localize(None)
    return close


def _read_cache(path: Path) -> pd.DataFrame | None:
    try:
        df = pd.read_csv(path, parse_dates=["date"], index_col="date")
    except (OSError, ValueError, KeyError):
        return None
    if df.empty or not {"close", "btc"} <= set(df.columns):
        return None
    return df[["close", "btc"]].astype(float).sort_index()


def fetch_pair(yf_ticker: str, cache_path: Path, today: date, download=_download) -> dict:
    """-> {"df": DataFrame(index=交易日, close, btc), "warnings": [..], "from_cache": bool,
    "fetched": iso}。缓存已含今天 (或今天不是交易日而缓存到前一交易日) 就不重下; 取数
    失败退回缓存 (带 ⚠️); 没缓存就抛。"""
    cached = _read_cache(cache_path)
    if cached is not None and cached.index[-1].date() >= today:
        return {"df": cached, "warnings": [], "from_cache": True, "fetched": None}
    try:
        close = download([yf_ticker, BTC_TICKER])
        if yf_ticker not in close.columns or BTC_TICKER not in close.columns:
            raise KeyError(f"yfinance 没有 {yf_ticker} 或 {BTC_TICKER}")
        px = close[yf_ticker].dropna()
        btc = close[BTC_TICKER].dropna()
        if px.empty or btc.empty:
            raise RuntimeError(f"{yf_ticker} 或 {BTC_TICKER} 序列为空")
        # 对齐到标的的交易日; BTC 周末也交易, 标的收盘那天取同一天的 BTC (UTC 日收盘,
        # 今天那行是实时价), 缺的向前填
        btc_al = btc.reindex(px.index.union(btc.index)).ffill().reindex(px.index)
        df = pd.DataFrame({"close": px.astype(float), "btc": btc_al.astype(float)}).dropna()
        df = df[(df.close > 0) & (df.btc > 0)]
        df.index.name = "date"
        if df.empty:
            raise RuntimeError("对齐后为空")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        df.to_csv(tmp, float_format="%.6f", lineterminator="\n")
        os.replace(tmp, cache_path)
        return {"df": df, "warnings": [], "from_cache": False,
                "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    except Exception as e:          # noqa: BLE001
        why = f"{type(e).__name__}: {str(e).splitlines()[0][:80]}" if str(e) else type(e).__name__
        if cached is not None:
            return {"df": cached, "from_cache": True, "fetched": None,
                    "warnings": [f"yfinance 取数失败 ({why}), 用的是到 "
                                 f"{cached.index[-1].date()} 的缓存"]}
        raise RuntimeError(f"yfinance 取数失败且无缓存 ({why})") from e


# --------------------------------------------------------------------------
# Pure math
# --------------------------------------------------------------------------

def bucket(r: float) -> str:
    for edge, label in BUCKET_EDGES:
        if r < edge:
            return label
    return BUCKET_EDGES[-1][1]


def ratio(price: float, btc: float) -> float:
    """价格比值 ‰ = 收盘 ÷ BTC × 1000。"""
    return price / btc * 1000.0


def price_at(r: float, btc: float) -> float:
    """比值 = r 时的股价。"""
    return r * btc / 1000.0


def _interp_anchors(anchors, index: pd.DatetimeIndex) -> pd.Series:
    s = pd.Series({pd.Timestamp(d): v for d, v in anchors}).sort_index()
    full = s.reindex(s.index.union(index)).interpolate(method="time").ffill().bfill()
    return full.reindex(index)


def mcap_ratio_series(df: pd.DataFrame) -> pd.Series:
    """历史市值比值 % = 收盘 × 股本锚点 ÷ (BTC × 流通量锚点) × 100。"""
    sh = _interp_anchors(SHARES_ANCHORS, df.index)
    sup = _interp_anchors(BTC_SUPPLY_ANCHORS, df.index)
    return (df.close * sh / (df.btc * sup) * 100.0).rename("mcap_pct")


def btc_supply_now() -> float:
    return float(BTC_SUPPLY_ANCHORS[-1][1])


def mcap_pct(price: float, shares_m: float, btc: float, supply_m: float) -> float:
    return price * shares_m / (btc * supply_m) * 100.0


def price_at_mcap(pct: float, shares_m: float, btc: float, supply_m: float) -> float:
    return pct / 100.0 * btc * supply_m / shares_m


def ladder(btc: float, cfg: dict, shares_m: float | None = None) -> dict:
    """阶梯价格, 按当前 BTC 和 BTC −10% / −20% 各给一组。"""
    out = {"btc_grid": [1.0, 0.9, 0.8], "watch": [], "t1": [], "t2": [], "t3": [],
           "mcap_floor": [] if shares_m else None}
    for f in out["btc_grid"]:
        b = btc * f
        for k in ("watch", "t1", "t2", "t3"):
            out[k].append(price_at(cfg[k], b))
        if shares_m:
            out["mcap_floor"].append(price_at_mcap(cfg["mcap_floor_pct"], shares_m, b,
                                                   btc_supply_now()))
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


def threshold_stats(r: pd.Series, thresholds=STATS_THRESHOLDS,
                    gap: int = EPISODE_GAP) -> dict:
    """{阈值: {"days", "episodes", "runs"}} —— 交易日口径。"""
    s = r.dropna()
    out = {}
    for thr in thresholds:
        runs = episodes(s < thr, gap)
        out[thr] = {"days": int((s < thr).sum()), "episodes": len(runs), "runs": runs}
    return out


def first_break_below(r: pd.Series, thr: float, lookback: int) -> bool:
    """最后一个值 < thr, 且之前 lookback 个值都 ≥ thr (样本不足 lookback 不算)。"""
    s = r.dropna()
    if len(s) < lookback + 1:
        return False
    return bool(s.iloc[-1] < thr and (s.iloc[-lookback - 1:-1] >= thr).all())


def streak_at_or_below(r: pd.Series, thr: float) -> int:
    """末尾连续 ≤ thr 的天数。"""
    s = r.dropna().to_numpy()
    n = 0
    for v in s[::-1]:
        if v <= thr:
            n += 1
        else:
            break
    return n


def percentile(prior: pd.Series, r: float) -> float:
    """r 在历史里的分位 % (严格低于 r 的天数占比)。"""
    s = prior.dropna()
    if s.empty:
        return float("nan")
    return float((s < r).mean() * 100.0)


def btc_trend(btc: pd.Series) -> dict:
    """BTC 序列 (这里是标的交易日上的 BTC) -> 20/50 日均线位置。样本不足 = None。"""
    s = btc.dropna()
    px = float(s.iloc[-1])
    sma20 = float(s.iloc[-20:].mean()) if len(s) >= 20 else None
    sma50 = float(s.iloc[-50:].mean()) if len(s) >= 50 else None
    return {"px": px, "sma20": sma20, "sma50": sma50,
            "above20": (px > sma20) if sma20 is not None else None,
            "above50": (px > sma50) if sma50 is not None else None}


def divergence(r: pd.Series, btc: pd.Series, lookback: int, btc_days: int) -> dict:
    """比值创 lookback 日新低 (含今天) 且 BTC 高于 btc_days 个交易日前 → 背离。"""
    rs, bs = r.dropna(), btc.dropna()
    if len(rs) < lookback or len(bs) < btc_days + 1:
        return {"ratio_low": False, "btc_up": None, "btc_chg": None, "flag": False}
    ratio_low = bool(rs.iloc[-1] <= rs.iloc[-lookback:].min())
    chg = float(bs.iloc[-1] / bs.iloc[-btc_days - 1] - 1) * 100
    return {"ratio_low": ratio_low, "btc_up": chg > 0, "btc_chg": chg,
            "flag": ratio_low and chg > 0}


def realized_vol(close: pd.Series, days: int = 60) -> float | None:
    """近 days 个交易日的年化实际波动 (对数收益)。"""
    s = close.dropna()
    if len(s) < days + 1:
        return None
    lr = np.log(s.iloc[-days - 1:]).diff().dropna()
    return float(lr.std(ddof=1) * math.sqrt(252))


def two_x_drag_pct(rv: float | None, lev: float = 2.0) -> float | None:
    """日内重置杠杆 ETF 相对 lev × 正股的年化损耗近似 ≈ (lev² − lev)/2 × σ² (对数口径)。
    lev=2 时就是 σ²: RV 75% → 约 −56%/年。"""
    if rv is None:
        return None
    return float((lev * lev - lev) / 2.0 * rv * rv * 100.0)


def rel_perf(close: pd.Series, btc: pd.Series, days: int) -> dict | None:
    """近 days 个交易日: 标的 / BTC 各自涨跌 % 与差。"""
    c, b = close.dropna(), btc.dropna()
    if len(c) < days + 1 or len(b) < days + 1:
        return None
    rc = float(c.iloc[-1] / c.iloc[-days - 1] - 1) * 100
    rb = float(b.iloc[-1] / b.iloc[-days - 1] - 1) * 100
    return {"days": days, "stock": rc, "btc": rb, "diff": rc - rb}


# --------------------------------------------------------------------------
# Assess (one symbol)
# --------------------------------------------------------------------------

def assess(df: pd.DataFrame, cfg: dict, *, today: date,
           live_px: float | None = None, live_btc: float | None = None) -> dict:
    """一只标的 -> 今天的读数 + 旗标 + 阶梯 + 历史统计。

    今天的点: 缓存最后一行, 股价可被扫描器自己的收盘 (live_px) 覆盖, BTC 可被 live_btc
    覆盖。旗标 / 分位用的序列 = 今天之前的交易日 + 今天这一个点。"""
    if df is None or df.empty:
        raise ValueError("日线为空")
    last = df.iloc[-1]
    px = float(live_px) if live_px else float(last.close)
    btc = float(live_btc) if live_btc else float(last.btc)
    r = ratio(px, btc)

    prior = df[df.index.date < today]
    rp = ratio(prior.close, prior.btc) if len(prior) else pd.Series(dtype=float)
    s = rp.copy()
    s.loc[pd.Timestamp(today)] = r
    s = s.sort_index()
    btc_s = prior.btc.copy()
    btc_s.loc[pd.Timestamp(today)] = btc
    btc_s = btc_s.sort_index()
    close_s = prior.close.copy()
    close_s.loc[pd.Timestamp(today)] = px
    close_s = close_s.sort_index()

    shares_m = cfg["shares_m"]
    mc_now = mc_pct = mc_min = mc_min_date = None
    if shares_m:
        mc_now = mcap_pct(px, shares_m, btc, btc_supply_now())
        if len(prior):
            mcs = mcap_ratio_series(prior)
            mc_pct = percentile(mcs, mc_now)
            mc_min, mc_min_date = float(mcs.min()), mcs.idxmin().date().isoformat()

    lad = ladder(btc, cfg, shares_m)
    tier = None
    if r <= cfg["t3"]:
        tier = "T3"
    elif r <= cfg["t2"]:
        tier = "T2"
    elif r <= cfg["t1"]:
        tier = "T1"
    all_time_low = bool(len(rp) and r <= float(rp.min()))
    div = divergence(s, btc_s, cfg["div_lookback"], cfg["div_btc_days"])
    flags = {
        "watch": bool(r <= cfg["watch"]),
        "tier": tier,
        "break_t1": first_break_below(s, cfg["t1"], cfg["break_lookback"]),
        "all_time_low": all_time_low,
        "divergence": bool(div["flag"]),
        # 止盈提醒: 到了带的下沿就提醒 (含上方), 只在近 tp_memory_days 个交易日内到过 T1
        # 线 (= 可能持有比值多头) 之后才出 —— 否则溢价年份天天提醒
        "take_profit": bool(r >= cfg["take_profit"][0]
                            and (s.iloc[-cfg["tp_memory_days"] - 1:-1] <= cfg["t1"]).any()),
    }
    rv60 = realized_vol(close_s, 60)
    warnings = []
    asof = last.name.date()
    if (today - asof).days > STALE_DAYS:
        warnings.append(f"日线最后一行是 {asof}, 落后 {(today - asof).days} 天")
    if shares_m:
        age = (today - date.fromisoformat(cfg["shares_asof"])).days
        if age > cfg["shares_stale_days"]:
            warnings.append(f"股本 shares_m 已 {age} 天没更新 (shares_asof {cfg['shares_asof']}) "
                            "— 市值比值按旧股本算, 去最新 10-Q 封面页抄 A+B 类股数")
    return {
        "asof": asof.isoformat(), "px": px, "px_src": "live" if live_px else "yf",
        "btc": btc, "btc_src": "live" if live_btc else "yf",
        "ratio": r, "pct": percentile(rp, r), "n_days": int(len(rp)) + 1,
        "first_date": (prior.index[0].date().isoformat() if len(prior) else today.isoformat()),
        "hist_min": (float(rp.min()) if len(rp) else r),
        "hist_min_date": (rp.idxmin().date().isoformat() if len(rp) else today.isoformat()),
        "mcap_pct": mc_now, "mcap_pctile": mc_pct, "mcap_min": mc_min, "mcap_min_date": mc_min_date,
        "bucket": bucket(r), "flags": flags, "ladder": lad,
        "take_profit_px": [price_at(x, btc) for x in cfg["take_profit"]],
        "streak_t1": streak_at_or_below(s, cfg["t1"]),
        "btc_trend": btc_trend(btc_s), "divergence": div,
        "rel_120": rel_perf(close_s, btc_s, 120),
        "rv60": rv60, "two_x_drag": two_x_drag_pct(rv60),
        "stats": threshold_stats(s), "warnings": warnings,
    }


def run(cfgs: dict, *, cache_dir: Path, today: date, live_px: dict | None = None,
        live_btc: float | None = None, download=_download) -> dict:
    """{SYMBOL: cfg} -> {"by_symbol": {SYMBOL: assess | {"error"}}, "cfgs", "warnings",
    "from_cache", "fetched"}。每只各自取数 (各自缓存), 一只失败不连累别的。"""
    live_px = live_px or {}
    by, warns, from_cache, fetched = {}, [], True, None
    for sym, cfg in cfgs.items():
        try:
            got = fetch_pair(cfg["yf_ticker"], cache_dir / CACHE_NAME.format(sym), today,
                             download)
            warns += got["warnings"]
            from_cache = from_cache and got["from_cache"]
            fetched = fetched or got["fetched"]
            a = assess(got["df"], cfg, today=today, live_px=live_px.get(sym), live_btc=live_btc)
            a["from_cache"] = got["from_cache"]
            by[sym] = a
        except Exception as e:      # noqa: BLE001
            by[sym] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
    return {"by_symbol": by, "cfgs": cfgs, "warnings": warns,
            "from_cache": from_cache if by else False, "fetched": fetched}


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
    head = (f"- **{sym}** 收 {a['px']:.2f}{' (扫描器收盘)' if a['px_src'] == 'live' else ''} "
            f"/ BTC {_k(a['btc'])} → 比值 **{a['ratio']:.3f}‰** · 全史 {a['pct']:.1f} 分位 "
            f"({a['first_date']} 起 {a['n_days']:,} 个交易日, 最低 {a['hist_min']:.3f} @{a['hist_min_date']})")
    if a["mcap_pct"] is not None:
        head += (f" · 市值比值 {a['mcap_pct']:.2f}% (股本 {cfg['shares_m']:g}M @{cfg['shares_asof']}"
                 + (f", 历史最低 {a['mcap_min']:.2f}% @{a['mcap_min_date']}, "
                    f"{a['mcap_pctile']:.1f} 分位" if a["mcap_min"] is not None else "") + ")")
    out = [head, f"  - 桶 {a['bucket']}: {BUCKET_NOTE[a['bucket']]}"]
    if f["all_time_low"]:
        out.append("  - ⚠️ 比值历史新低 — 之前的最低 (2023-05-03 1.672) 是 SEC 起诉前夜的定价, "
                   "新低区没有历史可参考, 只能用市值比值地板和分档")
    if f["break_t1"]:
        out.append(f"  - 首次收盘跌破 {cfg['t1']:g} (之前 {cfg['break_lookback']} 个交易日都在上方) — "
                   f"{EPISODE_NOTE}")
    if f["tier"]:
        out.append(f"  - 🟢 现价已在 {f['tier']} 档内 (连续 {a['streak_t1']} 个交易日 ≤ {cfg['t1']:g}; "
                   f"{EPISODE_NOTE})")
    elif f["watch"]:
        out.append(f"  - 👀 关注区 (比值 ≤ {cfg['watch']:g}, 2.0–2.2 桶 60 天胜率 99%) — 等 T1 {cfg['t1']:g}")
    if f["take_profit"]:
        tp_lo, tp_hi = cfg["take_profit"]
        where = (f"在止盈带 {tp_lo:g}–{tp_hi:g} 内" if a["ratio"] < tp_hi
                 else f"已高于止盈带上沿 {tp_hi:g}")
        out.append(f"  - 🔔 {where} (近 {cfg['tp_memory_days']} 个交易日到过 {cfg['t1']:g} 线): "
                   "持有比值多头的话分批了结 — 2.4–2.6 是一年中位附近, 60 天胜率跌到六成; "
                   "2025 的三段 250 天只剩 +1 ~ +21%")
    lad = a["ladder"]
    lad_s = (f"  - 阶梯 (按当前 BTC / −10% / −20% 换算): 关注 ≤{cfg['watch']:g} {_grid(lad['watch'])} · "
             f"T1 ≤{cfg['t1']:g} {_grid(lad['t1'])} (第一批 1/3, 接受 −25% 回撤) · "
             f"T2 ≤{cfg['t2']:g} {_grid(lad['t2'])} (历史最低) · "
             f"T3 ≤{cfg['t3']:g} {_grid(lad['t3'])} (从未出现)")
    if lad["mcap_floor"]:
        lad_s += f" · 市值比值地板 {cfg['mcap_floor_pct']:g}% {_grid(lad['mcap_floor'])}"
    lad_s += (f" · 止盈 {cfg['take_profit'][0]:g}–{cfg['take_profit'][1]:g} = "
              f"${a['take_profit_px'][0]:,.0f}–{a['take_profit_px'][1]:,.0f}")
    out.append(lad_s)
    dv = a["divergence"]
    if f["divergence"]:
        out.append(f"  - ⚠️ 背离: 比值创 {cfg['div_lookback']} 日新低而 BTC 高于 {cfg['div_btc_days']} "
                   f"日前 ({dv['btc_chg']:+.0f}%) — {DIVERGENCE_NOTE}")
    rp = a["rel_120"]
    if rp:
        out.append(f"  - 近 {rp['days']} 个交易日 {sym} {rp['stock']:+.0f}% vs BTC {rp['btc']:+.0f}% "
                   f"(差 {rp['diff']:+.0f} 点)")
    if tr["sma20"] is not None:
        pos50 = ("" if tr["sma50"] is None else
                 f", 50 日线 {_k(tr['sma50'])} {'上' if tr['above50'] else '下'}")
        out.append(f"  - BTC {_k(tr['px'])} 在 20 日线 {_k(tr['sma20'])} "
                   f"{'上' if tr['above20'] else '下'}方{pos50} — {BTC_FILTER_NOTE}")
    tools = "工具: 正股分批 (阶梯三档), 不出期权票"
    if cfg.get("two_x"):
        drag = (f", 近 60 日 RV {a['rv60'] * 100:.0f}% → 约 −{a['two_x_drag']:.0f}%/年 vs 2 倍正股"
                if a["two_x_drag"] is not None else "")
        tools += f"; 2x ETF {cfg['two_x']} {TWO_X_NOTE}{drag}"
    out.append("  - " + tools)
    st = a["stats"]
    tail = ["历史 (比值, 交易日): " + " · ".join(
        f"<{thr:g} {st[thr]['days']} 天/{st[thr]['episodes']} 段" for thr in STATS_THRESHOLDS)]
    out.append("  - " + " · ".join(tail))
    for w in a["warnings"]:
        out.append(f"  - ⚠️ {w}")
    if cfg.get("notes"):
        out.append(f"  - 备注: {cfg['notes']}")
    return out


def report_block(meta: dict | None) -> list[str]:
    """报告段。meta=None (--no-btcratio / 未配置) 整段不出。cfgs 随 meta 走 (run 放进去)。"""
    cfgs = (meta or {}).get("cfgs")
    if meta is None or not cfgs:
        return []
    lines = ["## BTC 平台股 / BTC 比值 (yfinance)", ""]
    for sym, cfg in cfgs.items():
        a = (meta.get("by_symbol") or {}).get(sym)
        if a is None:
            continue
        if a.get("error"):
            lines.append(f"- ⚠️ **{sym}** 比值取不到: {a['error']} — 只缺这一段, 下次扫描重试")
            continue
        lines += symbol_lines(sym, a, cfg)
    for w in meta.get("warnings") or []:
        lines.append(f"- ⚠️ {w}")
    lines += ["",
              "> 比值 = 收盘 ÷ BTC × 1000 (‰); 市值比值 = 标的市值 ÷ BTC 市值 (股本历史按 10-Q 锚点"
              "插值, 近似)。阶梯按当时 BTC 实时换算, 别记固定价。回测 2021-04 → 2026-10, <2.0 的"
              "样本只有三组 (2022-05 / 2022-12→2023-06 / 2025-04→05), 胜率按三次经验读 "
              "(research/btcratio_backtest.py); BTC 上涨中的比值新低先例只有 2023-04/05 和 2025-03/04。"
              "只做提示, 不进状态机/门控/票据; 只考虑正股与 2x ETF, 不出期权票"
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
                     "btc": round(a["btc"], 2), "ratio": round(a["ratio"], 5),
                     "pct": round(a["pct"], 2) if a["pct"] == a["pct"] else "",
                     "mcap_pct": (round(a["mcap_pct"], 4) if a["mcap_pct"] is not None else ""),
                     "shares_m": cfg["shares_m"] if cfg["shares_m"] is not None else "",
                     "bucket": a["bucket"], "flags": flags.strip(","),
                     "streak_t1": a["streak_t1"], "px_src": a["px_src"],
                     "data_asof": a["asof"]})
    return rows


def append_history(rows: list[dict], path: Path) -> int:
    """追加 -> 新写入行数; (date, symbol) 已有的跳过 (同 iv_history / mnav_history)。"""
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
    cfgs = validate_config(raw.get("btcratio"), today)
    if argv:
        keep = {a.upper() for a in argv}
        cfgs = {k: v for k, v in cfgs.items() if k in keep}
    if not cfgs:
        print("watchlist.toml 没有 [btcratio.*] 配置 (或过滤后为空)")
        return 1
    meta = run(cfgs, cache_dir=base / "data", today=today)
    print("\n".join(report_block(meta)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
