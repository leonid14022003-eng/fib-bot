"""
Local Grid Agent (ИССЛЕДОВАТЕЛЬСКИЙ МОДУЛЬ, не часть боевого пайплайна)
========================================================================
Реализует алгоритм из PDF "Как строятся глобальная и локальные сетки"
(загружен Леонидом 9 сентября 2026, срез 04.09.2026 / реестр 08.09.2026):
без ATR, D1 по теням, цепочка локальных сеток после глобальной точки 2 с
переходом через 0% (продолжение) или 100% (разворот).

СТАТУС: по прямому решению Леонида (9 сентября 2026) это ОТДЕЛЬНЫЙ модуль,
НЕ подключённый к живому алертингу (dispatch_agent/orchestrator/screener).
Ничего отсюда не должно попасть в Telegram трём людям без отдельного
явного решения — см. CLAUDE.md, раздел "Никогда: не запускать ничего, что
реально шлёт в Telegram без явного разрешения".

Два осознанных отличия от боевого fibo_agent.py (обсуждено с Леонидом,
раздел 25 регламента — при конфликте методик молчать нельзя):

1. Глобальный экстремум здесь — БУКВАЛЬНЫЙ ATH/ATL всей переданной истории
   (Шаг 2 документа), БЕЗ проверки "минимум 5 баров слева" и БЕЗ правила
   "самый старый непробитый экстремум" (то правило — find_oldest_unbroken_
   extremes() в fibo_agent.py, используется в бою и здесь НЕ ЗАМЕНЯЕТСЯ).
   Тай-брейк при равной цене — самое раннее по времени вхождение.

2. Локальная сетка — не пересчёт "последних N баров" (как черновой
   build_local_fibo()), а полноценная цепочка с памятью: формирование с
   плавающей точкой 2 сразу после точки 1 (Шаг 4-5) → подтверждение
   двусвечным закреплением за локальными 50% (Шаг 6) → фиксация (Шаг 7) →
   выход тем же двусвечным закреплением, но за границей диапазона 0-100%
   (Шаг 8) → разворот через 100% (Шаг 9) или продолжение через 0%
   (Шаг 10), и так далее по цепочке.

УТОЧНЕНИЯ Леонида от 9 сентября 2026 (тот же день, после первой версии
модуля и живой сверки на CRWV) — зафиксированы, не пересматривать без
нового прямого указания:
  - Шаг 4: первая локальная запускается СРАЗУ после точки 2 глобальной
    сетки, без ожидания глобальных 50% (раньше был отдельный статус
    "ожидание" -- убран целиком, состояния WAITING больше нет).
  - Шаг 6: вместо правила "минимум 3 свечи возраста экстремума" --
    двусвечное "закрепление" за локальными 50%: свеча А пробивает уровень
    ТЕНЬЮ (low/high, закрытие не обязано быть за уровнем), следующая
    свеча Б ЗАКРЫВАЕТСЯ за тем же уровнем -- подтверждение фиксируется на
    свече Б. Не обязательно 3 свечи, не обязательно подряд начиная с
    точки 2 -- пара ищется заново после каждого нового экстремума и после
    каждой неподтвердившейся попытки.
  - Шаг 8: тот же двусвечный принцип, но за границей диапазона (0% или
    100%) вместо локальных 50% -- сетка не завершается по одной свече с
    тенью за границей (это по-прежнему только "тест", ровно как в Шаге 6),
    нужна следующая свеча с ЗАКРЫТИЕМ за той же границей. До подтверждения
    её уровни продолжают считаться действующими.

Все функции — чистые (CandleSeries -> результат), без сети, без записи в
alert_state.json/screener_state.json (общая память боевого бота не трогается).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

from agents.data_agent import Candle, CandleSeries

LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]


class Direction(str, Enum):
    UP = "восходящий"
    DOWN = "нисходящий"


class LocalState(str, Enum):
    FORMING = "формирование"    # Шаг 4-5: запущена сразу после точки 1, точка 2 предварительная/плавающая
    FIXED = "зафиксирована"     # Шаг 7: P1/P2/уровни неизменны, ждём двусвечного закрепления за диапазоном
    EXITED = "завершена"        # Шаг 8: двусвечное закрепление за 0% или 100%


@dataclass(frozen=True)
class ExtremePoint:
    dt: date
    price: float
    kind: str  # "HIGH" или "LOW"
    index: int  # индекс свечи в исходном CandleSeries.candles


def level_prices(point1_price: float, point2_price: float) -> dict[float, float]:
    """Цена уровня r = P2 + (P1 - P2) * r (документ, Шаг 3). Без логарифмов."""
    return {r: point2_price + (point1_price - point2_price) * r for r in LEVELS}


@dataclass(frozen=True)
class GlobalGrid:
    direction: Direction
    point1: ExtremePoint  # хронологически раньше, 100%
    point2: ExtremePoint  # хронологически позже, 0%
    levels: dict[float, float]


def find_global_grid(series: CandleSeries) -> GlobalGrid:
    """
    Шаг 2-3: буквальный ATH/ATL всей переданной истории (не окно, не
    "самый старый непробитый экстремум" — это сознательное отличие от
    find_oldest_unbroken_extremes() в fibo_agent.py, см. докстринг модуля).
    Тай-брейк при равной цене — первое вхождение по времени (Шаг 2).

    Раскрывает ValueError, если ATH и ATL пришлись на одну и ту же свечу —
    документ прямо запрещает в этом случае однозначно направленную
    глобальную сетку (Шаг 2: "однозначно направленную глобальную сетку
    не принимаю").
    """
    candles = series.candles
    if len(candles) < 2:
        raise ValueError(f"Недостаточно свечей для глобальной сетки: {len(candles)}")

    hi_idx = 0
    for i in range(1, len(candles)):
        if candles[i].high > candles[hi_idx].high:
            hi_idx = i
    lo_idx = 0
    for i in range(1, len(candles)):
        if candles[i].low < candles[lo_idx].low:
            lo_idx = i

    if hi_idx == lo_idx:
        raise ValueError(
            f"ATH и ATL пришлись на одну свечу ({candles[hi_idx].dt}) -- "
            "однозначно направленную глобальную сетку построить нельзя (Шаг 2)."
        )

    hi = candles[hi_idx]
    lo = candles[lo_idx]

    if hi_idx < lo_idx:
        # HIGH раньше LOW по времени -> нисходящее движение
        direction = Direction.DOWN
        point1 = ExtremePoint(hi.dt, hi.high, "HIGH", hi_idx)
        point2 = ExtremePoint(lo.dt, lo.low, "LOW", lo_idx)
    else:
        direction = Direction.UP
        point1 = ExtremePoint(lo.dt, lo.low, "LOW", lo_idx)
        point2 = ExtremePoint(hi.dt, hi.high, "HIGH", hi_idx)

    return GlobalGrid(direction, point1, point2, level_prices(point1.price, point2.price))


@dataclass(frozen=True)
class LocalGrid:
    seq: int  # номер локальной в цепочке, начиная с 1
    direction: Direction
    point1: ExtremePoint  # фиксирован с момента запуска этой локальной, 100%
    state: LocalState
    launch_date: date  # дата запуска (Шаг 4/9/10 -- переход в "формирование")
    point2_preliminary: ExtremePoint  # текущая/последняя точка 2 (плавает, пока не FIXED)
    confirmed_date: date | None = None  # Шаг 6
    point2_final: ExtremePoint | None = None  # заморожена в момент FIXED (Шаг 7)
    exit_date: date | None = None  # Шаг 8
    exit_border: str | None = None  # "0%" (продолжение) или "100%" (разворот)
    exit_price: float | None = None

    def levels(self) -> dict[float, float] | None:
        if self.point2_final is None:
            return None
        return level_prices(self.point1.price, self.point2_final.price)


@dataclass(frozen=True)
class LocalGridChainResult:
    global_grid: GlobalGrid
    completed: list[LocalGrid]  # локальные, у которых уже случился выход (Шаг 8)
    current: LocalGrid | None  # текущая незавершённая (FORMING/FIXED), если есть


def _extreme_value(candle: Candle, direction: Direction) -> tuple[float, str]:
    return (candle.high, "HIGH") if direction is Direction.UP else (candle.low, "LOW")


def _two_candle_confirmation(pending: bool, wick_breach: bool, close_breach: bool) -> tuple[bool, bool]:
    """
    Общий механизм "закрепления" для Шага 6 (за локальными 50%) и Шага 8
    (за границей диапазона 0-100%), уточнено Леонидом 9 сентября 2026:
    свеча А пробивает уровень ТЕНЬЮ (wick_breach) -> следующая свеча Б
    ЗАКРЫВАЕТСЯ за тем же уровнем (close_breach) -> подтверждено НА свече Б.

    pending -- пробила ли уровень тенью ПРЕДЫДУЩАЯ свеча (кандидат на роль
    "свечи А" для текущей "свечи Б"). Возвращает (подтверждено_на_этой_
    свече, новое_pending_для_следующей_свечи) -- новое pending всегда равно
    wick_breach ЭТОЙ свечи: она становится кандидатом "свечи А" для
    следующей проверки независимо от того, подтвердила ли она что-то сама
    (пара ищется заново, не только сразу после точки 1/фиксации).
    """
    confirmed = pending and close_breach
    return confirmed, wick_breach


def run_local_grid_chain(series: CandleSeries) -> LocalGridChainResult:
    """
    Шаги 4-10 целиком: от точки 2 глобальной сетки строит цепочку локальных
    сеток, проходя по свечам вперёд по времени один раз (walk-forward).

    Глобальная сетка вычисляется по ВСЕЙ переданной series (документ явно
    отмечает: "глобальная сетка зафиксирована ретроспективно на выбранном
    срезе" -- поэтому и здесь она стабильна на всю длину прогона, не
    пересчитывается на каждом шаге).
    """
    candles = series.candles
    global_grid = find_global_grid(series)
    p2 = global_grid.point2
    start_idx = p2.index + 1

    if start_idx >= len(candles):
        return LocalGridChainResult(global_grid, completed=[], current=None)

    seq = 1
    direction = Direction.UP if p2.kind == "LOW" else Direction.DOWN
    point1 = p2
    # Шаг 4 (уточнено 9 сентября 2026): формирование стартует СРАЗУ на точке 1,
    # без промежуточного статуса "ожидание" -- его больше нет.
    state = LocalState.FORMING
    launch_date = p2.dt
    ext_price, ext_dt, ext_index = p2.price, p2.dt, p2.index
    pending_retracement = False  # Шаг 6: пробила ли предыдущая свеча локальные 50% тенью
    pending_exit_border: str | None = None  # Шаг 8: "lower"/"upper"/None -- та же идея, за границей диапазона
    confirmed_date: date | None = None
    point2_final: ExtremePoint | None = None
    lower_bound = upper_bound = 0.0

    completed: list[LocalGrid] = []

    def _current_snapshot() -> LocalGrid:
        return LocalGrid(
            seq=seq,
            direction=direction,
            point1=point1,
            state=state,
            launch_date=launch_date,
            point2_preliminary=ExtremePoint(ext_dt, ext_price, "HIGH" if direction is Direction.UP else "LOW", ext_index),
            confirmed_date=confirmed_date,
            point2_final=point2_final,
        )

    i = start_idx
    while i < len(candles):
        c = candles[i]

        if state is LocalState.FORMING:
            val, _kind = _extreme_value(c, direction)
            if (direction is Direction.UP and val > ext_price) or (direction is Direction.DOWN and val < ext_price):
                ext_price, ext_dt, ext_index = val, c.dt, i
                pending_retracement = False  # новый экстремум -- любая незавершённая пара аннулируется
                i += 1
                continue
            mid = (point1.price + ext_price) / 2.0
            if direction is Direction.UP:
                wick_breach = c.low <= mid
                close_breach = c.close <= mid
            else:
                wick_breach = c.high >= mid
                close_breach = c.close >= mid
            confirmed, pending_retracement = _two_candle_confirmation(pending_retracement, wick_breach, close_breach)
            if confirmed:
                state = LocalState.FIXED
                confirmed_date = c.dt
                point2_final = ExtremePoint(ext_dt, ext_price, "HIGH" if direction is Direction.UP else "LOW", ext_index)
                lower_bound = min(point1.price, point2_final.price)
                upper_bound = max(point1.price, point2_final.price)
                pending_exit_border = None
            i += 1
            continue

        if state is LocalState.FIXED:
            breached_lower_wick = c.low < lower_bound
            breached_upper_wick = c.high > upper_bound
            if breached_lower_wick and breached_upper_wick:
                raise ValueError(
                    f"Свеча {c.dt} пробивает оба края локальной сетки №{seq} одновременно "
                    f"(low={c.low}, high={c.high}, диапазон [{lower_bound}, {upper_bound}]) -- "
                    "аномалия данных, честно останавливаюсь вместо угадывания (раздел 2)."
                )
            closed_lower = c.close < lower_bound
            closed_upper = c.close > upper_bound

            confirmed_border: str | None = None
            if pending_exit_border == "lower" and closed_lower:
                confirmed_border = "lower"
            elif pending_exit_border == "upper" and closed_upper:
                confirmed_border = "upper"

            if confirmed_border is None:
                pending_exit_border = "lower" if breached_lower_wick else "upper" if breached_upper_wick else None
                i += 1
                continue

            if direction is Direction.UP:
                border = "100%" if confirmed_border == "lower" else "0%"
            else:
                border = "0%" if confirmed_border == "lower" else "100%"
            exit_price = c.close

            finished = LocalGrid(
                seq=seq,
                direction=direction,
                point1=point1,
                state=LocalState.EXITED,
                launch_date=launch_date,
                point2_preliminary=point2_final,
                confirmed_date=confirmed_date,
                point2_final=point2_final,
                exit_date=c.dt,
                exit_border=border,
                exit_price=exit_price,
            )
            completed.append(finished)

            if border == "100%":
                # Шаг 9: разворот. Старая точка 2 -> новая точка 1. Направление меняется.
                new_point1 = point2_final
                new_direction = Direction.DOWN if direction is Direction.UP else Direction.UP
                new_val, _new_kind = _extreme_value(c, new_direction)
                seq += 1
                point1 = new_point1
                direction = new_direction
                ext_price, ext_dt, ext_index = new_val, c.dt, i
            else:
                # Шаг 10: продолжение через 0%. Ищем экстремум коррекции внутри
                # старого диапазона, после старой точки 2 и до свечи выхода (не включая её).
                window = candles[point2_final.index + 1: i]
                if not window:
                    raise ValueError(
                        f"Между точкой 2 локальной №{seq} ({point2_final.dt}) и выходом ({c.dt}) "
                        "нет ни одной свечи -- не могу найти экстремум коррекции для продолжения "
                        "(раздел 2: честно останавливаюсь, а не подставляю точку 2 как точку 1)."
                    )
                indexed_window = list(enumerate(window))
                if direction is Direction.UP:
                    offset, corr = min(indexed_window, key=lambda pair: pair[1].low)
                    corr_idx = point2_final.index + 1 + offset
                    new_point1 = ExtremePoint(corr.dt, corr.low, "LOW", corr_idx)
                else:
                    offset, corr = max(indexed_window, key=lambda pair: pair[1].high)
                    corr_idx = point2_final.index + 1 + offset
                    new_point1 = ExtremePoint(corr.dt, corr.high, "HIGH", corr_idx)
                new_direction = direction  # то же направление продолжается
                new_val, _new_kind = _extreme_value(c, new_direction)
                seq += 1
                point1 = new_point1
                direction = new_direction
                ext_price, ext_dt, ext_index = new_val, c.dt, i

            state = LocalState.FORMING
            launch_date = c.dt
            pending_retracement = False
            pending_exit_border = None
            confirmed_date = None
            point2_final = None
            i += 1
            continue

    current = None if state is LocalState.EXITED else _current_snapshot()
    return LocalGridChainResult(global_grid, completed, current)
