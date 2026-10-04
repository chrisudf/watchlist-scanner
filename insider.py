#!/usr/bin/env python3
"""内部人公开市场买入 (SEC Form 4) — 取数 / 解析 / 过滤 / 汇总.

只做**买入** (交易代码 P)。2026-10-05 用户拍板: 卖出不推 —— 大票的计划
卖出、扣税、行权几乎每天都有 (NVDA 一位董事一年自主卖出 $15 亿), 推了只剩
噪音; 金额下限 $25k (单笔或同一人同一周合计)。只做提示, 不进状态机/门控。

数据源: SEC EDGAR 一手。openinsider / secform4 是它的二次加工 —— SOFI 近一年
7 笔买入与 openinsider 逐条对上, SEC 原文还多一个 10b5-1 勾选字段。
  - 公司申报列表: https://data.sec.gov/submissions/CIK##########.json
  - Form 4 原文:  https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}.xml
SEC 合规: User-Agent 必须带联系邮箱 (环境变量 SEC_EMAIL), 限速 10 次/秒。

四个实测坑 (2026-10-02 拉 watchlist 14 只个股 12 个月 Form 4 时撞上的,
光看 SOFI 一只看不出来):
  1. 公司申报列表里混着它作为**别家**股东报的 Form 4 —— SEC 按申报人归档。
     GOOG 176 份里 7 份是谷歌风投卖 Ethos, HOOD 158 份里 25 份是 Robinhood
     卖 Robinhood Ventures Fund I。必须核对 XML 里的 issuerCik。
  2. 代码 P 不一定是公开市场买入: TSM 每月的员工购股计划代买, 30 多位高管
     同日同价, 交易日期的脚注写 ESPP, 持有方式写 "By ESPP Trust"。
  3. 是/否字段有 "1"/"0" 也有 "true"/"false" (GOOG 的 aff10b5One) —— 只认
     "1" 会把 Pichai 的计划卖出整批判反。
  4. 价格单位: TSM 高管买的是台股普通股 (2330.TW, 价格由新台币折算), 美股
     ADR 是 5 股一份 —— 成交价不能直接拿去对比价值区。用当天收盘价校验单位,
     顺带挡住拆股前的旧价格 (日线是复权的, Form 4 价格不是)。

命令行 (部署时预热缓存 / 手工看一眼):
    SEC_EMAIL=you@example.com .venv/bin/python insider.py [SOFI HOOD ...]
不带代码 = watchlist.toml 里全部个股。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"

REQUEST_GAP = 0.12          # SEC 限速 10 次/秒, 留余量
TIMEOUT = 20
# 缓存里的解析结果版本。改了 parse_form4 的输出 (比如以后要卖出) 就 +1,
# 旧条目自动重拉, 不用手动清缓存
PARSE_VERSION = 1

FLOOR_USD = 25_000          # 单笔或同一人同一 ISO 周合计 (用户 2026-10-05 拍板)
WINDOW_DAYS = 180           # 报告里的汇总窗口 (按交易日)
JOURNAL_WINDOW_DAYS = 90    # 推荐流水账里快照的窗口
FETCH_LOOKBACK_DAYS = 200   # 按申报日拉: 汇总窗口 + 迟报余量
NEW_MAX_AGE_DAYS = 7        # "新申报"只认 7 天内申报的 —— 换机器/清了 seen/新加
                            # 标的时, 不会把一年的旧买入当新消息刷屏
ESPP_CLUSTER_MIN = 5        # 同一天同一价格 ≥5 人 = 公司代买计划 (坑 2 的兜底)
CLUSTER_DAYS = 30           # 30 天内 ≥2 人买入 = 多人买入
MAX_FETCH_PER_RUN = 400     # 单次运行最多拉多少份新 XML; 首次回填超了就分几次跑完
MAX_CONSECUTIVE_FAILS = 3   # 连续失败这么多次就认定 SEC 不通, 别一份份等超时
CACHE_KEEP_DAYS = 400
SEEN_KEEP_DAYS = 60
TICKER_MAP_TTL_DAYS = 7
UNIT_RATIO_MAX = 1.5        # 成交价 / 当天收盘 超出 [1/1.5, 1.5] = 单位不同

# 公司代买类计划。只看挂在交易本身 (证券名/日期/代码/数量价格) 上的脚注和
# 持有方式 —— 挂在"交易后持股"上的脚注常写"其中 N 股来自 ESPP", 那是在说
# 存量, 不是这笔交易, 拿它判会误杀真买入
PLAN_RE = re.compile(
    r"employee stock purchase|\bESPP\b|employee stock ownership|\bESOP\b"
    r"|dividend reinvest|\bDRIP\b|401\s*\(k\)", re.I)
_TXN_PARTS = ("securityTitle", "transactionDate", "transactionCoding",
              "transactionAmounts")

CACHE_NAME = "form4_cache.json"
SEEN_NAME = "insider_seen.json"
TICKER_MAP_NAME = "sec_tickers.json"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def sec_email() -> str | None:
    email = (os.environ.get("SEC_EMAIL") or "").strip()
    return email if "@" in email else None


class SecClient:
    """顺序请求 + 固定间隔限速。scanner 只在主线程里调, 不需要锁。"""

    def __init__(self, email: str, gap: float = REQUEST_GAP,
                 timeout: float = TIMEOUT):
        self.ua = f"watchlist-scanner/1.0 ({email})"
        self.gap = gap
        self.timeout = timeout
        self.requests = 0
        self._last = 0.0

    def get(self, url: str) -> bytes:
        wait = self.gap - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": self.ua})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as f:
                return f.read()
        finally:
            self._last = time.monotonic()
            self.requests += 1

    def get_json(self, url: str):
        return json.loads(self.get(url))


# --------------------------------------------------------------------------
# Parsing (pure)
# --------------------------------------------------------------------------

def _bool(v) -> bool:
    """坑 3: SEC 的是/否字段两种写法都有。"""
    return (v or "").strip().lower() in ("1", "true")


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _text(node, path: str) -> str:
    return (node.findtext(path) or "").strip() if node is not None else ""


def parse_form4(xml: bytes) -> dict:
    """Form 4 原文 -> 发行人 / 申报人 / 买入行 (纯函数).

    只留代码 P 的非衍生品交易 —— v1 只做买入, 缓存小一个数量级 (大票的
    Form 4 绝大多数是授予/行权/扣税/计划卖出)。以后要卖出就改这里并把
    PARSE_VERSION +1。"""
    root = ET.fromstring(xml)
    foot = {f.get("id"): " ".join((f.text or "").split())
            for f in root.findall("footnotes/footnote")}
    owners = []
    for o in root.findall("reportingOwner"):
        rel = o.find("reportingOwnerRelationship")
        owners.append({
            "cik": _text(o, "reportingOwnerId/rptOwnerCik").lstrip("0"),
            "name": _text(o, "reportingOwnerId/rptOwnerName"),
            "director": _bool(_text(rel, "isDirector")),
            "officer": _bool(_text(rel, "isOfficer")),
            "ten_pct": _bool(_text(rel, "isTenPercentOwner")),
            "title": _text(rel, "officerTitle"),
        })
    rows = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        if _text(t, "transactionCoding/transactionCode") != "P":
            continue
        ids = set()
        for part in _TXN_PARTS:
            node = t.find(part)
            if node is not None:
                ids |= {f.get("id") for f in node.iter("footnoteId")}
        rows.append({
            "security": _text(t, "securityTitle/value"),
            "date": _text(t, "transactionDate/value")[:10],
            "shares": _num(_text(t, "transactionAmounts/transactionShares/value")),
            "price": _num(_text(t, "transactionAmounts/transactionPricePerShare/value")),
            "ad": _text(t, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "direct": _text(t, "ownershipNature/directOrIndirectOwnership/value"),
            "nature": _text(t, "ownershipNature/natureOfOwnership/value"),
            "notes": [foot[i] for i in sorted(ids, key=str) if i in foot],
        })
    issuer = _text(root, "issuer/issuerCik").lstrip("0")
    return {
        "v": PARSE_VERSION,
        "issuer_cik": int(issuer) if issuer.isdigit() else 0,
        "owners": owners,
        "plan": _bool(_text(root, "aff10b5One")),
        "rows": rows,
    }


# --------------------------------------------------------------------------
# Filtering / summary (pure)
# --------------------------------------------------------------------------

_CEO_RE = re.compile(r"chief executive|\bCEO\b", re.I)
_CFO_RE = re.compile(r"chief financial|\bCFO\b", re.I)


def role_label(owners: list[dict]) -> str:
    """'董事/CEO' 这类短标签。联名申报 (基金 + GP + 本人) 的身份分散在
    几个申报人上, 合起来看。"""
    director = any(o["director"] for o in owners)
    officer = any(o["officer"] for o in owners)
    ten = any(o["ten_pct"] for o in owners)
    title = next((o["title"] for o in owners if o["officer"] and o["title"]), "")
    parts = []
    if director:
        parts.append("董事")
    if officer:
        if _CEO_RE.search(title):
            parts.append("CEO")
        elif _CFO_RE.search(title):
            parts.append("CFO")
        else:
            parts.append(title[:24] or "高管")
    if ten and not (director or officer):
        parts.append("10%股东")
    return "/".join(parts) or "其他"


def _owner_name(owners: list[dict]) -> str:
    name = owners[0]["name"] if owners else "?"
    return name if len(owners) <= 1 else f"{name} 等{len(owners)}个申报人"


def _iso_week(d: str) -> tuple[int, int]:
    y, w, _ = date.fromisoformat(d).isocalendar()
    return y, w


def units_ok(price: float | None, close: float | None) -> bool | None:
    """成交价和当天收盘是不是同一个单位 (坑 4)。查不到收盘 = None (未知)。"""
    if not price or not close:
        return None
    ratio = price / close
    return 1 / UNIT_RATIO_MAX <= ratio <= UNIT_RATIO_MAX


def open_market_buys(filings: list[dict], issuer_cik: int,
                     floor: float = FLOOR_USD,
                     ref_close: Callable[[str], float | None] | None = None
                     ) -> list[dict]:
    """[{acc, filed, form, doc}] -> 合格的公开市场买入, 一行一笔交易.

    过滤顺序: 发行人 (坑 1) → 代码 P + 取得 → 公司代买计划 (坑 2: 脚注/持有
    方式, 再加同日同价 ≥5 人兜底) → 修正申报 4/A 重复报的同一笔去重 → 同一人
    同一周合计 ≥ floor。价格缺失的行不计金额, 但同周其他行过线时一起保留。"""
    cand = []
    for f in sorted(filings, key=lambda f: (f["filed"], f["acc"])):
        doc = f["doc"]
        if doc.get("issuer_cik") != issuer_cik or not doc.get("owners"):
            continue
        owners = doc["owners"]
        key = owners[0]["cik"] or owners[0]["name"]
        for r in doc.get("rows", []):
            if r["ad"] != "A" or not r["shares"] or not r["date"]:
                continue
            if PLAN_RE.search(" ".join(r["notes"]) + " " + r["nature"]):
                continue
            value = r["shares"] * r["price"] if r["price"] else None
            close = ref_close(r["date"]) if ref_close else None
            cand.append({
                "acc": f["acc"], "filed": f["filed"], "form": f["form"],
                "owner_key": key, "owner": _owner_name(owners),
                "role": role_label(owners), "date": r["date"],
                "shares": r["shares"], "price": r["price"], "value": value,
                "security": r["security"], "direct": r["direct"],
                "nature": r["nature"], "plan": doc.get("plan", False),
                "units_ok": units_ok(r["price"], close) if ref_close else True,
            })

    owners_at = defaultdict(set)
    for c in cand:
        owners_at[(c["date"], c["price"])].add(c["owner_key"])
    cand = [c for c in cand
            if len(owners_at[(c["date"], c["price"])]) < ESPP_CLUSTER_MIN]

    # 修正申报 (4/A) 常把原申报的交易整行重报一遍 —— 只拿 4/A 跟**别的**
    # 申报比对去重。同一份申报里两行一模一样是真实的两笔: TSM 一份申报里
    # 同日两笔 1,000 股 @77.09 (家庭成员名下), 按内容去重会吞掉一笔
    accs_of = defaultdict(set)
    deduped = []
    for c in cand:
        k = (c["owner_key"], c["date"], c["shares"], c["price"])
        if c["form"] == "4/A" and accs_of[k] - {c["acc"]}:
            continue
        accs_of[k].add(c["acc"])
        deduped.append(c)

    week_sum = defaultdict(float)
    for c in deduped:
        if c["value"]:
            week_sum[(c["owner_key"], _iso_week(c["date"]))] += c["value"]
    return [c for c in deduped
            if week_sum[(c["owner_key"], _iso_week(c["date"]))] >= floor]


def filing_events(buys: list[dict]) -> list[dict]:
    """同一份申报里的多笔买入合成一条 (报告里一份申报一行), 新的在前。"""
    by_acc: dict[str, list[dict]] = defaultdict(list)
    for b in buys:
        by_acc[b["acc"]].append(b)
    out = []
    for acc, rows in by_acc.items():
        priced = [b for b in rows if b["price"]]
        shares_p = sum(b["shares"] for b in priced)
        value = sum(b["value"] for b in priced) if priced else None
        first = rows[0]
        out.append({
            "acc": acc, "filed": first["filed"], "owner": first["owner"],
            "owner_key": first["owner_key"], "role": first["role"],
            "date_lo": min(b["date"] for b in rows),
            "date_hi": max(b["date"] for b in rows),
            "shares": sum(b["shares"] for b in rows),
            "value": value,
            "avg_price": value / shares_p if priced and shares_p else None,
            # 全部同单位才拿去对比价值区; 有任何一笔单位不对就整条不比
            "units_ok": all(b["units_ok"] for b in rows),
            "indirect": any(b["direct"] == "I" for b in rows),
            "nature": next((b["nature"] for b in rows if b["nature"]), ""),
            "plan": any(b["plan"] for b in rows),
            "security": first["security"],
        })
    out.sort(key=lambda e: (e["filed"], e["date_hi"], e["acc"]), reverse=True)
    return out


def _has_cluster(rows: list[dict], days: int = CLUSTER_DAYS) -> bool:
    """窗口内是否有 ≥2 个不同的人, 买入日期相距 ≤ days 天。"""
    pts = sorted((date.fromisoformat(b["date"]), b["owner_key"]) for b in rows)
    for i, (d0, k0) in enumerate(pts):
        for d1, k1 in pts[i + 1:]:
            if (d1 - d0).days > days:
                break
            if k1 != k0:
                return True
    return False


def summarize(buys: list[dict], today: date,
              window_days: int = WINDOW_DAYS) -> dict | None:
    """窗口内 (按交易日) 的买入汇总; 没有买入 = None。均价只用与股价同单位
    的成交 (坑 4), 全部不同单位时 avg_price = None。"""
    start = (today - timedelta(days=window_days)).isoformat()
    rows = [b for b in buys if b["date"] >= start]
    if not rows:
        return None
    comp = [b for b in rows if b["price"] and b["units_ok"]]
    comp_sh = sum(b["shares"] for b in comp)
    by_owner = defaultdict(float)
    for b in rows:
        by_owner[b["owner"]] += b["value"] or 0
    # "最近一笔"按交易日取: filing_events 按申报日排 (新申报提醒要那个顺序),
    # 而迟报很常见 —— TSM 一份 7/2 的交易 9/4 才申报, 按申报日它会盖过
    # 8/19 那笔真正更近的买入
    last = max(filing_events(rows), key=lambda e: (e["date_hi"], e["filed"]))
    return {
        "window_days": window_days,
        "n": len(rows),
        "n_buyers": len({b["owner_key"] for b in rows}),
        "value": sum(b["value"] or 0 for b in rows),
        "avg_price": (sum(b["value"] for b in comp) / comp_sh
                      if comp and comp_sh else None),
        "lo": min((b["price"] for b in comp), default=None),
        "hi": max((b["price"] for b in comp), default=None),
        "units_mixed": len(comp) < len([b for b in rows if b["price"]]),
        "cluster": _has_cluster(rows),
        "buyers": sorted(by_owner, key=lambda k: -by_owner[k]),
        "last": last,
    }


def new_events(buys: list[dict], seen: dict, today: date,
               max_age_days: int = NEW_MAX_AGE_DAYS) -> list[dict]:
    """还没报过、且是最近 max_age_days 天内申报的买入 (按申报合并)。"""
    start = (today - timedelta(days=max_age_days)).isoformat()
    return [e for e in filing_events(buys)
            if e["acc"] not in seen and e["filed"] >= start]


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save_json(path: Path, obj) -> None:
    """先写临时文件再改名 —— 中途被杀不会留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def prune_cache(cache: dict, today: date, keep_days: int = CACHE_KEEP_DAYS) -> dict:
    start = (today - timedelta(days=keep_days)).isoformat()
    return {k: v for k, v in cache.items() if v.get("filed", "") >= start}


def prune_seen(seen: dict, today: date, keep_days: int = SEEN_KEEP_DAYS) -> dict:
    start = (today - timedelta(days=keep_days)).isoformat()
    return {k: v for k, v in seen.items() if v >= start}


def ticker_ciks(client: SecClient, data_dir: Path, symbols: list[str],
                today: date) -> dict[str, int]:
    """代码 -> CIK。映射表约 1 MB, 本地缓存 7 天; 有代码查不到时强制刷新一次
    (新上市/改代码)。BRK.B 这类 SEC 用连字符。"""
    path = data_dir / TICKER_MAP_NAME
    cached = _load_json(path, {})
    fresh = (cached.get("fetched", "") >=
             (today - timedelta(days=TICKER_MAP_TTL_DAYS)).isoformat())
    mapping = cached.get("map") or {}

    def lookup(m):
        return {s: m.get(s) or m.get(s.replace(".", "-")) for s in symbols}

    found = lookup(mapping)
    # 查不到的代码最多触发一天一次刷新 —— 真不在 SEC 表里的代码 (非美国
    # 发行人、退市) 不能让每次扫描都重下整张表
    missing = any(v is None for v in found.values())
    if not fresh or (missing and cached.get("fetched") != today.isoformat()):
        raw = client.get_json(TICKERS_URL)
        mapping = {v["ticker"].upper(): int(v["cik_str"]) for v in raw.values()}
        _save_json(path, {"fetched": today.isoformat(), "map": mapping})
        found = lookup(mapping)
    return {s: c for s, c in found.items() if c}


# --------------------------------------------------------------------------
# Fetch
# --------------------------------------------------------------------------

class SecUnavailable(RuntimeError):
    """连续失败, 判定 SEC 这次不通 —— 剩下的标的不再逐个等超时。"""


def fetch_filings(client: SecClient, cik: int, cache: dict, today: date,
                  budget: list[int]) -> tuple[list[dict], dict]:
    """一个发行人近 FETCH_LOOKBACK_DAYS 天的 Form 4 (缓存优先).

    -> (filings, status); status = {"missing": 本次没拉到的份数,
    "partial_history": submissions 的 recent 没覆盖到窗口起点}。
    budget 是 [剩余次数], 跨标的共享。"""
    subs = client.get_json(SUBMISSIONS_URL.format(cik=cik))
    rec = subs["filings"]["recent"]
    start = (today - timedelta(days=FETCH_LOOKBACK_DAYS)).isoformat()
    dates = rec["filingDate"]
    # recent 只装最近约 1000 份; 大票 1000 份能覆盖好几年, 真不够就标出来
    # (不去拉分页文件 —— v1 的窗口只有 200 天, 撞上的概率很低)
    partial = bool(subs["filings"].get("files")) and bool(dates) and dates[-1] > start
    filings, missing, fails = [], 0, 0
    for i, form in enumerate(rec["form"]):
        if form not in ("4", "4/A") or dates[i] < start:
            continue
        acc = rec["accessionNumber"][i]
        ent = cache.get(acc)
        if ent is None or ent.get("v") != PARSE_VERSION:
            doc = rec["primaryDocument"][i].rsplit("/", 1)[-1]
            if not doc.lower().endswith(".xml"):
                continue        # 2003 年前的纯文本申报, 窗口内不会出现
            if budget[0] <= 0:
                missing += 1
                continue
            budget[0] -= 1
            try:
                xml = client.get(DOC_URL.format(cik=cik, acc=acc.replace("-", ""),
                                                doc=doc))
                ent = {"filed": dates[i], "form": form, **parse_form4(xml)}
            except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError):
                missing += 1
                fails += 1
                if fails >= MAX_CONSECUTIVE_FAILS:
                    raise SecUnavailable(f"连续 {fails} 份 Form 4 取数失败")
                continue
            fails = 0
            cache[acc] = ent
        filings.append({"acc": acc, "filed": ent["filed"], "form": ent["form"],
                        "doc": ent})
    return filings, {"missing": missing, "partial_history": partial}


def run(symbols: list[str], today: date, *, data_dir: Path,
        ref_closes: dict[str, Callable[[str], float | None]] | None = None,
        email: str | None = None,
        client: SecClient | None = None) -> dict:
    """scanner 的入口: 拉取 + 汇总 + 标出新申报.

    -> {"enabled", "reason", "requests", "by_symbol": {sym: info}}
    info = {"cik", "buys", "summary", "summary90", "new", "missing",
            "partial_history"} 或 {"error": "..."}。

    取数失败**不等于**没有买入 —— 失败的标的带 error, 报告必须写出来
    (和 next_earnings 的 None/'' 同一个约定)。这里只读"已报过", 不写 ——
    写由调用方在报告落盘之后调 mark_seen()。"""
    email = email or sec_email()
    if client is None:
        if not email:
            return {"enabled": False, "reason": "未配置 SEC_EMAIL",
                    "requests": 0, "by_symbol": {}}
        client = SecClient(email)
    ref_closes = ref_closes or {}
    cache_path = data_dir / CACHE_NAME
    cache = _load_json(cache_path, {})
    seen = _load_json(data_dir / SEEN_NAME, {})
    out: dict = {"enabled": True, "reason": None, "by_symbol": {}}
    try:
        ciks = ticker_ciks(client, data_dir, symbols, today)
    except Exception as e:      # noqa: BLE001 — 任何失败都只降级, 不拖垮扫描
        out["by_symbol"] = {s: {"error": f"SEC 代码表取数失败: {e}"} for s in symbols}
        out["requests"] = client.requests
        return out

    budget = [MAX_FETCH_PER_RUN]
    down: str | None = None
    net_fails = 0           # 连续几个标的在网络层失败
    for sym in symbols:
        if down:
            out["by_symbol"][sym] = {"error": f"未尝试 ({down})"}
            continue
        cik = ciks.get(sym)
        if cik is None:
            out["by_symbol"][sym] = {"error": "SEC 代码表里没有这个代码"}
            continue
        try:
            filings, status = fetch_filings(client, cik, cache, today, budget)
        except SecUnavailable as e:
            down = str(e)
            out["by_symbol"][sym] = {"error": down}
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            net_fails += 1
            out["by_symbol"][sym] = {"error": f"取数失败: {type(e).__name__}: {e}"}
            if net_fails >= MAX_CONSECUTIVE_FAILS:
                down = f"连续 {net_fails} 个标的连不上 SEC"
            continue
        except Exception as e:  # noqa: BLE001
            out["by_symbol"][sym] = {"error": f"取数失败: {type(e).__name__}: {e}"}
            continue
        net_fails = 0
        buys = open_market_buys(filings, cik, ref_close=ref_closes.get(sym))
        out["by_symbol"][sym] = {
            "cik": cik, "buys": buys,
            "summary": summarize(buys, today, WINDOW_DAYS),
            "summary90": summarize(buys, today, JOURNAL_WINDOW_DAYS),
            "new": new_events(buys, seen, today),
            **status,
        }

    _save_json(cache_path, prune_cache(cache, today))
    out["requests"] = client.requests
    return out


def mark_seen(out: dict, today: date, data_dir: Path) -> int:
    """把 run() 标出的新申报记为"已报过" -> 新记的份数.

    必须在报告**写盘之后**调: 先记再写, 中间崩了这条就永远不报了。写盘
    之后邮件没发出去不怕 —— scanner 的补发会重发同一份报告。手工运行
    (--force/--tickers) 不调, 免得定时扫描把真正的新申报当旧的吞掉。"""
    seen_path = data_dir / SEEN_NAME
    seen = _load_json(seen_path, {})
    stamp, n = today.isoformat(), 0
    for info in out.get("by_symbol", {}).values():
        for e in info.get("new", []):
            if e["acc"] not in seen:
                seen[e["acc"]] = stamp
                n += 1
    _save_json(seen_path, prune_seen(seen, today))
    return n


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _usd(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v / 1e4:,.1f}万" if v >= 1e4 else f"${v:,.0f}"


def main(argv: list[str]) -> int:
    import tomllib
    base = Path(__file__).resolve().parent
    if argv:
        symbols = [a.upper() for a in argv]
    else:
        cfg = tomllib.loads((base / "watchlist.toml").read_text(encoding="utf-8"))
        symbols = [s for s, v in cfg["tickers"].items()
                   if v.get("kind", "stock") == "stock"]
    if not sec_email():
        print("先设置环境变量 SEC_EMAIL (SEC 要求 User-Agent 带联系邮箱)")
        return 2
    t0 = time.monotonic()
    res = run(symbols, date.today(), data_dir=base / "data")
    print(f"{res['requests']} 次请求, {time.monotonic() - t0:.0f}s\n")
    for sym in symbols:
        info = res["by_symbol"].get(sym, {})
        if info.get("error"):
            print(f"{sym:6} 失败: {info['error']}")
            continue
        s = info["summary"]
        flags = "".join([" [本次未拉全]" if info["missing"] else "",
                         " [历史未覆盖窗口]" if info["partial_history"] else ""])
        if not s:
            print(f"{sym:6} 近 {WINDOW_DAYS} 天无公开市场买入{flags}")
            continue
        avg = f"均价 {s['avg_price']:.2f}" if s["avg_price"] else "均价 — (单位不同)"
        print(f"{sym:6} {s['n_buyers']} 人 {s['n']} 笔 {_usd(s['value'])} {avg}"
              f"{' 多人买入' if s['cluster'] else ''}{flags}")
        for e in filing_events([b for b in info["buys"]
                                if b["date"] >= (date.today() - timedelta(days=WINDOW_DAYS)).isoformat()]):
            px = f"@{e['avg_price']:.2f}" if e["avg_price"] else ""
            print(f"         {e['date_hi']} {e['owner'][:28]:28} {e['role'][:16]:16} "
                  f"{e['shares']:>10,.0f} {px:>9} {_usd(e['value']):>9}"
                  f"{'  间接:' + e['nature'] if e['indirect'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
