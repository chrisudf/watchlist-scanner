#!/usr/bin/env python3
"""Left/right-side watchlist scanner (开盘异动 + 尾盘信号).

Two scheduled passes over a fixed watchlist (watchlist.toml), mechanically
applying the rules from 危机黄金与左右侧交易-实战笔记.md:

  open  pass (~09:45 ET): overnight gaps, value-zone entries, earnings
        today/next days, regime changes, trailing-stop warnings.
        NO right-side confirmations — those are close-based by design.
  close pass (~15:45 ET): full signal engine per ticker:
        - right-side confirmation, 2 of 3: stopped making new lows /
          reclaimed the 20dma on volume / broke the 20d high
          (only counted after an actual pullback — a quiet uptrend never
          "confirms")
        - state machine UPTREND/PULLBACK/LEFT_ZONE/CONFIRMED/TREND with a
          20dma trailing-stop alert once right-side
        - VIX/VIX3M regime gate: stage 1 (inverted) = sellers only, no new
          right-side entries; stage 2 window (inversion just resolved) =
          LEAP entries allowed ("buy the relief, not the panic")
        - concrete option tickets: CSP (2-4wk delta 0.10-0.15 normal /
          weekly 16-rule distance in panic, never across earnings) and
          LEAP calls (deep ITM 0.70-0.85 delta, 450-1100 DTE Jan cycle,
          OI >= 500, spread <= 5%, extrinsic <= 40%, enter after earnings)

Data: yfinance only (prices, chains, earnings calendar) — free, ~15min
delayed, quotes zero out overnight, so both scan windows sit inside US RTH
by construction (a freshness gate skips holidays/half-days).

Value zones are YOUR judgment, maintained by hand in watchlist.toml; the
scanner only measures distance to them. Left-side suggestions stay off for
tickers without a zone. Not investment advice — the tickets follow the
playbook mechanically and know nothing the tape doesn't.

Exit codes: 0 = report written, 3 = intentionally skipped (outside window,
duplicate run, market not live), 1 = error.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import sys
import tomllib
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf

# ETFs have no earnings calendar — silence yfinance's 404 chatter
import logging
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

ET = ZoneInfo("America/New_York")
BASE = Path(__file__).resolve().parent
REPORTS = BASE / "reports"
DATA = BASE / "data"
STATE_FILE = DATA / "state.json"
IV_HISTORY = DATA / "iv_history.csv"
CONFIG_FILE = BASE / "watchlist.toml"

RATE = 0.04                      # risk-free for BS delta/IV inversion
SKIP = 3                         # exit code for intentional no-op runs

# ET windows the auto mode accepts. launchd fires at 4 fixed Brisbane times
# (2 candidates per scan to survive the US DST shift); whichever lands inside
# a window runs, the rest exit SKIP. Duplicates are caught by report-exists.
OPEN_WINDOW = ((9, 40), (10, 50))
CLOSE_WINDOW = ((15, 30), (16, 5))

MAX_STALE_TRADE_DAYS = 5         # option lastPrice older than this = unusable


# --------------------------------------------------------------------------
# Config / persistence
# --------------------------------------------------------------------------

TICKER_DEFAULTS = {"kind": "stock", "options": True, "high_beta": False,
                   "value_zone": None, "two_x": None, "notes": ""}

SETTINGS_DEFAULTS = {
    "gap_alert_pct": 1.5,        # open pass: flag |gap| above this
    "move_alert_pct": 2.0,       # open pass: flag |move since prev close|
    "volume_surge": 1.5,         # x 20d avg volume = "放量"
    "near_zone_pct": 5.0,        # within this % above zone top = NEAR_ZONE
    "earnings_alert_days": 2,    # open pass: flag earnings within N days
    # CSP (剧本: 平时 2-4 周 delta 0.10-0.15)
    "csp_delta_lo": 0.10, "csp_delta_hi": 0.15, "csp_delta_target": 0.12,
    "csp_dte_normal": [12, 31],  # 平时卖 2-4 周
    "csp_dte_panic": [4, 10],    # 恐慌期卖周权
    "csp_min_oi": 200, "csp_min_mid": 0.20,
    "csp_min_annualized": 10.0,  # 年化下限 % (moomoo 筛选器同口径 10~80)
    "sixteen_rule_mult": 2.75,   # 距离 >= 2.5-3 x IV/16 x sqrt(DTE)
    # 正股分批 (剧本: 档位更深、间距更大、末档留给真正的恐慌价)
    "ladder_panic_discount": 0.18,   # 末档 = 区间下沿再打 18% 折扣
    # 回踩 call spread (剧本: 突破后首次回踩 -> 3-6 个月 call spread)
    "spread_dte": [80, 200],
    "spread_long_delta": 0.60, "spread_short_delta": 0.30,
    "spread_min_reward_risk": 0.6,  # 0.60/0.30 价差正常 ~1:1 — 低于此=报价失真
    "trend_middle_days": 30,     # TREND 持续 N 天 -> 2x/PMCC 工具切换提示
    "two_x_vix_max": 25.0,       # 波动收敛门: VIX 低于此才提示 2x/PMCC
    # LEAP
    "leap_dte": [450, 1100],
    "leap_delta_index": [0.70, 0.80],
    "leap_delta_stock": [0.75, 0.85],
    "leap_min_oi": 500, "leap_max_spread_pct": 5.0,
    "leap_max_extrinsic_pct": 40.0,
    "leap_earnings_buffer_days": 14,  # 财报前 <=2 周不进 LEAP (2026-08-09 校准)
    "leap_earnings_note_days": 30,    # 财报 15-30 天内出票但带提示
    # regime
    "stage2_window_bars": 10,    # trading days after inversion resolves
    "episode_min_days": 3, "episode_min_peak": 1.10,
    # 倒挂门控矩阵 (2026-09-02 研究: VIX 系族横评 + 回测证据实查)
    "vx_full_backwardation_halt": True,  # VX 全曲线倒挂 = 停开新票 (21/22 crash filter)
    "vx_curve_contracts": 5,             # 曲线形态看前 N 个月度合约
    "vvix_halt": 110.0,          # NORMAL 期 VVIX >= 此值 = 停开新 CSP
    "move_divergence": 100.0,    # MOVE > 此值且 VIX 平静 = 债波先行预警
    "move_calm_vix_max": 18.0,   # "VIX 平静"的上限
    "rr_dte": [20, 60],          # 25Δ risk reversal 取样窗口 (取最接近 35 DTE)
    "rr_delta_tol": 0.10,        # 链上找不到 |Δ-0.25|<=tol 的行权价 = 无读数
    "rr_invert_min_pts": 1.0,    # RR < -此值才亮倒挂旗标 (延迟报价噪声地板)
    "rr_max_rel_spread": 0.25,   # (ask-bid)/mid 超此值的报价不用于 RR
    "rr_min_oi": 10,             # RR 两腿的未平仓量地板
    # forward 反解 spot 与日线收盘的差超此值 = 日线陈旧。35 DTE 的正常持有
    # 成本 |F/S-1| = |e^((r-q)T)-1| 约 0.3%, 1.5% 已是其 5 倍; 难借券的负
    # rebate 可能触发, 但那种标的本来也值得看一眼 (2026-09-04 实测: 陈旧
    # 一天的日线造成 2.0%~16.5% 的差, 3% 会漏掉 MSFT 这档)
    "rr_spot_gap_warn": 0.015,
}


def load_config(path: Path = CONFIG_FILE) -> tuple[dict, dict]:
    """-> (settings, {symbol: ticker_cfg}). Ticker order follows the file."""
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    settings = {**SETTINGS_DEFAULTS, **raw.get("settings", {})}
    tickers = {}
    for sym, tcfg in raw.get("tickers", {}).items():
        cfg = {**TICKER_DEFAULTS, **tcfg}
        zone = cfg["value_zone"]
        if zone is not None and (len(zone) != 2 or zone[0] >= zone[1]):
            raise ValueError(f"{sym}: value_zone must be [low, high]")
        tickers[sym.upper()] = cfg
    if not tickers:
        raise ValueError("watchlist.toml has no [tickers.*] entries")
    return settings, tickers


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                          encoding="utf-8")


def next_persisted_state(prev: dict, r: dict, today: str) -> dict:
    """close 收盘后单标的 state.json 条目 (纯函数, 便于测试).
    - since: 状态变了才刷新
    - leap_window: 阶段2窗口 dedup key, 跨日携带
    - retested: 回踩一次性提示 — 新一轮确认重新计数, 本日回踩置位, 否则沿用
    - leap_pending / retest_pending: 被硬停牌拦下的一次性事件补发标记 —
      默认跨日**沿用**, 只在显式消耗 (真票发出 / 回踩已提示) 或确认周期
      死亡 (状态离开 CONFIRMED/TREND) 时清除。不能拿"本次结果没带标记"
      当消耗信号: --no-options 的非 manual 收盘跑在期权分析前就 return,
      会静默抹掉所有在途标记 (三轮评审)"""
    since = prev.get("since")
    if prev.get("state") != r["state"]:
        since = today
    entry = {"state": r["state"], "since": since}
    lw = r.get("leap_window") or prev.get("leap_window")
    if lw:
        entry["leap_window"] = lw
    # NORMAL 期 LEAP 被硬停牌拦下的补发标记: 确认转换是一次性的且会被
    # state.json 无条件消耗, halt 不该吞掉它 — 标记随右侧状态存活,
    # 止损出局 (转 PULLBACK) 即失效; 真票发出当日不再置位, 自然清除
    leap_pending = bool(prev.get("leap_pending") or r.get("leap_pending"))
    if r.get("leap_emitted"):           # 真票已发 = 显式消耗
        leap_pending = False
    if leap_pending and entry["state"] in ("CONFIRMED", "TREND"):
        entry["leap_pending"] = True
    retested = prev.get("retested", False)
    retest_pending = bool(prev.get("retest_pending") or r.get("retest_pending"))
    if r["state"] == "CONFIRMED" \
            and prev.get("state") not in ("CONFIRMED", "TREND"):
        retested = False                # 新一轮确认: 一次性提示重新计数
        retest_pending = False
    if r.get("retest"):                 # 回踩已提示 = 显式消耗
        retested = True
        retest_pending = False
    if retested:
        entry["retested"] = True
    if retest_pending and entry["state"] in ("CONFIRMED", "TREND"):
        entry["retest_pending"] = True
    return entry


# --------------------------------------------------------------------------
# Market clock gating
# --------------------------------------------------------------------------

def in_window(now_et: datetime, window) -> bool:
    (h1, m1), (h2, m2) = window
    t = (now_et.hour, now_et.minute)
    return (h1, m1) <= t <= (h2, m2)


def resolve_mode(arg_mode: str, now_et: datetime) -> str | None:
    """'open'/'close' passthrough; 'auto' decides from the ET clock.
    Returns None when auto lands outside both windows (caller exits SKIP)."""
    if arg_mode != "auto":
        return arg_mode
    if now_et.weekday() >= 5:
        return None
    if in_window(now_et, OPEN_WINDOW):
        return "open"
    if in_window(now_et, CLOSE_WINDOW):
        return "close"
    return None


def market_is_live() -> bool:
    """SPY has printed a 1m bar in the last 20 minutes = US session live.
    Catches weekends, holidays and half-days without a holiday calendar.
    A dead feed raises instead of returning False — on a holiday Yahoo still
    serves the previous session's bars, so exception/empty means the data
    feed is broken and must surface as a FAILURE, not a silent skip."""
    try:
        bars = yf.Ticker("SPY").history(period="1d", interval="1m")
    except Exception as e:
        raise RuntimeError(
            f"SPY liveness fetch failed ({type(e).__name__}: {e})") from e
    if bars.empty:
        raise RuntimeError("SPY liveness fetch returned no bars — feed problem")
    last = bars.index[-1].to_pydatetime()
    return (datetime.now(timezone.utc) - last).total_seconds() < 20 * 60


# --------------------------------------------------------------------------
# Regime: VIX/VIX3M term-structure state machine
# --------------------------------------------------------------------------

def inversion_episodes(ratio: pd.Series) -> list[dict]:
    """Contiguous runs of ratio >= 1.0 -> [{start, end, days, peak, ongoing}]."""
    episodes, cur = [], None
    for ts, val in ratio.items():
        if val >= 1.0:
            if cur is None:
                cur = {"start": ts, "end": ts, "days": 0, "peak": float(val)}
            cur["days"] += 1
            cur["end"] = ts
            cur["peak"] = max(cur["peak"], float(val))
        elif cur is not None:
            episodes.append({**cur, "ongoing": False})
            cur = None
    if cur is not None:
        episodes.append({**cur, "ongoing": True})
    return episodes


def classify_regime(ratio: pd.Series, s: dict) -> tuple[str, list[dict]]:
    """-> (stage, episodes). Stages:
    STAGE1_DEEP  ratio >= 1.10 (历史级恐慌区: CSP 第二/三档)
    STAGE1       ratio >= 1.0  (倒挂: 只做卖方, 右侧停)
    STAGE2_WINDOW inversion (>=3d, peak >=1.10) resolved within N bars
                 (解除窗口: LEAP/risk-reversal 允许)
    NORMAL       everything else
    """
    episodes = inversion_episodes(ratio)
    r = float(ratio.iloc[-1])
    if r >= 1.10:
        return "STAGE1_DEEP", episodes
    if r >= 1.0:
        return "STAGE1", episodes
    qual = [e for e in episodes if not e["ongoing"]
            and e["days"] >= s["episode_min_days"]
            and e["peak"] >= s["episode_min_peak"]]
    if qual:
        bars_since = len(ratio) - 1 - ratio.index.get_loc(qual[-1]["end"])
        if bars_since <= s["stage2_window_bars"]:
            return "STAGE2_WINDOW", episodes
    return "NORMAL", episodes


CBOE_HISTORY_URL = ("https://cdn.cboe.com/api/global/us_indices/"
                    "daily_prices/{}_History.csv")


def _cboe_series(name: str) -> pd.Series:
    """Official CBOE daily closes. Primary source for the VIX complex —
    Yahoo's ^VIX3M/^VIX9D feeds go stale for weeks at a time (observed
    2026-08: ^VIX3M frozen since 07-17 while ^VIX stayed current).
    VIX/VIX3M/VXN files carry an OHLC 的 CLOSE 列; VVIX 只有两列
    (DATE,VVIX) — 没有 CLOSE 时取第二列。"""
    req = urllib.request.Request(CBOE_HISTORY_URL.format(name),
                                 headers={"User-Agent": "Mozilla/5.0"})
    text = urllib.request.urlopen(req, timeout=30).read().decode()
    df = pd.read_csv(io.StringIO(text))
    col = "CLOSE" if "CLOSE" in df.columns else df.columns[1]
    ser = pd.Series(df[col].astype(float).values,
                    index=pd.to_datetime(df["DATE"], format="%m/%d/%Y"))
    return ser.tail(400)


def _yahoo_vix_pair() -> tuple[pd.Series, pd.Series]:
    df = yf.download(["^VIX", "^VIX3M"], period="1y", interval="1d",
                     auto_adjust=False, progress=False)["Close"]
    return df["^VIX"].dropna(), df["^VIX3M"].dropna()


CBOE_QUOTE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_{}.json"


def _cboe_delayed(name: str) -> tuple[float, datetime]:
    """15-min delayed intraday index level + last trade time (ET, naive)."""
    req = urllib.request.Request(CBOE_QUOTE_URL.format(name),
                                 headers={"User-Agent": "Mozilla/5.0"})
    d = json.loads(urllib.request.urlopen(req, timeout=30).read())["data"]
    return float(d["current_price"]), datetime.fromisoformat(d["last_trade_time"])


VX_SETTLE_URL = ("https://www.cboe.com/us/futures/market_statistics/"
                 "settlement/csv?dt={}")


def parse_vx_settlement(text: str) -> list[tuple[str, float]]:
    """Monthly VX settlements from the CFE daily settlement CSV
    (Product,Symbol,Expiration Date,Price), sorted by expiry. Weekly rows
    (VX35/U6 ...) are excluded — the CSV pads them with the front-month
    price (observed 2026-09-01: six different weeklies all printing
    17.2528), so they carry no curve information. VXM/VA rows likewise."""
    out = []
    for line in text.splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4 or parts[0] != "VX":
            continue
        if not parts[1].startswith("VX/"):   # monthly = "VX/U6", weekly = "VX35/U6"
            continue
        try:
            out.append((parts[2], float(parts[3])))
        except ValueError:
            continue
    return sorted(out)


def vx_curve_state(prices: list[float], n_front: int = 5) -> str | None:
    """VX 期货曲线形态, 看前 n_front 个月度合约:
    FULL_BACKWARDATION   逐对递减 (eco3min 口径 — 2004 年以来 22 次,
                         21 次在 30 天内伴随 SPX >5% 回撤; 唯一漏网
                         2013 taper tantrum 只是局部倒挂)
    PARTIAL_BACKWARDATION  M1 > M2 但未全曲线
    CONTANGO             其余 (含混合形态)
    None                 合约不足 2 个, 无读数"""
    p = prices[:n_front]
    if len(p) < 2:
        return None
    diffs = [p[i + 1] - p[i] for i in range(len(p) - 1)]
    # 相邻月度平价 (tie) 是这个 feed 的实测形态 (未成交行填充) — 全程
    # 非升且至少一段真跌 = 实质全曲线倒挂, 严格 < 会被一个 tie 静默降级
    if all(d <= 0 for d in diffs) and any(d < 0 for d in diffs):
        return "FULL_BACKWARDATION"
    # tie 容忍只给 FULL (全程非升的曲线里一个填充平价不该静默降级)。
    # PARTIAL 是"前端承压"的判断, 必须真的 M1 > M2 — 平价前端 + 后段
    # 单点回落但整体上行是混合曲线, 归 CONTANGO; 否则警告会渲染出
    # "25.00 > 25.00" 这种自相矛盾的读数 (三轮评审)
    if diffs[0] < 0:
        return "PARTIAL_BACKWARDATION"
    return "CONTANGO"


def fetch_vx_curve(n_front: int = 5) -> dict:
    """Latest CFE settlement curve — walks back up to a week to find the
    most recent business day with rows (settlement publishes after the
    close, so intraday the newest file is yesterday's). Returns
    {'error': ...} instead of raising: the VX gate degrades to
    advisory-off when the feed is down, mirroring the vxn convention."""
    today = datetime.now(ET).date()
    last_err = "no settlement rows found"
    for back in range(7):
        dt = today - timedelta(days=back)
        if dt.weekday() >= 5:
            continue
        try:
            req = urllib.request.Request(VX_SETTLE_URL.format(dt.isoformat()),
                                         headers={"User-Agent": "Mozilla/5.0"})
            text = urllib.request.urlopen(req, timeout=30).read().decode()
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            continue
        rows = parse_vx_settlement(text)
        # VX 月度到期日当天上午结算之后, 前一交易日的文件里那份**已到期**
        # 合约仍在, sorted 之后会被当成 M1。临到期合约收敛到现货 VIX,
        # VIX 一跳就把曲线前端顶起来 — 长得像倒挂, 而它已经不可交易 →
        # 误报硬停牌。必须在完整性检查**之前**滤掉 (顺序反了的话, 滤完
        # 不足 n_front 会被当成"结算未发布"而多回看一天)
        rows = [(e, px) for e, px in rows if e > today.isoformat()]
        # 盘中/假日/未来日期该端点会回 1-2 行的 stub (HTTP 200) — 不足
        # n_front 个月度合约视为"当日结算未发布", 回看上一交易日, 绝不
        # 拿残缺曲线宣判 FULL_BACKWARDATION 硬停牌
        if len(rows) >= n_front:
            prices = [p for _e, p in rows]
            return {
                "as_of": dt.isoformat(),
                "state": vx_curve_state(prices, n_front),
                "m1": prices[0], "m2": prices[1],
                "m1_exp": rows[0][0], "m2_exp": rows[1][0],
                "m1_m2_pct": (prices[1] / prices[0] - 1) * 100,
                "n_contracts": len(rows),
            }
    return {"error": last_err}


def fetch_vvix() -> dict:
    """VVIX (VIX 期权隐含的 vol-of-vol) — CBOE 日收盘 + 同日盘中临时点,
    与 VIX 的 intraday graft 同约定. -> {'value','as_of'} or {'error'}.
    历史 CSV 冻结 (>5 交易日) 且盘中点也拿不到时按 error 报 — 这是唯一
    在 NORMAL 期硬拦 CSP 的门, 绝不能拿旧数当读数 (与 fetch_move 同约定;
    CBOE/Yahoo 指数 feed 断更有前科)."""
    # 两条路径必须**独立**: 历史 CSP 请求失败时盘中点仍可能是健康的,
    # 把它嵌在 CSV 成功之后的内层 try 里, 等于把文档写的"且"做成了"或"。
    # 而且失效方向是 fail-open — 这是 NORMAL 期唯一硬拦 CSP 的门, CSV
    # 端点打个嗝就静默关掉整道门 (三轮评审)
    val = as_of = None
    fresh = False
    errs = []
    try:
        ser = _cboe_series("VVIX")
        val, as_of = float(ser.iloc[-1]), str(ser.index[-1].date())
        fresh = int(np.busday_count(ser.index[-1].date(),
                                    datetime.now(ET).date())) <= 5
        if not fresh:
            errs.append(f"history stale (最新 {as_of})")
    except Exception as e:
        errs.append(f"history {type(e).__name__}: {e}")
    try:
        v_now, v_ts = _cboe_delayed("VVIX")
        if v_ts.date() == datetime.now(ET).date():
            val, as_of, fresh = v_now, f"{v_ts.date()} 盘中", True
        else:
            errs.append(f"delayed 非当日 ({v_ts.date()})")
    except Exception as e:
        errs.append(f"delayed {type(e).__name__}: {e}")
    if not fresh or val is None:
        return {"error": "; ".join(errs) or "no reading"}
    return {"value": val, "as_of": as_of}


def fetch_move() -> dict:
    """^MOVE (ICE BofA 美债波动率) — 只有 Yahoo 源, 无 CBOE 兜底。
    Yahoo 指数 feed 会断更 (见 ^VIX3M 前科): 最新点老于 5 个交易日
    按 error 报, 不当读数用. -> {'value','as_of'} or {'error'}."""
    try:
        ser = yf.download("^MOVE", period="3mo", interval="1d",
                          auto_adjust=False, progress=False)["Close"]
        if hasattr(ser, "columns"):     # 单 ticker 也可能回 DataFrame
            ser = ser.iloc[:, 0]
        ser = ser.dropna()
        if ser.empty:
            return {"error": "empty feed"}
        last_date = ser.index[-1].date()
        age = int(np.busday_count(last_date, datetime.now(ET).date()))
        if age > 5:
            return {"error": f"stale (最新 {last_date})"}
        return {"value": float(ser.iloc[-1]), "as_of": str(last_date)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def assess_vol_gates(stage: str, vx: dict, vvix: dict, move: dict,
                     vix_level: float, s: dict) -> dict:
    """Cross-signal gates layered on top of the VIX/VIX3M stage machine.
    -> {"halt_csp": str|None, "halt_new_longs": str|None, "warnings": [...]}
    halt_csp 拦新 CSP 票, halt_new_longs 拦 LEAP/回踩 spread —
    票据以 skip_reason 呈现, 原因随票可见。数据缺失 (error dict) 不拦:
    门控宁可漏也不能靠坏数据硬拦。"""
    halt_csp, halt_longs, warnings = None, None, []
    if vx.get("state") == "FULL_BACKWARDATION":
        # FULL 容忍相邻平价, 前端可能 m1 == m2 — 只渲染观测值, 不写 ">"
        base = (f"VX 期货全曲线倒挂 (M1 {vx['m1']:.2f} / M2 {vx['m2']:.2f}, "
                f"结算 {vx['as_of']}) — 2004 年以来 22 次中 21 次在 30 天内 "
                "SPX 回撤 >5%")
        # 消息必须与实际拦截范围一致 — 开关关闭时 CSP 在流, 不能仍写"停开新票"
        # 开关的语义是"放行**剧本恐慌档** CSP" (README) — 而 VX 全曲线
        # 倒挂恰恰可以与 VIX/VIX3M NORMAL 并存 (这正是本门存在的理由),
        # 所以不能让它在 NORMAL 期放行普通 CSP: 那正是本门要拦的场景
        if s["vx_full_backwardation_halt"]:
            halt_csp = halt_longs = (
                base + ": 停开新 CSP/LEAP/回踩 spread, 持有对冲 "
                "(恢复剧本恐慌档 CSP 关 vx_full_backwardation_halt)")
        elif stage.startswith("STAGE1"):
            halt_longs = (
                base + ": 停开新 LEAP/回踩 spread, 持有对冲 — CSP 已按 "
                "vx_full_backwardation_halt=false 放行 (剧本恐慌档)")
        else:
            halt_csp = halt_longs = (
                base + ": 停开新 CSP/LEAP/回踩 spread, 持有对冲 — "
                f"vx_full_backwardation_halt=false 只放行剧本恐慌档 "
                f"(STAGE1) CSP, 当前 {stage} 不适用")
    elif vx.get("state") == "PARTIAL_BACKWARDATION":
        warnings.append(
            f"VX 期货 M1 {vx['m1']:.2f} > M2 {vx['m2']:.2f} 局部倒挂 — "
            "前端承压, 关注是否蔓延成全曲线 (全曲线 = 硬停牌)")

    # VVIX 停牌线: 只管 NORMAL — 平静表面下 vol-of-vol 抢跑 = 对冲拥挤/
    # 裂缝先兆, 不该再开新短 put。STAGE1 恐慌档 (16法则) 与 STAGE2 解除窗
    # (统计加成) 都按剧本走, VVIX 高是那两个 regime 的常态, 不加拦。
    v = vvix.get("value")
    if halt_csp is None and stage == "NORMAL" \
            and v is not None and v >= s["vvix_halt"]:
        halt_csp = (f"VVIX {v:.1f} (as of {vvix.get('as_of', '?')}) >= "
                    f"{s['vvix_halt']:g} 而 regime 仍 NORMAL — vol-of-vol "
                    "抢跑 (对冲拥挤/裂缝先兆): 停开新 CSP, 等 VVIX 回落"
                    "或 regime 表态")
    # MOVE 背离预警: 债波先行于股波 (2023-03 SVB: MOVE 130→200 两天,
    # VIX 晚数日; 2025-04 basis trade: MOVE ~172 先到) — 预警不拦票。
    m = move.get("value")
    if m is not None and m > s["move_divergence"] \
            and vix_level < s["move_calm_vix_max"]:
        warnings.append(
            f"MOVE {m:.1f} > {s['move_divergence']:g} 而 VIX 仅 "
            f"{vix_level:.1f} — 债券波动率先行 (2023-03 SVB / 2025-04 "
            "序列): 缩短 put 名义, 对冲前移到长期限指数 put")
    return {"halt_csp": halt_csp, "halt_new_longs": halt_longs,
            "warnings": warnings}


def fetch_regime(s: dict) -> dict:
    try:
        vix, vix3m = _cboe_series("VIX"), _cboe_series("VIX3M")
        source = "CBOE"
    except Exception as e:
        print(f"  CBOE index feed failed ({type(e).__name__}) — Yahoo fallback",
              file=sys.stderr)
        vix, vix3m = _yahoo_vix_pair()
        source = "Yahoo"
    try:
        vxn_last = float(_cboe_series("VXN").iloc[-1])
    except Exception:
        vxn_last = None

    settled = (vix / vix3m).dropna()
    if len(settled) < 30:
        raise RuntimeError("VIX/VIX3M history too short — data problem")
    # staleness/as-of judged on SETTLED closes only (the honest date)
    age_days = (datetime.now(ET).date() - settled.index[-1].date()).days
    as_of = str(settled.index[-1].date())

    # The daily file lags a session (CBOE settles after the close), so graft
    # a provisional intraday point on top — a day-one inversion must gate
    # TODAY's tickets, not tomorrow's.
    intraday = False
    try:
        v_now, v_ts = _cboe_delayed("VIX")
        v3_now, v3_ts = _cboe_delayed("VIX3M")
        today_et = datetime.now(ET).date()
        if v_ts.date() == today_et == v3_ts.date():
            stamp = pd.Timestamp(today_et)
            vix = pd.concat(
                [vix[vix.index < stamp], pd.Series([v_now], index=[stamp])])
            vix3m = pd.concat(
                [vix3m[vix3m.index < stamp], pd.Series([v3_now], index=[stamp])])
            intraday = True
    except Exception:
        pass  # settled series still stands; staleness warning covers the gap

    ratio = (vix / vix3m).dropna()
    stage, episodes = classify_regime(ratio, s)
    prev = float(ratio.iloc[-2])
    cur = float(ratio.iloc[-1])
    vx = fetch_vx_curve(s["vx_curve_contracts"])
    vvix = fetch_vvix()
    move = fetch_move()
    gates = assess_vol_gates(stage, vx, vvix, move, float(vix.iloc[-1]), s)
    return {
        "vix": float(vix.iloc[-1]), "vix3m": float(vix3m.iloc[-1]),
        "vxn": vxn_last,
        "vvix": vvix, "move": move,
        "gate_lines": {"vvix_halt": s["vvix_halt"],
                       "move_divergence": s["move_divergence"]},
        "ratio": cur, "ratio_prev": prev,
        "crossed_up": prev < 1.0 <= cur, "crossed_down": prev >= 1.0 > cur,
        "stage": stage,
        "last_episode": episodes[-1] if episodes else None,
        "as_of": as_of,
        "source": source, "stale_days": age_days, "intraday": intraday,
        "vx": vx,
        "halt_csp": gates["halt_csp"],
        "halt_new_longs": gates["halt_new_longs"],
        "gate_warnings": gates["warnings"],
    }


REGIME_NOTES = {
    "NORMAL": "正常结构 — 左侧看个股价值区, 右侧按确认信号走",
    "STAGE1": "倒挂 (阶段1) — 剧本: 只做卖方 (CSP 第一档), 不加右侧仓",
    "STAGE1_DEEP": "倒挂 >1.1 (历史级恐慌区) — 剧本: CSP 加第二/三档, 周权+16法则, 右侧仍停",
    "STAGE2_WINDOW": ("倒挂解除窗口 (阶段2) — 剧本: buy the relief — 价格确认后 "
                      "LEAP/risk reversal; CSP 常规档解锁 (解除窗 = 统计最强"
                      "卖权入场窗: 解除日起 SPX 5日 +3.04%/88%, 21日 +4.38%/91%)"),
}


# --------------------------------------------------------------------------
# Technical signals — pure functions over daily OHLCV (unit-testable)
# --------------------------------------------------------------------------

def no_new_low(low: pd.Series) -> bool:
    """不再新低: last 5 sessions' low holds above the prior 15 sessions' low."""
    if len(low) < 20:
        return False
    return float(low.iloc[-5:].min()) > float(low.iloc[-20:-5].min())


def reclaimed_20dma(close: pd.Series, vol_ratio: float, surge: float) -> bool:
    """放量收复20日线: above the 20dma today, was below it within the last
    10 sessions, and today's volume runs >= surge x the 20d average."""
    if len(close) < 31:
        return False
    sma20 = close.rolling(20).mean()
    above_now = float(close.iloc[-1]) > float(sma20.iloc[-1])
    was_below = bool((close.iloc[-11:-1] < sma20.iloc[-11:-1]).any())
    return above_now and was_below and vol_ratio >= surge


def broke_20d_high(close: pd.Series, high: pd.Series) -> bool:
    """突破: close above the prior 20 sessions' high (trendline-break proxy)."""
    if len(high) < 21:
        return False
    return float(close.iloc[-1]) > float(high.iloc[-21:-1].max())


def had_pullback(close: pd.Series) -> bool:
    """Confirmation only means something after an actual decline: closed
    below the 20dma within the last 15 sessions, or 60d drawdown >= 8%."""
    if len(close) < 61:
        return False
    sma20 = close.rolling(20).mean()
    below_recent = bool((close.iloc[-16:-1] < sma20.iloc[-16:-1]).any())
    dd60 = float(close.iloc[-1]) / float(close.iloc[-60:].max()) - 1.0
    return below_recent or dd60 <= -0.08


def confirmation(close, high, low, vol_ratio, surge) -> dict:
    """右侧确认 (三选二), gated on a real pullback having happened."""
    a = no_new_low(low)
    b = reclaimed_20dma(close, vol_ratio, surge)
    c = broke_20d_high(close, high)
    pullback = had_pullback(close)
    return {"no_new_low": a, "reclaim20": b, "breakout": c,
            "pullback_context": pullback,
            "confirmed": pullback and (a + b + c) >= 2}


def next_state(prev: str, *, close: float, sma20: float, confirmed: bool,
               zone, near_pct: float) -> tuple[str, list[str]]:
    """State machine. Right-side states trail the 20dma; the trailing stop
    fires as a note on the transition day.

    左侧状态要求真实弱势 (收盘在20日线下): 价格从下方涨穿价值区不算左侧,
    那是趋势 (UPTREND)。CSP 触发与状态标签解耦 — 只要价格在/近价值区就
    出票 (在接货价挂收钱限价单, 与趋势方向无关)。"""
    notes = []
    in_zone = zone is not None and close <= zone[1]
    near_zone = (zone is not None and not in_zone
                 and close <= zone[1] * (1 + near_pct / 100))
    if prev in ("CONFIRMED", "TREND"):
        if close < sma20:
            notes.append("右侧止损触发: 收盘跌破20日线 (剧本: 移动止损, 凸性档减半/结构破清仓)")
            state = "PULLBACK"
        else:
            state = "TREND"
    elif confirmed:
        state = "CONFIRMED"
    elif close < sma20:
        if in_zone:
            state = "LEFT_ZONE"
            if close < zone[0]:
                notes.append("已跌破价值区下沿 — 剧本: 检查论点是否失效, 而不是继续摊")
        elif near_zone:
            state = "NEAR_ZONE"
        else:
            state = "PULLBACK"
    else:
        state = "UPTREND"
    return state, notes


STATE_LABEL = {
    "UPTREND": "趋势上方", "PULLBACK": "回调中(20日线下)",
    "NEAR_ZONE": "接近价值区", "LEFT_ZONE": "价值区内(左侧)",
    "CONFIRMED": "右侧确认", "TREND": "右侧持仓(跟踪20日线)",
    "NO_DATA": "数据不足",
}


def yang_zhang(price_data, window=30, trading_periods=252):
    """30d realized vol — copied unchanged from earnings-iv-scanner."""
    log_ho = (price_data["High"] / price_data["Open"]).apply(np.log)
    log_lo = (price_data["Low"] / price_data["Open"]).apply(np.log)
    log_co = (price_data["Close"] / price_data["Open"]).apply(np.log)
    log_oc = (price_data["Open"] / price_data["Close"].shift(1)).apply(np.log)
    log_cc = (price_data["Close"] / price_data["Close"].shift(1)).apply(np.log)
    rs = log_ho * (log_ho - log_co) + log_lo * (log_lo - log_co)
    close_vol = (log_cc ** 2).rolling(window).sum() / (window - 1.0)
    open_vol = (log_oc ** 2).rolling(window).sum() / (window - 1.0)
    window_rs = rs.rolling(window).sum() / (window - 1.0)
    k = 0.34 / (1.34 + ((window + 1) / (window - 1)))
    result = (open_vol + k * close_vol + (1 - k) * window_rs).apply(np.sqrt) \
        * math.sqrt(trading_periods)
    return float(result.iloc[-1])


# --------------------------------------------------------------------------
# Options math (BS bits from forward-volatility-calculator)
# --------------------------------------------------------------------------

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, r, sigma, is_call):
    if sigma <= 0 or T <= 0:
        fwd = S - K * math.exp(-r * T)
        return max(fwd, 0.0) if is_call else max(-fwd, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(S, K, T, r, sigma, is_call):
    if sigma <= 0 or T <= 0:
        itm = S > K if is_call else S < K
        return (1.0 if itm else 0.0) if is_call else (-1.0 if itm else 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


def implied_vol(price, S, K, T, r, is_call, lo=1e-3, hi=5.0):
    if price <= bs_price(S, K, T, r, lo, is_call) + 1e-8:
        return None
    if price >= bs_price(S, K, T, r, hi, is_call):
        return None
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if bs_price(S, K, T, r, mid, is_call) < price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def sixteen_rule_distance(iv: float, dte: int, mult: float) -> float:
    """恐慌期 CSP 距离 (fraction of spot): mult x IV/16 x sqrt(DTE).
    iv is a decimal (0.45 = 45%): 0.45/16 = daily move fraction."""
    return mult * (iv / 16.0) * math.sqrt(dte)


def csp_annualized(mid: float, strike: float, dte: int) -> float:
    """moomoo 口径: 权利金/(行权价-权利金) x 365/DTE, per share."""
    return mid / (strike - mid) * 365.0 / max(dte, 1) * 100


def _mark(row, stale_cutoff):
    """Usable option price: live bid/ask mid, else a recent last trade.

    crossed 报价 (bid > ask, Yahoo 实测会出) 不算有盘口 — mid 无意义,
    落到 lastPrice 路径 (src=\"last\" 自动带\"下单前实查\"提示)。此前只有
    _rr_mark 拒 crossed, CSP/LEAP/spread 的票价照收: 一档 bid 10.60 /
    ask 10.00 出来的 mid 10.30 标成 \"live\", 派生的 delta/外在/年化全
    是编的, 且负 spread_pct 反而通过 LEAP 的 <=5% 清洁过滤 (五轮评审)。"""
    bid = float(row.get("bid") or 0)
    ask = float(row.get("ask") or 0)
    if 0 < bid <= ask:
        return (bid + ask) / 2.0, "live"
    last = float(row.get("lastPrice") or 0)
    traded = row.get("lastTradeDate")
    if last > 0 and traded is not None and traded.to_pydatetime() >= stale_cutoff:
        return last, "last"
    return None, None


def _stale_cutoff():
    return datetime.now(timezone.utc) - timedelta(days=MAX_STALE_TRADE_DAYS)


def _oi(row) -> int:
    """openInterest as int; Yahoo omits the field for some contracts (NaN)."""
    oi = row.get("openInterest")
    return 0 if oi is None or pd.isna(oi) else int(oi)


def contract_iv(row, mid, spot, T, is_call):
    """Yahoo's impliedVolatility column when sane, else invert from mid."""
    iv = row.get("impliedVolatility")
    if iv is not None and not pd.isna(iv) and 0.01 < float(iv) < 5.0:
        return float(iv)
    if mid is not None:
        return implied_vol(mid, spot, float(row["strike"]), T, RATE, is_call)
    return None


class ChainCache:
    """One yf.Ticker per symbol; option chains fetched at most once."""

    def __init__(self, symbol: str):
        self.tk = yf.Ticker(symbol)
        self._chains: dict[str, object] = {}
        self._expiries: list[str] | None = None

    def expiries(self) -> list[tuple[str, int]]:
        if self._expiries is None:
            self._expiries = list(self.tk.options)
        today = datetime.now(ET).date()
        out = []
        for exp in self._expiries:
            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
            if dte > 0:
                out.append((exp, dte))
        return sorted(out, key=lambda x: x[1])

    def chain(self, exp: str):
        if exp not in self._chains:
            self._chains[exp] = self.tk.option_chain(exp)
        return self._chains[exp]


def atm_iv30(cc: ChainCache, spot: float) -> float | None:
    """ATM IV interpolated to 30 DTE from the two bracketing expiries
    (nearest expiry alone when only one side exists in 7..90 DTE)."""
    usable = [(e, d) for e, d in cc.expiries() if 7 <= d <= 90]
    if not usable:
        return None
    below = [x for x in usable if x[1] <= 30]
    above = [x for x in usable if x[1] > 30]
    picks = ([below[-1]] if below else []) + ([above[0]] if above else [])
    pts = []
    cutoff = _stale_cutoff()
    for exp, dte in picks:
        ch = cc.chain(exp)
        T = dte / 365.0
        ivs = []
        for df, is_call in ((ch.calls, True), (ch.puts, False)):
            if df is None or df.empty:
                continue
            idx = (df["strike"] - spot).abs().idxmin()
            row = df.loc[idx]
            mid, _src = _mark(row, cutoff)
            iv = contract_iv(row, mid, spot, T, is_call)
            if iv:
                ivs.append(iv)
        if ivs:
            pts.append((dte, sum(ivs) / len(ivs)))
    if not pts:
        return None
    if len(pts) == 1:
        return pts[0][1]
    (d1, v1), (d2, v2) = pts
    if d1 == d2:
        return (v1 + v2) / 2
    return v1 + (v2 - v1) * (30 - d1) / (d2 - d1)


def closest_delta_row(rows: list[dict], target: float,
                      tol: float = 0.10) -> dict | None:
    """|delta| 最接近 target 的行; 链稀疏到 tol 以外 = 无读数 (None),
    不硬凑 — 25Δ 读数宁缺毋错。"""
    if not rows:
        return None
    best = min(rows, key=lambda c: abs(abs(c["delta"]) - target))
    return best if abs(abs(best["delta"]) - target) <= tol else None


def rr25(call_rows: list[dict], put_rows: list[dict],
         tol: float = 0.10, invert_floor: float = 0.0) -> dict | None:
    """25Δ risk reversal = put IV - call IV (vol pts, OTM 两侧)。
    正常 skew 为正 (put 更贵); 负数 = call skew 倒挂 — 上涨追逐压过
    下跌对冲 (meme/挤仓形态, 2026-07 曾有 ~55% 的 SPX 成分股 1 月期
    倒挂, 超过 2021 meme 峰值口径)。"""
    c = closest_delta_row(call_rows, 0.25, tol)
    p = closest_delta_row(put_rows, 0.25, tol)
    if c is None or p is None or not c.get("iv") or not p.get("iv"):
        return None
    rr = (p["iv"] - c["iv"]) * 100
    # 噪声地板: Yahoo 延迟报价的 RR 读数有 ~1 vol pt 的 run-to-run 漂移
    # (2026-09-02 实测), 零阈值会在真实 skew ≈ 0 时反复亮假旗标 —
    # 倒挂旗标是"少卖 CSP/别买 LEAP"的行动信号, 假阳性直接烧权利金收入
    return {"call_iv": c["iv"], "put_iv": p["iv"],
            "call_strike": c["strike"], "put_strike": p["strike"],
            "call_delta": c["delta"], "put_delta": p["delta"],
            "rr": rr, "inverted": rr < -invert_floor}


def _rr_mark(row, cutoff, s: dict) -> float | None:
    """RR 专用的报价过滤 — 比 _mark 严得多, 因为 RR 是两腿 IV 的**符号
    敏感差值**, 一腿的坏报价就能凭空造出倒挂旗标:

    - 只认 live bid/ask mid (陈旧 lastPrice 与另一腿的实时 mid 不同源;
      crossed 报价已在 _mark 统一拒掉 — 不再有 \"live\" 的 crossed mid)
    - 相对价差上限: bid>0 and ask>0 只证明"有两个正数", 不证明 mid 可用;
      任意宽的报价照样过, 而宽报价的 mid 正是假倒挂的来源 (三轮评审)
    - 未平仓量地板: 无人持有的行权价报价不可信"""
    mid, src = _mark(row, cutoff)
    if mid is None or src != "live":
        return None
    bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
    if mid <= 0 or (ask - bid) / mid > s["rr_max_rel_spread"]:
        return None
    if _oi(row) < s["rr_min_oi"]:
        return None
    return mid


def forward_from_parity(calls, puts, T: float, cutoff, s: dict) -> float | None:
    """由 put-call parity 反解该到期日的 forward: C - P = e^(-rT)(F - K)
    → F = K + e^(rT)(C - P), 取 |C - P| 最小的行权价 (构造上离 F 最近,
    也是最活跃的那档)。

    这样 IV 与 delta 都从**期权市场自己**出发, 一次性消掉两个偏置:
    ① 零股息 BS 用 S 而非 S·e^(-qT), 会压低 call IV/抬高 put IV, 把
       RR 系统性推向"不倒挂", 可能盖掉真信号;
    ② 更要命的是外部 spot 本身可能陈旧 — 实测 yfinance 日线最新一根
       返回 NaN 时会退回前一日收盘, 用昨天的股价去反解今天的期权报价,
       spot 偏低使 call 抬高/put 压低, 恰好是 RR 倒挂的形态 (2026-09-04
       盘后 11 个标的亮了 10 个假旗标, 见 lesson.md)。"""
    if calls is None or puts is None or calls.empty or puts.empty:
        return None
    cmid, pmid = {}, {}
    for df, out in ((calls, cmid), (puts, pmid)):
        for _, row in df.iterrows():
            mid = _rr_mark(row, cutoff, s)
            if mid is not None:
                out[float(row["strike"])] = mid
    common = set(cmid) & set(pmid)
    if not common:
        return None
    k = min(common, key=lambda x: abs(cmid[x] - pmid[x]))
    return k + math.exp(RATE * T) * (cmid[k] - pmid[k])


def rr25_snapshot(cc: ChainCache, spot: float, s: dict) -> dict | None:
    """~35 DTE 的 25Δ risk reversal 快照 (call/put 各取 OTM 侧
    |Δ| 最接近 0.25 的行权价)。链/报价不可用时返回 None — 例外才
    报告, 正常 skew 不进报告。"""
    lo, hi = s["rr_dte"]
    window = [(e, d) for e, d in cc.expiries() if lo <= d <= hi]
    if not window:
        return None
    exp, dte = min(window, key=lambda x: abs(x[1] - 35))
    ch = cc.chain(exp)
    cutoff = _stale_cutoff()
    T = dte / 365.0
    # forward 拿不到就没有读数 — 绝不退回可能陈旧的外部 spot
    fwd = forward_from_parity(ch.calls, ch.puts, T, cutoff, s)
    if fwd is None:
        return None
    # 零股息 BS 里代入 S_eff = F·e^(-rT) 就等价于带股息/持有成本的定价
    # (S·e^(-qT) = F·e^(-rT)), 不必改 bs_price/bs_delta 的签名
    s_eff = fwd * math.exp(-RATE * T)
    sides = []
    for df, is_call in ((ch.calls, True), (ch.puts, False)):
        rows = []
        if df is not None and not df.empty:
            # OTM 的分界是 forward 而不是现货 — 两腿必须对称于同一个中心
            otm = df[df["strike"] > fwd] if is_call else df[df["strike"] < fwd]
            for _, row in otm.iterrows():
                mid = _rr_mark(row, cutoff, s)
                if mid is None:
                    continue
                strike = float(row["strike"])
                # RR 是两腿 IV 的**符号敏感差值** — 两腿必须同源: 一律从
                # bid/ask mid 反解, 不用 Yahoo 预算的 impliedVolatility 列
                # (实测与 mid 反解值差 +1.9~2.3 pts 且方向恰在 put-call 上,
                # 曾把 MSFT/GOOG 的正常 skew 翻成假倒挂)。contract_iv 只留
                # 给关心绝对水平的 atm_iv30。
                iv = implied_vol(mid, s_eff, strike, T, RATE, is_call)
                if iv is None:
                    continue
                rows.append({"strike": strike, "iv": iv,
                             "delta": bs_delta(s_eff, strike, T, RATE, iv,
                                               is_call)})
        sides.append(rows)
    out = rr25(sides[0], sides[1], s["rr_delta_tol"],
               s["rr_invert_min_pts"])
    if out is not None:
        # forward 反解出的现货 vs 传进来的日线收盘 — 正常只差个股息/借券,
        # 差得多就是那根日线陈旧/缺失, 意味着**整份报告**的价格与状态都不
        # 可信 (RR 本身已改用 forward, 不受影响)
        gap = (s_eff / spot - 1) * 100 if spot else None
        out.update({"exp": exp, "dte": dte, "forward": fwd,
                    "fwd_spot": s_eff, "spot_gap_pct": gap})
    return out


# --------------------------------------------------------------------------
# Tickets
# --------------------------------------------------------------------------

def daily_bar_stale(cc: ChainCache, bar_date: str) -> str | None:
    """日线是否落后于期权链的最新成交 -> 落后时给出说明, 否则 None。

    判据是**日期比对**, 不是"forward 反解现货 vs 日线收盘"的价差。价差
    判据分不开两件事: 借券费在定价上等同于股息 (F = S·e^((r-q-b)T)), 难
    借券的高借券费会让 forward 合法地低于现货好几个百分点, 拿价差硬拦会
    把那些标的永久静音, 而且给出的理由还是错的。日期不一样 —— 借券费再
    贵也不会让"日线停在哪天"和"期权链最新成交在哪天"对不上 (三轮评审)。

    单向判断: 只有期权**比日线新**才算陈旧。反过来 (期权好几天没成交)
    是流动性问题, 不是数据问题, 不报。

    到期日取**最近的**, 不借用 rr_dte 的 20-60 DTE 窗口: 借用的话, 没有
    到期日落在窗口内的标的会让整道门静默失效, 而它照样能出 CSP/LEAP 票 —
    那些票就建立在过期价格上 (四轮评审)。最近的一档没有成交记录时往后
    再试两档就停, 免得为一道校验多拉一堆链。"""
    chain_date = None
    for exp, _dte in cc.expiries()[:3]:
        dates = []
        ch = cc.chain(exp)
        for df in (ch.calls, ch.puts):
            if df is None or df.empty or "lastTradeDate" not in df:
                continue
            ts = pd.to_datetime(df["lastTradeDate"], errors="coerce",
                                utc=True).max()
            if pd.notna(ts):
                dates.append(ts.tz_convert(ET).date())
        if dates:
            chain_date = max(dates)
            break
    if chain_date is None:
        return None
    if str(chain_date) <= bar_date:
        return None
    # 短句: 这条会按标的 (乃至票种) 重复, 长解释只放在"今日动作"的合并行
    # 里一次 — 和 regime halt 同一个教训 (三轮评审 :1640)
    return (f"日线陈旧: 停在 {bar_date}, 而期权链已有 {chain_date} 的成交 "
            "— 停出票直到日线补齐")


def blocked_ticket(stale_msg: str | None, regime_msg: str | None) -> dict:
    """被拦票的统一构造。

    陈旧数据优先于市场门 — 价格都不可信时, 市场门拦没拦这一票已无意义。
    且陈旧是**每标的**问题: 不打 regime_halt 标记, 免得被 action_block
    合并进"全市场"那一行 (那行专给市场级硬停牌去重)。"""
    if stale_msg:
        return {"skip_reason": stale_msg, "stale_data": True}
    return {"skip_reason": regime_msg, "regime_halt": True}


def _put_candidates(cc: ChainCache, exp: str, dte: int, spot: float):
    ch = cc.chain(exp)
    puts = ch.puts
    if puts is None or puts.empty:
        return []
    cutoff = _stale_cutoff()
    out = []
    T = dte / 365.0
    for _, row in puts[puts["strike"] < spot].iterrows():
        mid, src = _mark(row, cutoff)
        if mid is None:
            continue
        strike = float(row["strike"])
        iv = contract_iv(row, mid, spot, T, is_call=False)
        if iv is None:
            continue
        delta = abs(bs_delta(spot, strike, T, RATE, iv, is_call=False))
        bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
        out.append({
            "exp": exp, "dte": dte, "strike": strike, "mid": mid, "src": src,
            "iv": iv, "delta": delta,
            "oi": _oi(row),
            # crossed 行经 lastPrice 路径进来时 bid/ask 仍是脏的 — 价差
            # 只对健康盘口有意义 (负价差曾冒充"干净"通过过滤)
            "spread_pct": (ask - bid) / mid * 100 if 0 < bid <= ask else None,
        })
    return out


def _finish_csp(c: dict, spot: float, s: dict, zone, panic: bool,
                extra_notes: list[str]) -> dict:
    c["annualized_pct"] = csp_annualized(c["mid"], c["strike"], c["dte"])
    c["cushion_pct"] = (spot - c["strike"]) / spot * 100
    c["breakeven"] = c["strike"] - c["mid"]
    c["panic_mode"] = panic
    notes = list(extra_notes)
    if c["mid"] < s["csp_min_mid"]:
        notes.append(f"权利金 {c['mid']:.2f} < {s['csp_min_mid']:.2f} — 手续费占比过高, 仅供参考")
    if c["oi"] < s["csp_min_oi"]:
        notes.append(f"OI {c['oi']} < {s['csp_min_oi']} — 流动性弱")
    if c["spread_pct"] is not None and c["spread_pct"] > 10:
        notes.append(f"价差 {c['spread_pct']:.0f}% mid — 挂 mid 磨, 别市价")
    if c["src"] == "last":
        notes.append("盘口不可用, 按最近成交价估算 — 下单前实查")
    if zone is not None and c["strike"] > zone[1]:
        notes.append(f"行权价高于价值区上沿 {zone[1]:g} — 被行权成本不在接货区, 可下移到 <= {zone[1]:g}")
    c["notes"] = notes
    return c


def stage2_leap_gate(price_ok: bool, prev_leap_window, ep_end: str) -> bool:
    """阶段2 LEAP 决策 (纯函数): 价格条件满足且本窗口还没出过真票。

    每窗口一次的 leap_window dedup key 由**调用方在真票发出后**才烧 —
    任何形式的没出成票都不烧:
    - halt (VX 全曲线倒挂等硬门) / 日线陈旧: 暂态市场条件, 且 VX 结算
      滞后一个交易日, 解除窗第一天常读到恐慌尾巴的旧曲线;
    - 票据级临时 skip (财报缓冲/无可用到期/无报价): 之前在门口就烧 key,
      leap_ticket 一句\"财报 5 天后\"就让整个 10 天解除窗的补发静默丢失
      — NORMAL 路径的 leap_pending 补偿明确 gate 在 stage==NORMAL, 从
      不护这里, 而阶段2恰是全剧本统计最强的入场窗 (五轮评审)。
    代价是永久性 skip (标的没有 LEAP) 在窗口内每天重复一条 ⏸ 行 —
    与 NORMAL 路径对非 emitted 票的现状一致, 可见的重复好过静默丢失。"""
    return price_ok and prev_leap_window != ep_end


def normal_leap_gate(fresh_confirm: bool, prev_leap_pending: bool,
                     state: str) -> bool:
    """NORMAL 期 LEAP 决策 (纯函数)。

    fresh_confirm 是一次性转换, 硬停牌当天会被 state.json 无条件消耗 —
    leap_pending 把被拦的确认带到解除后补发。补发前必须复核**当日**右侧
    状态: 标的在 halt 期间跌破 20 日线时 next_state 已经给出 PULLBACK,
    而 state.json 里的失效要等本次扫描之后才写 — 不复核就会在止损出局
    当天补一张新多头票 (三轮评审)。"""
    return fresh_confirm or (prev_leap_pending
                             and state in ("CONFIRMED", "TREND"))


def retest_gate(state: str, touched_20dma: bool, prev_retested: bool,
                prev_retest_pending: bool, stage: str) -> bool:
    """首次回踩提示决策 (纯函数)。

    touched_20dma 是**当日**事件: 只看它的话, 价格在 halt 期间离开 20 日
    线之后这轮回踩就再也发不出来, 承诺的"解除后再提示"落空 — 被拦当日落
    retest_pending, 跨日沿用, 解除后补发 (三轮评审)。STAGE1 不提示 (纪律:
    倒挂持续期间不加右侧仓), 且每轮确认只提示一次。"""
    return state == "TREND" and not stage.startswith("STAGE1") \
        and not prev_retested and (touched_20dma or prev_retest_pending)


def csp_window_open(zone, in_or_near_zone: bool, stage: str) -> bool:
    """CSP 出票窗口: 有接货价, 且 (价格在/近区 或 恐慌档 或 阶段2解除窗口)。

    阶段2 加入依据 (2026-09-02 研究, options.cafe 2009 年以来 43 次倒挂
    事件): 解除日买入 SPX 前瞻 5日 +3.04%/胜率88%, 21日 +4.38%/91%,
    63日 +6.93%/88%, 每个周期都碾压基线 (+0.26%/60%, +1.07%/68%,
    +3.10%/75%) — 解除窗口是全数据里胜率最高的卖权入场窗, 且 IV 尚未
    塌完时权利金最肥。倒挂开始日反而无短期边际 (5日 -0.15%/51%,
    74% 的 episode 期间继续跌) — 所以加成给解除, 不给开始。"""
    return zone is not None and (
        in_or_near_zone or stage.startswith("STAGE1")
        or stage == "STAGE2_WINDOW")


def csp_ticket(cc: ChainCache, spot: float, iv30: float | None,
               earnings_iso: str, stage: str, zone, s: dict) -> dict | None:
    """One cash-secured-put suggestion. Normal: 12-31 DTE, delta 0.10-0.15.
    Panic (stage 1): weekly, strike at the 16-rule distance. Expiries that
    contain an earnings date are excluded outright (short 不跨财报)."""
    panic = stage.startswith("STAGE1")
    lo, hi = s["csp_dte_panic"] if panic else s["csp_dte_normal"]
    window = [(e, d) for e, d in cc.expiries() if lo <= d <= hi]
    blocked = []
    if earnings_iso:
        blocked = [e for e, _d in window if earnings_iso <= e]
        window = [(e, d) for e, d in window if earnings_iso > e]
    if not window:
        if blocked:
            return {"skip_reason": f"窗口内到期日都在财报 {earnings_iso} 之后 — 跳过 (short 不跨财报)"}
        return {"skip_reason": f"无 {lo}-{hi} DTE 到期日"}
    notes = []
    if earnings_iso is None:
        notes.append("财报日期获取失败 — 下单前自查该到期日是否跨财报")
    if blocked:
        notes.append(f"财报 {earnings_iso}: 已剔除跨财报到期日 {', '.join(blocked)}")

    target_dte = 7 if panic else 21
    exp, dte = min(window, key=lambda x: abs(x[1] - target_dte))
    cands = _put_candidates(cc, exp, dte, spot)
    if not cands:
        return {"skip_reason": f"{exp} put 链无可用报价 (市场关闭/流动性)"}

    # 16法则参考 IV 用截断前的完整链 — zone 截断后只剩深 OTM, put skew 会
    # 把参考 IV 系统性抬高 (spot 远在接货带上方时 near_atm 取空, 距离虚增)
    full_chain = cands

    # 行权价 = 愿意接货的价位, 是硬约束不是警告 — 接货带上沿以上的 put
    # 不在考虑范围 (剧本 ORCL 教训)
    if zone is not None:
        capped = [c for c in cands if c["strike"] <= zone[1]]
        if not capped:
            return {"skip_reason": f"{exp} 接货带上沿 {zone[1]:g} 以下无可用行权价"}
        cands = capped

    if panic:
        # 纪律: 距离以所卖周权链上自身的 IV 为准 — 倒挂期 30 天口径系统性
        # 低估周权 IV, 会把行权价放得太近 (剧本: IVR/IVP 30天口径会骗人)
        near_atm = [c["iv"] for c in full_chain if c["strike"] >= spot * 0.9]
        weekly_iv = float(np.median(near_atm or [c["iv"] for c in full_chain]))
        ref_iv = max(iv30 or 0.0, weekly_iv)
        dist = sixteen_rule_distance(ref_iv, dte, s["sixteen_rule_mult"])
        max_strike = spot * (1 - dist)
        ok = [c for c in cands if c["strike"] <= max_strike]
        if not ok:
            return {"skip_reason": f"16法则距离 {dist * 100:.1f}% 外无可用行权价"}
        pick = max(ok, key=lambda c: c["strike"])
        notes.append(f"恐慌模式: 16法则距离 {dist * 100:.1f}% (mult {s['sixteen_rule_mult']}, iv {ref_iv * 100:.0f}%)")
    else:
        band = [c for c in cands
                if s["csp_delta_lo"] <= c["delta"] <= s["csp_delta_hi"]]
        pool = band or cands
        # 年化下限先筛后选: 年化随 delta 单调升, 0.12 目标常差一档落在线下 —
        # 带内有达标行权价时不该整票跳过
        ok = [c for c in pool
              if csp_annualized(c["mid"], c["strike"], c["dte"])
              >= s["csp_min_annualized"] and c["mid"] >= s["csp_min_mid"]]
        pool = ok or pool
        liquid = [c for c in pool if c["oi"] >= s["csp_min_oi"]]
        pick = min(liquid or pool,
                   key=lambda c: abs(c["delta"] - s["csp_delta_target"]))
        if not band:
            notes.append(f"目标 delta 带 {s['csp_delta_lo']:.2f}-"
                         f"{s['csp_delta_hi']:.2f} 内无行权价 — 取最接近的 "
                         f"(delta {pick['delta']:.2f})")
    ticket = _finish_csp(dict(pick), spot, s, zone, panic, notes)
    # 年化下限: IV 低/离接货价远 => 权利金太薄, 卖方三需求不齐, 不出票。
    # 恐慌档同样适用 — STAGE1 是市场级旗标, 浅倒挂里低 IV 标的的周权照样薄;
    # 真恐慌时周权 IV 全曲线最肥 (剧本), 年化天然过线, 此门不拦
    if (ticket["annualized_pct"] < s["csp_min_annualized"]
            or ticket["mid"] < s["csp_min_mid"]):
        return {"skip_reason": (
            f"接货档权利金太薄: {exp} {ticket['strike']:g}P @ ~{ticket['mid']:.2f} "
            f"年化仅 {ticket['annualized_pct']:.1f}% (< {s['csp_min_annualized']:g}%) — "
            "IV 低/距离远, 剧本: 左侧改正股限价单或等 IV 回升再卖")}
    return ticket


def leap_ticket(cc: ChainCache, spot: float, cfg: dict,
                earnings_iso: str | None, s: dict) -> dict | None:
    """Deep-ITM LEAP call per the playbook: 450-1100 DTE (Jan cycle
    preferred), delta 0.70-0.80 index / 0.75-0.85 single names, OI >= 500,
    spread <= 5% of mid, extrinsic <= 40% of premium. Earnings inside the
    buffer is a hard gate (剧本: 默认财报后入场), not a footnote."""
    days_to_earnings = None
    if earnings_iso:
        days_to_earnings = (date.fromisoformat(earnings_iso)
                            - datetime.now(ET).date()).days
        if 0 <= days_to_earnings <= s["leap_earnings_buffer_days"]:
            return {"skip_reason": (
                f"财报 {earnings_iso} 就在 {days_to_earnings} 天后 — 财报前 <="
                f"{s['leap_earnings_buffer_days']} 天不进 LEAP; "
                "crush 落地即解禁 (通常隔天), 不是再等两周")}
    lo, hi = s["leap_dte"]
    cands_exp = [(e, d) for e, d in cc.expiries() if lo <= d <= hi]
    if not cands_exp:
        return {"skip_reason": f"无 {lo}-{hi} DTE 到期日 (标的可能没有 LEAP)"}
    jan = [(e, d) for e, d in cands_exp if e[5:7] == "01"]
    pool = jan or cands_exp
    exp, dte = min(pool, key=lambda x: abs(x[1] - 730))

    dlo, dhi = (s["leap_delta_index"] if cfg["kind"] == "index"
                else s["leap_delta_stock"])
    target = (dlo + dhi) / 2
    ch = cc.chain(exp)
    calls = ch.calls
    if calls is None or calls.empty:
        return {"skip_reason": f"{exp} call 链为空"}
    cutoff = _stale_cutoff()
    T = dte / 365.0
    rows = []
    for _, row in calls[calls["strike"] < spot].iterrows():
        mid, src = _mark(row, cutoff)
        if mid is None:
            continue
        strike = float(row["strike"])
        iv = contract_iv(row, mid, spot, T, is_call=True)
        if iv is None:
            continue
        delta = bs_delta(spot, strike, T, RATE, iv, is_call=True)
        intrinsic = max(spot - strike, 0.0)
        extrinsic = max(mid - intrinsic, 0.0)
        bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
        rows.append({
            "exp": exp, "dte": dte, "strike": strike, "mid": mid, "src": src,
            "iv": iv, "delta": delta,
            "oi": _oi(row),
            # 同 _put_candidates: 价差只对健康盘口有意义
            "spread_pct": (ask - bid) / mid * 100 if 0 < bid <= ask else None,
            "extrinsic_pct": extrinsic / mid * 100 if mid > 0 else None,
            "lam": spot * delta / mid if mid > 0 else None,
            "breakeven": strike + mid,
            "insurance_pct_yr": extrinsic / spot / (dte / 365.0) * 100,
        })
    if not rows:
        return {"skip_reason": f"{exp} 无可用 ITM call 报价"}

    def passes(c):
        return (dlo <= c["delta"] <= dhi and c["oi"] >= s["leap_min_oi"]
                and (c["extrinsic_pct"] is None
                     or c["extrinsic_pct"] <= s["leap_max_extrinsic_pct"])
                and (c["spread_pct"] is None
                     or c["spread_pct"] <= s["leap_max_spread_pct"]))

    clean = [c for c in rows if passes(c)]
    pick = min(clean or rows, key=lambda c: abs(c["delta"] - target))
    notes = []
    if not clean:
        notes.append("无合约同时满足 delta带/OI/价差/外在价值全部过滤 — 取最接近目标 delta 的, 自查旗标")
    if pick["oi"] < s["leap_min_oi"]:
        notes.append(f"OI {pick['oi']} < {s['leap_min_oi']}")
    if pick["spread_pct"] is not None and pick["spread_pct"] > s["leap_max_spread_pct"]:
        notes.append(f"价差 {pick['spread_pct']:.1f}% > {s['leap_max_spread_pct']}% — 挂 mid 磨或换行权价")
    if pick["extrinsic_pct"] is not None and pick["extrinsic_pct"] > s["leap_max_extrinsic_pct"]:
        notes.append(f"外在价值 {pick['extrinsic_pct']:.0f}% > {s['leap_max_extrinsic_pct']}% — 深度不够")
    if pick["src"] == "last":
        notes.append("盘口不可用, 按最近成交价估算 — 下单前实查")
    if days_to_earnings is not None and s["leap_earnings_buffer_days"] \
            < days_to_earnings <= s["leap_earnings_note_days"]:
        notes.append(f"财报 {earnings_iso} 在 {days_to_earnings} 天后, 持有期内 — "
                     "想完全避事件可等 crush 后进")
    if earnings_iso is None:
        notes.append("财报日期获取失败 — 自查 2 周内无二元事件 (财报/发布会/FDA)")
    if cfg["high_beta"]:
        notes.append("高 beta 个股 — 剧本: 仓位折半 + 财报后入场")
    notes.append("仓位: 单次事件权利金 <= 组合 3-5%; 剩 6-9 个月 roll; delta>0.9 roll up 提现")
    pick = dict(pick)
    pick["notes"] = notes
    return pick


def stock_ladder(zone, s: dict) -> list[float]:
    """正股分批档位: ① 接货带上沿 ② 下沿 ③ 恐慌档 (下沿再打折) —
    末档留给真正的恐慌价; 间距是否递增取决于区间宽度, render 侧核对."""
    lo, hi = zone
    return [hi, lo, round(lo * (1 - s["ladder_panic_discount"]), 2)]


def call_spread_ticket(cc: ChainCache, spot: float, s: dict) -> dict:
    """突破后首次回踩的 3-6 个月 call spread (剧本工具切换表):
    买 ~0.60 delta / 卖 ~0.30 delta 同到期."""
    lo, hi = s["spread_dte"]
    exps = [(e, d) for e, d in cc.expiries() if lo <= d <= hi]
    if not exps:
        return {"skip_reason": f"无 {lo}-{hi} DTE 到期日"}
    exp, dte = min(exps, key=lambda x: abs(x[1] - 135))
    ch = cc.chain(exp)
    calls = ch.calls
    if calls is None or calls.empty:
        return {"skip_reason": f"{exp} call 链为空"}
    cutoff = _stale_cutoff()
    T = dte / 365.0
    rows = []
    for _, row in calls.iterrows():
        mid, src = _mark(row, cutoff)
        if mid is None:
            continue
        strike = float(row["strike"])
        iv = contract_iv(row, mid, spot, T, is_call=True)
        if iv is None:
            continue
        rows.append({"strike": strike, "mid": mid, "src": src, "iv": iv,
                     "delta": bs_delta(spot, strike, T, RATE, iv, True),
                     "oi": _oi(row)})
    if len(rows) < 2:
        return {"skip_reason": f"{exp} 可用报价不足"}
    long_leg = min(rows, key=lambda c: abs(c["delta"] - s["spread_long_delta"]))
    shorts = [c for c in rows if c["strike"] > long_leg["strike"]]
    if not shorts:
        return {"skip_reason": "长腿上方无行权价"}
    short_leg = min(shorts, key=lambda c: abs(c["delta"] - s["spread_short_delta"]))
    debit = long_leg["mid"] - short_leg["mid"]
    width = short_leg["strike"] - long_leg["strike"]
    if debit <= 0 or width <= 0:
        return {"skip_reason": "价差腿报价异常 (debit <= 0)"}
    stale = "last" in (long_leg["src"], short_leg["src"])
    stale_hint = "有腿无盘口按旧成交价估算, " if stale else ""
    # 旧成交价 mark 会把 debit 顶到甚至超过宽度 — 数学上必亏/赔率失真, 不出票
    if debit >= width:
        return {"skip_reason": (f"净支出 {debit:.2f} >= 宽度 {width:g}, "
                                f"最大盈利为负 — {stale_hint}报价失真")}
    rr = (width - debit) / debit
    if rr < s["spread_min_reward_risk"]:
        return {"skip_reason": (
            f"赔率 {rr:.2f}:1 < {s['spread_min_reward_risk']:g}:1 — "
            f"{stale_hint}报价可疑, 下单前实查盘口再手动构造")}
    notes = []
    if abs(long_leg["delta"] - s["spread_long_delta"]) > 0.10 \
            or abs(short_leg["delta"] - s["spread_short_delta"]) > 0.10:
        notes.append(
            f"腿 delta {long_leg['delta']:.2f}/{short_leg['delta']:.2f} 偏离目标 "
            f"{s['spread_long_delta']:.2f}/{s['spread_short_delta']:.2f} — "
            "链稀疏, 结构已变形, 下单前自查")
    if min(long_leg["oi"], short_leg["oi"]) < s["csp_min_oi"]:
        notes.append(f"OI {long_leg['oi']}/{short_leg['oi']} < "
                     f"{s['csp_min_oi']} — 流动性弱, 挂 mid 磨")
    return {
        "exp": exp, "dte": dte,
        "long_strike": long_leg["strike"], "long_mid": long_leg["mid"],
        "long_delta": long_leg["delta"], "long_oi": long_leg["oi"],
        "short_strike": short_leg["strike"], "short_mid": short_leg["mid"],
        "short_delta": short_leg["delta"], "short_oi": short_leg["oi"],
        "debit": debit, "width": width, "max_profit": width - debit,
        "reward_risk": rr,
        "breakeven": long_leg["strike"] + debit,
        "notes": notes,
        "src": "last" if stale else "live",
    }


def next_earnings(tk) -> str | None:
    """Next earnings date as ISO string; '' = fetched fine, none upcoming;
    None = lookup FAILED — callers must warn, not treat as no-earnings.
    yfinance swallows HTTP errors and hands back calendar == {}, so an
    empty/non-dict calendar counts as failed (ETFs legitimately 404 — the
    caller downgrades None to '' for kind etf/index).

    "今天"必须是 ET 日历日, 不是本机的 — 布里斯班机器上跑 close 扫描
    (15:45 ET = 本地次日 05:45/06:45) 时 date.today() 已经是 ET+1,
    会把**当天 AMC 财报**当过去过滤掉: earnings_iso 变成下季/空,
    csp_ticket 的"short 不跨财报"剔除失效, 在财报公布前几小时放行
    跨财报的 CSP 票 (五轮评审; 全文件唯一一处非 ET 时钟)。UTC 主机上
    扫描窗内两个日历日恰好相等, 所以 droplet 上潜伏未爆。"""
    try:
        cal = tk.calendar
        if not isinstance(cal, dict) or not cal:
            return None
        dates = cal.get("Earnings Date")
        today_et = datetime.now(ET).date()
        future = [d for d in dates or [] if d >= today_et]
        return min(future).isoformat() if future else ""
    except Exception:
        return None


# --------------------------------------------------------------------------
# Per-ticker analysis
# --------------------------------------------------------------------------

def technical_snapshot(hist: pd.DataFrame, s: dict) -> dict | None:
    """Levels + signals from daily OHLCV (today's bar may be partial).

    新上市标的降级模式: 25-60 根日线时价格/20日线/量比/价值区照算,
    右侧确认自动关闭 (confirmation 内部的长度门各自兜底 — had_pullback
    需要 61 根, 不满足时 confirmed 恒为 False)。<25 根才整体放弃。"""
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 25:
        return None
    close, high, low, vol = hist["Close"], hist["High"], hist["Low"], hist["Volume"]
    last = float(close.iloc[-1])
    prev = float(close.iloc[-2])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None
    vol20 = float(vol.rolling(20).mean().shift(1).iloc[-1])
    vol_ratio = float(vol.iloc[-1]) / vol20 if vol20 > 0 else 0.0
    sig = confirmation(close, high, low, vol_ratio, s["volume_surge"])
    hi52 = float(close.iloc[-252:].max())
    low_today = float(low.iloc[-1])
    return {
        "bars": len(hist),
        # 最后一根**有效**日线的日期 (dropna 之后取, NaN 行不会冒充"今天的
        # 价") — 与期权链的 lastTradeDate 比对即可直接证明日线陈旧
        "as_of": str(hist.index[-1].date()),
        # 当日下探过20日线但收盘守住 — 回踩提示的原料
        "touched_20dma": low_today <= sma20 <= last,
        "low_today": low_today,
        "close": last, "prev_close": prev,
        "change_pct": (last / prev - 1) * 100,
        "gap_pct": (float(hist["Open"].iloc[-1]) / prev - 1) * 100,
        "sma20": sma20, "sma200": sma200,
        "vs_sma20_pct": (last / sma20 - 1) * 100,
        "vs_sma200_pct": (last / sma200 - 1) * 100 if sma200 else None,
        "vol_ratio": vol_ratio,
        "from_52w_high_pct": (last / hi52 - 1) * 100,
        "signals": sig,
        "rv30": yang_zhang(hist) if len(hist) >= 40 else None,
    }


def analyze_ticker(sym: str, cfg: dict, hist: pd.DataFrame | None,
                   prev_state: dict, regime: dict, s: dict, mode: str,
                   fetch_options: bool) -> dict:
    r = {"symbol": sym, "cfg": cfg, "state": "NO_DATA", "notes": [],
         "tech": None, "earnings": "", "iv30": None, "self_ivp": None,
         "csp": None, "leap": None, "spread": None, "ladder": None,
         "rr25": None, "error": None}
    try:
        tech = technical_snapshot(hist, s) if hist is not None else None
        if tech is None:
            r["error"] = "insufficient price history"
            return r
        r["tech"] = tech
        zone = cfg["value_zone"]
        prev = prev_state.get("state", "UPTREND")
        state, notes = next_state(
            prev, close=tech["close"], sma20=tech["sma20"],
            confirmed=tech["signals"]["confirmed"], zone=zone,
            near_pct=s["near_zone_pct"])
        r["state"], r["prev_state"] = state, prev
        r["notes"].extend(notes)
        if tech["bars"] < 61:
            r["notes"].append(
                f"数据仅 {tech['bars']} 个交易日 (Yahoo 起点晚/新上市) — "
                "右侧确认需 61 根日线, 当前只看价格/20日线/价值区")
        if zone is not None:
            r["zone_dist_pct"] = (tech["close"] / zone[1] - 1) * 100

        if not fetch_options:
            return r

        cc = ChainCache(sym)
        # 日线陈旧 = 本标的的价格/状态/票据全部建立在过期价格上 → 硬拦
        # (⏸), 不是提示。判据见 daily_bar_stale 的 docstring
        stale_msg = daily_bar_stale(cc, tech["as_of"]) \
            if cfg["options"] else None
        if stale_msg:
            # 硬拦只在"本来就要出票"时才看得见 — 而多数标的当天并不出票,
            # 那时陈旧会完全静默, 而概览表照样印着过期的收盘价和据此判定
            # 的右侧状态。陈旧本身就是结论, 必须无条件出声
            r["stale_data"] = True
            r["notes"].append(f"⛔ {stale_msg}")

        # 首次回踩 (收盘口径, 每轮确认只提示一次): 剧本首选入场/加仓点
        # 倒挂期不提示也不烧一次性标记 (纪律: 阶段1不加右侧仓) — 解除后的
        # 首次回踩才算这轮的"首次"
        retest_seen = retest_gate(
            state, tech["touched_20dma"], bool(prev_state.get("retested")),
            bool(prev_state.get("retest_pending")), regime["stage"])
        if retest_seen and (stale_msg or regime.get("halt_new_longs")):
            r["retest_pending"] = True
            # 呈现被拦的回踩 (⏸ skip_reason) 但不烧一次性 retested 标记 —
            # 恢复后同一轮回踩仍可提示, 不是静默消失
            if cfg["options"]:
                r["spread"] = blocked_ticket(stale_msg,
                                             regime.get("halt_new_longs"))
            else:
                r["notes"].append(
                    "首次回踩20日线不破, 但"
                    + ("日线数据陈旧" if stale_msg else "硬停牌中 (见市场状态 ⛔)")
                    + " — 恢复后再按股价操作")
        elif retest_seen:
            r["retest"] = True
            how = "票见下" if cfg["options"] else "无期权链 — 按股价操作"
            r["notes"].append("首次回踩20日线不破 — 剧本首选入场/加仓点 "
                              f"(3-6个月 call spread, {how})")

        # 趋势中段工具切换 (剧本: 趋势确立、**波动收敛**后 -> 2x/PMCC 加仓)
        # 波动收敛门: 环境止损 (VIX持续>25 / 破200日线) 成立时不得建议进场
        vol_contracted = regime["vix"] < s["two_x_vix_max"] and (
            tech["sma200"] is None or tech["close"] > tech["sma200"])
        if state == "TREND" and regime["stage"] == "NORMAL" \
                and vol_contracted and prev_state.get("since"):
            days_in = (datetime.now(ET).date()
                       - date.fromisoformat(prev_state["since"])).days
            if days_in >= s["trend_middle_days"]:
                if regime.get("halt_new_longs"):
                    # PMCC/2x 都是开新多头 — 硬停牌期间不给可执行建议
                    r["notes"].append(
                        f"趋势中段条件已满足 (右侧已 {days_in} 天) 但硬停牌中 "
                        "(见市场状态 ⛔) — 解除后再切换 2x/PMCC")
                else:
                    two_x = (f"2x ETF {cfg['two_x']} / "
                             if cfg.get("two_x") else "")
                    r["notes"].append(
                        f"趋势中段 (右侧已 {days_in} 天): 剧本工具切换 → {two_x}"
                        "PMCC 金字塔加仓 — 绝不摊低成本; 硬止损: 破20日线无条件; "
                        "环境止损: 指数破200日线 / VIX 持续>25 / 倒挂重现即清")

        # 正股分批档位只要 zone, 不依赖期权链 — options=false 的标的也要出
        in_or_near_zone = zone is not None \
            and tech["close"] <= zone[1] * (1 + s["near_zone_pct"] / 100)
        if in_or_near_zone:
            r["ladder"] = stock_ladder(zone, s)

        r["earnings"] = next_earnings(cc.tk)
        if r["earnings"] is None and cfg["kind"] in ("etf", "index"):
            r["earnings"] = ""  # ETF/指数无财报 — 404 是常态不是失败
        if not cfg["options"]:
            return r
        try:
            r["iv30"] = atm_iv30(cc, tech["close"])
        except Exception as e:
            r["notes"].append(f"iv30 获取失败: {type(e).__name__}")
        # 25Δ RR 倒挂 = 每标的 froth 旗标 — 例外才报告 (正常 skew 沉默);
        # 获取失败也沉默 (纯提示信号, 不值得占报告版面)
        try:
            r["rr25"] = rr25_snapshot(cc, tech["close"], s)
        except Exception:
            pass
        rr = r["rr25"]
        gap = (rr or {}).get("spot_gap_pct")
        # 日线**不**陈旧 (日期对得上) 却仍有大幅 forward/现货背离 = 持有成本
        # 异常, 最常见的原因是难借券的高借券费 (F = S·e^((r-q-b)T))。这不是
        # 数据问题而是信息: 借券贵 = 做空拥挤, 且卖 put 的净收益会被这块成本
        # 侵蚀。陈旧那条路已经在上面硬拦掉了, 到这里的都是真背离
        if stale_msg is None and gap is not None \
                and abs(gap) >= s["rr_spot_gap_warn"] * 100:
            r["notes"].append(
                f"期权 forward 反解现货 ~{rr['fwd_spot']:.2f} vs 日线收盘 "
                f"{tech['close']:.2f} ({gap:+.1f}%), 而日线并不陈旧 — 持有成本"
                "异常, 常见于难借券的高借券费: 做空拥挤的信号, 且卖 put 的净"
                "收益会被借券成本侵蚀, 下单前核对券商的借券费率")
        if rr and rr["inverted"]:
            r["notes"].append(
                f"⚠️ 25Δ risk reversal 倒挂 ({rr['exp']}: "
                f"{rr['call_strike']:g}C IV {rr['call_iv'] * 100:.1f}% > "
                f"{rr['put_strike']:g}P IV {rr['put_iv'] * 100:.1f}%, "
                f"RR {rr['rr']:+.1f} pts) — 上涨追逐挤压 (2021 meme 形态): "
                "CSP 对下行风险结构性少收钱 (行权价放更远或跳过); "
                "OTM/ATM LEAP 在付倒挂税, 只用 deep ITM/正股; "
                "covered call/PMCC 短腿溢价异常肥 — 只 covered 不裸卖")

        stage = regime["stage"]
        # CSP = 在愿意接货的价位卖 put — 没设价值区就没有接货价, 不出票
        # (剧本 ORCL 教训: 不想接货的 put 本来就不该卖)。与状态标签解耦:
        # 价格在/近价值区就出, 趋势上方也一样 — 接货限价单与趋势方向无关。
        want_csp = csp_window_open(zone, in_or_near_zone, stage)
        if zone is None and stage.startswith("STAGE1"):
            r["notes"].append("倒挂期但未设价值区 — 剧本: 不想接货的 put 不该卖; "
                              "在 watchlist.toml 设好 value_zone 才出 CSP 票")
        if stage == "STAGE2_WINDOW":
            if want_csp and not in_or_near_zone:
                r["notes"].append(
                    "阶段2解除窗口 = 统计最强卖权入场窗 (解除日起 SPX 5日 "
                    "+3.04%/88%, 21日 +4.38%/91% — options.cafe 2009-2025 "
                    "43 次事件): 价格虽在接货带上方仍试出 CSP 常规档, "
                    "行权价仍卡接货带上沿, 年化不过线自然拦")
            elif zone is None:
                r["notes"].append(
                    "阶段2解除窗口 (统计最强卖权窗: 解除日起 5日 +3.04%/88%) "
                    "但未设价值区 — 设好 value_zone 才出 CSP 票")

        fresh_confirm = state == "CONFIRMED" and prev not in ("CONFIRMED", "TREND")
        if stage == "STAGE2_WINDOW":
            # 阶段2: 倒挂解除 + 价格确认二选一 (收上20日线 / 不再新低, 无量能
            # 条件) → LEAP。历史典型序列是价格先确认、倒挂后解除, 所以不能
            # 依赖 CONFIRMED 的一次性转换 — 以 episode 结束日为 key, 每个
            # 解除窗口重挂一次。
            ep_end = str(regime["last_episode"]["end"].date())
            price_ok = tech["close"] > tech["sma20"] \
                or tech["signals"]["no_new_low"]
            # dedup key 在下方真票发出后才烧 (见 stage2_leap_gate docstring):
            # halt/陈旧/票据级临时 skip 都不烧, 否则约束解除后整个 episode
            # 的 LEAP 补发窗静默丢失
            want_leap = stage2_leap_gate(
                price_ok, prev_state.get("leap_window"), ep_end)
            if want_leap and not (stale_msg or regime.get("halt_new_longs")):
                r["notes"].append("阶段2解除窗口 — buy the relief: 价格条件"
                                  "(收上20日线/不再新低 二选一)已满足; 工具: "
                                  "deep ITM LEAP / risk reversal (卖put融资买call)")
        else:
            # fresh_confirm 是一次性转换, 硬停牌那天会被 state.json 无条件
            # 消耗 — leap_pending 把被拦的确认带到 halt 解除后补发
            want_leap = stage == "NORMAL" and normal_leap_gate(
                fresh_confirm, bool(prev_state.get("leap_pending")), state)
        if fresh_confirm and stage.startswith("STAGE1"):
            r["notes"].append("右侧信号出现但倒挂未解除 — 剧本: 倒挂持续期间不加右侧仓, 等阶段2")
        vvix_now = (regime.get("vvix") or {}).get("value")
        if want_leap and vvix_now is not None and vvix_now >= s["vvix_halt"]:
            r["notes"].append(
                f"VVIX {vvix_now:.0f} >= {s['vvix_halt']:g} — vega 贵: LEAP "
                "只要 deep ITM 低外在档 (票内过滤器已管), 或等 VVIX 回落; "
                "<90 才是囤凸性的窗口")

        # 倒挂门控矩阵的硬门: 拦截以 skip_reason 呈现, 原因随票可见 —
        # 不是静默消失 (被拦的票在报告里显示 ⏸ + 原因)。regime_halt 标记
        # 让 action_block 把全市场硬停牌合并成一行, 免得每票重复长文
        if want_csp:
            if stale_msg or regime.get("halt_csp"):
                r["csp"] = blocked_ticket(stale_msg, regime.get("halt_csp"))
            else:
                r["csp"] = csp_ticket(cc, tech["close"], r["iv30"],
                                      r["earnings"], stage, zone, s)
        if want_leap:
            if stale_msg or regime.get("halt_new_longs"):
                r["leap"] = blocked_ticket(stale_msg,
                                           regime.get("halt_new_longs"))
                if stage == "NORMAL":
                    r["leap_pending"] = True
            else:
                r["leap"] = leap_ticket(cc, tech["close"], cfg,
                                        r["earnings"], s)
                # 只有**真票**才算消耗 pending — 财报缓冲期内/无可用合约都
                # 是临时约束, 清掉标记就等于约束解除后不再补发, 与"显式消耗"
                # 的生命周期自相矛盾 (三轮评审)。仍由"状态离开 CONFIRMED/
                # TREND"兜底失效, 不会无限挂着
                emitted = "skip_reason" not in r["leap"]
                r["leap_emitted"] = emitted
                if emitted and stage == "STAGE2_WINDOW":
                    # 真票已发 — 现在才烧每窗口一次的 dedup key。ep_end
                    # 只在 STAGE2 分支里绑定, 由 stage 判断护住
                    r["leap_window"] = ep_end
                # leap_emitted=False 只保住**已存在**的标记, 不会新建一个 —
                # 首次 NORMAL 确认当天就撞上财报缓冲/暂无合约时, 一次性的
                # fresh_confirm 会被 state.json 消耗掉而没有任何补发标记,
                # 约束解除后再也发不出来 (四轮评审)
                if not emitted and stage == "NORMAL":
                    r["leap_pending"] = True
        if r.get("retest"):  # STAGE1 已在回踩检测处拦掉
            r["spread"] = call_spread_ticket(cc, tech["close"], s)
            if "skip_reason" not in r["spread"]:
                # 剧本工具切换表: 止损放回踩低点下方
                r["spread"]["retest_low"] = tech["low_today"]
        # RR 倒挂时给已出的票追加短提示 (完整解释在上面的 notes 行)
        if rr and rr["inverted"]:
            if r["csp"] and "skip_reason" not in r["csp"]:
                r["csp"]["notes"].append(
                    "call skew 倒挂中 — 本票对下行风险结构性少收钱: "
                    "宁可行权价更远/仓位更小, 或跳过这轮")
            if r["leap"] and "skip_reason" not in r["leap"]:
                r["leap"]["notes"].append(
                    "call skew 倒挂中 — 核对外在价值占比, 只要 deep ITM 档 "
                    "(OTM/ATM 在付倒挂税)")
    except Exception as e:
        r["error"] = f"{type(e).__name__}: {e}"
    return r


# --------------------------------------------------------------------------
# IV history (self-built percentile — moomoo IVP stays authoritative)
# --------------------------------------------------------------------------

def load_iv_history() -> pd.DataFrame:
    if IV_HISTORY.exists():
        return pd.read_csv(IV_HISTORY)
    return pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])


def append_iv_history(results: list[dict], scan_date: str) -> pd.DataFrame:
    df = load_iv_history()
    rows = []
    for r in results:
        # iv30 的 ATM 档位是按 tech["close"] 选的 — 陈旧价选出的档位和据此
        # 算的 IV 一样不可信, 不能进自建 IVP 的历史样本 (四轮评审)
        if r["iv30"] is None or r.get("stale_data"):
            continue
        dup = ((df["date"] == scan_date) & (df["symbol"] == r["symbol"])).any()
        if not dup:
            rv = r["tech"]["rv30"] if r["tech"] else None
            rows.append({"date": scan_date, "symbol": r["symbol"],
                         "iv30": round(r["iv30"], 4),
                         "rv30": round(rv, 4) if rv else None})
    if rows:
        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
        DATA.mkdir(exist_ok=True)
        df.to_csv(IV_HISTORY, index=False)
    return df


def self_ivp(df: pd.DataFrame, symbol: str, iv30: float) -> float | None:
    hist = df[(df["symbol"] == symbol) & df["iv30"].notna()]["iv30"]
    if len(hist) < 60:
        return None
    return float((hist < iv30).mean() * 100)


# --------------------------------------------------------------------------
# Reports
# --------------------------------------------------------------------------

def fmt(x, spec=".2f", suffix=""):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:{spec}}{suffix}"


# 概览表/详情段排序: 可操作的在上, 观望垫底 (止损提示额外置顶)
STATE_ORDER = {"CONFIRMED": 0, "LEFT_ZONE": 1, "NEAR_ZONE": 2, "TREND": 3,
               "PULLBACK": 4, "UPTREND": 5, "NO_DATA": 6}


def _has_stop(r) -> bool:
    return any("止损触发" in n for n in r["notes"])


def by_actionability(results: list[dict]) -> list[dict]:
    return sorted(results, key=lambda r: (0 if _has_stop(r) else 1,
                                          STATE_ORDER.get(r["state"], 9)))


def action_label(r: dict, ivp) -> str:
    """概览表"操作"列的短词 — 细节永远在下方票据里, 这里只指路."""
    if r["error"] or r["tech"] is None:
        return "—"
    if _has_stop(r):
        return "⚠️止损"
    leap, csp = r["leap"], r["csp"]
    if leap and "skip_reason" not in leap:
        return "IV高·spread" if ivp is not None and ivp > 60 else "LEAP票👇"
    if leap and "skip_reason" in leap:
        return "等财报后" if "财报" in leap["skip_reason"] else "LEAP被拦"
    if any("等阶段2" in n for n in r["notes"]):
        return "等阶段2"
    if csp and "skip_reason" not in csp:
        return "CSP票👇"
    if csp and "skip_reason" in csp:
        return "CSP被拦"
    spread = r.get("spread")
    if spread and "skip_reason" not in spread:
        return "spread票👇"
    if r.get("retest"):
        return "回踩中👀"
    state = r["state"]
    if state == "TREND":
        return "持有·跟20日线"
    if state == "CONFIRMED":
        return "确认·看下文"
    if r.get("ladder") and not csp:
        return "分批档👇"  # 无期权链但在接货带内 — 正股分批是唯一工具
    zone = r["cfg"]["value_zone"]
    if zone is not None and r["tech"]["close"] > zone[1]:
        return "等回落入区"  # 设了接货带, 现价还在上方 — 等价格回来
    if state in ("LEFT_ZONE", "NEAR_ZONE", "PULLBACK") and zone is None:
        return "设区间"  # 设了才解锁左侧工具 (CSP 票 / 正股分批档)
    return "别追·等回调"  # 趋势里无入场事件: 空仓不追高, 持有继续拿


def _regime_halted(ticket) -> bool:
    return bool(ticket) and "skip_reason" in ticket and ticket.get("regime_halt")


def ticket_skip_line(label: str, ticket: dict) -> str:
    """被拦票在明细区的一行。

    全市场硬停牌的 ~150 字理由只在市场状态的 ⛔ 行出现一次 — 明细区按
    标的 x 票种 重复 (10 个标的各有 CSP+LEAP 就是 20 遍) 会把报告淹掉,
    而手机上扫一眼就能看懂正是当初做合并的初衷 (三轮评审)。"""
    if _regime_halted(ticket):
        return f"- {label}: ⏸ 市场门拦下 — 完整理由见市场状态 ⛔ 行"
    return f"- {label}: {ticket['skip_reason']}"


def action_block(results: list[dict], ivdf) -> list[str]:
    """报告最顶上的 3-6 行 — 手机上扫一眼就知道今天要不要动手.
    全市场硬停牌 (regime_halt 票) 合并成一行 — 同一段 ~150 字的拦截
    理由不逐票重复, 全文只在市场状态的 ⛔ 行出现一次."""
    lines = ["## 今日动作", ""]
    items: list[tuple[str, str]] = []
    halted: list[str] = []
    stale: list[str] = []
    for r in by_actionability(results):
        sym = r["symbol"]
        if r.get("stale_data"):
            stale.append(sym)
        if _has_stop(r):
            items.append((sym, f"- ⚠️ **{sym}** 右侧止损: 收盘跌破20日线 — "
                               "凸性档减半 / 结构破清仓"))
        leap, csp = r["leap"], r["csp"]
        if leap and "skip_reason" not in leap:
            ivp = self_ivp(ivdf, sym, r["iv30"]) if r["iv30"] else None
            if ivp is not None and ivp > 60:
                items.append((sym, f"- 🟡 **{sym}** 右侧确认但自建IVP "
                                   f"{ivp:.0f}>60 — 改 spread/PMCC (见下)"))
            else:
                items.append((sym, f"- 🟢 **{sym}** LEAP: BUY {leap['exp']} "
                                   f"{leap['strike']:g}C @ ~{leap['mid']:.2f} "
                                   f"(delta {leap['delta']:.2f}, 详见下)"))
        elif _regime_halted(leap):
            if sym not in halted:
                halted.append(sym)
        elif leap:
            items.append((sym, f"- ⏸ **{sym}** LEAP: {leap['skip_reason']}"))
        if csp and "skip_reason" not in csp:
            items.append((sym, f"- 🔵 **{sym}** CSP: SELL {csp['exp']} "
                               f"{csp['strike']:g}P @ ~{csp['mid']:.2f} "
                               f"(delta {csp['delta']:.2f}, 年化 "
                               f"~{csp['annualized_pct']:.0f}%, 详见下)"))
        elif _regime_halted(csp):
            if sym not in halted:
                halted.append(sym)
        elif csp:
            items.append((sym, f"- ⏸ **{sym}** CSP: {csp['skip_reason']}"))
        spread = r.get("spread")
        if spread and "skip_reason" not in spread:
            items.append((sym, f"- 🟣 **{sym}** 回踩 spread: BUY {spread['exp']} "
                               f"{spread['long_strike']:g}C / SELL "
                               f"{spread['short_strike']:g}C 净支出 "
                               f"~{spread['debit']:.2f} (详见下)"))
        elif _regime_halted(spread):
            if sym not in halted:
                halted.append(sym)
        elif spread:
            items.append((sym, f"- ⏸ **{sym}** 回踩 spread: {spread['skip_reason']}"))
        elif r.get("retest"):
            items.append((sym, f"- 👀 **{sym}** 首次回踩20日线不破 — 剧本首选"
                               "加仓点 (3-6个月 call spread, 手动构造)"))
    if stale:
        # 手机上扫一眼的那一屏必须看得到 — 概览表里这些标的的收盘价与
        # 右侧状态都建立在过期日线上
        items.append(("", f"- ⛔ 日线陈旧, 已停出票: {', '.join(stale)} — "
                          "Yahoo 日线与期权链是两个端点, 会不同步; 这些标的"
                          "的收盘价、概览表里的右侧状态判定与全部票据读数"
                          "都建立在过期价格上, 等日线补齐再看"))
    if halted:
        # regime_halt 也可能是 VVIX 的"只拦 CSP"或 VX 开关下的"只拦 LEAP/
        # spread" — 同一标的可以一边被拦一边有别的有效票, 写死"全市场/新票
        # 暂停"会与上面刚发出的票自相矛盾 (三轮评审)
        items.append(("", f"- ⏸ 市场门拦下部分新票 ({', '.join(halted)}) "
                          "— 拦截范围与原因见下方市场状态 ⛔ 行"))
    if items:
        lines += [line for _sym, line in items]
        mentioned = {sym for sym, _line in items} | set(halted) | set(stale)
        others = [r["symbol"] for r in results if r["symbol"] not in mentioned]
        if others:
            lines.append(f"- 其余今日无动作: {', '.join(others)}")
    else:
        lines.append("- 今日无动作 (无止损 / 无票 / 无新信号)")
    lines.append("")
    return lines


def sig_marks(sig: dict) -> str:
    m = lambda b: "✓" if b else "·"
    return (f"低{m(sig['no_new_low'])} 收{m(sig['reclaim20'])} "
            f"破{m(sig['breakout'])}")


def regime_block(regime: dict) -> list[str]:
    lines = [
        "## 市场状态 (VIX/VIX3M)",
        "",
        f"- VIX **{regime['vix']:.2f}** | VIX3M {regime['vix3m']:.2f} | "
        f"ratio **{regime['ratio']:.3f}**"
        + (f" | VXN {regime['vxn']:.2f}" if regime["vxn"] else "")
        + f"  (as of {regime['as_of']}, {regime['source']}"
        + (", 含盘中临时点·延迟15min" if regime.get("intraday") else "") + ")",
        f"- 阶段: **{regime['stage']}** — {REGIME_NOTES[regime['stage']]}",
    ]
    vx = regime.get("vx") or {}
    if vx.get("error"):
        lines.append(f"- VX 期货曲线: 获取失败 ({vx['error']}) — 全曲线倒挂门"
                     "未生效, 手动核对 volchart.io/moomoo")
    elif vx:
        vx_label = {"CONTANGO": "contango",
                    "PARTIAL_BACKWARDATION": "**局部倒挂**",
                    "FULL_BACKWARDATION": "**全曲线倒挂**"}.get(
            vx.get("state"), str(vx.get("state")))
        lines.append(
            f"- VX 期货: M1 {vx['m1']:.2f} ({vx['m1_exp']}) → "
            f"M2 {vx['m2']:.2f} ({vx['m2_exp']}), M1→M2 {vx['m1_m2_pct']:+.1f}% "
            f"— {vx_label} (结算 {vx['as_of']})")
    vvix, move = regime.get("vvix") or {}, regime.get("move") or {}
    if vvix or move:
        def _gauge(d, name, line_val, line_label):
            if d.get("error"):
                # 与 VX 错误行同规格: 带原因 + 明示门未生效 (README 承诺)
                return (f"{name} 获取失败 ({d['error']}) — "
                        f"{line_label}门未生效, 手动核对")
            return (f"{name} **{d['value']:.1f}** ({line_label} {line_val:g}, "
                    f"as of {d['as_of']})")
        gl = regime.get("gate_lines", SETTINGS_DEFAULTS)
        lines.append(
            "- " + _gauge(vvix, "VVIX", gl["vvix_halt"], "停牌线")
            + " | " + _gauge(move, "MOVE", gl["move_divergence"], "背离线"))
    for w in regime.get("gate_warnings", []):
        lines.append(f"- ⚠️ {w}")
    # 两道硬门可能同时活跃且消息不同 (如 VVIX 拦 CSP + VX 拦 LEAP/spread
    # 当 vx_full_backwardation_halt=false) — 各渲染一行, 相同消息去重
    seen_halts = []
    for h in (regime.get("halt_csp"), regime.get("halt_new_longs")):
        if h and h not in seen_halts:
            seen_halts.append(h)
            lines.append(f"- ⛔ {h}")
    if regime["stale_days"] > 5:
        lines.append(f"- ⚠️ **VIX 数据已 {regime['stale_days']} 天未更新** — "
                     "阶段判定不可信, 手动核对 CBOE/moomoo")
    if regime["crossed_up"]:
        lines.append("- ⚠️ **ratio 上穿 1.0** — 新一轮倒挂开始: CSP 第一档启动, 右侧停")
    if regime["crossed_down"]:
        lines.append(
            "- ⚠️ **ratio 下穿 1.0** — 倒挂解除: 历史上是统计最强入场窗 "
            "(2009 年以来解除日买 SPX: 5日 +3.04%/88%, 21日 +4.38%/91% vs "
            "基线 +0.26%/60%); 达标 episode (≥3日, 峰值≥1.10) 进阶段2 → "
            "CSP 常规档 + LEAP 窗口, 浅倒挂解除无加成")
    ep = regime["last_episode"]
    if ep and (ep["ongoing"] or regime["stage"] == "STAGE2_WINDOW"):
        lines.append(
            f"- 最近倒挂: {ep['start'].date()} → {ep['end'].date()}"
            f" ({ep['days']} 日, 峰值 {ep['peak']:.3f}"
            f"{', 进行中' if ep['ongoing'] else ''})")
    lines.append("")
    return lines


def render_close(results, regime, ivdf, now_et) -> str:
    d = now_et.strftime("%Y-%m-%d")
    lines = [f"# 左右侧 watchlist 扫描 — {d} 尾盘 "
             f"({now_et:%H:%M} ET)", ""]
    lines += action_block(results, ivdf)
    lines += regime_block(regime)

    ordered = by_actionability(results)
    lines += ["## 概览 (按可操作性排序)", "",
              "| 标的 | 状态 | 操作 | 收盘 | Δ% | vs20日 | 量比 | 三选二 "
              "| 价值区 | iv/rv | IVP |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in ordered:
        t = r["tech"]
        if t is None:
            lines.append(f"| {r['symbol']} | {STATE_LABEL['NO_DATA']} | — | — "
                         f"| — | — | — | — | — | — | — |")
            continue
        zone = r["cfg"]["value_zone"]
        if zone:
            if t["close"] > zone[1]:
                pos = f"上方+{(t['close'] / zone[1] - 1) * 100:.0f}%"
            elif t["close"] < zone[0]:
                pos = f"破下沿{(t['close'] / zone[0] - 1) * 100:.0f}%"
            else:
                pos = "区内"
            zone_s = f"{zone[0]:g}-{zone[1]:g} ({pos})"
        else:
            zone_s = "未设"
        ivp = self_ivp(ivdf, r["symbol"], r["iv30"]) if r["iv30"] else None
        ivrv = (f"{r['iv30'] * 100:.0f}/{t['rv30'] * 100:.0f}%"
                if r["iv30"] and t["rv30"]
                else fmt(r["iv30"] and r["iv30"] * 100, ".0f", "%"))
        lines.append(
            f"| {r['symbol']} | {STATE_LABEL[r['state']]} "
            f"| {action_label(r, ivp)} | {t['close']:.2f} "
            f"| {t['change_pct']:+.1f} | {t['vs_sma20_pct']:+.1f}% "
            f"| {t['vol_ratio']:.1f}x | {sig_marks(t['signals'])} "
            f"| {zone_s} | {ivrv} | {fmt(ivp, '.0f')} |")
    lines += [
        "",
        "> 三选二: **低**=不再新低(近5日低点 > 前15日低点) · **收**=放量收复"
        "20日线(收盘上穿 + 量比≥1.5) · **破**=突破前20日高 — ✓成立/·未成立, "
        "≥2个✓且有真实回调前提才算确认。价值区: 上方+X% = 现价高于接货带"
        "上沿X%(等回落), 区内 = 可接货, 破下沿 = 检查论点。",
        ""]

    detail = [r for r in ordered
              if r["state"] in ("CONFIRMED", "TREND", "LEFT_ZONE", "NEAR_ZONE")
              or r["notes"] or r["csp"] or r["leap"] or r.get("spread")
              or r.get("ladder") or r["error"]]
    if detail:
        lines += ["## 信号与建议", ""]
    for r in detail:
        since = f" (自 {r.get('state_since')})" if r.get("state_since") else ""
        lines.append(f"### {r['symbol']} — {STATE_LABEL.get(r['state'], r['state'])}{since}")
        if r["error"]:
            lines += [f"- 错误: {r['error']}", ""]
            continue
        t = r["tech"]
        lines.append(f"- 位置: 收盘 {t['close']:.2f} ({t['change_pct']:+.1f}%) · "
                     f"20日线 {t['vs_sma20_pct']:+.1f}% · "
                     f"200日线 {fmt(t['vs_sma200_pct'], '+.1f', '%')} · "
                     f"距52w高 {t['from_52w_high_pct']:+.1f}%")
        if r["state"] == "CONFIRMED" and r.get("prev_state") not in ("CONFIRMED", "TREND"):
            s_ = t["signals"]
            parts = [n for n, k in (("不再新低", "no_new_low"),
                                    ("放量收复20日线", "reclaim20"),
                                    ("突破20日高", "breakout")) if s_[k]]
            lines.append(f"- **今日右侧确认**: {' + '.join(parts)} (量比 {t['vol_ratio']:.1f}x)")
            lines.append("- 剧本: 突破入场假信号多 — 首次回踩突破位/20日线不破再走强时进, 止损放确认结构下方")
        for n in r["notes"]:
            lines.append(f"- {n}")
        if r["earnings"]:
            lines.append(f"- 下次财报: {r['earnings']}")
        csp = r["csp"]
        if csp:
            if "skip_reason" in csp:
                lines.append(ticket_skip_line("CSP", csp))
            else:
                tag = "恐慌档" if csp["panic_mode"] else "常规"
                lines.append(
                    f"- **CSP ({tag})**: SELL {r['symbol']} {csp['exp']} "
                    f"{csp['strike']:g}P @ ~{csp['mid']:.2f} — delta {csp['delta']:.2f}, "
                    f"{csp['dte']}DTE, 年化 ~{csp['annualized_pct']:.0f}%, "
                    f"缓冲 {csp['cushion_pct']:.1f}%, BE {csp['breakeven']:.2f}, "
                    f"OI {csp['oi']}"
                    + (f", 价差 {csp['spread_pct']:.0f}%" if csp["spread_pct"] is not None else ""))
                for n in csp["notes"]:
                    lines.append(f"  - {n}")
                zone = r["cfg"]["value_zone"]
                if zone is not None and csp["strike"] <= zone[1]:
                    lines.append("  - 愿意接货档 (行权价在价值区内): 拿到到期, "
                                 "跌破行权价 = 接货流程; 赚 50-60% 权利金可提前收")
                else:
                    lines.append("  - GTC 三角 (不想接货档): 赚 50-60% 权利金平 / "
                                 "权利金翻 3 倍或收盘跌破行权价平/roll / 剩 21 DTE 离场")
        ladder = r.get("ladder")
        if ladder:
            # 剧本要求间距递增 — 区间宽时固定折扣的末档间距反而更窄, 不谎报
            widening = (ladder[1] - ladder[2]) > (ladder[0] - ladder[1])
            spacing_note = ("间距递增" if widening else
                            "区间较宽, 末档间距未递增 — 剧本要求间距递增, 自行加深恐慌档")
            lines.append(
                f"- 正股分批档位: ① {ladder[0]:g} ② {ladder[1]:g} "
                f"③ {ladder[2]:g} (恐慌档) — {spacing_note}; 总仓位按\"还能再跌"
                f"30-50%\"定, 打完末档仍扛得住再跌; 止损靠论点失效不靠价格")
        leap = r["leap"]
        if leap:
            ivp = self_ivp(ivdf, r["symbol"], r["iv30"]) if r["iv30"] else None
            if "skip_reason" in leap:
                lines.append(ticket_skip_line("LEAP", leap))
            elif ivp is not None and ivp > 60:
                # 剧本 IV 档位: IVP >60 连 deep ITM 都改用 spread/PMCC/RR
                lines.append(
                    f"- LEAP: 自建IVP {ivp:.0f} > 60 — 剧本: 改用 spread/PMCC/"
                    f"risk reversal 或等 IVP 回落 (候选 {leap['exp']} "
                    f"{leap['strike']:g}C @ ~{leap['mid']:.2f}, 合约 IV "
                    f"{leap['iv'] * 100:.0f}%; moomoo IVP 实查确认)")
            else:
                lines.append(
                    f"- **LEAP**: BUY {r['symbol']} {leap['exp']} {leap['strike']:g}C "
                    f"@ ~{leap['mid']:.2f} — delta {leap['delta']:.2f}, "
                    f"外在 {fmt(leap['extrinsic_pct'], '.0f', '%')}, "
                    f"λ {fmt(leap['lam'], '.1f', 'x')}, BE {leap['breakeven']:.2f} "
                    f"({(leap['breakeven'] / t['close'] - 1) * 100:+.1f}%), "
                    f"保险费率 ~{leap['insurance_pct_yr']:.1f}%/年, "
                    f"合约 IV {leap['iv'] * 100:.0f}%, OI {leap['oi']}"
                    + (f", 价差 {leap['spread_pct']:.1f}%" if leap["spread_pct"] is not None else ""))
                for n in leap["notes"]:
                    lines.append(f"  - {n}")
        spread = r.get("spread")
        if spread:
            if "skip_reason" in spread:
                lines.append(ticket_skip_line("回踩 spread", spread))
            else:
                lines.append(
                    f"- **Call spread (回踩)**: BUY {r['symbol']} {spread['exp']} "
                    f"{spread['long_strike']:g}C @ ~{spread['long_mid']:.2f} / "
                    f"SELL {spread['short_strike']:g}C @ ~{spread['short_mid']:.2f} "
                    f"— 净支出 ~{spread['debit']:.2f}, 最大盈利 "
                    f"{spread['max_profit']:.2f} (赔率 {spread['reward_risk']:.1f}:1), "
                    f"BE {spread['breakeven']:.2f}, {spread['dte']}DTE, delta "
                    f"{spread['long_delta']:.2f}/{spread['short_delta']:.2f}, "
                    f"OI {spread['long_oi']}/{spread['short_oi']}")
                if spread.get("retest_low") is not None:
                    lines.append(
                        f"  - 止损: 收盘跌破回踩低点 {spread['retest_low']:.2f} → 平, "
                        "回收剩余权利金 (右侧止损必挂必执行)")
                lines.append("  - 仓位: 权利金 <= 0.5-1% NAV, 计入该标的总敞口")
                if r["earnings"] and r["earnings"] <= spread["exp"]:
                    lines.append(f"  - 财报 {r['earnings']} 在到期前 — 事件重估, 自查缺口风险")
                for n in spread.get("notes", []):
                    lines.append(f"  - {n}")
                if spread["src"] == "last":
                    lines.append("  - 盘口不可用, 按最近成交价估算 — 下单前实查")
        lines.append("")

    lines += [
        "---",
        "执行提醒: 左侧三条件齐才进 (价值区+被迫卖出证据+右侧确认); 加仓只加在强势上; "
        "IV 建议以 moomoo IVP/合约 IV 实查为准 (自建IVP 样本积累中)。",
        "数据: yfinance ~15min 延迟。合约价为 mid 估算, 下单前实查盘口。非投资建议。",
    ]
    return "\n".join(lines)


def render_open(results, regime, now_et, s: dict) -> str:
    d = now_et.strftime("%Y-%m-%d")
    lines = [f"# 开盘异动 — {d} ({now_et:%H:%M} ET)", ""]
    lines += regime_block(regime)
    alerts = []
    for r in results:
        t = r["tech"]
        sym = r["symbol"]
        if t is None:
            continue
        if abs(t["gap_pct"]) >= s["gap_alert_pct"]:
            alerts.append(f"- **{sym}** 开盘 gap {t['gap_pct']:+.1f}% "
                          f"(现价 {t['close']:.2f}, 较昨收 {t['change_pct']:+.1f}%)")
        elif abs(t["change_pct"]) >= s["move_alert_pct"]:
            alerts.append(f"- **{sym}** 盘初波动 {t['change_pct']:+.1f}% "
                          f"(现价 {t['close']:.2f})")
        zone = r["cfg"]["value_zone"]
        if zone and t["close"] <= zone[1]:
            alerts.append(f"- **{sym}** 在价值区内 ({zone[0]:g}-{zone[1]:g}, "
                          f"现价 {t['close']:.2f}) — 核对 CSP 挂单/接货档位")
        if r.get("prev_state") in ("CONFIRMED", "TREND") and t["close"] < t["sma20"]:
            alerts.append(f"- **{sym}** 盘中在20日线下 ({t['sma20']:.2f}) — "
                          f"右侧移动止损以**收盘**为准, 尾盘扫描确认")
        if r["earnings"]:
            days = (date.fromisoformat(r["earnings"]) - now_et.date()).days
            if 0 <= days <= s["earnings_alert_days"]:
                when = "今天" if days == 0 else f"{days} 天内"
                alerts.append(f"- **{sym}** 财报 {when} ({r['earnings']}) — "
                              f"short option 不跨财报; LEAP 等财报后")
    lines += alerts or ["- 无异动 (gap/波动/价值区/财报 均未触发)"]
    lines += ["", "---",
              "开盘扫描只做提醒 — 右侧确认以收盘为准, 见尾盘报告。数据 ~15min 延迟。"]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# Email rendering
# --------------------------------------------------------------------------

# 邮件客户端 (尤其 Gmail 手机版) 会剥掉 <style> 块, 所以样式一律内联。
# 也不用 flex/grid —— 老客户端不认。
_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',"
         "'PingFang SC','Microsoft YaHei',Roboto,sans-serif")
_MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"

# 行首状态标记 -> 卡片色 (手机上扫一眼靠它)
_MARK_COLORS = {
    "⛔": ("#fef2f2", "#dc2626"),   # 硬拦/停牌 — 红
    "🔴": ("#fef2f2", "#dc2626"),
    "⚠️": ("#fffbeb", "#d97706"),   # 预警 — 琥珀
    "🟡": ("#fffbeb", "#d97706"),
    "⏸": ("#f8fafc", "#94a3b8"),   # 被拦的票 — 灰
    "🟢": ("#f0fdf4", "#16a34a"),   # LEAP 票 — 绿
    "🔵": ("#eff6ff", "#2563eb"),   # CSP 票 — 蓝
    "👀": ("#f5f3ff", "#7c3aed"),   # 关注 — 紫
}


def _md_inline(text: str) -> str:
    """**粗体** / `代码` -> HTML, 其余转义。"""
    import html as _html
    import re
    out = _html.escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(
        r"`(.+?)`",
        r"<code style=\"font-family:" + _MONO +
        r";font-size:12px;background:#f1f5f9;padding:1px 4px;"
        r"border-radius:3px\">\1</code>", out)
    return out


def _mark_style(text: str):
    """行首若是状态标记, 返回 (背景色, 左边框色), 否则 None。"""
    for mark, colors in _MARK_COLORS.items():
        if text.startswith(mark):
            return colors
    return None


def _is_numeric_cell(s: str) -> bool:
    import re
    return bool(re.fullmatch(r"[+\-—–]?[\d.,]*%?x?", s.strip())) and any(
        c.isdigit() for c in s)


def _html_table(rows: list[list[str]]) -> str:
    """真表格 + 横向滚动。概览表有 11 列, 手机上必须能横拖, 否则挤成一团。"""
    head, body = rows[0], rows[1:]
    # 字体/字号/颜色由 <table> 继承下去 — 逐 cell 重复这些会让正文膨胀
    # 到 Gmail 的 102KB 截断线附近 (实测 15 只标的就到 76KB)
    th = "".join(
        f'<th style="padding:7px 9px;font-weight:600;font-size:12px;'
        f'color:#475569;border-bottom:2px solid #cbd5e1">{_md_inline(c)}</th>'
        for c in head)
    trs = []
    for i, row in enumerate(body):
        bg = "#ffffff" if i % 2 == 0 else "#f8fafc"
        tds = "".join(
            f'<td style="padding:7px 9px;border-bottom:1px solid #e2e8f0'
            + (f';text-align:right;font-family:{_MONO}"'
               if _is_numeric_cell(c) else '"')
            + f'>{_md_inline(c)}</td>' for c in row)
        trs.append(f'<tr style="background:{bg}">{tds}</tr>')
    # 默认给**卡片**, 宽表靠 @media (min-width) 在大屏才出现 —— 默认状态
    # 要选那个"CSS 全被剥掉时仍然可读"的。反过来 (默认宽表) 实测在 Gmail
    # 手机版就是一张截断的表, 而卡片在桌面上只是不如表格紧凑, 仍然能读。
    # 失败方向要倒向影响小的那边。
    return (
        '<div class="wl-wide" style="display:none;overflow-x:auto;'
        '-webkit-overflow-scrolling:touch;margin:12px 0;'
        'border:1px solid #e2e8f0;border-radius:6px">'
        '<table cellpadding="0" cellspacing="0" border="0" '
        f'style="border-collapse:collapse;width:100%;min-width:560px;'
        f'text-align:left;white-space:nowrap;font-size:13px;color:#0f172a;'
        f'font-family:{_FONT}">'
        f'<thead><tr style="background:#f1f5f9">{th}</tr></thead>'
        f'<tbody>{"".join(trs)}</tbody></table></div>'
        f'<div class="wl-narrow" style="margin:12px 0">'
        f'{_html_cards(rows)}</div>')


def _html_cards(rows: list[list[str]]) -> str:
    """同一份表格的窄屏版: 每行摊成一张卡片, 任何宽度都不用横拖。

    概览表 11 列, 375px 的手机上每列只能分到 ~30px, 表格无论如何压不下。
    横向滚动在 Gmail 手机版又经常退化成"整封邮件横向滚动"—— 比不滚更糟。
    所以窄屏直接换布局: 首列 (标的) 当标题, 其余列摊成 `标签 值` 的流式
    文本, 自然换行。"""
    head, body = rows[0], rows[1:]
    cards = []
    for row in body:
        title = _md_inline(row[0]) if row else ""
        state = _md_inline(row[1]) if len(row) > 1 else ""
        bits = []
        for label, val in zip(head[2:], row[2:]):
            v = val.strip()
            if not v or v in ("—", "-", "未设"):
                continue
            # 值用 <b>, 标签色由卡片外层继承 — 少一层 span 少一串内联样式
            bits.append(f'<span style="white-space:nowrap">{_md_inline(label)} '
                        f'<b style="color:#0f172a">{_md_inline(v)}</b></span>')
        cards.append(
            f'<div style="margin:8px 0;padding:10px 12px;background:#f8fafc;'
            f'border:1px solid #e2e8f0;border-radius:6px">'
            f'<div style="font-size:15px;font-weight:700;color:#0f172a">'
            f'{title} <span style="font-size:12px;font-weight:500;'
            f'color:#475569">{state}</span></div>'
            f'<div style="margin-top:5px;font-size:12px;line-height:1.9;'
            f'color:#94a3b8">' + " · ".join(bits) + '</div></div>')
    return "".join(cards)


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_table_sep(line: str) -> bool:
    body = line.replace("|", "").replace("-", "").replace(":", "").strip()
    return "|" in line and "-" in line and body == ""


def md_to_email_html(md: str) -> str:
    """把报告的 markdown 子集转成邮件 HTML。

    纯文本邮件里 11 列的表格就是一堆竖线, **粗体** 显示成星号 —— 手机上
    基本没法读。这里只处理 render_close/render_open 实际会产出的子集:
    #/##/### 标题、- 与两级缩进列表、表格、> 引用、--- 分隔线、行内
    **粗体** 和 `代码`。不引第三方 markdown 库: 子集是我们自己生成的,
    而且 droplet 上少一个依赖少一处会坏的地方。"""
    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        stripped = ln.strip()
        if not stripped:
            i += 1
            continue

        # 表格
        if stripped.startswith("|") and i + 1 < len(lines) \
                and _is_table_sep(lines[i + 1]):
            rows = [_split_row(stripped)]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(_split_row(lines[i]))
                i += 1
            out.append(_html_table(rows))
            continue

        # 标题
        if stripped.startswith("### "):
            out.append(
                f'<h3 style="margin:22px 0 6px;font-size:15px;font-weight:600;'
                f'color:#0f172a;border-left:3px solid #334155;padding-left:8px">'
                f'{_md_inline(stripped[4:])}</h3>')
            i += 1
            continue
        if stripped.startswith("## "):
            out.append(
                f'<h2 style="margin:26px 0 8px;font-size:17px;font-weight:700;'
                f'color:#0f172a">{_md_inline(stripped[3:])}</h2>')
            i += 1
            continue
        if stripped.startswith("# "):
            out.append(
                f'<h1 style="margin:0 0 4px;font-size:19px;font-weight:700;'
                f'color:#0f172a;line-height:1.35">'
                f'{_md_inline(stripped[2:])}</h1>')
            i += 1
            continue

        # 分隔线
        if stripped == "---":
            out.append('<hr style="border:0;border-top:1px solid #e2e8f0;'
                       'margin:22px 0">')
            i += 1
            continue

        # 引用 (表格下面那行图例)
        if stripped.startswith("> "):
            buf = []
            while i < len(lines) and lines[i].strip().startswith("> "):
                buf.append(lines[i].strip()[2:])
                i += 1
            out.append(
                f'<div style="margin:10px 0;padding:9px 11px;background:#f8fafc;'
                f'border-left:3px solid #cbd5e1;font-size:12px;color:#475569;'
                f'line-height:1.6">{_md_inline(" ".join(buf))}</div>')
            continue

        # 列表 (含两级缩进)
        if stripped.startswith("- "):
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                indent = len(lines[i]) - len(lines[i].lstrip())
                items.append((indent, lines[i].strip()[2:]))
                i += 1
            for indent, text in items:
                colors = _mark_style(text)
                if indent >= 2:
                    out.append(
                        f'<div style="margin:2px 0 2px 20px;font-size:12px;'
                        f'color:#64748b;line-height:1.65">· '
                        f'{_md_inline(text)}</div>')
                elif colors:
                    bg, bar = colors
                    out.append(
                        f'<div style="margin:6px 0;padding:9px 11px;'
                        f'background:{bg};border-left:4px solid {bar};'
                        f'border-radius:0 4px 4px 0;font-size:13px;'
                        f'color:#0f172a;line-height:1.6">'
                        f'{_md_inline(text)}</div>')
                else:
                    out.append(
                        f'<div style="margin:4px 0;padding-left:14px;'
                        f'font-size:13px;color:#1e293b;line-height:1.65;'
                        f'text-indent:-14px">• {_md_inline(text)}</div>')
            continue

        # 其余按段落
        out.append(f'<p style="margin:8px 0;font-size:13px;color:#475569;'
                   f'line-height:1.65">{_md_inline(stripped)}</p>')
        i += 1

    # 必须输出**完整 HTML 文档**: Gmail 只认 <head> 里的 <style>, 正文里
    # 的 <style> 会被直接剥掉 (实测: 手机上媒体查询完全不生效, 看到的是
    # 一张截断的宽表)。唯一的 <style> 只做增强 —— 大屏才把卡片换成表格,
    # 剥掉了也只是所有设备都用卡片。
    style = (
        "<style>@media only screen and (min-width:601px){"
        ".wl-wide{display:block!important}"
        ".wl-narrow{display:none!important}"
        ".wl-outer{padding:14px!important}"
        ".wl-inner{padding:18px 20px!important}"
        "}</style>")
    body = (
        f'<div class="wl-outer" style="margin:0;padding:8px;'
        f'background:#f1f5f9">'
        f'<div class="wl-inner" style="max-width:680px;margin:0 auto;'
        f'background:#ffffff;padding:16px 14px;border-radius:8px;'
        f'font-family:{_FONT};-webkit-text-size-adjust:100%">'
        + "".join(out) +
        f'<p style="margin:20px 0 0;padding-top:12px;'
        f'border-top:1px solid #e2e8f0;font-size:11px;color:#94a3b8">'
        f'watchlist-scanner · 自动发送, 请勿回复</p>'
        f'</div></div>')
    return ('<!DOCTYPE html><html><head>'
            '<meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,'
            'initial-scale=1">'
            '<meta name="color-scheme" content="light only">'
            '<meta name="supported-color-schemes" content="light only">'
            + style + '</head><body style="margin:0;padding:0;'
            'background:#f1f5f9">' + body + '</body></html>')


def send_via_resend(api_key: str, sender: str, to: str, subject: str,
                    body: str) -> None:
    """Resend 的 HTTPS API (443)。

    DigitalOcean 默认封锁 droplet 的出站 SMTP —— 25/465/587/2525 一律
    超时 (不是拒绝, 是静默丢包), 而 443 正常。所以云上唯一能把报告发出去
    的通道是 HTTPS 邮件 API, Gmail SMTP 在那种机器上永远连不上。
    sender 必须是 Resend 已验证的域名, 或沙箱地址 onboarding@resend.dev
    (沙箱只能发给账号自己的邮箱)。"""
    import json as _json
    import urllib.error

    # text + html 都带: html 给正常客户端 (markdown 纯文本在手机上没法读),
    # text 作为纯文本客户端和摘要预览的回落
    payload = _json.dumps({"from": sender, "to": [to], "subject": subject,
                           "text": body,
                           "html": md_to_email_html(body)}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json",
                 # 必须显式给 UA: urllib 默认发 "Python-urllib/3.x", 会被
                 # Resend 前面的 Cloudflare 按 UA 签名拦掉, 返回 403 且正文
                 # 是 "error code: 1010" —— 那是 Cloudflare 的码不是 Resend
                 # 的业务码, 很容易被误读成 api key 有问题
                 "User-Agent": "watchlist-scanner/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 201):
                raise RuntimeError(f"Resend HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        # 正文带的是 Resend 的错误说明 (未验证发件域/额度等), 不含 api key
        raise RuntimeError(
            f"Resend HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}"
        ) from e


def send_email_report(report_path: Path, subject: str) -> None:
    """Push the report for headless (droplet) deployments.

    两条通道, 有 SCAN_RESEND_API_KEY 就优先走 HTTPS:
    - Resend (HTTPS 443): SCAN_RESEND_API_KEY, SCAN_EMAIL_FROM, SCAN_EMAIL_TO
    - SMTP (STARTTLS):    SCAN_SMTP_HOST, SCAN_SMTP_PORT (默认 587),
                          SCAN_SMTP_USER, SCAN_SMTP_PASS, SCAN_EMAIL_TO
    云主机 (DigitalOcean 等) 普遍封锁出站 SMTP 端口, 那里必须用 HTTPS 那条;
    SMTP 留给本机/自建机器。任何失败都抛异常 —— 在无人值守的机器上, 邮件
    就是唯一的交付通道, 静默失败等于报告没跑。"""
    import smtplib
    from email.message import EmailMessage

    to = os.environ.get("SCAN_EMAIL_TO")
    if not to:
        raise RuntimeError("SCAN_EMAIL_TO not set (see deploy/.env.example)")
    api_key = os.environ.get("SCAN_RESEND_API_KEY")
    if api_key:
        send_via_resend(
            api_key, os.environ.get("SCAN_EMAIL_FROM", "onboarding@resend.dev"),
            to, subject, report_path.read_text(encoding="utf-8"))
        return

    host = os.environ.get("SCAN_SMTP_HOST")
    if not host:
        raise RuntimeError("SCAN_RESEND_API_KEY 或 SCAN_SMTP_HOST 至少要设一个 "
                           "(see deploy/.env.example)")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get(
        "SCAN_EMAIL_FROM", os.environ.get("SCAN_SMTP_USER", "watchlist-scanner"))
    msg["To"] = to
    report_md = report_path.read_text(encoding="utf-8")
    msg.set_content(report_md)
    msg.add_alternative(md_to_email_html(report_md), subtype="html")
    with smtplib.SMTP(host, int(os.environ.get("SCAN_SMTP_PORT", "587")),
                      timeout=30) as smtp:
        smtp.starttls()
        user = os.environ.get("SCAN_SMTP_USER")
        if user:
            smtp.login(user, os.environ["SCAN_SMTP_PASS"])
        smtp.send_message(msg)


def _sent_marker(report_path: Path) -> Path:
    """{date}-{mode}.md 的投递凭证 — 只在 send_email_report 成功后写。
    dedup 门与 watchdog 都以它为准: \"报告文件存在\"证明的是扫描跑过,
    不证明报告到过收件箱, 两件事必须分开记账 (五轮评审)。"""
    return report_path.with_suffix(".sent")


def resend_pending_reports(d: str, now_et: datetime) -> None:
    """补发当日已写盘但没发出去的 canonical 报告 (.md 在而 .sent 不在)。

    没有这条, 一次 Resend/SMTP 抖动 = 当天报告静默丢失: 发信失败的 run
    以 exit 1 收场, DST 双保险的下一次 fire 又被\"报告已存在\"的 dedup
    拦回 (exit 3), watchdog 只看得到文件存在, 而 FAILED 告警本身走的还是
    同一条坏通道。补发挂在每次 --email 定时运行的最前面、窗口门之前 —
    投递不需要市场在开盘, 窗口外的 fire 正好当补发班车。

    只补投递, 绝不重扫: state.json 在上一轮已推进, 重扫会把 fresh_confirm
    这类一次性转换当场消耗掉 (等价于 R1 那个事故的自制版)。主题取
    latest-{mode}.json 里的 regime 阶段, 拿不到就裸主题 — 补发标题带
    resend 字样, 与原始邮件区分且不进同一 Gmail 会话。"""
    for mode in ("open", "close"):
        p = REPORTS / f"{d}-{mode}.md"
        marker = _sent_marker(p)
        if not p.exists() or marker.exists():
            continue
        stage = ""
        try:
            latest = json.loads(
                (REPORTS / f"latest-{mode}.json").read_text(encoding="utf-8"))
            stage = (latest.get("regime") or {}).get("stage", "")
        except Exception:
            pass
        subject = (f"[watchlist] {d} {mode} {now_et:%H:%M} ET resend"
                   + (f" — {stage}" if stage else ""))
        try:
            send_email_report(p, subject)
            marker.write_text(now_et.isoformat(), encoding="utf-8")
            print(f"resent {p.name}")
            ping_heartbeat()
        except Exception as e:
            # 不让补发失败弄死本次运行 — 当前窗口的正式扫描照跑;
            # 未送达状态由 watchdog 的 .sent 检查兜底
            print(f"EMAIL RESEND FAILED ({p.name}): {e}", file=sys.stderr)


def ping_heartbeat() -> None:
    """SCAN_HEARTBEAT_URL (healthchecks.io / ntfy 等) — 邮件之外的独立
    活性信号。邮件商故障时 FAILED/MISSED 告警会跟着一起哑 (同通道),
    心跳服务按\"没收到 ping\"从它那边报警, 不依赖本机任何出站邮件。
    只在**投递成功**后 ping — 心跳的语义是\"报告送达\", 不是\"进程活着\"。
    未配置时是 no-op。"""
    url = os.environ.get("SCAN_HEARTBEAT_URL")
    if not url:
        return
    try:
        urllib.request.urlopen(url, timeout=10)
    except Exception as e:
        print(f"heartbeat ping failed: {e}", file=sys.stderr)


def batch_history(symbols: list[str]) -> dict[str, pd.DataFrame | None]:
    df = yf.download(symbols, period="1y", interval="1d", auto_adjust=True,
                     group_by="ticker", progress=False, threads=True)
    out = {}
    for sym in symbols:
        try:
            # yfinance with group_by="ticker" returns (Ticker, Price) MultiIndex
            # columns even for a single symbol — no special case needed
            sub = df[sym].dropna(subset=["Close"])
            out[sym] = sub if not sub.empty else None
        except KeyError:
            out[sym] = None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="左右侧 watchlist 扫描器")
    ap.add_argument("--mode", choices=["open", "close", "auto"], default="auto")
    ap.add_argument("--force", action="store_true",
                    help="ignore window/duplicate/market-live gates")
    ap.add_argument("--tickers", help="comma-separated subset (testing)")
    ap.add_argument("--no-options", action="store_true",
                    help="skip option chains (fast technicals-only pass)")
    ap.add_argument("--email", action="store_true",
                    help="email the report after writing it (SCAN_SMTP_* env)")
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    now_et = datetime.now(ET)
    # 补发在窗口门**之前**: 投递不需要市场开着, 窗口外的定时 fire (DST
    # 双保险的另一半、看门狗前的任意一发) 正好当补发班车。只看 canonical
    # 报告名, manual 报告不补
    if args.email and not args.force and not args.tickers:
        resend_pending_reports(now_et.strftime("%Y-%m-%d"), now_et)
    mode = resolve_mode(args.mode, now_et)
    if mode is None:
        if args.force:
            mode = "close" if now_et.hour >= 13 else "open"
        else:
            print(f"outside scan windows ({now_et:%a %H:%M} ET) — skip")
            return SKIP

    # --force/--tickers = manual test run: keep it off the canonical report
    # name (the auto run's dup gate checks it) and off state/iv history, so
    # a mid-session test never makes the real scheduled scan silently skip
    manual = args.force or bool(args.tickers)
    d = now_et.strftime("%Y-%m-%d")
    suffix = "-manual" if manual else ""
    report_path = REPORTS / f"{d}-{mode}{suffix}.md"
    if report_path.exists() and not args.force:
        print(f"{report_path.name} already exists — skip (DST double-fire)")
        return SKIP
    if not args.force and not market_is_live():
        print("US market not live (holiday/half-day/stale feed) — skip")
        return SKIP

    settings, tickers = load_config()
    if args.tickers:
        keep = {t.strip().upper() for t in args.tickers.split(",")}
        tickers = {k: v for k, v in tickers.items() if k in keep}
    symbols = list(tickers)

    print(f"mode={mode} {d} {now_et:%H:%M} ET — {len(symbols)} tickers")
    regime = fetch_regime(settings)
    hist = batch_history(symbols)
    state = load_state()

    fetch_options = (mode == "close") and not args.no_options
    fetch_earnings_only = mode == "open"
    results_by_sym = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {
            pool.submit(
                analyze_ticker, sym, cfg, hist.get(sym),
                state.get(sym, {}), regime, settings, mode,
                fetch_options) : sym
            for sym, cfg in tickers.items()}
        for fut in as_completed(futs):
            r = fut.result()
            results_by_sym[r["symbol"]] = r
            status = r["error"] or r["state"]
            print(f"  {r['symbol']:<6} {status}")
    results = [results_by_sym[s_] for s_ in symbols]

    # open pass still wants earnings dates for the alert block
    if fetch_earnings_only:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(next_earnings, yf.Ticker(sym)): sym
                    for sym in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                e = fut.result()
                if e is None and tickers[sym]["kind"] in ("etf", "index"):
                    e = ""
                results_by_sym[sym]["earnings"] = e

    ivdf = load_iv_history()
    if mode == "close":
        if manual:
            for r in results:
                r["state_since"] = state.get(r["symbol"], {}).get("since")
        else:
            ivdf = append_iv_history(results, d)
            # persist state transitions (close-basis only, per the playbook)
            for r in results:
                if r["error"] or r["tech"] is None:
                    continue
                if r.get("stale_data"):
                    # r["state"] 是拿过期价格算出来的 — 落盘会覆盖
                    # state/since 并消耗一次性转换, 等于用昨天的价把今天的
                    # 信号用掉。报告已声明这些读数不可信, 持久化更不能收
                    # (四轮评审)
                    r["state_since"] = state.get(r["symbol"], {}).get("since")
                    continue
                prev = state.get(r["symbol"], {})
                state[r["symbol"]] = next_persisted_state(prev, r, d)
                r["state_since"] = state[r["symbol"]]["since"]
            save_state(state)
        report = render_close(results, regime, ivdf, now_et)
    else:
        report = render_open(results, regime, now_et, settings)

    REPORTS.mkdir(exist_ok=True)
    report_path.write_text(report + "\n", encoding="utf-8")
    latest = {
        "mode": mode, "generated_et": now_et.isoformat(), "regime": regime,
        "results": [{k: v for k, v in r.items() if k != "cfg"} for r in results],
    }
    (REPORTS / f"latest-{mode}{suffix}.json").write_text(
        json.dumps(latest, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8")

    print()
    print(report)
    print(f"\nREPORT {report_path}")
    if args.email:
        # 带上扫描时间: 主题重复时 Gmail 会把多封归进同一会话, 然后把
        # "重复"的正文折叠成 "Show quoted text" / 三个点 —— 报告正文每天
        # 高度相似, 极易命中。每封主题唯一就不会归会话, 也就不会被折叠。
        subject = (f"[watchlist] {d} {mode} {now_et:%H:%M} ET"
                   + (" manual" if manual else "")
                   + f" — {regime['stage']}")
        try:
            send_email_report(report_path, subject)
            # 投递凭证与报告文件分开记账 — dedup 判"跑没跑过"看 .md,
            # watchdog 判"送没送到"看 .sent, 补发看两者之差
            _sent_marker(report_path).write_text(now_et.isoformat(),
                                                 encoding="utf-8")
            print(f"email sent to {os.environ.get('SCAN_EMAIL_TO')}")
            ping_heartbeat()
        except Exception as e:
            print(f"EMAIL FAILED: {e}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
