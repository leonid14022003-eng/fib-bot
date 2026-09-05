"""
Analyst Report
==============
Полный анализ по ВСЕМ отслеживаемым инструментам (IBM + 14 из
screener.py) -- по прямому запросу Леонида, 5 сентября 2026: бот должен
присылать "полный анализ того, что он думает сейчас -- закупать,
продавать либо холодить", "как мой лучший аналитик".

Чем это отличается от orchestrator.py/screener.py: те шлют алерт ТОЛЬКО
когда коррекция дошла до 0.618+ (should_send_level_watch()) -- модель
"молчим, пока не случится что-то значимое". Этот скрипт формирует вердикт
(agents/analyst_agent.py) для ВСЕХ инструментов на КАЖДОМ запуске,
независимо от того, сработал бы обычный алерт -- модель "вот что сейчас
происходит и что я думаю", как периодическая сводка от аналитика.

НЕ заменяет и НЕ меняет существующий level-watch (cron, run_live.sh/
run_screener.sh, should_send_level_watch()) -- полностью отдельный,
независимый режим поверх тех же данных и той же методики Фибо (регламент,
раздел 25 -- пороги и правила построения структуры не менялись).

Запуск (нужны реальные данные, см. backtest.py про то же самое):
    MARKET_DATA_API_KEY=... python3 analyst_report.py

По умолчанию DRY RUN: ничего не уходит в Telegram, результат печатается в
консоль и пишется в output/last_analyst_report.txt. Реальная отправка
требует ДВОЙНОГО явного согласия -- TELEGRAM_BOT_TOKEN в окружении И
ANALYST_REPORT_SEND_REAL=1 -- чтобы случайный `source .env` без намерения
слать не разослал реальный отчёт всем троим (см. CLAUDE.md, "Никогда").

ANALYST_REPORT_INCLUDE_INTRADAY=0 -- отключает 1H/4H проверку (см.
intraday_agent.py) для ВСЕХ инструментов сразу: экономит ~2 запроса к FMP
на инструмент (~30 запросов на полный прогон по 15 инструментам). По
умолчанию включено ("1" или переменная не задана); cron-обёртка
(run_analyst_report.sh) выставляет "0" -- решение Леонида 5 сентября 2026,
часовой автоматический прогон дешевле по лимиту FMP, ручной запуск видит
полную картину.
"""
from __future__ import annotations

import os
from pathlib import Path

from agents.analyst_agent import build_verdict, format_verdict
from agents.data_agent import load_fmp_daily
from agents.dispatch_agent import AnalysisBundle, Recipient, send_via_telegram
from agents.fibo_agent import build_global_fibo, find_fractal_swing_extremes, find_oldest_unbroken_extremes
from agents.intraday_agent import get_intraday_confirmations
from agents.price_behavior_agent import nearest_level, recent_level_events
from agents.verification_agent import cross_check_structures, run_checklist
from screener import INSTRUMENTS, Instrument

ROOT = Path(__file__).resolve().parent

# Точечный watch по IBM живёт отдельной веткой в orchestrator.py, не входит
# в screener.INSTRUMENTS -- добавляем его сюда первым, чтобы полный отчёт
# покрывал ровно то же множество инструментов, что реально отслеживается
# ботом (см. CLAUDE.md, таблица "Что / Как часто").
IBM = Instrument("IBM", "IBM", "NASDAQ/NYSE (US)")
ALL_INSTRUMENTS: list[Instrument] = [IBM] + list(INSTRUMENTS)

# По умолчанию включено (полный анализ). Cron-обёртка (run_analyst_report.sh)
# выставляет "0" -- 5 сентября 2026, по решению Леонида: часовой прогон по
# всем 15 инструментам без внутридневных проверок примерно вдвое дешевле по
# лимиту FMP (не тратит 2 дополнительных запроса на инструмент), а
# ручной/разовый запуск (без этой переменной) по-прежнему видит полную картину.
INCLUDE_INTRADAY = os.environ.get("ANALYST_REPORT_INCLUDE_INTRADAY", "1") != "0"


def analyze_instrument(instrument: Instrument, include_intraday: bool = True) -> dict:
    """
    Один инструмент, целиком: Data -> Structure/Fibo -> Price-Behavior ->
    Verification -> независимая сверка -> (опционально) внутридневное
    подтверждение -> Analyst. Намеренно НЕ переиспользует
    screener.scan_instrument() -- тому нужен только формат. строка
    consensus_note, а здесь нужен сырой ConsensusResult для build_verdict();
    дублирует несколько строк его тела, но не меняет и не трогает сам
    screener.py (production-код, от которого зависит боевой cron).

    Никогда не бросает исключение наружу -- любая ошибка (сеть, тикер,
    структура не подтвердилась) превращается в статус в словаре, чтобы
    один проблемный инструмент не обрывал отчёт по остальным (регламент,
    раздел 2).
    """
    try:
        series = load_fmp_daily(instrument.symbol, exchange_hint=instrument.exchange_hint)
    except Exception as e:
        return {"instrument": instrument, "status": "DATA_ERROR", "detail": str(e)}
    try:
        structure = build_global_fibo(series, extremes_fn=find_oldest_unbroken_extremes)
    except ValueError as e:
        return {"instrument": instrument, "status": "NO_STRUCTURE", "detail": str(e)}

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

    # Внутридневное подтверждение -- 2 сетевых запроса на инструмент (см.
    # intraday_agent.py про стоимость). Этот отчёт запускается вручную/по
    # отдельному расписанию (не раз в 15 минут), поэтому включено по
    # умолчанию -- в отличие от screener.py, где это было бы неоправданно
    # на каждом часовом прогоне по всем 14 инструментам.
    intraday_confirmations = None
    if include_intraday:
        try:
            intraday_confirmations = get_intraday_confirmations(
                series.symbol, current_price, exchange_hint=series.exchange_or_source
            )
        except Exception:
            intraday_confirmations = None

    verdict = build_verdict(
        bundle,
        consensus_agree=consensus_agree,
        consensus_detail=consensus_detail,
        intraday_confirmations=intraday_confirmations,
    )

    return {"instrument": instrument, "status": "OK", "bundle": bundle, "verdict": verdict}


def build_digest(results: list[dict]) -> str:
    """Один текстовый дайджест по всем инструментам -- заголовок + вердикт
    на каждый, включая те, что не удалось проанализировать (честно, не
    молча пропускаем -- регламент, раздел 2)."""
    ok = [r for r in results if r["status"] == "OK"]
    problems = [r for r in results if r["status"] != "OK"]

    lines = [
        f"📋 <b>Полный анализ -- {len(results)} инструмент(ов)</b>",
        f"Проанализировано: {len(ok)} · Пропущено: {len(problems)}",
        "",
    ]
    divider = "─" * 24
    cards = [format_verdict(r["verdict"], symbol=r["instrument"].symbol, display_name=r["instrument"].label) for r in ok]
    lines.append(f"\n{divider}\n".join(cards))

    if problems:
        lines.append("")
        lines.append("<b>Пропущено (нет данных/структуры):</b>")
        for r in problems:
            lines.append(f"• {r['instrument'].label} [{r['instrument'].symbol}]: {r['status']} -- {r['detail']}")

    return "\n".join(lines)


def main() -> None:
    print("=" * 70)
    print(f"ANALYST REPORT -- {len(ALL_INSTRUMENTS)} инструментов (IBM + скринер)")
    print("=" * 70)

    if not INCLUDE_INTRADAY:
        print("(ANALYST_REPORT_INCLUDE_INTRADAY=0 -- внутридневное подтверждение пропущено, экономим лимит FMP)")
    results = [
        analyze_instrument(instrument, include_intraday=INCLUDE_INTRADAY) for instrument in ALL_INSTRUMENTS
    ]

    for r in results:
        inst = r["instrument"]
        if r["status"] == "OK":
            v = r["verdict"]
            print(f"  {inst.label:30s} [{inst.symbol:10s}] {v.call:15s} {v.confidence}")
        else:
            print(f"  {inst.label:30s} [{inst.symbol:10s}] {r['status']}: {r['detail']}")

    digest = build_digest(results)

    out_path = ROOT / "output" / "last_analyst_report.txt"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(digest, encoding="utf-8")
    print()
    print(f"Дайджест сохранён в {out_path}")

    # Двойное явное согласие на реальную отправку -- см. докстринг файла.
    # Без обеих переменных всегда DRY RUN, даже если TELEGRAM_BOT_TOKEN
    # случайно оказался в окружении (например, после `source .env` для
    # другой задачи).
    send_real = bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and os.environ.get("ANALYST_REPORT_SEND_REAL") == "1"
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if send_real else None

    recipients = [
        Recipient(label="Леонид (@vaskodevasko)", telegram_chat_id="885989790"),
        Recipient(label="Сергей (@sergikvsl)", telegram_chat_id="1253087193"),
        Recipient(label="Pavel", telegram_chat_id="980723803"),
    ]
    result = send_via_telegram(digest, recipients, bot_token=bot_token)
    print()
    print(f"--- Отправка дайджеста (dry_run={result['dry_run']}) ---")
    for entry in result["sent_to"]:
        print(f"  {entry['recipient']}: {entry['status']}")


if __name__ == "__main__":
    main()
