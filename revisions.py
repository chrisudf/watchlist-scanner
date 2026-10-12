#!/usr/bin/env python3
"""分析师预期修正 (Yahoo earningsTrend) — 取数 / 解析 / 一次性判据 / 快照.

只做提示, 不进状态机/门控/票据 (2026-10-12 定)。雅虎只给当下快照、没有历史,
回测不了; 这个仓库的规矩是指标先回测再进门控 (MACD/KDJ/布林都被回测否掉过)。
所以先每天收盘存一份快照 (data/eps_revisions.csv), 攒满约 120 个交易日再验证
有没有预测力 (todo.md #10)。

数据: quoteSummary 的 earningsTrend 模块。每个期间 (0q/+1q/0y/+1y = 本季/下季/
本财年/下财年) 一项, 四个小节都存进快照:
  - epsRevisions: 近 7/30 天上调/下调 EPS 预期的分析师家数
  - epsTrend: 一致预期 EPS 和它 7/30/60/90 天前的值 (最多回溯 90 天)
  - earningsEstimate: 覆盖家数、最低/最高 (分歧度)、去年 EPS、增长
  - revenueEstimate: 营收的家数/平均/最低/最高/去年/增长 —— 营收修正 Yahoo 不给
    历史, 只能靠自己每天存
  - endDate: 这个期间对应的财年/季度结束日。页面上不显示, yfinance 的公开接口也丢了
    它, 但回测离不开: 公司公布年报后 0y/+1y 的标签整体换到下一年, 没有结束日就会
    把两个财年接成一条线; 算"未来 12 个月"加权也要知道本财年还剩几个月。

报告看下财年 (+1y): 本财年会被一次性项目污染 (见 one_time_flag), 估值也看下财年。
注意"下财年"离现在多远因公司而异 (12 月财年 15 个月, NVDA 16 个月, MSFT/COHR 21
个月) —— 哪个期间/加权方式更有预测力留给回测。

两套口径 (2026-10-12 实测, 快照两套都存, 报告只显示默认那套):
  - 默认 (earningsTrend): Yahoo 页面切换钮上标 "GAAP" 的那套, 与 yfinance 的
    eps_revisions/eps_trend 逐格对得上。标签别按字面理解 —— NVDA 两套数字完全相同,
    而它的 GAAP 与 non-GAAP 本应差一块股权激励; 更像"多数分析师用的口径"的大池子。
    不报 non-GAAP 的公司 (AMZN/META) 大池子就是 GAAP, 一次性项目会混进去。
  - Normalized (earningsTrendNonGaap): 未公开的模块, yfinance 的 valid_modules 里
    没有。营收和 endDate 两个模块逐字相同, 快照只存一份; EPS 部分存两份。
  - watchlist 个股 + MCD 共 15 只里 10 只两套一样; RKLB 下财年默认 -0.07 / ↑2↓6、
    Normalized +0.06 / ↑5↓0, 修正方向相反; META 54 家 vs 15 家; AMZN 本财年 51 家
    (含 Anthropic) vs 4 家。

取数: 一个请求同时拿两个模块的原始 JSON (yfinance 私有接口 _data.get_raw_json)。
私有接口 yfinance 一改就会失效, 所以失效时默认口径退回 yfinance 公开接口
(eps_revisions / eps_trend / earnings_estimate / revenue_estimate), 只缺 endDate
和 Normalized, 报告照常并出一行 ⚠️; 快照的 src 列记走的是哪条路 (raw / yf)。

快照池 (10/12 用户拍板扩到标普 100): watchlist 个股之外, snapshot_universe.txt 里的
股票也每天存一行, 只进快照不进报告 —— 14 只 × 120 天测不出 1% 量级的效应。这一批在
尾盘报告和邮件发出之后才取 (多约 85 个请求, 不拖慢报告), 结果记进状态文件
(eps_revisions_status.json), 有缺口由下一份报告出 ⚠️ (status_warning)。

四个实测坑 (2026-10-12):
  1. ETF/指数没有这个模块, Yahoo 回 404, yfinance 公开接口默认吞掉异常、返回空表。
     所以 ETF/指数按 kind 直接不查 (静默)。个股拿到空表多半是取数失败被吞了
     (watchlist 14 只个股全有覆盖), 要写进失败提示, 不能当成"没有修正"。
  2. 字段名大小写不统一: upLast7days 是小写 d, downLast7Days 是大写 D —— 按小写比对。
  3. 亏损股 (RKLB 两年都亏, SPCX 本财年 90 天前还是负数) 的 EPS 为负或穿零, 百分比
     没有方向意义 (-1.00 → -0.50 是减亏却算出 -50%), 改报绝对值 a→b。
  4. 币种: TSM 的 EPS 是美元 (每 ADR), 营收却是新台币 (7.4 万亿) —— 快照存
     eps_ccy / rev_ccy 两列, 别拿营收去和美元数比。

命令行 (手工看一眼; 不写快照):
    .venv/bin/python revisions.py [MCD NVDA ...]
不带代码 = watchlist.toml 里全部个股。
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

PERIODS = ("0q", "+1q", "0y", "+1y")
SHOW_PERIOD = "+1y"
DEFAULT_MODULE = "earningsTrend"
NORMALIZED_MODULE = "earningsTrendNonGaap"
NORM_PREFIX = "norm_"

# (我们的键, Yahoo 字段名小写) —— 小写比对见坑 2。四个小节在原始 JSON 和 yfinance
# 公开接口的 DataFrame 里字段名相同, 两条路共用这张表
REV_FIELDS = (("up7", "uplast7days"), ("up30", "uplast30days"),
              ("down30", "downlast30days"), ("down7", "downlast7days"))
TREND_FIELDS = (("eps", "current"), ("eps_7d", "7daysago"),
                ("eps_30d", "30daysago"), ("eps_60d", "60daysago"),
                ("eps_90d", "90daysago"))
EST_FIELDS = (("n", "numberofanalysts"), ("eps_low", "low"), ("eps_high", "high"),
              ("eps_year_ago", "yearagoeps"), ("eps_growth", "growth"))
REVENUE_FIELDS = (("rev_n", "numberofanalysts"), ("rev_avg", "avg"),
                  ("rev_low", "low"), ("rev_high", "high"),
                  ("rev_year_ago", "yearagorevenue"), ("rev_growth", "growth"))
SECTIONS = (("epsRevisions", REV_FIELDS), ("epsTrend", TREND_FIELDS),
            ("earningsEstimate", EST_FIELDS), ("revenueEstimate", REVENUE_FIELDS))
INT_KEYS = frozenset({"up7", "up30", "down30", "down7", "n", "rev_n"})
# 分口径的 (两套各存一份) / 不分口径的 (两个模块逐字相同, 只存一份)
EPS_KEYS = ("up7", "up30", "down30", "down7", "n", "eps", "eps_7d", "eps_30d",
            "eps_60d", "eps_90d", "eps_low", "eps_high", "eps_year_ago", "eps_growth")
SHARED_KEYS = ("end", "rev_n", "rev_avg", "rev_low", "rev_high", "rev_year_ago",
               "rev_growth")
PERIOD_KEYS = EPS_KEYS + SHARED_KEYS

# 一次性判据, 数值照抄 sec-filing-downloader valuation/ref_table.py 的
# one_time_warning (pe_rank.py 的 one_time_flag 复用同一判据)
ONE_TIME_MIN_JUMP = 0.15    # 本财年 90 天变动的绝对值下限
ONE_TIME_RATIO = 2.5        # 且大于下财年变动的这么多倍

SNAPSHOT_NAME = "eps_revisions.csv"
# 快照的扩展股票池 (标普 100, 仓库根目录): watchlist 之外的只进快照、不进报告,
# 给回测加样本。取完把结果记进状态文件, 下一份报告据此提示缺口
UNIVERSE_NAME = "snapshot_universe.txt"
STATUS_NAME = "eps_revisions_status.json"
STATUS_STALE_DAYS = 5       # 最长休市间隔 (周五 → 周一假日后的周二) 是 4 个日历日
# 每行 = 一个 ET 交易日 × 一只个股。src = raw (私有接口, 全字段) / yf (退回公开接口,
# 缺 end 和 norm_ 列)。"{period}_{键}" = 默认口径 + 营收 + end; "norm_{period}_{键}"
# = Normalized 口径的 EPS 部分, 那天没取到就留空
SNAPSHOT_FIELDS = (["date", "symbol", "src", "eps_ccy", "rev_ccy"]
                   + [f"{p}_{k}" for p in PERIODS for k in SHARED_KEYS + EPS_KEYS]
                   + [f"{NORM_PREFIX}{p}_{k}" for p in PERIODS for k in EPS_KEYS])

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def _int(v: float | None) -> int | None:
    return None if v is None else int(round(v))


def _typed(key: str, v: float | None):
    return _int(v) if key in INT_KEYS else v


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
        out[p] = {k: _typed(k, _num(row[cols[src]]) if src in cols else None)
                  for k, src in fields}
    return out


def parse(rev_df, trend_df, est_df=None, revenue_df=None) -> dict[str, dict]:
    """yfinance 公开接口的四张表 -> {period: {PERIOD_KEYS...}} (end 恒为 None,
    公开接口没有)。四张表都没有这个 period = 不要; 全空 = {}。"""
    parts = [_rows(df, fields) for df, fields in
             ((rev_df, REV_FIELDS), (trend_df, TREND_FIELDS),
              (est_df, EST_FIELDS), (revenue_df, REVENUE_FIELDS))]
    out = {}
    for p in PERIODS:
        if not any(p in part for part in parts):
            continue
        d = dict.fromkeys(PERIOD_KEYS)
        for part in parts:
            d.update(part.get(p, {}))
        out[p] = d
    return out


def _raw(v) -> float | None:
    """quoteSummary 的单元格是 {"raw": 2, "fmt": "2"} (formatted=false 也照样包一层),
    缺值是 {}。"""
    return _num(v.get("raw") if isinstance(v, dict) else v)


def parse_trend_json(items) -> dict[str, dict]:
    """quoteSummary 原始 JSON 的 trend 列表 (两个模块同一结构) -> 与 parse() 同形的
    {period: {...}}, 多一个 end; 一个 period 全空就不要。"""
    out = {}
    for it in items or []:
        p = it.get("period") if isinstance(it, dict) else None
        if p not in PERIODS:
            continue
        d = dict.fromkeys(PERIOD_KEYS)
        for sec, fields in SECTIONS:
            cells = {str(k).lower(): v for k, v in (it.get(sec) or {}).items()}
            d.update({k: _typed(k, _raw(cells.get(src))) for k, src in fields})
        end = it.get("endDate")
        d["end"] = end if isinstance(end, str) and _ISO_DATE.match(end) else None
        if any(v is not None for v in d.values()):
            out[p] = d
    return out


def _ccy_json(items) -> dict:
    """EPS 和营收各自的币种 (坑 4); 取第一个期间的。"""
    it = next((x for x in items or [] if isinstance(x, dict)), {})
    return {"eps": ((it.get("earningsEstimate") or {}).get("earningsCurrency")
                    or (it.get("epsTrend") or {}).get("epsTrendCurrency")),
            "rev": (it.get("revenueEstimate") or {}).get("revenueCurrency")}


def _ccy_df(est_df, revenue_df, trend_df=None) -> dict:
    """公开接口把币种放在 DataFrame 的 currency 列。家数表是可选的, 它缺了 EPS 币种
    就从趋势表拿 —— 退回公开接口本来就是降级的日子, 别再把币种也丢了。"""
    def first(df):
        if df is None or getattr(df, "empty", True) or "currency" not in df.columns:
            return None
        v = df["currency"].dropna()
        return str(v.iloc[0]) if len(v) else None
    return {"eps": first(est_df) or first(trend_df), "rev": first(revenue_df)}


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


def _fetch_raw(tk, sym: str) -> dict[str, list]:
    """一个请求拿两个模块的原始 trend 列表 -> {模块名: trend}。走 yfinance 私有
    接口 (见模块文档), 和公开接口共用同一个会话/crumb; 接口改了就在这里抛异常,
    由 fetch_one 兜住。直接调 get_raw_json 不会像公开接口那样把 HTTP 错误吞成空表。"""
    from yfinance.scrapers.quote import _QUOTE_SUMMARY_URL_
    r = tk._data.get_raw_json(
        f"{_QUOTE_SUMMARY_URL_}/{sym}",
        params={"modules": f"{DEFAULT_MODULE},{NORMALIZED_MODULE}",
                "formatted": "false", "symbol": sym,
                "corsDomain": "finance.yahoo.com"})
    res = ((r or {}).get("quoteSummary") or {}).get("result") or []
    res0 = res[0] if res else {}
    return {m: (res0.get(m) or {}).get("trend") or []
            for m in (DEFAULT_MODULE, NORMALIZED_MODULE)}


def _fetch_public(tk) -> tuple[dict, dict]:
    """退路: yfinance 公开接口, 只有默认口径、没有 endDate。四张表共用一次请求的缓存;
    家数/营收两张表单独出错不影响修正数据。"""
    rev, trend = tk.eps_revisions, tk.eps_trend
    extra = []
    for name in ("earnings_estimate", "revenue_estimate"):
        try:
            extra.append(getattr(tk, name))
        except Exception:               # noqa: BLE001
            extra.append(None)
    return parse(rev, trend, *extra), _ccy_df(*extra, trend_df=trend)


def fetch_one(sym: str, ticker_factory=_yf_ticker, raw_fetch=_fetch_raw) -> dict:
    """一只个股 -> {"data": 默认口径, "ccy": {eps, rev}, "src": "raw"|"yf",
    "raw_error": 退回公开接口的原因 | None,
    "normalized": Normalized 口径 | None, "normalized_error": 原因 | None}。

    先走私有接口 (一个请求两套口径全字段); 它坏了或没给默认模块, 默认口径退回
    公开接口。默认口径两条路都拿不到就抛异常, 空表也算 (坑 1)。Normalized 只进
    快照, 取不到只记原因。"""
    tk = ticker_factory(sym)
    try:
        mods, raw_err = raw_fetch(tk, sym), None
    except Exception as e:              # noqa: BLE001
        mods, raw_err = {}, _why(e)
    out = {"data": parse_trend_json(mods.get(DEFAULT_MODULE)),
           "ccy": _ccy_json(mods.get(DEFAULT_MODULE)), "src": "raw",
           "raw_error": None, "normalized": None, "normalized_error": None}
    if not out["data"]:
        out["data"], out["ccy"] = _fetch_public(tk)
        out["src"] = "yf"
        out["raw_error"] = raw_err or "私有接口没返回默认口径"
        if not out["data"]:
            raise EmptyData("Yahoo 返回空表")
    norm = parse_trend_json(mods.get(NORMALIZED_MODULE))
    if norm:
        out["normalized"] = norm
    else:
        out["normalized_error"] = raw_err or "Yahoo 返回空表"
    return out


def _why(e: Exception) -> str:
    if isinstance(e, EmptyData):
        return str(e)
    msg = str(e).splitlines()[0][:80] if str(e) else ""
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def run(symbols: list[str], workers: int = 4, fetch=fetch_one) -> dict:
    """并发取数 -> {"by_symbol": {sym: {fetch_one 的键 + "flag"} | {"error"}}}。
    只传个股 (ETF/指数由调用方按 kind 剔掉); 单只失败只记 error, 不抛。一次性
    判据只看默认口径 —— 报告显示的就是它。"""
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


def breadth(p: dict) -> float | None:
    """净上调比例 = (30 天上调 − 下调) ÷ 覆盖家数。原始家数会偏向覆盖多的票:
    SOFI 9 家里 ↑2/↓0 (+22%) 比 AMZN 53 家里 ↑4/↓0 (+8%) 强, 按家数却排在后面。"""
    n, up, down = p.get("n"), p.get("up30"), p.get("down30")
    if not n or up is None or down is None:
        return None
    return (up - down) / n


def line(sym: str, info: dict, earnings: str | None = None) -> str | None:
    """一只个股一行 (下财年); 缺下财年的家数 -> None (调用方记进失败提示)。
    earnings = 下次财报日 (ISO), 没有就不写。"""
    p = (info.get("data") or {}).get(SHOW_PERIOD) or {}
    if p.get("up30") is None or p.get("down30") is None:
        return None
    cover = f" {p['n']} 家" if p.get("n") else ""
    s = f"- **{sym}** 下财年{cover}: 30天 ↑{p['up30']}/↓{p['down30']}"
    b = breadth(p)
    if b is not None:
        s += f" 净 {b * 100:+.0f}%"
    if p.get("up7") is not None and p.get("down7") is not None:
        s += f" (7天 ↑{p['up7']}/↓{p['down7']})"
    # 30 天之外再给 90 天: 修正扎堆在财报后一两周, 财报季之间 30 天窗口会整个漏掉
    # 上一次财报后的那波 (10/12: NVDA 30 天 +2.5% 而 90 天 +25%, TSLA +1.1% 而 −15%)
    chg = [f"{days} 天 {c}" for days, key in (("30", "eps_30d"), ("90", "eps_90d"))
           if (c := consensus_change(p, key))]
    if chg:
        s += "，一致预期 " + "、".join(chg)
    if earnings and _ISO_DATE.match(earnings):
        s += f" · 财报 {earnings[5:]}"
    f = info.get("flag")
    if f:
        # 判据对上下两个方向都成立: 一次性亏损 (减值) 进了 GAAP 口径同样只压本财年
        what = ("上调疑似一次性收益驱动" if f["j0"] > 0
                else "下调疑似一次性损失驱动")
        s += (f" · ⚠️ 本财年一致预期 90 天 {f['j0']:+.0%}、下财年仅 "
              f"{f['j1']:+.0%} — {what}，看下财年")
    return s


def _sort_key(info: dict) -> tuple:
    p = info["data"][SHOW_PERIOD]
    b = breadth(p)
    return (b if b is not None else 0.0, p["up30"] - p["down30"])


def report_block(items: list[tuple[str, dict | None]],
                 earnings: dict[str, str] | None = None,
                 extra_warnings: list[str] | None = None) -> list[str]:
    """[(symbol, info)] -> 报告段落: 标题、每只个股一行 (按净上调比例从高到低)、
    失败各合并成一条 ⚠️、说明。info=None (ETF/指数) 静默跳过。
    earnings = {symbol: 下次财报日}, 可缺; extra_warnings = 扩展快照的缺口提示
    (status_warning), 排在本次的失败提示后面。"""
    earnings = earnings or {}
    body, failed, raw_failed, norm_failed = [], {}, {}, {}
    for sym, info in items:
        if info is None:
            continue
        if info.get("error"):
            failed.setdefault(info["error"], []).append(sym)
            continue
        if info.get("src") == "yf":
            raw_failed.setdefault(info.get("raw_error") or "原因不明", []).append(sym)
        elif info.get("normalized_error"):
            norm_failed.setdefault(info["normalized_error"], []).append(sym)
        text = line(sym, info, earnings.get(sym))
        if text is None:
            failed.setdefault("缺下财年修正数据", []).append(sym)
            continue
        body.append((_sort_key(info), text))

    def join(groups):
        return "; ".join(f"{', '.join(syms)} ({w})" for w, syms in groups.items())

    lines = ["## 分析师预期修正 (下财年 EPS, Yahoo)", ""]
    lines += [t for _, t in sorted(body, key=lambda x: x[0], reverse=True)]
    if failed:
        lines.append(f"- ⚠️ 分析师修正取不到: {join(failed)} — 只缺这一段提示, "
                     "下次扫描重试")
    # 快照是以后回测的唯一来源, 少存一部分不能只躺在 cron log 里
    if raw_failed:
        lines.append(f"- ⚠️ 私有接口取不到: {join(raw_failed)} — 上面照常 (退回公开"
                     "接口), 快照缺 Normalized 和财年结束日; 天天出现 = Yahoo/yfinance "
                     "改了接口")
    if norm_failed:
        lines.append(f"- ⚠️ Normalized 口径取不到: {join(norm_failed)} — 上面照常, "
                     "只是快照里缺这一套")
    lines += extra_warnings or []
    if not (body or failed):
        lines.append("- 无 (没有个股)")
    lines += ["",
              "> ↑/↓ = 近 30 天 (括号内 7 天) 上调/下调下财年 EPS 预期的分析师家数; "
              "净 = (上调−下调) ÷ 覆盖家数, 按它从高到低排; "
              "一致预期 = 下财年 EPS 一致预期较 30 / 90 天前 (亏损股报绝对值); "
              "财报 = 下次财报日 —— 修正多扎堆在财报后一两周, 刚发完财报的票家数"
              "天然多, 横比时留意。"
              "⚠️ = 本财年 90 天跳幅远大于下财年, 疑似一次性项目进了 GAAP 口径预期。"
              "只做提示, 未经回测, 不影响任何信号和票据。",
              ""]
    return lines


# --------------------------------------------------------------------------
# Snapshot (以后回测的唯一来源 —— 雅虎没有历史)
# --------------------------------------------------------------------------

def snapshot_rows(by_symbol: dict, day: str) -> list[dict]:
    """run() 的结果 -> 快照行。默认口径取数失败的不写 (缺一天就是缺一天, 不补 0);
    只有 Normalized 失败的照写, norm_ 列留空。"""
    rows = []
    for sym, info in by_symbol.items():
        data = (info or {}).get("data")
        if not data:
            continue
        ccy = info.get("ccy") or {}
        row = {"date": day, "symbol": sym, "src": info.get("src"),
               "eps_ccy": ccy.get("eps"), "rev_ccy": ccy.get("rev")}
        for p in PERIODS:
            for k in SHARED_KEYS + EPS_KEYS:
                row[f"{p}_{k}"] = (data.get(p) or {}).get(k)
        norm = info.get("normalized") or {}
        for p in PERIODS:
            for k in EPS_KEYS:
                row[f"{NORM_PREFIX}{p}_{k}"] = (norm.get(p) or {}).get(k)
        rows.append(row)
    return rows


def _snapshot_keys(path: Path) -> tuple[list[str] | None, set]:
    """-> (表头, 已有的 (date, symbol))。逐行流式读, 只留两列 —— 文件会长到
    180 天 × 106 只 ≈ 1.9 万行 × 145 列, 整行读进内存要上百 MB, 尾盘扫描一天还
    调两次 (watchlist 一次、扩展池一次)。"""
    if not path.exists():
        return None, set()
    with path.open(newline="", encoding="utf-8") as f:
        rd = csv.reader(f)
        header = next(rd, None)
        if not header or "date" not in header or "symbol" not in header:
            return header, set()
        i, j = header.index("date"), header.index("symbol")
        return header, {(r[i], r[j]) for r in rd if len(r) > max(i, j)}


def append_snapshot(rows: list[dict], path: Path) -> int:
    """追加到快照 CSV -> 新写入的行数。(date, symbol) 已有的跳过 (同 iv_history)。

    只增不改: 表头和 SNAPSHOT_FIELDS 一致就直接追加 (平常都走这条, 只读两列);
    不一致 (以后加了列) 才整表读进来, 按新旧列的并集重写 (临时文件 + 原子替换),
    旧行缺的列留空。"""
    header, have = _snapshot_keys(path)
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
        old = []
        if header:
            with path.open(newline="", encoding="utf-8") as f:
                old = list(csv.DictReader(f))
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


def load_universe(path: Path) -> list[str]:
    """扩展股票池文件 -> 代码列表 (大写、去重保序); # 后是注释, 空白或逗号分隔。
    文件不在 = []。"""
    if not path.exists():
        return []
    out = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        for sym in re.split(r"[\s,]+", raw_line.split("#", 1)[0].strip()):
            sym = sym.upper()
            if sym and sym not in out:
                out.append(sym)
    return out


def write_status(path: Path, day: str, wanted: list[str], by_symbol: dict,
                 crash: str | None = None) -> None:
    """记下扩展快照这一轮存到了几只。它在报告发出之后才跑, 缺口只能留给下一份报告
    说 (status_warning) —— 快照是以后回测的唯一来源, 缺了不能只躺在 cron log 里。"""
    failed: dict[str, list[str]] = {}
    degraded: dict[str, list[str]] = {}     # 存了, 但退回公开接口: 缺 end 和 Normalized
    for sym in wanted:
        info = by_symbol.get(sym) or {}
        if not info.get("data"):
            failed.setdefault(info.get("error") or crash or "没取到", []).append(sym)
        elif info.get("src") == "yf":
            degraded.setdefault(info.get("raw_error") or "原因不明", []).append(sym)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"date": day, "wanted": len(wanted),
                               "got": len(wanted) - sum(map(len, failed.values())),
                               "failed": failed, "degraded": degraded},
                              ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)


def status_warning(path: Path, today: date) -> str | None:
    """上一轮扩展快照有缺口 / 太久没跑 -> 一行 ⚠️; 没有状态文件 (还没跑过) = None。"""
    try:
        st = json.loads(path.read_text(encoding="utf-8"))
        day = date.fromisoformat(st["date"])
        wanted, got = int(st["wanted"]), int(st["got"])
        failed = st.get("failed") or {}
        degraded = st.get("degraded") or {}
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as e:
        return f"- ⚠️ 扩展快照状态文件读不了 ({type(e).__name__}) — 查 data/{STATUS_NAME}"
    if (today - day).days > STATUS_STALE_DAYS:
        return (f"- ⚠️ 扩展快照 (标普 100) 最近一次是 {day.isoformat()}, 之后没跑 — "
                "回测样本在断档, 查尾盘扫描的 cron log")
    if got >= wanted and not degraded:
        return None

    def fmt(groups):
        parts = []
        for why, syms in groups.items():
            shown = ", ".join(syms[:6]) + (f" 等 {len(syms)} 只" if len(syms) > 6 else "")
            parts.append(f"{shown} ({why})")
        return "; ".join(parts)
    segs = []
    if got < wanted:
        segs.append(f"只存到 {got}/{wanted} 只: {fmt(failed)}")
    if degraded:
        n = sum(map(len, degraded.values()))
        segs.append(f"{n} 只退回公开接口、缺财年结束日和 Normalized: {fmt(degraded)}")
    return (f"- ⚠️ 扩展快照 (标普 100) {day.isoformat()} {'；'.join(segs)} — "
            "报告不受影响, 回测样本这一天不全")


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
