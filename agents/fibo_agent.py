"""
Structure / Fibo Agent
=======================
Ищет подтверждённые HIGH/LOW строго по теням свечей (регламент, раздел 6),
строит глобальную (раздел 7.1) и черновую локальную (7.2) сетку Фибоначчи,
считает уровни (10-16).

КЛЮЧЕВОЙ момент, который легко перепутать (реализовано БУКВАЛЬНО по тексту
регламента, а не по "интуитивной" логике большинства торговых платформ):

  Восходящий ФИБО (раздел 11): точка 1 = LOW = 1 (100%), точка 2 = HIGH = 0 (0%).
  Нисходящий ФИБО (раздел 12): точка 1 = HIGH = 1 (100%), точка 2 = LOW = 0 (0%).

  То есть точка 1 -- это ВСЕГДА хронологически более ранний экстремум
  (начало движения), и она ВСЕГДА = 100%. Точка 2 -- хронологически более
  поздний экстремум (конец движения), и она ВСЕГДА = 0%. Это работает как
  "глубина отката от последнего экстремума в сторону начала движения": 0% --
  вообще без отката, 100% -- полный откат до начала движения.

  Для нисходящего движения это совпадает с привычной логикой большинства
  платформ (HIGH сверху = 100%, LOW снизу = 0%). А вот для ВОСХОДЯЩЕГО --
  это ПЕРЕВЁРНУТО относительно того, как обычно рисуют Fibo retracement на
  TradingView "по умолчанию" (там чаще 0%=низ, 100%=верх для аптренда).
  Это не баг, это то, что явно и многократно (разделы 8-13) написано в
  регламенте Леонида -- поэтому реализовано именно так. Если это не то,
  что реально имелось в виду, -- это стоит перепроверить с Леонидом,
  прежде чем пускать в бой (регламент, раздел 25: при неясности -- спросить,
  не менять методику молча; здесь неясности не было, было явное и
  повторённое условие, но раз оно противоречит распространённой практике,
  явно выношу на подтверждение отдельно от этого файла).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from enum import Enum

from agents.data_agent import Candle, CandleSeries

STANDARD_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786]
EXTENSION_LEVELS = [1.0, 1.414, 1.618, 2.0, 2.414, 2.618]
ALL_LEVELS = STANDARD_LEVELS + EXTENSION_LEVELS[1:]  # 1.0 уже есть неявно как "точка 1"

MIN_LEFT_BARS = 5  # регламент, разделы 8-9: не менее 5 свечей слева для подтверждения


class Direction(str, Enum):
    ASCENDING = "восходящий"
    DESCENDING = "нисходящий"


class StructureScope(str, Enum):
    GLOBAL = "глобальный"
    LOCAL = "локальный"


@dataclass(frozen=True)
class SwingPoint:
    dt: date
    price: float
    kind: str  # "HIGH" или "LOW"
    index: int  # индекс свечи в общем списке (для проверки confirmation)


@dataclass(frozen=True)
class FiboLevel:
    level: float
    price: float


@dataclass(frozen=True)
class FiboStructure:
    scope: StructureScope
    direction: Direction
    point1: SwingPoint  # = 1 (100%)
    point2: SwingPoint  # = 0 (0%)
    levels: list[FiboLevel]

    def price_at(self, level: float) -> float:
        p1, p2 = self.point1.price, self.point2.price
        return p2 + level * (p1 - p2)


def _confirmed_left(candles: list[Candle], idx: int, kind: str) -> bool:
    """Раздел 8-9: 'не менее 5 свечей слева от точки находились выше/ниже неё'
    -- буквально: среди всех свечей слева, минимум 5 штук (не обязательно
    подряд) должны быть по другую сторону от точки (выше для LOW, ниже для HIGH)."""
    left = candles[:idx]
    if len(left) < MIN_LEFT_BARS:
        return False
    target = candles[idx]
    if kind == "LOW":
        count = sum(1 for c in left if c.low > target.low)
    else:
        count = sum(1 for c in left if c.high < target.high)
    return count >= MIN_LEFT_BARS


def find_global_extremes(series: CandleSeries) -> tuple[SwingPoint, SwingPoint]:
    """Глобальный HIGH и LOW строго внутри окна свечей (раздел 7.1) -- никаких
    экстремумов за пределами переданного series.candles."""
    candles = series.candles
    hi_idx = max(range(len(candles)), key=lambda i: candles[i].high)
    lo_idx = min(range(len(candles)), key=lambda i: candles[i].low)

    hi = candles[hi_idx]
    lo = candles[lo_idx]

    if not _confirmed_left(candles, hi_idx, "HIGH"):
        raise ValueError(
            f"Глобальный HIGH {hi.dt}={hi.high} не проходит правило "
            f"'минимум {MIN_LEFT_BARS} свечей слева выше' (раздел 9) -- "
            f"экстремум слишком близко к левому краю окна, нужно расширить период."
        )
    if not _confirmed_left(candles, lo_idx, "LOW"):
        raise ValueError(
            f"Глобальный LOW {lo.dt}={lo.low} не проходит правило "
            f"'минимум {MIN_LEFT_BARS} свечей слева выше' (раздел 8) -- "
            f"экстремум слишком близко к левому краю окна, нужно расширить период."
        )

    return (
        SwingPoint(dt=hi.dt, price=hi.high, kind="HIGH", index=hi_idx),
        SwingPoint(dt=lo.dt, price=lo.low, kind="LOW", index=lo_idx),
    )


def find_fractal_swing_extremes(series: CandleSeries, window: int = MIN_LEFT_BARS) -> tuple[SwingPoint, SwingPoint]:
    """
    Альтернативный, НЕЗАВИСИМЫЙ способ найти глобальные HIGH/LOW -- не для
    замены find_global_extremes(), а для реальной сверки с ним (Verification
    Agent, 27 августа, по прямому запросу Леонида: "настоящая сверка
    структур", а не повтор одного и того же прогона на тех же данных, как
    было раньше -- см. verification_agent.py и orchestrator.py).

    find_global_extremes() берёт буквальный максимум/минимум всего окна,
    подтверждённый СЧЁТОМ баров слева (минимум 5 из ЛЮБОГО числа слева
    должны быть ниже/выше, раздел 8-9 регламента). Этот метод -- другой,
    классический алгоритм разворотных точек ("фрактал"/swing point): свеча
    считается подтверждённым свинг-хаем/лоу, только если она СТРОГО выше/ниже
    ВСЕХ `window` баров И СЛЕВА, И СПРАВА одновременно -- симметричное
    подтверждение с обеих сторон, а не счётное правило по одной стороне.
    Среди всех точек, подтверждённых этим способом, берётся самая крайняя.

    Из-за требования подтверждения СПРАВА последние `window` баров окна
    никогда не могут стать подтверждённой точкой этим методом -- это не
    ограничение по недосмотру, а честное и ожидаемое свойство классических
    фрактальных индикаторов (разворот виден только спустя `window` баров
    ПОСЛЕ него). find_global_extremes(), для сравнения, подтверждается
    только барами слева -- поэтому свежий, ещё не устоявшийся экстремум он
    может принять, а этот метод -- нет. Именно в этом и ценность сверки:
    если два метода расходятся, велика вероятность, что буквальный
    глобальный максимум/минимум -- это свежий, ещё не подтверждённый выброс,
    а не настоящая структурная точка разворота.

    Бросает ValueError, если не нашлось ни одной подтверждённой с обеих
    сторон точки (окно слишком короткое или слишком "гладкое", без явных
    разворотов) -- вызывающий код обязан это поймать и честно показать
    "сверка недоступна в этом прогоне", а не подставлять примерное значение
    (регламент, раздел 2).
    """
    candles = series.candles
    n = len(candles)
    confirmed_high_idx: list[int] = []
    confirmed_low_idx: list[int] = []

    for i in range(n):
        left = candles[max(0, i - window):i]
        right = candles[i + 1:i + 1 + window]
        if len(left) < window or len(right) < window:
            continue  # у самого края окна (особенно справа) фрактал не может подтвердиться
        target = candles[i]
        if all(c.high < target.high for c in left) and all(c.high < target.high for c in right):
            confirmed_high_idx.append(i)
        if all(c.low > target.low for c in left) and all(c.low > target.low for c in right):
            confirmed_low_idx.append(i)

    if not confirmed_high_idx or not confirmed_low_idx:
        raise ValueError(
            f"Не нашлось ни одной фрактальной точки разворота, подтверждённой с "
            f"обеих сторон (window={window}) в окне из {n} свечей -- независимая "
            f"сверка недоступна для этого прогона."
        )

    hi_idx = max(confirmed_high_idx, key=lambda i: candles[i].high)
    lo_idx = min(confirmed_low_idx, key=lambda i: candles[i].low)
    hi, lo = candles[hi_idx], candles[lo_idx]
    return (
        SwingPoint(dt=hi.dt, price=hi.high, kind="HIGH", index=hi_idx),
        SwingPoint(dt=lo.dt, price=lo.low, kind="LOW", index=lo_idx),
    )


def _is_unbroken_high(candles: list[Candle], idx: int) -> bool:
    """True, если ни одна свеча ПОСЛЕ idx не дала high выше candles[idx]."""
    target = candles[idx].high
    return all(c.high <= target for c in candles[idx + 1:])


def _is_unbroken_low(candles: list[Candle], idx: int) -> bool:
    """True, если ни одна свеча ПОСЛЕ idx не дала low ниже candles[idx]."""
    target = candles[idx].low
    return all(c.low >= target for c in candles[idx + 1:])


def find_oldest_unbroken_extremes(series: CandleSeries) -> tuple[SwingPoint, SwingPoint]:
    """
    Реализация правила «самый старый непробитый экстремум» (уточнение
    Леонида от 28 августа 2026, см. claude/fibonacci-reglament.md, раздел 7.1,
    и claude/brief.md). Заменяет идею фиксированного окна/количества баров на
    таймфрейм -- вместо этого ищет по свечам от старых к новым, пока не
    найдётся подтверждённый (правило раздела 8-9, минимум MIN_LEFT_BARS
    свечей слева) HIGH или LOW, который цена НИ РАЗУ не пробила вплоть до
    последней свечи в series. Если непробитых кандидатов несколько на разном
    удалении в прошлое -- побеждает самый старый (первый найденный при
    проходе от начала списка), а не самый свежий и не самый крупный.

    Пример, из-за которого появилось это правило: Pavel вручную построил ФИБО
    по Meta от ~годового хая 796,25, а не от хая, который бот нашёл в узком
    6-месячном окне -- бот физически не мог увидеть более старый хай, потому
    что тот был за пределами переданного окна. Это правило решает проблему на
    уровне выбора точек, а не размера окна: вызывающий код (data_agent.py)
    должен передавать достаточно длинную историю (см. months_back), а эта
    функция сама найдёт внутри неё правильную, давно не тронутую точку.

    Ожидает series.candles в хронологическом порядке (старые -> новые), как и
    везде в проекте. Бросает ValueError, а не выдуманную точку (раздел 2/25
    регламента), если внутри переданных свечей вообще нет ни одного
    подтверждённого и ни разу не пробитого HIGH или LOW -- это значит, что
    нужно расширить окно данных (в пределах согласованного потолка 5 лет) или
    уточнить диапазон у пользователя вручную.
    """
    candles = series.candles

    hi_idx = None
    for i in range(len(candles)):
        if _confirmed_left(candles, i, "HIGH") and _is_unbroken_high(candles, i):
            hi_idx = i
            break

    lo_idx = None
    for i in range(len(candles)):
        if _confirmed_left(candles, i, "LOW") and _is_unbroken_low(candles, i):
            lo_idx = i
            break

    if hi_idx is None:
        raise ValueError(
            "Не найден ни один подтверждённый HIGH, который цена ни разу не "
            f"пробила бы за весь переданный период ({len(candles)} свечей) -- "
            "нужно расширить окно данных (в пределах потолка 5 лет) или "
            "уточнить у пользователя диапазон вручную (раздел 5/25 регламента)."
        )
    if lo_idx is None:
        raise ValueError(
            "Не найден ни один подтверждённый LOW, который цена ни разу не "
            f"пробила бы за весь переданный период ({len(candles)} свечей) -- "
            "нужно расширить окно данных (в пределах потолка 5 лет) или "
            "уточнить у пользователя диапазон вручную (раздел 5/25 регламента)."
        )

    hi = candles[hi_idx]
    lo = candles[lo_idx]
    return (
        SwingPoint(dt=hi.dt, price=hi.high, kind="HIGH", index=hi_idx),
        SwingPoint(dt=lo.dt, price=lo.low, kind="LOW", index=lo_idx),
    )


def build_global_fibo(series: CandleSeries, extremes_fn=find_global_extremes) -> FiboStructure:
    """
    extremes_fn -- по умолчанию find_global_extremes() (буквальный метод
    регламента, разделы 7-9), НЕ изменилось для существующих вызовов.
    Передайте extremes_fn=find_fractal_swing_extremes для независимой
    сверки (см. verification_agent.py / orchestrator.py / screener.py,
    27 августа) -- та же функция строит структуру и уровни, меняется
    только способ поиска точек 1/2.
    """
    hi, lo = extremes_fn(series)

    # Направление -- по хронологии (см. Задача №1 в brief.md: "по их хронологии
    # определяется направление тренда"). Раздел 7 регламента не даёт более
    # формального критерия направления, чем это.
    if lo.index < hi.index:
        # LOW раньше HIGH по времени -> восходящее движение
        direction = Direction.ASCENDING
        point1, point2 = lo, hi  # LOW = 1, HIGH = 0 (раздел 11)
    else:
        # HIGH раньше LOW по времени -> нисходящее движение
        direction = Direction.DESCENDING
        point1, point2 = hi, lo  # HIGH = 1, LOW = 0 (раздел 12)

    return _build_structure(StructureScope.GLOBAL, direction, point1, point2)


def build_local_fibo(series: CandleSeries, lookback_bars: int) -> FiboStructure | None:
    """
    ЧЕРНОВАЯ реализация локального ФИБО (раздел 7.2). Регламент описывает
    локальный ФИБО качественно ("значимое локальное движение", "реальная
    подтверждённая структура"), но не даёт формального алгоритма выбора
    окна -- в отличие от глобального, где окно = весь запрошенный диапазон.

    Здесь я беру последние `lookback_bars` свечей как рабочее окно и внутри
    него применяю ТЕ ЖЕ правила (тени, confirmation слева), что и для
    глобального. Это рабочая гипотеза, не зафиксированное правило -- нужно
    сверить с Леонидом, прежде чем полагаться на неё в реальных сигналах.
    Возвращает None, если внутри окна нет структуры, проходящей confirmation.
    """
    candles = series.candles
    if len(candles) < lookback_bars:
        return None
    window = candles[-lookback_bars:]
    window_series = CandleSeries(
        symbol=series.symbol,
        exchange_or_source=series.exchange_or_source,
        timeframe=series.timeframe,
        candles=window,
        fetched_via=series.fetched_via,
        fetch_note=series.fetch_note,
    )
    try:
        return build_global_fibo(window_series)  # тот же алгоритм, локальное окно
    except ValueError:
        return None  # структура внутри окна не подтвердилась -- честно вернуть None,
        # а не подставлять что-то приблизительное


def _build_structure(
    scope: StructureScope, direction: Direction, point1: SwingPoint, point2: SwingPoint
) -> FiboStructure:
    levels = []
    for lvl in [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.414, 1.618, 2.0, 2.414, 2.618]:
        price = point2.price + lvl * (point1.price - point2.price)
        levels.append(FiboLevel(level=lvl, price=price))

    structure = FiboStructure(
        scope=scope, direction=direction, point1=point1, point2=point2, levels=levels
    )

    # Самопроверка раздела 10: точка 1 всегда 1, точка 2 всегда 0 -- это гарантировано
    # конструктивно (point1/point2 переданы уже в правильном порядке), но проверим explicit.
    # math.isclose, а не == -- p2 + 1.0*(p1-p2) не всегда бит-в-бит восстанавливает p1
    # из-за округления в двоичной арифметике (напр. GOOGL 120.21/252.41 -- расхождение ~1.4e-14).
    assert math.isclose(structure.price_at(1.0), point1.price, rel_tol=1e-9, abs_tol=1e-9)
    assert math.isclose(structure.price_at(0.0), point2.price, rel_tol=1e-9, abs_tol=1e-9)

    return structure
