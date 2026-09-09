"""
Тесты для agents/local_grid_agent.py -- ИССЛЕДОВАТЕЛЬСКОГО модуля (не часть
боевого пайплайна, см. докстринг модуля). Проверяют буквальное соответствие
шагам 2-10 документа "Как строятся глобальная и локальные сетки"
(загружен 9 сентября 2026), на синтетических свечах с просчитанными вручную
ожидаемыми значениями -- не на реальных рыночных данных.
"""

import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, ".")

from agents.data_agent import Candle, CandleSeries
from agents.local_grid_agent import (
    Direction,
    LocalState,
    find_global_grid,
    level_prices,
    run_local_grid_chain,
)


def _d(day: int) -> date:
    return date(2020, 1, 1) + timedelta(days=day - 1)


def _candle(day: int, high: float, low: float) -> Candle:
    mid = (high + low) / 2.0
    return Candle(dt=_d(day), open=mid, high=high, low=low, close=mid, volume=0)


def _series(candles: list[Candle]) -> CandleSeries:
    return CandleSeries(
        symbol="TEST",
        exchange_or_source="synthetic",
        timeframe="1D",
        candles=candles,
        fetched_via="synthetic",
        fetch_note="synthetic fixture for local_grid_agent tests",
    )


# Свечи 1-7, общие для нескольких тестов: глобальная сетка ATH=100 (день 1) ->
# ATL=10 (день 4). Первая локальная восходящая запускается СРАЗУ на дне 4
# (Шаг 4, уточнено 9 сентября 2026 -- без "ожидания"): день 5 -- новый
# экстремум (точка 2 предварительная = 30), день 6 -- свеча А (пробивает
# локальные 50%=20 ТЕНЬЮ: low=18<=20, но закрытие 21.5 НЕ за уровнем),
# день 7 -- свеча Б (закрывается за 50%: close=18<=20) -> ПОДТВЕРЖДЕНО на
# дне 7, точка 2 финальная = 30 (день 5), диапазон [10, 30].
BASE_CANDLES = [
    _candle(1, high=100, low=95),   # глобальный ATH
    _candle(2, high=90, low=85),
    _candle(3, high=88, low=80),
    _candle(4, high=85, low=10),    # глобальный ATL -> точка 1 локальной №1 = 10
    _candle(5, high=30, low=20),    # новый экстремум -> точка 2 предв. = 30
    _candle(6, high=25, low=18),    # свеча А: тень пробивает 50%=20, закрытие 21.5 -- нет
    _candle(7, high=20, low=16),    # свеча Б: закрытие 18 <= 20 -- ПОДТВЕРЖДЕНО
]


class TestFindGlobalGrid(unittest.TestCase):
    def test_ascending_when_low_earlier_than_high(self):
        candles = [_candle(1, high=50, low=10), _candle(2, high=100, low=40)]
        grid = find_global_grid(_series(candles))
        self.assertEqual(grid.direction, Direction.UP)
        self.assertEqual(grid.point1.kind, "LOW")
        self.assertEqual(grid.point1.price, 10)
        self.assertEqual(grid.point2.kind, "HIGH")
        self.assertEqual(grid.point2.price, 100)

    def test_descending_when_high_earlier_than_low(self):
        grid = find_global_grid(_series(BASE_CANDLES))
        self.assertEqual(grid.direction, Direction.DOWN)
        self.assertEqual(grid.point1.price, 100)
        self.assertEqual(grid.point1.dt, _d(1))
        self.assertEqual(grid.point2.price, 10)
        self.assertEqual(grid.point2.dt, _d(4))

    def test_tie_break_uses_earliest_occurrence(self):
        candles = [
            _candle(1, high=100, low=50),
            _candle(2, high=100, low=20),  # тот же high, позже -- не должен победить
            _candle(3, high=60, low=20),
        ]
        grid = find_global_grid(_series(candles))
        self.assertEqual(grid.point1.dt, _d(1))  # раньше по времени, не день 2

    def test_same_candle_ath_atl_raises(self):
        candles = [_candle(1, high=100, low=1), _candle(2, high=50, low=50)]
        with self.assertRaises(ValueError):
            find_global_grid(_series(candles))

    def test_insufficient_candles_raises(self):
        with self.assertRaises(ValueError):
            find_global_grid(_series([_candle(1, high=10, low=5)]))


class TestLevelPrices(unittest.TestCase):
    def test_formula_matches_document_step3(self):
        levels = level_prices(point1_price=100.0, point2_price=0.0)
        self.assertEqual(levels[0.0], 0.0)
        self.assertEqual(levels[1.0], 100.0)
        self.assertEqual(levels[0.618], 61.8)
        self.assertEqual(levels[0.5], 50.0)


class TestLocalGridChainConfirmation(unittest.TestCase):
    """Шаги 4-7 (уточнено 9 сентября 2026): формирование стартует СРАЗУ на
    точке 1 (без "ожидания" глобальных 50%), подтверждение -- двусвечное
    закрепление за локальными 50% (свеча А пробивает тенью, свеча Б
    закрывается за уровнем)."""

    def test_launches_immediately_on_global_point2_without_waiting(self):
        # Шаг 4: локальная №1 в статусе FORMING сразу с дня 4 (точка 2
        # глобальной), а не после достижения глобальных 50%.
        candles = BASE_CANDLES[:5]  # глобальная сетка + день 5 (новый экстремум)
        result = run_local_grid_chain(_series(candles))
        current = result.current
        self.assertEqual(current.state, LocalState.FORMING)
        self.assertEqual(current.launch_date, _d(4))
        self.assertEqual(current.point1.price, 10)
        self.assertEqual(current.point2_preliminary.price, 30)

    def test_two_candle_confirmation_wick_then_close(self):
        result = run_local_grid_chain(_series(BASE_CANDLES))
        self.assertEqual(len(result.completed), 0)
        current = result.current
        self.assertIsNotNone(current)
        self.assertEqual(current.state, LocalState.FIXED)
        self.assertEqual(current.direction, Direction.UP)
        self.assertEqual(current.point1.price, 10)           # глобальная точка 2 = локальная точка 1
        self.assertEqual(current.launch_date, _d(4))          # сразу, без ожидания (Шаг 4)
        self.assertEqual(current.confirmed_date, _d(7))       # свеча Б, не день 6 (только тень)
        self.assertEqual(current.point2_final.price, 30)
        self.assertEqual(current.point2_final.dt, _d(5))

    def test_single_candle_closing_beyond_level_does_not_confirm_alone(self):
        # День 6' сам закрывается за 50%=20 (close=17), но БЕЗ предшествующей
        # свечи-теста (пары) это не подтверждение -- нужна ИМЕННО пара.
        candles = BASE_CANDLES[:5] + [_candle(6, high=18, low=16)]  # close=17<=20
        result = run_local_grid_chain(_series(candles))
        self.assertEqual(result.current.state, LocalState.FORMING)
        self.assertEqual(result.current.point2_preliminary.price, 30)

    def test_new_extreme_invalidates_pending_pair(self):
        # День 6: свеча А (тень пробивает 50%=20, закрытие 21.5 -- нет).
        # День 7: НОВЫЙ экстремум (40) -- пара со дня 6 аннулируется.
        # День 8: сама по себе закрывается за новыми 50%=25 (close=23), но
        # пары ещё нет (пендинг сброшен днём 7) -- не подтверждение.
        # День 9: свеча Б после дня 8 (пробившего тенью) -- ПОДТВЕРЖДЕНО.
        candles = BASE_CANDLES[:5] + [
            _candle(6, high=25, low=18),   # свеча А для старой (отменённой) пары
            _candle(7, high=40, low=35),   # новый экстремум -> точка 2 предв. = 40
            _candle(8, high=24, low=22),   # тень пробивает 50%=25, close=23<=25 -- но пендинга нет
            _candle(9, high=24, low=20),   # свеча Б после дня 8 -- close=22<=25 -- ПОДТВЕРЖДЕНО
        ]
        result = run_local_grid_chain(_series(candles))
        current = result.current
        self.assertEqual(current.state, LocalState.FIXED)
        self.assertEqual(current.confirmed_date, _d(9))
        self.assertEqual(current.point2_final.price, 40)
        self.assertEqual(current.point2_final.dt, _d(7))


class TestLocalGridChainExit(unittest.TestCase):
    """Шаг 8 (уточнено 9 сентября 2026): выход -- тот же двусвечный принцип,
    что и подтверждение (Шаг 6), но за границей диапазона [10, 30] вместо
    локальных 50%. Точка 1=10, точка 2=30 -- к концу BASE_CANDLES (день 7)."""

    def _fixed_prefix(self) -> list[Candle]:
        return list(BASE_CANDLES)  # уже FIXED к концу (точка 1=10, точка 2=30, диапазон [10,30])

    def test_exact_touch_of_border_is_not_exit(self):
        candles = self._fixed_prefix() + [_candle(8, high=30.0, low=25)]  # ровно граница
        result = run_local_grid_chain(_series(candles))
        self.assertEqual(len(result.completed), 0)
        self.assertEqual(result.current.state, LocalState.FIXED)

    def test_single_wick_breach_without_close_confirmation_stays_fixed(self):
        candles = self._fixed_prefix() + [
            _candle(8, high=32, low=27),   # тень пробивает верхнюю границу (30), закрытие 29.5 -- нет
            _candle(9, high=28, low=26),   # обратно внутри диапазона -- пендинг сброшен
        ]
        result = run_local_grid_chain(_series(candles))
        self.assertEqual(len(result.completed), 0)
        self.assertEqual(result.current.state, LocalState.FIXED)

    def test_two_candle_exit_through_upper_border_continues_same_direction(self):
        # Шаг 10: пробой ВЕРХНЕЙ границы (30, она же 0% восходящей сетки) --
        # продолжение, подтверждённое ВТОРОЙ свечой (закрытие 31 за 30).
        # Новая точка 1 = экстремум коррекции (минимальный low) внутри
        # диапазона [точка2+1 .. свеча_выхода) = дни 6-8, минимум -- день 7 (low=16).
        candles = self._fixed_prefix() + [
            _candle(8, high=32, low=27),   # свеча А: тень пробивает 30, закрытие 29.5 -- нет
            _candle(9, high=33, low=29),   # свеча Б: закрытие 31 за 30 -- ПОДТВЕРЖДЕНО
        ]
        result = run_local_grid_chain(_series(candles))
        self.assertEqual(len(result.completed), 1)
        finished = result.completed[0]
        self.assertEqual(finished.exit_border, "0%")
        self.assertEqual(finished.exit_date, _d(9))
        self.assertEqual(finished.exit_price, 31)

        nxt = result.current
        self.assertIsNotNone(nxt)
        self.assertEqual(nxt.seq, 2)
        self.assertEqual(nxt.direction, Direction.UP)   # то же направление
        self.assertEqual(nxt.point1.price, 16)          # экстремум коррекции (день 7), не 18/27
        self.assertEqual(nxt.point1.dt, _d(7))
        self.assertEqual(nxt.state, LocalState.FORMING)
        self.assertEqual(nxt.launch_date, _d(9))
        self.assertEqual(nxt.point2_preliminary.price, 33)

    def test_two_candle_exit_through_lower_border_reverses_direction(self):
        # Шаг 9, проверено на ВТОРОЙ локальной (не на первой): пробой ниже
        # точки 1 первой локальной (=10) совпал бы с обновлением глобального
        # ATL (точка 1 первой локальной = глобальная точка 2) и пересчитал
        # бы всю глобальную сетку -- честный, но отдельный edge case самого
        # алгоритма (глобальная сетка строится по ВСЕЙ серии), не то, что
        # проверяет этот тест. Поэтому: локальная №1 сначала продолжается
        # через верхнюю границу (дни 8-9, как в тесте continues_same_
        # direction выше) -> локальная №2 (точка 1=16, НЕ мировой рекорд) --
        # формируется (дни 10-11), фиксируется и затем пробивает точку 1
        # (16) вниз с двусвечным закреплением (дни 12-13) -- разворот.
        candles = self._fixed_prefix() + [
            _candle(8, high=32, low=27),   # продолжение локальной №1: свеча А
            _candle(9, high=33, low=29),   # продолжение локальной №1: свеча Б -> локальная №2 (точка1=16, точка2 предв=33)
            _candle(10, high=26, low=20),  # локальная №2, свеча А: тень пробивает 50%=24.5
            _candle(11, high=24, low=18),  # локальная №2, свеча Б: закрытие 21<=24.5 -- ПОДТВЕРЖДЕНО (диапазон [16,33])
            _candle(12, high=20, low=14),  # локальная №2, выход, свеча А: тень пробивает 16, закрытие 17 -- нет
            _candle(13, high=17, low=12),  # локальная №2, выход, свеча Б: закрытие 14.5 за 16 -- ПОДТВЕРЖДЕНО
        ]
        result = run_local_grid_chain(_series(candles))
        self.assertEqual(len(result.completed), 2)
        second = result.completed[1]
        self.assertEqual(second.seq, 2)
        self.assertEqual(second.point1.price, 16)
        self.assertEqual(second.point2_final.price, 33)
        self.assertEqual(second.exit_border, "100%")
        self.assertEqual(second.exit_date, _d(13))
        self.assertEqual(second.exit_price, 14.5)

        nxt = result.current
        self.assertEqual(nxt.seq, 3)
        self.assertEqual(nxt.direction, Direction.DOWN)   # развернулось
        self.assertEqual(nxt.point1.price, 33)            # старая точка 2 локальной №2
        self.assertEqual(nxt.point1.dt, _d(9))
        self.assertEqual(nxt.point2_preliminary.price, 12)  # low свечи-подтверждения (Б)


if __name__ == "__main__":
    unittest.main(verbosity=2)
