"""
Отчёт по цепочке локальных сеток (agents/local_grid_agent.py) для одного
инструмента -- ИССЛЕДОВАТЕЛЬСКИЙ скрипт, НЕ часть боевого пайплайна.

Ничего не шлёт в Telegram, ничего не пишет в output/alert_state.json или
output/screener_state.json (память боевого бота не трогает). Только читает
дневные свечи через load_fmp_daily() и печатает построенную цепочку в
консоль -- чтобы Леонид мог свериться с этим до любого решения о переносе
в боевые агенты.

Использование:
    source .env && python3 local_grid_report.py CRWV
    source .env && python3 local_grid_report.py NG=F --months-back 1200
"""
from __future__ import annotations

import argparse
import sys

from agents.data_agent import load_fmp_daily
from agents.local_grid_agent import LocalGrid, LocalState, run_local_grid_chain


def _fmt_point(p) -> str:
    return f"{p.dt} = {p.price:.3f} ({p.kind})"


def _fmt_local(local: LocalGrid) -> str:
    lines = [
        f"  Локальная №{local.seq} [{local.direction.value}] -- состояние: {local.state.value}",
        f"    Точка 1 (100%): {_fmt_point(local.point1)}",
        f"    Запуск (переход в формирование): {local.launch_date}",
    ]
    if local.point2_final is not None:
        lines.append(f"    Точка 2 (0%, финальная): {_fmt_point(local.point2_final)}")
        lines.append(f"    Подтверждена: {local.confirmed_date}")
    else:
        lines.append(f"    Точка 2 (0%, ПРЕДВАРИТЕЛЬНАЯ, ещё формируется): {_fmt_point(local.point2_preliminary)}")
    if local.exit_date is not None:
        lines.append(f"    Выход: {local.exit_date}, через {local.exit_border}, цена {local.exit_price:.3f}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbol", help="Тикер FMP, например CRWV, WEN, NG=F")
    parser.add_argument("--months-back", type=int, default=1200, help="Глубина истории в месяцах (по умолчанию ~100 лет = вся доступная)")
    args = parser.parse_args()

    try:
        series = load_fmp_daily(args.symbol, months_back=args.months_back)
    except Exception as exc:  # честно показываем ошибку, не подставляем данные (раздел 2)
        print(f"ОШИБКА загрузки данных для {args.symbol}: {exc}", file=sys.stderr)
        return 1

    print(f"{args.symbol}: {len(series.candles)} дневных свечей, {series.start} .. {series.end}")
    print(f"Источник: {series.exchange_or_source}")

    try:
        result = run_local_grid_chain(series)
    except ValueError as exc:
        print(f"ОШИБКА построения цепочки: {exc}", file=sys.stderr)
        return 1

    g = result.global_grid
    print()
    print(f"Глобальная сетка [{g.direction.value}]:")
    print(f"  Точка 1 (100%): {_fmt_point(g.point1)}")
    print(f"  Точка 2 (0%):   {_fmt_point(g.point2)}")
    print(f"  G 50% = {g.levels[0.5]:.3f}")

    print()
    print(f"Завершённых локальных сеток: {len(result.completed)}")
    for local in result.completed:
        print(_fmt_local(local))
        print()

    if result.current is not None:
        print("Текущая (незавершённая) локальная сетка:")
        print(_fmt_local(result.current))
    else:
        print("Цепочка не дошла даже до состояния ожидания первой локальной "
              "(данных после глобальной точки 2 недостаточно).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
