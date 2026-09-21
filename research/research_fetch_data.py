"""
Загрузка большого набора исторических дневных данных для исследования
методики (21 сентября 2026). Только чтение публичных источников (Yahoo,
Binance, CoinGecko), ничего не пишет в боевые файлы бота.

Кэш: research_cache/<source>__<symbol>.json  -- список [iso_date,o,h,l,c,v].

Запуск (из корня репозитория):
    python3 research_fetch_data.py
"""
from __future__ import annotations

import sys as _sys
from pathlib import Path as _Path
_sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))  # корень репозитория (agents/, data/, backtest.py)
_sys.path.insert(0, str(_Path(__file__).resolve().parent))

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agents.data_agent import load_binance_daily, load_yahoo_daily
from data.broad_universe import provider_symbol, yahoo_mappable_instruments

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "research_cache"
CACHE.mkdir(exist_ok=True)

MONTHS_BACK = 120  # до 10 лет, где Yahoo столько отдаёт
EXTRA_YAHOO = ["^GSPC", "^DJI", "^RUT", "GC=F", "SI=F", "^IXIC", "CL=F", "HG=F"]


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.=^-]", "_", name)


def _save(kind: str, symbol: str, series) -> None:
    rows = [[str(c.dt), c.open, c.high, c.low, c.close, c.volume] for c in series.candles]
    (CACHE / f"{kind}__{_safe(symbol)}.json").write_text(json.dumps(rows), encoding="utf-8")


def yahoo_universe() -> list[str]:
    syms = [provider_symbol(a) for a in yahoo_mappable_instruments()]
    sp = json.loads((ROOT / "data" / "sp500_constituents.json").read_text(encoding="utf-8"))["constituents"]
    syms += [c["symbol"].replace(".", "-") for c in sp]
    syms += EXTRA_YAHOO
    seen, out = set(), []
    for s in syms:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def fetch_yahoo(symbol: str):
    path = CACHE / f"yahoo__{_safe(symbol)}.json"
    if path.exists():
        return symbol, "cached", None
    try:
        series = load_yahoo_daily(symbol, months_back=MONTHS_BACK)
    except Exception as e:  # noqa: BLE001 -- один плохой тикер не должен рвать весь сбор
        return symbol, "error", str(e)[:120]
    _save("yahoo", symbol, series)
    return symbol, "ok", len(series.candles)


def fetch_binance(symbol: str):
    path = CACHE / f"binance__{_safe(symbol)}.json"
    if path.exists():
        return symbol, "cached", None
    try:
        series = load_binance_daily(symbol, market="futures", limit=1500)
    except Exception as e:  # noqa: BLE001
        return symbol, "error", str(e)[:120]
    _save("binance", symbol, series)
    return symbol, "ok", len(series.candles)


def main() -> None:
    ysyms = yahoo_universe()
    print(f"Yahoo: {len(ysyms)} символов, месяцев назад: {MONTHS_BACK}")
    t0 = time.time()
    ok = err = cached = 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(fetch_yahoo, s) for s in ysyms]
        for i, f in enumerate(as_completed(futs), 1):
            sym, status, info = f.result()
            if status == "ok":
                ok += 1
            elif status == "cached":
                cached += 1
            else:
                err += 1
                print(f"  ERR {sym}: {info}")
            if i % 50 == 0:
                print(f"  ... {i}/{len(ysyms)} ({time.time() - t0:.0f}s)")
    print(f"Yahoo готово: ok={ok} cached={cached} err={err}")

    try:
        from data.crypto_universe import build_crypto_universe

        matched, _ = build_crypto_universe(top_n=50)
        bsyms = [m["binance_symbol"] for m in matched]
    except Exception as e:  # noqa: BLE001 -- CoinGecko может отдать 429
        print(f"CoinGecko недоступен ({e}); беру фиксированный список крупных пар")
        bsyms = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT", "TRXUSDT", "DOGEUSDT", "LINKUSDT",
                 "ADAUSDT", "XLMUSDT", "BCHUSDT", "AVAXUSDT", "LTCUSDT", "SUIUSDT", "HBARUSDT", "AAVEUSDT",
                 "DOTUSDT", "UNIUSDT", "NEARUSDT", "ETCUSDT", "ARBUSDT", "ALGOUSDT", "ICPUSDT", "WLDUSDT"]
    print(f"Binance: {len(bsyms)} символов")
    ok = err = cached = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for f in as_completed([ex.submit(fetch_binance, s) for s in bsyms]):
            sym, status, info = f.result()
            if status == "ok":
                ok += 1
            elif status == "cached":
                cached += 1
            else:
                err += 1
                print(f"  ERR {sym}: {info}")
    print(f"Binance готово: ok={ok} cached={cached} err={err}")


if __name__ == "__main__":
    sys.exit(main())
