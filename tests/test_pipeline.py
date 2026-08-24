"""
Юнит-тесты на СИНТЕТИЧЕСКИХ, явно помеченных фикстурах -- проверяют, что
код агентов правильно реализует правила регламента. Это НЕ анализ рынка и
никогда не должно выдаваться пользователю как реальный график (регламент,
раздел 2) -- это только проверка логики, как обычный юнит-тест в любом
софте. Реальный прогон -- в orchestrator.py на данных IBM.

Запуск: python3 tests/test_pipeline.py (из корня fib-bot/)
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.data_agent import Candle, CandleSeries
from agents.fibo_agent import Direction, build_global_fibo, find_global_extremes
from agents.verification_agent import cross_check_structures, run_checklist


def _mk_series(highs_lows: list[tuple[float, float]], start: date = date(2026, 1, 1)) -> CandleSeries:
    """Синтетические свечи: список (high, low) пар, close = середина, open = close.
    Только для юнит-тестов логики, не выдаётся за реальные котировки."""
    candles = []
    for i, (h, l) in enumerate(highs_lows):
        mid = (h + l) / 2
        candles.append(
            Candle(dt=start + timedelta(days=i), open=mid, high=h, low=l, close=mid, volume=1000)
        )
    return CandleSeries(
        symbol="TEST",
        exchange_or_source="synthetic-test-fixture",
        timeframe="1D",
        candles=candles,
        fetched_via="synthetic",
        fetch_note="unit test fixture, not real market data",
    )


def test_ascending_point1_is_low_100_point2_is_high_0():
    # LOW раньше (день 2), HIGH позже (день 8) -> восходящее движение
    data = [(105, 100)] * 5 + [(102, 90)] + [(105, 100)] * 4 + [(120, 110)] + [(105, 100)] * 2
    series = _mk_series(data)
    s = build_global_fibo(series)
    assert s.direction == Direction.ASCENDING, s.direction
    assert s.point1.kind == "LOW", s.point1
    assert s.point2.kind == "HIGH", s.point2
    assert s.price_at(1.0) == s.point1.price == 90
    assert s.price_at(0.0) == s.point2.price == 120
    print("OK  test_ascending_point1_is_low_100_point2_is_high_0")


def test_descending_point1_is_high_100_point2_is_low_0():
    # HIGH раньше (день 2), LOW позже (день 8) -> нисходящее движение
    data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 4 + [(102, 90)] + [(105, 100)] * 2
    series = _mk_series(data)
    s = build_global_fibo(series)
    assert s.direction == Direction.DESCENDING, s.direction
    assert s.point1.kind == "HIGH", s.point1
    assert s.point2.kind == "LOW", s.point2
    assert s.price_at(1.0) == s.point1.price == 120
    assert s.price_at(0.0) == s.point2.price == 90
    print("OK  test_descending_point1_is_high_100_point2_is_low_0")


def test_confirmation_rule_rejects_extremum_too_close_to_left_edge():
    # LOW на 2-й свече -- слева всего 1 свеча, правило "минимум 5 слева" (раздел 8) должно отклонить
    data = [(105, 100), (102, 50)] + [(105, 100)] * 10
    series = _mk_series(data)
    try:
        find_global_extremes(series)
        raised = False
    except ValueError:
        raised = True
    assert raised, "Ожидали ValueError -- экстремум слишком близко к левому краю окна"
    print("OK  test_confirmation_rule_rejects_extremum_too_close_to_left_edge")


def test_wick_used_not_close():
    # Свеча с длинной верхней тенью (open=close=126, high=150) должна быть
    # взята по тени (150), а не по телу/close (126).
    data = (
        [(105, 102)] * 5       # idx 0-4: филлеры
        + [(150, 102)]         # idx 5: HIGH по тени = 150 (тело open=close=126)
        + [(105, 102)] * 5     # idx 6-10: филлеры
        + [(105, 60)]          # idx 11: уникальный LOW = 60
        + [(105, 102)] * 2     # idx 12-13: хвост
    )
    series = _mk_series(data)
    s = build_global_fibo(series)
    hi_point = s.point1 if s.point1.kind == "HIGH" else s.point2
    lo_point = s.point1 if s.point1.kind == "LOW" else s.point2
    assert hi_point.price == 150, f"Ожидали HIGH=150 (тень), получили {hi_point.price}"
    assert lo_point.price == 60, f"Ожидали LOW=60, получили {lo_point.price}"
    print("OK  test_wick_used_not_close")


def test_cross_check_agrees_on_identical_structure():
    data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 4 + [(102, 90)] + [(105, 100)] * 2
    series = _mk_series(data)
    s1 = build_global_fibo(series)
    s2 = build_global_fibo(series)
    result = cross_check_structures(s1, s2)
    assert result.agree, result.detail
    print("OK  test_cross_check_agrees_on_identical_structure")


def test_cross_check_flags_disagreement():
    asc_data = [(105, 100)] * 5 + [(102, 90)] + [(105, 100)] * 4 + [(120, 110)] + [(105, 100)] * 2
    desc_data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 4 + [(102, 90)] + [(105, 100)] * 2
    s1 = build_global_fibo(_mk_series(asc_data))
    s2 = build_global_fibo(_mk_series(desc_data))
    result = cross_check_structures(s1, s2)
    assert not result.agree, "Ожидали расхождение (разное направление), но агент согласился"
    print("OK  test_cross_check_flags_disagreement -- " + result.detail)


def test_checklist_catches_mixed_source_tag():
    data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 4 + [(102, 90)] + [(105, 100)] * 2
    s = build_global_fibo(_mk_series(data))
    report = run_checklist(s, source_tag="Binance Spot, TradingView NASDAQ")  # намеренно смешанный тег
    names_failed = [r.name for r in report.failed()]
    assert "источник не смешан (один тег на всю структуру)" in names_failed, report.results
    print("OK  test_checklist_catches_mixed_source_tag")


def test_checklist_passes_clean_structure():
    data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 4 + [(102, 90)] + [(105, 100)] * 2
    s = build_global_fibo(_mk_series(data))
    report = run_checklist(s, source_tag="NASDAQ Stock")
    assert report.all_passed, report.failed()
    print("OK  test_checklist_passes_clean_structure")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
    print()
    print(f"{len(tests) - failures}/{len(tests)} тестов прошли")
    if failures:
        sys.exit(1)
