#!/usr/bin/env python3
"""空头面 (FINRA 空头股数 + IBKR 借券费) — 取数 / 解析 / 旗标 / 报告段 / 快照.

只做提示, 不进状态机/门控/票据 (2026-10-28 定)。三个源, 各自失败互不影响:

  - FINRA 空头股数 (consolidated short interest): 每月两期 (月中、月底结算日),
    结算日后约 8 个交易日公布 (10/15 那期 10/28 能取到) —— 这是监管规定的申报
    频率, 没有更快的官方数 (更快的借券日报 SEC 10c-1a 推迟到 2029 才公开)。报告看
    **空头占流通股**、**回补天数** (空头股数 ÷ 日均成交量) 和较上期变化。公开 API
    不用 key; 按代码过滤时**不能带排序** (报错要求 settlementDate 用 EQUAL), 所以用
    domainFilters 一次拿全部代码、dateRangeFilters 圈近 45 天, 在本地取最新一期。
    没有匹配记录时 FINRA 回的是**空响应体**, 不是 []。
  - Yahoo 股本 (yfinance info, 只当分母): 个股用流通股; 流通股缺失或比总股本还大
    时用总股本 (多股份类 / ADR 口径对不上: 10/28 GOOG 流通股 108.8 亿含 A 类、总股本
    55.3 亿只算 C 类; TSM 流通股 378 亿是普通股、总股本 51.9 亿是折成 ADR)。ETF 用
    资产 ÷ 净值 —— yfinance 给 ETF 的 sharesOutstanding 是旧数 (QQQ 3.93 亿 vs
    7.01 亿、GLD 2.60 亿 vs 3.84 亿)。10/28 核对: SOFI 算出 17.2%, Yahoo 页面 17.17%。
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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

IBKR_URL = "ftp://shortstock:@ftp2.interactivebrokers.com/usa.txt"
IBKR_COLUMNS = ("SYM", "REBATERATE", "FEERATE", "AVAILABLE")
FINRA_URL = ("https://api.finra.org/data/group/otcMarket/name/"
             "consolidatedShortInterest")
FINRA_LOOKBACK_DAYS = 45     # 最新一期最老约 30 天 (半个月间隔 + 约 8 个交易日公布), 留余量
SI_STALE_DAYS = 35           # 最新结算日老于此 = FINRA 可能断更
BORROW_STALE_DAYS = 4        # IBKR 文件日期老于此 (周五晚 → 周一 + 一个假日) = 断更
SNAPSHOT_NAME = "borrow_history.csv"
SNAPSHOT_FIELDS = ["date", "symbol", "fee", "rebate", "avail", "avail_plus", "asof"]

NOTE = ("> 空头占流通股 = 空头股数 ÷ Yahoo 流通股 (多股份类/ADR 口径对不上时用总股本, "
        "ETF 用资产 ÷ 净值估算份额); 回补天数 = 空头股数 ÷ 日均成交量 (按平常成交量, 空头"
        "全部买回要几天); 较上期 = 空头股数比上一期 (FINRA 每月两期, 结算日后约 8 个交易日"
        "公布)。借券费 = IBKR 给自家客户的年化费率 (普通券约 0.25%), 可借 = IBKR 自己能借出"
        "的股数。个股按占比从高到低排, ETF/指数排后面: 它们的空头含申赎和对冲盘, 一期变动"
        "比个股大得多, 不出 ⚠️。只做提示, 未经回测, 不影响任何信号和票据。")


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
    不可比, 变化率没有意义)。dtc 也自己算 (空头股数 ÷ 日均成交量): FINRA 的
    daysToCoverQuantity 最低只写 1.00 —— 10/28 实测 DRAM 1,636 万 ÷ 3,261 万 = 0.50
    也写成 1.00 (上市以来 13 期全是 1.00), GLD 过去一年 25 期里 8 期是。没有日均量
    时才退回 FINRA 的值。"""
    latest = {}
    for r in records:
        sym, d = r.get("symbolCode"), r.get("settlementDate")
        if not sym or not d or (sym in latest and latest[sym]["date"] >= d):
            continue
        si, prev = r.get("currentShortPositionQuantity"), r.get("previousShortPositionQuantity")
        adv = r.get("averageDailyVolumeQuantity")
        split = bool(r.get("stockSplitFlag"))
        latest[sym] = {"date": d, "si": si, "prev": prev, "split": split,
                       "dtc": (si / adv if si is not None and adv
                               else r.get("daysToCoverQuantity")),
                       "chg": ((si / prev - 1) * 100
                               if si is not None and prev and not split else None)}
    return latest


# --------------------------------------------------------------------------
# Yahoo 股本 (只当"空头占流通股"的分母)
# --------------------------------------------------------------------------

def shares_basis(info: dict) -> tuple[float | None, str | None]:
    """yfinance info -> (分母股数, 口径 "float" / "out" / "aum") 或 (None, None)。
    个股: 流通股; 流通股缺失或比总股本还大 (多股份类 / ADR 口径对不上) 时用总股本。
    ETF: 资产 ÷ 净值 (yfinance 给 ETF 的 sharesOutstanding 是旧数)。"""
    if (info.get("quoteType") or "").upper() == "ETF":
        ta, nav = info.get("totalAssets"), info.get("navPrice")
        return (ta / nav, "aum") if ta and nav else (None, None)
    out, flt = info.get("sharesOutstanding"), info.get("floatShares")
    if flt and (not out or flt <= out):
        return flt, "float"
    return (out, "out") if out else (None, None)


def fetch_shares(symbols: list[str], workers: int = 4, ticker_factory=None) -> dict:
    """-> {sym: {"shares", "basis"} | None}。单只取不到 = None (那一格显示 —), 不抛。"""
    if ticker_factory is None:
        import yfinance as yf
        ticker_factory = yf.Ticker

    def one(sym):
        try:
            n, basis = shares_basis(ticker_factory(sym).info or {})
        except Exception:       # noqa: BLE001
            return sym, None
        return sym, ({"shares": n, "basis": basis} if n else None)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return dict(pool.map(one, symbols))


def si_pct(info: dict | None) -> float | None:
    """空头占流通股 (%) = FINRA 空头股数 ÷ Yahoo 股本; 缺任一个 = None。"""
    si = ((info or {}).get("si") or {}).get("si")
    sh = ((info or {}).get("shares") or {}).get("shares")
    return si / sh * 100 if si is not None and sh else None


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------

def run(symbols: list[str], today: date, timeout: float = 30.0,
        ibkr=fetch_ibkr, finra=fetch_finra, shares=fetch_shares) -> dict:
    """三个源各取一次 -> {
        "by_symbol": {sym: {"borrow": {...} | None, "si": {...} | None,
                            "shares": {...} | None}},
        "borrow_asof": IBKR 文件时间 | None, "si_date": 最新结算日 | None,
        "errors": {"borrow" / "si" / "shares": 原因}}  (只有失败的源才有键)。
    代码不在文件/记录里 = 对应字段 None; 一个源失败不影响另外两个。"""
    meta = {"by_symbol": {s_: {"borrow": None, "si": None, "shares": None} for s_ in symbols},
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
    try:
        got = shares(symbols)
        for s_ in symbols:
            meta["by_symbol"][s_]["shares"] = got.get(s_)
    except Exception as e:      # noqa: BLE001
        meta["errors"]["shares"] = _why(e)
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


def si_flag(v: dict | None, s: dict, pct: float | None = None) -> str | None:
    """只给个股 (调用方按 kind 判断): 空头占流通股 >= short_pct_warn%、回补天数 >=
    short_dtc_warn 或空头较上期 >= +short_chg_warn% -> 原因; 否则 None。"""
    if not v:
        return None
    parts = []
    if pct is not None and pct >= s["short_pct_warn"]:
        parts.append(f"空头占流通股 {pct:.1f}%")
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

def _stale(d: str | None, today: date, days: int) -> bool:
    try:
        return d is not None and (today - date.fromisoformat(d[:10])).days > days
    except ValueError:
        return False


KIND_LABEL = {"index": "指数", "etf": "ETF"}


def _row(sym: str, kind: str, info: dict | None) -> str:
    """表格一行: 标的 | 空头占流通股 | 回补天数 | 较上期 | 借券费 | 可借。缺的格子写 —。"""
    info = info or {}
    v, b = info.get("si") or {}, info.get("borrow")
    pct = si_pct(info)
    label = f"{sym} ({KIND_LABEL[kind]})" if kind in KIND_LABEL else sym
    chg = ("拆股" if v.get("split") else
           f"{v['chg']:+.0f}%" if v.get("chg") is not None else "—")
    cells = [label,
             f"{pct:.1f}%" if pct is not None else "—",
             f"{v['dtc']:.1f}" if v.get("dtc") is not None else "—",
             chg,
             f"{b['fee']:.2f}%" if b else "—",
             shares_cn(b["avail"], b["avail_plus"]) if b else "—"]
    return "| " + " | ".join(cells) + " |"


def report_block(items: list[tuple[str, str, dict | None]], meta: dict, s: dict,
                 today: date) -> list[str]:
    """[(symbol, kind, info)] -> 报告段落: 数据日期一行、每只一行的表格 (个股按空头占
    流通股从高到低, ETF/指数排后面)、⚠️ 旗标逐只、取数失败/缺记录各一条、说明。"""
    errors = meta.get("errors") or {}
    lines = ["## 空头面 (FINRA 空头股数 / IBKR 借券费)", ""]
    d, asof = meta.get("si_date"), meta.get("borrow_asof")

    def order(x):
        pct = si_pct(x[2])
        return (x[1] != "stock", -(pct if pct is not None else -1.0))
    rows = sorted([x for x in items if x[2] and (x[2].get("si") or x[2].get("borrow"))],
                  key=order)
    if rows:
        lines.append(f"- FINRA {d[5:] if d else '?'} 结算 (每月两期) · "
                     f"IBKR 借券 {asof[5:] + ' ET' if asof else '?'}")
        lines += ["", "| 标的 | 空头占流通股 | 回补天数 | 较上期 | 借券费 | 可借 |",
                  "|---|---|---|---|---|---|"]
        lines += [_row(*x) for x in rows]
        lines.append("")
    if _stale(d, today, SI_STALE_DAYS):
        lines.append(f"- ⚠️ FINRA 最新一期是 {d}, 超过 {SI_STALE_DAYS} 天 —— 数据可能断更")
    if _stale(asof, today, BORROW_STALE_DAYS):
        lines.append(f"- ⚠️ IBKR 借券文件停在 {asof} —— 超过 {BORROW_STALE_DAYS} 天没更新")

    for sym, kind, info in rows:
        b = info.get("borrow")
        if why := borrow_flag(b, s):
            lines.append(f"- ⚠️ **{sym}** 难借券: {why} —— 做空拥挤, put 贵、call 便宜都不是白捡的")
        if kind == "stock" and (why := si_flag(info.get("si"), s, si_pct(info))):
            lines.append(f"- ⚠️ **{sym}** {why}")

    for key, name in (("si", "FINRA 空头股数"), ("borrow", "IBKR 借券费"),
                      ("shares", "Yahoo 股本 (空头占流通股那一列)")):
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
    if lines[-1] != "":
        lines.append("")
    lines += [NOTE, ""]
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
