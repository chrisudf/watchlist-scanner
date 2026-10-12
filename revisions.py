#!/usr/bin/env python3
"""分析师预期修正 (Yahoo eps_revisions / eps_trend) — 取数 / 解析 / 一次性判据 / 快照.

只做提示, 不进状态机/门控/票据 (2026-10-12 定)。雅虎只给当下快照、没有历史,
回测不了; 这个仓库的规矩是指标先回测再进门控 (MACD/KDJ/布林都被回测否掉过)。
所以先每天收盘存一份快照 (data/eps_revisions.csv), 攒满约 120 个交易日再验证
有没有预测力 (todo.md #10)。

数据: yfinance 的 earningsTrend 模块, 一次请求, 下面三张表共用它的缓存:
  - eps_revisions: 行 0q/+1q/0y/+1y (本季/下季/本财年/下财年), 列 upLast7days /
    upLast30days / downLast30days / downLast7Days = 近 7/30 天上调/下调 EPS 预期
    的分析师家数
  - eps_trend: 同四行, 列 current/7daysAgo/30daysAgo/60daysAgo/90daysAgo =
    一致预期 EPS 和它 N 天前的值 (最多回溯 90 天)
  - earnings_estimate 的 numberOfAnalysts: 覆盖家数, 只进快照 (回测时当分母)

报告看下财年 (+1y): 本财年会被一次性项目污染 (见 one_time_flag), 估值也看下财年。

两套口径 (2026-10-12 实测, 快照两套都存, 报告只显示默认那套):
  - 默认: 上面三张表 = Yahoo 页面切换钮上标 "GAAP" 的那套, 逐格对得上。标签别按
    字面理解 —— NVDA 两套数字完全相同, 而它的 GAAP 与 non-GAAP 本应差一块股权激励;
    更像"多数分析师用的口径"的大池子。不报 non-GAAP 的公司 (AMZN/META) 大池子
    就是 GAAP, 一次性项目会混进去。
  - Normalized: 未公开的 quoteSummary 模块 earningsTrendNonGaap, yfinance 的
    valid_modules 里没有, 只能走它的私有接口 (_data.get_raw_json) —— yfinance
    一改就会失效, 所以单独一个请求、单独降级, 坏了不影响默认那套和报告。
  - watchlist 个股 + MCD 共 15 只里 10 只两套一样; RKLB 下财年默认 -0.07 / ↑2↓6、
    Normalized +0.06 / ↑5↓0, 修正方向相反; META 54 家 vs 15 家; AMZN 本财年 51 家
    (含 Anthropic) vs 4 家。哪套更有预测力留给回测 (todo.md #10)。

三个实测坑 (2026-10-12):
  1. ETF/指数没有这个模块, Yahoo 回 404, yfinance 默认吞掉异常、返回空表。所以
     ETF/指数按 kind 直接不查 (静默)。个股拿到空表多半是取数失败被吞了
     (watchlist 14 只个股全有覆盖), 要写进失败提示, 不能当成"没有修正"。
  2. 列名大小写不统一: upLast7days 是小写 d, downLast7Days 是大写 D —— 按小写比对。
  3. 亏损股 (RKLB 两年都亏, SPCX 本财年 90 天前还是负数) 的 EPS 为负或穿零, 百分比
     没有方向意义 (-1.00 → -0.50 是减亏却算出 -50%), 改报绝对值 a→b。

命令行 (手工看一眼; 不写快照):
    .venv/bin/python revisions.py [MCD NVDA ...]
不带代码 = watchlist.toml 里全部个股。
"""
from __future__ import annotations

import csv
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PERIODS = ("0q", "+1q", "0y", "+1y")
SHOW_PERIOD = "+1y"
# (我们的键, Yahoo 列名小写) —— 小写比对见坑 2
REV_FIELDS = (("up7", "uplast7days"), ("up30", "uplast30days"),
              ("down30", "downlast30days"), ("down7", "downlast7days"))
TREND_FIELDS = (("eps", "current"), ("eps_7d", "7daysago"),
                ("eps_30d", "30daysago"), ("eps_60d", "60daysago"),
                ("eps_90d", "90daysago"))
PERIOD_KEYS = (*(k for k, _ in REV_FIELDS), "n", *(k for k, _ in TREND_FIELDS))

# 一次性判据, 数值照抄 sec-filing-downloader valuation/ref_table.py 的
# one_time_warning (pe_rank.py 的 one_time_flag 复用同一判据)
ONE_TIME_MIN_JUMP = 0.15    # 本财年 90 天变动的绝对值下限
ONE_TIME_RATIO = 2.5        # 且大于下财年变动的这么多倍

NORMALIZED_MODULE = "earningsTrendNonGaap"
NORM_PREFIX = "norm_"

SNAPSHOT_NAME = "eps_revisions.csv"
# 每行 = 一个 ET 交易日 × 一只个股。列名 "{period}_{键}" = 默认口径 (例如 +1y_up30 /
# 0y_eps_90d), "norm_{period}_{键}" = Normalized 口径; 那天 Normalized 没取到就留空
_COLS = [f"{p}_{k}" for p in PERIODS for k in PERIOD_KEYS]
SNAPSHOT_FIELDS = (["date", "symbol"] + _COLS
                   + [NORM_PREFIX + c for c in _COLS])


class EmptyData(Exception):
    """个股拿到空表 —— 坑 1: 多半是取数失败被 yfinance 吞了。"""


# --------------------------------------------------------------------------
# Parsing (pure)
# --------------------------------------------------------------------------

def _num(v) -> float | None:
    """表格单元 -> float; NaN / None / 非数字 -> None。"""
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _rows(df, fields) -> dict[str, dict]:
    """DataFrame (index = period) -> {period: {我们的键: 值}}; 列名按小写比对。"""
    if df is None or getattr(df, "empty", True):
        return {}
    cols = {str(c).lower(): c for c in df.columns}
    out = {}
    for p in PERIODS:
        if p not in df.index:
            continue
        row = df.loc[p]
        out[p] = {k: (_num(row[cols[src]]) if src in cols else None)
                  for k, src in fields}
    return out


def _int(v: float | None) -> int | None:
    return None if v is None else int(round(v))


def parse(rev_df, trend_df, est_df=None) -> dict[str, dict]:
    """三张表 -> {period: {up7, up30, down30, down7, n, eps, eps_7d, ...}}.
    修正表和趋势表都没有内容 = {}。"""
    rev = _rows(rev_df, REV_FIELDS)
    trend = _rows(trend_df, TREND_FIELDS)
    est = _rows(est_df, (("n", "numberofanalysts"),))
    out = {}
    for p in PERIODS:
        if p not in rev and p not in trend:
            continue
        d = dict.fromkeys(PERIOD_KEYS)
        d.update(trend.get(p, {}))
        d.update({k: _int(v) for k, v in rev.get(p, {}).items()})
        d["n"] = _int(est.get(p, {}).get("n"))
        out[p] = d
    return out


def _raw(v) -> float | None:
    """quoteSummary 的单元格是 {"raw": 2, "fmt": "2"} (formatted=false 也照样包一层),
    缺值是 {}。"""
    return _num(v.get("raw") if isinstance(v, dict) else v)


def parse_trend_json(items) -> dict[str, dict]:
    """quoteSummary 原始 JSON 的 trend 列表 (earningsTrend / earningsTrendNonGaap 同一
    结构) -> 与 parse() 同形的 {period: {...}}; 一个 period 全空就不要。"""
    out = {}
    for it in items or []:
        p = it.get("period") if isinstance(it, dict) else None
        if p not in PERIODS:
            continue
        rev, trend, est = ({str(k).lower(): v for k, v in (it.get(sec) or {}).items()}
                           for sec in ("epsRevisions", "epsTrend", "earningsEstimate"))
        d = dict.fromkeys(PERIOD_KEYS)
        d.update({k: _raw(trend.get(src)) for k, src in TREND_FIELDS})
        d.update({k: _int(_raw(rev.get(src))) for k, src in REV_FIELDS})
        d["n"] = _int(_raw(est.get("numberofanalysts")))
        if any(v is not None for v in d.values()):
            out[p] = d
    return out


def one_time_flag(data: dict) -> dict | None:
    """本财年 (0y) 一致预期 90 天跳幅远大于下财年 (+1y) -> {j0, j1}, 否则 None.

    判据照抄 sec-filing-downloader 的 ref_table.one_time_warning (不跨仓库
    import)。校准: AMZN 2026 Anthropic 重估后本财年 90 天 +41~48%、下财年只
    +5% —— 真实的基本面上修会同时抬两年 (下财年通常抬得更多), 只抬本财年的
    大跳变几乎只有一次性项目一种解释。四个值必须全为正且有限: 亏损或穿零时
    百分比没有方向 (坑 3), 会触发一条完全反向的提示。"""
    t0, t1 = data.get("0y") or {}, data.get("+1y") or {}
    vals = [t0.get("eps_90d"), t0.get("eps"), t1.get("eps_90d"), t1.get("eps")]
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0
               for v in vals):
        return None
    j0 = t0["eps"] / t0["eps_90d"] - 1
    j1 = t1["eps"] / t1["eps_90d"] - 1
    if abs(j0) > ONE_TIME_MIN_JUMP and abs(j0) > ONE_TIME_RATIO * abs(j1):
        return {"j0": j0, "j1": j1}
    return None


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

def _yf_ticker(sym: str):
    import yfinance as yf
    return yf.Ticker(sym)


def _fetch_normalized(tk, sym: str) -> list:
    """Normalized 口径的 trend 列表。走 yfinance 私有接口 (见模块文档), 和公开的
    eps_trend 共用同一个会话/crumb; 接口改了就在这里抛异常, 由 fetch_one 兜住。"""
    from yfinance.scrapers.quote import _QUOTE_SUMMARY_URL_
    r = tk._data.get_raw_json(
        f"{_QUOTE_SUMMARY_URL_}/{sym}",
        params={"modules": NORMALIZED_MODULE, "formatted": "false",
                "symbol": sym, "corsDomain": "finance.yahoo.com"})
    res = ((r or {}).get("quoteSummary") or {}).get("result") or []
    return ((res[0] if res else {}).get(NORMALIZED_MODULE) or {}).get("trend") or []


def fetch_one(sym: str, ticker_factory=_yf_ticker,
              normalized_fetch=_fetch_normalized) -> dict:
    """一只个股 -> {"data": 默认口径, "normalized": Normalized 口径 | None,
    "normalized_error": 原因 | None}。

    默认口径取不到抛异常, 空表也算 (坑 1)。Normalized 单独降级: 它只进快照,
    取不到只记原因, 不影响默认那套和报告。"""
    tk = ticker_factory(sym)
    rev, trend = tk.eps_revisions, tk.eps_trend
    try:
        est = tk.earnings_estimate      # 同一次请求的缓存; 家数只进快照
    except Exception:                   # noqa: BLE001
        est = None
    data = parse(rev, trend, est)
    if not data:
        raise EmptyData("Yahoo 返回空表")
    out = {"data": data, "normalized": None, "normalized_error": None}
    try:
        norm = parse_trend_json(normalized_fetch(tk, sym))
        if not norm:
            raise EmptyData("Yahoo 返回空表")
        out["normalized"] = norm
    except Exception as e:              # noqa: BLE001
        out["normalized_error"] = _why(e)
    return out


def _why(e: Exception) -> str:
    if isinstance(e, EmptyData):
        return str(e)
    msg = str(e).splitlines()[0][:80] if str(e) else ""
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def run(symbols: list[str], workers: int = 4, fetch=fetch_one) -> dict:
    """并发取数 -> {"by_symbol": {sym: {"data", "normalized", "normalized_error",
    "flag"} | {"error"}}}。只传个股 (ETF/指数由调用方按 kind 剔掉); 单只失败只记
    error, 不抛。一次性判据只看默认口径 —— 报告显示的就是它。"""
    def one(sym):
        try:
            got = fetch(sym)
        except Exception as e:          # noqa: BLE001
            return {"error": _why(e)}
        return {**got, "flag": one_time_flag(got["data"])}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        infos = list(pool.map(one, symbols))
    return {"by_symbol": dict(zip(symbols, infos))}


# --------------------------------------------------------------------------
# Report lines
# --------------------------------------------------------------------------

def consensus_change(p: dict, ago_key: str = "eps_30d") -> str | None:
    """一致预期较 N 天前: 两头都为正 -> 百分比, 否则 -> 绝对值 a→b (坑 3)。"""
    cur, ago = p.get("eps"), p.get(ago_key)
    if cur is None or ago is None:
        return None
    if cur > 0 and ago > 0:
        return f"{(cur / ago - 1) * 100:+.1f}%"
    return f"{ago:.2f}→{cur:.2f}"


def line(sym: str, info: dict) -> str | None:
    """一只个股一行 (下财年); 缺下财年的家数 -> None (调用方记进失败提示)。"""
    p = (info.get("data") or {}).get(SHOW_PERIOD) or {}
    if p.get("up30") is None or p.get("down30") is None:
        return None
    cover = f" {p['n']} 家" if p.get("n") else ""
    s = f"- **{sym}** 下财年{cover}: 30天 ↑{p['up30']}/↓{p['down30']}"
    if p.get("up7") is not None and p.get("down7") is not None:
        s += f" (7天 ↑{p['up7']}/↓{p['down7']})"
    chg = consensus_change(p)
    if chg:
        s += f"，一致预期 30 天 {chg}"
    f = info.get("flag")
    if f:
        # 判据对上下两个方向都成立: 一次性亏损 (减值) 进了 GAAP 口径同样只压本财年
        what = ("上调疑似一次性收益驱动" if f["j0"] > 0
                else "下调疑似一次性损失驱动")
        s += (f" · ⚠️ 本财年一致预期 90 天 {f['j0']:+.0%}、下财年仅 "
              f"{f['j1']:+.0%} — {what}，看下财年")
    return s


def _net(info: dict) -> int:
    p = info["data"][SHOW_PERIOD]
    return p["up30"] - p["down30"]


def report_block(items: list[tuple[str, dict | None]]) -> list[str]:
    """[(symbol, info)] -> 报告段落: 标题、每只个股一行 (按 30 天净上调家数
    从多到少)、失败合并成一条 ⚠️、说明。info=None (ETF/指数) 静默跳过。"""
    body, failed, norm_failed = [], {}, {}
    for sym, info in items:
        if info is None:
            continue
        if info.get("error"):
            failed.setdefault(info["error"], []).append(sym)
            continue
        if info.get("normalized_error"):
            norm_failed.setdefault(info["normalized_error"], []).append(sym)
        text = line(sym, info)
        if text is None:
            failed.setdefault("缺下财年修正数据", []).append(sym)
            continue
        body.append((_net(info), text))
    lines = ["## 分析师预期修正 (下财年 EPS, Yahoo)", ""]
    lines += [t for _, t in sorted(body, key=lambda x: -x[0])]
    if failed:
        why = "; ".join(f"{', '.join(syms)} ({w})" for w, syms in failed.items())
        lines.append(f"- ⚠️ 分析师修正取不到: {why} — 只缺这一段提示, 下次扫描重试")
    if norm_failed:
        # 快照是以后回测的唯一来源, 少存一套不能只躺在 cron log 里
        why = "; ".join(f"{', '.join(syms)} ({w})" for w, syms in norm_failed.items())
        lines.append(f"- ⚠️ Normalized 口径取不到: {why} — 上面照常, 只是快照里"
                     "缺这一套; 天天出现 = Yahoo/yfinance 改了接口")
    if not (body or failed):
        lines.append("- 无 (没有个股)")
    lines += ["",
              "> ↑/↓ = 近 30 天 (括号内 7 天) 上调/下调下财年 EPS 预期的分析师家数; "
              "一致预期 = 下财年 EPS 一致预期较 30 天前 (亏损股报绝对值)。"
              "⚠️ = 本财年 90 天跳幅远大于下财年, 疑似一次性项目进了 GAAP 口径预期。"
              "只做提示, 未经回测, 不影响任何信号和票据。",
              ""]
    return lines


# --------------------------------------------------------------------------
# Snapshot (以后回测的唯一来源 —— 雅虎没有历史)
# --------------------------------------------------------------------------

def snapshot_rows(by_symbol: dict, day: str) -> list[dict]:
    """run() 的结果 -> 快照行, 两套口径各一组列。默认口径取数失败的不写 (缺一天
    就是缺一天, 不补 0); 只有 Normalized 失败的照写, norm_ 列留空。"""
    rows = []
    for sym, info in by_symbol.items():
        data = (info or {}).get("data")
        if not data:
            continue
        row = {"date": day, "symbol": sym}
        for prefix, src in (("", data), (NORM_PREFIX, info.get("normalized") or {})):
            for p in PERIODS:
                for k in PERIOD_KEYS:
                    row[f"{prefix}{p}_{k}"] = (src.get(p) or {}).get(k)
        rows.append(row)
    return rows


def append_snapshot(rows: list[dict], path: Path) -> int:
    """追加到快照 CSV -> 新写入的行数。(date, symbol) 已有的跳过 (同 iv_history)。

    只增不改: 表头和 SNAPSHOT_FIELDS 一致就直接追加; 不一致 (以后加了列) 就按
    新旧列的并集整表重写 (临时文件 + 原子替换), 旧行缺的列留空。"""
    header, old = None, []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            rd = csv.DictReader(f)
            header = rd.fieldnames
            old = list(rd)
    have = {(r.get("date"), r.get("symbol")) for r in old}
    new = []
    for r in rows:
        key = (r["date"], r["symbol"])
        if key not in have:
            have.add(key)
            new.append(r)
    if not new:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if header == SNAPSHOT_FIELDS:
        with path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, SNAPSHOT_FIELDS, lineterminator="\n").writerows(new)
    else:
        fields = list(header or []) + [c for c in SNAPSHOT_FIELDS
                                       if c not in (header or [])]
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fields, lineterminator="\n",
                               extrasaction="ignore")
            w.writeheader()
            w.writerows(old)
            w.writerows(new)
        os.replace(tmp, path)
    return len(new)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    import logging
    import tomllib
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)
    base = Path(__file__).resolve().parent
    cfg = tomllib.loads((base / "watchlist.toml").read_text(encoding="utf-8"))
    kinds = {s: v.get("kind", "stock") for s, v in cfg["tickers"].items()}
    # 同扫描器: watchlist 里标了 etf/index 的不查 (坑 1); 不在 watchlist 的照查
    symbols = [s for s in ([a.upper() for a in argv] or kinds)
               if kinds.get(s, "stock") == "stock"]
    res = run(symbols)
    print("\n".join(report_block(list(res["by_symbol"].items()))))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
