"""Лесенка исходов: вероятность достичь +1R/+2R РАНЬШЕ, чем стоп (-1R закрытием), по горизонтам.
Считается для сигналов G0v и для контроля (случайные входы с той же геометрией)."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория (agents/, data/, backtest.py)
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import random
import sys
import zlib
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

import research_engine as E

HORIZONS = (20, 60, 120, 250)
LEVELS_R = (1.0, 2.0)
NULL_REPS = 8


def first_passage(c, t, sign, entry, risk, horizon, r_target=None):
    """('up1'|'up2'|'stop'|None) для порогов +1R, +2R и стопа -1R за horizon баров;
    возвращает (hit1, hit2, stopped_before_1R, stopped_any) с учётом порядка событий."""
    if t + horizon >= len(c):
        return None
    r = sign * (c[t + 1:t + 1 + horizon] - entry) / risk
    stop_i = np.where(r <= -1.0)[0]
    s = stop_i[0] if len(stop_i) else 10 ** 9
    out = []
    for lv in LEVELS_R:
        up_i = np.where(r >= lv)[0]
        u = up_i[0] if len(up_i) else 10 ** 9
        out.append(u < s)        # уровень достигнут раньше стопа
    up1 = np.where(r >= 1.0)[0]
    out.append(bool(s < 10 ** 9 and (len(up1) == 0 or s < up1[0])))   # стоп раньше +1R
    if r_target is not None:
        tg = np.where(r >= r_target)[0]
        out.append(bool((len(tg) > 0) and tg[0] < s))                 # точка 2 раньше стопа
    return tuple(bool(x) for x in out)


def work(inst):
    rows = []
    sigs = [s for s in E.gen_global(inst, window=1260, watch=E.WATCH_VALID) if s.f < 1.0 and s.atr == s.atr and s.risk >= s.atr]
    for s in sigs:
        rnd = random.Random(zlib.crc32(f"{inst.name}|{s.t}".encode()))
        for h in HORIZONS:
            res = first_passage(inst.c, s.t, s.sign, s.entry, s.risk, h, s.r_target)
            if res is None:
                continue
            nul = []
            lo, hi = E.WARMUP, inst.n - 1 - h - 1
            if hi > lo:
                for _ in range(NULL_REPS):
                    tt = rnd.randint(lo, hi)
                    e = float(inst.c[tt])
                    rr = first_passage(inst.c, tt, s.sign, e, s.risk_pct * e, h, s.r_target)
                    if rr:
                        nul.append(rr)
            rows.append((inst.name, inst.kind, s.date, s.sign, s.level, h, res,
                         tuple(float(np.mean([x[i] for x in nul])) for i in range(4)) if nul else None))
    return rows


def main():
    insts = E.load_all()
    allrows = []
    with Pool(10) as p:
        for rows in p.imap_unordered(work, insts, chunksize=4):
            allrows.extend(rows)
    print(f"строк: {len(allrows)}")

    def agg(sel, label):
        print(f"\n{label}")
        print("  горизонт |     n | +1R раньше стопа (контроль) | +2R раньше стопа (контроль) | стоп раньше +1R (контроль)")
        for h in HORIZONS:
            rs = [r for r in allrows if r[5] == h and sel(r)]
            if not rs:
                continue
            n = len(rs)
            p1 = sum(r[6][0] for r in rs) / n
            p2 = sum(r[6][1] for r in rs) / n
            ps = sum(r[6][2] for r in rs) / n
            nn = [r[7] for r in rs if r[7]]
            c1 = sum(x[0] for x in nn) / len(nn)
            c2 = sum(x[1] for x in nn) / len(nn)
            cs = sum(x[2] for x in nn) / len(nn)
            print(f"  {h:4d} дн  | {n:5d} |        {p1:5.1%}  ({c1:5.1%})        |        {p2:5.1%}  ({c2:5.1%})        |       {ps:5.1%}  ({cs:5.1%})")

    nc = lambda r: r[1] != "crypto"
    agg(lambda r: nc(r) and r[3] == 1, "НЕ-КРИПТО, LONG (покупка отката вверх-структуры)")
    agg(lambda r: nc(r) and r[3] == 1 and r[4] == 0.618, "  ... уровень 0.618")
    agg(lambda r: nc(r) and r[3] == 1 and r[4] == 0.786, "  ... уровень 0.786")
    agg(lambda r: nc(r) and r[3] == -1, "НЕ-КРИПТО, SHORT (продажа отскока вниз-структуры)")
    agg(lambda r: r[1] == "crypto", "КРИПТО (все)")
    json.dump(allrows, open("research_ladder.json", "w"))


if __name__ == "__main__":
    main()
