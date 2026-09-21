"""Генерирует agents/edge_stats.py из edge_table.json -- цифры в боте не набираются руками,
а берутся ровно из исследования (research_ladder.py / research_edge_table.py)."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
table = json.load(open(HERE / "edge_table.json", encoding="utf-8"))
HORIZONS = ("60", "120", "250")

lines = []
for key, cell in table.items():
    if not cell:
        continue
    row = {}
    for h in HORIZONS:
        c = cell.get(h)
        if c:
            row[h] = (c["n"], round(c["p1"], 4), round(c["c_p1"], 4), round(c["stop"], 4), round(c["c_stop"], 4),
                      round(c["tgt"], 4), round(c["c_tgt"], 4))
    lines.append(f'    "{key}": {{')
    for h, tup in row.items():
        lines.append(f"        {h}: {tup},")
    lines.append("    },")
body = "\n".join(lines)

src = f'''"""
Edge Stats (21 сентября 2026) -- измеренные базовые частоты исходов сигнала.
==============================================================================
Цифры ниже НЕ придуманы и не набраны руками: сгенерированы скриптом
research/research_gen_edge_stats.py из результатов исследования на ~650
инструментах (599 акций, 9 индексов/товаров, 38 криптовалют; дневные данные
Yahoo/Binance, 2016-2026), 2 900+ сигналов метода, ровно как он работает в
боте (глобальный экстремум окна 5 лет, откат до 0.618/0.786, стоп -- закрытие
за точкой 1). Каждая цифра сравнивается с КОНТРОЛЕМ -- случайными входами на
тех же инструментах с той же геометрией (направление, размер стопа, цель).
Метод проверки описан в research/research_engine.py (валидирован: побитово
совпадает с боевым backtest.simulate_symbol на 271 сигнале).

Что показывают цифры (кортеж на каждый горизонт в днях):
  (n, p_plus1R, control_plus1R, p_stop_first, control_stop_first, p_target, control_target)
  p_plus1R      -- доля сигналов, где цена дошла до +1R РАНЬШЕ, чем сработал стоп
                   (закрытие за точкой 1);
  p_stop_first  -- доля сигналов, где стоп сработал раньше, чем цена дошла до +1R;
  p_target      -- доля сигналов, дошедших до точки 2 раньше стопа;
  control_*     -- то же для случайного входа с той же геометрией.

ЧЕСТНЫЕ ОГОВОРКИ (важнее самих цифр):
  * Выборка "выжившая" (сегодняшние члены индексов/топ-капитализации) --
    результаты покупки откатов в ней завышены; контроль это частично, но не
    полностью убирает. Реальное преимущество, скорее всего, меньше измеренного.
  * Сигналы одного месяца коррелированы через рынок -- доверительные интервалы
    в исследовании считались блочным бутстрэпом по месяцам и по инструментам.
  * Прошлые частоты не гарантируют будущих. Это калибровка ожиданий, а не прогноз.
  * Для продажи отскока (short) измеренного преимущества над случайным входом НЕТ;
    для крипты выборка мала -- см. format_edge_line().
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория (agents/, data/, backtest.py)
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

STUDY_DATE = "2026-09-21"
ADVANTAGE_MIN = 0.05   # p_plus1R - control_plus1R (на 60 днях) от этого порога считаем преимущество заметным
NO_EDGE_BAND = 0.03    # внутри +-этой полосы -- неотличимо от случайного входа

# {{класс: {{горизонт_дней: (n, p1, c_p1, p_stop, c_stop, p_tgt, c_tgt)}}}}
TABLE = {{
{body}
}}


def edge_key(kind: str, sign: int, level: float) -> str | None:
    """sign: +1 -- покупка отката (восходящая структура), -1 -- продажа отскока.
    kind: "crypto" -- отдельный класс; всё остальное (акции/индексы/товары) -- "stock"."""
    if kind == "crypto":
        return "crypto_all"
    if level not in (0.618, 0.786):
        return None
    return f"{{'long' if sign > 0 else 'short'}}_{{level:g}}"


def _pct(x: float) -> str:
    return f"{{x * 100:.0f}}"


def format_edge_line(kind: str, sign: int, level: float) -> str | None:
    """Одна строка "как это исторически заканчивалось" для алерта; None, если для
    такого класса сигнала статистики нет (например, уровень 1.0) -- ничего не выдумываем."""
    key = edge_key(kind, sign, level)
    if key is None or key not in TABLE:
        return None
    row = TABLE[key]
    r60, r120 = row.get(60), row.get(120)
    if r60 is None:
        return None
    if key == "crypto_all":
        return f"📊 Крипта: преимущество не подтверждено (n={{r60[0]}})"
    p60, c60 = r60[1], r60[2]
    p120, c120 = (r120[1], r120[2]) if r120 else (None, None)
    tail = f"{{_pct(p60)}}/{{_pct(p120)}}%" if p120 is not None else f"{{_pct(p60)}}%"
    ctrl = f"{{_pct(c60)}}/{{_pct(c120)}}%" if c120 is not None else f"{{_pct(c60)}}%"
    line = f"📊 60/120 дн: +1R раньше стопа {{tail}} (случайно {{ctrl}})"
    diff = p60 - c60
    if diff >= ADVANTAGE_MIN:
        return line
    if diff > -NO_EDGE_BAND:
        return line + " ⚠️ не лучше случайного входа"
    return line + " ⚠️ хуже случайного входа"
'''
out = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "agents" / "edge_stats.py"
out.write_text(src, encoding="utf-8")
print("written", out, len(src), "bytes")
