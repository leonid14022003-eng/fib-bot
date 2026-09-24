"""
Тесты для НОВОГО кода 14 сентября 2026 (широкий охват -- объединение живой
FMP-системы с движком-портом FibonacciDesk, по решению Леонида "объединить
лучшие качества обеих версий"):
- agents/data_agent.py: load_yahoo_daily()
- broad_screener.py: _deduped_instruments(), _new_local_grid_events() (с 24
  сентября -- устойчивость к откатам цепочки из-за дыр в данных Yahoo),
  _report_delivery()
- agents/dispatch_agent.py: format_local_grid_message()

Отдельный файл, тот же принцип и стиль mock'а requests.get, что и в
tests/test_intraday_agent.py (unittest.TestCase + patch("requests.get", ...)).
Запускается отдельно: python3 tests/test_broad_screener.py
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.data_agent import CandleSeries, load_yahoo_daily
from agents.dispatch_agent import format_local_grid_message
from agents.local_grid_agent import Direction, ExtremePoint, GlobalGrid, LocalGrid, LocalState, level_prices


def _mock_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status.side_effect = None
    return resp


def _yahoo_payload(bars, exchange_tz="UTC", session_end=None):
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "exchangeTimezoneName": exchange_tz,
                        "currentTradingPeriod": {
                            "regular": {"end": session_end if session_end is not None else time.time() + 3600}
                        },
                        "exchangeName": "TEST",
                    },
                    "timestamp": [b["ts"] for b in bars],
                    "indicators": {
                        "quote": [
                            {
                                "open": [b["open"] for b in bars],
                                "high": [b["high"] for b in bars],
                                "low": [b["low"] for b in bars],
                                "close": [b["close"] for b in bars],
                                "volume": [b.get("volume", 1000) for b in bars],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def _ts(dt: datetime) -> int:
    return int(dt.timestamp())


class TestLoadYahooDaily(unittest.TestCase):
    def test_drops_still_forming_today_bar(self):
        now = datetime.now(timezone.utc)
        past_day = now - timedelta(days=10)
        bars = [
            {"ts": _ts(past_day), "open": 10, "high": 11, "low": 9, "close": 10.5},
            {"ts": _ts(now), "open": 20, "high": 21, "low": 19, "close": 20.5},  # сегодня, сессия ещё идёт
        ]
        payload = _yahoo_payload(bars, session_end=time.time() + 3600)
        with patch("requests.get", return_value=_mock_response(payload)):
            series = load_yahoo_daily("TEST")
        self.assertEqual(len(series.candles), 1, "сегодняшний ещё не закрытый бар должен быть отброшен")
        self.assertEqual(series.candles[0].close, 10.5)

    def test_keeps_today_bar_once_session_has_ended(self):
        now = datetime.now(timezone.utc)
        bars = [{"ts": _ts(now), "open": 20, "high": 21, "low": 19, "close": 20.5}]
        # Сессия закончилась больше 120с назад -> сегодняшний бар уже закрыт.
        payload = _yahoo_payload(bars, session_end=time.time() - 1000)
        with patch("requests.get", return_value=_mock_response(payload)):
            series = load_yahoo_daily("TEST")
        self.assertEqual(len(series.candles), 1)

    def test_sorts_ascending_regardless_of_input_order(self):
        now = datetime.now(timezone.utc)
        d1, d2 = now - timedelta(days=20), now - timedelta(days=10)
        bars = [
            {"ts": _ts(d2), "open": 20, "high": 21, "low": 19, "close": 20.5},
            {"ts": _ts(d1), "open": 10, "high": 11, "low": 9, "close": 10.5},
        ]
        payload = _yahoo_payload(bars, session_end=time.time() - 1000)
        with patch("requests.get", return_value=_mock_response(payload)):
            series = load_yahoo_daily("TEST")
        self.assertLess(series.candles[0].dt, series.candles[1].dt)

    def test_raises_on_structurally_invalid_ohlc(self):
        now = datetime.now(timezone.utc)
        past_day = now - timedelta(days=10)
        bars = [{"ts": _ts(past_day), "open": 10, "high": 9, "low": 8, "close": 10.5}]  # high < close
        payload = _yahoo_payload(bars, session_end=time.time() - 1000)
        with patch("requests.get", return_value=_mock_response(payload)):
            with self.assertRaises(ValueError):
                load_yahoo_daily("TEST")

    def test_raises_when_no_closed_bars_at_all(self):
        payload = _yahoo_payload([], session_end=time.time() - 1000)
        with patch("requests.get", return_value=_mock_response(payload)):
            with self.assertRaises(ValueError):
                load_yahoo_daily("TEST")

    def test_raises_with_yahoo_error_message_when_no_result(self):
        payload = {"chart": {"result": None, "error": {"description": "No data found"}}}
        with patch("requests.get", return_value=_mock_response(payload)):
            with self.assertRaises(ValueError) as ctx:
                load_yahoo_daily("BADTICKER")
        self.assertIn("No data found", str(ctx.exception))

    def test_months_back_controls_period1(self):
        now = datetime.now(timezone.utc)
        bars = [{"ts": _ts(now - timedelta(days=10)), "open": 10, "high": 11, "low": 9, "close": 10.5}]
        payload = _yahoo_payload(bars, session_end=time.time() - 1000)
        with patch("requests.get", return_value=_mock_response(payload)) as mock_get:
            load_yahoo_daily("TEST", months_back=12)
        params = mock_get.call_args.kwargs["params"]
        expected_from = date.today() - timedelta(days=int(12 * 30.44))
        actual_from = datetime.fromtimestamp(params["period1"], tz=timezone.utc).date()
        self.assertEqual(actual_from, expected_from)


class TestDedupedInstruments(unittest.TestCase):
    def test_no_overlap_with_live_screener_instruments(self):
        import broad_screener
        from screener import INSTRUMENTS

        broad_symbols = {i.symbol for i in broad_screener.BROAD_INSTRUMENTS}
        live_symbols = {i.symbol for i in INSTRUMENTS}
        self.assertEqual(broad_symbols & live_symbols, set())

    def test_produces_a_non_trivial_list(self):
        import broad_screener

        # Широкий универсум -- 248 в data/tradfi_universe.json, минус ~6 без
        # Yahoo-маппинга, минус пересечение с screener.py -- в любом случае
        # должно остаться заметно больше нуля, иначе дедуп/маппинг сломан.
        self.assertGreater(len(broad_screener.BROAD_INSTRUMENTS), 100)


class TestNewLocalGridEvents(unittest.TestCase):
    def _point(self, day, price, kind):
        return ExtremePoint(dt=date(2020, 1, 1) + timedelta(days=day), price=price, kind=kind, index=day)

    def _local(self, seq, state, point1_price=100.0, point2_price=50.0):
        p1 = self._point(0, point1_price, "HIGH")
        p2 = self._point(seq, point2_price, "LOW")
        return LocalGrid(
            seq=seq, direction=Direction.DOWN, point1=p1, state=state,
            launch_date=p1.dt, point2_preliminary=p2,
            confirmed_date=p2.dt if state != LocalState.FORMING else None,
            point2_final=p2 if state != LocalState.FORMING else None,
        )

    @staticmethod
    def _global(point2_day=0):
        p1 = ExtremePoint(dt=date(2019, 1, 1), price=100.0, kind="HIGH", index=0)
        p2 = ExtremePoint(dt=date(2020, 1, 1) + timedelta(days=point2_day), price=50.0, kind="LOW", index=1)
        return GlobalGrid(direction=Direction.DOWN, point1=p1, point2=p2, levels=level_prices(100.0, 50.0))

    @dataclass
    class _FakeChain:
        completed: list
        current: object
        global_grid: object = field(default_factory=lambda: TestNewLocalGridEvents._global())

    def test_first_run_reports_everything_seen(self):
        import broad_screener

        chain = self._FakeChain(completed=[self._local(1, LocalState.EXITED)], current=self._local(2, LocalState.FORMING))
        state: dict = {}
        new_events = broad_screener._new_local_grid_events("TEST", chain, state)
        self.assertEqual([e.seq for e in new_events], [1, 2])
        self.assertEqual(state["TEST"].last_seq, 2)
        self.assertEqual(state["TEST"].last_state, LocalState.FORMING.value)

    def test_no_repeat_when_nothing_changed(self):
        import broad_screener

        chain = self._FakeChain(completed=[], current=self._local(1, LocalState.FORMING))
        state: dict = {}
        broad_screener._new_local_grid_events("TEST", chain, state)  # первый прогон, заполняет память
        new_events = broad_screener._new_local_grid_events("TEST", chain, state)  # тот же chain повторно
        self.assertEqual(new_events, [])

    def test_state_progression_within_same_seq_is_reported(self):
        import broad_screener

        state: dict = {}
        forming_chain = self._FakeChain(completed=[], current=self._local(1, LocalState.FORMING))
        broad_screener._new_local_grid_events("TEST", forming_chain, state)

        fixed_chain = self._FakeChain(completed=[], current=self._local(1, LocalState.FIXED))
        new_events = broad_screener._new_local_grid_events("TEST", fixed_chain, state)
        self.assertEqual(len(new_events), 1)
        self.assertEqual(new_events[0].state, LocalState.FIXED)

    def test_state_rollback_from_yahoo_gap_is_not_resent(self):
        # INTC 22-23 сентября 2026: зафиксирована -> (дыра в свече у Yahoo)
        # формирование -> (свеча вернулась) зафиксирована. Второй раз
        # "зафиксирована" уходить не должна, откат -- тоже.
        import broad_screener

        state: dict = {}
        fixed = self._FakeChain(completed=[], current=self._local(1, LocalState.FIXED))
        rolled_back = self._FakeChain(completed=[], current=self._local(1, LocalState.FORMING))
        self.assertEqual(len(broad_screener._new_local_grid_events("TEST", fixed, state)), 1)
        self.assertEqual(broad_screener._new_local_grid_events("TEST", rolled_back, state), [])
        self.assertEqual(broad_screener._new_local_grid_events("TEST", fixed, state), [])
        self.assertEqual(state["TEST"].last_state, LocalState.FIXED.value)

    def test_seq_rollback_then_return_is_not_resent(self):
        # AMC 22-23 сентября 2026: №2 завершена + №3 формирование -> молча
        # откат до №2 зафиксирована -> снова №2 завершена + №3 формирование.
        import broad_screener

        state: dict = {}
        ahead = self._FakeChain(completed=[self._local(2, LocalState.EXITED)], current=self._local(3, LocalState.FORMING))
        behind = self._FakeChain(completed=[], current=self._local(2, LocalState.FIXED))
        self.assertEqual(len(broad_screener._new_local_grid_events("TEST", ahead, state)), 2)
        self.assertEqual(broad_screener._new_local_grid_events("TEST", behind, state), [])
        self.assertEqual(broad_screener._new_local_grid_events("TEST", ahead, state), [])
        # Настоящее продвижение после этого по-прежнему уходит.
        further = self._FakeChain(completed=[self._local(2, LocalState.EXITED)], current=self._local(3, LocalState.FIXED))
        self.assertEqual([e.state for e in broad_screener._new_local_grid_events("TEST", further, state)], [LocalState.FIXED])

    def test_new_global_grid_keeps_previous_rule(self):
        # Бумага каждый день обновляет экстремум: новая точка 2 -> цепочка
        # снова "№1 формирование". Как и до 24 сентября, это не повод слать
        # каждый день; дальнейшее продвижение новой цепочки -- повод.
        import broad_screener

        state: dict = {}
        broad_screener._new_local_grid_events("TEST", self._FakeChain([], self._local(1, LocalState.FORMING), self._global(0)), state)
        self.assertEqual(
            broad_screener._new_local_grid_events("TEST", self._FakeChain([], self._local(1, LocalState.FORMING), self._global(1)), state), []
        )
        new_events = broad_screener._new_local_grid_events(
            "TEST", self._FakeChain([], self._local(1, LocalState.FIXED), self._global(1)), state
        )
        self.assertEqual([e.state for e in new_events], [LocalState.FIXED])

    def test_flapping_global_point2_is_not_resent(self):
        # Дыра пришлась на день экстремума: точка 2 глобальной сетки то одна,
        # то другая. Возврат к уже виденной глобальной сетке не повод
        # повторять её события.
        import broad_screener

        state: dict = {}
        grid_a = self._FakeChain([self._local(1, LocalState.EXITED)], self._local(2, LocalState.FIXED), self._global(0))
        grid_b = self._FakeChain([], self._local(1, LocalState.FORMING), self._global(1))
        self.assertEqual(len(broad_screener._new_local_grid_events("TEST", grid_a, state)), 2)
        self.assertEqual(broad_screener._new_local_grid_events("TEST", grid_b, state), [])
        self.assertEqual(broad_screener._new_local_grid_events("TEST", grid_a, state), [])

    def test_point2_moving_back_in_time_sends_nothing(self):
        # UVXY/SKDD на реальных данных 24 сентября: без двух последних свечей
        # точка 2 глобальной сетки уезжает на более раннюю дату (сентябрь 4
        # вместо 22) -- это потеря данных, а не новый экстремум.
        import broad_screener

        state: dict = {}
        broad_screener._new_local_grid_events("TEST", self._FakeChain([], self._local(1, LocalState.FORMING), self._global(10)), state)
        older = self._FakeChain([], self._local(1, LocalState.FIXED), self._global(2))
        self.assertEqual(broad_screener._new_local_grid_events("TEST", older, state), [])
        back = self._FakeChain([], self._local(1, LocalState.FORMING), self._global(10))
        self.assertEqual(broad_screener._new_local_grid_events("TEST", back, state), [])

    def test_old_format_state_is_treated_as_current_global_grid(self):
        # Файл памяти до 24 сентября 2026: только last_seq/last_state, без global_key.
        import broad_screener

        state = {"TEST": broad_screener.LocalGridSeenState(last_seq=1, last_state=LocalState.FIXED.value)}
        rolled_back = self._FakeChain(completed=[], current=self._local(1, LocalState.FORMING))
        self.assertEqual(broad_screener._new_local_grid_events("TEST", rolled_back, state), [])
        self.assertEqual(state["TEST"].last_state, LocalState.FIXED.value)

    def test_state_file_roundtrip_keeps_global_grid_memory(self):
        import broad_screener

        state: dict = {}
        broad_screener._new_local_grid_events(
            "TEST", self._FakeChain([], self._local(2, LocalState.FIXED), self._global(0)), state
        )
        broad_screener._new_local_grid_events(
            "TEST", self._FakeChain([], self._local(1, LocalState.FORMING), self._global(1)), state
        )
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(broad_screener, "BROAD_LOCAL_GRID_STATE_PATH", Path(tmp) / "state.json"):
                broad_screener._save_local_grid_state(state)
                loaded = broad_screener._load_local_grid_state()
        self.assertEqual(loaded, state)
        self.assertEqual(
            loaded["TEST"].previous, {broad_screener._global_key(self._global(0)): [2, LocalState.FIXED.value]}
        )

    def test_remembered_global_grids_are_capped(self):
        import broad_screener

        state: dict = {}
        for day in range(broad_screener.MAX_REMEMBERED_GLOBAL_GRIDS + 3):
            broad_screener._new_local_grid_events(
                "TEST", self._FakeChain([], self._local(1, LocalState.FORMING), self._global(day)), state
            )
        self.assertEqual(len(state["TEST"].previous), broad_screener.MAX_REMEMBERED_GLOBAL_GRIDS)


class TestReportDelivery(unittest.TestCase):
    def test_dry_run_is_not_a_failure(self):
        import broad_screener

        skipped = {"sent_to": [{"recipient": "A", "status": "SKIPPED (нет токена или chat_id)"}]}
        self.assertEqual(broad_screener._report_delivery({"текст": skipped}), 0)

    def test_counts_errors_per_send(self):
        import broad_screener

        photo = {"sent_to": [{"recipient": "A", "status": "sent"}, {"recipient": "B", "status": "ERROR 400: x"}]}
        text = {"sent_to": [{"recipient": "A", "status": "EXCEPTION: timeout"}, {"recipient": "B", "status": "sent"}]}
        self.assertEqual(broad_screener._report_delivery({"фото": photo, "текст": text}), 2)


class TestFormatLocalGridMessage(unittest.TestCase):
    def test_html_tags_balanced_and_symbol_present(self):
        p1 = ExtremePoint(dt=date(2020, 1, 1), price=100.0, kind="HIGH", index=0)
        p2 = ExtremePoint(dt=date(2020, 3, 1), price=50.0, kind="LOW", index=10)
        global_grid = GlobalGrid(direction=Direction.DOWN, point1=p1, point2=p2, levels=level_prices(100.0, 50.0))
        local = LocalGrid(
            seq=1, direction=Direction.UP, point1=p2, state=LocalState.FIXED,
            launch_date=p2.dt, point2_preliminary=p1, confirmed_date=date(2020, 3, 15), point2_final=p1,
        )
        msg = format_local_grid_message("TEST", global_grid, local, display_name="Test Instrument")
        for tag in ("b", "i", "pre"):
            self.assertEqual(msg.count(f"<{tag}>"), msg.count(f"</{tag}>"), f"несбалансированные теги <{tag}>")
        self.assertIn("TEST", msg)
        self.assertIn("Test Instrument", msg)
        self.assertIn(LocalState.FIXED.value, msg)

    def test_exited_grid_shows_exit_details(self):
        p1 = ExtremePoint(dt=date(2020, 1, 1), price=100.0, kind="HIGH", index=0)
        p2 = ExtremePoint(dt=date(2020, 3, 1), price=50.0, kind="LOW", index=10)
        global_grid = GlobalGrid(direction=Direction.DOWN, point1=p1, point2=p2, levels=level_prices(100.0, 50.0))
        local = LocalGrid(
            seq=1, direction=Direction.UP, point1=p2, state=LocalState.EXITED,
            launch_date=p2.dt, point2_preliminary=p1, confirmed_date=date(2020, 3, 15), point2_final=p1,
            exit_date=date(2020, 4, 1), exit_border="0%", exit_price=101.0,
        )
        msg = format_local_grid_message("TEST", global_grid, local)
        self.assertIn("0%", msg)
        self.assertIn("101.00", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
