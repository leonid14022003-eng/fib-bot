"""
Сетки из agents/local_grid_agent.py как альтернатива "старейшему непробитому
экстремуму" (21 сентября 2026) -- с контролем случайными входами и делением на
обучение (<2022) / проверку (>=2022), тем же движком, что research_study.py.

Три варианта (все -- скользящее окно 5 лет, как в бою; вход/стоп/цель --
та же геометрия и те же исходы E1/E2):
  G0v -- как сейчас в бою (эталон, из research_engine)
  GLG -- точки 1/2 = find_global_grid() (буквальный ATH/ATL окна). Проверка
         гипотезы "это то же самое, что G0v": сравниваем наборы сигналов.
  L1  -- структура = ТЕКУЩАЯ локальная сетка из run_local_grid_chain()
         (точка 1 -- начало локальной; точка 2 -- зафиксированная или,
         пока сетка формируется, плавающая). Это и есть "последний свинг"
         по спецификации Леонида. Пересчёт на каждый день только по данным
         до этого дня (глобальная сетка внутри цепочки тоже считается на
         префиксе -- без заглядывания вперёд).
  L2  -- то же, но только пока локальная сетка в состоянии "зафиксирована"
         (двусвечное подтверждение за локальными 50% уже случилось).
Сигнал: глубина отката до 0.618 / 0.786 текущей структуры (f < 1), стоп не
уже 1 ATR -- те же ворота, что у G0v.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import time
from collections import defaultdict
from datetime import date
from multiprocessing import Pool

import research_engine as E
import research_study as S
from agents.data_agent import Candle, CandleSeries
from agents.local_grid_agent import LocalState, find_global_grid, run_local_grid_chain

W = 1260
_G0V = S.GENERATORS["G0v"]


def _candles(inst):
    return [Candle(dt=date.fromisoformat(d), open=float(o), high=float(h), low=float(l), close=float(c), volume=int(v))
            for d, o, h, l, c, v in zip(inst.dates, inst.o, inst.h, inst.l, inst.c, inst.v)]


def _emit(inst, t, p1, p2, i1, i2, state):
    """Общая логика level-watch + ворота как у G0v. state -- dict с ключами sid/lvl."""
    if p1 == p2:
        return None
    f = (float(inst.c[t]) - p2) / (p1 - p2)
    if f >= 1.0:
        return None
    reached = [lv for lv in E.WATCH_VALID if f >= lv]
    if not reached:
        return None
    deepest = max(reached)
    sid = (i1, i2)
    if state.get("sid") == sid and state.get("lvl") is not None and deepest <= state["lvl"]:
        return None
    state["sid"], state["lvl"] = sid, deepest
    s = E._mk(inst, t, deepest, p1, p2, i1, i2)
    if s is None or not (s.atr == s.atr) or s.risk < s.atr:
        return None
    return s


def gen_lg_global(inst):
    candles, sigs, state = _candles(inst), [], {}
    for t in range(E.WARMUP, inst.n):
        a = max(0, t + 1 - W)
        try:
            g = find_global_grid(CandleSeries(symbol=inst.name, exchange_or_source="x", timeframe="1D",
                                              candles=candles[a:t + 1], fetched_via="", fetch_note=""))
        except ValueError:
            continue
        s = _emit(inst, t, g.point1.price, g.point2.price, a + g.point1.index, a + g.point2.index, state)
        if s:
            sigs.append(s)
    return sigs


def gen_lg_chain(inst, only_fixed: bool):
    candles, sigs, state = _candles(inst), [], {}
    for t in range(E.WARMUP, inst.n):
        a = max(0, t + 1 - W)
        try:
            chain = run_local_grid_chain(CandleSeries(symbol=inst.name, exchange_or_source="x", timeframe="1D",
                                                      candles=candles[a:t + 1], fetched_via="", fetch_note=""))
        except ValueError:
            continue
        loc = chain.current
        if loc is None or (only_fixed and loc.state is not LocalState.FIXED):
            continue
        p2pt = loc.point2_final or loc.point2_preliminary
        s = _emit(inst, t, loc.point1.price, p2pt.price, a + loc.point1.index, a + p2pt.index, state)
        if s:
            sigs.append(s)
    return sigs


# Подмена набора генераторов для S._work (в воркерах выполняется при импорте главного модуля)
S.GENERATORS = {
    "G0v": _G0V,
    "GLG": gen_lg_global,
    "L1": lambda inst: gen_lg_chain(inst, False),
    "L2": lambda inst: gen_lg_chain(inst, True),
}


def main():
    t0 = time.time()
    insts = E.load_all()
    per_gen = defaultdict(list)
    with Pool(10) as pool:
        for k, (name, out) in enumerate(pool.imap_unordered(S._work, insts, chunksize=2), 1):
            for g, rows in out.items():
                per_gen[g].extend(rows)
            if k % 100 == 0:
                print(f"  ... {k}/{len(insts)} ({time.time() - t0:.0f}s)", flush=True)
    json.dump(per_gen, open("research_signals_lg.json", "w"))
    print("Сигналов:", ", ".join(f"{g}={len(r)}" for g, r in per_gen.items()), flush=True)

    # --- 1. GLG == G0v? ---
    key = lambda r: (r["inst"], r["date"], r["level"])
    a, b = {key(r) for r in per_gen["G0v"]}, {key(r) for r in per_gen["GLG"]}
    print(f"\nПРОВЕРКА ГИПОТЕЗЫ: find_global_grid() даёт те же сигналы, что боевой метод?")
    print(f"  G0v: {len(a)} сигналов, GLG: {len(b)}, общих: {len(a & b)}, только в G0v: {len(a - b)}, только в GLG: {len(b - a)}"
          f"  -> совпадение {len(a & b) / max(len(a | b), 1):.1%}")

    # --- 2. таблицы избытка над контролем ---
    def table(title, key_, nkey):
        print(f"\n--- {title} ---")
        for g in ("G0v", "GLG", "L1", "L2"):
            for pname, cond in (("ВСЕ    ", lambda r: True), ("ОБУЧ.<22", lambda r: r["date"] < S.SPLIT), ("ПРОВ.>=22", lambda r: r["date"] >= S.SPLIT)):
                rows = [r for r in per_gen[g] if cond(r)]
                print(f"{g:4s} {pname} {S.fmt(S.summarize(rows, key_, nkey))}")

    print("\n" + "=" * 118)
    print("СРАВНЕНИЕ (EXCESS = результат сигнала минус результат случайных входов с той же геометрией; * -- CI95 не включает 0)")
    print("=" * 118)
    table("E1 (как backtest.py: -1R / +5R / 20 дней)", "e1", "n1")
    table("E2 (цель = точка 2, стоп = точка 1, 40 дней)", "e2", "n2")

    print("\n" + "=" * 118)
    print("ПО НАПРАВЛЕНИЮ (только E2, вся история)")
    print("=" * 118)
    for g in ("G0v", "L1", "L2"):
        for sg, nm in ((1, "LONG "), (-1, "SHORT")):
            rows = [r for r in per_gen[g] if r["sign"] == sg]
            print(f"{g:4s} {nm} {S.fmt(S.summarize(rows, 'e2', 'n2'))}")
    print(f"\nВремя: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
