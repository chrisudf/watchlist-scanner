#!/usr/bin/env python3
"""空头面 (FINRA 空头股数 + IBKR 借券费) — 取数 / 解析 / 旗标 / 报告段 / 快照.

只做提示, 不进状态机/门控/票据 (2026-10-28 定)。两个源, 各自失败互不影响:

  - FINRA 空头股数 (consolidated short interest): 每月两期 (月中、月底结算日),
    结算日后约 8 个交易日公布 (10/15 那期 10/28 能取到)。报告看**回补天数**
    (空头股数 ÷ 日均成交量) 和较上期变化, 不算占流通股比例 —— yfinance 给 ETF
    的份额是旧数 (10/25 QQQ 给 3.93 亿, 资产 ÷ 净值约 7.08 亿)。公开 API 不用
    key; 按代码过滤时**不能带排序** (报错要求 settlementDate 用 EQUAL), 所以用
    domainFilters 一次拿全部代码、dateRangeFilters 圈近 45 天, 在本地取最新一期。
    没有匹配记录时 FINRA 回的是**空响应体**, 不是 []。
  - IBKR 借券费 + 可借股数: 公共 FTP 上的 usa.txt (约 1.8MB, 约 2 万行, 几分钟
    一更), `|` 分隔, 表头 #SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI。
    首行 #BOF|2026.10.28|01:18:49 是美东时间 (10/28 实测比对过)。可借股数超过
    1000 万写成 ">10000000"; 难借券的 REBATERATE 是负数。**主机是 ftp2** ——
    ftp3 在 2026-10-28 本机和 droplet 都连不上。

为什么只做提示: 借券费高的票后续收益明显偏差 (Drechsler & Drechsler 2014 等),
回补天数高的也偏差 (Hong 等的 days-to-cover 研究), 但都是全市场横截面的结论;
watchlist 十几只大票 10/28 全是普通券 (0.25-0.43%), 测不出也用不上门控。
ETF/指数的空头含 AP 申赎和对冲盘, 只列数、不出旗标。

借券费的另一个用处: 扫描器原有的"持有成本异常"提示 (forward 反解现货 vs 日线
收盘背离 >1.5%) 只能说"常见于难借券, 下单前核对券商的借券费率"; 有了实际费率,
就能直接说背离是不是借券造成的 (carry_line)。

代码按原样匹配两边 (BRK.B 这类带点的代码 IBKR 写成 "BRK B", watchlist 现在没有)。

命令行 (手工看一眼; 不写快照):
    .venv/bin/python shortside.py [SOFI QQQ ...]
不带代码 = watchlist.toml 里全部标的。
"""
from __future__ import annotations

import csv
import json
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path

IBKR_URL = "ftp://shortstock:@ftp2.interactivebrokers.com/usa.txt"
IBKR_COLUMNS = ("SYM", "REBATERATE", "FEERATE", "AVAILABLE")
FINRA_URL = ("https://api.finra.org/data/group/otcMarket/name/"
             "consolidatedShortInterest")
FINRA_LOOKBACK_DAYS = 45     # 最新一期最老约 30 天 (半个月间隔 + 约 8 个交易日公布), 留余量
SI_STALE_DAYS = 35           # 最新结算日老于此 = FINRA 可能断更
BORROW_STALE_DAYS = 4        # IBKR 文件日期老于此 (周五晚 → 周一 + 一个假日) = 断更
GC_MAX_FEE = 0.5             # 费率不超过这个 (%/年) 就叫"普通券" —— IBKR 的普通券是 0.25%
SNAPSHOT_NAME = "borrow_history.csv"
SNAPSHOT_FIELDS = ["date", "symbol", "fee", "rebate", "avail", "avail_plus", "asof"]

NOTE = ("> 回补天数 = 空头股数 ÷ 日均成交量 (FINRA 每月两期, 结算日后约 8 个交易日公布), "
        "按它从高到低排; 括号内 = 空头股数较上一期。借券费 = IBKR 给自家客户的年化费率, "
        "普通券约 0.25%。⚠️ 只给个股: ETF/指数的空头含申赎和对冲盘, 只列数。"
        "只做提示, 未经回测, 不影响任何信号和票据。")


def _why(e: Exception) -> str:
    msg = str(e).splitlines()[0][:80] if str(e) else ""
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def shares_cn(n: int, plus: bool = False) -> str:
    """150000 -> "15万"; 2800000 -> "280万"; 2.2e8 -> "2.2亿"; plus -> 前面加 ">"。"""
    if n >= 100_000_000:
        s = f"{n / 1e8:.1f}".rstrip("0").rstrip(".") + "亿"
    elif n >= 10_000:
        s = f"{n / 1e4:.0f}万"
    else:
        s = f"{n:,}"
    return (">" if plus else "") + s


# --------------------------------------------------------------------------
# IBKR 借券费
# --------------------------------------------------------------------------

def parse_ibkr(text: str) -> tuple[dict[str, dict], str | None]:
    """usa.txt 全文 -> ({SYM: {"fee", "rebate", "avail", "avail_plus"}}, 文件时间
    "YYYY-MM-DD HH:MM" (美东) | None)。

    按表头的列名取值, 列顺序变了也不会读错列; 表头缺列或根本没有表头 = 格式变了,
    直接抛 (报告出一行 ⚠️), 不能静默读成一张空表。数字解析不了的行跳过。"""
    asof, cols, out = None, None, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#BOF"):
            parts = line.split("|")
            if len(parts) >= 3:
                asof = f"{parts[1].replace('.', '-')} {parts[2][:5]}"
            continue
        if line.startswith("#SYM"):
            cols = [c.strip().lstrip("#") for c in line.split("|")]
            missing = [c for c in IBKR_COLUMNS if c not in cols]
            if missing:
                raise ValueError(f"IBKR 文件表头缺列: {', '.join(missing)}")
            continue
        if line.startswith("#"):
            continue                                  # #EOF|行数
        if cols is None:
            raise ValueError("IBKR 文件没有表头行 (#SYM|...)")
        f = dict(zip(cols, (c.strip() for c in line.split("|"))))
        avail_s = f.get("AVAILABLE", "")
        try:
            fee, rebate = float(f["FEERATE"]), float(f["REBATERATE"])
            avail = int(avail_s.lstrip(">").replace(",", ""))
        except (KeyError, ValueError):
            continue
        if f.get("SYM"):
            out[f["SYM"]] = {"fee": fee, "rebate": rebate, "avail": avail,
                             "avail_plus": avail_s.startswith(">")}
    if cols is None:
        raise ValueError("IBKR 文件没有表头行 (#SYM|...)")
    return out, asof


def fetch_ibkr(timeout: float, opener=urllib.request.urlopen) -> tuple[dict, str | None]:
    with opener(IBKR_URL, timeout=timeout) as f:
        return parse_ibkr(f.read().decode("latin-1"))


# --------------------------------------------------------------------------
# FINRA 空头股数
# --------------------------------------------------------------------------

def finra_request(symbols: list[str], today: date,
                  lookback: int = FINRA_LOOKBACK_DAYS) -> dict:
    return {"limit": max(100, len(symbols) * 6),
            "domainFilters": [{"fieldName": "symbolCode", "values": sorted(symbols)}],
            "dateRangeFilters": [{"fieldName": "settlementDate",
                                  "startDate": (today - timedelta(days=lookback)).isoformat(),
                                  "endDate": today.isoformat()}]}


def _post_json(url: str, body: dict, timeout: float):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as f:
        raw = f.read()
    return json.loads(raw) if raw.strip() else []     # 没有匹配记录 = 空响应体


def fetch_finra(symbols: list[str], today: date, timeout: float,
                post=_post_json) -> list[dict]:
    got = post(FINRA_URL, finra_request(symbols, today), timeout)
    if isinstance(got, dict):                         # 200 但带错误体
        raise ValueError(got.get("message") or "FINRA 返回错误")
    return got


def parse_finra(records: list[dict]) -> dict[str, dict]:
    """FINRA 记录 -> {SYM: 最新一期 {"date", "si", "prev", "chg", "dtc", "split"}}。
    chg (%) 用两期股数自己算; 上期为 0 或带拆股标记时 = None (拆股前后的股数
    不可比, 变化率没有意义)。"""
    latest = {}
    for r in records:
        sym, d = r.get("symbolCode"), r.get("settlementDate")
        if not sym or not d or (sym in latest and latest[sym]["date"] >= d):
            continue
        si, prev = r.get("currentShortPositionQuantity"), r.get("previousShortPositionQuantity")
        split = bool(r.get("stockSplitFlag"))
        latest[sym] = {"date": d, "si": si, "prev": prev, "split": split,
                       "dtc": r.get("daysToCoverQuantity"),
                       "chg": ((si / prev - 1) * 100
                               if si is not None and prev and not split else None)}
    return latest


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def run(symbols: list[str], today: date, timeout: float = 30.0,
        ibkr=fetch_ibkr, finra=fetch_finra) -> dict:
    """两个源各取一次 -> {
        "by_symbol": {sym: {"borrow": {...} | None, "si": {...} | None}},
        "borrow_asof": IBKR 文件时间 | None, "si_date": 最新结算日 | None,
        "errors": {"borrow": 原因, "si": 原因}}  (只有失败的源才有键)。
    代码不在文件/记录里 = 对应字段 None; 一个源失败不影响另一个。"""
    meta = {"by_symbol": {s_: {"borrow": None, "si": None} for s_ in symbols},
            "borrow_asof": None, "si_date": None, "errors": {}}
    try:
        rows, meta["borrow_asof"] = ibkr(timeout)
        for s_ in symbols:
            meta["by_symbol"][s_]["borrow"] = rows.get(s_)
    except Exception as e:      # noqa: BLE001
        meta["errors"]["borrow"] = _why(e)
    try:
        si = parse_finra(finra(symbols, today, timeout))
        for s_ in symbols:
            meta["by_symbol"][s_]["si"] = si.get(s_)
        dates = [v["date"] for s_, v in si.items() if s_ in meta["by_symbol"]]
        meta["si_date"] = max(dates) if dates else None
    except Exception as e:      # noqa: BLE001
        meta["errors"]["si"] = _why(e)
    return meta


# --------------------------------------------------------------------------
# 旗标与提示行
# --------------------------------------------------------------------------

def borrow_flag(b: dict | None, s: dict) -> str | None:
    """难借券 (费率 >= short_fee_warn) 或可借很少 (< short_avail_warn 股) -> 原因; 否则 None。"""
    if not b:
        return None
    parts = []
    if b["fee"] >= s["short_fee_warn"]:
        parts.append(f"借券费 {b['fee']:.1f}%")
    if not b["avail_plus"] and b["avail"] < s["short_avail_warn"]:
        parts.append(f"可借只剩 {shares_cn(b['avail'])}股")
    return "、".join(parts) or None


def borrow_note(b: dict, why: str) -> str:
    """标的详情里的一行 (只在 borrow_flag 成立时)。"""
    return (f"⚠️ 难借券 (IBKR {why}): 做空拥挤 —— 期权按借券费压低了 forward, "
            "put 贵、call 便宜, 都不是白捡的; 历史上难借券的票后续收益偏差")


def carry_line(b: dict | None, s: dict) -> str:
    """接在扫描器"持有成本异常"提示后面的一行: 拿 IBKR 的实际费率说清背离是不是借券造成的。
    b=None = 代码不在 IBKR 文件里 (调用方保证 IBKR 文件本身取到了)。"""
    if b is None:
        return "↳ IBKR 借券文件里没有这个代码 —— 多半借不到, 背离可能就是做空受限"
    desc = (f"IBKR 借券费 {b['fee']:.2f}%, 可借 "
            f"{shares_cn(b['avail'], b['avail_plus'])}股")
    if b["fee"] >= s["short_fee_warn"]:
        return f"↳ {desc} —— 背离来自借券成本"
    return f"↳ {desc} —— 不是难借券, 这次背离另有原因 (股息/除权/报价), 先核对再下单"


def si_flag(v: dict | None, s: dict) -> str | None:
    """只给个股 (调用方按 kind 判断): 回补天数 >= short_dtc_warn 或空头较上期
    >= +short_chg_warn% -> 原因; 否则 None。"""
    if not v:
        return None
    parts = []
    if v.get("dtc") is not None and v["dtc"] >= s["short_dtc_warn"]:
        parts.append(f"回补 {v['dtc']:.1f} 天")
    if v.get("chg") is not None and v["chg"] >= s["short_chg_warn"]:
        parts.append(f"空头较上期 {v['chg']:+.0f}%")
    if not parts:
        return None
    return ("、".join(parts) + f" (FINRA {v['date'][5:]}) —— 空头拥挤: 历史上后续收益"
            "偏差, 但也容易被逼空急涨")


# --------------------------------------------------------------------------
# 报告段
# --------------------------------------------------------------------------

def _si_part(sym: str, v: dict) -> str:
    if v.get("dtc") is None:
        return f"{sym} —"
    tail = (" (拆股)" if v.get("split") else
            f" ({v['chg']:+.0f}%)" if v.get("chg") is not None else "")
    return f"{sym} {v['dtc']:.1f}{tail}"


def _stale(d: str | None, today: date, days: int) -> bool:
    try:
        return d is not None and (today - date.fromisoformat(d[:10])).days > days
    except ValueError:
        return False


def report_block(items: list[tuple[str, str, dict | None]], meta: dict, s: dict,
                 today: date) -> list[str]:
    """[(symbol, kind, info)] -> 报告段落: 回补天数一行 (全部标的, 从高到低)、
    借券费一行 (没亮旗标的汇总成区间)、⚠️ 旗标逐只、取数失败/缺记录各一条、说明。"""
    errors = meta.get("errors") or {}
    lines = ["## 空头面 (FINRA 空头股数 / IBKR 借券费)", ""]

    si_items = [(sym, kind, info["si"]) for sym, kind, info in items
                if info and info.get("si")]
    if si_items:
        si_items.sort(key=lambda x: -(x[2].get("dtc") or 0.0))
        d = meta.get("si_date")
        lines.append(f"- 回补天数 (FINRA {d[5:] if d else '?'} 结算, 括号内较上期): "
                     + " · ".join(_si_part(sym, v) for sym, _, v in si_items))
        if _stale(d, today, SI_STALE_DAYS):
            lines.append(f"- ⚠️ FINRA 最新一期是 {d}, 超过 {SI_STALE_DAYS} 天 —— 数据可能断更")

    b_items = [(sym, info["borrow"]) for sym, _, info in items
               if info and info.get("borrow")]
    flagged = [(sym, b, why) for sym, b in b_items if (why := borrow_flag(b, s))]
    calm = [(sym, b) for sym, b in b_items if not borrow_flag(b, s)]
    asof = meta.get("borrow_asof")
    when = f"IBKR {asof[5:]} ET" if asof else "IBKR"
    if calm:
        lo = f"{min(b['fee'] for _, b in calm):.2f}"
        hi_sym, hi = max(calm, key=lambda x: x[1]["fee"])
        rng = f"{lo}%" if f"{hi['fee']:.2f}" == lo else f"{lo}–{hi['fee']:.2f}%"
        what = ("都是普通券" if hi["fee"] <= GC_MAX_FEE else f"最高 {hi_sym}")
        tight = [(sym, b) for sym, b in calm if not b["avail_plus"]]
        if tight:
            t_sym, t = min(tight, key=lambda x: x[1]["avail"])
            avail = f"可借最少 {shares_cn(t['avail'])}股 ({t_sym})"
        else:
            avail = "可借都超过 1000万股"
        lead = "其余 " if flagged else ""
        lines.append(f"- 借券费 ({when}): {lead}{rng}, {what}; {avail}")
    for sym, b, why in flagged:
        lines.append(f"- ⚠️ **{sym}** 难借券: {why} —— 做空拥挤, put 贵、call 便宜都不是白捡的")
    if _stale(asof, today, BORROW_STALE_DAYS):
        lines.append(f"- ⚠️ IBKR 借券文件停在 {asof} —— 超过 {BORROW_STALE_DAYS} 天没更新")

    for sym, kind, v in si_items:
        if kind == "stock" and (why := si_flag(v, s)):
            lines.append(f"- ⚠️ **{sym}** {why}")

    for key, name in (("si", "FINRA 空头股数"), ("borrow", "IBKR 借券费")):
        if key in errors:
            lines.append(f"- ⚠️ {name}取不到 ({errors[key]}) —— 只缺这一项, 下次扫描重试")
    if "si" not in errors:
        miss = [sym for sym, _, info in items if not (info or {}).get("si")]
        if miss:
            lines.append(f"- FINRA 没有记录: {', '.join(miss)}")
    if "borrow" not in errors:
        miss = [sym for sym, _, info in items if not (info or {}).get("borrow")]
        if miss:
            lines.append(f"- IBKR 借券文件里没有: {', '.join(miss)} (多半借不到)")
    if not items:
        lines.append("- 无 (没有标的)")
    lines += ["", NOTE, ""]
    return lines


# --------------------------------------------------------------------------
# 快照 (IBKR 只给当下, 没有历史 —— 想看费率走势只能自己存; FINRA 有历史不用存)
# --------------------------------------------------------------------------

def snapshot_rows(by_symbol: dict, day: str, asof: str | None) -> list[dict]:
    rows = []
    for sym, info in by_symbol.items():
        b = (info or {}).get("borrow")
        if b:
            rows.append({"date": day, "symbol": sym, "fee": b["fee"],
                         "rebate": b["rebate"], "avail": b["avail"],
                         "avail_plus": int(b["avail_plus"]), "asof": asof or ""})
    return rows


def append_snapshot(rows: list[dict], path: Path) -> int:
    """追加到快照 CSV -> 新写入的行数。(date, symbol) 已有的跳过 (同 iv_history)。"""
    have = set()
    if path.exists():
        with path.open(newline="", encoding="utf-8") as f:
            have = {(r.get("date"), r.get("symbol")) for r in csv.DictReader(f)}
    new = [r for r in rows if (r["date"], r["symbol"]) not in have]
    if not new:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, SNAPSHOT_FIELDS, lineterminator="\n")
        if fresh:
            w.writeheader()
        w.writerows(new)
    return len(new)


# --------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    from datetime import datetime
    import scanner          # 函数内 import: scanner 在模块级 import 了本模块
    settings, tickers = scanner.load_config()
    symbols = [a.upper() for a in argv] or list(tickers)
    today = datetime.now(scanner.ET).date()
    meta = run(symbols, today, timeout=settings["short_timeout_s"])
    items = [(s_, tickers.get(s_, {}).get("kind", "stock"), meta["by_symbol"][s_])
             for s_ in symbols]
    print("\n".join(report_block(items, meta, settings, today)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
