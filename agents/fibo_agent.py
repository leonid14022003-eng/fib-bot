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


def build_global_fibo(series: CandleSeries) -> FiboStructure:
    hi, lo = find_global_extremes(series)

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
