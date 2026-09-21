"""Полная история акций/индексов (всё, что отдаёт Yahoo) -> research_cache_full/ (для проверки "убрать потолок 5 лет")."""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория (agents/, data/, backtest.py)
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import research_fetch_data as F

F.CACHE = Path(__file__).resolve().parent / "research_cache_full"
F.CACHE.mkdir(exist_ok=True)
F.MONTHS_BACK = 800   # ~66 лет -- фактически "всё"

syms = F.yahoo_universe()
print("символов:", len(syms), flush=True)
ok = err = 0
with ThreadPoolExecutor(max_workers=6) as ex:
    for i, f in enumerate(as_completed([ex.submit(F.fetch_yahoo, s) for s in syms]), 1):
        sym, status, info = f.result()
        ok += status in ("ok", "cached")
        err += status == "error"
        if i % 100 == 0:
            print(f"  ... {i}/{len(syms)}", flush=True)
print("готово: ok=%d err=%d" % (ok, err), flush=True)
