"""
Opportunity Scanner
====================
Широкий скан ЗА ПРЕДЕЛАМИ 15 уже отслеживаемых инструментов (см.
analyst_report.ALL_INSTRUMENTS) -- S&P 500 + топ-N Binance по объёму (см.
universe.py про то, почему не "буквально всё"). По прямому запросу
Леонида, 5 сентября 2026: бот должен присылать сообщение, когда анализ
находит "интересную акцию либо криптовалюту, которой нету в текущем
списке, в которую можно сейчас вложиться, с полным объяснением".

Отличие от analyst_report.py: тот шлёт дайджест по ВСЕМ 15 инструментам
на КАЖДОМ запуске, даже если все ЖДАТЬ (модель "вот текущая картина").
Этот скрипт МОЛЧИТ, если не нашёл НИ ОДНОЙ возможности -- отправляет
только когда build_verdict() вернул ПОКУПКА или ПРОДАЖА (регламент,
раздел 2: не слать шум, честно молчать, когда сказать нечего).

НЕ заменяет и НЕ трогает существующие 15 инструментов, screener.py,
orchestrator.py -- полностью отдельный, дополнительный охват поверх той
же методики Фибо (пороги/правила построения структуры не менялись).

Запуск: MARKET_DATA_API_KEY=... python3 opportunity_scanner.py
По умолчанию DRY RUN. Реальная отправка -- то же двойное согласие, что и
у analyst_report.py: TELEGRAM_BOT_TOKEN + OPPORTUNITY_SCANNER_SEND_REAL=1.

СТОИМОСТЬ: ~500 (S&P 500) + N (Binance, дефолт 200) запросов за один
прогон -- на порядок больше, чем у остальных cron-джобов, и это без
внутридневного подтверждения (сознательно выключено везде в этом файле --
см. analyst_report.py про его стоимость). НЕ подключено к crontab
автоматически -- сначала нужно вживую замерить время прогона и убедиться,
что это не упирается в недокументированный лимit FMP, на небольшом
поднаборе, а не сразу на всех ~700 инструментах.
"""
from __future__ import annotations

import os

from agents.analyst_agent import CALL_BUY, CALL_SELL, analyze_series, format_verdict
from agents.data_agent import load_binance_daily, load_fmp_daily
from agents.dispatch_agent import Recipient, _strip_html, send_document_via_telegram
from analyst_report import ALL_INSTRUMENTS as TRACKED_INSTRUMENTS
from screener import Instrument
from universe import load_binance_top_universe, load_sp500_universe

BINANCE_TOP_N = 200


def build_scan_universe(binance_top_n: int = BINANCE_TOP_N) -> list[Instrument]:
    """S&P 500 + топ-N Binance, минус то, что уже отслеживается отдельно
    (IBM watch + 14 инструментов скринера) -- нет смысла сообщать о том,
    что и так уже видно в существующих алертах."""
    tracked_symbols = {i.symbol for i in TRACKED_INSTRUMENTS}
    sp500 = load_sp500_universe()
    crypto = load_binance_top_universe(n=binance_top_n)
    return [i for i in sp500 + crypto if i.symbol not in tracked_symbols]


def analyze_instrument(instrument: Instrument) -> dict:
    """
    Data (по instrument.source) -> analyze_series() (agents/analyst_agent.py).
    Без внутридневного подтверждения (стоимость -- см. докстринг файла).

    Никогда не бросает исключение наружу -- см. тот же принцип в
    analyst_report.analyze_instrument()/screener.scan_instrument()
    (регламент, раздел 2: один проблемный тикер не должен обрывать скан).
    """
    try:
        if instrument.source == "binance":
            series = load_binance_daily(instrument.symbol, market="futures")
        else:
            series = load_fmp_daily(instrument.symbol, exchange_hint=instrument.exchange_hint)
    except Exception as e:
        return {"instrument": instrument, "status": "DATA_ERROR", "detail": str(e)}

    try:
        bundle, verdict = analyze_series(series)
    except ValueError as e:
        return {"instrument": instrument, "status": "NO_STRUCTURE", "detail": str(e)}

    return {"instrument": instrument, "status": "OK", "bundle": bundle, "verdict": verdict}


def find_opportunities(results: list[dict]) -> list[dict]:
    return [r for r in results if r["status"] == "OK" and r["verdict"].call in (CALL_BUY, CALL_SELL)]


def build_opportunity_report(opportunities: list[dict]) -> str:
    """
    Единый ПРОСТОЙ текст (без HTML-тегов) со всеми найденными возможностями
    -- 5 сентября 2026, по прямому запросу Леонида ("хочу, чтобы это
    объединилось в одно сообщение"): вместо разбиения на несколько
    sendMessage (лимит 4096 символов, см. analyst_report.build_digest_messages)
    отчёт уходит ОДНИМ сообщением как файл-вложение (send_document_via_telegram) --
    там лимита на размер текста нет. format_verdict() собирает карточки с
    HTML-тегами для sendMessage -- здесь они сняты через _strip_html()
    (dispatch_agent.py), т.к. Telegram не рендерит HTML внутри содержимого
    файла-вложения, только в теле текстового сообщения.
    """
    header = f"Новые возможности вне текущего списка -- {len(opportunities)}\n{'=' * 60}\n"
    divider = "\n" + "-" * 40 + "\n"
    cards = [
        _strip_html(format_verdict(r["verdict"], symbol=r["instrument"].symbol, display_name=r["instrument"].label))
        for r in opportunities
    ]
    return header + divider.join(cards)


def main() -> None:
    universe = build_scan_universe()
    print("=" * 70)
    print(f"OPPORTUNITY SCANNER -- {len(universe)} инструментов вне текущего списка из 15")
    print("=" * 70)

    results = []
    for i, instrument in enumerate(universe, start=1):
        r = analyze_instrument(instrument)
        results.append(r)
        if r["status"] == "OK":
            print(f"  [{i}/{len(universe)}] {instrument.label:30s} [{instrument.symbol:14s}] {r['verdict'].call}")
        else:
            print(
                f"  [{i}/{len(universe)}] {instrument.label:30s} [{instrument.symbol:14s}] "
                f"{r['status']}: {r['detail']}"
            )

    opportunities = find_opportunities(results)
    print()
    print(f"Найдено возможностей (ПОКУПКА/ПРОДАЖА): {len(opportunities)} из {len(universe)}")

    if not opportunities:
        print("Ничего не отправляем -- нет возможностей вне текущего списка (регламент, раздел 2: не слать шум).")
        return

    report_text = build_opportunity_report(opportunities)

    send_real = bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and os.environ.get("OPPORTUNITY_SCANNER_SEND_REAL") == "1"
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if send_real else None
    recipients = [
        Recipient(label="Леонид (@vaskodevasko)", telegram_chat_id="885989790"),
        Recipient(label="Сергей (@sergikvsl)", telegram_chat_id="1253087193"),
        Recipient(label="Pavel", telegram_chat_id="980723803"),
    ]
    caption = f"🆕 Новых возможностей вне списка: {len(opportunities)} -- полный разбор во вложении"
    result = send_document_via_telegram(
        report_text.encode("utf-8"), recipients, bot_token=bot_token, caption=caption, filename="opportunities.txt"
    )
    print(f"--- Отправка (dry_run={result['dry_run']}, {result['document_bytes']} байт) ---")
    for entry in result["sent_to"]:
        print(f"  {entry['recipient']}: {entry['status']}")


if __name__ == "__main__":
    main()
