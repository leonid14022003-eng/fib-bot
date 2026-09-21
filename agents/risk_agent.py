"""
Risk Agent (21 сентября 2026)
==============================
Профессиональный риск-блок для алерта: R:R, расстояние до стопа, размер
позиции при фиксированном риске на сделку, проверка "стоп внутри шума".

Зачем (по итогам исследования 21 сентября, см. research/): бот до этого
показывал только цены уровней -- то есть отдавал человеку 100% работы по
управлению риском, а профессиональная практика как раз в этом (фиксированный
процент депозита на сделку, минимальный R:R, стоп вне дневного шума) и состоит.
Здесь -- только арифметика по геометрии структуры; никаких прогнозов и
никаких торговых указаний (бот их не даёт, см. дисклеймер).

Геометрия (та же, что в backtest.py и в тексте алерта): вход = текущая цена,
стоп/инвалидация = точка 1 (закрытие за ней отменяет тезис), цель = точка 2.
План строится только когда цена МЕЖДУ точкой 1 и точкой 2 -- то есть это
действительно откат внутри движения. Иначе (цена уже за точкой 2 в зоне
расширений, или за точкой 1) осмысленного плана "вход/стоп/цель" нет -- None,
а не выдуманные числа (регламент, раздел 2).

ВАЖНО про честность цифры R:R: цель (точка 2) -- масштаб ВСЕГО движения
(для глобальной структуры это годы), а не ближайшего отскока. По истории
646 инструментов до точки 2 доходит малая доля сигналов даже за 250 дней (см.
agents/edge_stats.py) -- поэтому R:R здесь показан как ГЕОМЕТРИЯ, а
реалистичные вероятности лежат отдельной строкой рядом.
"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RISK_PER_TRADE_PCT = 1.0  # стандарт фиксированной доли: 1% депозита на сделку
NOISE_STOP_ATR = 1.0  # стоп ближе 1 ATR(14) от входа -- внутри обычного дневного разброса
WIDE_STOP_PCT = 30.0  # стоп дальше 30% цены -- структура слишком крупная для обычного управления риском


@dataclass(frozen=True)
class RiskPlan:
    entry: float
    stop: float
    target: float
    stop_pct: float  # расстояние до стопа, % от входа
    target_pct: float  # расстояние до цели, % от входа
    rr: float  # reward:risk = |цель - вход| / |вход - стоп|
    position_pct: float  # размер позиции, % депозита, при котором срабатывание стопа = risk_per_trade_pct депозита
    risk_per_trade_pct: float
    stop_atr: float | None  # расстояние до стопа в единицах ATR(14); None, если ATR неизвестен
    stop_inside_noise: bool  # True, если stop_atr < NOISE_STOP_ATR

    @property
    def needs_leverage(self) -> bool:
        return self.position_pct > 100.0

    @property
    def stop_very_wide(self) -> bool:
        return self.stop_pct > WIDE_STOP_PCT


def atr14(candles) -> float | None:
    """ATR(14) (простое среднее истинного диапазона) по последним 14 свечам.
    None, если свечей меньше 15 (нужна предыдущая цена закрытия для первой)."""
    if len(candles) < 15:
        return None
    trs = []
    for prev, cur in zip(candles[-15:-1], candles[-14:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / len(trs)


def build_risk_plan(
    entry: float,
    point1: float,
    point2: float,
    atr: float | None = None,
    risk_per_trade_pct: float = DEFAULT_RISK_PER_TRADE_PCT,
) -> RiskPlan | None:
    """None, если вход не лежит строго между точкой 1 и точкой 2 (см. докстринг модуля)."""
    lo, hi = sorted((point1, point2))
    if not (lo < entry < hi) or entry <= 0:
        return None
    risk = abs(entry - point1)
    reward = abs(point2 - entry)
    stop_pct = risk / entry * 100.0
    target_pct = reward / entry * 100.0
    stop_atr = (risk / atr) if atr and atr > 0 else None
    return RiskPlan(
        entry=entry,
        stop=point1,
        target=point2,
        stop_pct=stop_pct,
        target_pct=target_pct,
        rr=reward / risk,
        position_pct=risk_per_trade_pct / stop_pct * 100.0,
        risk_per_trade_pct=risk_per_trade_pct,
        stop_atr=stop_atr,
        stop_inside_noise=stop_atr is not None and stop_atr < NOISE_STOP_ATR,
    )


def format_risk_line(plan: RiskPlan) -> str:
    """Одна короткая строка для сообщения (без HTML -- см. тесты баланса тегов).
    Цель (точка 2) в строке не дублируется: R:R уже её содержит (21 сентября 2026,
    "слишком много лишней информации")."""
    size = f"{plan.position_pct:.1f}%" if plan.position_pct < 10 else f"{plan.position_pct:.0f}%"
    line = f"📐 Стоп {plan.stop_pct:.1f}% · R:R 1:{plan.rr:.1f} · риск {plan.risk_per_trade_pct:g}% → позиция {size} депозита"
    if plan.needs_leverage:
        line += " (нужно плечо)"
    if plan.stop_inside_noise:
        line += f" ⚠️ стоп {plan.stop_atr:.1f} ATR — в шуме"
    if plan.stop_very_wide:
        line += " ⚠️ очень широкий стоп"
    return line
