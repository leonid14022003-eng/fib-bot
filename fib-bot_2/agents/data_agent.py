"""
Data Agent
==========
Единственная задача этого агента — отдать чистые, реальные OHLC-свечи
с точным источником и таймфреймом, без какой-либо интерпретации
(регламент claude/fibonacci-reglament.md, разделы 3-4).

Два источника в этом файле:

1. load_ibm_demo_daily() -- демо-данные Alpha Vantage (сохранены в
   data/ibm_daily_raw.txt), использовались, пока не было платного ключа.
   Оставлено для регрессионных прогонов / офлайн-тестов.

2. load_fmp_daily() -- РЕАЛЬНЫЙ источник для продакшена. Леонид оплатил
   FMP Starter, ключ подтверждён рабочим 23 августа. ВАЖНО: FMP 27 августа
   2025 перевели весь API на новую структуру эндпоинтов /stable/ -- старые
   /api/v3/... теперь отдают 403 "Legacy Endpoint" даже с валидным ключом
   (это не ошибка ключа, а просто устаревший путь). Используем только
   /stable/.

   Эта функция сделана через requests.get -- НЕ через WebFetch. WebFetch
   пропускает контент через суммаризирующую модель (риск для точности
   чисел), а requests.get отдаёт сырой JSON напрямую. Из облачной песочницы
   Claude requests.get к financialmodelingprep.com не пройдёт (тот же
   allowlist, что блокировал alphavantage.co и api.telegram.org) -- эта
   функция рассчитана на запуск на VPS, где обычный интернет есть. Схема
   ответа проверена вручную 23 августа через WebFetch на реальных данных
   IBM и сверена 1:1 с Alpha Vantage за пересекающиеся даты -- совпало.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


@dataclass(frozen=True)
class Candle:
    dt: date
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class CandleSeries:
    """Свечи + обязательные метаданные источника (регламент, раздел 3-4)."""

    symbol: str
    exchange_or_source: str
    timeframe: str
    candles: list[Candle]  # отсортированы по возрастанию даты (старые -> новые)
    fetched_via: str
    fetch_note: str

    @property
    def start(self) -> date:
        return self.candles[0].dt

    @property
    def end(self) -> date:
        return self.candles[-1].dt


_LINE_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"open=(?P<open>[\d.]+)\s+"
    r"high=(?P<high>[\d.]+)\s+"
    r"low=(?P<low>[\d.]+)\s+"
    r"close=(?P<close>[\d.]+)\s+"
    r"volume=(?P<volume>\d+)\s*$"
)


def load_ibm_demo_daily(raw_path: str | Path) -> CandleSeries:
    """
    Парсит raw_path (сырой verbatim-текст, полученный от WebFetch) детерминированным
    регэкспом -- НЕ через LLM -- чтобы не добавлять ещё один слой риска
    транскрипции поверх уже сделанной WebFetch-выборки.
    """
    raw_path = Path(raw_path)
    text = raw_path.read_text(encoding="utf-8")

    candles: list[Candle] = []
    for line in text.splitlines():
        m = _LINE_RE.match(line.strip())
        if not m:
            continue
        candles.append(
            Candle(
                dt=datetime.strptime(m.group("date"), "%Y-%m-%d").date(),
                open=float(m.group("open")),
                high=float(m.group("high")),
                low=float(m.group("low")),
                close=float(m.group("close")),
                volume=int(m.group("volume")),
            )
        )

    if not candles:
        raise ValueError(f"Не удалось распарсить ни одной свечи из {raw_path}")

    candles.sort(key=lambda c: c.dt)

    # Санити-чек целостности: high >= max(open,close) и low <= min(open,close) на каждой свече.
    # Это не "анализ" -- это проверка, что сами сырые данные структурно валидны
    # (регламент делает то же самое неявно, требуя реальные тени свечей).
    bad = [c for c in candles if not (c.high >= max(c.open, c.close) and c.low <= min(c.open, c.close))]
    if bad:
        raise ValueError(f"Структурно некорректные свечи (high/low не огибают open/close): {bad}")

    return CandleSeries(
        symbol="IBM",
        exchange_or_source="Alpha Vantage TIME_SERIES_DAILY (demo key) -- NYSE-листинг IBM",
        timeframe="1D",
        candles=candles,
        fetched_via="WebFetch (bash curl к alphavantage.co заблокирован allowlist'ом песочницы)",
        fetch_note=(
            "Кросс-проверено двумя независимыми WebFetch-запросами "
            "(полный список свечей vs. точечный запрос max high/min low) -- совпали."
        ),
    )


def load_fmp_daily(
    symbol: str,
    api_key: str | None = None,
    months_back: int = 6,
    exchange_hint: str = "NASDAQ/NYSE (US)",
) -> CandleSeries:
    """
    Реальный источник для продакшена: Financial Modeling Prep, /stable/
    historical-price-eod/full. Требует пакет `requests` (pip install requests)
    и сетевой доступ -- рассчитано на запуск на VPS, не в песочнице Claude.

    api_key: если не передан явно, берётся из переменной окружения
    MARKET_DATA_API_KEY (см. .env.example).
    """
    import requests  # локальный импорт: пакета может не быть в песочнице Claude,
    # но он обязателен на VPS (requirements.txt)

    api_key = api_key or os.environ.get("MARKET_DATA_API_KEY")
    if not api_key:
        raise ValueError("Нет API-ключа: передай api_key или задай MARKET_DATA_API_KEY в окружении")

    today = date.today()
    date_from = today - timedelta(days=int(months_back * 30.44))  # ~месяцы в днях

    url = "https://financialmodelingprep.com/stable/historical-price-eod/full"
    params = {
        "symbol": symbol,
        "apikey": api_key,
        "from": date_from.isoformat(),
        "to": today.isoformat(),
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()

    if not isinstance(payload, list):
        # FMP отдаёт ошибки как {"Error Message": "..."} -- бросаем как есть,
        # НЕ пытаемся угадать/подставить данные вместо ошибки (регламент, раздел 2).
        raise ValueError(f"Неожиданный ответ FMP (ожидался массив свечей): {payload}")

    candles = [
        Candle(
            dt=datetime.strptime(row["date"], "%Y-%m-%d").date(),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=int(row["volume"]),
        )
        for row in payload
    ]
    candles.sort(key=lambda c: c.dt)

    if not candles:
        raise ValueError(f"FMP вернул пустой список свечей для {symbol} за {date_from}..{today}")

    bad = [c for c in candles if not (c.high >= max(c.open, c.close) and c.low <= min(c.open, c.close))]
    if bad:
        raise ValueError(f"Структурно некорректные свечи от FMP (high/low не огибают open/close): {bad}")

    return CandleSeries(
        symbol=symbol,
        exchange_or_source=f"Financial Modeling Prep /stable/historical-price-eod (Starter plan) -- {exchange_hint}",
        timeframe="1D",
        candles=candles,
        fetched_via="requests.get (прямой HTTP, без суммаризирующего слоя)",
        fetch_note=f"Запрошен диапазон {date_from.isoformat()}..{today.isoformat()} ({months_back} мес.)",
    )


if __name__ == "__main__":
    series = load_ibm_demo_daily(Path(__file__).resolve().parent.parent / "data" / "ibm_daily_raw.txt")
    print(f"{series.symbol} [{series.exchange_or_source}] {series.timeframe}")
    print(f"Диапазон: {series.start} .. {series.end}, свечей: {len(series.candles)}")
    print(f"Источник получения: {series.fetched_via}")
    print(f"Заметка: {series.fetch_note}")
