"""
Бэктест (walk-forward, без заглядывания вперёд)
=================================================
27 августа, по собственному отзыву "брокера с многолетним опытом" (см.
project doc, раздел про роль-плей), который Леонид попросил дать, а потом
прямо процитировал обратно как ТЗ: "у бота нет ни одного бэктеста. Он ни
разу не проверялся на истории... Без этого числа это красиво оформленная
система уведомлений, а не проверенное преимущество. Я бы это назвал
приоритетом номер один перед любыми новыми фичами."

Что здесь проверяется: НЕ вся торговая система (это не симулятор ордеров с
проскальзыванием, комиссией, размером позиции) -- а конкретно СТРУКТУРНЫЙ
СИГНАЛ бота: "коррекция дошла до уровня X (0.618/0.786/1.0), should_send_
level_watch() решил бы отправить алерт" -- и что случалось с ценой ПОСЛЕ
этого момента, если считать гипотезу "коррекция исчерпалась, движение
возобновится в сторону точки 2" (та же гипотеза, что уже явно написана в
сообщении алерта, см. agents/dispatch_agent.py, строка про "Ориентир
инвалидации").

САМОЕ ВАЖНОЕ ПРАВИЛО ЭТОГО ФАЙЛА -- нет заглядывания в будущее при ПРИНЯТИИ
РЕШЕНИЯ:

  simulate_symbol() идёт по свечам ДЕНЬ ЗА ДНЁМ. На шаге t решение "сработал
  бы алерт" считается СТРОГО на candles[:t+1] -- ни одна свеча после t не
  передаётся ни в build_global_fibo(), ни в find_fractal_swing_extremes(),
  ни в какую другую часть пайплайна на этом шаге. Это тот же самый пайплайн
  агентов, что и в проде (build_global_fibo -> nearest_level ->
  recent_level_events -> run_checklist -> should_send_level_watch), просто
  прогнанный по истории вместо реального времени -- если бы здесь была
  ошибка и куда-то случайно попадал полный `series` вместо обрезанного окна
  на момент t, то ранние решения задним числом "знали" бы о будущих
  экстремумах цены, и вся оценка была бы нечестной (классическая ошибка
  бэктестов). Есть отдельный тест именно на это
  (test_backtest_no_lookahead_bias в tests/test_pipeline.py) -- сравнивает
  решения на полной серии свечей и на её обрезанном префиксе; если где-то
  закралось заглядывание вперёд, более ранние решения на префиксе и на
  полной серии разойдутся, и тест это поймает.

  _score_outcome(), наоборот, СМОТРИТ ВПЕРЁД (candles[fired_idx+1 :
  fired_idx+1+MAX_HOLD_DAYS]) -- и это НЕ нарушение того же правила: решение
  "алерт сработал" уже принято выше строго по прошлым данным, здесь только
  оценивается результат УЖЕ ПРИНЯТОГО решения, ровно как реальный трейдер
  узнаёт исход сделки только после входа, а не до.

Направление "выгодного" движения и риск/цель берутся из той же
направленной гипотезы, что уже показывается в самом алерте (см.
agents/dispatch_agent.py, format_message(), блок "Ориентир инвалидации", и
README.md, раздел от 27 августа): инвалидация = point1.price (начало
исходного движения, закрытие за ним отменяет тезис), цель/ориентир =
point2.price (конец исходного движения, тезис "продолжение" -- это движение
обратно к ней и дальше). Это ПРОИЗВОДНОЕ рассуждение Claude от
зафиксированной в fibo_agent.py конвенции point1/point2, не отдельно
подтверждённое Леонидом правило регламента -- явно вынесено на проверку в
README и в сообщении Леониду, а не тихо принято как факт.

Ограничения, о которых нужно честно помнить при чтении результатов:
  -- R-мультипликатор идеализирован: вход и оценка -- по цене ЗАКРЫТИЯ
     свечи (не intrabar), без проскальзывания и комиссии;
  -- MAX_HOLD_DAYS и REWARD_CAP_R (ниже) -- первые рабочие значения, не
     откалиброванные под конкретный инструмент или стиль торговли;
  -- это оценка ОДНОГО сигнала (level-watch), не всей системы управления
     риском/позицией -- бот таких указаний и не даёт (см. дисклеймер в
     самом сообщении алерта).

Запуск: нужны реальные исторические данные (Financial Modeling Prep) и
сетевой доступ -- как и load_fmp_daily() в проде, это работает на VPS, НЕ
в песочнице Claude (см. agents/data_agent.py). На VPS:

    cd /root/fib-bot && MARKET_DATA_API_KEY=... python3 backtest.py

Без сети/ключа файл всё равно ИМПОРТИРУЕТСЯ и его функции (simulate_symbol,
_score_outcome, summarize) тестируются на синтетических/демо-данных в
tests/test_pipeline.py -- сетевой вызов нужен только в блоке __main__.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from agents.data_agent import Candle, CandleSeries, load_fmp_daily
from agents.dispatch_agent import AnalysisBundle, LevelWatchState, should_send_level_watch
from agents.fibo_agent import MIN_LEFT_BARS, build_global_fibo, find_fractal_swing_extremes
from agents.price_behavior_agent import nearest_level, recent_level_events
from agents.verification_agent import cross_check_structures, run_checklist

# Раньше этого индекса find_global_extremes() гарантированно бросит
# ValueError на ОБЕИХ точках (раздел 8-9 регламента: нужно минимум
# MIN_LEFT_BARS свечей СЛЕВА от экстремума) -- нет смысла даже пробовать
# строить структуру. Это только отсечение заведомо провальных попыток на
# самых первых барах, НЕ гарантия, что структура подтвердится сразу после
# этого индекса -- она может не подтвердиться ещё долго, try/except в
# simulate_symbol() ниже это и так честно обрабатывает.
_EARLIEST_T = MIN_LEFT_BARS

MAX_HOLD_DAYS = 20  # первое рабочее значение, не калибровка под инструмент
REWARD_CAP_R = 5.0  # потолок для одного удачного сигнала, чтобы редкий выброс не перекашивал среднее
MONTHS_BACK = 36  # ~3 года -- рабочее начальное значение; сколько реально отдаёт план FMP Starter, НЕ проверено вживую, см. __main__


@dataclass(frozen=True)
class BacktestSignal:
    """Один момент, когда should_send_level_watch() решил бы отправить алерт
    -- посчитано строго по данным ДО и ВКЛЮЧАЯ fired_idx (см. докстринг
    файла). direction/invalidation/target/risk_unit фиксируют структуру
    ИМЕННО на момент сигнала (структура на более поздних шагах может
    измениться -- новый свинг, другая structure_id -- это нормально и не
    переписывает уже зафиксированный сигнал задним числом)."""

    symbol: str
    fired_idx: int  # индекс свечи в ПОЛНОМ candles, на которой сработал бы алерт
    fired_dt: date
    alert_level: float  # 0.618 / 0.786 / 1.0 -- какой порог был достигнут
    direction: str  # Direction.value на момент сигнала
    entry_price: float  # close свечи fired_idx
    invalidation_price: float  # point1.price на момент сигнала
    target_price: float  # point2.price на момент сигнала
    risk_unit: float  # abs(entry_price - invalidation_price)
    consensus_agree: bool | None  # True/False/None(недоступна) -- та же трёхзначная логика, что и format_consensus_note()


@dataclass(frozen=True)
class ScoredOutcome:
    signal: BacktestSignal
    outcome: str  # "invalidated" | "target_hit" | "timeout" | "insufficient_data"
    final_r: float | None  # None только при "insufficient_data" -- не подставляем число, если честно его не знаем
    days_held: int


@dataclass(frozen=True)
class BacktestSummary:
    label: str
    n: int
    n_scored: int  # исключая insufficient_data
    win_rate: float | None
    avg_r: float | None
    n_invalidated: int
    n_target_hit: int
    n_timeout: int
    n_insufficient_data: int


def simulate_symbol(
    series: CandleSeries, watch_levels: tuple[float, ...] = (0.618, 0.786, 1.0)
) -> list[BacktestSignal]:
    """
    Идёт по series.candles день за днём (t = _EARLIEST_T .. конец). На
    каждом шаге строит структуру и решение should_send_level_watch()
    СТРОГО на candles[:t+1] -- см. докстринг файла про no-lookahead.
    LevelWatchState переносится между шагами ТАК ЖЕ, как оркестратор
    переносит его между запусками cron через output/alert_state.json --
    здесь просто вместо чтения/записи файла между реальными запусками это
    одна переменная `state`, которая живёт между итерациями цикла.

    watch_levels -- тот же порог по умолчанию (0.618, 0.786, 1.0), что и
    в should_send_level_watch() в проде (agents/dispatch_agent.py).
    """
    candles = series.candles
    signals: list[BacktestSignal] = []
    state = LevelWatchState()

    for t in range(_EARLIEST_T, len(candles)):
        window_candles = candles[: t + 1]  # СТРОГО candles[:t+1] -- ключевая строка всего файла
        window_series = CandleSeries(
            symbol=series.symbol,
            exchange_or_source=series.exchange_or_source,
            timeframe=series.timeframe,
            candles=window_candles,
            fetched_via=series.fetched_via,
            fetch_note=series.fetch_note,
        )

        try:
            structure = build_global_fibo(window_series)
        except ValueError:
            continue  # структура ещё не подтвердилась на этом шаге -- как и в проде, просто ждём следующего дня

        current_price = window_candles[-1].close
        n = nearest_level(structure, current_price)
        events = recent_level_events(structure, window_candles, lookback=10)
        report = run_checklist(structure, series.exchange_or_source)
        bundle = AnalysisBundle(
            symbol=series.symbol,
            source_tag=series.exchange_or_source,
            timeframe=series.timeframe,
            period_desc=f"{window_series.start} .. {window_series.end}",
            structure=structure,
            nearest=n,
            recent_events=events,
            checklist=report,
        )

        ok, _, new_state = should_send_level_watch(bundle, state, watch_levels=watch_levels)
        state = new_state
        if not ok:
            continue

        # Независимая сверка -- та же no-lookahead гарантия: фрактальный
        # метод тоже видит только window_candles/window_series, не полный
        # `candles`.
        try:
            fractal_structure = build_global_fibo(window_series, extremes_fn=find_fractal_swing_extremes)
            consensus_agree = cross_check_structures(structure, fractal_structure).agree
        except ValueError:
            consensus_agree = None

        signals.append(
            BacktestSignal(
                symbol=series.symbol,
                fired_idx=t,
                fired_dt=window_candles[-1].dt,
                alert_level=new_state.last_alerted_level,
                direction=structure.direction.value,
                entry_price=current_price,
                invalidation_price=structure.point1.price,
                target_price=structure.point2.price,
                risk_unit=abs(current_price - structure.point1.price),
                consensus_agree=consensus_agree,
            )
        )

    return signals


def _score_outcome(signal: BacktestSignal, all_candles: list[Candle]) -> ScoredOutcome:
    """
    Смотрит ВПЕРЁД (all_candles[fired_idx+1 : fired_idx+1+MAX_HOLD_DAYS]) --
    легитимно, см. докстринг файла: решение уже принято раньше строго по
    прошлым данным, здесь только оценка уже принятого решения.

    R считается по ЗАКРЫТИЮ каждой свечи (не по теням) -- сознательно то же
    самое, чем сам алерт называет инвалидацию ("закрытие за точку 1"), а не
    внутрисвечным касанием.

    direction_sign: +1, если выгодное направление -- рост цены (точка 2
    выше точки 1: восходящая структура, тезис "откат вниз исчерпался,
    дальше снова вверх к точке 2"), иначе -1. Определяется по знаку
    (target_price - invalidation_price), что эквивалентно направлению
    структуры, но не завязано на текстовое значение Direction.value.

    Три РАЗНЫХ способа честно не досчитать до полноценного R:
      -- risk_unit <= 0 (цена сигнала уже ровно в точке инвалидации -- на
         практике почти невозможно при watch_levels>=0.618, но не должно
         уронить бэктест делением на ноль) -> insufficient_data;
      -- свечей после сигнала вообще нет -> insufficient_data;
      -- свечи есть, но их МЕНЬШЕ MAX_HOLD_DAYS, и за это время не
         случилось ни инвалидации, ни цели -- то есть мы НЕ знаем, что
         было бы дальше, а полного окна не досмотрели -> insufficient_data,
         а не "timeout" (который подразумевает, что мы честно досмотрели
         весь MAX_HOLD_DAYS и ничего не случилось).
    """
    if signal.risk_unit <= 0:
        return ScoredOutcome(signal=signal, outcome="insufficient_data", final_r=None, days_held=0)

    direction_sign = 1.0 if signal.target_price > signal.invalidation_price else -1.0
    forward = all_candles[signal.fired_idx + 1 : signal.fired_idx + 1 + MAX_HOLD_DAYS]

    if not forward:
        return ScoredOutcome(signal=signal, outcome="insufficient_data", final_r=None, days_held=0)

    for i, c in enumerate(forward, start=1):
        r = direction_sign * (c.close - signal.entry_price) / signal.risk_unit
        if r <= -1.0:
            return ScoredOutcome(signal=signal, outcome="invalidated", final_r=r, days_held=i)
        if r >= REWARD_CAP_R:
            return ScoredOutcome(signal=signal, outcome="target_hit", final_r=REWARD_CAP_R, days_held=i)

    if len(forward) < MAX_HOLD_DAYS:
        return ScoredOutcome(signal=signal, outcome="insufficient_data", final_r=None, days_held=len(forward))

    last_r = direction_sign * (forward[-1].close - signal.entry_price) / signal.risk_unit
    return ScoredOutcome(signal=signal, outcome="timeout", final_r=last_r, days_held=len(forward))


def summarize(label: str, scored: list[ScoredOutcome]) -> BacktestSummary:
    """
    "win" здесь -- не только formal target_hit: таймаут с final_r > 0
    (цена двигалась в сторону тезиса, просто не дошла до полного
    REWARD_CAP_R за MAX_HOLD_DAYS) тоже считается в числителе win_rate, а
    invalidated и таймаут с final_r <= 0 -- нет. Ближе к тому, что реально
    интересно трейдеру ("была ли гипотеза направления верной"), чем
    формальное "дошла ли цена ровно до точки 2".

    insufficient_data ИСКЛЮЧЕНЫ из n_scored/win_rate/avg_r -- не
    подставляем предположение вместо честного "не знаем" (регламент,
    раздел 2), но n (общее число сигналов) их всё равно считает, чтобы
    было видно, сколько сигналов вообще не удалось оценить.
    """
    usable = [s for s in scored if s.outcome != "insufficient_data"]
    n_insufficient = len(scored) - len(usable)
    if not usable:
        return BacktestSummary(
            label=label,
            n=len(scored),
            n_scored=0,
            win_rate=None,
            avg_r=None,
            n_invalidated=0,
            n_target_hit=0,
            n_timeout=0,
            n_insufficient_data=n_insufficient,
        )
    wins = sum(1 for s in usable if s.final_r is not None and s.final_r > 0)
    avg_r = sum(s.final_r for s in usable if s.final_r is not None) / len(usable)
    return BacktestSummary(
        label=label,
        n=len(scored),
        n_scored=len(usable),
        win_rate=wins / len(usable),
        avg_r=avg_r,
        n_invalidated=sum(1 for s in usable if s.outcome == "invalidated"),
        n_target_hit=sum(1 for s in usable if s.outcome == "target_hit"),
        n_timeout=sum(1 for s in usable if s.outcome == "timeout"),
        n_insufficient_data=n_insufficient,
    )


def format_summary(summary: BacktestSummary) -> str:
    if summary.n_scored == 0:
        return f"{summary.label}: {summary.n} сигналов, ни один не удалось оценить (недостаточно данных вперёд)"
    return (
        f"{summary.label}: {summary.n} сигналов, {summary.n_scored} оценено "
        f"({summary.n_insufficient_data} без данных вперёд) -- "
        f"win_rate={summary.win_rate:.1%}, avg_r={summary.avg_r:+.2f}R "
        f"[инвалидировано={summary.n_invalidated}, цель={summary.n_target_hit}, таймаут={summary.n_timeout}]"
    )


def run_backtest_for_symbol(
    symbol: str,
    exchange_hint: str,
    months_back: int = MONTHS_BACK,
    fetch_fn=load_fmp_daily,
) -> list[ScoredOutcome]:
    """fetch_fn -- параметр для тестов (тот же паттерн, что и у
    screener.scan_instrument), позволяет подставить фиктивный/демо источник
    вместо реального load_fmp_daily (который требует сеть и API-ключ)."""
    series = fetch_fn(symbol, months_back=months_back, exchange_hint=exchange_hint)
    signals = simulate_symbol(series)
    return [_score_outcome(sig, series.candles) for sig in signals]


if __name__ == "__main__":
    import os

    import screener

    if not os.environ.get("MARKET_DATA_API_KEY"):
        print(
            "Нет MARKET_DATA_API_KEY -- бэктест не запускается: нужны реальные "
            "исторические данные (Financial Modeling Prep), а сеть до "
            "financialmodelingprep.com есть только на VPS, не в песочнице "
            "Claude (см. agents/data_agent.py). Запусти на сервере: "
            "MARKET_DATA_API_KEY=... python3 backtest.py"
        )
        raise SystemExit(1)

    print("=" * 70)
    print(
        f"БЭКТЕСТ -- {len(screener.INSTRUMENTS)} инструментов, {MONTHS_BACK} мес. истории, "
        f"MAX_HOLD_DAYS={MAX_HOLD_DAYS}, REWARD_CAP_R={REWARD_CAP_R}"
    )
    print("=" * 70)
    print(
        "Оценивается структурный сигнал level-watch (см. докстринг файла) -- "
        "не полноценная торговая система, без проскальзывания/комиссии."
    )
    print()

    all_scored: list[ScoredOutcome] = []
    for instrument in screener.INSTRUMENTS:
        try:
            scored = run_backtest_for_symbol(instrument.symbol, instrument.exchange_hint)
        except Exception as e:  # сеть, тикер, лимиты плана FMP -- один проблемный инструмент не должен обрывать остальные
            print(f"  {instrument.label:30s} [{instrument.symbol:10s}] ОШИБКА: {e}")
            continue
        all_scored.extend(scored)
        print(f"  {format_summary(summarize(f'{instrument.label:30s}', scored))}")

    print()
    print("=" * 70)
    print("ИТОГО")
    print("=" * 70)
    print(format_summary(summarize("Все инструменты", all_scored)))
    print(format_summary(summarize("  -- сверка согласна", [s for s in all_scored if s.signal.consensus_agree is True])))
    print(format_summary(summarize("  -- сверка НЕ согласна", [s for s in all_scored if s.signal.consensus_agree is False])))
    print(format_summary(summarize("  -- сверка недоступна", [s for s in all_scored if s.signal.consensus_agree is None])))
    for lv in (0.618, 0.786, 1.0):
        print(format_summary(summarize(f"  -- уровень {lv:g}", [s for s in all_scored if s.signal.alert_level == lv])))
