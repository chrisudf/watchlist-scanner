#!/usr/bin/env python3
"""Unit tests for the pure signal/state/ticket math in scanner.py.

Run:  .venv/bin/python test_signals.py
No network access needed — everything here is synthetic data.
"""

import io
import json
import math
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import scanner as sc


def series(vals):
    idx = pd.bdate_range("2026-01-01", periods=len(vals))
    return pd.Series(list(map(float, vals)), index=idx)


def downtrend_then_base(n_down=40, n_base=10, start=100.0, step=0.8):
    """Close path: steady decline then a flat base above the final low."""
    down = [start - i * step for i in range(n_down)]
    base = [down[-1] + 2.0] * n_base
    return down + base


class TestSignals(unittest.TestCase):
    def test_no_new_low_holds(self):
        lows = series(downtrend_then_base())
        self.assertTrue(sc.no_new_low(lows))

    def test_no_new_low_fails_on_fresh_low(self):
        vals = downtrend_then_base()
        vals[-1] = min(vals) - 5
        self.assertFalse(sc.no_new_low(series(vals)))

    def test_reclaim_needs_volume(self):
        # decline below the 20dma, then pop back above it on the last bar
        vals = [100 - i * 0.5 for i in range(40)] + [95.0]
        close = series(vals)
        self.assertTrue(sc.reclaimed_20dma(close, vol_ratio=2.0, surge=1.5))
        self.assertFalse(sc.reclaimed_20dma(close, vol_ratio=1.0, surge=1.5))

    def test_reclaim_requires_recent_cross(self):
        # always above the 20dma -> nothing to "reclaim"
        close = series([100 + i for i in range(60)])
        self.assertFalse(sc.reclaimed_20dma(close, vol_ratio=3.0, surge=1.5))

    def test_breakout(self):
        vals = [100.0] * 30 + [105.0]
        close = high = series(vals)
        self.assertTrue(sc.broke_20d_high(close, high))
        self.assertFalse(sc.broke_20d_high(series([100.0] * 31), series([100.0] * 31)))

    def test_uptrend_never_confirms(self):
        # grinding uptrend: signals may fire individually but the pullback
        # gate must keep `confirmed` False
        close = high = low = series([100 + i * 0.5 for i in range(80)])
        out = sc.confirmation(close, high, low, vol_ratio=2.0, surge=1.5)
        self.assertFalse(out["pullback_context"])
        self.assertFalse(out["confirmed"])

    def test_pullback_recovery_confirms(self):
        # slide well below the 20dma, base, then reclaim on volume:
        # no_new_low + reclaim20 = 2 of 3 with pullback context
        # (>= 61 bars so the pullback gate has enough history)
        vals = [100 - i * 0.5 for i in range(70)] + [66, 67, 68, 69, 80]
        close = series(vals)
        high = close + 1
        low = close - 1
        out = sc.confirmation(close, high, low, vol_ratio=2.0, surge=1.5)
        self.assertTrue(out["pullback_context"])
        self.assertTrue(out["no_new_low"])
        self.assertTrue(out["reclaim20"])
        self.assertTrue(out["confirmed"])


class TestStateMachine(unittest.TestCase):
    def test_confirmed_to_trend(self):
        state, notes = sc.next_state("CONFIRMED", close=110, sma20=100,
                                     confirmed=False, zone=None, near_pct=5)
        self.assertEqual(state, "TREND")
        self.assertEqual(notes, [])

    def test_trailing_stop_fires(self):
        state, notes = sc.next_state("TREND", close=95, sma20=100,
                                     confirmed=False, zone=None, near_pct=5)
        self.assertEqual(state, "PULLBACK")
        self.assertTrue(any("止损" in n for n in notes))

    def test_zone_states(self):
        # 左侧状态要求弱势: 收盘在20日线下 + 在价值区内
        state, _ = sc.next_state("UPTREND", close=250, sma20=260,
                                 confirmed=False, zone=[200, 260], near_pct=5)
        self.assertEqual(state, "LEFT_ZONE")
        state, _ = sc.next_state("UPTREND", close=268, sma20=280,
                                 confirmed=False, zone=[200, 260], near_pct=5)
        self.assertEqual(state, "NEAR_ZONE")
        state, notes = sc.next_state("UPTREND", close=190, sma20=260,
                                     confirmed=False, zone=[200, 260], near_pct=5)
        self.assertEqual(state, "LEFT_ZONE")
        self.assertTrue(any("下沿" in n for n in notes))

    def test_uptrend_through_zone_is_not_left_side(self):
        # MSFT case: 价格在20日线上方 18%, 只是还没涨出宽价值区 — 趋势,
        # 不是左侧 (CSP 触发与状态解耦, 由 analyze_ticker 的 zone 检查管)
        state, notes = sc.next_state("UPTREND", close=500, sma20=424,
                                     confirmed=False, zone=[460, 615], near_pct=5)
        self.assertEqual(state, "UPTREND")
        self.assertEqual(notes, [])

    def test_confirmation_beats_zone(self):
        state, _ = sc.next_state("LEFT_ZONE", close=250, sma20=240,
                                 confirmed=True, zone=[200, 260], near_pct=5)
        self.assertEqual(state, "CONFIRMED")

    def test_floor_break_note_decoupled_from_state(self):
        # zone 评审: 破下沿的"检查论点"跟价格走, 不跟状态标签走 — 深跌后
        # 反弹站上塌陷的 20 日线 (UPTREND) 时原实现一声不响, 而 CSP/档位
        # 照常可执行
        state, notes = sc.next_state("UPTREND", close=190, sma20=185,
                                     confirmed=False, zone=[200, 260], near_pct=5)
        self.assertEqual(state, "UPTREND")
        self.assertTrue(any("下沿" in n for n in notes))
        # TREND 持续期 (收盘在20日线上) 同理
        state, notes = sc.next_state("TREND", close=190, sma20=185,
                                     confirmed=False, zone=[200, 260], near_pct=5)
        self.assertEqual(state, "TREND")
        self.assertTrue(any("下沿" in n for n in notes))

    def test_floor_break_note_on_stop_day(self):
        # 止损转 PULLBACK 当日 (原实现只出止损, 下沿警告被状态分支吞掉):
        # 两条 note 并存, 且状态仍是 PULLBACK 不是 LEFT_ZONE (止损优先)
        state, notes = sc.next_state("TREND", close=190, sma20=195,
                                     confirmed=False, zone=[200, 260], near_pct=5)
        self.assertEqual(state, "PULLBACK")
        self.assertTrue(any("止损" in n for n in notes))
        self.assertTrue(any("下沿" in n for n in notes))

    def test_no_floor_note_inside_zone(self):
        # 在区内 (未破下沿) 不出论点检查 — 与既有 test_zone_states 的
        # 破下沿用例互为边界
        state, notes = sc.next_state("UPTREND", close=210, sma20=260,
                                     confirmed=False, zone=[200, 260], near_pct=5)
        self.assertEqual(state, "LEFT_ZONE")
        self.assertFalse(any("下沿" in n for n in notes))

    def test_near_zone_boundary_is_inclusive(self):
        # QQQ 上线态: zone [635,685], 685*1.05=719.25, 收 718.96 距边界
        # 0.04% — 边界语义 (<=) 必须钉死, 两份 in/near 实现 (next_state 与
        # analyze_ticker) 都以此为准
        state, _ = sc.next_state("PULLBACK", close=719.25, sma20=720,
                                 confirmed=False, zone=[635, 685], near_pct=5)
        self.assertEqual(state, "NEAR_ZONE")
        state, _ = sc.next_state("PULLBACK", close=719.26, sma20=720,
                                 confirmed=False, zone=[635, 685], near_pct=5)
        self.assertEqual(state, "PULLBACK")

    def test_stop_beats_zone_label(self):
        # 右侧持仓破 20 日线且落进价值区: 止损优先, 是 PULLBACK 不是
        # LEFT_ZONE — 止损日的动作是减/清多头, 不是接货打标签
        for prev in ("CONFIRMED", "TREND"):
            state, notes = sc.next_state(prev, close=250, sma20=255,
                                         confirmed=False, zone=[200, 260],
                                         near_pct=5)
            self.assertEqual(state, "PULLBACK", prev)
            self.assertTrue(any("止损" in n for n in notes), prev)


class TestOptionMath(unittest.TestCase):
    def test_sixteen_rule(self):
        # IV 45%, 7 DTE, mult 2.75: 2.75 * (0.45/16) * sqrt(7) ~= 20.5%
        d = sc.sixteen_rule_distance(0.45, 7, 2.75)
        self.assertAlmostEqual(d, 2.75 * 0.45 / 16 * math.sqrt(7), places=10)
        self.assertTrue(0.19 < d < 0.22)

    def test_csp_annualized(self):
        # 1.00 premium on a 100 strike, 30 DTE:
        # 1/(100-1) * 365/30 ~= 12.3% annualized
        self.assertAlmostEqual(sc.csp_annualized(1.0, 100.0, 30), 12.29, places=1)

    def test_bs_delta_bounds(self):
        atm = sc.bs_delta(100, 100, 1.0, 0.04, 0.3, is_call=True)
        self.assertTrue(0.5 < atm < 0.7)          # ATM call, r/vol drift
        deep = sc.bs_delta(100, 50, 1.0, 0.04, 0.3, is_call=True)
        self.assertGreater(deep, 0.95)
        otm_put = sc.bs_delta(100, 70, 0.05, 0.04, 0.3, is_call=False)
        self.assertGreater(otm_put, -0.05)        # far OTM put ~ 0

    def test_iv_roundtrip(self):
        price = sc.bs_price(100, 90, 1.5, 0.04, 0.42, is_call=True)
        iv = sc.implied_vol(price, 100, 90, 1.5, 0.04, is_call=True)
        self.assertAlmostEqual(iv, 0.42, places=3)

    def _quote_row(self, bid, ask, last=0.0, traded_days_ago=1):
        return pd.Series({
            "bid": bid, "ask": ask, "lastPrice": last,
            "lastTradeDate": pd.Timestamp.now(tz="UTC")
            - pd.Timedelta(days=traded_days_ago)})

    def test_mark_healthy_book(self):
        mid, src = sc._mark(self._quote_row(1.00, 1.10), sc._stale_cutoff())
        self.assertAlmostEqual(mid, 1.05)
        self.assertEqual(src, "live")

    def test_mark_rejects_crossed_book(self):
        # bid > ask (Yahoo 实测会出): mid 无意义 — 此前只有 _rr_mark 拒,
        # CSP/LEAP/spread 票价照收且负价差还通过 LEAP <=5% 过滤 (五轮评审)。
        # crossed 落到 lastPrice 路径 → src="last" 自动带"下单前实查"提示
        mid, src = sc._mark(self._quote_row(1.10, 1.00, last=1.02),
                            sc._stale_cutoff())
        self.assertEqual((mid, src), (1.02, "last"))
        # crossed 且无近期成交 → 无可用价
        mid, src = sc._mark(self._quote_row(1.10, 1.00, last=1.02,
                                            traded_days_ago=10),
                            sc._stale_cutoff())
        self.assertIsNone(mid)

    def test_stock_ladder(self):
        s = sc.SETTINGS_DEFAULTS
        ladder = sc.stock_ladder([380.0, 440.0], s)
        self.assertEqual(ladder[0], 440.0)
        self.assertEqual(ladder[1], 380.0)
        self.assertAlmostEqual(
            ladder[2], 380.0 * (1 - s["ladder_panic_discount"]), places=2)
        # 剧本: 间距递增, 末档留给恐慌价 (窄区间成立)
        self.assertGreater(ladder[1] - ladder[2], ladder[0] - ladder[1])
        # 宽区间 (AAPL 形状): 固定折扣末档间距不递增 — render 侧须走提示分支
        wide = sc.stock_ladder([176.0, 264.0], s)
        self.assertLess(wide[1] - wide[2], wide[0] - wide[1])


class TestRegime(unittest.TestCase):
    def _ratio(self, vals):
        idx = pd.bdate_range("2026-01-01", periods=len(vals))
        return pd.Series(vals, index=idx)

    def test_episode_detection(self):
        ratio = self._ratio([0.9] * 10 + [1.05, 1.12, 1.08, 1.11] + [0.95] * 5)
        eps = sc.inversion_episodes(ratio)
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["days"], 4)
        self.assertAlmostEqual(eps[0]["peak"], 1.12)
        self.assertFalse(eps[0]["ongoing"])

    def test_stages(self):
        s = sc.SETTINGS_DEFAULTS
        stage, _ = sc.classify_regime(self._ratio([0.9] * 40), s)
        self.assertEqual(stage, "NORMAL")
        stage, _ = sc.classify_regime(self._ratio([0.9] * 39 + [1.02]), s)
        self.assertEqual(stage, "STAGE1")
        stage, _ = sc.classify_regime(self._ratio([0.9] * 39 + [1.15]), s)
        self.assertEqual(stage, "STAGE1_DEEP")
        # qualifying inversion (4d, peak 1.12) resolved 5 bars ago -> stage 2
        stage, _ = sc.classify_regime(
            self._ratio([0.9] * 30 + [1.05, 1.12, 1.08, 1.11] + [0.95] * 5), s)
        self.assertEqual(stage, "STAGE2_WINDOW")
        # same episode but 15 bars ago -> window closed
        stage, _ = sc.classify_regime(
            self._ratio([0.9] * 20 + [1.05, 1.12, 1.08, 1.11] + [0.95] * 15), s)
        self.assertEqual(stage, "NORMAL")
        # shallow inversion (peak < 1.10) never opens a stage-2 window
        stage, _ = sc.classify_regime(
            self._ratio([0.9] * 30 + [1.02, 1.03, 1.04, 1.05] + [0.95] * 5), s)
        self.assertEqual(stage, "NORMAL")


class TestActionLabel(unittest.TestCase):
    def _r(self, **kw):
        base = {"error": None, "tech": {"close": 100}, "notes": [],
                "leap": None, "csp": None, "state": "UPTREND",
                "cfg": {"value_zone": None, "options": True}}
        base.update(kw)
        return base

    def test_labels(self):
        leap = {"exp": "2028-01-21", "strike": 100, "mid": 1.0, "delta": 0.8}
        csp = {"exp": "2026-08-28", "strike": 90, "mid": 1.0, "delta": 0.12,
               "annualized_pct": 10}
        cases = [
            (self._r(notes=["右侧止损触发: 收盘跌破20日线"]), None, "⚠️止损"),
            (self._r(leap=leap), None, "LEAP票👇"),
            (self._r(leap=leap), 70.0, "IV高·spread"),
            (self._r(leap={"skip_reason": "财报 2026-08-27 在 19 天内"}), None, "等财报后"),
            (self._r(csp=csp, state="LEFT_ZONE",
                     cfg={"value_zone": [80, 95], "options": True}), None, "CSP票👇"),
            (self._r(notes=["右侧信号出现但倒挂未解除 — 等阶段2"]), None, "等阶段2"),
            (self._r(state="TREND"), None, "持有·跟20日线"),
            (self._r(state="TREND", retest=True), None, "回踩中👀"),
            (self._r(state="TREND", retest=True,
                     spread={"exp": "2026-12-18", "long_strike": 500,
                             "short_strike": 550, "debit": 15.0}),
             None, "spread票👇"),
            (self._r(state="PULLBACK"), None, "设区间"),
            # 无期权链标的设区间同样解锁正股分批档 — 也要提示
            (self._r(state="PULLBACK",
                     cfg={"value_zone": None, "options": False}),
             None, "设区间"),
            # MSFT case: 设了接货带, 现价在上方 — 不是模糊的"观望"
            (self._r(state="UPTREND", tech={"close": 500},
                     cfg={"value_zone": [380, 440], "options": True}),
             None, "等回落入区"),
            (self._r(state="PULLBACK", tech={"close": 313},
                     cfg={"value_zone": [176, 264], "options": True}),
             None, "等回落入区"),
            # 无期权链但在接货带内 — 正股分批是唯一工具
            (self._r(state="LEFT_ZONE", ladder=[264, 176, 144.32],
                     cfg={"value_zone": [176, 264], "options": False},
                     tech={"close": 250}), None, "分批档👇"),
            (self._r(), None, "别追·等回调"),
            (self._r(error="boom", tech=None), None, "—"),
        ]
        for r, ivp, expect in cases:
            self.assertEqual(sc.action_label(r, ivp), expect)

    def test_sort_by_actionability(self):
        rs = [self._r(state="UPTREND"), self._r(state="CONFIRMED"),
              self._r(state="TREND", notes=["右侧止损触发"]),
              self._r(state="LEFT_ZONE")]
        ordered = [r["state"] for r in sc.by_actionability(rs)]
        self.assertEqual(ordered, ["TREND", "CONFIRMED", "LEFT_ZONE", "UPTREND"])


class TestPartialHistory(unittest.TestCase):
    def _frame(self, n):
        idx = pd.bdate_range("2026-06-01", periods=n)
        close = pd.Series([100 + i * 0.5 for i in range(n)], index=idx)
        return pd.DataFrame({"Open": close, "High": close + 1,
                             "Low": close - 1, "Close": close,
                             "Volume": [1e6] * n}, index=idx)

    def test_short_history_degrades_not_rejects(self):
        # SPCX case: 39 根日线 — 出快照但右侧确认关闭
        t = sc.technical_snapshot(self._frame(39), sc.SETTINGS_DEFAULTS)
        self.assertIsNotNone(t)
        self.assertEqual(t["bars"], 39)
        self.assertFalse(t["signals"]["confirmed"])
        self.assertIsNone(t["sma200"])

    def test_too_short_rejects(self):
        self.assertIsNone(
            sc.technical_snapshot(self._frame(20), sc.SETTINGS_DEFAULTS))


class TestPersistedState(unittest.TestCase):
    def test_carry_and_flags(self):
        prev = {"state": "TREND", "since": "2026-08-01",
                "leap_window": "2026-07-30"}
        e = sc.next_persisted_state(prev, {"state": "TREND", "retest": True},
                                    "2026-08-09")
        self.assertEqual(e["since"], "2026-08-01")        # 状态没变不刷新
        self.assertEqual(e["leap_window"], "2026-07-30")  # 跨日携带
        self.assertTrue(e["retested"])                    # 本日回踩置位

    def test_retested_resets_on_fresh_confirm(self):
        prev = {"state": "PULLBACK", "since": "2026-07-01", "retested": True}
        e = sc.next_persisted_state(prev, {"state": "CONFIRMED"}, "2026-08-09")
        self.assertEqual(e["since"], "2026-08-09")
        self.assertNotIn("retested", e)

    def test_retested_carries_forward(self):
        prev = {"state": "TREND", "since": "2026-08-01", "retested": True}
        e = sc.next_persisted_state(prev, {"state": "TREND"}, "2026-08-09")
        self.assertTrue(e["retested"])

    def test_leap_pending_lifecycle(self):
        # 二轮评审 finding: NORMAL 期 fresh_confirm 被硬停牌吞掉 —
        # 被拦当日置 leap_pending, 随右侧状态存活
        e = sc.next_persisted_state(
            {}, {"state": "CONFIRMED", "leap_pending": True}, "2026-09-03")
        self.assertTrue(e["leap_pending"])
        # 三轮评审 (Copilot): "结果没带标记" != 已消耗 — --no-options 的非
        # manual 收盘跑在期权分析前就 return, 不得静默抹掉在途标记
        e2 = sc.next_persisted_state(e, {"state": "TREND"}, "2026-09-04")
        self.assertTrue(e2["leap_pending"])
        # 真票发出 = 显式消耗
        e3 = sc.next_persisted_state(
            e, {"state": "TREND", "leap_emitted": True}, "2026-09-04")
        self.assertNotIn("leap_pending", e3)
        # 止损出局: 即使当日仍被拦, 标记不得跟进 PULLBACK (确认周期已死)
        e4 = sc.next_persisted_state(
            e, {"state": "PULLBACK", "leap_pending": True}, "2026-09-04")
        self.assertNotIn("leap_pending", e4)

    def test_temporary_skip_does_not_consume_leap_pending(self):
        # 三轮评审: leap_ticket 返回 skip_reason (财报缓冲期内 / 无可用合约)
        # 是**临时**约束 — 清掉补发标记等于约束解除后不再补发, 与"显式消耗"
        # 的生命周期自相矛盾。analyze 只在真票时置 leap_emitted=True
        e = sc.next_persisted_state(
            {}, {"state": "CONFIRMED", "leap_pending": True}, "2026-09-03")
        kept = sc.next_persisted_state(
            e, {"state": "TREND", "leap_emitted": False}, "2026-09-04")
        self.assertTrue(kept["leap_pending"])

    def test_retest_pending_lifecycle(self):
        # 三轮评审 (Copilot): 被拦的回踩若不落盘, 价格离开 20 日线后这轮
        # 就再也发不出来 — 承诺的"解除后再提示"落空
        prev = {"state": "TREND", "since": "2026-08-01"}
        e = sc.next_persisted_state(
            prev, {"state": "TREND", "retest_pending": True}, "2026-09-03")
        self.assertTrue(e["retest_pending"])
        self.assertNotIn("retested", e)        # 被拦不烧一次性标记
        e2 = sc.next_persisted_state(e, {"state": "TREND"}, "2026-09-04")
        self.assertTrue(e2["retest_pending"])  # 跨日沿用
        e3 = sc.next_persisted_state(
            e, {"state": "TREND", "retest": True}, "2026-09-04")
        self.assertTrue(e3["retested"])        # 补发 = 显式消耗
        self.assertNotIn("retest_pending", e3)
        e4 = sc.next_persisted_state(e, {"state": "PULLBACK"}, "2026-09-04")
        self.assertNotIn("retest_pending", e4)


class TestEmailHtml(unittest.TestCase):
    """纯文本邮件里 11 列表格就是一堆竖线, **粗体** 显示成星号 — 手机上
    没法读。转换只覆盖 render_close 实际产出的 markdown 子集。"""

    def test_table_becomes_real_table(self):
        md = "\n".join([
            "| 标的 | 收盘 | Δ% |",
            "|---|---|---|",
            "| QQQ | 709.24 | +0.2 |",
            "| MSFT | 496.82 | -0.8 |",
        ])
        h = sc.md_to_email_html(md)
        self.assertIn("<table", h)
        self.assertEqual(h.count("<tr"), 3)          # 表头 + 2 行
        self.assertIn("overflow-x:auto", h)          # 宽表在手机上要能横拖
        self.assertNotIn("|---|", h)

    def test_numeric_cells_right_aligned_and_monospaced(self):
        md = "| 标的 | 收盘 |\n|---|---|\n| QQQ | 709.24 |"
        h = sc.md_to_email_html(md)
        self.assertIn("text-align:right", h)         # 数字列右对齐才好比
        self.assertIn("text-align:left", h)          # 文本列仍左对齐

    def test_status_marks_become_colored_cards(self):
        for mark, bar in (("⛔", "#dc2626"), ("🟢", "#16a34a"),
                          ("🔵", "#2563eb"), ("⏸", "#94a3b8")):
            h = sc.md_to_email_html(f"- {mark} 某某标的")
            self.assertIn(f"border-left:4px solid {bar}", h, mark)

    def test_plain_bullet_is_not_a_card(self):
        h = sc.md_to_email_html("- 位置: 收盘 63.10")
        self.assertNotIn("border-left:4px", h)
        self.assertIn("•", h)

    def test_nested_bullet_is_indented(self):
        h = sc.md_to_email_html("- 顶层\n  - 缩进项")
        self.assertIn("margin:2px 0 2px 20px", h)

    def test_inline_bold_and_headings(self):
        h = sc.md_to_email_html("# 标题\n## 小节\n- VIX **14.32** 收盘")
        self.assertIn("<h1", h)
        self.assertIn("<h2", h)
        self.assertIn("<strong>14.32</strong>", h)
        self.assertNotIn("**", h)

    def test_source_html_is_escaped(self):
        # 报告正文来自外部数据 (标的名/错误信息), 不能让它注入标签
        h = sc.md_to_email_html("- <script>alert(1)</script> & 收盘")
        self.assertNotIn("<script>", h)
        self.assertIn("&lt;script&gt;", h)
        self.assertIn("&amp;", h)

    def test_style_block_is_only_progressive_enhancement(self):
        # Gmail 手机版可能剥掉 <style> — 所以 <style> 里只放媒体查询,
        # 剥掉之后剩下的内联样式必须仍然是一份完整可读的桌面版
        h = sc.md_to_email_html("# 标题\n- 一行")
        self.assertIn("@media", h)
        _, _, rest = h.partition("</style>")
        self.assertNotIn("@media", rest)          # 媒体查询只此一处
        self.assertIn("style=", rest)             # 正文仍是内联样式

    def test_cards_are_the_default_table_needs_a_wide_screen(self):
        md = "| 标的 | 状态 | 收盘 |\n|---|---|---|\n| QQQ | 回调中 | 709.24 |"
        h = sc.md_to_email_html(md)
        # 默认状态必须选"CSS 全被剥掉时仍可读"的那个 = 卡片。实测默认给
        # 宽表时, Gmail 手机版看到的是一张截断的表 (媒体查询被剥掉)
        self.assertIn('class="wl-wide" style="display:none', h)
        self.assertNotIn('class="wl-narrow" style="display:none', h)
        self.assertIn("min-width:601px", h)
        self.assertIn(".wl-wide{display:block!important}", h)
        self.assertIn(".wl-narrow{display:none!important}", h)
        self.assertEqual(h.count("QQQ"), 2)       # 两版各一次

    def test_html_stays_well_under_gmail_clip_limit(self):
        # Gmail 超过 ~102KB 会截断成 "[Message clipped]" — 报告被静默切掉
        # 一半比不发还糟。20 只标的的概览表 + 每只两行 note 是个偏悲观的
        # 规模, 留足余量。
        head = "| 标的 | 价值区 | 收盘 | 状态 | 操作 | Δ% | vs20日 | 量比 | 三选二 | iv/rv | IVP |"
        sep = "|" + "---|" * 11
        rows = ["| SYM%02d | 380-440 (上方+16%%) | 709.24 | 回调中(20日线下) "
                "| 设区间 | +1.2 | -0.0%% | 1.0x | 低✓ 收· 破· | 25/47%% | 62 |" % i
                for i in range(20)]
        notes = []
        for i in range(20):
            notes += [f"### SYM{i:02d} — 右侧确认",
                      "- 位置: 收盘 124.72 (+16.6%) · 20日线 +22.4% · 200日线 +31.2%",
                      "- **今日右侧确认**: 不再新低 + 突破20日高 (量比 2.8x)"]
        md = "\n".join(["# 标题", "", head, sep] + rows + [""] + notes)
        size = len(sc.md_to_email_html(md).encode("utf-8"))
        self.assertLess(size, 90_000, f"{size} 字节, 逼近 Gmail 102KB 截断线")

    def test_style_lives_in_head_of_a_full_document(self):
        # Gmail 只认 <head> 里的 <style>, 正文里的会被直接剥掉 —— 这正是
        # 手机端媒体查询完全不生效的原因
        h = sc.md_to_email_html("# 标题\n- 一行")
        self.assertTrue(h.startswith("<!DOCTYPE html>"))
        head = h[:h.index("</head>")]
        self.assertIn("<style>", head)
        self.assertIn("@media", head)
        self.assertIn('name="viewport"', head)
        self.assertNotIn("<style", h[h.index("</head>"):])

    def test_narrow_cards_label_each_value(self):
        md = ("| 标的 | 状态 | 收盘 | 量比 | 价值区 |\n|---|---|---|---|---|\n"
              "| QQQ | 回调中 | 709.24 | 1.0x | 未设 |")
        h = sc.md_to_email_html(md)
        cards = h.split('class="wl-narrow"')[1]
        self.assertIn("收盘", cards)               # 卡片里带列名当标签
        self.assertIn("709.24", cards)
        self.assertIn("量比", cards)
        self.assertNotIn("价值区", cards)          # 空值列不占位

    def test_narrow_card_subtitle_follows_state_column(self):
        # 概览表把价值区提到第二列后, 卡片标题旁的副标题必须还是状态 —
        # 按列序写死会变成 "QQQ 未设" (无 zone 的标的) 或 "SYM —"
        md = ("| 标的 | 价值区 | 状态 | 收盘 |\n|---|---|---|---|\n"
              "| QQQ | 未设 | 回调中 | 709.24 |")
        cards = sc.md_to_email_html(md).split('class="wl-narrow"')[1]
        title = cards[cards.index("QQQ"):cards.index("</div>")]
        self.assertIn("回调中", title)             # 副标题 = 状态
        self.assertNotIn("未设", title)            # 不是价值区, 更不是空值
        self.assertIn("收盘", cards)               # 其余列照常带标签摊开

    def test_resend_payload_carries_both_text_and_html(self):
        from unittest.mock import patch, MagicMock
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen.update(json.loads(req.data.decode()))
            m = MagicMock()
            m.status = 200
            m.__enter__ = lambda s: m
            m.__exit__ = lambda *a: False
            return m

        import tempfile
        from pathlib import Path
        # delete=False + 不清理 = 每跑一次测试就往仓库目录掉一个 tmpXXXX.md
        f = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                        encoding="utf-8")
        self.addCleanup(Path(f.name).unlink, missing_ok=True)
        f.write("# 报告\n\n- 🟢 **AAA** LEAP: BUY\n")
        f.close()
        env = {"SCAN_EMAIL_TO": "me@example.com",
               "SCAN_RESEND_API_KEY": "re_test"}
        with patch.dict(sc.os.environ, env, clear=True), \
                patch.object(sc.urllib.request, "urlopen", fake_urlopen):
            sc.send_email_report(Path(f.name), "[watchlist] test")
        self.assertIn("**AAA**", seen["text"])       # 纯文本回落保持原样
        self.assertIn("<strong>AAA</strong>", seen["html"])
        self.assertIn("border-left:4px solid #16a34a", seen["html"])


class TestEmailTransport(unittest.TestCase):
    """DigitalOcean 封锁 droplet 的出站 SMTP (25/465/587/2525 全部静默超时,
    443 正常) — 云上必须走 HTTPS 邮件 API, 所以 transport 的选择要可测。"""

    def _report(self):
        import tempfile
        from pathlib import Path
        # delete=False + 不清理 = 每跑一次测试就往仓库目录掉一个 tmpXXXX.md
        f = tempfile.NamedTemporaryFile("w", suffix=".md", delete=False,
                                        encoding="utf-8")
        self.addCleanup(Path(f.name).unlink, missing_ok=True)
        f.write("# 报告正文\n")
        f.close()
        return Path(f.name)

    def test_resend_key_takes_priority_over_smtp(self):
        from unittest.mock import patch, MagicMock
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            # urllib 的默认 UA 会被 Resend 前面的 Cloudflare 拦掉 (403 +
            # "error code: 1010"), 所以必须显式设置
            seen["ua"] = req.headers.get("User-agent")
            seen["body"] = json.loads(req.data.decode())
            m = MagicMock()
            m.status = 200
            m.__enter__ = lambda s: m
            m.__exit__ = lambda *a: False
            return m

        # 同时配了 SMTP 也不该走 SMTP — 云上那条根本连不通
        env = {"SCAN_EMAIL_TO": "me@example.com",
               "SCAN_RESEND_API_KEY": "re_test",
               "SCAN_EMAIL_FROM": "onboarding@resend.dev",
               "SCAN_SMTP_HOST": "smtp.gmail.com"}
        with patch.dict(sc.os.environ, env, clear=False), \
                patch.object(sc.urllib.request, "urlopen", fake_urlopen), \
                patch("smtplib.SMTP", side_effect=AssertionError("不该走 SMTP")):
            sc.send_email_report(self._report(), "[watchlist] test")
        self.assertEqual(seen["url"], "https://api.resend.com/emails")
        self.assertEqual(seen["auth"], "Bearer re_test")
        self.assertTrue(seen["ua"] and "urllib" not in seen["ua"].lower())
        self.assertEqual(seen["body"]["to"], ["me@example.com"])
        self.assertIn("报告正文", seen["body"]["text"])

    def test_missing_both_transports_raises(self):
        from unittest.mock import patch
        with patch.dict(sc.os.environ, {"SCAN_EMAIL_TO": "me@example.com"},
                        clear=True):
            with self.assertRaises(RuntimeError):
                sc.send_email_report(self._report(), "x")

    def test_resend_http_error_surfaces_reason_not_key(self):
        # 报错要能看出原因 (未验证域名/额度), 但绝不能把 api key 带进日志
        import urllib.error
        from unittest.mock import patch
        err = urllib.error.HTTPError(
            "https://api.resend.com/emails", 403, "Forbidden", {},
            io.BytesIO(b'{"message":"domain not verified"}'))
        env = {"SCAN_EMAIL_TO": "me@example.com",
               "SCAN_RESEND_API_KEY": "re_secret_do_not_leak"}
        with patch.dict(sc.os.environ, env, clear=True), \
                patch.object(sc.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                sc.send_email_report(self._report(), "x")
        self.assertIn("domain not verified", str(cm.exception))
        self.assertNotIn("re_secret_do_not_leak", str(cm.exception))


class TestDeliveryLoop(unittest.TestCase):
    """发信失败的自愈闭环 (五轮评审): 报告先落盘、邮件后发 — 发信失败时
    dedup 门会把 DST 双保险的下一次 fire 拦回, watchdog 又只查 .md 存在,
    FAILED 告警走同一条坏通道 → 一次 Resend 抖动 = 当天报告静默丢失。
    修复 = .sent 投递凭证 + 每次 --email fire 先补发 + 只送达后 ping 心跳。"""

    D = "2026-09-04"

    def _reports(self, stage="NORMAL", with_marker=False):
        import tempfile
        from pathlib import Path
        import shutil
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / f"{self.D}-close.md").write_text("# 报告", encoding="utf-8")
        (tmp / "latest-close.json").write_text(
            json.dumps({"regime": {"stage": stage}}), encoding="utf-8")
        if with_marker:
            (tmp / f"{self.D}-close.sent").write_text("x", encoding="utf-8")
        return tmp

    def _now(self):
        return datetime.fromisoformat(f"{self.D}T16:45:00").replace(tzinfo=sc.ET)

    def test_resend_pending_sends_and_writes_marker(self):
        from unittest.mock import patch
        tmp = self._reports(stage="STAGE2_WINDOW")
        sent = {}
        with patch.object(sc, "REPORTS", tmp), \
                patch.object(sc, "send_email_report",
                             lambda p, subj: sent.update(path=p, subj=subj)):
            sc.resend_pending_reports(self.D, self._now())
        self.assertEqual(sent["path"].name, f"{self.D}-close.md")
        # 补发主题带 resend 字样 (不进原 Gmail 会话) 且带 regime 阶段
        self.assertIn("resend", sent["subj"])
        self.assertIn("STAGE2_WINDOW", sent["subj"])
        self.assertTrue((tmp / f"{self.D}-close.sent").exists())

    def test_already_sent_is_noop(self):
        from unittest.mock import patch
        tmp = self._reports(with_marker=True)
        with patch.object(sc, "REPORTS", tmp), \
                patch.object(sc, "send_email_report",
                             side_effect=AssertionError("不该重发")):
            sc.resend_pending_reports(self.D, self._now())

    def test_resend_failure_keeps_marker_absent_and_survives(self):
        # 补发失败不能弄死本次运行 (当前窗口的正式扫描还要跑), 也不能写
        # 凭证 — 留给下一个 fire 再试 + watchdog 报"已写盘未送达"
        from unittest.mock import patch
        tmp = self._reports()
        with patch.object(sc, "REPORTS", tmp), \
                patch.object(sc, "send_email_report",
                             side_effect=RuntimeError("Resend 500")):
            sc.resend_pending_reports(self.D, self._now())   # 不抛
        self.assertFalse((tmp / f"{self.D}-close.sent").exists())

    def test_heartbeat_only_when_configured(self):
        from unittest.mock import patch
        hits = []
        with patch.dict(sc.os.environ, {}, clear=True), \
                patch.object(sc.urllib.request, "urlopen",
                             lambda *a, **k: hits.append(a)):
            sc.ping_heartbeat()
        self.assertEqual(hits, [])
        with patch.dict(sc.os.environ,
                        {"SCAN_HEARTBEAT_URL": "https://hc-ping.com/x"},
                        clear=True), \
                patch.object(sc.urllib.request, "urlopen",
                             lambda url, timeout=None: hits.append(url)):
            sc.ping_heartbeat()
        self.assertEqual(hits, ["https://hc-ping.com/x"])

    def test_heartbeat_failure_is_swallowed(self):
        from unittest.mock import patch
        with patch.dict(sc.os.environ,
                        {"SCAN_HEARTBEAT_URL": "https://hc-ping.com/x"},
                        clear=True), \
                patch.object(sc.urllib.request, "urlopen",
                             side_effect=RuntimeError("down")):
            sc.ping_heartbeat()   # 心跳挂了不影响主流程


class TestClock(unittest.TestCase):
    def _et(self, h, m, weekday_date="2026-08-07"):  # a Friday
        return datetime.fromisoformat(f"{weekday_date}T{h:02d}:{m:02d}:00").replace(
            tzinfo=sc.ET)

    def test_windows(self):
        self.assertEqual(sc.resolve_mode("auto", self._et(9, 45)), "open")
        self.assertEqual(sc.resolve_mode("auto", self._et(10, 45)), "open")
        self.assertEqual(sc.resolve_mode("auto", self._et(15, 45)), "close")
        self.assertIsNone(sc.resolve_mode("auto", self._et(12, 0)))
        self.assertIsNone(sc.resolve_mode("auto", self._et(16, 45)))
        # weekend
        self.assertIsNone(sc.resolve_mode("auto", self._et(9, 45, "2026-08-08")))
        # explicit mode bypasses the clock
        self.assertEqual(sc.resolve_mode("close", self._et(3, 0)), "close")


class _FakeTk:
    def __init__(self, calendar):
        self.calendar = calendar


class TestNextEarnings(unittest.TestCase):
    def test_et_clock_not_host_clock(self):
        # 布里斯班机上 close 扫描时本机日历日 = ET+1 — date.today() 会把
        # 当天 AMC 财报当过去滤掉, 在公布前几小时放行跨财报 CSP (五轮评审)。
        # 把 scanner 的 date.today 钉成 ET+1 模拟那台机器: 修复后不再引用它
        from unittest.mock import patch
        et_today = datetime.now(sc.ET).date()

        class _BrisbaneDate(sc.date):
            @classmethod
            def today(cls):
                return et_today + timedelta(days=1)

        with patch.object(sc, "date", _BrisbaneDate):
            out = sc.next_earnings(_FakeTk({"Earnings Date": [et_today]}))
        self.assertEqual(out, et_today.isoformat())

    def test_no_upcoming_is_empty_string(self):
        past = datetime.now(sc.ET).date() - timedelta(days=30)
        self.assertEqual(sc.next_earnings(_FakeTk({"Earnings Date": [past]})), "")

    def test_failed_lookup_is_none(self):
        # yfinance 吞 HTTP 错回空 calendar — 契约: None=失败, caller 必须 warn
        self.assertIsNone(sc.next_earnings(_FakeTk({})))
        self.assertIsNone(sc.next_earnings(_FakeTk(None)))

    def test_nearest_of_multiple(self):
        t = datetime.now(sc.ET).date()
        cal = {"Earnings Date": [t + timedelta(days=95), t + timedelta(days=4)]}
        self.assertEqual(sc.next_earnings(_FakeTk(cal)),
                         (t + timedelta(days=4)).isoformat())


VX_SETTLE_SAMPLE = """Product,Symbol,Expiration Date,Price
VX,VX35/U6,2026-09-02,17.2528
VX,VX36/U6,2026-09-09,17.2528
VX,VX/U6,2026-09-16,17.2528
VX,VX38/U6,2026-09-23,17.2528
VX,VX40/V6,2026-10-07,17.2528
VX,VX/V6,2026-10-21,18.8535
VX,VX/X6,2026-11-18,19.3489
VX,VX/Z6,2026-12-16,19.4024
VX,VX/F7,2027-01-20,20.4496
VXM,VXM/U6,2026-09-16,17.2528
VXM,VXM/V6,2026-10-21,18.8535
VA,VA/U6,2026-09-18,194.25
"""


class TestVXCurve(unittest.TestCase):
    def test_parse_monthlies_only(self):
        # weekly 行 (VX35/U6 ...) 带的是前月填充价, 必须剔除; VXM/VA 同剔
        rows = sc.parse_vx_settlement(VX_SETTLE_SAMPLE)
        self.assertEqual([e for e, _p in rows],
                         ["2026-09-16", "2026-10-21", "2026-11-18",
                          "2026-12-16", "2027-01-20"])
        self.assertAlmostEqual(rows[0][1], 17.2528)
        self.assertAlmostEqual(rows[1][1], 18.8535)

    def test_parse_garbage_rows(self):
        text = ("Product,Symbol,Expiration Date,Price\n"
                "VX,VX/U6,2026-09-16,bad\nVX,VX/V6\n")
        self.assertEqual(sc.parse_vx_settlement(text), [])

    def test_curve_state(self):
        self.assertEqual(sc.vx_curve_state([17.25, 18.85, 19.35, 19.40, 20.45]),
                         "CONTANGO")                       # 2026-09-01 实况
        self.assertEqual(sc.vx_curve_state([21.0, 19.0, 19.5, 20.0]),
                         "PARTIAL_BACKWARDATION")          # M1>M2 但后端翘
        self.assertEqual(sc.vx_curve_state([28.0, 25.0, 23.5, 22.0, 21.0]),
                         "FULL_BACKWARDATION")             # 2020-03 形态
        # n_front 截断: 前5递减、第6个月翘起 → 仍算全曲线倒挂
        self.assertEqual(
            sc.vx_curve_state([28, 25, 23.5, 22, 21, 24], n_front=5),
            "FULL_BACKWARDATION")
        self.assertIsNone(sc.vx_curve_state([17.0]))       # 合约不足无读数

    def test_curve_state_ties(self):
        # 二轮评审 finding: feed 会填充未成交行造成相邻平价 (tie) —
        # 全程非升且至少一段真跌 = 实质全曲线倒挂, 不因 tie 降级/静默
        self.assertEqual(sc.vx_curve_state([28, 25, 25, 22, 21]),
                         "FULL_BACKWARDATION")             # tie 在中段
        self.assertEqual(sc.vx_curve_state([25, 25, 22, 21, 20]),
                         "FULL_BACKWARDATION")             # tie 开头
        self.assertEqual(sc.vx_curve_state([25.0, 25.0, 25.0, 25.0]),
                         "CONTANGO")                       # 全平 ≠ 倒挂

    def test_partial_needs_inverted_front_pair(self):
        # 三轮评审 (Copilot): 平价前端 + 后段单点回落但整体上行 = 混合曲线,
        # 判成"前端承压"会让警告渲染出 "25.00 > 25.00" 的自相矛盾读数
        self.assertEqual(sc.vx_curve_state([25, 25, 24, 30, 31]), "CONTANGO")
        self.assertEqual(sc.vx_curve_state([25, 24, 26, 27, 28]),
                         "PARTIAL_BACKWARDATION")

    @staticmethod
    def _gates(stage, vx, vvix=None, move=None, vix_level=16.0, s=None):
        return sc.assess_vol_gates(stage, vx, vvix or {}, move or {},
                                   vix_level, s or sc.SETTINGS_DEFAULTS)

    def test_gates_full_backwardation_halts(self):
        vx = {"state": "FULL_BACKWARDATION", "m1": 28.0, "m2": 25.0,
              "as_of": "2026-09-01"}
        g = self._gates("STAGE1_DEEP", vx)
        self.assertIn("全曲线倒挂", g["halt_csp"])
        self.assertIn("全曲线倒挂", g["halt_new_longs"])
        # 开关只放行 CSP (剧本恐慌档), LEAP/spread 仍拦 — 且消息必须
        # 与实际拦截范围一致 (二轮评审: 不能一边发 CSP 票一边写"停开新票")
        s_off = {**sc.SETTINGS_DEFAULTS, "vx_full_backwardation_halt": False}
        g = self._gates("STAGE1_DEEP", vx, s=s_off)
        self.assertIsNone(g["halt_csp"])
        self.assertIn("CSP 已按", g["halt_new_longs"])
        self.assertIn("放行", g["halt_new_longs"])
        self.assertNotIn("停开新 CSP", g["halt_new_longs"])

    def test_gates_partial_warns_contango_silent(self):
        g = self._gates("NORMAL", {"state": "PARTIAL_BACKWARDATION",
                                   "m1": 21.0, "m2": 19.0, "as_of": "x"})
        self.assertIsNone(g["halt_csp"])
        self.assertTrue(any("局部倒挂" in w for w in g["warnings"]))
        g = self._gates("NORMAL", {"state": "CONTANGO", "m1": 17.0,
                                   "m2": 19.0, "as_of": "x"})
        self.assertEqual((g["halt_csp"], g["halt_new_longs"], g["warnings"]),
                         (None, None, []))

    def test_gates_degrade_on_feed_error(self):
        # 数据坏 = 门失效, 不硬拦
        g = self._gates("NORMAL", {"error": "HTTPError: 503"},
                        {"error": "x"}, {"error": "y"})
        self.assertEqual((g["halt_csp"], g["halt_new_longs"], g["warnings"]),
                         (None, None, []))

    def test_vvix_halt_only_in_normal(self):
        # NORMAL 期 VVIX >= 110 = 平静表面下的对冲拥挤 → 停开新 CSP;
        # STAGE1 恐慌档 / STAGE2 解除窗 VVIX 高是常态, 不拦 (剧本优先)
        vx = {"state": "CONTANGO", "m1": 17.0, "m2": 19.0, "as_of": "x"}
        g = self._gates("NORMAL", vx, {"value": 115.0, "as_of": "x"})
        self.assertIn("VVIX", g["halt_csp"])
        self.assertIsNone(g["halt_new_longs"])
        for stage in ("STAGE1", "STAGE1_DEEP", "STAGE2_WINDOW"):
            g = self._gates(stage, vx, {"value": 150.0, "as_of": "x"})
            self.assertIsNone(g["halt_csp"], stage)
        g = self._gates("NORMAL", vx, {"value": 109.9, "as_of": "x"})
        self.assertIsNone(g["halt_csp"])

    def test_vx_halt_message_wins_over_vvix(self):
        g = self._gates("NORMAL",
                        {"state": "FULL_BACKWARDATION", "m1": 28.0,
                         "m2": 25.0, "as_of": "x"},
                        {"value": 150.0, "as_of": "x"})
        self.assertIn("全曲线倒挂", g["halt_csp"])

    def test_move_divergence_warns_not_halts(self):
        # MOVE 破线且 VIX 平静 = 债波先行预警; VIX 已经起来就不是背离
        vx = {"state": "CONTANGO", "m1": 17.0, "m2": 19.0, "as_of": "x"}
        g = self._gates("NORMAL", vx, move={"value": 105.0, "as_of": "x"},
                        vix_level=16.0)
        self.assertTrue(any("MOVE" in w for w in g["warnings"]))
        self.assertIsNone(g["halt_csp"])
        g = self._gates("NORMAL", vx, move={"value": 105.0, "as_of": "x"},
                        vix_level=22.0)
        self.assertEqual(g["warnings"], [])
        g = self._gates("NORMAL", vx, move={"value": 95.0, "as_of": "x"},
                        vix_level=16.0)
        self.assertEqual(g["warnings"], [])


class TestVXHaltScope(unittest.TestCase):
    VX = {"state": "FULL_BACKWARDATION", "m1": 28.0, "m2": 25.0,
          "as_of": "2026-09-01"}

    def _gates(self, stage, halt=True, m1=28.0, m2=25.0):
        s = dict(sc.SETTINGS_DEFAULTS, vx_full_backwardation_halt=halt)
        vx = dict(self.VX, m1=m1, m2=m2)
        return sc.assess_vol_gates(stage, vx, {}, {}, 16.0, s)

    def test_override_only_releases_panic_stage_csp(self):
        # 三轮评审 (Copilot, critical): 开关的语义是"放行**剧本恐慌档**
        # CSP", 而 VX 全曲线倒挂可以与 VIX/VIX3M NORMAL 并存 — 让它在
        # NORMAL 期放行普通 CSP 正好放掉本门要拦的场景
        g1 = self._gates("STAGE1_DEEP", halt=False)
        self.assertIsNone(g1["halt_csp"])          # 恐慌档 CSP 放行
        self.assertIsNotNone(g1["halt_new_longs"])  # LEAP/spread 仍拦
        g2 = self._gates("NORMAL", halt=False)
        self.assertIsNotNone(g2["halt_csp"])       # NORMAL 期不放行
        self.assertIn("只放行剧本恐慌档", g2["halt_csp"])
        self.assertIn("NORMAL", g2["halt_csp"])
        g3 = self._gates("STAGE2_WINDOW", halt=False)
        self.assertIsNotNone(g3["halt_csp"])

    def test_full_message_never_asserts_strict_inequality(self):
        # FULL 容忍相邻平价 → 前端可能 m1 == m2, 消息只渲染观测值
        msg = self._gates("NORMAL", m1=25.0, m2=25.0)["halt_csp"]
        self.assertIn("M1 25.00 / M2 25.00", msg)
        self.assertNotIn("25.00 > ", msg)


class TestLeapAndRetestGates(unittest.TestCase):
    def test_pending_leap_rechecks_todays_state(self):
        # 三轮评审 (Copilot, critical): halt 期间跌破 20 日线 → 当日已是
        # PULLBACK, 而 state.json 的失效要等本次扫描之后才写 — 不复核就会
        # 在止损出局当天补一张新多头票
        self.assertTrue(sc.normal_leap_gate(False, True, "CONFIRMED"))
        self.assertTrue(sc.normal_leap_gate(False, True, "TREND"))
        self.assertFalse(sc.normal_leap_gate(False, True, "PULLBACK"))
        self.assertFalse(sc.normal_leap_gate(False, True, "LEFT_ZONE"))
        self.assertTrue(sc.normal_leap_gate(True, False, "CONFIRMED"))
        self.assertFalse(sc.normal_leap_gate(False, False, "TREND"))

    def test_retest_deferred_after_price_leaves_20dma(self):
        # 被拦当日落 pending; 次日价格已离开 20 日线仍要补发
        self.assertTrue(sc.retest_gate("TREND", True, False, False, "NORMAL"))
        self.assertTrue(sc.retest_gate("TREND", False, False, True, "NORMAL"))
        self.assertFalse(sc.retest_gate("TREND", False, False, False, "NORMAL"))
        self.assertFalse(sc.retest_gate("TREND", True, True, True, "NORMAL"))
        self.assertFalse(
            sc.retest_gate("TREND", True, False, True, "STAGE1_DEEP"))
        self.assertFalse(
            sc.retest_gate("PULLBACK", True, False, True, "NORMAL"))


class TestVXExpiryFilter(unittest.TestCase):
    def test_expired_front_contract_dropped(self):
        # 三轮评审 (critical): VX 月度到期日当天上午结算后, 前一交易日的
        # 文件里那份**当日到期**的合约仍在, sorted 后被当成 M1。它收敛到
        # 现货 VIX, VIX 一跳就把曲线前端顶起来 — 而它已经不可交易 →
        # 拿一个死合约误报硬停牌
        from unittest.mock import patch, MagicMock
        today = datetime.now(sc.ET).date()
        later = [(today + timedelta(days=30 * i)).isoformat()
                 for i in range(1, 6)]
        # 活曲线前段小幅递减、末月回升 = 局部倒挂 (只该给 warning)。
        # 当日到期那份被现货 VIX 顶到 34 顶在最前面, 整条链就变成逐对
        # 非升 = FULL_BACKWARDATION 硬停牌 — 一个已死合约把警告升级成停牌
        live = [20.0, 19.5, 19.0, 18.5, 21.0]
        rows = [(today.isoformat(), 34.0)] + list(zip(later, live))
        self.assertEqual(sc.vx_curve_state([px for _e, px in rows][:5]),
                         "FULL_BACKWARDATION")        # 不滤 = 误报硬停牌
        self.assertEqual(sc.vx_curve_state(live),
                         "PARTIAL_BACKWARDATION")     # 真实形态 = 仅警告
        fake = MagicMock()
        fake.read.return_value.decode.return_value = "csv"
        with patch.object(sc.urllib.request, "urlopen", return_value=fake), \
             patch.object(sc, "parse_vx_settlement", return_value=rows):
            out = sc.fetch_vx_curve()
        self.assertEqual(out["m1_exp"], later[0])   # 死合约不当 M1
        self.assertEqual(out["n_contracts"], 5)
        self.assertEqual(out["state"], "PARTIAL_BACKWARDATION")  # 降回警告


class TestTicketSkipLine(unittest.TestCase):
    def test_regime_halt_abbreviated(self):
        long_reason = "VX 期货全曲线倒挂 " + "详细理由" * 40
        line = sc.ticket_skip_line("CSP", {"skip_reason": long_reason,
                                           "regime_halt": True})
        self.assertIn("见市场状态", line)
        self.assertNotIn("详细理由" * 40, line)

    def test_ordinary_skip_keeps_full_reason(self):
        line = sc.ticket_skip_line(
            "LEAP", {"skip_reason": "财报 2026-09-09 就在 6 天后"})
        self.assertIn("财报 2026-09-09 就在 6 天后", line)


class TestDailyBarStale(unittest.TestCase):
    """三轮评审: 陈旧日线改成**日期比对 + 硬拦**。

    价差判据 (forward 反解现货 vs 日线收盘) 分不开"日线陈旧"和"难借券的
    高借券费" —— 后者让 forward 合法地低于现货好几个百分点, 拿它硬拦会把
    那些标的永久静音。日期不会骗人。"""

    def _cc(self, chain_day, expiries=None):
        ts = pd.Timestamp(f"{chain_day} 19:58", tz="UTC")   # = 当日 15:58 ET
        df = pd.DataFrame([{"strike": 100.0, "lastTradeDate": ts}])
        return _FakeCC(_FakeChain(df, df), expiries=expiries)

    def test_works_outside_the_rr_dte_window(self):
        # 四轮评审: 判据原本借用 rr_dte 的 20-60 DTE 窗口选到期日 — 没有
        # 到期日落在窗口内的标的会让整道门**静默失效**, 而它照样出 CSP/
        # LEAP 票, 那些票就建立在过期价格上
        cc = self._cc("2026-09-03", expiries=[("2026-09-11", 7)])
        self.assertIsNotNone(sc.daily_bar_stale(cc, "2026-09-02"))

    def test_falls_through_to_next_expiry_without_trades(self):
        # 最近一档没有成交记录时往后再试
        ts = pd.Timestamp("2026-09-03 19:58", tz="UTC")
        empty = _FakeChain(pd.DataFrame([{"strike": 100.0}]),
                           pd.DataFrame([{"strike": 100.0}]))
        traded = _FakeChain(
            pd.DataFrame([{"strike": 100.0, "lastTradeDate": ts}]),
            pd.DataFrame([{"strike": 100.0, "lastTradeDate": ts}]))

        class _CC:
            def expiries(self):
                return [("2026-09-11", 7), ("2026-09-18", 14)]

            def chain(self, exp):
                return empty if exp == "2026-09-11" else traded

        self.assertIsNotNone(sc.daily_bar_stale(_CC(), "2026-09-02"))

    def test_chain_newer_than_bar_is_stale(self):
        # 2026-09-04 事故的形态: 日线停在 9/2, 期权链已有 9/3 的成交
        msg = sc.daily_bar_stale(self._cc("2026-09-03"), "2026-09-02")
        self.assertIsNotNone(msg)
        self.assertIn("2026-09-02", msg)
        self.assertIn("2026-09-03", msg)

    def test_same_day_is_fresh(self):
        self.assertIsNone(sc.daily_bar_stale(self._cc("2026-09-03"), "2026-09-03"))

    def test_illiquid_chain_is_not_stale(self):
        # 单向判据: 期权好几天没成交是流动性问题, 不是数据问题 — 不能反过来报
        self.assertIsNone(sc.daily_bar_stale(self._cc("2026-08-28"), "2026-09-03"))

    def test_no_trade_dates_is_not_stale(self):
        df = pd.DataFrame([{"strike": 100.0}])
        self.assertIsNone(sc.daily_bar_stale(_FakeCC(_FakeChain(df, df)), "2026-09-03"))


class TestBlockedTicket(unittest.TestCase):
    def test_stale_beats_regime_and_stays_per_ticker(self):
        # 价格都不可信时, 市场门拦没拦这一票已无意义 → 陈旧优先。
        # 且陈旧是每标的问题, 不能被 action_block 合并进"全市场"那一行
        t = sc.blocked_ticket("日线停在 2026-09-02", "全市场硬停牌")
        self.assertIn("2026-09-02", t["skip_reason"])
        self.assertTrue(t["stale_data"])
        self.assertFalse(sc._regime_halted(t))

    def test_regime_path_keeps_merge_flag(self):
        t = sc.blocked_ticket(None, "VX 全曲线倒挂")
        self.assertTrue(sc._regime_halted(t))


class TestCSPWindow(unittest.TestCase):
    def test_no_zone_never_opens(self):
        # 没有接货价就没有 CSP — 任何 regime 都一样 (ORCL 教训)
        for stage in ("NORMAL", "STAGE1", "STAGE1_DEEP", "STAGE2_WINDOW"):
            self.assertFalse(sc.csp_window_open(None, False, stage))

    def test_normal_needs_zone_proximity(self):
        self.assertTrue(sc.csp_window_open([80, 95], True, "NORMAL"))
        self.assertFalse(sc.csp_window_open([80, 95], False, "NORMAL"))

    def test_stage1_and_stage2_open_regardless_of_price(self):
        # 恐慌档 (既有) 和解除窗加成 (新增): 价格在带上方也出票 —
        # 行权价仍被接货带上沿硬约束, 年化不过线自然拦
        self.assertTrue(sc.csp_window_open([80, 95], False, "STAGE1"))
        self.assertTrue(sc.csp_window_open([80, 95], False, "STAGE1_DEEP"))
        self.assertTrue(sc.csp_window_open([80, 95], False, "STAGE2_WINDOW"))


class TestStage2LeapGate(unittest.TestCase):
    """dedup key 的烧毁不再由 gate 决定 — 调用方在**真票发出后**才写
    r[\"leap_window\"] (五轮评审: 之前在门口就烧, leap_ticket 一句
    \"财报 5 天后\"的临时 skip 就吞掉整个 10 天解除窗的补发;
    NORMAL 的 leap_pending 补偿从不护 STAGE2)。gate 只回答一件事:
    价格条件满足且本窗口还没出过真票。"""

    def test_want_while_blocked_key_survives(self):
        # halt/陈旧/票据级 skip 期间: want=True (⏸ 行可见), key 不烧 —
        # 次日 prev_leap_window 仍是 None, 同窗口内继续想出票
        self.assertTrue(sc.stage2_leap_gate(True, None, "2026-08-28"))
        self.assertTrue(sc.stage2_leap_gate(True, None, "2026-08-28"))

    def test_key_burned_after_real_ticket_dedups(self):
        # 真票发出当日调用方写入 leap_window=ep_end → 之后同窗口不再出
        self.assertFalse(sc.stage2_leap_gate(True, "2026-08-28", "2026-08-28"))

    def test_new_episode_new_key(self):
        # 上一窗口烧过的 key 不影响新 episode (ep_end 不同)
        self.assertTrue(sc.stage2_leap_gate(True, "2026-08-28", "2026-09-15"))

    def test_price_not_ok(self):
        self.assertFalse(sc.stage2_leap_gate(False, None, "2026-08-28"))


class TestActionBlockHaltDedup(unittest.TestCase):
    def _r(self, sym, **kw):
        base = {"symbol": sym, "error": None, "tech": {"close": 100},
                "notes": [], "state": "LEFT_ZONE", "leap": None, "csp": None,
                "iv30": None,
                "cfg": {"value_zone": [80, 95], "options": True}}
        base.update(kw)
        return base

    def test_regime_halt_merges_into_one_line(self):
        # 二轮评审 finding: 全市场硬停牌逐票重复 ~150 字长文 — 合并一行,
        # 全文只留在市场状态 ⛔ 行
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        halt = {"skip_reason": "VX 期货全曲线倒挂 (M1 28.00 > M2 25.00) — 停开新票",
                "regime_halt": True}
        rs = [self._r("AAA", csp=dict(halt)),
              self._r("BBB", csp=dict(halt), leap=dict(halt)),
              self._r("CCC", state="UPTREND")]
        text = "\n".join(sc.action_block(rs, ivdf))
        self.assertEqual(text.count("市场门拦下部分新票"), 1)
        # 三轮评审 (Copilot): VVIX 只拦 CSP、VX 开关下只拦 LEAP/spread —
        # 同一标的可以一边被拦一边有别的有效票, 汇总行不得写死"全市场停牌"
        self.assertNotIn("全市场", text)
        self.assertIn("AAA", text)
        self.assertIn("BBB", text)
        self.assertNotIn("VX 期货全曲线倒挂", text)   # 长文不进今日动作
        self.assertIn("其余今日无动作: CCC", text)

    def test_stale_symbols_surface_even_without_tickets(self):
        # 硬拦只在"本来就要出票"时才看得见 — 多数标的当天并不出票, 那时
        # 陈旧会完全静默, 而概览表照样印着过期收盘价 (实测: HOOD 价格陈旧
        # 16.5%, 报告里零提示)。陈旧必须无条件出现在"今日动作"那一屏
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        rs = [self._r("AAA", stale_data=True), self._r("BBB")]
        text = "\n".join(sc.action_block(rs, ivdf))
        self.assertIn("日线陈旧", text)
        self.assertIn("AAA", text)
        self.assertNotIn("其余今日无动作: AAA", text)   # 不能被算进"无动作"
        self.assertIn("BBB", text)

    def test_non_regime_skip_still_itemized(self):
        # 普通 skip (年化不足等) 照旧逐票 ⏸, 且 LEAP 行带工具前缀
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        rs = [self._r("AAA", csp={"skip_reason": "年化仅 6.0%"},
                      leap={"skip_reason": "财报 2026-09-09 就在 6 天后"})]
        text = "\n".join(sc.action_block(rs, ivdf))
        self.assertIn("⏸ **AAA** CSP: 年化仅", text)
        self.assertIn("⏸ **AAA** LEAP: 财报", text)
        self.assertNotIn("全市场硬停牌", text)


class TestCSPTicketZoneCap(unittest.TestCase):
    """CSP 的接货带上沿硬 cap — 整个 CSP 设计的唯一硬门 — 此前在
    csp_ticket 内零直接断言 (9/4 评审 D2 的 zone 特化落地)。合成链按
    BS 定价, 复用 TestRR25Snapshot 的 fake 基建路数。

    对 SETTINGS_DEFAULTS 的耦合余量 (改这些参数前先看这里):
    - test_strike_hard_capped: cap 87 必须真 binding (无 cap 时 delta 带
      自己选 88) — 选中的 86.5 档年化 ~11.9%, csp_min_annualized 上调到
      12+ 会误红本测试; sigma 调高不可行 (0.6 起 delta 带滑到 85 以下,
      cap 又不 binding)
    - test_thin_premium_skips 有 mid<0.20 与年化双门兜底, 对阈值不敏感
    - csp_dte_normal 必须仍含 21 DTE"""

    T_DTE = 21
    EXPIRIES = [("2026-10-02", 21)]

    def _cc(self, spot=100.0, sigma=0.50, oi=500):
        T = self.T_DTE / 365.0
        traded = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)
        rows = []
        for i in range(30):
            k = round(spot * (0.55 + 0.015 * i), 2)   # 55 … 98.5
            px = sc.bs_price(spot, k, T, sc.RATE, sigma, is_call=False)
            half = max(px * 0.02, 0.005)
            rows.append({"strike": k, "lastPrice": px, "lastTradeDate": traded,
                         "openInterest": oi, "bid": px - half, "ask": px + half})
        df = pd.DataFrame(rows)
        return _FakeCC(_FakeChain(pd.DataFrame([]), df),
                       expiries=list(self.EXPIRIES))

    def test_strike_hard_capped_at_zone_top(self):
        # cap 必须真 binding: 此 fixture 下无 cap 时 delta 带选 88.0
        # (> 87), 有 cap 时退到 86.5 — cap 被删掉或放松 (如 *1.05)
        # 本断言都会红。zone [80,95] 之类宽带是空转 pin: delta 带自己
        # 就选在 cap 之下, 删 cap 照样绿
        t = sc.csp_ticket(self._cc(), 100.0, None, "2026-11-20", "NORMAL",
                          [70.0, 87.0], sc.SETTINGS_DEFAULTS)
        self.assertNotIn("skip_reason", t)
        self.assertLessEqual(t["strike"], 87.0)

    def test_no_strike_under_cap_skips_whole_ticket(self):
        # zone 上沿低于链上全部行权价 → 整票 skip, 不是退而求其次选高 strike
        t = sc.csp_ticket(self._cc(), 100.0, None, "2026-11-20", "NORMAL",
                          [40.0, 50.0], sc.SETTINGS_DEFAULTS)
        self.assertIn("skip_reason", t)
        self.assertIn("上沿", t["skip_reason"])

    def test_thin_premium_skips(self):
        # 低 IV → 接货档年化过不了下限 → 拒票 (剧本: 改正股限价单)
        t = sc.csp_ticket(self._cc(sigma=0.10), 100.0, None, "2026-11-20",
                          "NORMAL", [80.0, 95.0], sc.SETTINGS_DEFAULTS)
        self.assertIn("skip_reason", t)
        self.assertIn("太薄", t["skip_reason"])

    def test_earnings_inside_window_blocks(self):
        # short 不跨财报: 窗口内唯一到期日在财报之后 → 整票 skip
        t = sc.csp_ticket(self._cc(), 100.0, None, "2026-09-20", "NORMAL",
                          [80.0, 95.0], sc.SETTINGS_DEFAULTS)
        self.assertIn("skip_reason", t)
        self.assertIn("财报", t["skip_reason"])


class TestLoadConfig(unittest.TestCase):
    """配置错误要在装载时炸 (zone 评审): 字符串 zone 此前能穿过校验,
    运行期变成被宽 except 吞掉的 TypeError; 拼错的键静默落回默认值;
    [tickers.spcx]+[tickers.SPCX] 合法共存, upper() 后 last-wins 丢条目。"""

    def _load(self, text):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "watchlist.toml"
            p.write_text(text, encoding="utf-8")
            return sc.load_config(p)

    def test_valid_config_loads_and_normalizes_zone(self):
        s, t = self._load(
            "[settings]\nnear_zone_pct = 4.0\n"
            "[tickers.rklb]\nhigh_beta = true\nvalue_zone = [45, 57.5]\n")
        self.assertEqual(s["near_zone_pct"], 4.0)
        self.assertEqual(t["RKLB"]["value_zone"], [45.0, 57.5])
        self.assertIsInstance(t["RKLB"]["value_zone"][0], float)

    def test_repo_watchlist_still_loads(self):
        # 白名单/类型门不得误伤真实配置文件
        s, t = sc.load_config()
        self.assertTrue(t)

    def test_unknown_settings_key_raises(self):
        # 已知 D1: vvix_halt 拼成 vivx_halt → 默认值顶着你以为改过的名字生效
        with self.assertRaises(ValueError):
            self._load("[settings]\nvivx_halt = 105\n[tickers.QQQ]\n")

    def test_settings_value_type_gated(self):
        # TOML 引号手滑: 键名合法、值是字符串 — 会在 render_open 的
        # abs(gap) >= s[...] 处裸崩 (宽 except 之外), 无报告无邮件
        with self.assertRaises(ValueError):
            self._load('[settings]\ngap_alert_pct = "1.5"\n[tickers.QQQ]\n')
        with self.assertRaises(ValueError):
            self._load("[settings]\nvvix_halt = true\n[tickers.QQQ]\n")
        with self.assertRaises(ValueError):
            self._load("[settings]\ncsp_dte_normal = [12]\n[tickers.QQQ]\n")
        # int 给 float 键是合法宽容 (TOML 里 5 与 5.0 都常见)
        s, _ = self._load("[settings]\nnear_zone_pct = 5\n[tickers.QQQ]\n")
        self.assertEqual(s["near_zone_pct"], 5)

    def test_unknown_ticker_key_raises(self):
        with self.assertRaises(ValueError):
            self._load("[tickers.QQQ]\nvalue_zon = [600, 700]\n")

    def test_string_zone_raises(self):
        with self.assertRaises(ValueError):
            self._load('[tickers.GOOG]\nvalue_zone = ["315", "340"]\n')

    def test_scalar_zone_raises_valueerror_not_typeerror(self):
        # 旧代码在 len(int) 处裸崩 TypeError, 整个 load 无提示炸掉
        with self.assertRaises(ValueError):
            self._load("[tickers.GOOG]\nvalue_zone = 315\n")

    def test_bool_zone_raises(self):
        with self.assertRaises(ValueError):
            self._load("[tickers.GOOG]\nvalue_zone = [true, 340]\n")

    def test_nonfinite_zone_raises(self):
        with self.assertRaises(ValueError):
            self._load("[tickers.GOOG]\nvalue_zone = [315, inf]\n")
        with self.assertRaises(ValueError):
            self._load("[tickers.GOOG]\nvalue_zone = [nan, 340]\n")

    def test_reversed_or_nonpositive_zone_raises(self):
        with self.assertRaises(ValueError):
            self._load("[tickers.GOOG]\nvalue_zone = [340, 315]\n")
        with self.assertRaises(ValueError):
            self._load("[tickers.GOOG]\nvalue_zone = [0, 340]\n")

    def test_nonfinite_settings_raise(self):
        # PR #10 评审: TOML 的 nan 是合法 float, 而 nan 参与的比较恒为
        # False — near_zone_pct = nan 会让 NEAR_ZONE 永远不成立, 报告
        # 照出、零错误, 信号静默消失
        with self.assertRaises(ValueError):
            self._load("[settings]\nnear_zone_pct = nan\n[tickers.QQQ]\n")
        with self.assertRaises(ValueError):
            self._load("[settings]\nvvix_halt = inf\n[tickers.QQQ]\n")
        with self.assertRaises(ValueError):
            self._load("[settings]\ncsp_dte_normal = [nan, 31]\n"
                       "[tickers.QQQ]\n")

    def test_ticker_value_types_gated(self):
        # options = "false" 是合法 TOML 字符串且非空为真 — 想关期权票,
        # 结果照常抓链出票, 与配置意图相反
        with self.assertRaises(ValueError):
            self._load('[tickers.QQQ]\noptions = "false"\n')
        with self.assertRaises(ValueError):
            self._load('[tickers.QQQ]\nhigh_beta = "true"\n')
        with self.assertRaises(ValueError):
            self._load("[tickers.QQQ]\nkind = 123\n")
        with self.assertRaises(ValueError):
            self._load("[tickers.QQQ]\ntwo_x = 2\n")

    def test_unknown_kind_raises(self):
        # kind 只在相等比较里出现: "ETF" 静默退化成个股 LEAP delta 带,
        # 并让 ETF/index 的财报豁免失效
        with self.assertRaises(ValueError):
            self._load('[tickers.QQQ]\nkind = "ETF"\n')
        _, tk = self._load('[tickers.QQQ]\nkind = "index"\n')
        self.assertEqual(tk["QQQ"]["kind"], "index")

    def test_unknown_toplevel_table_raises(self):
        # [setting] 手滑: 整段阈值连同你以为改过的每个键被静默忽略
        with self.assertRaises(ValueError):
            self._load("[setting]\nnear_zone_pct = 4.0\n[tickers.QQQ]\n")
        with self.assertRaises(ValueError):
            self._load('[ticker.QQQ]\nkind = "index"\n')

    def test_case_collision_raises(self):
        with self.assertRaises(ValueError):
            self._load("[tickers.spcx]\noptions = false\n"
                       "[tickers.SPCX]\nkind = \"stock\"\n")

    def test_empty_tickers_raises(self):
        with self.assertRaises(ValueError):
            self._load("[settings]\nnear_zone_pct = 5.0\n")

    def test_zone_asof_accepted_and_normalized(self):
        _, t = self._load('[tickers.NVDA]\nvalue_zone = [185, 205]\n'
                          'zone_asof = "2026-09-05"\n')
        self.assertEqual(t["NVDA"]["zone_asof"], "2026-09-05")
        # TOML 裸日期 (无引号) 解析成 date 对象 — 同样归一成 ISO 字符串
        _, t = self._load("[tickers.NVDA]\nvalue_zone = [185, 205]\n"
                          "zone_asof = 2026-09-05\n")
        self.assertEqual(t["NVDA"]["zone_asof"], "2026-09-05")
        # TOML 裸 datetime (带时间) 是 datetime 对象 — isinstance 分支
        # 顺序 (datetime 先于 date) 的 pin
        _, t = self._load("[tickers.NVDA]\nvalue_zone = [185, 205]\n"
                          "zone_asof = 2026-09-05T10:00:00\n")
        self.assertEqual(t["NVDA"]["zone_asof"], "2026-09-05")

    def test_zone_asof_without_zone_raises(self):
        with self.assertRaisesRegex(ValueError, "只在设了 value_zone"):
            self._load('[tickers.NVDA]\nzone_asof = "2026-09-05"\n')

    def test_zone_asof_bad_format_raises(self):
        with self.assertRaisesRegex(ValueError, "必须是日期"):
            self._load('[tickers.NVDA]\nvalue_zone = [185, 205]\n'
                       'zone_asof = "09/05/2026"\n')

    def test_zone_asof_future_raises(self):
        # 年份 typo (2026→2062) 会同时静默废掉超龄提醒与拆股检测
        with self.assertRaisesRegex(ValueError, "在未来"):
            self._load('[tickers.NVDA]\nvalue_zone = [185, 205]\n'
                       'zone_asof = "2062-09-05"\n')


class TestZoneWatchScaffold(unittest.TestCase):
    """zone 生命周期监控脚手架: sig 身份 / ref 自校准基准 / 提示频控 /
    超龄提醒 / state.json 携带 (zone 评审: AAPL 旧带带着自己写在 notes
    里的过时警告烂了 26 天, 因为 notes 是 write-only 字段)。"""

    S = sc.SETTINGS_DEFAULTS

    def test_no_zone_no_watch(self):
        w, notes = sc.zone_watch_update(None, close=100.0, zone=None,
                                        zone_asof=None,
                                        today_iso="2026-09-07", s=self.S)
        self.assertIsNone(w)
        self.assertEqual(notes, [])

    def test_init_captures_sig_and_ref(self):
        w, _ = sc.zone_watch_update(None, close=230.36, zone=[185.0, 205.0],
                                    zone_asof="2026-09-05",
                                    today_iso="2026-09-07", s=self.S)
        self.assertEqual(w["sig"], "185-205@2026-09-05")
        self.assertAlmostEqual(w["ref_pct"], (230.36 / 205 - 1) * 100,
                               places=6)

    def test_ref_floored_at_zero_inside_zone(self):
        w, _ = sc.zone_watch_update(None, close=335.31, zone=[315.0, 340.0],
                                    zone_asof="2026-09-05",
                                    today_iso="2026-09-07", s=self.S)
        self.assertEqual(w["ref_pct"], 0.0)

    def test_sig_change_resets_flags(self):
        prev = {"sig": "185-205@2026-09-05", "ref_pct": 12.4,
                "flags": {"age": "2026-11-10"}}
        w, _ = sc.zone_watch_update(prev, close=230.0, zone=[185.0, 210.0],
                                    zone_asof="2026-11-15",
                                    today_iso="2026-11-16", s=self.S)
        self.assertEqual(w["sig"], "185-210@2026-11-15")
        self.assertEqual(w["flags"], {})

    def test_age_reminder_fires_and_repeats_weekly(self):
        zone = [185.0, 205.0]
        w, notes = sc.zone_watch_update(None, close=230.0, zone=zone,
                                        zone_asof="2026-09-05",
                                        today_iso="2026-11-05", s=self.S)
        self.assertTrue(any("复核" in n for n in notes))     # 61 天 > 60
        w, notes2 = sc.zone_watch_update(w, close=230.0, zone=zone,
                                         zone_asof="2026-09-05",
                                         today_iso="2026-11-06", s=self.S)
        self.assertEqual(notes2, [])                          # 频控: 次日静默
        _, notes3 = sc.zone_watch_update(w, close=230.0, zone=zone,
                                         zone_asof="2026-09-05",
                                         today_iso="2026-11-12", s=self.S)
        self.assertTrue(any("复核" in n for n in notes3))     # 7 天后重复

    def test_fresh_zone_is_silent(self):
        _, notes = sc.zone_watch_update(None, close=230.0,
                                        zone=[185.0, 205.0],
                                        zone_asof="2026-09-05",
                                        today_iso="2026-09-07", s=self.S)
        self.assertEqual(notes, [])

    def test_no_asof_hint_once_per_sig(self):
        zone = [45.0, 57.5]
        w, notes = sc.zone_watch_update(None, close=64.0, zone=zone,
                                        zone_asof=None,
                                        today_iso="2026-09-07", s=self.S)
        self.assertTrue(any("zone_asof" in n for n in notes))
        _, notes2 = sc.zone_watch_update(w, close=64.0, zone=zone,
                                         zone_asof=None,
                                         today_iso="2026-10-07", s=self.S)
        self.assertEqual(notes2, [])          # 只提示一次, 一个月后也不再提

    def test_persisted_state_carries_watch(self):
        watch = {"sig": "45-57.5@2026-09-05", "ref_pct": 11.8, "flags": {}}
        entry = sc.next_persisted_state(
            {}, {"state": "PULLBACK", "zone_watch": watch}, "2026-09-07")
        self.assertEqual(entry["zone_watch"], watch)
        entry2 = sc.next_persisted_state(
            {"zone_watch": watch},
            {"state": "PULLBACK", "zone_watch": None}, "2026-09-08")
        self.assertNotIn("zone_watch", entry2)   # zone 删掉 → watch 消失

    def test_analyze_ticker_wires_watch(self):
        cfg = {**sc.TICKER_DEFAULTS, "value_zone": [45.0, 57.5],
               "zone_asof": "2026-09-05"}
        r = sc.analyze_ticker("XX", cfg, _ohlcv_downtrend(50.0), {},
                              {"stage": "NORMAL"}, sc.SETTINGS_DEFAULTS,
                              "close", fetch_options=False)
        self.assertEqual(r["zone_watch"]["sig"], "45-57.5@2026-09-05")

    def test_legacy_watch_missing_ref_self_heals(self):
        # 手编/损坏的 state.json 丢了 ref_pct — 按当日距离重锚, 不 KeyError
        w, _ = sc.zone_watch_update({"sig": "45-57.5@2026-09-05"},
                                    close=64.0, zone=[45.0, 57.5],
                                    zone_asof="2026-09-05",
                                    today_iso="2026-09-08", s=self.S)
        self.assertAlmostEqual(w["ref_pct"], (64.0 / 57.5 - 1) * 100,
                               places=6)

    def test_open_pass_computes_watch_but_suppresses_notes(self):
        # zone_asof 用永久超龄的 2020 日期绕开 datetime.now 注入问题:
        # open pass 要携带 watch (持久化老坑) 但不出 notes (open 报告
        # 不渲染 notes, 且计数/提示以收盘为准); close pass 出提示
        cfg = {**sc.TICKER_DEFAULTS, "value_zone": [45.0, 57.5],
               "zone_asof": "2020-01-01"}
        r_open = sc.analyze_ticker("XX", cfg, _ohlcv_downtrend(50.0), {},
                                   {"stage": "NORMAL"}, sc.SETTINGS_DEFAULTS,
                                   "open", fetch_options=False)
        self.assertIsNotNone(r_open["zone_watch"])
        self.assertFalse(any("复核" in n for n in r_open["notes"]))
        r_close = sc.analyze_ticker("XX", cfg, _ohlcv_downtrend(50.0), {},
                                    {"stage": "NORMAL"}, sc.SETTINGS_DEFAULTS,
                                    "close", fetch_options=False)
        self.assertTrue(any("复核" in n for n in r_close["notes"]))


class TestActionBlockFloorTag(unittest.TestCase):
    """zone 评审: 破下沿铸出的 CSP 票在第一屏的 🔵 行必须自带论点检查前置
    — 原实现的 SELL 行不携带任何 note, 警告只可能藏在详情区。"""

    CSP = {"exp": "2026-10-02", "strike": 50.0, "mid": 0.60, "delta": 0.12,
           "annualized_pct": 15.0}

    def _r(self, **kw):
        base = {"symbol": "RKLB", "error": None, "tech": {"close": 40.0},
                "notes": [], "state": "LEFT_ZONE", "leap": None,
                "csp": dict(self.CSP), "iv30": None,
                "cfg": {"value_zone": [45.0, 57.5], "options": True}}
        base.update(kw)
        return base

    def test_below_floor_prefixes_sell_line(self):
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        text = "\n".join(sc.action_block([self._r(below_floor=True)], ivdf))
        self.assertIn("破下沿", text)
        self.assertIn("论点检查", text)
        self.assertIn("SELL 2026-10-02 50P", text)

    def test_in_zone_sell_line_unprefixed(self):
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        text = "\n".join(sc.action_block(
            [self._r(below_floor=False, tech={"close": 50.0})], ivdf))
        self.assertNotIn("破下沿", text)
        self.assertIn("SELL 2026-10-02 50P", text)

    def test_leap_line_carries_floor_tag(self):
        # CONFIRMED+破下沿可达 (深跌后不再新低+突破前20日高即三选二) —
        # 开新多头的论点检查分量不低于卖 put, 🟢 行同样带前置
        leap = {"exp": "2028-01-21", "strike": 30.0, "mid": 12.0,
                "delta": 0.80}
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        text = "\n".join(sc.action_block(
            [self._r(below_floor=True, csp=None, leap=leap)], ivdf))
        self.assertIn("🟢", text)
        self.assertIn("破下沿", text)

    def test_ladder_only_below_floor_gets_standalone_warning(self):
        # DRAM/SPCX 场景 (options=false 只有分批档): 破下沿恰是剧本要求
        # 论点检查的日子, 不能被第一屏归进"其余今日无动作"
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        r = self._r(below_floor=True, csp=None,
                    ladder=[57.5, 45.0, 36.9],
                    cfg={"value_zone": [45.0, 57.5], "options": False})
        text = "\n".join(sc.action_block([r], ivdf))
        self.assertIn("已破价值区下沿", text)
        self.assertNotIn("其余今日无动作", text)

    def test_csp_skipped_below_floor_gets_standalone_warning(self):
        # CSP 被 skip (权利金太薄) 时 ⏸ 行原文是"改正股限价单" — 破下沿
        # 当天这等于催继续摊, 独立 ⚠️ 行必须在场
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        r = self._r(below_floor=True,
                    csp={"skip_reason": "接货档权利金太薄: 年化仅 4.1%"})
        text = "\n".join(sc.action_block([r], ivdf))
        self.assertIn("已破价值区下沿", text)


class TestFinishCSPFloorNote(unittest.TestCase):
    """_finish_csp: 现价破 zone 下沿时票面第一条 note = 论点检查前置。"""

    C = {"exp": "2026-10-02", "dte": 21, "strike": 34.0, "mid": 0.50,
         "src": "live", "iv": 0.8, "delta": 0.12, "oi": 500,
         "spread_pct": 4.0}

    def test_below_floor_prepends_thesis_check(self):
        t = sc._finish_csp(dict(self.C), spot=40.0, s=sc.SETTINGS_DEFAULTS,
                           zone=[45.0, 57.5], panic=False, extra_notes=[])
        self.assertTrue(t["notes"])
        self.assertIn("论点", t["notes"][0])
        self.assertIn("下沿", t["notes"][0])

    def test_in_zone_no_floor_note(self):
        t = sc._finish_csp(dict(self.C), spot=50.0, s=sc.SETTINGS_DEFAULTS,
                           zone=[45.0, 57.5], panic=False, extra_notes=[])
        self.assertFalse(any("下沿" in n for n in t["notes"]))

    def test_no_zone_no_floor_note(self):
        t = sc._finish_csp(dict(self.C), spot=40.0, s=sc.SETTINGS_DEFAULTS,
                           zone=None, panic=False, extra_notes=[])
        self.assertFalse(any("下沿" in n for n in t["notes"]))


class TestRenderOpenZoneAlert(unittest.TestCase):
    """开盘 pass 的价值区警报: 破下沿 ≠ 在价值区内 — 原实现对两种情形
    同一句"核对 CSP 挂单/接货档位", 对破下沿的标的是在催继续摊。"""

    REGIME = {"vix": 15.0, "vix3m": 17.0, "ratio": 0.882, "vxn": None,
              "as_of": "2026-09-04", "source": "CBOE", "stage": "NORMAL",
              "vx": {}, "vvix": {}, "move": {}, "stale_days": 0,
              "last_episode": None, "crossed_up": False,
              "crossed_down": False}

    def _r(self, sym, close, zone):
        return {"symbol": sym, "error": None, "earnings": "",
                "prev_state": "UPTREND", "notes": [],
                "tech": {"close": close, "sma20": close, "gap_pct": 0.0,
                         "change_pct": 0.0},
                "cfg": {"value_zone": zone, "options": True}}

    def _render(self, r):
        now = datetime(2026, 9, 4, 9, 45, tzinfo=sc.ET)
        return sc.render_open([r], dict(self.REGIME), now,
                              sc.SETTINGS_DEFAULTS)

    def test_below_floor_says_thesis_check_not_buy(self):
        text = self._render(self._r("RKLB", 40.0, [45.0, 57.5]))
        self.assertIn("已跌破价值区下沿", text)
        self.assertIn("论点", text)
        self.assertNotIn("核对 CSP 挂单", text)

    def test_in_zone_still_prompts_orders(self):
        text = self._render(self._r("GOOG", 335.31, [315.0, 340.0]))
        self.assertIn("在价值区内", text)
        self.assertIn("核对 CSP 挂单", text)


def _split(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def _ohlcv_downtrend(last_close: float, n: int = 70) -> pd.DataFrame:
    """单调下行、收在 last_close 的合成 OHLCV — 喂 technical_snapshot 够用。"""
    idx = pd.bdate_range("2026-05-01", periods=n)
    c = pd.Series([last_close + (n - 1 - i) * 0.5 for i in range(n)],
                  index=idx, dtype=float)
    return pd.DataFrame({"Open": c, "High": c * 1.01, "Low": c * 0.99,
                         "Close": c, "Volume": 1_000_000.0}, index=idx)


class TestAnalyzeTickerBelowFloor(unittest.TestCase):
    """below_floor 旗标的生产端接线 (fetch_options=False 纯离线路径)。
    消费端 (action_block) 只用 .get() — 生产端键名/逻辑被挪走时 get 会
    静默吞掉, 第一屏 ⚠️ 整体消失而全套件保绿, 必须钉住生产端本身。"""

    def _run(self, close, zone):
        cfg = {**sc.TICKER_DEFAULTS, "value_zone": zone}
        return sc.analyze_ticker("XX", cfg, _ohlcv_downtrend(close), {},
                                 {"stage": "NORMAL"}, sc.SETTINGS_DEFAULTS,
                                 "close", fetch_options=False)

    def test_flag_and_note_below_floor(self):
        r = self._run(40.0, [45.0, 57.5])
        self.assertTrue(r["below_floor"])
        self.assertTrue(any("下沿" in n for n in r["notes"]))

    def test_flag_false_in_zone(self):
        r = self._run(50.0, [45.0, 57.5])
        self.assertFalse(r["below_floor"])
        self.assertFalse(any("下沿" in n for n in r["notes"]))

    def test_flag_false_without_zone(self):
        r = self._run(40.0, None)
        self.assertFalse(r["below_floor"])


class TestZonePosition(unittest.TestCase):
    """概览表"现价 vs 接货带"那半句的措辞。"""

    def test_hairline_gets_one_decimal(self):
        # ISRG 实况: 收 366.70, 上沿 365 — 差 0.46%, 取整成 "上方+0%" 与
        # 同一行的"接近价值区"自相矛盾
        self.assertEqual(sc.zone_position(366.70, [320.0, 365.0]),
                         "上方+0.5%")
        self.assertEqual(sc.zone_position(363.5, [365.0, 400.0]),
                         "破下沿-0.4%")

    def test_normal_distance_stays_integer(self):
        self.assertEqual(sc.zone_position(499.70, [380.0, 440.0]),
                         "上方+14%")
        self.assertEqual(sc.zone_position(40.0, [45.0, 57.5]),
                         "破下沿-11%")

    def test_inside_zone(self):
        self.assertEqual(sc.zone_position(335.31, [315.0, 340.0]), "区内")
        self.assertEqual(sc.zone_position(340.0, [315.0, 340.0]), "区内")
        self.assertEqual(sc.zone_position(315.0, [315.0, 340.0]), "区内")


class TestRenderCloseZoneLines(unittest.TestCase):
    """render_close 的两处 zone 分支烟测: CSP 三档措辞 (strike<下沿不再说
    "在价值区内") 与 ladder 档位相对现价的计数 (挂 GTC 会立即成交的实钱
    footgun) — render_close 此前在本套件里从未被调用过。"""

    def _result(self, close, zone, strike, ladder=None):
        cfg = {**sc.TICKER_DEFAULTS, "value_zone": list(zone)}
        r = sc.analyze_ticker("XX", cfg, _ohlcv_downtrend(close), {},
                              {"stage": "NORMAL"}, sc.SETTINGS_DEFAULTS,
                              "close", fetch_options=False)
        base = {"exp": "2026-10-02", "dte": 21, "strike": strike, "mid": 0.60,
                "src": "live", "iv": 0.6, "delta": 0.12, "oi": 500,
                "spread_pct": 4.0}
        r["csp"] = sc._finish_csp(base, close, sc.SETTINGS_DEFAULTS,
                                  list(zone), False, [])
        if ladder is not None:
            r["ladder"] = ladder
        return r

    def _render(self, r):
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        now = datetime(2026, 9, 4, 15, 45, tzinfo=sc.ET)
        return sc.render_close([r], dict(TestRenderOpenZoneAlert.REGIME),
                               ivdf, now)

    def test_csp_wording_below_floor_strike(self):
        # GOOG 在区内, delta 带选到下沿之下的 310P — 不再说"在价值区内"
        text = self._render(self._result(335.31, (315.0, 340.0), 310.0))
        self.assertIn("已低于价值区下沿", text)
        self.assertNotIn("行权价在价值区内", text)

    def test_csp_wording_in_zone_strike(self):
        text = self._render(self._result(335.31, (315.0, 340.0), 320.0))
        self.assertIn("行权价在价值区内", text)

    def test_ladder_counts_rungs_above_close(self):
        # 破下沿: ①② 档全在现价上方 (③ 恐慌档还在下方) → 前 2 档
        text = self._render(self._result(40.0, (45.0, 57.5), 34.0,
                                         ladder=[57.5, 45.0, 36.9]))
        self.assertIn("前 2 档已在现价上方", text)

    def test_ladder_first_rung_above_close_in_zone(self):
        # 区内: 只有 ① 档 (带上沿) 高于现价 → 前 1 档
        text = self._render(self._result(335.31, (315.0, 340.0), 310.0,
                                         ladder=[340.0, 315.0, 258.3]))
        self.assertIn("前 1 档已在现价上方", text)

    def test_overview_puts_zone_next_to_symbol(self):
        # 手机上"这票的接货带在哪"要和标的挨着 — 原来隔了 7 列 (状态/操作/
        # 收盘/Δ%/vs20日/量比/三选二), 横向扫过去才对得上
        text = self._render(self._result(335.31, (315.0, 340.0), 310.0))
        self.assertIn("| 标的 | 价值区 | 收盘 | 状态 |", text)
        row = next(l for l in text.splitlines() if l.startswith("| XX |"))
        self.assertEqual(_split(row)[1], "315-340 (区内)")
        self.assertEqual(_split(row)[2], "335.31")
        self.assertEqual(_split(row)[3], "价值区内(左侧)")
        self.assertEqual(len(_split(row)), 11)     # 列数不变, 只换位

    def test_ladder_all_rungs_below_close_no_note(self):
        text = self._render(self._result(400.0, (315.0, 340.0), 310.0,
                                         ladder=[340.0, 315.0, 258.3]))
        self.assertNotIn("已在现价上方", text)


class _FakeChain:
    def __init__(self, calls, puts):
        self.calls, self.puts = calls, puts


class _FakeCC:
    def __init__(self, chain, expiries=None):
        self._chain = chain
        self._expiries = expiries or [("2026-10-16", 35)]

    def expiries(self):
        return self._expiries

    def chain(self, exp):
        return self._chain


class TestRR25Snapshot(unittest.TestCase):
    """两腿按**同一个 sigma(K)** 定价 -> put-call parity 逐档精确成立,
    所以 forward_from_parity 必须还原出 F = S*e^((r-q)T)。"""

    T = 35 / 365.0
    NORMAL_SKEW = staticmethod(lambda m: 0.35 - 0.20 * (m - 1.0))
    CALL_SKEW = staticmethod(lambda m: 0.35 + 0.20 * (m - 1.0))

    def _chain(self, S, sigma_fn, live=True, oi=100, rel_width=0.04,
               crossed=False, q=0.0):
        s_eff = S * math.exp(-q * self.T)
        traded = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)
        rows = {True: [], False: []}
        for i in range(31):
            k = round(S * (0.70 + 0.02 * i), 2)
            sig = sigma_fn(k / S)
            for is_call in (True, False):
                px = sc.bs_price(s_eff, k, self.T, sc.RATE, sig, is_call)
                half = px * rel_width / 2
                bid, ask = px - half, px + half
                if crossed:
                    bid, ask = ask, bid
                rows[is_call].append(
                    {"strike": k, "lastPrice": px, "lastTradeDate": traded,
                     "openInterest": oi,
                     "bid": bid if live else 0.0,
                     "ask": ask if live else 0.0})
        return _FakeChain(pd.DataFrame(rows[True]), pd.DataFrame(rows[False]))

    def _snap(self, spot, **kw):
        S = kw.pop("S", spot)
        sigma_fn = kw.pop("sigma_fn", self.NORMAL_SKEW)
        ch = self._chain(S, sigma_fn, **kw)
        return sc.rr25_snapshot(_FakeCC(ch), spot, sc.SETTINGS_DEFAULTS)

    def test_forward_recovers_stale_spot(self):
        # 2026-09-04 事故的回归 (见 lesson.md): yfinance 日线最新一根 NaN,
        # scanner 退回前一日收盘当 spot, 用昨天的股价反解今天的期权报价 —
        # spot 偏低使 call 抬高/put 压低, 11 个标的亮了 10 个假倒挂旗标。
        # 现在 IV/delta 都从期权自己的 forward 出发, 陈旧 spot 不再传导。
        out = self._snap(106.99, S=125.0)          # 链是 125 的, 喂进 106.99
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["fwd_spot"], 125.0, delta=0.5)
        self.assertGreater(out["spot_gap_pct"], 15.0)
        self.assertFalse(out["inverted"])          # 正常 skew 不该被判倒挂
        self.assertGreater(out["rr"], 0)

    def test_genuine_inversion_still_flagged(self):
        # 过滤收紧后不能变成"永不报告" — 真倒挂仍要亮
        out = self._snap(100.0, sigma_fn=self.CALL_SKEW)
        self.assertIsNotNone(out)
        self.assertTrue(out["inverted"])
        self.assertLess(out["rr"], -sc.SETTINGS_DEFAULTS["rr_invert_min_pts"])

    def test_dividend_carry_no_longer_biases_iv(self):
        # 零股息 BS 会压低 call IV/抬高 put IV; forward 口径下两腿都该
        # 还原回输入的 sigma(K)
        out = self._snap(100.0, q=0.03)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["fwd_spot"], 100.0 * math.exp(-0.03 * self.T),
                               delta=0.05)
        for leg in ("call", "put"):
            want = self.NORMAL_SKEW(out[f"{leg}_strike"] / 100.0)
            self.assertAlmostEqual(out[f"{leg}_iv"], want, delta=0.006)

    def test_stale_leg_kills_the_reading(self):
        # 二/三轮评审: _mark 会退回到最多 5 天前的 lastPrice — 一腿陈旧
        # 成交价 vs 另一腿实时 mid 能造出假倒挂。没有 live 报价 = 无读数
        ch = self._chain(100.0, self.NORMAL_SKEW, live=False)
        self.assertEqual(sc._mark(ch.puts.iloc[0], sc._stale_cutoff())[1],
                         "last")            # 仍是"可用价", 但 RR 不收
        self.assertIsNone(
            sc.rr25_snapshot(_FakeCC(ch), 100.0, sc.SETTINGS_DEFAULTS))

    def test_crossed_quotes_rejected(self):
        # bid > ask 的 mid 无意义 — bid>0 and ask>0 挡不住 (三轮评审)
        self.assertIsNone(self._snap(100.0, crossed=True))

    def _bump_call(self, ch, strike, dpx):
        m = ch.calls["strike"] == strike
        ch.calls.loc[m, ["bid", "ask", "lastPrice"]] += dpx

    def test_forward_median_survives_one_bad_parity_pair(self):
        # 单档 |C−P| 反解对该档噪声全暴露 (F 误差 ~5 pts/1% 地搬进 RR),
        # 且坏档的 |C−P| 常恰好因此变小、被"取最小"优先选中 — 把离 F 最近
        # 的档 (100, F≈100.38) 的 call mid 压 0.5: 老实现 F 偏 ~-0.5%
        # (≈-2.5 pts RR, 足以把正常 skew 翻成假倒挂), 中位数聚合下坏档
        # 被灭, forward 还原精确 (五轮评审)
        ch = self._chain(100.0, self.NORMAL_SKEW)
        self._bump_call(ch, 100.0, -0.5)
        out = sc.rr25_snapshot(_FakeCC(ch), 100.0, sc.SETTINGS_DEFAULTS)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out["fwd_spot"], 100.0, delta=0.15)
        self.assertFalse(out["inverted"])
        self.assertGreater(out["rr"], 0)

    @staticmethod
    def _leg(rr, inverted):
        return {"rr": rr, "inverted": inverted, "call_iv": 0.30,
                "put_iv": 0.30 + rr / 100, "call_strike": 108.0,
                "put_strike": 92.0, "call_delta": 0.25, "put_delta": -0.25}

    def test_split_verdict_across_forwards_is_no_reading(self):
        # 2026-09-05 实测 GOOG 的形态: 五个候选 forward 各自反解出
        # -1.96/-1.14/-0.95/-0.73/-0.29 — 两个越过 1.0 地板, 三个没越过。
        # "算不算倒挂"取决于挑了哪个 forward, 那不是市场事实。当天报告里
        # 它以 -1.1 亮了旗标 (五轮评审后的六轮观测)
        from unittest.mock import patch
        ch = self._chain(100.0, self.NORMAL_SKEW)
        seq = [self._leg(v, v < -1.0)
               for v in (-1.96, -1.14, -0.95, -0.73, -0.29)]
        with patch.object(sc, "_rr_at_forward",
                          side_effect=lambda *a, **k: seq.pop(0)):
            out = sc.rr25_snapshot(_FakeCC(ch), 100.0, sc.SETTINGS_DEFAULTS)
        self.assertIsNone(out)

    def test_unanimous_verdict_survives_and_reports_dispersion(self):
        # 同日 GLD 的形态: 五个候选全在 -1.85 附近 — 判定一致, 是真读数。
        # 散布随读数带出来, 让人能自己判置信度
        from unittest.mock import patch
        ch = self._chain(100.0, self.NORMAL_SKEW)
        vals = (-1.88, -1.85, -1.85, -1.84, -1.83)
        seq = [self._leg(v, True) for v in vals]
        with patch.object(sc, "_rr_at_forward",
                          side_effect=lambda *a, **k: seq.pop(0)):
            out = sc.rr25_snapshot(_FakeCC(ch), 100.0, sc.SETTINGS_DEFAULTS)
        self.assertIsNotNone(out)
        self.assertTrue(out["inverted"])
        self.assertAlmostEqual(out["rr"], -1.85, places=2)   # 取中位那份
        self.assertAlmostEqual(out["rr_dispersion"], 0.05, places=2)

    def test_unanimous_not_inverted_also_survives(self):
        # 一致判"不倒挂"同样是有效读数 — 门管的是判定分裂, 不是方向
        from unittest.mock import patch
        ch = self._chain(100.0, self.NORMAL_SKEW)
        seq = [self._leg(v, False) for v in (0.14, 0.23, 0.48, 0.55, 0.56)]
        with patch.object(sc, "_rr_at_forward",
                          side_effect=lambda *a, **k: seq.pop(0)):
            out = sc.rr25_snapshot(_FakeCC(ch), 100.0, sc.SETTINGS_DEFAULTS)
        self.assertIsNotNone(out)
        self.assertFalse(out["inverted"])

    def test_forward_chaotic_parity_is_no_reading(self):
        # 多数档都在漂 = 报价面自相矛盾 — 中位数救不了, 极差门拒掉整个
        # 读数 (宁缺毋错; 借券费是全曲线一致平移, 极差不受影响不会误伤)
        ch = self._chain(100.0, self.NORMAL_SKEW)
        self._bump_call(ch, 98.0, +1.2)
        self._bump_call(ch, 100.0, -1.2)
        self._bump_call(ch, 102.0, +0.8)
        self.assertIsNone(
            sc.rr25_snapshot(_FakeCC(ch), 100.0, sc.SETTINGS_DEFAULTS))

    def test_forward_single_pair_is_no_reading(self):
        # 只剩一档可用 = 无从交叉验证 — 之前单档照出读数, 正是噪声全
        # 暴露的形态
        traded = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=1)
        row = lambda px: {"strike": 100.0, "bid": px - 0.05, "ask": px + 0.05,
                          "lastPrice": px, "lastTradeDate": traded,
                          "openInterest": 100}
        fwd = sc.forward_from_parity(
            pd.DataFrame([row(4.5)]), pd.DataFrame([row(4.1)]),
            self.T, sc._stale_cutoff(), sc.SETTINGS_DEFAULTS)
        self.assertIsNone(fwd)

    def test_wide_quotes_rejected(self):
        # 任意宽的报价照样满足 bid>0 and ask>0, 其 mid 正是假倒挂来源
        self.assertIsNone(self._snap(100.0, rel_width=0.60))
        self.assertIsNotNone(self._snap(100.0, rel_width=0.20))

    def test_illiquid_strikes_rejected(self):
        self.assertIsNone(self._snap(100.0, oi=1))


class TestVVIXStaleness(unittest.TestCase):
    def _frozen_series(self, bdays_ago, val=118.0):
        end = pd.Timestamp(np.busday_offset(
            np.datetime64(datetime.now(sc.ET).date()), -bdays_ago,
            roll="backward"))
        idx = pd.bdate_range(end=end, periods=90)
        return pd.Series([val] * 90, index=idx)

    def test_frozen_feed_is_error_not_reading(self):
        # 冻结 15 个交易日 + 盘中点也挂 → error, 门自动失效 (评审 finding #2:
        # 这是唯一在 NORMAL 期硬拦 CSP 的门, 不能拿旧数当读数)
        from unittest.mock import patch
        with patch.object(sc, "_cboe_series",
                          return_value=self._frozen_series(15)), \
             patch.object(sc, "_cboe_delayed",
                          side_effect=RuntimeError("down")):
            out = sc.fetch_vvix()
        self.assertIn("error", out)
        self.assertIn("stale", out["error"])

    def test_fresh_close_passes(self):
        from unittest.mock import patch
        with patch.object(sc, "_cboe_series",
                          return_value=self._frozen_series(1, val=91.25)), \
             patch.object(sc, "_cboe_delayed",
                          side_effect=RuntimeError("down")):
            out = sc.fetch_vvix()
        self.assertAlmostEqual(out["value"], 91.25)

    def test_delayed_quote_rescues_failed_history(self):
        # 三轮评审: 历史 CSV **请求失败**但盘中点健康时, 原实现直接落到外层
        # except 报 error — docstring 写的是"且", 实现做成了"或"。而且失效
        # 方向是 fail-open: NORMAL 期唯一硬拦 CSP 的门被静默关掉
        from unittest.mock import patch
        today = datetime.now(sc.ET)
        with patch.object(sc, "_cboe_series",
                          side_effect=RuntimeError("csv 500")), \
             patch.object(sc, "_cboe_delayed", return_value=(112.0, today)):
            out = sc.fetch_vvix()
        self.assertNotIn("error", out)
        self.assertAlmostEqual(out["value"], 112.0)
        self.assertIn("盘中", out["as_of"])

    def test_both_paths_down_is_error(self):
        from unittest.mock import patch
        with patch.object(sc, "_cboe_series",
                          side_effect=RuntimeError("csv 500")), \
             patch.object(sc, "_cboe_delayed",
                          side_effect=RuntimeError("quote down")):
            out = sc.fetch_vvix()
        self.assertIn("error", out)
        self.assertIn("history", out["error"])
        self.assertIn("delayed", out["error"])

    def test_intraday_graft_rescues_frozen_history(self):
        # 历史 CSV 冻结但今天的盘中点拿得到 → 用盘中点, 不报 error
        from unittest.mock import patch
        today = datetime.now(sc.ET)
        with patch.object(sc, "_cboe_series",
                          return_value=self._frozen_series(15)), \
             patch.object(sc, "_cboe_delayed",
                          return_value=(95.5, today)):
            out = sc.fetch_vvix()
        self.assertAlmostEqual(out["value"], 95.5)
        self.assertIn("盘中", out["as_of"])


class TestRR25(unittest.TestCase):
    @staticmethod
    def _row(strike, iv, delta):
        return {"strike": strike, "iv": iv, "delta": delta}

    def test_closest_delta_row(self):
        rows = [self._row(110, 0.30, 0.35), self._row(115, 0.28, 0.24),
                self._row(120, 0.27, 0.15)]
        self.assertEqual(sc.closest_delta_row(rows, 0.25)["strike"], 115)
        # put 侧 delta 为负, 取绝对值
        puts = [self._row(90, 0.33, -0.26), self._row(85, 0.36, -0.15)]
        self.assertEqual(sc.closest_delta_row(puts, 0.25)["strike"], 90)
        # 链稀疏: tol 外 = 无读数, 不硬凑
        sparse = [self._row(150, 0.5, 0.05)]
        self.assertIsNone(sc.closest_delta_row(sparse, 0.25))
        self.assertIsNone(sc.closest_delta_row([], 0.25))

    def test_rr_normal_skew_positive(self):
        calls = [self._row(115, 0.28, 0.25)]
        puts = [self._row(90, 0.34, -0.25)]
        out = sc.rr25(calls, puts)
        self.assertAlmostEqual(out["rr"], 6.0, places=6)   # put 贵 = 正常
        self.assertFalse(out["inverted"])

    def test_rr_inverted_call_skew(self):
        # meme 形态: call 比 put 贵
        calls = [self._row(115, 0.42, 0.26)]
        puts = [self._row(90, 0.35, -0.24)]
        out = sc.rr25(calls, puts)
        self.assertAlmostEqual(out["rr"], -7.0, places=6)
        self.assertTrue(out["inverted"])
        self.assertEqual((out["call_strike"], out["put_strike"]), (115, 90))

    def test_rr_noise_floor(self):
        # 二轮评审 finding (high): 延迟报价 RR 有 ~1 pt run-to-run 漂移,
        # 零阈值在真实 skew≈0 时反复亮假旗标 — 地板内不亮, 地板外才亮
        calls = [self._row(115, 0.304, 0.25)]
        puts = [self._row(90, 0.300, -0.25)]
        out = sc.rr25(calls, puts, invert_floor=1.0)
        self.assertAlmostEqual(out["rr"], -0.4, places=6)
        self.assertFalse(out["inverted"])       # -0.4 pts = 噪声, 不亮
        out = sc.rr25([self._row(115, 0.315, 0.25)], puts, invert_floor=1.0)
        self.assertTrue(out["inverted"])        # -1.5 pts = 真倒挂

    def test_rr_missing_side_is_none(self):
        calls = [self._row(115, 0.28, 0.25)]
        self.assertIsNone(sc.rr25(calls, []))
        self.assertIsNone(sc.rr25([], [self._row(90, 0.3, -0.25)]))
        # iv 缺失同样无读数
        self.assertIsNone(
            sc.rr25([self._row(115, None, 0.25)], [self._row(90, 0.3, -0.25)]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
