"""
Широкий универсум инструментов (14 сентября 2026) -- data/tradfi_universe.json
(248 инструментов из десктопного FibonacciDesk, портирован в data/), плюс
маппинг тикер -> Yahoo-символ для load_yahoo_daily() (agents/data_agent.py).

Маппинг -- прямой порт ~/Downloads/fib-bot_2/engine/core.py::provider_symbol()
(та же логика, что уже проверена вживую в локальном движке): US как есть с
"." -> "-", HKEX дополняется нулями до 4 знаков + ".HK", KRX до 6 знаков +
".KS", SSE + ".SS", явный словарь для 3 FX-пар и 4 фьючерсных товаров. Для
XAU/USD, XAG/USD, XPT/USD, XPD/USD маппинга нет вообще (в прототипе они шли
только через HistData, которую сюда сознательно не переносим в первой версии
-- см. план) -- provider_symbol() возвращает None, такие инструменты
исключаются на этапе построения списка, а не падают с ошибкой посреди
прогона.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent
UNIVERSE_PATH = ROOT / "tradfi_universe.json"

_EXPLICIT_SYMBOLS = {
    "EUR/USD": "EURUSD=X",
    "GBP/USD": "GBPUSD=X",
    "USD/JPY": "JPY=X",
    "Copper (HG)": "HG=F",
    "Henry Hub (NG)": "NG=F",
    "WTI (CL)": "CL=F",
    "Brent (BZ/BRN)": "BZ=F",
}


def provider_symbol(instrument: dict) -> str | None:
    """Тикер -> Yahoo-символ, или None если бесплатного Yahoo-маппинга нет."""
    ticker = instrument.get("underlying_ticker")
    market = instrument.get("market")
    if not ticker:
        return None
    if market == "US":
        return ticker.replace(".", "-")
    if market == "HKEX":
        return ticker.zfill(4) + ".HK"
    if market == "KRX":
        return ticker.zfill(6) + ".KS"
    if market == "SSE":
        return ticker + ".SS"
    return _EXPLICIT_SYMBOLS.get(ticker)


def load_universe() -> list[dict]:
    return json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))["instruments"]


def yahoo_mappable_instruments() -> list[dict]:
    """Только те инструменты из широкого универсума, у которых есть
    бесплатный Yahoo-маппинг (см. докстринг модуля про XAU/XAG/XPT/XPD)."""
    return [a for a in load_universe() if provider_symbol(a) is not None]


if __name__ == "__main__":
    all_instruments = load_universe()
    mappable = yahoo_mappable_instruments()
    print(f"Всего в универсуме: {len(all_instruments)}")
    print(f"С Yahoo-маппингом:  {len(mappable)}")
    print(f"Без маппинга:       {len(all_instruments) - len(mappable)}")
    for a in all_instruments:
        if provider_symbol(a) is None:
            print(f"  пропущен: {a.get('underlying_ticker')} ({a.get('market')})")
