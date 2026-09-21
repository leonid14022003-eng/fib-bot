"""Проверка: быстрый движок G0 == боевой backtest.simulate_symbol (те же сигналы)."""
import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория (agents/, data/, backtest.py)
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import sys
import time
from datetime import date

import backtest
from agents.data_agent import Candle, CandleSeries
import research_engine as re_

CHECK = sys.argv[1:] or ["yahoo__AAPL", "yahoo__JPM", "yahoo__^GSPC", "binance__BTCUSDT"]
LIMIT = 2600

ok_all = True
for name in CHECK:
    rows = json.loads((re_.CACHE / f"{name}.json").read_text(encoding="utf-8"))[-LIMIT:]
    candles = [Candle(dt=date.fromisoformat(r[0]), open=r[1], high=r[2], low=r[3], close=r[4], volume=int(r[5])) for r in rows]
    series = CandleSeries(symbol=name, exchange_or_source="x", timeframe="1D", candles=candles, fetched_via="", fetch_note="")
    t0 = time.time()
    prod = backtest.simulate_symbol(series)
    tp = time.time() - t0

    # тот же срез в исследовательский движок
    inst = re_.load_inst(re_.CACHE / f"{name}.json")
    if inst is None:
        print(f"{name}: пропуск (мало баров)")
        continue
    k = max(0, len(inst.c) - LIMIT)
    for attr in ("o", "h", "l", "c", "v"):
        setattr(inst, attr, getattr(inst, attr)[k:])
    inst.dates = inst.dates[k:]
    inst.atr14 = re_._atr(inst.h, inst.l, inst.c, 14)
    inst.sma200 = re_._rolling_mean(inst.c, 200)
    mine = re_.gen_global(inst, window=None, watch=re_.WATCH_PROD, start=backtest._EARLIEST_T)

    p = [(s.fired_idx, s.alert_level) for s in prod]
    m = [(s.t, s.level) for s in mine]
    same = p == m
    ok_all &= same
    print(f"{name:22s} prod={len(p):3d} mine={len(m):3d} identical={same}  (prod {tp:.1f}s)")
    if not same:
        print("   prod only:", sorted(set(p) - set(m))[:6])
        print("   mine only:", sorted(set(m) - set(p))[:6])
print("ALL IDENTICAL" if ok_all else "MISMATCH")
