#!/usr/bin/env python3
"""推荐复盘: 结算 data/recommendations.jsonl 里的 CSP / LEAP, 出胜率。

用法:
  .venv/Scripts/python.exe review.py                 # 结算 + 汇总
  .venv/Scripts/python.exe review.py --backfill      # 从 reports/*.md 补历史
  .venv/Scripts/python.exe review.py --symbol NVDA   # 只看某标的
  .venv/Scripts/python.exe review.py --json out.json # 结算结果另存

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
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

import scanner as sc

BASE = Path(__file__).resolve().parent


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


# ---------------------------------------------------------------- 汇总
def summarize(res: list[dict]) -> str:
    L = []
    csp = [r for r in res if r["kind"] == "csp"]
    leap = [r for r in res if r["kind"] == "leap"]
    done = [r for r in csp if r["status"] in ("expired_otm", "assigned")]
    openc = [r for r in csp if r["status"] == "open"]
    bad = [r for r in csp if r["status"] == "unresolved_no_price"]

    L.append("=" * 68)
    L.append(f"推荐复盘  共 {len(res)} 条 (CSP {len(csp)} / LEAP {len(leap)})")
    src = {}
    for r in res:
        src[r.get("source", "?")] = src.get(r.get("source", "?"), 0) + 1
    L.append(f"来源: " + " / ".join(f"{k} {v}" for k, v in sorted(src.items()))
             + "   (backfill 缺 zone/stage/现价, 数据质量低于 scan)")
    rt = {}
    for r in res:
        rt[r.get("run_type") or "auto"] = rt.get(r.get("run_type") or "auto", 0) + 1
    if rt.get("manual"):
        L.append(f"运行类型: " + " / ".join(f"{k} {v}" for k, v in sorted(rt.items()))
                 + "   ⚠️ manual 来自手工跑, 时点与标的是人挑的, 采样非等间隔 ——"
                 " 胜率里含选择性偏差, 量大时用 --exclude-manual 对照")
    L.append("=" * 68)

    L.append("")
    L.append(f"【CSP】已结算 {len(done)} / 未到期 {len(openc)}"
             + (f" / 无价格无法结算 {len(bad)}" if bad else ""))
    if done:
        otm = [r for r in done if r["status"] == "expired_otm"]
        abv = [r for r in done if r.get("above_breakeven")]
        brc = [r for r in done if r.get("breached")]
        pnl = [r["pnl_per_share"] for r in done if r.get("pnl_per_share") is not None]
        L.append(f"  ① 作废率 (到期 > 行权价, 权利金全收): "
                 f"{len(otm)}/{len(done)} = {len(otm) / len(done):.0%}")
        L.append(f"  ② 越过盈亏平衡率 (到期 > 行权价 − 权利金): "
                 f"{len(abv)}/{len(done)} = {len(abv) / len(done):.0%}")
        L.append(f"     —— ② 比 ① 高的部分 = 被行权但仍不亏的单子; 这套剧本的"
                 "行权价压在愿意接货的价值区里, 接货是预期内结果不是失败")
        L.append(f"  持有期内曾跌破行权价: {len(brc)}/{len(done)} = "
                 f"{len(brc) / len(done):.0%}  (曾破位 ≠ 到期被行权)")
        if pnl:
            L.append(f"  每股账面合计 {sum(pnl):+.2f} / 单均 {sum(pnl) / len(pnl):+.2f} "
                     f"(= 权利金 + min(0, 到期收盘 − 行权价), 未计手续费与资金占用)")
    else:
        L.append("  (还没有到期的 CSP —— 胜率要等第一批到期后才有意义)")
    if openc:
        itm = [r for r in openc if r.get("itm_now")]
        L.append(f"  未到期 {len(openc)} 笔, 其中当前已在行权价下方 {len(itm)} 笔")

    L.append("")
    L.append(f"【LEAP】{len(leap)} 笔 —— **不计入胜率**")
    L.append("  LEAP 是 450-1100 DTE 的多头仓, 复盘窗口内没有结局; 它的胜负取决于")
    L.append("  你何时平仓, 那是持仓决策不是推荐决策。这里只给未实现状态。")
    if leap:
        itm = [r for r in leap if r.get("itm_now")]
        rets = [r["underlying_ret"] for r in leap if r.get("underlying_ret") is not None]
        L.append(f"  当前 ITM: {len(itm)}/{len(leap)}")
        if rets:
            L.append(f"  正股自推荐日涨跌: 中位 {pd.Series(rets).median():+.1%} / "
                     f"均值 {sum(rets) / len(rets):+.1%} / "
                     f"上涨 {sum(1 for x in rets if x > 0)}/{len(rets)}")
        else:
            L.append("  (无法算正股涨跌 —— 回填记录没有推荐日现价)")

    by = {}
    for r in done:
        by.setdefault(r["symbol"], []).append(r)
    if by:
        L.append("")
        L.append("【按标的 (仅已结算 CSP)】")
        for sym in sorted(by, key=lambda x: -len(by[x])):
            g = by[sym]
            o = sum(1 for r in g if r["status"] == "expired_otm")
            L.append(f"  {sym:<6} {o}/{len(g)} 作废  "
                     f"每股合计 {sum(r.get('pnl_per_share') or 0 for r in g):+.2f}")
    return "\n".join(L)


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
    a = ap.parse_args()
    path = Path(a.journal) if a.journal else sc.JOURNAL

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
    if a.json_out:
        Path(a.json_out).write_text(
            json.dumps(res, ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
        print(f"\n明细 -> {a.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
