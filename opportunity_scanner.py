"""
Opportunity Scanner
====================
Широкий скан ЗА ПРЕДЕЛАМИ 15 уже отслеживаемых инструментов (см.
analyst_report.ALL_INSTRUMENTS). По прямому запросу Леонида, 5 сентября
2026: бот должен присылать сообщение, когда анализ находит "интересную
акцию либо криптовалюту, которой нету в текущем списке, в которую можно
сейчас вложиться, с полным объяснением".

ОХВАТ (уточнено в том же разговоре, второй раз, 5 сентября 2026): берём
ПОЛНЫЙ список инструментов из раздела TradFi на Binance -- токенизированные
перпетуалы на акции/сырьё/индексы (XAUUSDT, AMDUSDT, NVDAUSDT, ...), но
АНАЛИЗ ведём на РЕАЛЬНОМ графике базового актива через FMP, а не на
истории цены самого Binance-токена (см. universe.load_binance_tradfi_universe
про то, как сопоставлены тикеры и что честно исключено -- часть TradFi
контрактов физически не имеет реального биржевого графика, доступного
через FMP Starter, или ещё непубличная компания). Binance-тикер при этом
остаётся тем местом, где реально торговать -- он показан в каждой
карточке в строке "Где торговать" (source_tag несёт информацию об обоих:
и о реальном графике, и о Binance-контракте).

ЭТО ЗАМЕНИЛО прежний охват (топ-200/топ-1000 обычных крипто-пар Binance,
universe.load_binance_top_universe) -- по прямому решению Леонида "в
дальнейшем для анализа будем брать только следующие инструменты"
(TradFi-раздел). Чистая крипта (BTC, ETH, SOL и т.п. без TradFi-статуса)
больше НЕ входит в этот скан.

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

СТОИМОСТЬ: ~155 запросов к FMP за один прогон (данные реальных акций/
сырья, не Binance), без внутридневного подтверждения (сознательно
выключено -- см. analyst_report.py про его стоимость). НЕ подключено к
crontab автоматически.
"""
from __future__ import annotations

import os

from agents.analyst_agent import BACKTEST_CAVEAT, CALL_BUY, CALL_SELL, analyze_series, format_verdict
from agents.data_agent import load_binance_daily, load_fmp_daily
from agents.dispatch_agent import Recipient, _strip_html, send_document_via_telegram
from analyst_report import ALL_INSTRUMENTS as TRACKED_INSTRUMENTS
from screener import Instrument
from universe import load_binance_tradfi_universe


def build_scan_universe() -> list[Instrument]:
    """TradFi-сопоставленные инструменты Binance (реальный график через
    FMP), минус то, что уже отслеживается отдельно (IBM watch + 14
    инструментов скринера) -- нет смысла сообщать о том, что и так уже
    видно в существующих алертах."""
    tracked_symbols = {i.symbol for i in TRACKED_INSTRUMENTS}
    tradfi, skipped = load_binance_tradfi_universe()
    for s in skipped:
        print(f"  (пропущено: {s['binance_symbol']} -- {s['reason']})")
    return [i for i in tradfi if i.symbol not in tracked_symbols]


def analyze_instrument(instrument: Instrument) -> dict:
    """
    Data (по instrument.source) -> analyze_series() (agents/analyst_agent.py).
    Без внутридневного подтверждения (стоимость -- см. докстринг файла).

    source="fmp" -- основной путь сейчас (TradFi-сопоставленные инструменты,
    реальный график через FMP, см. universe.load_binance_tradfi_universe).
    source="binance" -- оставлено для обычных крипто-пар (universe.load_binance_top_universe),
    если когда-нибудь снова понадобится.

    Никогда не бросает исключение наружу -- см. тот же принцип в
    analyst_report.analyze_instrument()/screener.scan_instrument()
    (регламент, раздел 2: один проблемный тикер не должен обрывать скан).
    """
    try:
        if instrument.source == "binance":
            series = load_binance_daily(instrument.symbol, market="futures")
        elif instrument.source == "fmp":
            series = load_fmp_daily(instrument.symbol, exchange_hint=instrument.exchange_hint)
        else:
            raise ValueError(f"opportunity_scanner.py -- неизвестный source={instrument.source!r}")
    except Exception as e:
        return {"instrument": instrument, "status": "DATA_ERROR", "detail": str(e)}

    try:
        bundle, verdict = analyze_series(series)
    except ValueError as e:
        return {"instrument": instrument, "status": "NO_STRUCTURE", "detail": str(e)}

    return {"instrument": instrument, "status": "OK", "bundle": bundle, "verdict": verdict}


def find_opportunities(results: list[dict]) -> list[dict]:
    return [r for r in results if r["status"] == "OK" and r["verdict"].call in (CALL_BUY, CALL_SELL)]


def _format_card(r: dict) -> str:
    return _strip_html(
        format_verdict(
            r["verdict"],
            symbol=r["instrument"].symbol,
            display_name=r["instrument"].label,
            source_tag=r["bundle"].source_tag,
        )
    )


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

    Разбито на два раздела -- ПОКУПКА отдельно, ПРОДАЖА отдельно -- по
    прямому запросу Леонида в том же разговоре ("покупку отдельным
    столбом, продажу отдельным столбом").
    """
    buys = [r for r in opportunities if r["verdict"].call == CALL_BUY]
    sells = [r for r in opportunities if r["verdict"].call == CALL_SELL]

    header = f"Новые возможности вне текущего списка -- {len(opportunities)}\n{'=' * 60}\n{BACKTEST_CAVEAT}\n"
    divider = "\n" + "-" * 40 + "\n"

    sections = []
    if buys:
        sections.append(f"\n### ПОКУПКА ({len(buys)}) ###\n" + divider.join(_format_card(r) for r in buys))
    if sells:
        sections.append(f"\n### ПРОДАЖА ({len(sells)}) ###\n" + divider.join(_format_card(r) for r in sells))

    return header + ("\n" + "=" * 60 + "\n").join(sections)


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
