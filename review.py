#!/usr/bin/env python3
"""推荐复盘: 结算 data/recommendations.jsonl 里的 CSP / LEAP, 出胜率。

用法:
  .venv/Scripts/python.exe review.py                 # 结算 + 汇总
  .venv/Scripts/python.exe review.py --backfill      # 从 reports/*.md 补历史
  .venv/Scripts/python.exe review.py --symbol NVDA   # 只看某标的
  .venv/Scripts/python.exe review.py --json out.json # 结算结果另存
  .venv/Scripts/python.exe review.py --demo          # 造 mock 数据看报表长什么样
  .venv/Scripts/python.exe review.py --md out.md     # 输出 markdown (可邮寄/手机读)

—— CSP 和 LEAP 不能合成一个胜率 ——
CSP 有自然的二元结局 (到期日那天要么在行权价上方作废、要么被行权), 到期即可
结算。LEAP 是 450-1100 DTE 的多头仓, 在复盘窗口内**没有结局** —— 它的"胜负"
取决于你什么时候平, 而那是持仓决策不是推荐决策。把未平仓的 LEAP 按当前浮盈
算进胜率, 等于用"还没结束的比赛"的中场比分凑胜场数。所以这里分两张表:
CSP 出已结算胜率, LEAP 只出未实现状态并明确标注不计入胜率。

—— CSP 的"胜"有两个口径, 都要看 ——
① 作废率 (expired OTM): 到期收盘 > 行权价, 权利金全收。这是机械胜率。
② 越过盈亏平衡率: 到期收盘 > 行权价 − 权利金。被行权**不等于**亏 —— 这套
   剧本的 CSP 行权价本来就压在"愿意接货"的价值区里, 接到货是预期内结果。
只报①会把"按计划接货"记成失败, 只报②会掩盖接货频率。两个一起看才是实情。
"""
import argparse
import json
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

import scanner as sc

BASE = Path(__file__).resolve().parent
DEMO_JOURNAL = sc.DATA / "demo_journal.jsonl"

# mock 数据的免责声明。**跟着数据走, 不跟着命令走** —— summarize() 只要在行里
# 看见 source="mock" 就无条件打印这一整块。理由: 报表会被截图、复制、隔几周
# 再翻出来看, 那时"这是 --demo 跑的"这个上下文早没了, 只剩下一个 95% 的作废率。
# 警告必须和数字绑在一起, 分不开。
MOCK_CAVEATS = [
    "⚠️  以下含 MOCK 数据 —— 这不是策略业绩, 是为了看报表长什么样造的。四处与真实系统不同:",
    "   ① 前视偏差 (最严重): value_zone 是 2026-09 手工定的, 拿它筛更早的入场 = 用未来信息挑历史仓位。",
    "   ② IV 用滚动已实现波动率代理: 真 IV 通常高于 RV (方差风险溢价) → 权利金被低估;",
    "      同一 delta 下行权价也被摆得更近 → 被行权率被高估。两个方向都偏。",
    "   ③ 无盘口: 没有 bid/ask/OI, 不过流动性门。",
    "   ④ 入场是固定周期, 不是真的状态机 / regime 闸 / 财报排除。",
    "   另: 卖 put 在任何非崩盘期都会显示高作废率 —— 务必对着下面的 delta 基准线读, 别看绝对值。",
]


# ---------------------------------------------------------------- 回填
# 报告正文里的票据行 (render_close 的格式):
#   - **CSP (常规)**: SELL NVDA 2026-10-17 205P @ ~2.35 — delta 0.12, 21DTE, ...
#   - **LEAP**: BUY NVDA 2027-06-18 180C @ ~52.30 — delta 0.80, ...
CSP_RE = re.compile(
    r"\*\*CSP \((?P<tag>[^)]+)\)\*\*: SELL (?P<sym>[A-Z.]+) (?P<exp>\d{4}-\d\d-\d\d) "
    r"(?P<strike>[\d.]+)P @ ~(?P<mid>[\d.]+).*?delta (?P<delta>[\d.]+), "
    r"(?P<dte>\d+)DTE")
LEAP_RE = re.compile(
    r"\*\*LEAP\*\*: BUY (?P<sym>[A-Z.]+) (?P<exp>\d{4}-\d\d-\d\d) "
    r"(?P<strike>[\d.]+)C @ ~(?P<mid>[\d.]+).*?delta (?P<delta>[\d.]+)")
DATE_RE = re.compile(r"(\d{4}-\d\d-\d\d)")
# 行尾的可选字段: 有就取, 没有就算了 (格式随版本演进过, 老报告可能缺)
OI_RE = re.compile(r"OI (\d+)")
CIV_RE = re.compile(r"合约 IV (\d+)%")


def backfill_rows(reports_dir: Path) -> list[dict]:
    """从历史 .md 报告正文反解推荐 -> journal 行 (source='backfill')。

    这是**有损**的: 报告是给人读的, 不是结构化存档。回填拿不到 oi/iv/spread/
    zone/stage 这些只在 JSON 里出现过的字段, 也拿不到当时的现价 —— 所以
    spot_at_rec 为 None, LEAP 的浮盈基准只能退化成"以权利金为基准"。
    标 source='backfill' 就是为了让复盘时能把它和 source='scan' 分开看,
    别把两种数据质量混在一张表里当同一回事。

    只扫 close 报告: open 报告不出期权票。
    """
    rows = []
    for f in sorted(reports_dir.glob("*-close*.md")):
        m = DATE_RE.search(f.name)
        if not m:
            continue
        d = m.group(1)
        # -manual 报告来自 --force/--tickers 的手工跑。那些票是用真实链算的、
        # 当时确实推荐过, 所以回填进来; 但手工跑的时点与标的是人挑的, 采样
        # 不是每日等间隔 —— 混进胜率会有选择性偏差。打 run_type 标签让它可分离,
        # 汇总里单列一行。同一合约同一天重复跑由 journal_key 挡掉。
        run_type = "manual" if "-manual" in f.name else "auto"
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for mm in CSP_RE.finditer(txt):
            rows.append({
                "date": d, "mode": "close", "symbol": mm["sym"], "kind": "csp",
                "action": "SELL_PUT", "exp": mm["exp"],
                "strike": float(mm["strike"]), "mid": float(mm["mid"]),
                "delta": float(mm["delta"]), "dte": int(mm["dte"]),
                "panic_mode": mm["tag"].startswith("恐慌"),
                "breakeven": round(float(mm["strike"]) - float(mm["mid"]), 4),
                "spot_at_rec": None, "source": "backfill",
                "run_type": run_type, "source_file": f.name, "notes": [],
            })
        for mm in LEAP_RE.finditer(txt):
            tail = txt[mm.end():mm.end() + 220]
            oi, civ = OI_RE.search(tail), CIV_RE.search(tail)
            rows.append({
                "date": d, "mode": "close", "symbol": mm["sym"], "kind": "leap",
                "action": "BUY_CALL", "exp": mm["exp"],
                "strike": float(mm["strike"]), "mid": float(mm["mid"]),
                "delta": float(mm["delta"]),
                "oi": int(oi.group(1)) if oi else None,
                "iv": (int(civ.group(1)) / 100) if civ else None,
                "spot_at_rec": None, "source": "backfill",
                "run_type": run_type, "source_file": f.name, "notes": [],
            })
    return rows


# ---------------------------------------------------------------- 结算
def _closes(symbols, start, end) -> dict:
    """批量取日线收盘 -> {sym: Series}。一次网络往返, 失败的标的留空。"""
    if not symbols:
        return {}
    df = yf.download(sorted(symbols), start=start, end=end, progress=False,
                     auto_adjust=False, group_by="ticker", threads=True)
    out = {}
    for s in sorted(symbols):
        try:
            col = df[s]["Close"] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
            col = col.dropna()
            col.index = [i.date() for i in col.index]
            if len(col):
                out[s] = col
        except (KeyError, TypeError):
            continue
    return out


def _on_or_before(ser, d: date):
    """d 当日收盘, 没有就取之前最近一个交易日 (到期日逢假/半日市)。"""
    idx = [i for i in ser.index if i <= d]
    return (idx[-1], float(ser[idx[-1]])) if idx else (None, None)


def resolve(rows: list[dict], today: date) -> list[dict]:
    """给每行加结算字段。CSP 到期后二元结算; LEAP 一律未实现。"""
    syms = {r["symbol"] for r in rows}
    if not syms:
        return []
    start = min(r["date"] for r in rows)
    px = _closes(syms, start, (today + pd.Timedelta(days=1)).isoformat())
    out = []
    for r in dict_rows(rows):
        ser = px.get(r["symbol"])
        r["last_px"] = float(ser.iloc[-1]) if ser is not None and len(ser) else None
        exp = date.fromisoformat(r["exp"]) if r.get("exp") else None
        if r["kind"] == "csp":
            if exp is None or exp > today:
                r["status"] = "open"
                r["dte_left"] = (exp - today).days if exp else None
                # 未到期也给一个"此刻在不在行权价上方", 方便看盘中风险
                if r["last_px"] is not None and r.get("strike"):
                    r["itm_now"] = r["last_px"] <= r["strike"]
            elif ser is None:
                r["status"] = "unresolved_no_price"
            else:
                sd, close = _on_or_before(ser, exp)
                r["settle_date"], r["settle_close"] = (
                    sd.isoformat() if sd else None), close
                if close is None:
                    r["status"] = "unresolved_no_price"
                else:
                    r["status"] = "expired_otm" if close > r["strike"] else "assigned"
                    r["above_breakeven"] = close > (
                        r.get("breakeven") if r.get("breakeven") is not None
                        else r["strike"] - (r.get("mid") or 0))
                    # 被行权时的账面结果: (到期收盘 − 行权价) + 权利金, 每股
                    r["pnl_per_share"] = round(
                        (r.get("mid") or 0) + min(0.0, close - r["strike"]), 4)
                    # 持有期内最深回撤到行权价下方多少 (曾破位 != 到期被行权)
                    win = ser[[i for i in ser.index
                               if date.fromisoformat(r["date"]) <= i <= exp]]
                    if len(win):
                        r["min_close_in_window"] = round(float(win.min()), 4)
                        r["breached"] = bool(float(win.min()) <= r["strike"])
        else:  # leap —— 复盘窗口内没有结局, 只给未实现状态
            r["status"] = "open_unrealized"
            r["dte_left"] = (exp - today).days if exp else None
            if r["last_px"] is not None:
                r["itm_now"] = r["last_px"] > r["strike"]
                base = r.get("spot_at_rec")
                r["underlying_ret"] = (round(r["last_px"] / base - 1, 4)
                                       if base else None)
        out.append(r)
    return out


def dict_rows(rows):
    for r in rows:
        yield dict(r)


# ---------------------------------------------------------------- demo
def _round_strike(k: float) -> float:
    """按标的价位取常见行权价档距 (纯函数)。"""
    step = 1.0 if k < 50 else (2.5 if k < 200 else 5.0)
    return round(k / step) * step


def _strike_for_delta(spot, sigma, T, target, rate):
    """二分找 |delta|≈target 的 put 行权价。

    用 scanner 自己的 bs_delta, 不重写 —— 平行实现会漏掉所有你不知道自己
    依赖的东西 (这个教训在 sec-filing-downloader 上刚吃过一次)。
    """
    lo, hi = spot * 0.30, spot * 0.999
    for _ in range(60):
        mid = (lo + hi) / 2
        if abs(sc.bs_delta(spot, mid, T, rate, sigma, False)) > target:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def generate_demo(out_path: Path, start="2025-06-01", end="2026-09-18",
                  every_days=21, seed=7) -> list[dict]:
    """造一份 MOCK 流水账 —— 只为了看统计报表长什么样, 不是业绩估计。

    用**真实日线** + scanner 自己的 bs_delta/bs_price 选行权价与定价, 所以
    结算结果是真实市场走出来的; 但入场规则只是对扫描器逻辑的粗略近似, 且带
    前视偏差 (见 MOCK_CAVEATS)。每行打 source="mock", summarize() 见到就会
    无条件把整块免责声明打在报表顶部。

    **不写进真实流水账**: 调用方必须给一个不同于 sc.JOURNAL 的路径, main()
    里有硬拦。mock 行一旦混进 data/recommendations.jsonl, 以后每份复盘都得
    先分辨哪些是真的 —— 而 source 字段是唯一的区分手段, 太脆。
    """
    import math
    import numpy as np
    S, TICK = sc.load_config()
    uni = {k: v for k, v in TICK.items() if v["value_zone"] and v["options"]}
    px = yf.download(sorted(uni), start=start, end="2026-09-20", progress=False,
                     auto_adjust=False, group_by="ticker", threads=True)
    d_start, d_end = date.fromisoformat(start), date.fromisoformat(end)
    rows = []
    for sym, cfg in uni.items():
        try:
            c = px[sym]["Close"].dropna()
            c.index = [i.date() for i in c.index]
        except (KeyError, TypeError):
            continue
        zlo, zhi = cfg["value_zone"]
        idx = [d for d in c.index if d_start <= d <= d_end]
        ret = np.log(c / c.shift(1))
        for i in range(0, len(idx), every_days):
            d0 = idx[i]
            spot = float(c[d0])
            # 近似真扫描器的 zone 闸: 带内或带上沿 near_zone_pct 以内才出 CSP
            if spot > zhi * (1 + S["near_zone_pct"] / 100):
                continue
            win = ret[[x for x in ret.index if x <= d0]][-60:]
            if len(win) < 30:
                continue
            sigma = float(win.std() * math.sqrt(252))
            if not (0.05 < sigma < 3):
                continue
            dte = 21
            T = dte / 365
            k = _round_strike(_strike_for_delta(
                spot, sigma, T, S["csp_delta_target"], sc.RATE))
            if k > zhi:                       # 行权价 <= 接货带上沿 (硬约束)
                k = _round_strike(min(k, zhi))
            if k <= 0:
                continue
            mid = round(sc.bs_price(spot, k, T, sc.RATE, sigma, False), 2)
            ann = sc.csp_annualized(mid, k, dte)
            # 与真扫描器同一道薄权利金闸
            if mid < S["csp_min_mid"] or ann < S["csp_min_annualized"]:
                continue
            rows.append({
                "date": d0.isoformat(), "mode": "close", "symbol": sym,
                "kind": "csp", "action": "SELL_PUT",
                "exp": (d0 + timedelta(days=dte)).isoformat(),
                "strike": float(k), "mid": mid,
                "delta": round(abs(sc.bs_delta(spot, k, T, sc.RATE, sigma, False)), 3),
                "dte": dte, "iv": round(sigma, 4), "spot_at_rec": round(spot, 2),
                "annualized_pct": round(ann, 1),
                "cushion_pct": round((spot - k) / spot * 100, 2),
                "breakeven": round(k - mid, 2), "panic_mode": False,
                "zone": [zlo, zhi], "zone_asof": cfg["zone_asof"],
                "high_beta": cfg["high_beta"], "ticker_state": "MOCK",
                "stage": "NORMAL", "source": "mock", "run_type": "auto",
                "notes": []})
        for i in range(0, len(idx), every_days * 6):        # LEAP 稀疏得多
            d0 = idx[i]
            spot = float(c[d0])
            win = ret[[x for x in ret.index if x <= d0]][-120:]
            if len(win) < 60:
                continue
            sigma = float(win.std() * math.sqrt(252))
            if not (0.05 < sigma < 3):
                continue
            T = 500 / 365
            k = _round_strike(_strike_for_delta(spot, sigma, T, 0.80, sc.RATE))
            rows.append({
                "date": d0.isoformat(), "mode": "close", "symbol": sym,
                "kind": "leap", "action": "BUY_CALL",
                "exp": (d0 + timedelta(days=500)).isoformat(),
                "strike": float(k),
                "mid": round(sc.bs_price(spot, k, T, sc.RATE, sigma, False)
                             + spot - k, 2),
                "delta": 0.80, "iv": round(sigma, 4),
                "spot_at_rec": round(spot, 2), "zone": [zlo, zhi],
                "high_beta": cfg["high_beta"], "ticker_state": "MOCK",
                "stage": "NORMAL", "source": "mock", "run_type": "auto",
                "notes": []})
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
        encoding="utf-8")
    return rows


# ---------------------------------------------------------------- 汇总
def _dw(t: str) -> int:
    """显示宽度: CJK 全角算 2 —— 中文表头与 ASCII 数据混排时用 len() 会错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in str(t))


def _pad(t, w: int, right=False) -> str:
    """按显示宽度补空格。"""
    t = str(t)
    gap = " " * max(0, w - _dw(t))
    return (gap + t) if right else (t + gap)


def leap_table(leap: list[dict]) -> list[str]:
    """LEAP 逐笔明细 (纯函数) -> 报表行。

    只给"当前 ITM 17/30"这类聚合数读不出任何可操作信息 —— 是哪几张、什么时候
    进的、离到期还有多久, 全看不见。LEAP 是要长期持有并择机 roll 的仓位, 明细
    才是这一段的用处。

    **ITM 不等于赚钱**: 多头 call 的盈亏平衡是 行权价 + 权利金, 不是行权价。
    深度 ITM 的 LEAP 权利金本来就厚 (实测 mock 里 NVDA 150C 付了 45+),
    现价越过行权价只说明有内在价值, 越过盈亏平衡才是真的不亏。所以两列都给,
    并且汇总里两个计数并排 —— 只报 ITM 会系统性高估这条腿的表现。

    入手时现价取自 spot_at_rec: scan/mock 行有, 回填行没有 (报告正文里没写),
    那几行显示 "—" 而不是留空或补 0。
    """
    if not leap:
        return []
    hdr = [("标的", 6, False), ("入手", 11, False), ("到期", 11, False),
           ("行权价", 8, True), ("入手价", 8, True), ("权利金", 8, True),
           ("盈亏平衡", 9, True), ("最新价", 8, True), ("正股涨跌", 9, True),
           ("状态", 12, False), ("剩余", 7, True)]
    # 列间留一格: 右对齐的数字列与紧随其后的列会贴死 (实测 "+18.7%✓ 越过平衡")
    out = ["  " + " ".join(_pad(h, w, r) for h, w, r in hdr)]
    out.append("  " + "-" * (sum(w for _, w, _ in hdr) + len(hdr) - 1))
    for r in sorted(leap, key=lambda x: (x["symbol"], x["date"])):
        k, mid, last = r.get("strike"), r.get("mid"), r.get("last_px")
        be = (k + mid) if (k is not None and mid is not None) else None
        sp = r.get("spot_at_rec")
        ret = r.get("underlying_ret")
        if last is None:
            st = "无价格"
        elif be is not None and last > be:
            st = "✓ 越过平衡"
        elif r.get("itm_now"):
            st = "ITM 未回本"
        else:
            st = "✗ OTM"
        cells = [r["symbol"], r["date"], r.get("exp") or "—",
                 f"{k:g}" if k is not None else "—",
                 f"{sp:.2f}" if sp is not None else "—",
                 f"{mid:.2f}" if mid is not None else "—",
                 f"{be:.2f}" if be is not None else "—",
                 f"{last:.2f}" if last is not None else "—",
                 f"{ret:+.1%}" if ret is not None else "—",
                 st,
                 f"{r['dte_left']}天" if r.get("dte_left") is not None else "—"]
        out.append("  " + " ".join(_pad(c, w, rt)
                                   for c, (_, w, rt) in zip(cells, hdr)))
    return out


def delta_baseline(done: list[dict]) -> dict | None:
    """卖方作废率 vs delta 隐含的理论作废率 (纯函数) -> dict|None。

    **为什么必须有这条**: put 的 |delta| 近似它到期 ITM 的概率, 所以卖 0.12
    delta 就"本该"有约 88% 作废。不给基准线, 一个 95% 的作废率会被读成
    "系统很准", 而它可能只是按定义就该这么高 —— 甚至可能低于应有水平还被
    当成好消息。要看的是**超出基准多少**, 以及样本够不够撑住那个差。

    一起给 sigma: 二项标准误 sqrt(p(1-p)/n)。mock 实测 n=39 时 se≈5.2pp,
    +7.1pp 的超额只有 1.4σ —— 看起来很像 edge, 统计上还什么都不是。
    """
    dl = [abs(r["delta"]) for r in done if r.get("delta") is not None]
    if not dl or not done:
        return None
    n = len(done)
    exp_otm = sum(1 - d for d in dl) / len(dl)
    real = sum(1 for r in done if r.get("status") == "expired_otm") / n
    se = (exp_otm * (1 - exp_otm) / n) ** 0.5
    return {"n": n, "avg_delta": sum(dl) / len(dl), "expected_otm": exp_otm,
            "realized_otm": real, "excess": real - exp_otm, "se": se,
            "sigma": (real - exp_otm) / se if se > 0 else 0.0,
            "n_for_half_se": n * 4}



def compute_stats(res: list[dict]) -> dict:
    """所有口径**只在这里算一次** -> dict。文本与 markdown 两个渲染器共用。

    让两个渲染器各自算一遍 = 必然漂移: 改了一处忘另一处, 两份报表给出不同的
    胜率, 而且没人会同时看两份所以不会被发现。仓库 lesson 里记过同型问题
    (同源文案会同步扩散错误)。这里把"算"和"排版"彻底分开。
    """
    csp = [r for r in res if r["kind"] == "csp"]
    leap = [r for r in res if r["kind"] == "leap"]
    done = [r for r in csp if r["status"] in ("expired_otm", "assigned")]
    openc = [r for r in csp if r["status"] == "open"]
    bad = [r for r in csp if r["status"] == "unresolved_no_price"]
    src, rt = {}, {}
    for r in res:
        src[r.get("source", "?")] = src.get(r.get("source", "?"), 0) + 1
        rt[r.get("run_type") or "auto"] = rt.get(r.get("run_type") or "auto", 0) + 1

    st = {"n": len(res), "csp": csp, "leap": leap, "done": done, "open": openc,
          "bad": bad, "src": src, "run_type": rt,
          "has_mock": any(r.get("source") == "mock" for r in res),
          "baseline": delta_baseline(done) if done else None}

    if done:
        st["otm"] = sum(1 for r in done if r["status"] == "expired_otm")
        st["above_be"] = sum(1 for r in done if r.get("above_breakeven"))
        st["breached"] = sum(1 for r in done if r.get("breached"))
        pnl = [r["pnl_per_share"] for r in done if r.get("pnl_per_share") is not None]
        st["pnl_sum"] = sum(pnl) if pnl else None
        st["pnl_avg"] = (sum(pnl) / len(pnl)) if pnl else None
        pairs = [(r["pnl_per_share"] / r["strike"], r.get("dte") or 21)
                 for r in done
                 if r.get("pnl_per_share") is not None and r.get("strike")]
        if pairs:
            roc = [x for x, _ in pairs]
            avg_d = sum(d for _, d in pairs) / len(pairs)
            st["roc_sum"] = sum(roc)
            st["roc_avg"] = sum(roc) / len(roc)
            st["roc_days"] = avg_d
            st["roc_ann"] = (sum(roc) / len(roc)) * 365 / avg_d
    if openc:
        st["open_itm"] = sum(1 for r in openc if r.get("itm_now"))
    if leap:
        st["leap_itm"] = sum(1 for r in leap if r.get("itm_now"))
        # 多头 call 的盈亏平衡是 行权价 + 权利金。只报 ITM 会系统性高估这条腿:
        # 深 ITM 的 LEAP 权利金本来就厚, 有内在价值 != 回本
        st["leap_be"] = sum(
            1 for r in leap
            if r.get("last_px") is not None and r.get("strike") is not None
            and r.get("mid") is not None and r["last_px"] > r["strike"] + r["mid"])
        rets = [r["underlying_ret"] for r in leap
                if r.get("underlying_ret") is not None]
        if rets:
            st["leap_ret_med"] = float(pd.Series(rets).median())
            st["leap_ret_avg"] = sum(rets) / len(rets)
            st["leap_ret_up"] = sum(1 for x in rets if x > 0)
            st["leap_ret_n"] = len(rets)
    by = {}
    for r in done:
        by.setdefault(r["symbol"], []).append(r)
    st["by_symbol"] = [
        (sym, sum(1 for r in g if r["status"] == "expired_otm"), len(g),
         sum(r.get("pnl_per_share") or 0 for r in g))
        for sym, g in sorted(by.items(), key=lambda kv: -len(kv[1]))]
    return st


def _src_line(st) -> str:
    return ("来源: " + " / ".join(f"{k} {v}" for k, v in sorted(st["src"].items()))
            + ("   (backfill 缺 zone/stage/现价, 数据质量低于 scan)"
               if st["src"].get("backfill") else ""))


def _rt_line(st):
    if not st["run_type"].get("manual"):
        return None
    return ("运行类型: "
            + " / ".join(f"{k} {v}" for k, v in sorted(st["run_type"].items()))
            + "   ⚠️ manual 来自手工跑, 时点与标的是人挑的, 采样非等间隔 ——"
              " 胜率里含选择性偏差, 量大时用 --exclude-manual 对照")


def _sigma_verdict(bl) -> str:
    return ("—— 在噪声范围内, **还不能说系统有 edge**"
            if abs(bl["sigma"]) < 2 else "—— 超出 2σ, 值得继续观察")


def summarize(res: list[dict]) -> str:
    """纯文本报表 (终端用)。数字全部来自 compute_stats, 这里只排版。"""
    st = compute_stats(res)
    done, openc, leap = st["done"], st["open"], st["leap"]
    L = []
    if st["has_mock"]:
        L.append("=" * 68)
        L += MOCK_CAVEATS
    L.append("=" * 68)
    L.append(f"推荐复盘  共 {st['n']} 条 (CSP {len(st['csp'])} / LEAP {len(leap)})")
    L.append(_src_line(st))
    if _rt_line(st):
        L.append(_rt_line(st))
    L.append("=" * 68)
    L.append("")
    L.append(f"【CSP】已结算 {len(done)} / 未到期 {len(openc)}"
             + (f" / 无价格无法结算 {len(st['bad'])}" if st["bad"] else ""))
    if done:
        n = len(done)
        L.append(f"  ① 作废率 (到期 > 行权价, 权利金全收): "
                 f"{st['otm']}/{n} = {st['otm'] / n:.0%}")
        L.append(f"  ② 越过盈亏平衡率 (到期 > 行权价 − 权利金): "
                 f"{st['above_be']}/{n} = {st['above_be'] / n:.0%}")
        L.append("     —— ② 比 ① 高的部分 = 被行权但仍不亏的单子; 这套剧本的"
                 "行权价压在愿意接货的价值区里, 接货是预期内结果不是失败")
        L.append(f"  持有期内曾跌破行权价: {st['breached']}/{n} = "
                 f"{st['breached'] / n:.0%}  (曾破位 ≠ 到期被行权)")
        bl = st["baseline"]
        if bl:
            L.append(f"  ★ delta 基准: 均 delta {bl['avg_delta']:.3f} → 理论作废率 "
                     f"{bl['expected_otm']:.0%}; 实际 {bl['realized_otm']:.0%}, "
                     f"差 {bl['excess']:+.1%}")
            L.append(f"    n={bl['n']}, 标准误 {bl['se']:.1%} → {abs(bl['sigma']):.1f}σ "
                     + _sigma_verdict(bl)
                     + f"; 要把 ±{bl['se'] * 100:.0f}pp 的误差压到一半"
                       f"需要约 {bl['n_for_half_se']} 笔")
            L.append("    (作废率高本身不是本事: delta 越低越容易作废, 代价是"
                     "权利金越薄。真正要看的是下面按抵押金归一的收益率)")
        if st.get("roc_sum") is not None:
            L.append(f"  抵押金回报率 (pnl/行权价, 每笔独立占用): "
                     f"合计 {st['roc_sum']:+.2%} / 单均 {st['roc_avg']:+.3%} / "
                     f"平均持有 {st['roc_days']:.0f} 天")
            L.append(f"    单笔年化当量 ~{st['roc_ann']:+.1%} "
                     "(假设资金连续复用且始终有票可卖 —— 实际有空窗, 别当真实年化)")
        if st.get("pnl_sum") is not None:
            L.append(f"  每股账面合计 {st['pnl_sum']:+.2f} / 单均 {st['pnl_avg']:+.2f} "
                     "(跨标的每股金额**不可加**, 仅供对账; 归一口径看上面一行)")
    else:
        L.append("  (还没有到期的 CSP —— 胜率要等第一批到期后才有意义)")
    if openc:
        L.append(f"  未到期 {len(openc)} 笔, 其中当前已在行权价下方 "
                 f"{st['open_itm']} 笔")

    L.append("")
    L.append(f"【LEAP】{len(leap)} 笔 —— **不计入胜率**")
    L.append("  LEAP 是 450-1100 DTE 的多头仓, 复盘窗口内没有结局; 它的胜负取决于")
    L.append("  你何时平仓, 那是持仓决策不是推荐决策。这里只给未实现状态。")
    if leap:
        L.append(f"  当前 ITM: {st['leap_itm']}/{len(leap)}   "
                 f"越过盈亏平衡 (行权价+权利金): {st['leap_be']}/{len(leap)}")
        L.append("    —— ITM 只说明有内在价值; 越过盈亏平衡才是真的不亏。"
                 "两个数差得远说明权利金付贵了")
        if st.get("leap_ret_n"):
            L.append(f"  正股自推荐日涨跌: 中位 {st['leap_ret_med']:+.1%} / "
                     f"均值 {st['leap_ret_avg']:+.1%} / "
                     f"上涨 {st['leap_ret_up']}/{st['leap_ret_n']}")
        else:
            L.append("  (无法算正股涨跌 —— 回填记录没有推荐日现价)")
        L.append("")
        L += leap_table(leap)

    if st["by_symbol"]:
        L.append("")
        L.append("【按标的 (仅已结算 CSP)】")
        for sym, o, tot, pnl in st["by_symbol"]:
            L.append(f"  {sym:<6} {o}/{tot} 作废  每股合计 {pnl:+.2f}")
    return "\n".join(L)


def summarize_md(res: list[dict], title="推荐复盘") -> str:
    """Markdown 报表。数字与 summarize() 同源 (compute_stats), 这里只排版。

    为什么值得单出一份 md: 扫描器本身的日报就是 markdown (邮件推送 + 手机阅读),
    这份复盘同一条路就能发出去; 而且明细表在 md 下是真表格, GitHub/邮件客户端
    /预览器都能渲染, 不依赖等宽字体 —— 纯文本那版在手机上一定会折行错位。
    """
    st = compute_stats(res)
    done, openc, leap = st["done"], st["open"], st["leap"]
    M = [f"# {title}", ""]
    if st["has_mock"]:
        # 免责声明在 md 里用引用块 —— 视觉上和数字分开, 但仍在同一份文件里,
        # 截图也带得走 (这是 MOCK_CAVEATS 绑在数据上的同一条理由)
        M += ["> " + line.strip() for line in MOCK_CAVEATS]
        M.append("")
    M.append(f"共 **{st['n']}** 条（CSP {len(st['csp'])} / LEAP {len(leap)}）。"
             + _src_line(st))
    if _rt_line(st):
        M += ["", _rt_line(st)]
    M += ["", "## CSP", "",
          f"已结算 **{len(done)}** / 未到期 {len(openc)}"
          + (f" / 无价格无法结算 {len(st['bad'])}" if st["bad"] else "")]
    if done:
        n = len(done)
        M += ["", "| 口径 | 值 | 说明 |", "|---|---|---|",
              f"| ① 作废率 | **{st['otm']}/{n} = {st['otm'] / n:.0%}** "
              f"| 到期 > 行权价, 权利金全收 |",
              f"| ② 越过盈亏平衡率 | **{st['above_be']}/{n} = "
              f"{st['above_be'] / n:.0%}** | 到期 > 行权价 − 权利金 |",
              f"| 持有期内曾跌破行权价 | {st['breached']}/{n} = "
              f"{st['breached'] / n:.0%} | 曾破位 ≠ 到期被行权 |", ""]
        M.append("② 比 ① 高的部分 = 被行权但仍不亏的单子。这套剧本的行权价压在"
                 "愿意接货的价值区里，**接货是预期内结果不是失败**。")
        bl = st["baseline"]
        if bl:
            M += ["", "### ★ delta 基准线", "",
                  f"均 delta {bl['avg_delta']:.3f} → 理论作废率 "
                  f"**{bl['expected_otm']:.0%}**；实际 **{bl['realized_otm']:.0%}**，"
                  f"差 **{bl['excess']:+.1%}**。",
                  "",
                  f"n={bl['n']}，标准误 {bl['se']:.1%} → **{abs(bl['sigma']):.1f}σ** "
                  + _sigma_verdict(bl)
                  + f"。要把 ±{bl['se'] * 100:.0f}pp 的误差压到一半需要约 "
                    f"**{bl['n_for_half_se']}** 笔。",
                  "",
                  "> 作废率高本身不是本事：delta 越低越容易作废，代价是权利金越薄。"
                  "真正要看的是下面按抵押金归一的收益率。"]
        if st.get("roc_sum") is not None:
            M += ["", "### 抵押金回报率", "",
                  f"`pnl / 行权价`（每笔独立占用）：合计 **{st['roc_sum']:+.2%}** / "
                  f"单均 **{st['roc_avg']:+.3%}** / 平均持有 {st['roc_days']:.0f} 天。",
                  "",
                  f"单笔年化当量 ~**{st['roc_ann']:+.1%}** —— 假设资金连续复用且"
                  "始终有票可卖，实际有空窗，别当真实年化。"]
        if st.get("pnl_sum") is not None:
            M += ["", f"每股账面合计 {st['pnl_sum']:+.2f} / 单均 "
                      f"{st['pnl_avg']:+.2f}（跨标的每股金额**不可加**，仅供对账）。"]
    else:
        M += ["", "_还没有到期的 CSP —— 胜率要等第一批到期后才有意义。_"]
    if openc:
        M += ["", f"未到期 {len(openc)} 笔，其中当前已在行权价下方 "
                  f"{st['open_itm']} 笔。"]

    M += ["", "## LEAP", "",
          f"**{len(leap)} 笔 —— 不计入胜率。** LEAP 是 450-1100 DTE 的多头仓，"
          "复盘窗口内没有结局；胜负取决于何时平仓，那是持仓决策不是推荐决策。"]
    if leap:
        M += ["",
              f"当前 ITM **{st['leap_itm']}/{len(leap)}**，"
              f"越过盈亏平衡（行权价+权利金）**{st['leap_be']}/{len(leap)}**。",
              "",
              "> ITM 只说明有内在价值；越过盈亏平衡才是真的不亏。"
              "两个数差得远说明权利金付贵了。"]
        if st.get("leap_ret_n"):
            M += ["", f"正股自推荐日涨跌：中位 {st['leap_ret_med']:+.1%} / "
                      f"均值 {st['leap_ret_avg']:+.1%} / "
                      f"上涨 {st['leap_ret_up']}/{st['leap_ret_n']}。"]
        M += ["", "| 标的 | 入手 | 到期 | 行权价 | 入手价 | 权利金 | 盈亏平衡 "
                  "| 最新价 | 正股涨跌 | 状态 | 剩余 |",
              "|---|---|---|--:|--:|--:|--:|--:|--:|---|--:|"]
        for r in sorted(leap, key=lambda x: (x["symbol"], x["date"])):
            k, mid, last = r.get("strike"), r.get("mid"), r.get("last_px")
            be = (k + mid) if (k is not None and mid is not None) else None
            sp, ret = r.get("spot_at_rec"), r.get("underlying_ret")
            if last is None:
                stt = "无价格"
            elif be is not None and last > be:
                stt = "✓ 越过平衡"
            elif r.get("itm_now"):
                stt = "ITM 未回本"
            else:
                stt = "✗ OTM"
            M.append("| " + " | ".join([
                r["symbol"], r["date"], r.get("exp") or "—",
                f"{k:g}" if k is not None else "—",
                f"{sp:.2f}" if sp is not None else "—",
                f"{mid:.2f}" if mid is not None else "—",
                f"{be:.2f}" if be is not None else "—",
                f"{last:.2f}" if last is not None else "—",
                f"{ret:+.1%}" if ret is not None else "—", stt,
                f"{r['dte_left']}天" if r.get("dte_left") is not None else "—",
            ]) + " |")

    if st["by_symbol"]:
        M += ["", "## 按标的（仅已结算 CSP）", "",
              "| 标的 | 作废 | 每股合计 |", "|---|---|--:|"]
        for sym, o, tot, pnl in st["by_symbol"]:
            M.append(f"| {sym} | {o}/{tot} | {pnl:+.2f} |")
    M += ["", "---", "", "_分析工具输出，不构成投资建议。_"]
    return "\n".join(M)


def main() -> int:
    ap = argparse.ArgumentParser(description="推荐复盘 / 胜率")
    ap.add_argument("--backfill", action="store_true",
                    help="先从 reports/*-close*.md 反解历史推荐写入流水账")
    ap.add_argument("--reports", default=str(BASE / "reports"))
    ap.add_argument("--journal", default=None, help="流水账路径 (默认 data/recommendations.jsonl)")
    ap.add_argument("--symbol", help="只看某标的 (逗号分隔)")
    ap.add_argument("--exclude-manual", action="store_true",
                    help="只看自动跑的推荐 (剔除 --force/--tickers 手工跑的采样偏差)")
    ap.add_argument("--json", dest="json_out", help="结算明细另存为 JSON")
    ap.add_argument("--md", dest="md_out", nargs="?", const="AUTO",
                    help="输出 markdown 报表 (给路径, 或不给参数则写 "
                         "reports/review-<日期>.md)。表格在 md 下是真表格, "
                         "手机与邮件客户端不会错位")
    ap.add_argument("--demo", action="store_true",
                    help="造 MOCK 数据看报表长什么样 (写 data/demo_journal.jsonl, "
                         "**不碰**真实流水账; 报表顶部会打死免责声明)")
    a = ap.parse_args()
    path = Path(a.journal) if a.journal else sc.JOURNAL

    if a.demo:
        # 硬拦: mock 行一旦混进真实流水账, source 字段就是唯一的区分手段 —— 太脆。
        # 没给 --journal 就落到 demo 专用文件; 显式指向真账本则直接拒绝。
        if a.journal is None:
            path = DEMO_JOURNAL
        elif path.resolve() == sc.JOURNAL.resolve():
            print(f"拒绝: --demo 不能写进真实流水账 {sc.JOURNAL}"
                  f"\n  (要看 mock 就用默认路径 {DEMO_JOURNAL.name}, "
                  "或 --journal 指到别处)")
            return 2
        rows = generate_demo(path)
        print(f"MOCK: 生成 {len(rows)} 条 "
              f"(CSP {sum(1 for r in rows if r['kind'] == 'csp')} / "
              f"LEAP {sum(1 for r in rows if r['kind'] == 'leap')}) -> {path}")
        print("  这份数据不是业绩估计 —— 报表顶部有完整免责声明\n")

    if a.backfill:
        rd = Path(a.reports)
        rows = backfill_rows(rd)
        n = sc.append_journal(rows, path)
        print(f"回填: 从 {rd} 扫到 {len(rows)} 条, 新写入 {n} 条 "
              f"(重复的已按 date|symbol|kind|exp|strike 跳过)")
        if not rows:
            print("  —— 没扫到票据行。本地 reports/ 里若只有陈旧数据日的报告, "
                  "那些天本来就没出票; 历史在 droplet 上, 把它的 reports/ 同步"
                  "过来再跑一次即可。")

    rows = sc.load_journal(path)
    if a.symbol:
        keep = {x.strip().upper() for x in a.symbol.split(",")}
        rows = [r for r in rows if r["symbol"] in keep]
    if a.exclude_manual:
        rows = [r for r in rows if (r.get("run_type") or "auto") != "manual"]
    if not rows:
        print(f"流水账为空: {path}")
        print("  收盘扫描 (非 --force/--tickers 的手工跑) 会自动记录; "
              "历史用 --backfill 从报告反解。")
        return 0
    res = resolve(rows, datetime.now(sc.ET).date())
    print(summarize(res))
    if a.md_out:
        mp = (BASE / "reports" / f"review-{date.today().isoformat()}.md"
              if a.md_out == "AUTO" else Path(a.md_out))
        mp.parent.mkdir(exist_ok=True)
        mp.write_text(summarize_md(res) + chr(10), encoding="utf-8")
        print(chr(10) + f"markdown -> {mp}")
    if a.json_out:
        Path(a.json_out).write_text(
            json.dumps(res, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
        print(f"\n明细 -> {a.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
