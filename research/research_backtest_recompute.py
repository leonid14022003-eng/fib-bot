"""
Буквальный пересчёт backtest.py с подменой extremes_fn (21 сентября 2026).

Прогоняет НАСТОЯЩИЕ backtest.simulate_symbol / _score_outcome / summarize
(ничего не переписано) на ~650 инструментах кэша. Меняется ровно одно --
функция поиска точек 1/2, которую backtest.py передаёт в build_global_fibo:

  BASE -- как сейчас: find_global_extremes (экстремумы окна, 5 свечей слева)
  LG   -- логика глобальной сетки из agents/local_grid_agent.py
          (find_global_grid: буквальный ATH/ATL окна, без правила 5 свечей)
  RS   -- "последний значимый свинг": зигзаг на подтверждённых фракталах
          (5 баров с каждой стороны, порог 3 ATR), точки 1/2 = два последних
          принятых свинга (только ПОДТВЕРЖДЁННЫЕ -- без заглядывания вперёд)

Метрика та же, что в backtest.py: win_rate и avg_r при MAX_HOLD_DAYS=20,
стоп -1R закрытием, потолок +5R. Контроля случайными входами здесь НЕТ --
он в research_local_grid_study.py; этот скрипт отвечает на вопрос "что
показал бы сам backtest.py".
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import time
from datetime import date
from multiprocessing import Pool

import numpy as np

import backtest
import research_engine as E
from agents import fibo_agent as F
from agents.data_agent import Candle, CandleSeries
from agents.local_grid_agent import find_global_grid



def lg_extremes(series):
    g = find_global_grid(series)  # ValueError, если ATH и ATL на одной свече -- backtest.py это уже ловит
    pts = (g.point1, g.point2)
    hi = next(p for p in pts if p.kind == "HIGH")
    lo = next(p for p in pts if p.kind == "LOW")
    return (F.SwingPoint(dt=hi.dt, price=hi.price, kind="HIGH", index=hi.index),
            F.SwingPoint(dt=lo.dt, price=lo.price, kind="LOW", index=lo.index))


_RS_CACHE: dict = {}


def prepare_rs(candles, k: int = 5, m_atr: float = 3.0):
    """Один проход по ВСЕЙ истории, но БЕЗ заглядывания вперёд: фрактал на баре i
    смотрит только на бары [i-k, i+k] и "подтверждается" на баре i+k; ATR на баре i --
    только на данные до i. Поэтому состояние зигзага на момент t (после обработки всех
    фракталов с i+k <= t) идентично тому, что получилось бы при пересчёте на префиксе
    candles[:t+1] -- кэшируем снимок "двух последних принятых свингов" по времени
    подтверждения (иначе пересчёт на каждый день стоил бы минуты на инструмент)."""
    n = len(candles)
    h = np.array([x.high for x in candles]); l = np.array([x.low for x in candles]); cl = np.array([x.close for x in candles])
    prev = np.roll(cl, 1); prev[0] = cl[0]
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    cs = np.cumsum(np.insert(tr, 0, 0.0))
    swings, conf, snaps = [], [], []
    for i in range(max(k, 14), n - k):
        is_h = h[i] > h[i - k:i].max() and h[i] > h[i + 1:i + 1 + k].max()
        is_l = l[i] < l[i - k:i].min() and l[i] < l[i + 1:i + 1 + k].min()
        if not (is_h or is_l):
            continue
        typ, price = ("H", float(h[i])) if is_h else ("L", float(l[i]))
        atr_i = (cs[i + 1] - cs[i + 1 - 14]) / 14
        if not swings:
            swings.append((i, typ, price))
        else:
            li, lt, lp = swings[-1]
            if lt == typ:
                if (typ == "H" and price > lp) or (typ == "L" and price < lp):
                    swings[-1] = (i, typ, price)
            elif abs(price - lp) >= m_atr * atr_i:
                swings.append((i, typ, price))
        conf.append(i + k)
        snaps.append(tuple(swings[-2:]))
    _RS_CACHE[id(candles[0])] = (conf, snaps, candles)


def rs_extremes(series):
    from bisect import bisect_right
    conf, snaps, full = _RS_CACHE[id(series.candles[0])]
    j = bisect_right(conf, len(series.candles) - 1) - 1
    if j < 0 or len(snaps[j]) < 2:
        raise ValueError("нет двух подтверждённых свингов")
    (ia, ta, pa), (ib, tb, pb) = snaps[j]
    hi_i, hi_p = (ia, pa) if ta == "H" else (ib, pb)
    lo_i, lo_p = (ia, pa) if ta == "L" else (ib, pb)
    c = series.candles
    return (F.SwingPoint(dt=c[hi_i].dt, price=hi_p, kind="HIGH", index=hi_i),
            F.SwingPoint(dt=c[lo_i].dt, price=lo_p, kind="LOW", index=lo_i))


VARIANTS = {"BASE": None, "LG": lg_extremes, "RS": rs_extremes}
_ORIG_BG = backtest.build_global_fibo


def _run(series, alt):
    if alt is None:
        backtest.build_global_fibo = _ORIG_BG
    else:
        def bg(s, extremes_fn=None):
            return _ORIG_BG(s, extremes_fn=extremes_fn or alt)
        backtest.build_global_fibo = bg
    try:
        sigs = backtest.simulate_symbol(series)
    finally:
        backtest.build_global_fibo = _ORIG_BG
    return [backtest._score_outcome(s, series.candles) for s in sigs]


def work(path_str):
    inst = E.load_inst(_Path(path_str))
    if inst is None:
        return None
    candles = [Candle(dt=date.fromisoformat(d), open=float(o), high=float(h), low=float(l), close=float(c), volume=int(v))
               for d, o, h, l, c, v in zip(inst.dates, inst.o, inst.h, inst.l, inst.c, inst.v)]
    out = {"inst": inst.name, "kind": inst.kind}
    prepare_rs(candles)
    for name, alt in VARIANTS.items():
        # backtest.py считает РАСШИРЯЮЩИМСЯ окном (от начала истории) -- оставлено как есть, это и есть буквальный пересчёт
        rows = []
        series = CandleSeries(symbol=inst.name, exchange_or_source="cache", timeframe="1D",
                              candles=candles, fetched_via="", fetch_note="")
        for sc in _run(series, alt):
            rows.append((sc.signal.fired_dt.isoformat(), sc.signal.alert_level, sc.signal.direction,
                         sc.outcome, sc.final_r))
        out[name] = rows
    return out


def main():
    files = sorted(str(p) for p in E.CACHE.glob("*.json"))
    t0 = time.time()
    res = []
    with Pool(10) as pool:
        for i, r in enumerate(pool.imap_unordered(work, files, chunksize=2), 1):
            if r:
                res.append(r)
            if i % 100 == 0:
                print(f"  ... {i}/{len(files)} ({time.time() - t0:.0f}s)", flush=True)
    json.dump(res, open("backtest_recompute.json", "w"))

    def scored(name, cond=lambda row, kind: True, floor=None):
        out = []
        for r in res:
            for (dt_, lvl, dr, oc, fr) in r[name]:
                if cond((dt_, lvl, dr, oc, fr), r["kind"]):
                    out.append(backtest.ScoredOutcome(
                        signal=backtest.BacktestSignal(r["inst"], 0, date.fromisoformat(dt_), lvl, dr, 1.0, 1.0, 1.0, 1.0, None),
                        outcome=oc, final_r=(max(fr, floor) if (floor is not None and fr is not None) else fr), days_held=0))
        return out

    print(f"\nИнструментов: {len(res)}")
    for label, cond in (
        ("ВСЕ сигналы", lambda row, k: True),
        ("только LONG  (восходящая структура, покупка отката)", lambda row, k: row[2] == "восходящий"),
        ("только SHORT (нисходящая структура, продажа отскока)", lambda row, k: row[2] == "нисходящий"),
        ("дата < 2022", lambda row, k: row[0] < "2022-01-01"),
        ("дата >= 2022", lambda row, k: row[0] >= "2022-01-01"),
        ("без крипты", lambda row, k: k != "crypto"),
        ("БЕЗ уровня 1.0 (цена вернулась к точке 1 -- тезис мёртв, риск-юнит ~0)", lambda row, k: row[1] != 1.0),
        ("БЕЗ уровня 1.0 И убыток ограничен -3R (защита от артефакта деления на малый риск)", lambda row, k: row[1] != 1.0),
    ):
        print("\n" + "=" * 100 + f"\n{label}\n" + "=" * 100)
        floor = -3.0 if "ограничен -3R" in label else None
        for name in VARIANTS:
            s = backtest.summarize(name, scored(name, cond, floor))
            print(backtest.format_summary(s))
    print(f"\nВремя: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
