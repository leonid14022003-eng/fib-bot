"""
Opportunity Scanner
====================
Широкий скан ЗА ПРЕДЕЛАМИ 15 уже отслеживаемых инструментов (см.
analyst_report.ALL_INSTRUMENTS) -- ТОЛЬКО Binance (см. universe.py). По
прямому запросу Леонида, 5 сентября 2026: бот должен присылать
сообщение, когда анализ находит "интересную акцию либо криптовалюту,
которой нету в текущем списке, в которую можно сейчас вложиться, с
полным объяснением" -- уточнено в том же разговоре: только на Binance,
"нигде ещё", акции (S&P 500) из охвата убраны по прямому запросу.

Охват -- ВСЕ реально торгуемые (status="TRADING") пары к USDT на Binance
Futures (714 на 5 сентября 2026, см. universe.load_binance_top_universe),
а не только топ по объёму -- раз охват сузили до одной площадки, лимит
FMP (акции) больше не в игре, и есть запас гонять весь список Binance.

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

СТОИМОСТЬ: ~700+ запросов к Binance за один прогон, без внутридневного
подтверждения (сознательно выключено -- см. analyst_report.py про его
стоимость). НЕ подключено к crontab автоматически.
"""
from __future__ import annotations

import os

from agents.analyst_agent import BACKTEST_CAVEAT, CALL_BUY, CALL_SELL, analyze_series, format_verdict
from agents.data_agent import load_binance_daily
from agents.dispatch_agent import Recipient, _strip_html, send_document_via_telegram
from analyst_report import ALL_INSTRUMENTS as TRACKED_INSTRUMENTS
from screener import Instrument
from universe import load_binance_top_universe

BINANCE_TOP_N = 1000  # больше, чем реально существует TRADING-пар к USDT (714 на 5 сентября 2026) -- фактически "все"


def build_scan_universe(binance_top_n: int = BINANCE_TOP_N) -> list[Instrument]:
    """Все реально торгуемые (status=\"TRADING\") пары к USDT на Binance
    Futures, минус то, что уже отслеживается отдельно (IBM watch + 14
    инструментов скринера) -- нет смысла сообщать о том, что и так уже
    видно в существующих алертах. Акции (S&P 500) сознательно НЕ входят --
    по прямому решению Леонида 5 сентября 2026 ("только на Binance")."""
    tracked_symbols = {i.symbol for i in TRACKED_INSTRUMENTS}
    crypto = load_binance_top_universe(n=binance_top_n)
    return [i for i in crypto if i.symbol not in tracked_symbols]


def analyze_instrument(instrument: Instrument) -> dict:
    """
    Data (Binance) -> analyze_series() (agents/analyst_agent.py). Без
    внутридневного подтверждения (стоимость -- см. докстринг файла).

    Только Binance -- по прямому решению Леонида 5 сентября 2026. Если
    build_scan_universe() когда-нибудь снова начнёт отдавать инструменты с
    instrument.source != "binance", это честно упадёт в DATA_ERROR ниже
    (ValueError), а не молча возьмёт не тот источник.

    Никогда не бросает исключение наружу -- см. тот же принцип в
    analyst_report.analyze_instrument()/screener.scan_instrument()
    (регламент, раздел 2: один проблемный тикер не должен обрывать скан).
    """
    try:
        if instrument.source != "binance":
            raise ValueError(f"opportunity_scanner.py -- только Binance, получен source={instrument.source!r}")
        series = load_binance_daily(instrument.symbol, market="futures")
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
