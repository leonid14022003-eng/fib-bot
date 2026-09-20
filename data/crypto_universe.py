"""
Топ-N криптовалют по капитализации CoinGecko среди тех, что торгуются как
USDT perpetual на Binance Futures -- см. докстринг crypto_screener.py про
то, зачем и почему отдельно от opportunity_scanner.py.

В отличие от data/broad_universe.py (статический tradfi_universe.json,
портированный один раз) -- здесь список строится заново при каждом вызове,
двумя дешёвыми сетевыми запросами (CoinGecko + Binance exchangeInfo), без
файла-кэша. Причина: рыночная капитализация меняется, а хранить и
обновлять отдельный кэш-файл ради экономии одного HTTP-запроса в час не
стоит сложности -- сам fetch дешёвый и padает честной ошибкой при сбое
(регламент, раздел 2: не подставлять тихо старые/пустые данные вместо
честного отказа), а не тихо остаётся на прошлых данных, как это сделано в
десктопной FibonacciDesk 0.19.0 (README: "при отказе обновление не
заменяет последний успешный список" -- то поведение здесь сознательно НЕ
скопировано).
"""
from __future__ import annotations

import re

COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
BINANCE_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"

# Множитель контракта Binance для монет с очень низкой номинальной ценой
# (1000SHIBUSDT, 1000000BABYDOGEUSDT и т.п.) -- снимается при сопоставлении
# с CoinGecko. Не влияет на сам Фибо-анализ (проценты коррекции от диапазона
# свечей, не абсолютная цена) -- в отличие от десктопной FibonacciDesk, где
# множитель важен для сравнения цены с ценой на CoinGecko, здесь он
# сознательно не учитывается за пределами сопоставления тикеров.
_QTY_PREFIX_RE = re.compile(r"^(\d+)(?=[A-Z])")


def _base_asset(binance_symbol: str) -> str | None:
    """"1000SHIBUSDT" -> "SHIB", "BTCUSDT" -> "BTC". None если контракт не
    в USDT (сюда не должны попадать BUSD/USDC-контракты и т.п.)."""
    if not binance_symbol.endswith("USDT"):
        return None
    base = binance_symbol[: -len("USDT")]
    base = _QTY_PREFIX_RE.sub("", base)
    return base or None


def _fetch_binance_perpetuals() -> dict[str, list[str]]:
    """base_asset (без множителя) -> [полные Binance-символы] -- только
    status=TRADING, contractType=PERPETUAL, quoteAsset=USDT (то же условие,
    что описано в README FibonacciDesk 0.19.0)."""
    import requests  # локальный импорт, как и у load_binance_daily в data_agent.py

    resp = requests.get(BINANCE_EXCHANGE_INFO_URL, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if symbols is None:
        raise ValueError(f"Неожиданный ответ Binance exchangeInfo (нет ключа 'symbols'): {payload}")

    by_base: dict[str, list[str]] = {}
    for s in symbols:
        if s.get("status") != "TRADING":
            continue
        if s.get("contractType") != "PERPETUAL":
            continue
        if s.get("quoteAsset") != "USDT":
            continue
        base = _base_asset(s["symbol"])
        if base is None:
            continue
        by_base.setdefault(base, []).append(s["symbol"])
    return by_base


def _fetch_coingecko_markets(pages: int, per_page: int) -> list[dict]:
    """Топ по капитализации (order=market_cap_desc), публичный API без
    ключа -- та же конечная точка, что использует FibonacciDesk 0.19.0
    (см. README, раздел "Источники")."""
    import requests  # локальный импорт, тот же принцип, что и в data_agent.py

    out: list[dict] = []
    for page in range(1, pages + 1):
        resp = requests.get(
            COINGECKO_MARKETS_URL,
            params={"vs_currency": "usd", "order": "market_cap_desc", "per_page": per_page, "page": page},
            timeout=20,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not isinstance(batch, list):
            raise ValueError(f"Неожиданный ответ CoinGecko /coins/markets (ожидался массив): {batch}")
        out.extend(batch)
        if len(batch) < per_page:
            break
    return out


def build_crypto_universe(top_n: int = 50, candidate_pages: int = 3, per_page: int = 100) -> tuple[list[dict], list[dict]]:
    """
    Первые top_n монет CoinGecko по капитализации, у каждой из которых
    нашёлся РОВНО ОДИН однозначный Binance USDT-perpetual контракт --
    "top_n подходящих", а не первые top_n общего рейтинга (та же идея, что
    в README FibonacciDesk 0.19.0, раздел "Изменения 0.19.0").

    Возвращает (matched, skipped). matched -- список словарей
    {coingecko_id, name, symbol, binance_symbol, market_cap_rank}, в
    порядке убывания капитализации. skipped -- монеты из просмотренного
    диапазона рейтинга, которые НЕ попали в matched, с причиной (для
    логов/дебага, как и у data/broad_universe.py).

    candidate_pages*per_page должно быть заметно больше top_n -- часть
    верхних монет по капитализации не торгуется на Binance Futures вообще
    (стейблкоины, обёрнутые токены, монеты без USDT-перпа), поэтому чтобы
    набрать top_n ПОДХОДЯЩИХ, нужно просмотреть более широкий диапазон
    рейтинга.
    """
    binance_by_base = _fetch_binance_perpetuals()
    markets = _fetch_coingecko_markets(pages=candidate_pages, per_page=per_page)

    seen_symbol: set[str] = set()
    matched: list[dict] = []
    skipped: list[dict] = []

    for coin in markets:
        if len(matched) >= top_n:
            break
        symbol = (coin.get("symbol") or "").upper()
        if not symbol:
            continue
        if symbol in seen_symbol:
            # Тот же базовый тикер уже встречался выше в рейтинге (или уже
            # обработан на этом проходе) -- не сопоставляем повторно.
            continue
        seen_symbol.add(symbol)

        candidates = binance_by_base.get(symbol, [])
        if not candidates:
            skipped.append({"coingecko_id": coin.get("id"), "symbol": symbol, "reason": "нет Binance USDT perpetual"})
            continue
        if len(candidates) > 1:
            skipped.append({
                "coingecko_id": coin.get("id"), "symbol": symbol,
                "reason": f"неоднозначно -- несколько контрактов Binance: {sorted(candidates)}",
            })
            continue

        matched.append({
            "coingecko_id": coin.get("id"),
            "name": coin.get("name") or symbol,
            "symbol": symbol,
            "binance_symbol": candidates[0],
            "market_cap_rank": coin.get("market_cap_rank"),
        })

    # Строго по убыванию капитализации, как заявлено в докстринге. Обычно
    # это и так порядок обхода, НО ранг CoinGecko -- живые данные, и между
    # двумя последовательными постраничными запросами капитализация может
    # чуть сдвинуться на границе страницы (наблюдалось на практике: #50
    # пришёл раньше #51/#52 из-за разных запросов) -- сортировка здесь
    # устраняет этот сдвиг независимо от таймингов сети.
    matched.sort(key=lambda c: (c["market_cap_rank"] is None, c["market_cap_rank"]))
    return matched, skipped


if __name__ == "__main__":
    matched, skipped = build_crypto_universe()
    print(f"Подходящих: {len(matched)}")
    for c in matched:
        print(f"  #{c['market_cap_rank']:<5} {c['symbol']:<8} -> {c['binance_symbol']:<16} {c['name']}")
    print(f"Пропущено (из просмотренного диапазона рейтинга): {len(skipped)}")
    for s in skipped:
        print(f"  {s['symbol']}: {s['reason']}")
