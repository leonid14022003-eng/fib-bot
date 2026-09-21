"""Срезы и устойчивость: направление, уровень, класс актива, год, блочный бутстрэп по месяцам."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория (agents/, data/, backtest.py)
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import random
import sys
from collections import defaultdict

DATA = json.load(open("research_signals.json"))
GEN = sys.argv[1] if len(sys.argv) > 1 else "G0v"
rows_all = DATA[GEN]


def ex(rows, key, nkey):
    return [(r["date"][:7], r["inst"], r[key] - r[nkey], r[key], r[nkey]) for r in rows
            if r[key] is not None and r[nkey] is not None]


def stats(rows, key, nkey, b=800, seed=11):
    xs = ex(rows, key, nkey)
    if len(xs) < 20:
        return f"n={len(xs):5d}  (мало)"
    n = len(xs)
    avg = sum(x[3] for x in xs) / n
    nul = sum(x[4] for x in xs) / n
    exc = sum(x[2] for x in xs) / n
    # блочный бутстрэп по календарным МЕСЯЦАМ (кросс-секционная корреляция) ...
    by_m = defaultdict(list)
    by_i = defaultdict(list)
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
    lo, hi = min(ml, il), max(mh, ih)          # консервативно: самый широкий из двух
    star = "*" if (lo > 0 or hi < 0) else " "
    win = sum(1 for x in xs if x[3] > 0) / n
    return (f"n={n:5d} avgR={avg:+.3f} null={nul:+.3f} EXCESS={exc:+.3f}  "
            f"CI(месяцы)[{ml:+.3f},{mh:+.3f}] CI(инстр.)[{il:+.3f},{ih:+.3f}] "
            f"консерват.[{lo:+.3f},{hi:+.3f}]{star} win={win:.0%}")


for exit_name, key, nkey in (("E1 (backtest.py)", "e1", "n1"), ("E2 (цель = точка 2)", "e2", "n2")):
    print("=" * 130)
    print(f"{GEN}  |  {exit_name}")
    print("=" * 130)
    print("ВСЕ           ", stats(rows_all, key, nkey))
    print("--- по направлению тезиса (+1 = 'вверх', покупка отката; -1 = 'вниз', продажа отскока) ---")
    for sg in (1, -1):
        print(f"sign={sg:+d}       ", stats([r for r in rows_all if r["sign"] == sg], key, nkey))
    print("--- по уровню ---")
    for lv in sorted({r["level"] for r in rows_all}):
        print(f"level={lv:<6g}   ", stats([r for r in rows_all if r["level"] == lv], key, nkey))
    print("--- по классу актива ---")
    for k in sorted({r["kind"] for r in rows_all}):
        print(f"{k:13s}  ", stats([r for r in rows_all if r["kind"] == k], key, nkey))
    print("--- по направлению внутри акций ---")
    for sg in (1, -1):
        print(f"stock sign={sg:+d}", stats([r for r in rows_all if r["kind"] == "stock" and r["sign"] == sg], key, nkey))
    print("--- по годам ---")
    for y in sorted({r["date"][:4] for r in rows_all}):
        print(f"{y}          ", stats([r for r in rows_all if r["date"][:4] == y], key, nkey))
    print("--- по R:R цели (E2-геометрия) ---")
    for lo, hi in ((0, 1.5), (1.5, 2.5), (2.5, 5), (5, 1e9)):
        print(f"R:R {lo:>3}-{hi if hi < 1e8 else 'inf'}", stats([r for r in rows_all if lo <= r["r_target"] < hi], key, nkey))
    print()
