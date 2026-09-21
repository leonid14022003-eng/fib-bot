"""
"Убрать ограничения по времени и числу свечей, брать всю историю" (21 сентября 2026).
Сравнение окна структуры на ОДНИХ И ТЕХ ЖЕ инструментах и ОДНИХ И ТЕХ ЖЕ датах (сигналы с 2018-09-01):
  W5   -- как сейчас: последние 5 лет (1260 баров), скользящее окно
  W10  -- последние 10 лет (2520 баров)
  WALL -- вся история инструмента (окно растёт от первого бара)
Контроль -- случайные входы только из того же периода дат. Метрика E3 (стоп ровно по -1R).
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import statistics
import time
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

import research_engine as E
from research_lg_robust import cstats          # (импорт подменяет S.GENERATORS -- ниже переопределяем)
import research_study as S

FROM = "2018-09-01"
E.CACHE = _Path(__file__).resolve().parent / "research_cache_full"


def _g(window):
    def gen(inst):
        return [s for s in E.gen_global(inst, window=window, watch=E.WATCH_VALID)
                if s.date >= FROM and s.f < 1.0 and s.atr == s.atr and s.risk >= s.atr]
    return gen


S.GENERATORS = {"W5": _g(1260), "W10": _g(2520), "WALL": _g(None)}


def snapshot(inst):
    """Что видит бот СЕЙЧАС на последнем баре: глубина отката и расстояние до точки 1 при разных окнах."""
    out = {}
    for name, w in (("W5", 1260), ("W10", 2520), ("WALL", None)):
        a = 0 if w is None else max(0, inst.n - w)
        h, l = inst.h[a:], inst.l[a:]
        hi, lo = int(np.argmax(h)), int(np.argmin(l))
        if hi == lo:
            continue
        p1, p2 = (l[lo], h[hi]) if lo < hi else (h[hi], l[lo])
        price = float(inst.c[-1])
        f = (price - p2) / (p1 - p2)
        out[name] = (f, abs(price - p1) / price * 100.0 if 0 < f < 1 else None, inst.dates[a + min(hi, lo)])
    return inst.name, out


def main():
    t0 = time.time()
    insts = [i for i in E.load_all() if i.kind != "crypto"]
    for i in insts:
        i.null_lo = next((k for k, d in enumerate(i.dates) if d >= FROM), 0)
    print(f"Инструментов (акции/индексы) с полной историей: {len(insts)}; медиана свечей {int(statistics.median(i.n for i in insts))}", flush=True)
    per = defaultdict(list)
    with Pool(10) as pool:
        for _n, out in pool.imap_unordered(S._work, insts, chunksize=2):
            for g, rows in out.items():
                per[g].extend(rows)
        snaps = dict(pool.map(snapshot, insts, chunksize=8))
    print("Сигналов с", FROM, ":", ", ".join(f"{g}={len(r)}" for g, r in per.items()), flush=True)

    print("\n" + "=" * 128 + "\nГЕОМЕТРИЯ СИГНАЛОВ (насколько далеко стоп и цель)\n" + "=" * 128)
    for g, rows in per.items():
        rp = sorted(r["risk_pct"] * 100 for r in rows)
        rr = sorted(r["r_target"] for r in rows)
        big = np.mean([x > 30 for x in rp])
        huge = np.mean([x > 60 for x in rp])
        print(f"{g:5s} n={len(rows):5d} | стоп (% от цены): медиана {statistics.median(rp):5.1f}%  p90 {rp[int(.9*len(rp))]:5.1f}% | стоп>30%: {big:5.1%}  стоп>60%: {huge:5.1%} | медиана R:R {statistics.median(rr):.2f}"
              f" | позиция при риске 1%: медиана {100 / statistics.median(rp):.1f}% депозита")

    for title, key, nkey in (("E3: реалистичный стоп -1R, цель = точка 2, 40 дн (главная метрика)", "e3", "n3"),):
        print("\n" + "=" * 128 + f"\n{title}\n" + "=" * 128)
        for g in per:
            for pn, cond in (("ВСЕ    ", lambda r: True), ("2018-21 ", lambda r: r["date"] < "2022-01-01"), ("2022-26 ", lambda r: r["date"] >= "2022-01-01")):
                print(f"{g:5s} {pn} {cstats([r for r in per[g] if cond(r)], key, nkey)}")
            for sg, nm in ((1, "LONG   "), (-1, "SHORT  ")):
                print(f"{g:5s} {nm} {cstats([r for r in per[g] if r['sign'] == sg], key, nkey)}")
            print()

    print("=" * 128 + "\nЧТО ВИДИТ БОТ ПРЯМО СЕЙЧАС (последний бар, 641 инструмент)\n" + "=" * 128)
    for name in ("W5", "W10", "WALL"):
        fs = [(f, stop) for (f, stop, _d) in (v[name] for v in snaps.values() if name in v)]
        n = len(fs)
        deep = sum(1 for f, _ in fs if 0.618 <= f < 1.0)
        stops = sorted(stop for f, stop in fs if stop is not None)
        print(f"{name:5s} инструментов {n}: в зоне алерта (глубина 0.618..1): {deep:3d} ({deep / n:4.1%}) | медиана расстояния до точки 1 у всех: {statistics.median(stops):5.1f}%")
    # как сильно меняется сама точка 1
    same = sum(1 for v in snaps.values() if "W5" in v and "WALL" in v and v["W5"][2] == v["WALL"][2])
    print(f"Точка 1/2 совпадает у W5 и WALL (самая ранняя из двух точек та же): {same} из {len(snaps)} ({same / len(snaps):.0%})")
    old = sum(1 for v in snaps.values() if "WALL" in v and v["WALL"][2] < "2000-01-01")
    print(f"У WALL самая ранняя точка старше 2000 года: {old} из {len(snaps)} ({old / len(snaps):.0%})")
    print(f"\nВремя: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
