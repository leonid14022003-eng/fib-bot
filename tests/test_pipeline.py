"""
Юнит-тесты на СИНТЕТИЧЕСКИХ, явно помеченных фикстурах -- проверяют, что
код агентов правильно реализует правила регламента. Это НЕ анализ рынка и
никогда не должно выдаваться пользователю как реальный график (регламент,
раздел 2) -- это только проверка логики, как обычный юнит-тест в любом
софте. Реальный прогон -- в orchestrator.py на данных IBM.

Запуск: python3 tests/test_pipeline.py (из корня fib-bot/)
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.analyst_agent import (
    CALL_BUY,
    CALL_INVALIDATED,
    CALL_NO_ANALYSIS,
    CALL_SELL,
    CALL_WAIT,
    CONFIDENCE_CERTAIN,
    CONFIDENCE_GUESS,
    CONFIDENCE_LIKELY,
    build_verdict,
    format_verdict,
)
from agents.chart_agent import LEVEL_COLORS, render_chart
from agents.context_agent import MacroEvent, build_context_note, filter_tracked_events, get_upcoming_macro_events
from agents.data_agent import Candle, CandleSeries, load_ibm_demo_daily
from agents.dispatch_agent import (
    AnalysisBundle,
    LevelWatchState,
    Recipient,
    _depth_bar,
    _strip_html,
    format_message,
    send_photo_via_telegram,
    should_send_level_watch,
)
from agents.intraday_agent import IntradayConfirmation
from agents.ops_agent import LOG_TAIL_LIMIT, notify_failure
from analyst_report import TELEGRAM_TEXT_LIMIT, build_digest_messages
from opportunity_scanner import build_opportunity_messages, find_opportunities
from universe import load_sp500_universe
from agents.fibo_agent import (
    Direction,
    FiboLevel,
    FiboStructure,
    StructureScope,
    SwingPoint,
    build_global_fibo,
    find_fractal_swing_extremes,
    find_global_extremes,
)
from agents.price_behavior_agent import NearestLevelInfo, RecentEvent, recent_level_events
from agents.verification_agent import (
    CheckResult,
    ChecklistReport,
    cross_check_structures,
    format_consensus_note,
    run_checklist,
)
from backtest import (
    MAX_HOLD_DAYS,
    REWARD_CAP_R,
    BacktestSignal,
    ScoredOutcome,
    _score_outcome,
    simulate_symbol,
    summarize,
)
from screener import Instrument, scan_instrument


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


# --- should_send_level_watch (24 августа: "не слать постоянно, только когда
# коррекция реально дошла до значимого уровня, например 0.618") ---------


def _mk_bundle(
    fraction: float,
    point1_dt: date = date(2026, 1, 10),
    point2_dt: date = date(2026, 1, 20),
    checklist_ok: bool = True,
) -> AnalysisBundle:
    """Синтетический AnalysisBundle с точным откатом `fraction` от точки 2
    (0%) к точке 1 (100%) -- удобно для проверки should_send_level_watch()
    без необходимости подбирать свечи под конкретный уровень."""
    point1_price, point2_price = 200.0, 100.0
    point1 = SwingPoint(dt=point1_dt, price=point1_price, kind="HIGH", index=0)
    point2 = SwingPoint(dt=point2_dt, price=point2_price, kind="LOW", index=10)
    levels = [
        # Включая расширения (1.414 и далее) -- как и у настоящих структур
        # из _build_structure() в fibo_agent.py (они считаются всегда, не
        # только когда relevantны) -- нужно для test_format_message_shows_extension_levels_*.
        FiboLevel(level=lv, price=point2_price + lv * (point1_price - point2_price))
        for lv in [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.414, 1.618, 2.0, 2.414, 2.618]
    ]
    structure = FiboStructure(
        scope=StructureScope.GLOBAL,
        direction=Direction.DESCENDING,
        point1=point1,
        point2=point2,
        levels=levels,
    )
    current_price = point2_price + fraction * (point1_price - point2_price)
    nearest = NearestLevelInfo(
        current_price=current_price,
        below_level=None,
        below_price=None,
        above_level=None,
        above_price=None,
        nearest_level=0.0,
        nearest_price=current_price,
        is_testing=False,
    )
    checklist = ChecklistReport(results=[CheckResult("dummy", checklist_ok, "-")])
    return AnalysisBundle(
        symbol="TEST",
        source_tag="synthetic-test-fixture",
        timeframe="1D",
        period_desc="test",
        structure=structure,
        nearest=nearest,
        recent_events=[],
        checklist=checklist,
    )


def test_level_watch_silent_below_threshold():
    bundle = _mk_bundle(fraction=0.30)  # между 0.236 и 0.382 -- ровно то, что видели в реальных прогонах
    ok, reason, state = should_send_level_watch(bundle, LevelWatchState())
    assert not ok, reason
    print("OK  test_level_watch_silent_below_threshold -- " + reason)


def test_level_watch_fires_at_deep_correction():
    bundle = _mk_bundle(fraction=0.62)  # чуть глубже 0.618
    ok, reason, state = should_send_level_watch(bundle, LevelWatchState())
    assert ok, reason
    assert state.last_alerted_level == 0.618, state
    print("OK  test_level_watch_fires_at_deep_correction -- " + reason)


def test_level_watch_no_repeat_for_same_level():
    bundle = _mk_bundle(fraction=0.62)
    ok1, _, state_after_first = should_send_level_watch(bundle, LevelWatchState())
    assert ok1
    ok2, reason2, state_after_second = should_send_level_watch(bundle, state_after_first)
    assert not ok2, "Второй прогон на том же уровне той же структуры не должен слать повторно"
    print("OK  test_level_watch_no_repeat_for_same_level -- " + reason2)


def test_level_watch_fires_again_on_deeper_level():
    bundle_618 = _mk_bundle(fraction=0.62)
    _, _, state_after_618 = should_send_level_watch(bundle_618, LevelWatchState())
    bundle_786 = _mk_bundle(fraction=0.80)  # чуть глубже 0.786, та же структура (те же даты точек)
    ok, reason, state_after_786 = should_send_level_watch(bundle_786, state_after_618)
    assert ok, reason
    assert state_after_786.last_alerted_level == 0.786, state_after_786
    print("OK  test_level_watch_fires_again_on_deeper_level -- " + reason)


def test_level_watch_resets_on_new_structure():
    bundle_old = _mk_bundle(fraction=0.62, point1_dt=date(2026, 1, 10), point2_dt=date(2026, 1, 20))
    _, _, state_after_old = should_send_level_watch(bundle_old, LevelWatchState())
    # Новый свинг -- другие даты точек -- даже на том же уровне 0.618 должен снова сработать
    bundle_new = _mk_bundle(fraction=0.62, point1_dt=date(2026, 3, 1), point2_dt=date(2026, 3, 15))
    ok, reason, state_after_new = should_send_level_watch(bundle_new, state_after_old)
    assert ok, "Новая структура (новый свинг) должна сбрасывать память об уже отправленных уровнях"
    print("OK  test_level_watch_resets_on_new_structure -- " + reason)


def test_level_watch_blocked_by_failed_checklist():
    bundle = _mk_bundle(fraction=0.90, checklist_ok=False)  # глубокая коррекция, но чек-лист не прошёл
    ok, reason, state = should_send_level_watch(bundle, LevelWatchState())
    assert not ok, "Провал verification-чек-листа должен блокировать отправку независимо от глубины"
    print("OK  test_level_watch_blocked_by_failed_checklist -- " + reason)


# --- Визуальный редизайн format_message() (25 августа: "чтобы это не
# выглядело просто как слова и цифры, а чтобы глазу было приятно") --------


def test_depth_bar_scales_and_clamps():
    assert _depth_bar(0.0, width=10) == "▱" * 10
    assert _depth_bar(1.0, width=10) == "▰" * 10
    assert _depth_bar(0.5, width=10) == "▰" * 5 + "▱" * 5
    # значения за пределами [0,1] (цена ушла за исходный диапазон) не должны
    # падать -- полоска просто зажимается визуально по краю
    assert _depth_bar(-0.2, width=10) == "▱" * 10
    assert _depth_bar(1.3, width=10) == "▰" * 10
    print("OK  test_depth_bar_scales_and_clamps")


def test_strip_html_fallback_removes_tags():
    raw = "🔔 <b>META</b> — тест\n<pre>0.618 = 585.68</pre>"
    plain = _strip_html(raw)
    assert "<" not in plain and ">" not in plain
    assert "META" in plain and "585.68" in plain
    print("OK  test_strip_html_fallback_removes_tags")


def test_format_message_html_tags_balanced():
    # Несбалансированный/неэкранированный тег -- это не просто некрасиво, а
    # реальный риск: Telegram с parse_mode="HTML" целиком откажется
    # парсить такое сообщение (ошибка 400), и алерт не дойдёт вообще, пока
    # send_via_telegram не сработает через fallback на обычный текст.
    bundle = _mk_bundle(fraction=0.65)
    for alert_level, name in [(None, None), (0.618, "Тестовый инструмент")]:
        msg = format_message(bundle, alert_level=alert_level, display_name=name)
        for tag in ("b", "i", "pre"):
            assert msg.count(f"<{tag}>") == msg.count(f"</{tag}>"), (
                f"Несбалансированные теги <{tag}> (alert_level={alert_level}, name={name})"
            )
    print("OK  test_format_message_html_tags_balanced")


def test_format_message_keeps_required_fields_section_24():
    # Раздел 24 регламента требует: инструмент, источник, ТФ, период, тип
    # ФИБО, структуру, точку 1, точку 2, уровни, текущую цену, положение --
    # визуальный редизайн не должен потерять ни одного из них, только
    # изменить оформление.
    bundle = _mk_bundle(fraction=0.65)
    msg = format_message(bundle)
    assert "TEST" in msg  # инструмент (symbol)
    assert "synthetic-test-fixture" in msg  # источник
    assert "1D" in msg  # таймфрейм
    assert "test" in msg  # период
    assert bundle.structure.direction.value in msg  # тип ФИБО
    assert bundle.structure.scope.value in msg  # структура
    assert f"{bundle.structure.point1.price:.2f}" in msg  # точка 1
    assert f"{bundle.structure.point2.price:.2f}" in msg  # точка 2
    assert "0.618" in msg  # один из основных уровней
    assert f"{bundle.nearest.current_price:.2f}" in msg  # текущая цена
    print("OK  test_format_message_keeps_required_fields_section_24")


def test_format_message_display_name_shown_alongside_ticker():
    bundle = _mk_bundle(fraction=0.65)
    msg = format_message(bundle, display_name="Тестовый инструмент")
    assert "Тестовый инструмент" in msg
    assert "TEST" in msg  # тикер по-прежнему на месте, не подменён именем
    print("OK  test_format_message_display_name_shown_alongside_ticker")


# --- screener.scan_instrument (24 августа: скринер по списку инструментов,
# Задача №1 из brief.md) -- один плохой тикер не должен обрывать весь список ---


def test_scan_instrument_handles_data_fetch_error():
    def _fake_fetch_raises(symbol, exchange_hint="test"):
        raise ValueError("тикер не найден (тестовая ошибка)")

    instrument = Instrument("Тестовый актив", "BADSYM", "test")
    result = scan_instrument(instrument, fetch_fn=_fake_fetch_raises)
    assert result["status"] == "DATA_ERROR", result
    print("OK  test_scan_instrument_handles_data_fetch_error -- " + result["detail"])


def test_scan_instrument_handles_unconfirmed_structure():
    def _fake_fetch_no_structure(symbol, exchange_hint="test"):
        # LOW на 2-й свече -- слева всего 1 свеча, правило "минимум 5 слева" отклонит
        data = [(105, 100), (102, 50)] + [(105, 100)] * 10
        return _mk_series(data)

    instrument = Instrument("Тестовый актив", "TEST", "test")
    result = scan_instrument(instrument, fetch_fn=_fake_fetch_no_structure)
    assert result["status"] == "NO_STRUCTURE", result
    print("OK  test_scan_instrument_handles_unconfirmed_structure -- " + result["detail"])


def test_scan_instrument_returns_ok_with_correct_fraction():
    def _fake_fetch_ok(symbol, exchange_hint="test"):
        # HIGH=120 (idx5), LOW=80 (idx10) -- нисходящая структура; последняя
        # свеча (108,104) -> close=106 -> откат (106-80)/(120-80) = 0.65
        data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 4 + [(90, 80)] + [(105, 100)] + [(108, 104)]
        return _mk_series(data)

    instrument = Instrument("Тестовый актив", "TEST", "test")
    result = scan_instrument(instrument, fetch_fn=_fake_fetch_ok)
    assert result["status"] == "OK", result
    assert abs(result["fraction"] - 0.65) < 1e-9, result["fraction"]
    assert result["bundle"].checklist.all_passed, result["bundle"].checklist.failed()
    print(f"OK  test_scan_instrument_returns_ok_with_correct_fraction -- fraction={result['fraction']:.3f}")


def test_scan_instrument_includes_candles_for_charting():
    # 26 августа: скринеру нужны сырые свечи (не только bundle) для картинки
    # по кандидату -- render_chart() их не берёт из AnalysisBundle (он их не
    # хранит), см. agents/chart_agent.py.
    def _fake_fetch_ok(symbol, exchange_hint="test"):
        data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 4 + [(90, 80)] + [(105, 100)] + [(108, 104)]
        return _mk_series(data)

    instrument = Instrument("Тестовый актив", "TEST", "test")
    result = scan_instrument(instrument, fetch_fn=_fake_fetch_ok)
    assert result["status"] == "OK", result
    assert "candles" in result and len(result["candles"]) > 0, result
    assert all(isinstance(c, Candle) for c in result["candles"])
    print(f"OK  test_scan_instrument_includes_candles_for_charting -- {len(result['candles'])} свечей")


# --- Графики (chart_agent.py, 26 августа: "давай задний фон изменим с
# белого на чёрный... сделаем так чтобы бот присылал графики") -----------


_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _mk_structure_with_candles() -> tuple[FiboStructure, list[Candle]]:
    data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 4 + [(102, 90)] + [(105, 100)] * 2
    series = _mk_series(data)
    return build_global_fibo(series), series.candles


def test_render_chart_produces_valid_png():
    structure, candles = _mk_structure_with_candles()
    png = render_chart(structure, candles, current_price=candles[-1].close, symbol="TEST", timeframe="1D")
    assert png[:8] == _PNG_MAGIC, "Результат не похож на валидный PNG (нет сигнатуры)"
    assert len(png) > 1000, f"Подозрительно маленький PNG ({len(png)} байт) -- скорее всего пустой график"
    print(f"OK  test_render_chart_produces_valid_png -- {len(png)} байт")


def test_render_chart_neutral_and_alert_variants_both_render():
    structure, candles = _mk_structure_with_candles()
    price = candles[-1].close
    png_neutral = render_chart(structure, candles, price, "TEST", "1D")
    png_alert = render_chart(structure, candles, price, "TEST", "1D", alert_level=0.618)
    assert png_neutral[:8] == _PNG_MAGIC and png_alert[:8] == _PNG_MAGIC
    print("OK  test_render_chart_neutral_and_alert_variants_both_render")


def test_render_chart_accepts_display_name():
    # Скринеру нужно show и тикер, и человеко-читаемое имя (как у format_message) --
    # проверяем, что render_chart не падает, когда имя отличается от тикера.
    structure, candles = _mk_structure_with_candles()
    png = render_chart(
        structure, candles, candles[-1].close, symbol="META", timeframe="1D", display_name="Meta"
    )
    assert png[:8] == _PNG_MAGIC
    print("OK  test_render_chart_accepts_display_name")


def test_level_colors_cover_all_standard_levels():
    # Если кто-то отредактирует LEVEL_COLORS и забудет уровень -- он молча
    # съедет на нейтральный серый (.get(lv.level, MUTED) в render_chart) --
    # без этого теста такая ошибка не была бы заметна на глаз.
    for lv in [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]:
        assert lv in LEVEL_COLORS, f"Нет цвета для уровня {lv} в LEVEL_COLORS"
    print("OK  test_level_colors_cover_all_standard_levels")


def test_level_0786_color_is_green_per_reglament_section_16():
    # Раздел 16 регламента прямо называет цвет только для одного уровня --
    # 0.786 = зелёный. Это единственный цвет в LEVEL_COLORS, который НЕ
    # выбор Claude и не должен меняться без прямого решения по регламенту.
    assert LEVEL_COLORS[0.786] == "#008300", (
        "0.786 должен оставаться зелёным (раздел 16 регламента), это не часть рабочей схемы Claude"
    )
    print("OK  test_level_0786_color_is_green_per_reglament_section_16")


def test_send_photo_via_telegram_dry_run_reports_skipped():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20  # валидная сигнатура, содержимое не важно для dry-run
    recipients = [Recipient(label="Тест", telegram_chat_id="123")]
    result = send_photo_via_telegram(png, recipients, bot_token=None, caption="тест")
    assert result["dry_run"] is True
    assert result["photo_bytes"] == len(png)
    assert result["sent_to"][0]["status"].startswith("SKIPPED")
    print("OK  test_send_photo_via_telegram_dry_run_reports_skipped")


def _fmp_calendar_fixture() -> list[dict]:
    """
    Форма (имена и типы полей) повторяет реальный ответ FMP
    /stable/economic-calendar, полученный Леонидом на сервере 26 августа
    прямым curl'ом -- сами значения для теста, но характерные примеры
    настоящие: "Atlanta Fed GDPNow" (заведомый шум -- не плановый релиз, а
    постоянно обновляемая модель) и "GDP Price Index QoQ" (реальное
    совпадение) оба были в том самом ответе.
    """
    return [
        {
            "date": "2026-08-26 12:30:00", "country": "US", "event": "GDP Price Index QoQ (Q2)",
            "currency": "USD", "previous": 3.6, "estimate": 6.3, "actual": 6.4, "change": 2.8,
            "impact": "Medium", "changePercentage": 0, "unit": "%",
        },
        {
            "date": "2026-08-26 14:10:00", "country": "US", "event": "Atlanta Fed GDPNow (Q3)",
            "currency": "USD", "previous": None, "estimate": 3.6, "actual": 3.7, "change": 0,
            "impact": "Medium", "changePercentage": 0, "unit": "%",
        },
        {
            "date": "2026-08-26 15:45:00", "country": "US", "event": "Fed Barkin Speech",
            "currency": "USD", "previous": None, "estimate": None, "actual": None, "change": None,
            "impact": "Medium", "changePercentage": 0, "unit": None,
        },
        {
            "date": "2026-08-27 01:30:00", "country": "AU", "event": "Median CPI YoY (Jul)",
            "currency": "AUD", "previous": 3.6, "estimate": 3.6, "actual": 3.6, "change": 0,
            "impact": "Low", "changePercentage": 0, "unit": "%",
        },
        {
            "date": "2026-08-30 12:30:00", "country": "US", "event": "Core CPI YoY (Aug)",
            "currency": "USD", "previous": 3.0, "estimate": 3.0, "actual": None, "change": None,
            "impact": "High", "changePercentage": 0, "unit": "%",
        },
    ]


def test_filter_tracked_events_matches_gdp_and_excludes_gdpnow_and_speeches():
    raw = _fmp_calendar_fixture()
    now = datetime(2026, 8, 26, 10, 0, 0)
    events = filter_tracked_events(raw, now, lookahead_hours=24)
    names = [e.name for e in events]
    assert "GDP Price Index QoQ (Q2)" in names, "Реальный релиз ВВП должен отслеживаться"
    assert "Atlanta Fed GDPNow (Q3)" not in names, "GDPNow -- модель-нонкаст, не плановый релиз, должна отсеиваться"
    assert "Fed Barkin Speech" not in names, "Выступление не входит в список ключевых слов (не ставка/CPI/NFP/ВВП)"
    print("OK  test_filter_tracked_events_matches_gdp_and_excludes_gdpnow_and_speeches")


def test_filter_tracked_events_excludes_other_country_even_if_name_matches():
    raw = _fmp_calendar_fixture()
    now = datetime(2026, 8, 26, 10, 0, 0)
    events = filter_tracked_events(raw, now, lookahead_hours=48)
    names = [e.name for e in events]
    assert "Median CPI YoY (Jul)" not in names, "Событие Австралии не должно попадать -- фильтр по стране US"
    print("OK  test_filter_tracked_events_excludes_other_country_even_if_name_matches")


def test_filter_tracked_events_respects_lookahead_window():
    raw = _fmp_calendar_fixture()
    # "Core CPI YoY (Aug)" -- 2026-08-30 12:30:00, ровно 98.5ч после now
    # (26 августа 10:00) -- окно в 100ч специально взято чуть выше этого,
    # а не круглым числом, чтобы граница теста была осмысленной, а не
    # угаданной на глаз.
    now = datetime(2026, 8, 26, 10, 0, 0)
    events_short = filter_tracked_events(raw, now, lookahead_hours=1)
    events_long = filter_tracked_events(raw, now, lookahead_hours=100)
    assert not any(e.name.startswith("Core CPI") for e in events_short), "Событие 30 августа не должно попасть в окно 1 час"
    assert any(e.name.startswith("Core CPI") for e in events_long), "Событие 30 августа (98.5ч вперёд) должно попасть в окно 100ч"
    print("OK  test_filter_tracked_events_respects_lookahead_window")


def test_filter_tracked_events_includes_opec_plus_meeting_in_window():
    now = datetime(2026, 9, 1, 10, 0, 0)
    meetings = [date(2026, 9, 2)]
    events = filter_tracked_events([], now, lookahead_hours=48, opec_plus_meetings=meetings)
    assert len(events) == 1 and events[0].source == "OPEC+ (вручную)", "Встреча ОПЕК+ внутри окна должна попасть в результат"
    print("OK  test_filter_tracked_events_includes_opec_plus_meeting_in_window")


def test_build_context_note_returns_none_when_no_events():
    assert build_context_note([]) is None
    print("OK  test_build_context_note_returns_none_when_no_events")


def test_build_context_note_formats_single_and_multiple_events():
    e1 = MacroEvent(name="CPI YoY (Aug)", dt=datetime(2026, 8, 27, 12, 30), country="US", source="FMP calendar")
    e2 = MacroEvent(name="Nonfarm Payrolls (Aug)", dt=datetime(2026, 8, 28, 12, 30), country="US", source="FMP calendar")
    single = build_context_note([e1])
    multi = build_context_note([e1, e2])
    assert single is not None and "CPI YoY (Aug)" in single and "27.08 12:30" in single
    assert multi is not None and "CPI YoY (Aug)" in multi and "Nonfarm Payrolls (Aug)" in multi
    print("OK  test_build_context_note_formats_single_and_multiple_events")


def test_get_upcoming_macro_events_requires_api_key():
    # Без ключа (ни аргументом, ни в окружении) агент должен честно упасть,
    # а не молча продолжить без данных (регламент, раздел 2).
    saved = os.environ.pop("MARKET_DATA_API_KEY", None)
    try:
        raised = False
        try:
            get_upcoming_macro_events(datetime.now(), api_key=None)
        except ValueError:
            raised = True
        assert raised, "Без ключа get_upcoming_macro_events должен бросать ValueError"
    finally:
        if saved is not None:
            os.environ["MARKET_DATA_API_KEY"] = saved
    print("OK  test_get_upcoming_macro_events_requires_api_key")


# --- find_fractal_swing_extremes (27 августа: "настоящая сверка структур" --
# независимый метод поиска точек разворота, для реальной, а не самой-с-собой,
# сверки в verification_agent.py / orchestrator.py / screener.py) -----------


def test_find_fractal_swing_extremes_agrees_with_global_on_clean_data():
    # HIGH на idx5 и LOW на idx11, оба подтверждены минимум 5 барами и
    # СЛЕВА, и СПРАВА -- оба метода должны найти одни и те же точки.
    data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 5 + [(102, 90)] + [(105, 100)] * 5
    series = _mk_series(data)
    naive = build_global_fibo(series)
    fractal = build_global_fibo(series, extremes_fn=find_fractal_swing_extremes)
    consensus = cross_check_structures(naive, fractal)
    assert consensus.agree, f"Ожидали согласие на чистых данных, получили: {consensus.detail}"
    print("OK  test_find_fractal_swing_extremes_agrees_with_global_on_clean_data")


def test_find_fractal_swing_extremes_disagrees_on_unconfirmed_recent_spike():
    # Ранний HIGH (idx5=115) подтверждён с обеих сторон. Поздний LOW
    # (idx11=90) тоже подтверждён с обеих сторон. Но последняя свеча
    # (idx17=130) -- новый буквальный глобальный максимум -- у неё НЕТ ни
    # одного бара справа, фрактальный метод не может её подтвердить.
    # find_global_extremes() (нужны только бары слева) возьмёт 130 как
    # HIGH -> LOW раньше HIGH по хронологии -> ВОСХОДЯЩЕЕ движение.
    # find_fractal_swing_extremes() возьмёт 115 (idx5) как единственный
    # подтверждённый HIGH -> HIGH раньше LOW -> НИСХОДЯЩЕЕ движение.
    # Разные направления -- ровно то расхождение, которое и должна ловить
    # независимая сверка (свежий, ещё не подтверждённый выброс).
    data = (
        [(105, 100)] * 5
        + [(115, 108)]
        + [(105, 100)] * 5
        + [(102, 90)]
        + [(105, 100)] * 5
        + [(130, 125)]
    )
    series = _mk_series(data)
    naive = build_global_fibo(series)
    fractal = build_global_fibo(series, extremes_fn=find_fractal_swing_extremes)
    assert naive.direction == Direction.ASCENDING, naive.direction
    assert fractal.direction == Direction.DESCENDING, fractal.direction
    consensus = cross_check_structures(naive, fractal)
    assert not consensus.agree, "Ожидали расхождение (разное направление), но методы совпали"
    print("OK  test_find_fractal_swing_extremes_disagrees_on_unconfirmed_recent_spike "
          f"-- {consensus.detail}")


def test_find_fractal_swing_extremes_raises_when_no_confirmed_point():
    # Всего 5 свечей -- ни одна не может получить подтверждение с обеих
    # сторон при window=5 (нужно минимум 5+1+5=11 свечей).
    series = _mk_series([(105, 100)] * 5)
    raised = False
    try:
        find_fractal_swing_extremes(series)
    except ValueError:
        raised = True
    assert raised, "Ожидали ValueError -- слишком мало свечей для фрактального подтверждения"
    print("OK  test_find_fractal_swing_extremes_raises_when_no_confirmed_point")


def test_build_global_fibo_accepts_alternative_extremes_fn():
    # Проверка самого механизма внедрения (extremes_fn) -- не только что
    # find_fractal_swing_extremes() существует, а что build_global_fibo()
    # реально его использует, когда передан явно.
    data = [(105, 100)] * 5 + [(120, 110)] + [(105, 100)] * 5 + [(102, 90)] + [(105, 100)] * 5
    series = _mk_series(data)
    default_structure = build_global_fibo(series)
    explicit_structure = build_global_fibo(series, extremes_fn=find_global_extremes)
    assert default_structure.point1 == explicit_structure.point1
    assert default_structure.point2 == explicit_structure.point2
    print("OK  test_build_global_fibo_accepts_alternative_extremes_fn")


# --- recent_level_events: настоящие пробои и ретест (27 августа, по запросу
# Леонида -- "ловить реальные пробои", не только тест/ложный пробой) -------


def _mk_level_150_structure() -> FiboStructure:
    """point2=100 (0%), point1=200 (100%) -- уровень 0.5 ровно на 150,
    удобное круглое число для проверки пересечений close-to-close."""
    point1 = SwingPoint(dt=date(2026, 1, 1), price=200.0, kind="HIGH", index=0)
    point2 = SwingPoint(dt=date(2026, 2, 1), price=100.0, kind="LOW", index=20)
    levels = [FiboLevel(level=lv, price=100.0 + lv * 100.0) for lv in [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]]
    return FiboStructure(scope=StructureScope.GLOBAL, direction=Direction.DESCENDING, point1=point1, point2=point2, levels=levels)


def _mk_candles(closes: list[float], start: date = date(2026, 3, 1)) -> list[Candle]:
    """Свечи с заданными close (open=close, high/low чуть шире -- для этих
    тестов важны только close-to-close пересечения уровня 150, не тени)."""
    return [
        Candle(dt=start + timedelta(days=i), open=c, high=c + 2, low=c - 2, close=c, volume=1000)
        for i, c in enumerate(closes)
    ]


def test_recent_level_events_detects_confirmed_breakout():
    structure = _mk_level_150_structure()
    # 140 (ниже) -> 155 -> 160 -> 158: пересекли вверх и ни разу не
    # вернулись ниже 150 до конца окна -- подтверждённый пробой.
    candles = _mk_candles([140, 155, 160, 158])
    events = recent_level_events(structure, candles, lookback=10)
    breakouts = [e for e in events if e.kind == "пробой (закрепление подтверждено)" and e.level == 0.5]
    assert len(breakouts) == 1, f"Ожидали 1 подтверждённый пробой уровня 0.5, получили {len(breakouts)}: {events}"
    print("OK  test_recent_level_events_detects_confirmed_breakout")


def test_recent_level_events_detects_retest_after_breakout():
    # 140 -> 160 (пробой вверх) -> 170 -> 150.3 (ретест, чуть выше уровня,
    # не пересекая обратно) -> 165.
    structure = _mk_level_150_structure()
    candles = _mk_candles([140, 160, 170, 150.3, 165])
    events = recent_level_events(structure, candles, lookback=10)
    breakouts = [e for e in events if e.kind == "пробой (закрепление подтверждено)" and e.level == 0.5]
    retests = [e for e in events if e.kind == "ретест" and e.level == 0.5]
    assert len(breakouts) == 1, f"Ожидали 1 подтверждённый пробой, получили: {events}"
    assert len(retests) == 1, f"Ожидали 1 ретест после пробоя, получили: {events}"
    print("OK  test_recent_level_events_detects_retest_after_breakout")


def test_recent_level_events_false_breakout_both_directions():
    structure = _mk_level_150_structure()
    # Направление "вверх", потом откат обратно ниже -- ложный пробой.
    up_then_revert = _mk_candles([140, 160, 145])
    up_events = recent_level_events(structure, up_then_revert, lookback=10)
    up_false = [e for e in up_events if e.kind == "ложный пробой (вернулись выше после закрытия ниже)"]
    up_confirmed = [e for e in up_events if e.kind == "пробой (закрепление подтверждено)"]
    assert len(up_false) == 1, f"Ожидали 1 ложный пробой (вверх-вниз), получили: {up_events}"
    assert not up_confirmed, f"Не ожидали подтверждённый пробой в этом окне: {up_events}"

    # Направление "вниз", потом откат обратно выше -- ложный пробой,
    # симметричный случай, которого не было ДО 27 августа.
    down_then_revert = _mk_candles([160, 140, 155])
    down_events = recent_level_events(structure, down_then_revert, lookback=10)
    down_false = [e for e in down_events if e.kind == "ложный пробой (вернулись ниже после закрытия выше)"]
    down_confirmed = [e for e in down_events if e.kind == "пробой (закрепление подтверждено)"]
    assert len(down_false) == 1, f"Ожидали 1 ложный пробой (вниз-вверх), получили: {down_events}"
    assert not down_confirmed, f"Не ожидали подтверждённый пробой в этом окне: {down_events}"
    print("OK  test_recent_level_events_false_breakout_both_directions")


def test_recent_level_events_test_kind_unchanged():
    # Регрессия: внутрисвечная тень пересекла уровень, закрытие вернулось
    # выше -- "тест", условие то же самое, что было до 27 августа.
    structure = _mk_level_150_structure()
    candles = [
        Candle(dt=date(2026, 3, 1), open=155, high=156, low=154, close=155, volume=1000),
        Candle(dt=date(2026, 3, 2), open=155, high=157, low=148, close=153, volume=1000),  # тень ниже 150, close выше
    ]
    events = recent_level_events(structure, candles, lookback=10)
    tests = [e for e in events if e.kind == "тест" and e.level == 0.5]
    assert len(tests) == 1, f"Ожидали 1 тест уровня 0.5, получили: {events}"
    print("OK  test_recent_level_events_test_kind_unchanged")


# --- Уровни-расширения в сообщении и на графике (27 августа, по запросу
# Леонида -- "уровни-расширения на сообщении") ------------------------------


def test_format_message_shows_extension_levels_when_price_beyond_range():
    # fraction=1.2 -- цена за пределами 100% (point1), расширения должны
    # появиться в таблице уровней (хотя бы 1.414, следующая цель впереди).
    bundle = _mk_bundle(fraction=1.2)
    msg = format_message(bundle)
    assert "1.414" in msg, f"Ожидали уровень-расширение 1.414 в сообщении при fraction=1.2:\n{msg}"
    print("OK  test_format_message_shows_extension_levels_when_price_beyond_range")


def test_format_message_hides_extension_levels_within_normal_range():
    # Регрессия: в обычном диапазоне (0..1) расширения по-прежнему не
    # должны загромождать таблицу уровней.
    bundle = _mk_bundle(fraction=0.65)
    msg = format_message(bundle)
    assert "1.414" not in msg, f"Не ожидали уровень-расширение в сообщении при fraction=0.65:\n{msg}"
    print("OK  test_format_message_hides_extension_levels_within_normal_range")


def test_render_chart_handles_extension_levels_without_crashing():
    # Смоук-тест: current_price за пределами point1 -- render_chart должен
    # нарисовать расширения и вернуть валидный PNG, не упасть.
    series = load_ibm_demo_daily(Path(__file__).resolve().parent.parent / "data" / "ibm_daily_raw.txt")
    structure = build_global_fibo(series)
    beyond_price = structure.point1.price + 0.2 * (structure.point1.price - structure.point2.price)
    png = render_chart(structure, series.candles, beyond_price, series.symbol, series.timeframe)
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "Результат не похож на валидный PNG"
    assert len(png) > 1000
    print(f"OK  test_render_chart_handles_extension_levels_without_crashing -- {len(png)} байт")


# --- Ориентир инвалидации и пометка сверки в самом сообщении (27 августа,
# по отзыву "брокера": "нет уровня инвалидации" / "расхождение сверки не
# видно получателю алерта, только в консоли сервера") -----------------------


def test_format_message_shows_invalidation_price():
    bundle = _mk_bundle(fraction=0.65)
    msg = format_message(bundle)
    inval_lines = [line for line in msg.split("\n") if "Ориентир инвалидации" in line]
    assert len(inval_lines) == 1, f"Ожидали ровно одну строку с ориентиром инвалидации:\n{msg}"
    assert f"{bundle.structure.point1.price:.2f}" in inval_lines[0], inval_lines[0]
    print("OK  test_format_message_shows_invalidation_price")


def test_format_message_includes_consensus_note_when_provided():
    bundle = _mk_bundle(fraction=0.65)
    note = "🔍 Независимая сверка структуры: точки подтверждены вторым (фрактальным) методом."
    msg_with = format_message(bundle, consensus_note=note)
    msg_without = format_message(bundle)
    assert note in msg_with, msg_with
    assert "Независимая сверка" not in msg_without, msg_without
    print("OK  test_format_message_includes_consensus_note_when_provided")


def test_format_consensus_note_all_three_states():
    agree_msg = format_consensus_note(True, "не используется в этой ветке")
    disagree_msg = format_consensus_note(False, "Разное направление: восходящий vs нисходящий")
    unavailable_msg = format_consensus_note(None, "слишком мало данных")
    assert "подтверждены" in agree_msg, agree_msg
    assert "РАСХОЖДЕНИЕ" in disagree_msg and "Разное направление" in disagree_msg, disagree_msg
    assert "недоступна" in unavailable_msg and "слишком мало данных" in unavailable_msg, unavailable_msg
    print("OK  test_format_consensus_note_all_three_states")


# --- Учёт объёма при пробое (27 августа, по отзыву "брокера": "нет объёма
# нигде в логике... пробой на низком объёме и на всплеске -- это два разных
# события с разной надёжностью") -------------------------------------------


def _mk_candles_with_volume(closes: list[float], volumes: list[int], start: date = date(2026, 3, 1)) -> list[Candle]:
    assert len(closes) == len(volumes)
    return [
        Candle(dt=start + timedelta(days=i), open=c, high=c + 2, low=c - 2, close=c, volume=v)
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


def test_recent_level_events_volume_confirms_breakout():
    structure = _mk_level_150_structure()
    closes = [140, 140, 140, 140, 140, 160, 158, 159, 161, 162]
    volumes = [1000] * 5 + [5000] + [1000] * 4  # пробой (idx5) на объёме заметно выше среднего
    candles = _mk_candles_with_volume(closes, volumes)
    events = recent_level_events(structure, candles, lookback=10)
    breakouts = [e for e in events if e.kind == "пробой (закрепление подтверждено)" and e.level == 0.5]
    assert len(breakouts) == 1, f"Ожидали 1 подтверждённый пробой: {events}"
    assert "подтверждает" in breakouts[0].note and "НЕ подтверждает" not in breakouts[0].note, breakouts[0].note
    print("OK  test_recent_level_events_volume_confirms_breakout")


def test_recent_level_events_volume_does_not_confirm_breakout():
    structure = _mk_level_150_structure()
    closes = [140, 140, 140, 140, 140, 160, 158, 159, 161, 162]
    volumes = [1000] * 10  # пробой на обычном объёме, не выше среднего
    candles = _mk_candles_with_volume(closes, volumes)
    events = recent_level_events(structure, candles, lookback=10)
    breakouts = [e for e in events if e.kind == "пробой (закрепление подтверждено)" and e.level == 0.5]
    assert len(breakouts) == 1, f"Ожидали 1 подтверждённый пробой: {events}"
    assert "НЕ подтверждает" in breakouts[0].note, breakouts[0].note
    print("OK  test_recent_level_events_volume_does_not_confirm_breakout")


def test_recent_level_events_volume_omitted_with_too_few_prior_bars():
    structure = _mk_level_150_structure()
    closes = [140, 160, 158, 159, 161, 162]  # пробой на idx1 -- только 1 свеча до него в полном списке
    volumes = [1000] * 6
    candles = _mk_candles_with_volume(closes, volumes)
    events = recent_level_events(structure, candles, lookback=10)
    breakouts = [e for e in events if e.kind == "пробой (закрепление подтверждено)" and e.level == 0.5]
    assert len(breakouts) == 1, f"Ожидали 1 подтверждённый пробой: {events}"
    assert "объём" not in breakouts[0].note, (
        f"Меньше VOLUME_MIN_PRIOR_BARS предыдущих свечей -- комментарий про объём должен молчать, "
        f"а не подставлять ненадёжное число: {breakouts[0].note}"
    )
    print("OK  test_recent_level_events_volume_omitted_with_too_few_prior_bars")


# --- Инвалидация на графике (27 августа, тот же отзыв "брокера" -- см.
# agents/chart_agent.py, is_invalidation) -----------------------------------


def test_render_chart_handles_alert_level_equal_to_invalidation_level():
    # Тонкий случай: если коррекция дошла ровно до уровня 1.0 (точка 1),
    # достигнутый уровень (highlight) и уровень инвалидации -- ОДНА и та же
    # линия на графике одновременно (is_highlighted и is_invalidation оба
    # True для уровня 1.0) -- не должно падать.
    structure, candles = _mk_structure_with_candles()
    png = render_chart(structure, candles, structure.point1.price, "TEST", "1D", alert_level=1.0)
    assert png[:8] == _PNG_MAGIC
    assert len(png) > 1000
    print("OK  test_render_chart_handles_alert_level_equal_to_invalidation_level")


# --- backtest.py: walk-forward без заглядывания вперёд (27 августа, по
# отзыву "брокера": "у бота нет ни одного бэктеста... приоритет номер один") -


def test_backtest_no_lookahead_bias():
    # Ключевая гарантия всего backtest.py: решение на шаге t зависит ТОЛЬКО
    # от candles[:t+1]. Обрезаем хвост истории и сравниваем сигналы,
    # сработавшие ДО границы обрезки, на полной и на обрезанной сериях --
    # если где-то закралось заглядывание вперёд (например, по ошибке
    # передали в build_global_fibo полный `series` вместо обрезанного окна),
    # эти ранние сигналы разойдутся между прогонами.
    series = load_ibm_demo_daily(Path(__file__).resolve().parent.parent / "data" / "ibm_daily_raw.txt")
    full_signals = simulate_symbol(series)

    cutoff = len(series.candles) - 30
    truncated_series = CandleSeries(
        symbol=series.symbol,
        exchange_or_source=series.exchange_or_source,
        timeframe=series.timeframe,
        candles=series.candles[:cutoff],
        fetched_via=series.fetched_via,
        fetch_note=series.fetch_note,
    )
    truncated_signals = simulate_symbol(truncated_series)

    assert truncated_signals, "Ожидали хотя бы один сигнал в обрезанной серии для содержательной проверки"
    full_by_idx = {s.fired_idx: s for s in full_signals}
    for ts in truncated_signals:
        fs = full_by_idx.get(ts.fired_idx)
        assert fs == ts, (
            f"Сигнал на idx={ts.fired_idx} отличается между полной и обрезанной сериями -- "
            f"похоже на заглядывание вперёд:\n  обрезанная: {ts}\n  полная:     {fs}"
        )
    print(f"OK  test_backtest_no_lookahead_bias -- {len(truncated_signals)} сигналов совпали")


def _mk_backtest_signal(
    entry_price: float = 100.0,
    invalidation_price: float = 90.0,
    target_price: float = 130.0,
    fired_idx: int = 0,
) -> BacktestSignal:
    return BacktestSignal(
        symbol="TEST",
        fired_idx=fired_idx,
        fired_dt=date(2026, 3, 1),
        alert_level=0.618,
        direction="восходящий",
        entry_price=entry_price,
        invalidation_price=invalidation_price,
        target_price=target_price,
        risk_unit=abs(entry_price - invalidation_price),
        consensus_agree=True,
    )


def test_score_outcome_detects_invalidation():
    signal = _mk_backtest_signal(entry_price=100.0, invalidation_price=90.0, target_price=130.0, fired_idx=0)
    filler = _mk_candles([100.0])
    forward = _mk_candles([98, 95, 89, 85], start=date(2026, 3, 2))  # close=89 -> R=-1.1 на 3-й свече
    outcome = _score_outcome(signal, filler + forward)
    assert outcome.outcome == "invalidated", outcome
    assert outcome.days_held == 3, outcome
    assert outcome.final_r is not None and outcome.final_r <= -1.0, outcome
    print(f"OK  test_score_outcome_detects_invalidation -- final_r={outcome.final_r:.2f}")


def test_score_outcome_caps_reward_at_target_hit():
    signal = _mk_backtest_signal(entry_price=100.0, invalidation_price=90.0, target_price=130.0, fired_idx=0)
    filler = _mk_candles([100.0])
    # risk_unit=10 -> REWARD_CAP_R=5.0 достигается при close >= 150
    forward = _mk_candles([110, 130, 155, 200], start=date(2026, 3, 2))
    outcome = _score_outcome(signal, filler + forward)
    assert outcome.outcome == "target_hit", outcome
    assert outcome.final_r == REWARD_CAP_R, outcome
    assert outcome.days_held == 3, outcome  # 155 -> R=5.5 >= 5.0 на 3-й свече, дальше не смотрим
    print(f"OK  test_score_outcome_caps_reward_at_target_hit -- final_r={outcome.final_r}")


def test_score_outcome_reports_insufficient_data_when_forward_window_too_short():
    signal = _mk_backtest_signal(entry_price=100.0, invalidation_price=90.0, target_price=130.0, fired_idx=0)
    filler = _mk_candles([100.0])
    # Всего 3 свечи вперёд (меньше MAX_HOLD_DAYS), ни инвалидация, ни цель не
    # случились -- честно "не знаем", а не "таймаут" (который подразумевал
    # бы, что мы досмотрели весь MAX_HOLD_DAYS).
    forward = _mk_candles([102, 103, 101], start=date(2026, 3, 2))
    outcome = _score_outcome(signal, filler + forward)
    assert outcome.outcome == "insufficient_data", outcome
    assert outcome.final_r is None, outcome
    print("OK  test_score_outcome_reports_insufficient_data_when_forward_window_too_short")


def test_score_outcome_reports_insufficient_data_when_no_forward_candles_at_all():
    signal = _mk_backtest_signal(entry_price=100.0, invalidation_price=90.0, target_price=130.0, fired_idx=0)
    all_candles = _mk_candles([100.0])  # сигнал на самой последней доступной свече -- вперёд вообще ничего нет
    outcome = _score_outcome(signal, all_candles)
    assert outcome.outcome == "insufficient_data", outcome
    assert outcome.final_r is None, outcome
    print("OK  test_score_outcome_reports_insufficient_data_when_no_forward_candles_at_all")


def test_score_outcome_reports_timeout_after_full_hold_window():
    signal = _mk_backtest_signal(entry_price=100.0, invalidation_price=90.0, target_price=130.0, fired_idx=0)
    filler = _mk_candles([100.0])
    # Ровно MAX_HOLD_DAYS свечей вперёд, цена топчется около входа -- ни
    # инвалидация, ни цель не достигнуты, но окно досмотрено ПОЛНОСТЬЮ.
    forward = _mk_candles([101.0] * MAX_HOLD_DAYS, start=date(2026, 3, 2))
    outcome = _score_outcome(signal, filler + forward)
    assert outcome.outcome == "timeout", outcome
    assert outcome.days_held == MAX_HOLD_DAYS, outcome
    expected_r = (101.0 - 100.0) / 10.0
    assert outcome.final_r is not None and abs(outcome.final_r - expected_r) < 1e-9, outcome
    print(f"OK  test_score_outcome_reports_timeout_after_full_hold_window -- final_r={outcome.final_r:.3f}")


def test_score_outcome_handles_zero_risk_unit():
    # entry_price == invalidation_price -- risk_unit=0, R не считается
    # (было бы деление на 0) -- честно insufficient_data, а не падение и не
    # придуманное число.
    signal = _mk_backtest_signal(entry_price=90.0, invalidation_price=90.0, target_price=130.0, fired_idx=0)
    all_candles = _mk_candles([90.0, 95.0, 100.0])
    outcome = _score_outcome(signal, all_candles)
    assert outcome.outcome == "insufficient_data", outcome
    assert outcome.final_r is None, outcome
    print("OK  test_score_outcome_handles_zero_risk_unit")


def test_summarize_counts_wins_and_computes_avg_r():
    sig = _mk_backtest_signal()
    outcomes = [
        ScoredOutcome(signal=sig, outcome="target_hit", final_r=5.0, days_held=3),
        ScoredOutcome(signal=sig, outcome="invalidated", final_r=-1.2, days_held=2),
        ScoredOutcome(signal=sig, outcome="timeout", final_r=0.3, days_held=20),
        ScoredOutcome(signal=sig, outcome="insufficient_data", final_r=None, days_held=1),
    ]
    summary = summarize("тест", outcomes)
    assert summary.n == 4, summary
    assert summary.n_scored == 3, summary  # insufficient_data исключён из знаменателя
    assert summary.n_insufficient_data == 1, summary
    assert summary.win_rate == 2 / 3, summary  # target_hit и timeout(+0.3) -- выигрыши, invalidated -- нет
    expected_avg = (5.0 + -1.2 + 0.3) / 3
    assert summary.avg_r is not None and abs(summary.avg_r - expected_avg) < 1e-9, summary
    print("OK  test_summarize_counts_wins_and_computes_avg_r")


def test_summarize_handles_all_insufficient_data():
    sig = _mk_backtest_signal()
    outcomes = [ScoredOutcome(signal=sig, outcome="insufficient_data", final_r=None, days_held=1)]
    summary = summarize("тест", outcomes)
    assert summary.n_scored == 0, summary
    assert summary.win_rate is None and summary.avg_r is None, summary
    print("OK  test_summarize_handles_all_insufficient_data")


def test_build_verdict_no_analysis_when_checklist_fails():
    bundle = _mk_bundle(fraction=0.7, checklist_ok=False)
    v = build_verdict(bundle)
    assert v.call == CALL_NO_ANALYSIS, v
    assert v.confidence == CONFIDENCE_CERTAIN, v  # единственный случай, где уверенность абсолютная
    print("OK  test_build_verdict_no_analysis_when_checklist_fails")


def test_build_verdict_waits_below_watch_threshold():
    bundle = _mk_bundle(fraction=0.4)  # ниже 0.618
    v = build_verdict(bundle)
    assert v.call == CALL_WAIT, v
    assert v.retracement_pct is not None and abs(v.retracement_pct - 40.0) < 1e-6, v
    print("OK  test_build_verdict_waits_below_watch_threshold")


def test_build_verdict_invalidated_beyond_point1():
    bundle = _mk_bundle(fraction=1.05)  # цена прошла точку 1
    v = build_verdict(bundle)
    assert v.call == CALL_INVALIDATED, v
    print("OK  test_build_verdict_invalidated_beyond_point1")


def test_build_verdict_sell_call_for_descending_structure_in_watch_zone():
    # _mk_bundle строит DESCENDING структуру (точка 1 = HIGH) -- продолжение
    # нисходящего движения это ПРОДАЖА, не ПОКУПКА.
    bundle = _mk_bundle(fraction=0.7)
    v = build_verdict(bundle)
    assert v.call == CALL_SELL, v
    assert v.confidence == CONFIDENCE_GUESS, v  # без подтверждений -- низкая уверенность
    assert any("Бэктест" in c for c in v.caveats), v  # честное предупреждение всегда присутствует
    print("OK  test_build_verdict_sell_call_for_descending_structure_in_watch_zone")


def test_build_verdict_confidence_rises_with_aligned_confirmations():
    bundle = _mk_bundle(fraction=0.7)
    intraday = [
        IntradayConfirmation(timeframe="1H", available=True, note="1H", direction="нисходящий"),
        IntradayConfirmation(timeframe="4H", available=True, note="4H", direction="нисходящий"),
    ]
    v = build_verdict(bundle, consensus_agree=True, intraday_confirmations=intraday)
    assert v.call == CALL_SELL, v
    assert v.confidence == CONFIDENCE_LIKELY, v
    print("OK  test_build_verdict_confidence_rises_with_aligned_confirmations")


def test_build_verdict_waits_when_conflicts_outweigh_confirmations():
    bundle = _mk_bundle(fraction=0.7)
    intraday = [
        IntradayConfirmation(timeframe="1H", available=True, note="1H", direction="восходящий"),  # конфликт
    ]
    v = build_verdict(bundle, consensus_agree=False, consensus_detail="разное направление", intraday_confirmations=intraday)
    assert v.call == CALL_WAIT, v
    assert any("НЕ согласна" in c for c in v.caveats), v
    print("OK  test_build_verdict_waits_when_conflicts_outweigh_confirmations")


def test_format_verdict_contains_call_and_confidence():
    bundle = _mk_bundle(fraction=0.7)
    v = build_verdict(bundle)
    text = format_verdict(v, symbol="TEST")
    assert v.call in text, text
    assert v.confidence in text, text
    assert "TEST" in text, text
    print("OK  test_format_verdict_contains_call_and_confidence")


def test_notify_failure_dry_run_reports_skipped_and_only_leonid():
    result = notify_failure("test_job", "какая-то ошибка", bot_token=None)
    assert result["dry_run"] is True, result
    assert len(result["sent_to"]) == 1, result  # только Леонид, не все трое
    assert result["sent_to"][0]["recipient"].startswith("Леонид"), result
    print("OK  test_notify_failure_dry_run_reports_skipped_and_only_leonid")


def test_notify_failure_truncates_long_log_tail():
    long_log = "x" * (LOG_TAIL_LIMIT * 3)
    result = notify_failure("test_job", long_log, bot_token=None)
    # Само усечение происходит внутри message, а не в возвращаемом result --
    # проверяем через message_preview, который send_via_telegram кладёт в dry-run.
    assert len(result["message_preview"]) < len(long_log), result["message_preview"][:100]
    print("OK  test_notify_failure_truncates_long_log_tail")


def test_notify_failure_handles_empty_log():
    result = notify_failure("test_job", "", bot_token=None)
    assert "(лог пуст)" in result["message_preview"], result["message_preview"]
    print("OK  test_notify_failure_handles_empty_log")


def _mk_analyst_result(symbol: str, fraction: float = 0.4) -> dict:
    """Синтетический элемент results[] для build_digest_messages() --
    та же форма, что и analyze_instrument() возвращает при status='OK'."""
    bundle = _mk_bundle(fraction=fraction)
    verdict = build_verdict(bundle)
    return {"instrument": Instrument(symbol, symbol, "synthetic"), "status": "OK", "bundle": bundle, "verdict": verdict}


def test_build_digest_messages_single_message_when_small():
    results = [_mk_analyst_result("A"), _mk_analyst_result("B")]
    messages = build_digest_messages(results)
    assert len(messages) == 1, messages
    assert "часть" not in messages[0], messages[0]  # префикс страницы не нужен, когда сообщение одно
    print("OK  test_build_digest_messages_single_message_when_small")


def test_build_digest_messages_splits_when_too_long_for_telegram():
    # 15 инструментов -- ровно то число, на котором реально упал первый
    # прогон 5 сентября 2026 (единое сообщение 16+ тыс. символов, Telegram
    # ответил 400 всем троим).
    results = [_mk_analyst_result(f"SYM{i}") for i in range(15)]
    messages = build_digest_messages(results)
    assert len(messages) > 1, "15 инструментов должны были не влезть в одно сообщение"
    for m in messages:
        assert len(m) <= TELEGRAM_TEXT_LIMIT, f"сообщение превышает лимит Telegram: {len(m)} символов"
        assert "часть" in m, m  # при нескольких частях каждая помечена номером
    print("OK  test_build_digest_messages_splits_when_too_long_for_telegram")


def test_build_digest_messages_never_splits_a_single_card():
    # Каждая карточка целиком должна оказаться внутри РОВНО одного сообщения --
    # ищем текст конкретного тикера и убеждаемся, что он не размазан.
    results = [_mk_analyst_result(f"SYM{i}") for i in range(15)]
    messages = build_digest_messages(results)
    for i in range(15):
        symbol = f"SYM{i}"
        containing = [m for m in messages if f"<b>{symbol}</b>" in m]
        assert len(containing) == 1, f"{symbol} должен встретиться ровно в одном сообщении, найдено в {len(containing)}"
    print("OK  test_build_digest_messages_never_splits_a_single_card")


def test_load_sp500_universe_returns_real_list_no_dotted_symbols():
    universe = load_sp500_universe()
    assert len(universe) > 400, len(universe)  # реальный список, не заглушка
    symbols = {i.symbol for i in universe}
    assert "AAPL" in symbols, symbols
    assert "BRK-B" in symbols, symbols  # нормализовано из BRK.B (Wikipedia)
    assert not any("." in i.symbol for i in universe), [i for i in universe if "." in i.symbol]
    print("OK  test_load_sp500_universe_returns_real_list_no_dotted_symbols")


def _mk_opportunity_result(symbol: str, call: str) -> dict:
    """Синтетический элемент results[] со ЗАДАННЫМ call -- в отличие от
    _mk_analyst_result() (test_pipeline.py выше), здесь call подставляется
    напрямую через dataclasses.replace, не через fraction, потому что
    find_opportunities() фильтрует именно по call, а не по глубине."""
    import dataclasses

    bundle = _mk_bundle(fraction=0.7)
    verdict = build_verdict(bundle)
    verdict = dataclasses.replace(verdict, call=call)
    return {"instrument": Instrument(symbol, symbol, "synthetic"), "status": "OK", "bundle": bundle, "verdict": verdict}


def test_find_opportunities_filters_to_buy_and_sell_only():
    results = [
        _mk_opportunity_result("A", CALL_BUY),
        _mk_opportunity_result("B", CALL_WAIT),
        _mk_opportunity_result("C", CALL_SELL),
        _mk_opportunity_result("D", CALL_INVALIDATED),
        _mk_opportunity_result("E", CALL_NO_ANALYSIS),
    ]
    opportunities = find_opportunities(results)
    found_symbols = {r["instrument"].symbol for r in opportunities}
    assert found_symbols == {"A", "C"}, found_symbols
    print("OK  test_find_opportunities_filters_to_buy_and_sell_only")


def test_find_opportunities_empty_when_nothing_qualifies():
    results = [_mk_opportunity_result("A", CALL_WAIT), _mk_opportunity_result("B", CALL_NO_ANALYSIS)]
    assert find_opportunities(results) == []
    print("OK  test_find_opportunities_empty_when_nothing_qualifies")


def test_build_opportunity_messages_respects_telegram_limit():
    opportunities = [_mk_opportunity_result(f"SYM{i}", CALL_BUY) for i in range(15)]
    messages = build_opportunity_messages(opportunities)
    assert len(messages) > 1, "15 карточек должны были не влезть в одно сообщение"
    for m in messages:
        assert len(m) <= TELEGRAM_TEXT_LIMIT, f"сообщение превышает лимит Telegram: {len(m)} символов"
    print("OK  test_build_opportunity_messages_respects_telegram_limit")


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
