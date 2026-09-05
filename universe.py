"""
Universe
========
Строит РАСШИРЕННЫЙ список инструментов для opportunity_scanner.py -- в
отличие от screener.INSTRUMENTS (14 вручную отобранных инструментов),
здесь широкий, но всё ещё ОГРАНИЧЕННЫЙ охват: S&P 500 (акции) + топ-N пар
Binance по объёму за 24ч (крипта). Это НЕ "буквально все возможные
инструменты" -- у FMP 38854 тикера (акции+ETF+фонды вперемешку), у
Binance 762 фьючерсных пары -- по прямому решению Леонида 5 сентября
2026, после честного разбора рисков: у FMP Starter нет ни одного
заголовка в ответе про лимит запросов (не задокументирован), а сканировать
буквально всё рискует выжечь квоту и сломать уже боевые алерты (IBM watch
+ 14 инструментов + часовой аналитик) ради мусорных тикеров вроде
облигационных фондов.

S&P 500 -- СТАТИЧЕСКИЙ список (data/sp500_constituents.json), не
динамический: FMP-эндпоинт для S&P 500 (/stable/sp500-constituent)
закрыт тарифом (402 Payment Required на плане Starter -- та же картина,
что и с Nasdaq 100/WTI/газом в screener.py). Список получен 5 сентября
2026 прямым запросом к Wikipedia
(https://en.wikipedia.org/wiki/List_of_S%26P_500_companies) -- реальные
503 тикера на эту дату, НЕ воспроизведены по памяти модели (регламент,
раздел 2). Устаревает медленно (constituents S&P 500 меняются несколько
раз в год) -- чтобы обновить, повторить тот же скрейп и перезаписать
файл (см. git-историю коммита, добавившего этот файл, за точным кодом
парсинга).

Binance top-N -- ДИНАМИЧЕСКИЙ, считается заново при каждом вызове
(fapi.binance.com/fapi/v1/ticker/24hr, сортировка по quoteVolume) --
объёмы меняются постоянно, кэшировать список смысла нет.
"""
from __future__ import annotations

import json
from pathlib import Path

from screener import Instrument

ROOT = Path(__file__).resolve().parent
SP500_DATA_PATH = ROOT / "data" / "sp500_constituents.json"


def load_sp500_universe() -> list[Instrument]:
    """Читает статический файл -- без сети. Бросает исключение как есть,
    если файл повреждён/отсутствует (регламент, раздел 2 -- не подставлять
    пустой список молча)."""
    data = json.loads(SP500_DATA_PATH.read_text(encoding="utf-8"))
    return [
        Instrument(label=c["name"], symbol=c["symbol"], exchange_hint="NASDAQ/NYSE (US)", source="fmp")
        for c in data["constituents"]
    ]


def load_binance_top_universe(n: int = 200, market: str = "futures", quote_asset: str = "USDT") -> list[Instrument]:
    """
    Живой запрос к Binance 24hr ticker, топ-N пар по quoteVolume среди пар,
    котируемых в quote_asset (по умолчанию USDT -- самая ликвидная и
    единообразная база для сравнения объёмов между разными монетами).

    ВАЖНО (по прямому запросу Леонида, 5 сентября 2026 -- "чтобы это реально
    можно было торговать на Binance"): дополнительно сверяется с
    /fapi/v1/exchangeInfo и берёт ТОЛЬКО пары со status == "TRADING" --
    24hr ticker сам по себе не гарантирует, что пара торгуется прямо сейчас
    (могла быть приостановлена/делистнута, а статистика за 24ч -- устаревшей
    историей). На 5 сентября все топ-200 по объёму и так оказались TRADING,
    но это была бы случайность, если не проверять явно.
    """
    import requests

    base_url = (
        "https://fapi.binance.com/fapi/v1/ticker/24hr"
        if market == "futures"
        else "https://api.binance.com/api/v3/ticker/24hr"
    )
    exchange_info_url = (
        "https://fapi.binance.com/fapi/v1/exchangeInfo"
        if market == "futures"
        else "https://api.binance.com/api/v3/exchangeInfo"
    )

    resp = requests.get(base_url, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"Неожиданный ответ Binance 24hr ticker (ожидался массив): {data}")

    info_resp = requests.get(exchange_info_url, timeout=20)
    info_resp.raise_for_status()
    info = info_resp.json()
    trading_symbols = {s["symbol"] for s in info.get("symbols", []) if s.get("status") == "TRADING"}
    if not trading_symbols:
        raise ValueError(f"Неожиданный ответ Binance exchangeInfo (пустой список TRADING символов): {info}")

    filtered = [d for d in data if d["symbol"].endswith(quote_asset) and d["symbol"] in trading_symbols]
    filtered.sort(key=lambda d: float(d["quoteVolume"]), reverse=True)
    top = filtered[:n]
    return [
        Instrument(
            label=f"{d['symbol'][: -len(quote_asset)]}/{quote_asset}",
            symbol=d["symbol"],
            exchange_hint=f"Binance {market}",
            source="binance",
        )
        for d in top
    ]
