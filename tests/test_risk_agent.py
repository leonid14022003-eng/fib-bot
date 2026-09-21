"""
Тесты для НОВОГО кода 21 сентября 2026 (риск-блок и базовые частоты, итог
исследования на ~650 инструментах):
- agents/risk_agent.py: build_risk_plan(), format_risk_line(), atr14()
- agents/edge_stats.py: edge_key(), format_edge_line()
- agents/dispatch_agent.py: format_message() -- интеграция строк "📐" и "📊"

Запускается отдельно: python3 tests/test_risk_agent.py
"""
from __future__ import annotations

import re
import sys
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.data_agent import Candle
from agents.dispatch_agent import MIN_HISTORY_BARS_FOR_EDGE, _fmt_price, format_message
from agents.edge_stats import TABLE, edge_key, format_edge_line
from agents.risk_agent import NOISE_STOP_ATR, atr14, build_risk_plan, format_risk_line
from agents.dispatch_agent import AnalysisBundle
from agents.fibo_agent import Direction, FiboLevel, FiboStructure, StructureScope, SwingPoint
from agents.price_behavior_agent import nearest_level
from agents.verification_agent import CheckResult, ChecklistReport
from test_pipeline import _mk_bundle  # синтетический bundle: нисходящая структура 200 -> 100


def _mk_ascending_bundle(fraction: float, scale: float = 1.0) -> AnalysisBundle:
    """Восходящая структура: точка 1 = LOW (100*scale), точка 2 = HIGH (200*scale).
    Более глубокий уровень здесь лежит НИЖЕ по цене -- ровно тот случай, на котором
    раньше ломалась подсказка "следующий уровень"."""
    p1, p2 = 100.0 * scale, 200.0 * scale
    levels = [FiboLevel(level=lv, price=p2 + lv * (p1 - p2))
              for lv in [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.414, 1.618, 2.0, 2.414, 2.618]]
    st = FiboStructure(scope=StructureScope.GLOBAL, direction=Direction.ASCENDING,
                       point1=SwingPoint(dt=date(2026, 1, 10), price=p1, kind="LOW", index=0),
                       point2=SwingPoint(dt=date(2026, 1, 20), price=p2, kind="HIGH", index=10), levels=levels)
    price = p2 + fraction * (p1 - p2)
    return AnalysisBundle(symbol="ASC", source_tag="synthetic", timeframe="1D", period_desc="test",
                          structure=st, nearest=nearest_level(st, price), recent_events=[],
                          checklist=ChecklistReport(results=[CheckResult("dummy", True, "-")]))


class TestBuildRiskPlan(unittest.TestCase):
    def test_descending_structure_geometry(self):
        # точка 1 = 200 (начало), точка 2 = 100 (конец), вход 165: риск 35, цель 65
        plan = build_risk_plan(entry=165.0, point1=200.0, point2=100.0)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan.rr, 65 / 35, places=6)
        self.assertAlmostEqual(plan.stop_pct, 35 / 165 * 100, places=6)
        self.assertAlmostEqual(plan.target_pct, 65 / 165 * 100, places=6)
        # объём позиции: 1% депозита / расстояние до стопа
        self.assertAlmostEqual(plan.position_pct, 1.0 / (35 / 165 * 100) * 100, places=6)

    def test_ascending_structure_is_symmetric(self):
        up = build_risk_plan(entry=135.0, point1=100.0, point2=200.0)
        down = build_risk_plan(entry=165.0, point1=200.0, point2=100.0)
        self.assertAlmostEqual(up.rr, down.rr, places=6)
        self.assertAlmostEqual(up.stop_pct, 35 / 135 * 100, places=6)

    def test_none_when_price_outside_the_two_points(self):
        self.assertIsNone(build_risk_plan(entry=90.0, point1=200.0, point2=100.0))   # уже за точкой 2
        self.assertIsNone(build_risk_plan(entry=210.0, point1=200.0, point2=100.0))  # уже за точкой 1
        self.assertIsNone(build_risk_plan(entry=200.0, point1=200.0, point2=100.0))  # ровно в точке 1
        self.assertIsNone(build_risk_plan(entry=150.0, point1=150.0, point2=150.0))  # вырожденная структура

    def test_custom_risk_per_trade(self):
        plan = build_risk_plan(entry=165.0, point1=200.0, point2=100.0, risk_per_trade_pct=2.0)
        self.assertAlmostEqual(plan.position_pct, 2.0 / (35 / 165 * 100) * 100, places=6)

    def test_stop_inside_noise_flag_needs_atr(self):
        no_atr = build_risk_plan(entry=165.0, point1=200.0, point2=100.0)
        self.assertIsNone(no_atr.stop_atr)
        self.assertFalse(no_atr.stop_inside_noise)
        tight = build_risk_plan(entry=165.0, point1=200.0, point2=100.0, atr=40.0)  # стоп 35 < 1 ATR
        self.assertTrue(tight.stop_inside_noise)
        self.assertAlmostEqual(tight.stop_atr, 35 / 40)
        wide = build_risk_plan(entry=165.0, point1=200.0, point2=100.0, atr=5.0)
        self.assertFalse(wide.stop_inside_noise)
        self.assertGreaterEqual(wide.stop_atr, NOISE_STOP_ATR)

    def test_needs_leverage_when_stop_is_very_tight(self):
        plan = build_risk_plan(entry=199.5, point1=200.0, point2=100.0)
        self.assertGreater(plan.position_pct, 100.0)
        self.assertTrue(plan.needs_leverage)
        self.assertIn("плечо", format_risk_line(plan))


class TestFormatRiskLine(unittest.TestCase):
    def test_contains_key_numbers_and_no_markup(self):
        plan = build_risk_plan(entry=165.0, point1=200.0, point2=100.0)
        line = format_risk_line(plan)
        self.assertIn("R:R 1:1.9", line)
        self.assertIn("Стоп 21.2%", line)
        self.assertIn("объём ≈ 4.7% депозита", line)
        self.assertNotIn("<", line)  # без HTML -- не ломает баланс тегов
        self.assertNotIn("шум", line)

    def test_noise_warning_present_only_when_flagged(self):
        plan = build_risk_plan(entry=165.0, point1=200.0, point2=100.0, atr=40.0)
        self.assertIn("внутри обычного дневного шума", format_risk_line(plan))


class TestAtr14(unittest.TestCase):
    def _candles(self, n, high, low, close):
        d0 = date(2026, 1, 1)
        return [Candle(dt=d0 + timedelta(days=i), open=close, high=high, low=low, close=close, volume=1000) for i in range(n)]

    def test_none_when_too_few_candles(self):
        self.assertIsNone(atr14(self._candles(14, 11, 9, 10)))

    def test_constant_range(self):
        self.assertAlmostEqual(atr14(self._candles(30, 11.0, 9.0, 10.0)), 2.0)

    def test_gap_counts_via_previous_close(self):
        candles = self._candles(15, 10.0, 10.0, 10.0)  # нулевой внутридневной диапазон
        d = candles[-1].dt
        candles[-1] = Candle(dt=d, open=20.0, high=20.0, low=20.0, close=20.0, volume=1000)  # гэп +10 от прошлого закрытия
        self.assertAlmostEqual(atr14(candles), 10.0 / 14)


class TestEdgeStats(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(edge_key("stock", 1, 0.618), "long_0.618")
        self.assertEqual(edge_key("stock", -1, 0.786), "short_0.786")
        self.assertEqual(edge_key("crypto", 1, 0.618), "crypto_all")
        self.assertIsNone(edge_key("stock", 1, 1.0))  # для уровня 1.0 статистики нет -- ничего не выдумываем
        self.assertIsNone(edge_key("stock", 1, 0.5))

    def test_every_table_key_reachable_and_sane(self):
        for key, row in TABLE.items():
            for h, (n, p1, c1, ps, cs, pt, ct) in row.items():
                self.assertGreater(n, 30, f"{key}/{h}: слишком мало наблюдений для публикации")
                for v in (p1, c1, ps, cs, pt, ct):
                    self.assertTrue(0.0 <= v <= 1.0, f"{key}/{h}: доля вне [0,1]")

    def test_long_line_has_no_warning(self):
        line = format_edge_line("stock", 1, 0.618)
        self.assertIn("📊", line)
        self.assertIn("случайный вход", line)
        self.assertNotIn("⚠️", line)

    def test_short_line_warns_it_is_not_better_than_random(self):
        for lv in (0.618, 0.786):
            self.assertIn("не лучше случайного входа", format_edge_line("stock", -1, lv))

    def test_crypto_line_admits_no_confirmed_edge(self):
        self.assertIn("не подтверждено", format_edge_line("crypto", 1, 0.618))

    def test_unknown_class_returns_none(self):
        self.assertIsNone(format_edge_line("stock", 1, 1.0))


class TestFormatMessageIntegration(unittest.TestCase):
    def test_alert_has_risk_and_edge_lines(self):
        bundle = _mk_bundle(fraction=0.65)  # нисходящая 200 -> 100, цена 165: тезис "вниз" (продажа отскока)
        msg = format_message(bundle, alert_level=0.618)
        self.assertIn("📐", msg)
        self.assertIn("R:R 1:1.9", msg)
        self.assertIn("📊", msg)
        self.assertIn("не лучше случайного входа", msg)  # для short-класса

    def test_neutral_summary_has_risk_line_but_no_edge_line(self):
        msg = format_message(_mk_bundle(fraction=0.65))
        self.assertIn("📐", msg)
        self.assertNotIn("📊 История", msg)

    def test_no_risk_line_when_price_beyond_the_range(self):
        msg = format_message(_mk_bundle(fraction=1.2), alert_level=1.0)  # цена за точкой 1 -- плана нет
        self.assertNotIn("📐", msg)

    def test_crypto_kind_switches_the_edge_line(self):
        bundle = replace(_mk_bundle(fraction=0.65), asset_kind="crypto")
        msg = format_message(bundle, alert_level=0.618)
        self.assertIn("Крипта", msg)
        self.assertNotIn("История, 60/120", msg)

    def test_atr_makes_noise_warning_appear(self):
        bundle = replace(_mk_bundle(fraction=0.65), atr14=40.0)  # стоп 35 < 1 ATR
        self.assertIn("внутри обычного дневного шума", format_message(bundle, alert_level=0.618))

    def test_html_tags_stay_balanced_and_line_count_reasonable(self):
        for lvl in (None, 0.618, 0.786):
            msg = format_message(_mk_bundle(fraction=0.70), alert_level=lvl, display_name="Тест <&>")
            for tag in ("b", "i", "pre"):
                self.assertEqual(msg.count(f"<{tag}>"), msg.count(f"</{tag}>"))
            self.assertLessEqual(len(msg.splitlines()), 11, "сообщение перестало быть лёгким")
            self.assertIsNone(re.search(r"<(?!/?(b|i|pre)>)", msg), "неэкранированный '<' в тексте")


class TestNextLevelHintIsDirectionAgnostic(unittest.TestCase):
    """Регрессия 21 сентября 2026: для восходящей структуры подсказка показывала уже пройденный уровень."""

    def _next_line(self, msg):
        lines = [l for l in msg.splitlines() if l.startswith("➡️")]
        self.assertLessEqual(len(lines), 1)
        return lines[0] if lines else None

    def test_ascending_structure_points_to_deeper_level_below_price(self):
        bundle = _mk_ascending_bundle(fraction=0.637)  # цена 136.3: 0.618 пройден, глубже -- 0.786 (121.4)
        line = self._next_line(format_message(bundle, alert_level=0.618))
        self.assertIsNotNone(line)
        self.assertIn("0.786", line)
        self.assertIn("121.40", line)
        self.assertNotIn("0.618", line)

    def test_descending_structure_same_rule(self):
        line = self._next_line(format_message(_mk_bundle(fraction=0.65), alert_level=0.618))
        self.assertIn("0.786", line)
        self.assertIn("178.60", line)

    def test_shallow_depth_with_many_deeper_levels_does_not_crash_and_picks_nearest(self):
        # регрессия: sorted() по FiboLevel без ключа падал, когда глубже глубины лежало >1 уровня
        for bundle in (_mk_ascending_bundle(fraction=0.10), _mk_bundle(fraction=0.10)):
            line = self._next_line(format_message(bundle, alert_level=0.618))
            self.assertIn("0.236", line)  # ближайший более глубокий, не самый дальний

    def test_no_hint_when_next_level_is_point1_itself(self):
        # глубина 0.80 -> следующий уровень 1.0 = точка 1, она уже стоит строкой инвалидации
        self.assertIsNone(self._next_line(format_message(_mk_ascending_bundle(fraction=0.80), alert_level=0.786)))
        self.assertIsNone(self._next_line(format_message(_mk_bundle(fraction=0.80), alert_level=0.786)))


class TestPriceFormatting(unittest.TestCase):
    def test_normal_prices_unchanged(self):
        self.assertEqual(_fmt_price(200.0), "200.00")
        self.assertEqual(_fmt_price(27.583), "27.58")
        self.assertEqual(_fmt_price(1.0), "1.00")

    def test_sub_dollar_prices_keep_four_significant_digits(self):
        self.assertEqual(_fmt_price(0.4123), "0.4123")
        self.assertEqual(_fmt_price(0.03123), "0.03123")
        self.assertEqual(_fmt_price(0.0004123), "0.0004123")
        for x in (0.5, 0.05, 0.005, 0.00005):
            self.assertNotEqual(float(_fmt_price(x)), 0.0)

    def test_tiny_priced_asset_message_has_no_zero_prices(self):
        msg = format_message(_mk_ascending_bundle(fraction=0.70, scale=0.0004), alert_level=0.618)
        self.assertNotIn("0.00 ", msg)
        self.assertNotIn(">0.00<", msg)
        self.assertIn("закрытие за <b>0.0400", msg)  # точка 1 = 100 * 0.0004 = 0.04


class TestEdgeLineOnlyForDailyStructures(unittest.TestCase):
    def test_weekly_or_monthly_structure_gets_risk_line_but_no_base_rates(self):
        for tf in ("Неделя", "Месяц", "1H", "4H"):
            bundle = replace(_mk_ascending_bundle(fraction=0.70), timeframe=tf)
            msg = format_message(bundle, alert_level=0.618)
            self.assertIn("📐", msg, tf)
            self.assertNotIn("📊 История", msg, tf)
            self.assertNotIn("Крипта", msg, tf)

    def test_daily_structure_keeps_base_rates(self):
        msg = format_message(replace(_mk_ascending_bundle(fraction=0.70), timeframe="1D"), alert_level=0.618)
        self.assertIn("📊 История, 60/120", msg)


class TestHistoryGuardAndWideStop(unittest.TestCase):
    def test_short_history_replaces_edge_line_with_warning(self):
        bundle = replace(_mk_ascending_bundle(fraction=0.70), history_bars=38)
        msg = format_message(bundle, alert_level=0.618)
        self.assertIn("История всего 38 свечей", msg)
        self.assertNotIn("📊 История, 60/120", msg)

    def test_enough_history_keeps_edge_line(self):
        bundle = replace(_mk_ascending_bundle(fraction=0.70), history_bars=MIN_HISTORY_BARS_FOR_EDGE)
        self.assertIn("📊 История, 60/120", format_message(bundle, alert_level=0.618))

    def test_unknown_history_does_not_block_the_line(self):
        self.assertIn("📊 История, 60/120", format_message(_mk_ascending_bundle(fraction=0.70), alert_level=0.618))

    def test_wide_stop_flag(self):
        plan = build_risk_plan(entry=27.58, point1=8.28, point2=61.45)  # стоп 70% цены
        self.assertTrue(plan.stop_very_wide)
        self.assertIn("очень широкий стоп", format_risk_line(plan))
        normal = build_risk_plan(entry=165.0, point1=200.0, point2=100.0)  # 21%
        self.assertFalse(normal.stop_very_wide)
        self.assertNotIn("очень широкий", format_risk_line(normal))


if __name__ == "__main__":
    unittest.main(verbosity=2)
