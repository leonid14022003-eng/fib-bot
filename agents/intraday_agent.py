"""
Intraday Agent -- многотаймфреймовое подтверждение (1H/4H), раздел 7.3
регламента (добавлен 31 августа 2026 как рабочий дефолт Claude вместо не
пришедшего от команды Леонида списка окон по ТФ -- см.
claude/multi-agent-architecture.md).

Отзыв "брокера" 27 августа (см. project doc): "Дневной Fibo без
подтверждения на младшем ТФ -- грубый инструмент". Это НЕ замена дневной
структуре и НЕ блокирующая проверка -- по тому же принципу, что и
независимая (фрактальная) сверка (27 августа, verification_agent.py) и
недельный кросс-чек (31 августа, binance-fib-bot): дополнительная
информационная строка в уже готовом алерте, ничего не подменяет и не
блокирует отправку (should_send_level_watch её не видит и не должна).
Если станет ясно, что расхождение с дневным ФИБО происходит часто и
осмысленно -- это повод спросить Леонида, делать ли её блокирующей, а не
решение по умолчанию.

ВАЖНО про стоимость: build_intraday_note() рассчитан на вызов ТОЛЬКО когда
решение "ОТПРАВЛЯТЬ" уже принято (should_send_level_watch вернула True) --
то есть на реальных, сравнительно редких алертах, а не на каждом цикле
cron (раз в 15 минут для IBM-watch, раз в час для скринера на 14
инструментов). В отличие от фрактальной сверки (переиспользует уже
полученные дневные свечи, без сети), здесь 2 НОВЫХ сетевых запроса к FMP
(1H + 4H) -- считать их на каждом холостом цикле было бы неоправданным
расходом лимита API. Это осознанное архитектурное решение, не недосмотр.
"""
from __future__ import annotations

from dataclasses import dataclass

from agents.data_agent import load_fmp_intraday
from agents.fibo_agent import build_global_fibo, find_oldest_unbroken_extremes
from agents.price_behavior_agent import nearest_level


@dataclass(frozen=True)
class IntradayConfirmation:
    timeframe: str  # "1H" или "4H"
    available: bool
    note: str
    direction: str | None = None  # Direction.value внутридневной структуры, None если available=False --
    # добавлено 5 сентября для analyst_agent.py (сверка направления с дневной структурой)


def _check_one_timeframe(symbol: str, interval: str, current_price: float, exchange_hint: str) -> IntradayConfirmation:
    timeframe_label = "1H" if interval == "1hour" else "4H"
    try:
        series = load_fmp_intraday(symbol, interval=interval, exchange_hint=exchange_hint)
        structure = build_global_fibo(series, extremes_fn=find_oldest_unbroken_extremes)
    except Exception as e:
        # Раздел 7.3: "если в пределах потолка не находится подтверждённого
        # непробитого экстремума -- внутридневное уточнение честно не
        # показывается" -- то же самое честное поведение при сетевой ошибке
        # или неожиданной схеме ответа: не выдумываем результат (регламент,
        # раздел 2), просто отмечаем недоступность и продолжаем без неё.
        return IntradayConfirmation(
            timeframe=timeframe_label, available=False, note=f"{timeframe_label}: недоступно ({e})"
        )
    n = nearest_level(structure, current_price)
    if n.is_testing:
        detail = f"тестирует {n.nearest_level:g} ({n.nearest_price:.2f})"
    else:
        below = f"{n.below_level:g}" if n.below_level is not None else "—"
        above = f"{n.above_level:g}" if n.above_level is not None else "—"
        detail = f"между {below} и {above}, ближе к {n.nearest_level:g}"
    return IntradayConfirmation(
        timeframe=timeframe_label,
        available=True,
        note=(
            f"{timeframe_label} ({structure.direction.value}, "
            f"{structure.point1.dt:%d.%m %H:%M}..{structure.point2.dt:%d.%m %H:%M}): {detail}"
        ),
        direction=structure.direction.value,
    )


def get_intraday_confirmations(
    symbol: str, current_price: float, exchange_hint: str = "NASDAQ/NYSE (US)"
) -> list[IntradayConfirmation]:
    """Сырые 1H/4H проверки (см. IntradayConfirmation) -- вынесено отдельно
    от build_intraday_note() 5 сентября для analyst_agent.py, которому
    нужно направление структуры на каждом ТФ, а не только готовая строка."""
    return [
        _check_one_timeframe(symbol, "1hour", current_price, exchange_hint),
        _check_one_timeframe(symbol, "4hour", current_price, exchange_hint),
    ]


def build_intraday_note(symbol: str, current_price: float, exchange_hint: str = "NASDAQ/NYSE (US)") -> str | None:
    """
    Строит ОДНУ строку для алерта из 1H и 4H проверок (см.
    IntradayConfirmation выше). Возвращает None, если ОБЕ проверки
    недоступны -- тогда строку в сообщение вообще не добавляем, а не
    показываем пустую заглушку (та же дисциплина, что и у
    weekly_extreme_crosscheck в binance-fib-bot, 31 августа).
    """
    results = get_intraday_confirmations(symbol, current_price, exchange_hint)
    if not any(r.available for r in results):
        return None
    parts = [r.note for r in results]
    return "⏱ Внутридневное подтверждение (раздел 7.3, справочно): " + " · ".join(parts)
