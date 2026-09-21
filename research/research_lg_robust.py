"""
Строгая проверка вариантов "последний свинг" (21 сентября 2026) -- второй проход после
research_local_grid_study.py, где L1/L2 показали избыток над контролем +0.11..+0.13R.

Что проверяется дополнительно (всё, что могло сделать результат артефактом):
  1. E3 -- реалистичное исполнение стопа ровно по -1R (E1/E2 считают убыток по закрытию,
     что при узком стопе занижает и сигнал, и контроль: абсолютные R становятся нечестными).
  2. Консервативный доверительный интервал: самый широкий из двух бутстрэпов --
     по ИНСТРУМЕНТАМ и по календарным МЕСЯЦАМ (сигналы одного месяца связаны через рынок).
  3. Проредженная выборка: не больше 1 сигнала на инструмент в месяц (убирает перекрытие --
     локальные сетки порождают много соседних сигналов).
  4. Раздельно LONG / SHORT и обучение (<2022) / проверка (>=2022).
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import random
import time
from collections import defaultdict
from multiprocessing import Pool

import research_engine as E
import research_study as S
from research_local_grid_study import gen_lg_chain

S.GENERATORS = {
    "G0v": S.GENERATORS["G0v"] if "G0v" in S.GENERATORS else (lambda inst: [
        s for s in E.gen_global(inst, window=1260, watch=E.WATCH_VALID) if s.f < 1.0 and s.atr == s.atr and s.risk >= s.atr]),
    "G1": lambda inst: E.gen_swing(inst, k=5, m_atr=3.0),
    "L1": lambda inst: gen_lg_chain(inst, False),
    "L2": lambda inst: gen_lg_chain(inst, True),
}


def cstats(rows, key, nkey, b=600, seed=11):
    xs = [(r["date"][:7], r["inst"], r[key] - r[nkey], r[key], r[nkey]) for r in rows if r[key] is not None and r[nkey] is not None]
    n = len(xs)
    if n < 30:
        return f"n={n:6d}  (мало)"
    avg = sum(x[3] for x in xs) / n
    nul = sum(x[4] for x in xs) / n
    exc = sum(x[2] for x in xs) / n
    by_m, by_i = defaultdict(list), defaultdict(list)
    for m, i, e, *_ in xs:
        by_m[m].append(e)
        by_i[i].append(e)

    def boot(groups):
        keys = list(groups)
        sums = {k: (sum(v), len(v)) for k, v in groups.items()}
        rnd = random.Random(seed)
        vals = []
        for _ in range(b):
            s = c = 0
            for _k in range(len(keys)):
                ss, nn = sums[keys[rnd.randrange(len(keys))]]
                s += ss
                c += nn
            vals.append(s / c)
        vals.sort()
        return vals[int(.025 * b)], vals[int(.975 * b)]

    ml, mh = boot(by_m)
    il, ih = boot(by_i)
    lo, hi = min(ml, il), max(mh, ih)
    star = "*" if (lo > 0 or hi < 0) else " "
    win = sum(1 for x in xs if x[3] > 0) / n
    return f"n={n:6d} avgR={avg:+.3f} control={nul:+.3f} EXCESS={exc:+.3f} CI95[{lo:+.3f},{hi:+.3f}]{star} win={win:.0%}"


def thin(rows):
    """Не больше одного сигнала на (инструмент, месяц) -- первый по дате."""
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: (r["inst"], r["date"])):
        k = (r["inst"], r["date"][:7])
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def main():
    t0 = time.time()
    insts = E.load_all()
    per = defaultdict(list)
    with Pool(10) as pool:
        for _name, out in pool.imap_unordered(S._work, insts, chunksize=2):
            for g, rows in out.items():
                per[g].extend(rows)
    json.dump(per, open("research_signals_lg2.json", "w"))
    print("Сигналов:", ", ".join(f"{g}={len(r)}" for g, r in per.items()), flush=True)
    years = 5.0
    print("Сигналов на инструмент в год (грубо): " + ", ".join(f"{g}={len(r) / len(insts) / years:.1f}" for g, r in per.items()))

    periods = (("ВСЕ     ", lambda r: True), ("ОБУЧ.<22", lambda r: r["date"] < S.SPLIT), ("ПРОВ.>=22", lambda r: r["date"] >= S.SPLIT))
    for title, key, nkey in (
        ("E3: реалистичный стоп ровно по -1R, цель = точка 2, 40 дней  <-- ГЛАВНАЯ метрика", "e3", "n3"),
        ("E2: убыток по факту закрытия (как раньше)", "e2", "n2"),
    ):
        print("\n" + "=" * 130 + f"\n{title}\n" + "=" * 130)
        for g in S.GENERATORS:
            for pn, cond in periods:
                print(f"{g:4s} {pn} {cstats([r for r in per[g] if cond(r)], key, nkey)}")
            print()

    print("=" * 130 + "\nПРОРЕЖЕННАЯ выборка (<= 1 сигнала на инструмент в месяц), E3\n" + "=" * 130)
    for g in S.GENERATORS:
        th = thin(per[g])
        for pn, cond in periods:
            print(f"{g:4s} {pn} {cstats([r for r in th if cond(r)], 'e3', 'n3')}")
        print()

    print("=" * 130 + "\nПО НАПРАВЛЕНИЮ (E3, вся история)\n" + "=" * 130)
    for g in S.GENERATORS:
        for sg, nm in ((1, "LONG "), (-1, "SHORT")):
            print(f"{g:4s} {nm} {cstats([r for r in per[g] if r['sign'] == sg], 'e3', 'n3')}")
        print()
    print(f"Время: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
