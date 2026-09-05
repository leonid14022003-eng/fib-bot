"""
Analyst Agent
=============
Синтезирует явный вердикт (ПОКУПКА/ПРОДАЖА/ЖДАТЬ/ИНВАЛИДИРОВАНО) поверх
уже посчитанных сигналов остальных агентов -- по прямому запросу Леонида,
5 сентября 2026: бот должен присылать "полный анализ того, что он думает
сейчас -- закупать, продавать либо холодить", "как мой лучший аналитик".

ВАЖНО (регламент, раздел 2 -- не имитировать результат; раздел 25 -- не
менять методику молча): этот агент НЕ вводит новую методику построения
структуры и НЕ меняет пороги 0.618/0.786/1.0 -- вся математика Фибо
остаётся ровно той же, что в fibo_agent.py/dispatch_agent.py. Он только
СИНТЕЗИРУЕТ то, что уже посчитано (глубина коррекции, checklist,
независимая сверка, внутридневное подтверждение) в одну явную
рекомендацию с явной оценкой уверенности -- и всегда прикладывает честное
предупреждение об исторической эффективности сигнала (см. backtest.py),
чтобы структурный ориентир не выглядел как подтверждённое преимущество.

Что это НЕ делает: не считает размер позиции, не даёт риск-менеджмент --
бот по-прежнему не даёт торговых инструкций, только структурированное
мнение по текущей картине, честно помеченное уровнем уверенности.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from agents.data_agent import CandleSeries
from agents.dispatch_agent import AnalysisBundle, retracement_fraction
from agents.fibo_agent import Direction, build_global_fibo, find_fractal_swing_extremes, find_oldest_unbroken_extremes
from agents.intraday_agent import IntradayConfirmation
from agents.price_behavior_agent import nearest_level, recent_level_events
from agents.verification_agent import cross_check_structures, run_checklist

CALL_BUY = "ПОКУПКА"
CALL_SELL = "ПРОДАЖА"
CALL_WAIT = "ЖДАТЬ"
CALL_INVALIDATED = "ИНВАЛИДИРОВАНО"
CALL_NO_ANALYSIS = "НЕТ АНАЛИЗА"

# Оценка уверенности -- та же трёхзначная шкала, которую Леонид использует
# сам ("[Точно]/[Вероятно]/[Предположительно]"). "[Точно]" сознательно НЕ
# используется ни для одного рыночного вызова (BUY/SELL/WAIT/INVALIDATED) --
# у сигнала нет подтверждённого преимущества (см. BACKTEST_CAVEAT), точная
# уверенность в направлении рынка была бы нечестной. "[Точно]" оставлен
# только для случая, где мы действительно уверены -- что анализ невозможен
# (проверка не прошла).
CONFIDENCE_CERTAIN = "[Точно]"
CONFIDENCE_LIKELY = "[Вероятно]"
CONFIDENCE_GUESS = "[Предположительно]"

BACKTEST_CAVEAT = (
    "Бэктест этого сигнала (python3 backtest.py, история по 14 инструментам) исторически "
    "показывает слабый/отрицательный результат -- это НЕ подтверждённое торговое "
    "преимущество, а структурированное мнение по текущей картине. Решение по входу, "
    "размеру позиции и риску -- полностью за вами."
)


@dataclass(frozen=True)
class Verdict:
    call: str
    confidence: str
    reasons: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    invalidation_price: float | None = None
    target_price: float | None = None
    retracement_pct: float | None = None


def build_verdict(
    bundle: AnalysisBundle,
    consensus_agree: bool | None = None,
    consensus_detail: str | None = None,
    intraday_confirmations: list[IntradayConfirmation] | None = None,
    watch_levels: tuple[float, ...] = (0.618, 0.786, 1.0),
) -> Verdict:
    """
    Собирает Verdict из уже готовых результатов остальных агентов -- сам
    ничего не запрашивает по сети и не строит структуру заново.

    consensus_agree/consensus_detail -- результат cross_check_structures()
    (verification_agent.py), тот же самый, что уже используется в
    orchestrator.py/screener.py для consensus_note. None означает "сверка
    недоступна в этом прогоне" (см. find_fractal_swing_extremes ValueError).

    intraday_confirmations -- список из get_intraday_confirmations()
    (intraday_agent.py), необязательный (не считать на каждый холостой
    инструмент -- см. докстринг того модуля про стоимость сетевых запросов).
    """
    if not bundle.checklist.all_passed:
        failed = ", ".join(r.name for r in bundle.checklist.failed())
        return Verdict(
            call=CALL_NO_ANALYSIS,
            confidence=CONFIDENCE_CERTAIN,
            reasons=[f"Verification-агент не пропустил структуру: {failed}"],
        )

    s = bundle.structure
    frac = retracement_fraction(bundle)
    # BACKTEST_CAVEAT сюда больше НЕ добавляется -- 5 сентября 2026, по
    # прямому запросу Леонида: при большом числе карточек (opportunity_scanner.py,
    # десятки инструментов) один и тот же абзац повторялся на каждой,
    # это был шум, а не информация. Показывается ОДИН раз на весь отчёт
    # (см. build_digest()/build_opportunity_report()), а не на каждую
    # карточку. Здесь caveats остаются только для СПЕЦИФИЧНЫХ по этому
    # инструменту предупреждений (сверка не согласна, intraday конфликт) --
    # это не шаблон, а реальный сигнал риска именно по этой монете/акции.
    caveats: list[str] = []

    is_ascending = s.direction == Direction.ASCENDING
    continuation_call = CALL_BUY if is_ascending else CALL_SELL
    toward = "вверх к точке 2" if is_ascending else "вниз к точке 2"

    # Инвалидация -- цена уже прошла точку 1, гипотеза "коррекция
    # исчерпалась" для ЭТОЙ структуры не подтвердилась (та же логика, что и
    # ориентир инвалидации в dispatch_agent.py.format_message()).
    if frac > 1.0:
        return Verdict(
            call=CALL_INVALIDATED,
            confidence=CONFIDENCE_LIKELY,
            reasons=[
                f"Цена прошла точку 1 ({s.point1.price:.2f}) -- гипотеза 'коррекция "
                f"исчерпалась' по этой структуре не подтвердилась."
            ],
            caveats=caveats,
            invalidation_price=s.point1.price,
            target_price=s.point2.price,
            retracement_pct=frac * 100,
        )

    min_watch = min(watch_levels)
    if frac < min_watch:
        return Verdict(
            call=CALL_WAIT,
            confidence=CONFIDENCE_GUESS,
            reasons=[
                f"Коррекция сейчас {frac * 100:.1f}% -- ещё не дошла до зоны наблюдения "
                f"({min_watch:g}+). Ждём более глубокого отката, прежде чем рассматривать "
                f'сигнал "{continuation_call}".'
            ],
            caveats=caveats,
            invalidation_price=s.point1.price,
            target_price=s.point2.price,
            retracement_pct=frac * 100,
        )

    # frac в [min_watch, 1.0] -- зона наблюдения достигнута (та же зона,
    # что уже использует should_send_level_watch() для реального алерта).
    reasons: list[str] = [
        f"Коррекция дошла до {frac * 100:.1f}% -- в зоне наблюдения ({min_watch:g}+). "
        f"Гипотеза продолжения: движение {toward} ({s.point2.price:.2f})."
    ]

    confirmations = 0
    conflicts = 0

    if consensus_agree is True:
        confirmations += 1
        reasons.append("Независимая (фрактальная) сверка структуры согласна.")
    elif consensus_agree is False:
        conflicts += 1
        caveats.append(f"Независимая сверка НЕ согласна: {consensus_detail}")
    elif consensus_detail:
        caveats.append(f"Независимая сверка недоступна: {consensus_detail}")

    if intraday_confirmations:
        available = [c for c in intraday_confirmations if c.available]
        if available:
            aligned = [c for c in available if c.direction == s.direction.value]
            conflicting = [c for c in available if c.direction is not None and c.direction != s.direction.value]
            if aligned:
                confirmations += len(aligned)
                reasons.append(
                    "Внутридневное подтверждение (" + ", ".join(c.timeframe for c in aligned)
                    + ") согласуется по направлению."
                )
            if conflicting:
                conflicts += len(conflicting)
                caveats.append(
                    "Внутридневная структура (" + ", ".join(c.timeframe for c in conflicting)
                    + ") показывает ДРУГОЕ направление -- см. текст алерта."
                )
        else:
            caveats.append("Внутридневное подтверждение недоступно в этом прогоне.")

    if conflicts > confirmations:
        call = CALL_WAIT
        confidence = CONFIDENCE_GUESS
        reasons.append("Сигналов-противоречий больше, чем подтверждений -- предпочтительнее подождать.")
    else:
        call = continuation_call
        if confirmations >= 2 and conflicts == 0:
            confidence = CONFIDENCE_LIKELY
        else:
            confidence = CONFIDENCE_GUESS

    return Verdict(
        call=call,
        confidence=confidence,
        reasons=reasons,
        caveats=caveats,
        invalidation_price=s.point1.price,
        target_price=s.point2.price,
        retracement_pct=frac * 100,
    )


def format_verdict(
    verdict: Verdict, symbol: str, display_name: str | None = None, source_tag: str | None = None
) -> str:
    """HTML-блок для Telegram (тот же parse_mode='HTML', что и format_message
    в dispatch_agent.py) -- независимый от неё, добавляется отдельным
    сообщением/секцией, не подменяет обязательный формат раздела 24.

    source_tag -- AnalysisBundle.source_tag (тот же честный тег источника,
    что уже проходит через verification_agent.run_checklist), например
    "Binance futures klines -- CRYPTO" или "Financial Modeling Prep
    /stable/historical-price-eod (Starter plan) -- NASDAQ/NYSE (US)".
    Добавлено 5 сентября 2026 по прямому запросу Леонида -- "чтобы это
    реально можно было торговать", т.е. каждая карточка сама говорит, где
    искать этот тикер, а не только называет символ."""
    title = f"{display_name} ({symbol})" if display_name and display_name != symbol else symbol
    lines = [f"🧭 <b>{title}</b> — вердикт аналитика: <b>{verdict.call}</b> {verdict.confidence}"]
    if source_tag:
        lines.append(f"📍 Где торговать: {source_tag} · тикер <b>{symbol}</b>")
    if verdict.retracement_pct is not None:
        lines.append(f"Глубина коррекции: {verdict.retracement_pct:.1f}%")
    if verdict.invalidation_price is not None and verdict.target_price is not None:
        lines.append(f"Инвалидация: {verdict.invalidation_price:.2f} · Цель: {verdict.target_price:.2f}")
    if verdict.reasons:
        lines.append("")
        lines.append("Почему:")
        for r in verdict.reasons:
            lines.append(f"• {r}")
    if verdict.caveats:
        lines.append("")
        lines.append("Оговорки:")
        for c in verdict.caveats:
            lines.append(f"• {c}")
    return "\n".join(lines)


def analyze_series(
    series: CandleSeries,
    intraday_confirmations: list[IntradayConfirmation] | None = None,
) -> tuple[AnalysisBundle, Verdict]:
    """
    Общий путь Structure/Fibo -> Price-Behavior -> Verification ->
    независимая сверка -> Verdict, для УЖЕ ПОЛУЧЕННЫХ свечей (данные
    получает вызывающий код -- этой функции всё равно, откуда series,
    FMP или Binance). Вынесено 5 сентября 2026, чтобы analyst_report.py
    и opportunity_scanner.py не дублировали один и тот же кусок логики.

    НЕ используется screener.py/orchestrator.py (боевой level-watch cron)
    -- та ветка производственного кода не тронута, чтобы не рисковать
    уже работающими алертами ради рефакторинга.

    Бросает ValueError, если структура не подтвердилась (см.
    find_oldest_unbroken_extremes) -- вызывающий код должен поймать её
    сам, как и в screener.scan_instrument().
    """
    structure = build_global_fibo(series, extremes_fn=find_oldest_unbroken_extremes)
    current_price = series.candles[-1].close
    n = nearest_level(structure, current_price)
    events = recent_level_events(structure, series.candles, lookback=10)
    report = run_checklist(structure, series.exchange_or_source)

    try:
        fractal_structure = build_global_fibo(series, extremes_fn=find_fractal_swing_extremes)
        consensus = cross_check_structures(structure, fractal_structure)
        consensus_agree, consensus_detail = consensus.agree, consensus.detail
    except ValueError as e:
        consensus_agree, consensus_detail = None, str(e)

    bundle = AnalysisBundle(
        symbol=series.symbol,
        source_tag=series.exchange_or_source,
        timeframe=series.timeframe,
        period_desc=f"{series.start} .. {series.end} ({len(series.candles)} дневных свечей)",
        structure=structure,
        nearest=n,
        recent_events=events,
        checklist=report,
    )
    verdict = build_verdict(
        bundle,
        consensus_agree=consensus_agree,
        consensus_detail=consensus_detail,
        intraday_confirmations=intraday_confirmations,
    )
    return bundle, verdict
