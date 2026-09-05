"""
Тесты для НОВОГО кода 2 сентября 2026 (раздел 7.3 регламента):
- agents/data_agent.py: load_fmp_intraday(), IntradayCandle/IntradayCandleSeries
- agents/intraday_agent.py: build_intraday_note()
- agents/dispatch_agent.py: format_message(intraday_note=...)

Отдельный файл, НЕ добавлен внутрь существующего tests/test_pipeline.py --
у Claude в этой сессии нет содержимого реального test_pipeline.py с сервера,
поэтому дописывать в него вслепую (не видя структуру классов/стиль) рискованно.
Запускается отдельно: python3 tests/test_intraday_agent.py
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.data_agent import IntradayCandle, IntradayCandleSeries, load_fmp_intraday
from agents.dispatch_agent import (
    AnalysisBundle,
    format_message,
)
from agents.fibo_agent import Direction, FiboLevel, FiboStructure, StructureScope, SwingPoint
from agents.intraday_agent import IntradayConfirmation, _check_one_timeframe, build_intraday_note
from agents.price_behavior_agent import nearest_level
from agents.verification_agent import ChecklistReport, CheckResult


def _mock_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status.side_effect = None
    return resp


def _flat_row(date_str, o, h, l, c, v=1000):
    return {"date": date_str, "open": o, "high": h, "low": l, "close": c, "volume": v}


class TestLoadFmpIntraday(unittest.TestCase):
    def test_flat_list_response_parses(self):
        now = datetime.now()
        rows = [
            _flat_row((now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S"), 100, 101, 99, 100.5),
            _flat_row((now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"), 100.5, 102, 100, 101.5),
        ]
        with patch("requests.get", return_value=_mock_response(rows)):
            series = load_fmp_intraday("AAPL", interval="1hour", api_key="fake-key", days_back=5)
        self.assertIsInstance(series, IntradayCandleSeries)
        self.assertEqual(series.timeframe, "1H")
        self.assertEqual(len(series.candles), 2)
        # хронологический порядок (старые -> новые), как и у дневных свечей
        self.assertLess(series.candles[0].dt, series.candles[1].dt)

    def test_dict_wrapped_response_falls_back_to_results_key(self):
        now = datetime.now()
        rows = [_flat_row(now.strftime("%Y-%m-%d %H:%M:%S"), 1, 2, 0.5, 1.5)]
        with patch("requests.get", return_value=_mock_response({"symbol": "AAPL", "results": rows})):
            series = load_fmp_intraday("AAPL", interval="1hour", api_key="fake-key", days_back=5)
        self.assertEqual(len(series.candles), 1)

    def test_unrecognized_dict_shape_raises_clear_error(self):
        with patch("requests.get", return_value=_mock_response({"Error Message": "Invalid API KEY."})):
            with self.assertRaises(ValueError) as ctx:
                load_fmp_intraday("AAPL", interval="1hour", api_key="fake-key", days_back=5)
        self.assertIn("Неожиданный формат", str(ctx.exception))

    def test_bad_date_field_raises_clear_error_not_silent_skip(self):
        rows = [{"date": "not-a-date", "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 1}]
        with patch("requests.get", return_value=_mock_response(rows)):
            with self.assertRaises(ValueError) as ctx:
                load_fmp_intraday("AAPL", interval="1hour", api_key="fake-key", days_back=5)
        self.assertIn("даты/времени", str(ctx.exception))

    def test_days_back_cutoff_filters_old_candles(self):
        now = datetime.now()
        rows = [
            _flat_row((now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S"), 1, 2, 0.5, 1.5),  # старее потолка
            _flat_row((now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"), 1, 2, 0.5, 1.5),  # свежая
        ]
        with patch("requests.get", return_value=_mock_response(rows)):
            series = load_fmp_intraday("AAPL", interval="1hour", api_key="fake-key", days_back=5)
        self.assertEqual(len(series.candles), 1)  # старая свеча честно отфильтрована

    def test_empty_result_after_cutoff_raises_not_silent(self):
        now = datetime.now()
        rows = [_flat_row((now - timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S"), 1, 2, 0.5, 1.5)]
        with patch("requests.get", return_value=_mock_response(rows)):
            with self.assertRaises(ValueError) as ctx:
                load_fmp_intraday("AAPL", interval="1hour", api_key="fake-key", days_back=5)
        self.assertIn("пустой список", str(ctx.exception))

    def test_structurally_bad_ohlc_raises(self):
        now = datetime.now()
        # high меньше close -- нарушает "тени огибают тело", раздел 6 регламента
        rows = [_flat_row(now.strftime("%Y-%m-%d %H:%M:%S"), 100, 100.1, 99, 105)]
        with patch("requests.get", return_value=_mock_response(rows)):
            with self.assertRaises(ValueError) as ctx:
                load_fmp_intraday("AAPL", interval="1hour", api_key="fake-key", days_back=5)
        self.assertIn("Структурно некорректные", str(ctx.exception))

    def test_missing_api_key_raises_before_any_network_call(self):
        # patch.dict(..., clear=True) -- на реальном сервере MARKET_DATA_API_KEY
        # часто уже стоит в окружении текущей shell-сессии (например, после
        # `source .env` в этом же терминале чуть раньше) -- load_fmp_intraday
        # ПРАВИЛЬНО подхватывает такой ambient-ключ, когда api_key=None (это
        # её документированное поведение). Тест обязан сам гарантировать
        # пустое окружение для ЭТОГО конкретного сценария "ключа нет вообще
        # нигде", а не полагаться на то, что переменная случайно не задана
        # снаружи -- иначе тест ложно падает не из-за бага в коде, а из-за
        # состояния чужой shell-сессии (найдено вживую на сервере 2 сентября).
        with patch.dict("os.environ", {}, clear=True):
            with patch("requests.get") as mock_get:
                with self.assertRaises(ValueError) as ctx:
                    load_fmp_intraday("AAPL", interval="1hour", api_key=None, days_back=5)
                mock_get.assert_not_called()
        self.assertIn("API-ключа", str(ctx.exception))

    def test_unsupported_interval_rejected(self):
        with self.assertRaises(ValueError):
            load_fmp_intraday("AAPL", interval="15min", api_key="fake-key", days_back=5)


def _make_structure(direction=Direction.ASCENDING, p1_price=100.0, p2_price=200.0, dt1=None, dt2=None):
    dt1 = dt1 or datetime(2026, 8, 1, 10, 0, 0)
    dt2 = dt2 or datetime(2026, 8, 2, 14, 0, 0)
    p1 = SwingPoint(dt=dt1, price=p1_price, kind="LOW" if direction == Direction.ASCENDING else "HIGH", index=0)
    p2 = SwingPoint(dt=dt2, price=p2_price, kind="HIGH" if direction == Direction.ASCENDING else "LOW", index=10)
    levels = [FiboLevel(level=lv, price=p2_price + lv * (p1_price - p2_price)) for lv in (0.0, 0.618, 1.0)]
    return FiboStructure(scope=StructureScope.GLOBAL, direction=direction, point1=p1, point2=p2, levels=levels)


class TestCheckOneTimeframe(unittest.TestCase):
    def test_available_produces_note_with_timeframe_label(self):
        structure = _make_structure()
        with patch("agents.intraday_agent.load_fmp_intraday"), \
             patch("agents.intraday_agent.build_global_fibo", return_value=structure):
            result = _check_one_timeframe("AAPL", "1hour", current_price=150.0, exchange_hint="NASDAQ")
        self.assertIsInstance(result, IntradayConfirmation)
        self.assertTrue(result.available)
        self.assertEqual(result.timeframe, "1H")
        self.assertIn("1H", result.note)

    def test_4hour_label(self):
        structure = _make_structure()
        with patch("agents.intraday_agent.load_fmp_intraday"), \
             patch("agents.intraday_agent.build_global_fibo", return_value=structure):
            result = _check_one_timeframe("AAPL", "4hour", current_price=150.0, exchange_hint="NASDAQ")
        self.assertEqual(result.timeframe, "4H")

    def test_any_exception_is_caught_and_reported_as_unavailable(self):
        with patch("agents.intraday_agent.load_fmp_intraday", side_effect=ValueError("нет подтверждённого экстремума")):
            result = _check_one_timeframe("AAPL", "1hour", current_price=150.0, exchange_hint="NASDAQ")
        self.assertFalse(result.available)
        self.assertIn("недоступно", result.note)

    def test_network_style_exception_also_caught(self):
        # requests.HTTPError/ConnectionError и т.п. -- НЕ ValueError, важно
        # ловить широко (Exception), иначе сетевой сбой уронит весь алерт.
        with patch("agents.intraday_agent.load_fmp_intraday", side_effect=ConnectionError("сеть недоступна")):
            result = _check_one_timeframe("AAPL", "1hour", current_price=150.0, exchange_hint="NASDAQ")
        self.assertFalse(result.available)


class TestBuildIntradayNote(unittest.TestCase):
    def test_both_available_combined_into_one_line(self):
        both_ok = [
            IntradayConfirmation(timeframe="1H", available=True, note="1H (нисходящий): тестирует 0.618"),
            IntradayConfirmation(timeframe="4H", available=True, note="4H (нисходящий): тестирует 0.618"),
        ]
        with patch("agents.intraday_agent._check_one_timeframe", side_effect=both_ok):
            note = build_intraday_note("AAPL", 150.0)
        self.assertIsNotNone(note)
        self.assertIn("1H", note)
        self.assertIn("4H", note)
        self.assertIn("раздел 7.3", note)

    def test_both_unavailable_returns_none_not_empty_placeholder(self):
        both_missing = [
            IntradayConfirmation(timeframe="1H", available=False, note="1H: недоступно (...)"),
            IntradayConfirmation(timeframe="4H", available=False, note="4H: недоступно (...)"),
        ]
        with patch("agents.intraday_agent._check_one_timeframe", side_effect=both_missing):
            note = build_intraday_note("AAPL", 150.0)
        self.assertIsNone(note)

    def test_one_available_one_not_still_returns_note(self):
        mixed = [
            IntradayConfirmation(timeframe="1H", available=True, note="1H (нисходящий): тестирует 0.618"),
            IntradayConfirmation(timeframe="4H", available=False, note="4H: недоступно (...)"),
        ]
        with patch("agents.intraday_agent._check_one_timeframe", side_effect=mixed):
            note = build_intraday_note("AAPL", 150.0)
        self.assertIsNotNone(note)
        self.assertIn("1H", note)
        self.assertIn("4H", note)  # недоступность 4H тоже честно показана, не скрыта


class TestFormatMessageIntradayNote(unittest.TestCase):
    def _bundle(self):
        structure = _make_structure(p1_price=100.0, p2_price=200.0)
        n = nearest_level(structure, current_price=150.0)
        checklist = ChecklistReport(results=[CheckResult(name="test", passed=True, detail="ok")])
        return AnalysisBundle(
            symbol="AAPL",
            source_tag="Financial Modeling Prep",
            timeframe="1D",
            period_desc="2026-08-01 .. 2026-08-30",
            structure=structure,
            nearest=n,
            recent_events=[],
            checklist=checklist,
        )

    def test_intraday_note_appears_when_passed(self):
        msg = format_message(self._bundle(), intraday_note="⏱ Внутридневное подтверждение: 1H тестирует 0.618")
        self.assertIn("Внутридневное подтверждение", msg)

    def test_intraday_note_absent_when_none(self):
        msg = format_message(self._bundle(), intraday_note=None)
        self.assertNotIn("Внутридневное подтверждение", msg)

    def test_intraday_note_html_escaped(self):
        msg = format_message(self._bundle(), intraday_note="1H <тест> & прочее")
        self.assertNotIn("<тест>", msg)  # сырой '<' не должен пройти как есть
        self.assertIn("&lt;тест&gt;", msg)

    def test_message_still_has_balanced_tags_with_both_notes(self):
        msg = format_message(
            self._bundle(),
            alert_level=0.618,
            consensus_note="Независимая сверка: СОГЛАСНЫ",
            intraday_note="⏱ Внутридневное подтверждение: 1H тестирует 0.618",
        )
        # Простая проверка баланса открывающих/закрывающих тегов -- та же
        # идея, что и test_format_message_html_tags_balanced в реальном
        # test_pipeline.py (насколько можно судить по её имени и месту в
        # проекте, см. project doc, 25 августа) -- здесь минимальная копия
        # именно для новых полей, полноценный тест остаётся на сервере.
        import re

        opens = re.findall(r"<(b|i|pre)>", msg)
        closes = re.findall(r"</(b|i|pre)>", msg)
        self.assertEqual(sorted(opens), sorted(closes))


if __name__ == "__main__":
    unittest.main(verbosity=2)
