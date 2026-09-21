"""
Главное исследование (21 сентября 2026). Запуск:  python3 research_study.py

Что делает:
  1. Для каждого инструмента (акции/индексы/товары/крипта, ~700 шт., до 10 лет)
     генерирует сигналы двух методик:
        G0  -- как в боте сейчас (экстремумы окна 5 лет; уровни 0.618/0.786/1.0)
        G0v -- то же, но только "валидные" уровни (0.618/0.786, глубина < 1) и
               стоп не уже 1 ATR
        G1  -- "последний значимый свинг" (зигзаг на фракталах, порог 3 ATR)
  2. На каждый сигнал навешивает фильтры-confluence (тренд SMA200, свеча-
     подтверждение, объём отката) и считает исход двумя способами:
        E1 -- как в backtest.py (стоп -1R закрытием, потолок +5R, 20 дней)
        E2 -- тезис буквально: цель = точка 2, стоп = закрытие за точкой 1, 40 дней
  3. Для КАЖДОЙ конфигурации считает контрольную группу: случайные входы на тех
     же инструментах с той же геометрией (направление/риск%/R-цель).
  4. Делит время: ОБУЧЕНИЕ < 2022-01-01 <= ПРОВЕРКА.
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория (agents/, data/, backtest.py)
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import itertools
import json
import math
import random
import sys
import time
import zlib
from collections import defaultdict
from multiprocessing import Pool

import numpy as np

import research_engine as E

SPLIT = "2022-01-01"
NULL_REPS = 20

GENERATORS = {
    "G0":  lambda inst: E.gen_global(inst, window=1260, watch=E.WATCH_PROD),
    "G0v": lambda inst: [s for s in E.gen_global(inst, window=1260, watch=E.WATCH_VALID)
                         if s.f < 1.0 and s.atr == s.atr and s.risk >= s.atr],
    "G1":  lambda inst: E.gen_swing(inst, k=5, m_atr=3.0),
    "G1b": lambda inst: E.gen_swing(inst, k=5, m_atr=5.0),
}

FILTERS = {
    "none":          lambda r: True,
    "trend":         lambda r: r["trend"],
    "confirm":       lambda r: r["confirm"],
    "vol":           lambda r: r["vol"] is True,
    "trend+confirm": lambda r: r["trend"] and r["confirm"],
    "trend+vol":     lambda r: r["trend"] and r["vol"] is True,
    "all3":          lambda r: r["trend"] and r["confirm"] and r["vol"] is True,
}


def _market_regime():
    import research_engine as _E
    idx = _E.load_inst(_E.CACHE / "yahoo__^GSPC.json")
    out = {}
    for i, d in enumerate(idx.dates):
        sma = idx.sma200[i]
        if i >= 20 and sma == sma and idx.sma200[i - 20] == idx.sma200[i - 20]:
            out[d] = bool(idx.c[i] > sma and sma > idx.sma200[i - 20])
    return out


_MKT = _market_regime()


def _work(inst):
    out = {}
    for gname, gen in GENERATORS.items():
        try:
            sigs = gen(inst)
        except Exception as e:  # noqa: BLE001
            sigs = []
            print(f"  gen error {inst.name} {gname}: {e}", file=sys.stderr)
        rows = []
        for s in sigs:
            e1 = E.outcome_e1(inst.c, s.t, s.sign, s.entry, s.risk)
            e2 = E.outcome_e2(inst.c, s.t, s.sign, s.entry, s.risk, s.r_target)
            e3 = E.outcome_e3(inst.c, s.t, s.sign, s.entry, s.risk, s.r_target)
            # контроль: случайные входы с той же геометрией
            rnd = random.Random(zlib.crc32(f"{inst.name}|{gname}|{s.t}".encode()))
            lo, hi = max(E.WARMUP, getattr(inst, "null_lo", 0)), inst.n - 1 - max(E.H_E1, E.H_E2) - 1
            n1 = []
            n2 = []
            n3 = []
            if hi > lo:
                for _ in range(NULL_REPS):
                    tt = rnd.randint(lo, hi)
                    entry = float(inst.c[tt])
                    risk = s.risk_pct * entry
                    r1 = E.outcome_e1(inst.c, tt, s.sign, entry, risk)
                    r2 = E.outcome_e2(inst.c, tt, s.sign, entry, risk, s.r_target)
                    r3 = E.outcome_e3(inst.c, tt, s.sign, entry, risk, s.r_target)
                    if r1:
                        n1.append(r1[0])
                    if r2:
                        n2.append(r2[0])
                    if r3:
                        n3.append(r3[0])
            age = s.t - max(s.p1_idx, s.p2_idx)          # баров с конца импульса (H_b: свежесть отката)
            leg_atr = abs(s.p2 - s.p1) / s.atr if s.atr == s.atr and s.atr > 0 else None   # H_c: размер импульса
            rows.append({
                "age": age, "leg_atr": leg_atr, "mkt_ok": _MKT.get(s.date),
                "inst": s.inst, "kind": s.kind, "date": s.date, "level": s.level, "sign": s.sign,
                "f": s.f, "risk_pct": s.risk_pct, "r_target": s.r_target,
                "trend": s.trend_ok, "confirm": s.confirm_ok, "vol": s.vol_ok,
                "e1": e1[0] if e1 else None, "e1k": e1[1] if e1 else None,
                "e2": e2[0] if e2 else None, "e2k": e2[1] if e2 else None,
                "n1": (sum(n1) / len(n1)) if n1 else None,
                "n2": (sum(n2) / len(n2)) if n2 else None,
                "e3": e3[0] if e3 else None, "e3k": e3[1] if e3 else None,
                "n3": (sum(n3) / len(n3)) if n3 else None,
            })
        out[gname] = rows
    return inst.name, out


def summarize(rows, key, nkey):
    xs = [r[key] for r in rows if r[key] is not None]
    ns = [r[nkey] for r in rows if r[key] is not None and r[nkey] is not None]
    if not xs:
        return None
    m, se, n = E.mean_se(xs)
    null = sum(ns) / len(ns) if ns else float("nan")
    by_inst = defaultdict(list)
    for r in rows:
        if r[key] is not None and r[nkey] is not None:
            by_inst[r["inst"]].append(r[key] - r[nkey])   # избыток над контролем -- на уровне сигнала
    ex = [v for vs in by_inst.values() for v in vs]
    ex_m = sum(ex) / len(ex) if ex else float("nan")
    lo, hi = E.cluster_bootstrap(by_inst, b=600)
    wins = sum(1 for x in xs if x > 0) / len(xs)
    return {"n": n, "avgR": m, "se": se, "null": null, "excess": ex_m, "ci_lo": lo, "ci_hi": hi, "win": wins}


def fmt(s):
    if not s:
        return "      --"
    sig = "*" if (s["ci_lo"] > 0 or s["ci_hi"] < 0) else " "
    return (f"n={s['n']:6d} avgR={s['avgR']:+.3f}(se {s['se']:.3f}) null={s['null']:+.3f} "
            f"EXCESS={s['excess']:+.3f} CI[{s['ci_lo']:+.3f},{s['ci_hi']:+.3f}]{sig} win={s['win']:.0%}")


def main():
    t0 = time.time()
    insts = E.load_all()
    print(f"Инструментов загружено: {len(insts)}  "
          f"(stock={sum(i.kind=='stock' for i in insts)}, index/cmdty={sum(i.kind=='index_cmdty' for i in insts)}, "
          f"crypto={sum(i.kind=='crypto' for i in insts)})", flush=True)
    per_gen = defaultdict(list)
    with Pool(10) as pool:
        for k, (name, out) in enumerate(pool.imap_unordered(_work, insts, chunksize=4), 1):
            for g, rows in out.items():
                per_gen[g].extend(rows)
            if k % 100 == 0:
                print(f"  ... {k}/{len(insts)} инструментов, {time.time()-t0:.0f}s", flush=True)
    json.dump(per_gen, open("research_signals.json", "w"))
    print(f"Сигналов: " + ", ".join(f"{g}={len(r)}" for g, r in per_gen.items()), flush=True)

    for period_name, cond in (("ОБУЧЕНИЕ  (< 2022)", lambda r: r["date"] < SPLIT), ("ПРОВЕРКА (>= 2022)", lambda r: r["date"] >= SPLIT)):
        print("\n" + "=" * 118)
        print(f"{period_name}   avgR -- средний результат сигналов; null -- случайные входы с той же геометрией; EXCESS = сигнал - контроль; * -- CI95 (бутстрэп по инструментам) не включает 0")
        print("=" * 118)
        for exit_name, key, nkey in (("E1 (как backtest.py: -1R / +5R / 20д)", "e1", "n1"), ("E2 (цель=точка 2, стоп=точка 1, 40д)", "e2", "n2")):
            print(f"\n--- {exit_name} ---")
            for g in GENERATORS:
                base = [r for r in per_gen[g] if cond(r)]
                for fname, fn in FILTERS.items():
                    rows = [r for r in base if fn(r)]
                    print(f"{g:4s} {fname:14s} {fmt(summarize(rows, key, nkey))}")
    print(f"\nВремя: {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
