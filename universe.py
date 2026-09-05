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


# ---------------------------------------------------------------------------
# Binance TradFi perpetuals -> реальный биржевой график (не токенизированный)
# ---------------------------------------------------------------------------
# По прямому запросу Леонида, 5 сентября 2026: анализировать не сам
# Binance-токен (XAUUSDT, AMDUSDT и т.п.), а РЕАЛЬНЫЙ график базового
# актива (золото, акция AMD) через FMP -- Binance-тикер остаётся только как
# место, где реально торговать (24/7, в отличие от биржевых часов акций).
#
# Binance сам явно помечает такие контракты: exchangeInfo -> contractType
# == "TRADIFI_PERPETUAL", поле baseAsset -- это и есть тикер базового
# актива. Проверено вживую 5 сентября 2026: из 191 TRADIFI_PERPETUAL
# контракта у 8 baseAsset -- сырьё, у 156 -- акции США, у остальных
# (KR_EQUITY/HK_EQUITY/CN_EQUITY/PREMARKET, 33 штуки) -- корейские/
# гонконгские/китайские листинги или ещё непубличные компании.
#
# baseAsset НЕ всегда равен рабочему тикеру FMP -- проверено индивидуально,
# не предположено:
#   -- для сырья Binance использует короткие коды (XAU/XAG/CL/BZ/...), а у
#      FMP отдельная номенклатура (GCUSD/SIUSD/CLUSD/BZUSD/...) -- ДОКАЗАННАЯ
#      коллизия: FMP-тикеры "BZ" и "CL" резолвятся в РЕАЛЬНЫЕ, но НЕ ТЕ
#      компании (Kanzhun Limited и Colgate-Palmolive соответственно) --
#      если бы не проверили вручную, бот молча анализировал бы чужую акцию
#      под видом нефти;
#   -- аналогично "BYD" на FMP -- это Boyd Gaming (казино, NYSE), а не BYD
#      Company (китайские электромобили, HK_EQUITY на Binance) -- тоже
#      исключена;
#   -- часть тикеров (QNTX, STXX, BBX, весь KR_EQUITY/HK_EQUITY/CN_EQUITY
#      кроме BYD, весь PREMARKET) у FMP Starter вообще не резолвится в
#      исторические свечи (проверено запросом historical-price-eod, не
#      просто quote) -- честно исключены, а не заменены угадыванием
#      (регламент, раздел 2). QNTX (Quantinuum) -- отдельный случай: это
#      ДЕЙСТВИТЕЛЬНО ещё непубличная компания (подано на IPO, торгов на
#      бирже нет) -- у неё физически не может быть реального биржевого
#      графика, это не пробел в покрытии FMP, а сам факт.
_TRADFI_COMMODITY_MAP: dict[str, str] = {
    "XAU": "GCUSD",  # золото
    "XAG": "SIUSD",  # серебро
    "BZ": "BZUSD",  # нефть Brent
    # CL (WTI), NATGAS, COPPER, XPT, XPD -- FMP-эквиваленты (CLUSD/NGUSD/
    # HGUSD/PLUSD/PAUSD) существуют, но отвечают 402 Premium Query
    # Parameter на тарифе Starter -- проверено вживую 5 сентября 2026,
    # не в списке ниже, честно исключены.
}

_TRADFI_EQUITY_OVERRIDES: dict[str, str] = {
    "BRKB": "BRK-B",  # Berkshire Hathaway -- FMP использует дефис, не слитно
}

_TRADFI_EXCLUDE: dict[str, str] = {
    # Сырьё, недоступное на тарифе FMP Starter (402 Premium Query Parameter)
    "CL": "нефть WTI (CLUSD) -- 402 Premium Query Parameter на тарифе FMP Starter",
    "NATGAS": "природный газ (NGUSD) -- 402 Premium Query Parameter на тарифе FMP Starter",
    "COPPER": "медь (HGUSD) -- 402 Premium Query Parameter на тарифе FMP Starter",
    "XPT": "платина (PLUSD) -- 402 Premium Query Parameter на тарифе FMP Starter",
    "XPD": "палладий (PAUSD) -- 402 Premium Query Parameter на тарифе FMP Starter",
    # Акции США без надёжного соответствия у FMP
    "BBX": "не резолвится в FMP ни через quote, ни через historical-price-eod",
    "QNTX": "Quantinuum -- ещё непубличная компания (подано на IPO), реального биржевого графика не существует",
    "STXX": "не удалось найти достоверное соответствие ни через FMP, ни через веб-поиск",
    # KR/HK/CN -- FMP Starter не покрывает эти биржи (проверено вживую)
    "HANMI": "Корея (KOSPI) -- не покрывается FMP Starter",
    "HYUNDAI": "Корея (KOSPI) -- не покрывается FMP Starter",
    "KODEX200": "Корея (KOSPI, ETF) -- не покрывается FMP Starter",
    "LGELECTRONICS": "Корея (KOSPI) -- не покрывается FMP Starter",
    "NAVER": "Корея (KOSPI) -- не покрывается FMP Starter",
    "SAMSUNGEM": "Корея (KOSPI) -- не покрывается FMP Starter",
    "SAMSUNG": "Корея (KOSPI) -- не покрывается FMP Starter",
    "SKHYNIX": "Корея (KOSPI) -- не покрывается FMP Starter",
    "BYD": "FMP-тикер BYD -- это Boyd Gaming (NYSE, казино), НЕ BYD Company (китайские электромобили) -- ложное совпадение",
    "CSOPSAMSUNG2L": "Гонконг (HKEX, leveraged ETF) -- не покрывается FMP Starter",
    "CSOPSKHYNIX2L": "Гонконг (HKEX, leveraged ETF) -- не покрывается FMP Starter",
    "GIGADEV": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "HK0625": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "HK0700": "Гонконг (HKEX, Tencent) -- не покрывается FMP Starter",
    "HK0992": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "HK1810": "Гонконг (HKEX, Xiaomi) -- не покрывается FMP Starter",
    "KUAISHOU": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "MEITUAN": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "MINIMAX": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "POPMART": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "TENCENT": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "ZHIPU": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "ZHONGJI": "Гонконг (HKEX) -- не покрывается FMP Starter",
    "CXMT": "Китай (материковая биржа) -- не покрывается FMP Starter",
    "UNITREE": "Китай (материковая биржа) -- не покрывается FMP Starter",
    # PREMARKET -- ещё непубличные компании, реального графика не существует
    "ANTHROPIC": "непубличная компания, реального биржевого тикера нет",
    "OPENAI": "непубличная компания, реального биржевого тикера нет",
}


def load_binance_tradfi_universe(
    market: str = "futures", exchange_info: dict | None = None
) -> tuple[list[Instrument], list[dict]]:
    """
    Живой запрос к Binance /fapi/v1/exchangeInfo (если exchange_info не
    передан явно -- параметр для тестов, позволяет подставить фиктивный
    ответ вместо реального сетевого вызова, тот же принцип, что и
    fetch_fn в screener.scan_instrument()), фильтр по contractType ==
    "TRADIFI_PERPETUAL" и status == "TRADING". Для каждого контракта --
    реальный тикер базового актива на FMP (_TRADFI_EQUITY_OVERRIDES/
    _TRADFI_COMMODITY_MAP при необходимости, иначе baseAsset как есть).
    Всё, что не резолвится надёжно (см. _TRADFI_EXCLUDE, с указанием
    причины) -- честно пропускается, а не подставляется приблизительно
    (регламент, раздел 2).

    Возвращает (instruments, skipped) -- skipped это список
    {"binance_symbol", "base_asset", "reason"} для прозрачности (что и
    почему не попало в анализ), instruments -- Instrument с source="fmp"
    (анализ идёт на РЕАЛЬНОМ графике через load_fmp_daily), exchange_hint
    несёт Binance-тикер -- именно он должен появляться в карточке в строке
    "Где торговать" (format_verdict(source_tag=...)), поскольку торговать
    реально нужно на Binance, 24/7, а не на бирже, где котируется базовый
    актив.
    """
    if exchange_info is not None:
        info = exchange_info
    else:
        import requests

        base_url = (
            "https://fapi.binance.com/fapi/v1/exchangeInfo"
            if market == "futures"
            else "https://api.binance.com/api/v3/exchangeInfo"
        )
        resp = requests.get(base_url, timeout=20)
        resp.raise_for_status()
        info = resp.json()
    contracts = [
        s
        for s in info.get("symbols", [])
        if s.get("contractType") == "TRADIFI_PERPETUAL" and s.get("status") == "TRADING"
    ]
    if not contracts:
        raise ValueError(f"Неожиданный ответ Binance exchangeInfo (нет TRADIFI_PERPETUAL контрактов): {info}")

    instruments: list[Instrument] = []
    skipped: list[dict] = []
    seen_fmp_symbols: set[str] = set()

    for s in contracts:
        binance_symbol = s["symbol"]
        base_asset = s["baseAsset"]

        if base_asset in _TRADFI_EXCLUDE:
            skipped.append(
                {"binance_symbol": binance_symbol, "base_asset": base_asset, "reason": _TRADFI_EXCLUDE[base_asset]}
            )
            continue

        fmp_symbol = _TRADFI_COMMODITY_MAP.get(base_asset) or _TRADFI_EQUITY_OVERRIDES.get(base_asset) or base_asset

        # Один и тот же реальный актив иногда доступен на Binance под
        # несколькими TradFi-контрактами (например, SPCXUSDT и SPCXUSD1) --
        # берём первый встреченный, не дублируем анализ одного и того же
        # графика под двумя карточками.
        if fmp_symbol in seen_fmp_symbols:
            skipped.append(
                {
                    "binance_symbol": binance_symbol,
                    "base_asset": base_asset,
                    "reason": f"дубликат -- {fmp_symbol} уже покрыт другим Binance-контрактом",
                }
            )
            continue
        seen_fmp_symbols.add(fmp_symbol)

        instruments.append(
            Instrument(
                label=base_asset,
                symbol=fmp_symbol,
                exchange_hint=f"торговать на Binance TradFi ({binance_symbol})",
                source="fmp",
            )
        )

    return instruments, skipped
