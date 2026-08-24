"""
Price-Behavior Agent
=====================
Берёт текущую цену и построенную структуру ФИБО, классифицирует положение
и недавнее поведение цены относительно уровней (регламент, разделы 17-19).

Термины (раздел 17), которые размечает этот агент:
  тест, пробой (внутрисвечной / закрытие за уровнем / подтверждённый),
  закрепление, ретест, ложный пробой, отскок.

Ограничение сегодняшней версии: у нас только дневной таймфрейм (demo-ключ
не даёт внутридневных данных, см. брифинг 19 августа) -- поэтому
"внутрисвечной пробой" здесь трактуется как high/low свечи проходит уровень,
а "закрытие за уровнем" -- как close свечи проходит уровень. Это корректно
и на дневках, просто меньше свечей для наблюдения, чем было бы на часовике.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.data_agent import Candle, CandleSeries
from agents.fibo_agent import FiboStructure

TOUCH_TOLERANCE = 0.0015  # 0.15% от цены -- считаем это "на уровне" (тест), не "между"


@dataclass(frozen=True)
class NearestLevelInfo:
    current_price: float
    below_level: float | None
    below_price: float | None
    above_level: float | None
    above_price: float | None
    nearest_level: float
    nearest_price: float
    is_testing: bool  # цена практически на уровне (раздел 18, последний абзац)


@dataclass(frozen=True)
class RecentEvent:
    dt: str
    level: float
    level_price: float
    kind: str  # "тест" | "внутрисвечной пробой" | "закрытие за уровнем" | "ретест" | "ложный пробой"
    note: str


def nearest_level(structure: FiboStructure, current_price: float) -> NearestLevelInfo:
    """Раздел 18: ближайший уровень + между какими уровнями находится цена."""
    sorted_levels = sorted(structure.levels, key=lambda lv: lv.price)

    below = None
    above = None
    for lv in sorted_levels:
        if lv.price <= current_price:
            below = lv
        elif lv.price > current_price and above is None:
            above = lv

    candidates = [lv for lv in (below, above) if lv is not None]
    nearest = min(candidates, key=lambda lv: abs(lv.price - current_price))
    is_testing = abs(nearest.price - current_price) / current_price <= TOUCH_TOLERANCE

    return NearestLevelInfo(
        current_price=current_price,
        below_level=below.level if below else None,
        below_price=below.price if below else None,
        above_level=above.level if above else None,
        above_price=above.price if above else None,
        nearest_level=nearest.level,
        nearest_price=nearest.price,
        is_testing=is_testing,
    )


def recent_level_events(
    structure: FiboStructure, candles: list[Candle], lookback: int = 10
) -> list[RecentEvent]:
    """
    Проходит по последним `lookback` свечам и явно размечает взаимодействия
    с ключевыми уровнями (0.236/0.382/0.5/0.618/0.786) -- раздел 17.
    Не претендует на полноту (это первая рабочая версия классификатора,
    не формальный тула вроде TA-lib) -- если найдётся систематическая
    ошибка классификации на реальных данных, надо будет доработать вместе
    с Леонидом на конкретных примерах.
    """
    key_levels = [lv for lv in structure.levels if lv.level in (0.236, 0.382, 0.5, 0.618, 0.786)]
    events: list[RecentEvent] = []
    window = candles[-lookback:]

    for lv in key_levels:
        touched_below_then_closed_above = False
        for i, c in enumerate(window):
            crossed_down_intrabar = c.low < lv.price < c.high
            closed_below = c.close < lv.price < (window[i - 1].close if i > 0 else c.open)
            closed_above_after_below = (
                i > 0 and window[i - 1].close < lv.price and c.close > lv.price
            )

            if crossed_down_intrabar and c.low < lv.price and c.close >= lv.price:
                events.append(
                    RecentEvent(
                        dt=c.dt.isoformat(),
                        level=lv.level,
                        level_price=lv.price,
                        kind="тест",
                        note=(
                            f"low={c.low:.2f} прокалывал уровень {lv.level} ({lv.price:.2f}), "
                            f"но close={c.close:.2f} вернулся выше -- похоже на тест/отскок"
                        ),
                    )
                )
            if closed_above_after_below:
                events.append(
                    RecentEvent(
                        dt=c.dt.isoformat(),
                        level=lv.level,
                        level_price=lv.price,
                        kind="ложный пробой (вернулись выше после закрытия ниже)",
                        note=(
                            f"{window[i-1].dt} закрылись ниже {lv.level} ({lv.price:.2f}), "
                            f"{c.dt} закрылись выше -- цена не удержалась под уровнем"
                        ),
                    )
                )

    events.sort(key=lambda e: e.dt)
    return events
