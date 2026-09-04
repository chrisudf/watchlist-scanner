#!/usr/bin/env python3
"""Unit tests for the pure signal/state/ticket math in scanner.py.

Run:  .venv/bin/python test_signals.py
No network access needed — everything here is synthetic data.
"""

import math
import unittest
from datetime import datetime, timedelta

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
    def test_halt_shows_ticket_but_keeps_key(self):
        # halt 期间: want_leap=True (⏸ skip 行可见) 但不烧 dedup key —
        # VX 结算滞后一天, 解除窗第一天常读到恐慌尾巴的旧曲线,
        # 烧了 key 整个 episode 的 LEAP 补发窗静默丢失 (评审 finding #1)
        want, burn = sc.stage2_leap_gate(True, None, "2026-08-28", halted=True)
        self.assertTrue(want)
        self.assertFalse(burn)
        # halt 解除后同窗口内: 补发 + 烧 key
        want, burn = sc.stage2_leap_gate(True, None, "2026-08-28", halted=False)
        self.assertTrue(want)
        self.assertTrue(burn)

    def test_key_already_burned_dedups(self):
        want, burn = sc.stage2_leap_gate(True, "2026-08-28", "2026-08-28",
                                         halted=False)
        self.assertFalse(want)
        self.assertFalse(burn)

    def test_price_not_ok(self):
        want, burn = sc.stage2_leap_gate(False, None, "2026-08-28",
                                         halted=False)
        self.assertFalse(want)
        self.assertFalse(burn)


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

    def test_non_regime_skip_still_itemized(self):
        # 普通 skip (年化不足等) 照旧逐票 ⏸, 且 LEAP 行带工具前缀
        ivdf = pd.DataFrame(columns=["date", "symbol", "iv30", "rv30"])
        rs = [self._r("AAA", csp={"skip_reason": "年化仅 6.0%"},
                      leap={"skip_reason": "财报 2026-09-09 就在 6 天后"})]
        text = "\n".join(sc.action_block(rs, ivdf))
        self.assertIn("⏸ **AAA** CSP: 年化仅", text)
        self.assertIn("⏸ **AAA** LEAP: 财报", text)
        self.assertNotIn("全市场硬停牌", text)


class _FakeChain:
    def __init__(self, calls, puts):
        self.calls, self.puts = calls, puts


class _FakeCC:
    def __init__(self, chain):
        self._chain = chain

    def expiries(self):
        return [("2026-10-16", 35)]

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
