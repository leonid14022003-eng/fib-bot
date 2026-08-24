"""
Verification / Consensus Agent
================================
Два разных назначения, оба -- прямое требование Леонида и регламента:

1. Чек-лист перед выдачей результата (регламент, раздел 23) -- набор
   автоматических проверок структуры/уровней, которые ДОЛЖНЫ пройти,
   прежде чем Dispatch Agent вообще получит данные на отправку.

2. Сверка нескольких независимых прогонов анализа друг с другом
   ("чтобы этот анализ даже проводил несколько сразу агентов и
   собирались между собой" -- из сообщения Леонида 23 августа).
   Сегодня у нас только дневной таймфрейм, поэтому полноценной
   дневная-vs-часовая сверки (как задумано в Задаче №1 из brief.md)
   пока не проверить на живых данных без платного ключа -- но сама
   логика сравнения реализована и протестирована на синтетических
   фикстурах (см. tests/test_pipeline.py), а не выдаётся за анализ
   реального рынка.
"""

from __future__ import annotations

from dataclasses import dataclass

from agents.fibo_agent import FiboStructure


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ChecklistReport:
    results: list[CheckResult]

    @property
    def all_passed(self) -> bool:
        return all(r.passed for r in self.results)

    def failed(self) -> list[CheckResult]:
        return [r for r in self.results if not r.passed]


def run_checklist(structure: FiboStructure, source_tag: str) -> ChecklistReport:
    """Раздел 23 регламента, переложенный в конкретные проверки."""
    results: list[CheckResult] = []

    results.append(
        CheckResult(
            "точка 1 действительно равна 1",
            structure.price_at(1.0) == structure.point1.price,
            f"price_at(1.0)={structure.price_at(1.0)} vs point1.price={structure.point1.price}",
        )
    )
    results.append(
        CheckResult(
            "точка 2 действительно равна 0",
            structure.price_at(0.0) == structure.point2.price,
            f"price_at(0.0)={structure.price_at(0.0)} vs point2.price={structure.point2.price}",
        )
    )
    results.append(
        CheckResult(
            "источник не смешан (один тег на всю структуру)",
            bool(source_tag) and "," not in source_tag,
            f"source_tag={source_tag!r}",
        )
    )
    results.append(
        CheckResult(
            "точка 1 и точка 2 -- разные даты",
            structure.point1.dt != structure.point2.dt,
            f"{structure.point1.dt} vs {structure.point2.dt}",
        )
    )
    results.append(
        CheckResult(
            "уровни рассчитаны монотонно между точками",
            _levels_monotonic(structure),
            "цена уровня должна монотонно идти от point2 (0) к point1 (1)",
        )
    )

    return ChecklistReport(results=results)


def _levels_monotonic(structure: FiboStructure) -> bool:
    ordered = sorted(structure.levels, key=lambda lv: lv.level)
    prices = [lv.price for lv in ordered]
    increasing = all(a <= b for a, b in zip(prices, prices[1:]))
    decreasing = all(a >= b for a, b in zip(prices, prices[1:]))
    return increasing or decreasing


@dataclass(frozen=True)
class ConsensusResult:
    agree: bool
    detail: str


def cross_check_structures(a: FiboStructure, b: FiboStructure, tolerance: float = 0.01) -> ConsensusResult:
    """
    Сверяет два независимо построенных FiboStructure (например, два прогона
    на разных таймфреймах, или два прогона одного и того же для регрессии).
    tolerance -- допустимое относительное расхождение цены точек (по
    умолчанию 1%, т.к. точки на разных таймфреймах не обязаны совпадать
    буквально бит-в-бит -- важно, чтобы направление и порядок величины совпадали).
    """
    if a.direction != b.direction:
        return ConsensusResult(
            agree=False,
            detail=f"Разное направление: {a.direction.value} vs {b.direction.value}",
        )

    p1_diff = abs(a.point1.price - b.point1.price) / a.point1.price
    p2_diff = abs(a.point2.price - b.point2.price) / a.point2.price

    if p1_diff > tolerance or p2_diff > tolerance:
        return ConsensusResult(
            agree=False,
            detail=(
                f"Расхождение точек превышает {tolerance:.1%}: "
                f"точка1 {a.point1.price} vs {b.point1.price} ({p1_diff:.2%}), "
                f"точка2 {a.point2.price} vs {b.point2.price} ({p2_diff:.2%})"
            ),
        )

    return ConsensusResult(agree=True, detail="Направление и точки согласуются в пределах допуска.")
