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
    kind: str  # "тест" | "пробой (закрепление подтверждено)" | "ложный пробой (...)" | "ретест"
    note: str


# 27 августа, по запросу Леонида ("ловить реальные пробои") -- допуск для
# "ретеста" чуть шире TOUCH_TOLERANCE выше: после реального пробоя цена не
# обязана вернуться тик-в-тик к уровню, чтобы это считалось ретестом.
RETEST_TOLERANCE = 0.003

# 27 августа (продолжение, по отзыву "брокера" -- см. project doc, раздел
# про учёт объёма): пробой на низком объёме статистически менее надёжен,
# чем на всплеске -- это одна из первых вещей, на которую смотрит любой
# практик перед тем, как поверить пробою. VOLUME_LOOKBACK -- сколько
# предыдущих свечей брать для среднего; VOLUME_CONFIRM_MULTIPLIER --
# во сколько раз объём пробоя должен превышать это среднее, чтобы считаться
# подтверждённым (1.2x -- обычный, не экстремальный порог, первая рабочая
# версия, не финальная калибровка); VOLUME_MIN_PRIOR_BARS -- меньше свечей
# "до" -- среднее слишком шумное, честнее промолчать про объём, чем
# подставить ненадёжную цифру.
VOLUME_LOOKBACK = 20
VOLUME_CONFIRM_MULTIPLIER = 1.2
VOLUME_MIN_PRIOR_BARS = 5


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
    Не претендует на полноту (первая рабочая версия классификатора, не
    формальный тул вроде TA-lib) -- если найдётся систематическая ошибка
    классификации на реальных данных, надо будет доработать вместе с
    Леонидом на конкретных примерах.

    27 августа (по прямому запросу Леонида -- "ловить реальные пробои"):
    до этого момента функция умела различать только "тест" и один
    (несимметричный) вид "ложного пробоя" -- оба, по сути, варианты "цена
    подошла и отскочила". Если уровень реально пробивали и цена
    ЗАКРЕПЛЯЛАСЬ за ним -- то, что для трейдера обычно важнее всего --
    функция раньше молчала. Теперь добавлены:
      -- "пробой (закрепление подтверждено)" -- цена закрылась по другую
         сторону уровня и ни разу не вернулась обратно до конца
         рассмотренного окна;
      -- "ретест" -- после такого подтверждённого пробоя цена ещё раз
         подошла близко к уровню с новой стороны, не пересекая его обратно.
    "Тест" (внутрисвечная тень без закрытия за уровнем) не менялся. Старый
    "ложный пробой" был симметрично расширен: раньше ловилось только
    возвращение ВВЕРХ после закрытия ниже, теперь -- оба направления.
    """
    key_levels = [lv for lv in structure.levels if lv.level in (0.236, 0.382, 0.5, 0.618, 0.786)]
    events: list[RecentEvent] = []
    window = candles[-lookback:]
    window_start = len(candles) - len(window)  # для перевода индекса внутри window в индекс полного candles

    for lv in key_levels:
        # -- "тест": внутрисвечная тень пересекла уровень, но закрытие
        # вернулось на прежнюю (верхнюю) сторону -- условие не менялось.
        for c in window:
            crossed_intrabar = c.low < lv.price < c.high
            if crossed_intrabar and c.close >= lv.price:
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

        # -- Пробои по закрытию: находим ВСЕ close-to-close пересечения
        # уровня в обе стороны, затем для каждого смотрим ВПЕРЁД по тому
        # же окну -- вернулась ли цена обратно (= ложный пробой, в любую
        # из двух сторон) или нет (= пробой, закрепление подтверждено,
        # НАСКОЛЬКО ВИДНО в пределах этого окна -- за его пределами цена
        # ещё может развернуться, это не гарантия на будущее).
        confirmed_break_idx: int | None = None
        confirmed_break_dir: str | None = None
        for i in range(1, len(window)):
            prev_close, close = window[i - 1].close, window[i].close
            if prev_close < lv.price <= close:
                direction = "up"
            elif prev_close > lv.price >= close:
                direction = "down"
            else:
                continue

            if i == len(window) - 1:
                # Пересечение произошло на САМОЙ ПОСЛЕДНЕЙ свече окна -- ни
                # одной свечи ПОСЛЕ него ещё нет, значит нечем подтвердить
                # ни "закрепление", ни "вернулись обратно". Честнее промолчать
                # (регламент, раздел 2), чем поспешно назвать свежее
                # пересечение "подтверждённым пробоем" только потому, что
                # данных для проверки обратного разворота попросту ещё нет.
                continue

            c = window[i]
            reverted = any(
                (direction == "up" and window[j].close < lv.price)
                or (direction == "down" and window[j].close > lv.price)
                for j in range(i + 1, len(window))
            )
            if reverted:
                if direction == "up":
                    kind = "ложный пробой (вернулись выше после закрытия ниже)"
                    note = (
                        f"{window[i-1].dt} закрылись ниже {lv.level} ({lv.price:.2f}), "
                        f"{c.dt} закрылись выше -- цена не удержалась под уровнем"
                    )
                else:
                    kind = "ложный пробой (вернулись ниже после закрытия выше)"
                    note = (
                        f"{window[i-1].dt} закрылись выше {lv.level} ({lv.price:.2f}), "
                        f"{c.dt} закрылись ниже -- цена не удержалась над уровнем"
                    )
            else:
                side = "выше" if direction == "up" else "ниже"
                kind = "пробой (закрепление подтверждено)"
                note = (
                    f"{c.dt} закрылись {side} {lv.level} ({lv.price:.2f}) и не вернулись "
                    f"обратно до конца рассмотренного окна -- похоже на настоящий пробой"
                )
                confirmed_break_idx, confirmed_break_dir = i, direction

                # Объём пробоя vs среднее за VOLUME_LOOKBACK свечей ДО него --
                # берём из ПОЛНОГО candles (не только window), чтобы у среднего
                # было честное число свечей, даже если пробой близко к началу
                # рассматриваемого окна.
                full_idx = window_start + i
                prior = candles[max(0, full_idx - VOLUME_LOOKBACK):full_idx]
                if len(prior) >= VOLUME_MIN_PRIOR_BARS:
                    avg_volume = sum(p.volume for p in prior) / len(prior)
                    ratio = c.volume / avg_volume if avg_volume > 0 else 0.0
                    volume_confirmed = ratio >= VOLUME_CONFIRM_MULTIPLIER
                    note += (
                        f"; объём {c.volume:,} против среднего {avg_volume:,.0f} за "
                        f"{len(prior)} свечей ({ratio:.2f}x) -- объём "
                        f"{'подтверждает' if volume_confirmed else 'НЕ подтверждает'} пробой"
                    )

            events.append(
                RecentEvent(dt=c.dt.isoformat(), level=lv.level, level_price=lv.price, kind=kind, note=note)
            )

        # -- Ретест: после ПОСЛЕДНЕГО подтверждённого в этом окне пробоя
        # ищем более позднюю свечу, вернувшуюся близко к уровню (допуск
        # RETEST_TOLERANCE), но не пересёкшую его обратно по закрытию.
        if confirmed_break_idx is not None:
            for j in range(confirmed_break_idx + 1, len(window)):
                c = window[j]
                close_to_level = abs(c.close - lv.price) / lv.price <= RETEST_TOLERANCE
                still_on_break_side = (
                    (confirmed_break_dir == "up" and c.close >= lv.price)
                    or (confirmed_break_dir == "down" and c.close <= lv.price)
                )
                if close_to_level and still_on_break_side:
                    events.append(
                        RecentEvent(
                            dt=c.dt.isoformat(),
                            level=lv.level,
                            level_price=lv.price,
                            kind="ретест",
                            note=(
                                f"{c.dt}: цена вернулась к {lv.level} ({lv.price:.2f}) после пробоя "
                                f"{window[confirmed_break_idx].dt} и не пересекла его обратно -- похоже на ретест"
                            ),
                        )
                    )

    events.sort(key=lambda e: e.dt)
    return events
