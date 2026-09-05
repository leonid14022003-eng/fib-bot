"""
Data Agent
==========
Единственная задача этого агента — отдать чистые, реальные OHLC-свечи
с точным источником и таймфреймом, без какой-либо интерпретации
(регламент claude/fibonacci-reglament.md, разделы 3-4).

Источники в этом файле:
1. load_ibm_demo_daily() -- демо-данные Alpha Vantage (сохранены в
   data/ibm_daily_raw.txt), использовались, пока не было платного ключа.
   Оставлено для регрессионных прогонов / офлайн-тестов.
2. load_fmp_daily() -- РЕАЛЬНЫЙ источник для продакшена (дневные свечи).
   Леонид оплатил FMP Starter, ключ подтверждён рабочим 23 августа. ВАЖНО:
   FMP 27 августа 2025 перевели весь API на новую структуру эндпоинтов
   /stable/ -- старые /api/v3/... теперь отдают 403 "Legacy Endpoint" даже
   с валидным ключом (это не ошибка ключа, а просто устаревший путь).
   Используем только /stable/.
   Эта функция сделана через requests.get -- НЕ через WebFetch. WebFetch
   пропускает контент через суммаризирующую модель (риск для точности
   чисел), а requests.get отдаёт сырой JSON напрямую. Из облачной песочницы
   Claude requests.get к financialmodelingprep.com не пройдёт (тот же
   allowlist, что блокировал alphavantage.co и api.telegram.org) -- эта
   функция рассчитана на запуск на VPS, где обычный интернет есть. Схема
   ответа проверена вручную 23 августа через WebFetch на реальных данных
   IBM и сверена 1:1 с Alpha Vantage за пересекающиеся даты -- совпало.
3. load_fmp_intraday() -- НОВОЕ (2 сентября 2026), для раздела 7.3
   регламента (многотаймфреймовое подтверждение 1H/4H, см. докстринг ниже
   и agents/intraday_agent.py).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
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
    months_back: int = 60,
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


def load_binance_daily(
    symbol: str,
    market: str = "futures",
    limit: int = 1000,
) -> CandleSeries:
    """
    Реальный источник для крипто-инструментов -- Binance klines, дневной
    таймфрейм. Добавлено 5 сентября 2026, по запросу Леонида (расширение
    охвата бота на криптовалюты, не только акции из screener.py).

    ВАЖНО, в отличие от более раннего черновика binance-fib-bot/ (1
    сентября 2026, см. CLAUDE.md "Открытые пункты") -- ТАМ проверка шла из
    песочницы Claude, и Binance отвечал 451 (геоблок). Здесь функция
    рассчитана на запуск с VPS (как и load_fmp_daily) -- прямой curl с
    ЭТОГО сервера 5 сентября вернул 200 и реальные данные, геоблок здесь не
    действует. Если это когда-нибудь перестанет быть так (Binance изменит
    политику или сервер переедет) -- requests.raise_for_status() ниже
    честно бросит ошибку, а не тихо подставит пустые/старые данные.

    market="futures" -> fapi.binance.com (USDT-M perpetual и т.п., 762
    торгуемых пары на 5 сентября 2026), market="spot" -> api.binance.com.
    interval всегда "1d" -- тот же дневной таймфрейм, что у load_fmp_daily().

    limit -- сколько последних дневных свечей запросить (Binance отдаёт
    максимум 1500 за один вызов -- здесь дефолт 1000, с запасом ниже
    потолка, не проверялось программно, что именно 1500 -- предположение
    по публичной документации Binance).

    volume у Binance приходит строкой с плавающей точкой в БАЗОВОМ активе
    (не в USDT) -- Candle.volume типизирован int везде в проекте, поэтому
    дробная часть теряется при приведении. Для price_behavior_agent.py
    (сравнение объёма пробоя со средним ЗА ТОТ ЖЕ инструмент) это не
    критично -- отношения объёмов искажаются на пренебрежимую величину;
    точная сумма в мелких долях монеты честно не сохраняется, не
    выдаётся за неё.
    """
    import requests  # локальный импорт, как и у load_fmp_daily -- см. комментарий там

    base_url = (
        "https://fapi.binance.com/fapi/v1/klines"
        if market == "futures"
        else "https://api.binance.com/api/v3/klines"
    )
    params = {"symbol": symbol, "interval": "1d", "limit": limit}
    resp = requests.get(base_url, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()
    if not isinstance(payload, list):
        # Binance отдаёт ошибки как {"code": ..., "msg": "..."} -- бросаем как
        # есть, не пытаемся угадать/подставить данные вместо ошибки (регламент,
        # раздел 2).
        raise ValueError(f"Неожиданный ответ Binance (ожидался массив свечей): {payload}")
    candles = [
        Candle(
            dt=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).date(),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=int(float(row[5])),
        )
        for row in payload
    ]
    candles.sort(key=lambda c: c.dt)
    if not candles:
        raise ValueError(f"Binance вернул пустой список свечей для {symbol} ({market})")
    bad = [c for c in candles if not (c.high >= max(c.open, c.close) and c.low <= min(c.open, c.close))]
    if bad:
        raise ValueError(f"Структурно некорректные свечи от Binance (high/low не огибают open/close): {bad}")
    return CandleSeries(
        symbol=symbol,
        exchange_or_source=f"Binance {market} klines -- CRYPTO",
        timeframe="1D",
        candles=candles,
        fetched_via="requests.get (прямой HTTP к Binance)",
        fetch_note=f"interval=1d, limit={limit} (~{limit} последних дневных свечей)",
    )


# ---------------------------------------------------------------------------
# Внутридневные свечи (1H/4H) -- добавлено 2 сентября 2026, раздел 7.3
# регламента (многотаймфреймовое подтверждение, отзыв "брокера" 27 августа:
# "Дневной Fibo без подтверждения на младшем ТФ -- грубый инструмент").
#
# Отдельный Candle-класс, а не переиспользование обычного Candle: у
# Candle.dt тип date (только календарный день) -- корректно для дневных
# баров, но НЕПРИГОДНО для внутридневных, где несколько свечей в один и
# тот же день различаются только временем. Если бы часовые свечи
# складывались в обычный Candle, сортировка `candles.sort(key=lambda c:
# c.dt)` и проверка "5 баров слева" (раздел 8-9, agents/fibo_agent.py)
# перестали бы различать бары внутри одного дня -- реальный, а не
# гипотетический риск, замечен на этапе проектирования, до того как стал
# багом. IntradayCandle.dt -- datetime (дата + время), у остального —
# те же имена полей, что и у Candle, поэтому find_oldest_unbroken_extremes()
# и build_global_fibo() из fibo_agent.py подхватывают его без единого
# изменения -- они работают через доступ к атрибутам (.high/.low/.dt),
# а не через isinstance-проверку типа.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntradayCandle:
    dt: datetime  # дата И время бара (в отличие от Candle.dt -- только дата)
    open: float
    high: float
    low: float
    close: float
    volume: int


@dataclass(frozen=True)
class IntradayCandleSeries:
    symbol: str
    exchange_or_source: str
    timeframe: str  # "1H" или "4H"
    candles: list[IntradayCandle]
    fetched_via: str
    fetch_note: str

    @property
    def start(self) -> datetime:
        return self.candles[0].dt

    @property
    def end(self) -> datetime:
        return self.candles[-1].dt


# Раздел 7.3 регламента (добавлен 31 августа 2026 как рабочий дефолт Claude
# -- список окон по ТФ от команды Леонида, обещанный на 25 августа, так и
# не пришёл за 6+ дней). Потолок поиска задан в ТОРГОВЫХ днях в самом
# регламенте; здесь переведён в календарные с запасом (~1.5x на выходные +
# немного сверху на типичные праздники) -- это приближение, не точный
# биржевой календарь. Расхождение на пару дней не критично: сам потолок
# уже приближённый дефолт Claude, а не точное число от практикующих
# трейдеров команды -- как только оно придёт, имеет приоритет и полностью
# заменяет эти константы (тот же принцип, что и с временной цветовой
# палитрой графиков, раздел 16 регламента).
INTRADAY_LOOKBACK_DAYS: dict[str, int] = {
    "1hour": 23,  # ~15 торговых дней
    "4hour": 90,  # ~60 торговых дней
}


def load_fmp_intraday(
    symbol: str,
    interval: str,
    api_key: str | None = None,
    days_back: int | None = None,
    exchange_hint: str = "NASDAQ/NYSE (US)",
) -> IntradayCandleSeries:
    """
    Источник внутридневных свечей для раздела 7.3 (многотаймфреймовое
    подтверждение). Financial Modeling Prep, /stable/historical-chart/{interval}
    -- путь подтверждён по официальной документации FMP (см.
    claude/multi-agent-architecture.md, обновление от 2 сентября 2026):
    https://site.financialmodelingprep.com/developer/docs/stable/intraday-1-hour
    https://site.financialmodelingprep.com/developer/docs/stable/intraday-4-hour

    interval: буквально "1hour" или "4hour" -- ровно так подставляется в
    путь URL.

    ВАЖНО, ЧЕСТНО, ДО ПЕРВОГО БОЕВОГО ЗАПУСКА: путь эндпоинта подтверждён,
    но ТОЧНАЯ СХЕМА JSON-ОТВЕТА -- НЕТ (страница документации не показывает
    пример ответа). Ниже предполагается тот же плоский формат "список
    объектов с полем date", что уже подтверждён вживую для
    /stable/historical-price-eod/full (load_fmp_daily выше) и для
    /stable/economic-calendar (agents/context_agent.py, 26 августа) -- то
    есть это не случайная догадка, а наблюдаемый паттерн ВСЕХ остальных
    /stable/-эндпоинтов этого же плана FMP, просто конкретно для ЭТОГО
    эндпоинта живым запросом ещё не подтверждён. Функция:
      - сначала пробует разобрать ответ как плоский список [{"date": "...
        ЧЧ:ММ:СС", "open":..., ...}, ...];
      - если пришёл объект (dict), а не список -- пробует частые варианты
        обёртки (ключи "results"/"historical"/"data");
      - если НИЧЕГО из этого не подошло -- бросает ValueError с текстом
        реального ответа (обрезанным), а НЕ подставляет пустой список и не
        падает необъяснимо (регламент, раздел 2: честно показать проблему).
    ПЕРЕД тем, как полагаться на эту функцию в боевом алерте -- обязательно
    прогнать один реальный запрос и свериться с этим докстрингом (см.
    README/сопроводительное сообщение к деплою). Если реальная схема
    отличается -- функция при первом же вызове упадёт с понятной ошибкой,
    а не молча даст неверные точки ФИБО.
    """
    import requests  # локальный импорт, как и у load_fmp_daily выше

    api_key = api_key or os.environ.get("MARKET_DATA_API_KEY")
    if not api_key:
        raise ValueError("Нет API-ключа: передай api_key или задай MARKET_DATA_API_KEY в окружении")
    if interval not in ("1hour", "4hour"):
        raise ValueError(f"Неподдерживаемый интервал: {interval!r} (ожидается '1hour' или '4hour')")
    if days_back is None:
        days_back = INTRADAY_LOOKBACK_DAYS[interval]

    url = f"https://financialmodelingprep.com/stable/historical-chart/{interval}"
    params = {"symbol": symbol, "apikey": api_key}
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    payload = resp.json()

    if isinstance(payload, dict):
        for key in ("results", "historical", "data"):
            inner = payload.get(key)
            if isinstance(inner, list):
                payload = inner
                break
        else:
            raise ValueError(
                f"Неожиданный формат ответа FMP для {interval} {symbol} (объект без "
                f"распознанного списка внутри, ни 'results', ни 'historical', ни "
                f"'data') -- нужно свериться вживую с реальным curl: {str(payload)[:300]}"
            )
    if not isinstance(payload, list):
        raise ValueError(f"Неожиданный ответ FMP для {interval} {symbol} (ожидался список свечей): {str(payload)[:300]}")

    cutoff = datetime.now() - timedelta(days=days_back)
    candles: list[IntradayCandle] = []
    for row in payload:
        raw_dt = row.get("date") if isinstance(row, dict) else None
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(raw_dt, fmt)
                break
            except (TypeError, ValueError):
                continue
        if dt is None:
            raise ValueError(
                f"Не удалось разобрать поле даты/времени в ответе FMP для {interval} "
                f"{symbol}: {row!r} -- схема ответа не совпадает с ожидаемой "
                f"('YYYY-MM-DD HH:MM:SS'), нужно свериться с реальным curl."
            )
        if dt < cutoff:
            continue
        try:
            candles.append(
                IntradayCandle(
                    dt=dt,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(row.get("volume", 0) or 0),
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ValueError(
                f"Не удалось разобрать OHLC в ответе FMP для {interval} {symbol}: "
                f"{row!r} ({e})"
            ) from e
    candles.sort(key=lambda c: c.dt)
    if not candles:
        raise ValueError(
            f"FMP вернул пустой список внутридневных свечей для {symbol} ({interval}) "
            f"за последние {days_back} дней -- либо инструмент недоступен на этом "
            f"интервале, либо диапазон {days_back} дней слишком мал (для дальнейшего "
            f"использования нужен более длинный days_back)."
        )
    bad = [c for c in candles if not (c.high >= max(c.open, c.close) and c.low <= min(c.open, c.close))]
    if bad:
        raise ValueError(f"Структурно некорректные внутридневные свечи от FMP: {bad[:3]}")
    return IntradayCandleSeries(
        symbol=symbol,
        exchange_or_source=(
            f"Financial Modeling Prep /stable/historical-chart/{interval} "
            f"(Starter plan) -- {exchange_hint}"
        ),
        timeframe="1H" if interval == "1hour" else "4H",
        candles=candles,
        fetched_via="requests.get (прямой HTTP, без суммаризирующего слоя)",
        fetch_note=f"Запрошено {days_back} дней назад, получено {len(candles)} свечей после отсечки",
    )


if __name__ == "__main__":
    series = load_ibm_demo_daily(Path(__file__).resolve().parent.parent / "data" / "ibm_daily_raw.txt")
    print(f"{series.symbol} [{series.exchange_or_source}] {series.timeframe}")
    print(f"Диапазон: {series.start} .. {series.end}, свечей: {len(series.candles)}")
    print(f"Источник получения: {series.fetched_via}")
    print(f"Заметка: {series.fetch_note}")
