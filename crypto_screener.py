"""
Крипто-скринер (20 сентября 2026) -- 6-я ветка, топ-50 криптовалют по
рыночной капитализации CoinGecko среди тех, что торгуются как USDT
perpetual на Binance Futures. Идея -- как в десктопной FibonacciDesk
0.19.0 (Documents/README.md, "Изменения 0.19.0": "Добавлены 50
криптовалют Binance"), перенесена в бота по прямому решению Леонида,
20 сентября 2026.

ВАЖНО, чем это отличается от opportunity_scanner.py: та ветка сканирует
TradFi-инструменты, ТОКЕНИЗИРОВАННЫЕ на Binance (XAUUSDT, NVDAUSDT и
т.п.) -- анализ идёт по РЕАЛЬНОМУ графику базового актива через FMP, а
Binance-контракт только показывает, где торговать. Здесь -- наоборот:
анализируется цена САМОЙ монеты на Binance напрямую (load_binance_daily),
потому что у чистой крипты (BTC, ETH, SOL...) нет базового актива, на
который можно посмотреть вместо неё.

Это НЕ разворот решения 91be25a (opportunity_scanner остаётся
TradFi-only, не тронут этим файлом) -- отдельная, новая, явно
запрошенная ветка. До этого чистая крипта была явно и дважды исключена
из бота (89c6766 -> 6f64b13 -> 91be25a, см. `git log`) -- если в будущем
это решение снова захотят пересмотреть, пусть это будет столь же явным
решением, как и текущее.

Регистр (топ-50) строится ЗАНОВО при каждом запуске -- см.
data/crypto_universe.py::build_crypto_universe() -- а не кэшируется в
файл, в отличие от data/broad_universe.py (там источник статический,
здесь капитализация меняется). Собирается на уровне функции, а не при
импорте модуля (в отличие от BROAD_INSTRUMENTS в broad_screener.py) --
чтобы просто импортировать этот файл в тестах не означало делать 2
реальных сетевых запроса.

Тот же боевой конвейер, что у broad_screener.py -- screener.scan_instrument
(с локальной обёрткой над load_binance_daily, см. _load_binance_for_scan
ниже -- у load_binance_daily нет параметра exchange_hint, а
scan_instrument всегда его передаёт) + agents/local_grid_agent.py, тот же
принцип дедупликации (_new_local_grid_events -- код продублирован из
broad_screener.py умышленно, не вынесен в общий модуль: то же решение,
что и там, каждая ветка самодостаточна).

Полностью отдельная память (output/crypto_screener_state.json,
output/crypto_local_grid_state.json) -- 5 существующих боевых веток
(включая broad_screener.py) этот файл не трогает и от них не зависит.

Каденс -- задаётся снаружи (crontab), не этим файлом.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from agents.chart_agent import render_chart
from agents.data_agent import CandleSeries, load_binance_daily
from agents.dispatch_agent import (
    LevelWatchState,
    format_local_grid_message,
    format_message,
    send_photo_via_telegram,
    send_via_telegram,
    should_send_level_watch,
)
from agents.local_grid_agent import run_local_grid_chain
from data.crypto_universe import build_crypto_universe
from recipients import ALL_RECIPIENTS
from screener import Instrument, scan_instrument

ROOT = Path(__file__).resolve().parent
CRYPTO_SCREENER_STATE_PATH = ROOT / "output" / "crypto_screener_state.json"
CRYPTO_LOCAL_GRID_STATE_PATH = ROOT / "output" / "crypto_local_grid_state.json"
LIVE = os.environ.get("FIB_BOT_LIVE") == "1"
WATCH_LEVELS = (0.618, 0.786, 1.0)  # тот же порог, что у screener.py/broad_screener.py
TOP_N = 50


def _load_binance_for_scan(symbol: str, exchange_hint: str = "") -> CandleSeries:
    """Адаптер под сигнатуру, которую всегда использует scan_instrument
    (instrument.symbol, exchange_hint=instrument.exchange_hint) --
    load_binance_daily() не принимает exchange_hint (у крипты нет понятия
    биржи-подсказки, ключ один -- символ Binance)."""
    return load_binance_daily(symbol, market="futures")


def _build_instruments() -> tuple[list[Instrument], list[dict]]:
    matched, skipped = build_crypto_universe(top_n=TOP_N)
    instruments = [
        Instrument(label=c["name"], symbol=c["binance_symbol"], exchange_hint="", source="binance")
        for c in matched
    ]
    return instruments, skipped


@dataclass
class LocalGridSeenState:
    last_seq: int = 0
    last_state: str = ""


def _load_json_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_json_state(path: Path, data: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_screener_state() -> dict[str, LevelWatchState]:
    data = _load_json_state(CRYPTO_SCREENER_STATE_PATH)
    return {
        symbol: LevelWatchState(structure_id=v.get("structure_id"), last_alerted_level=v.get("last_alerted_level"))
        for symbol, v in data.items()
    }


def _save_screener_state(state: dict[str, LevelWatchState]) -> None:
    data = {
        symbol: {"structure_id": s.structure_id, "last_alerted_level": s.last_alerted_level}
        for symbol, s in state.items()
    }
    _save_json_state(CRYPTO_SCREENER_STATE_PATH, data)


def _load_local_grid_state() -> dict[str, LocalGridSeenState]:
    data = _load_json_state(CRYPTO_LOCAL_GRID_STATE_PATH)
    return {
        symbol: LocalGridSeenState(last_seq=v.get("last_seq", 0), last_state=v.get("last_state", ""))
        for symbol, v in data.items()
    }


def _save_local_grid_state(state: dict[str, LocalGridSeenState]) -> None:
    data = {symbol: {"last_seq": s.last_seq, "last_state": s.last_state} for symbol, s in state.items()}
    _save_json_state(CRYPTO_LOCAL_GRID_STATE_PATH, data)


def _new_local_grid_events(symbol: str, chain, state: dict[str, LocalGridSeenState]) -> list:
    """Код и обоснование идентичны broad_screener.py::_new_local_grid_events
    -- см. докстринг там, продублировано умышленно (см. докстринг файла)."""
    seen = state.get(symbol, LocalGridSeenState())
    candidates = list(chain.completed)
    if chain.current is not None:
        candidates.append(chain.current)
    new_events = []
    for local in candidates:
        if local.seq > seen.last_seq or (local.seq == seen.last_seq and local.state.value != seen.last_state):
            new_events.append(local)
    if candidates:
        last = candidates[-1]
        state[symbol] = LocalGridSeenState(last_seq=last.seq, last_state=last.state.value)
    return new_events


def run_crypto_screener() -> None:
    instruments, skipped = _build_instruments()
    print("=" * 70)
    print(f"КРИПТО-СКРИНЕР -- топ-{len(instruments)} по капитализации CoinGecko (Binance USDT perpetual)")
    print(f"Пропущено при отборе (из просмотренного диапазона рейтинга): {len(skipped)}")
    print("=" * 70)

    screener_state = _load_screener_state()
    local_grid_state = _load_local_grid_state()

    recipients = ALL_RECIPIENTS
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if LIVE else None

    level_watch_count = 0
    local_grid_count = 0
    error_count = 0

    for instrument in instruments:
        r = scan_instrument(instrument, fetch_fn=_load_binance_for_scan)
        if r["status"] != "OK":
            error_count += 1
            continue

        # --- Уровневый watch (тот же порог/логика, что у screener.py/broad_screener.py) ---
        ok, reason, new_level_state = should_send_level_watch(
            r["bundle"], screener_state.get(instrument.symbol, LevelWatchState()), watch_levels=WATCH_LEVELS
        )
        if ok:
            level_watch_count += 1
            screener_state[instrument.symbol] = new_level_state
            photo_png = render_chart(
                r["bundle"].structure, r["candles"], r["bundle"].nearest.current_price,
                r["bundle"].symbol, r["bundle"].timeframe,
                alert_level=new_level_state.last_alerted_level, display_name=instrument.label,
            )
            caption = f"{instrument.label} ({instrument.symbol}) — коррекция дошла до {new_level_state.last_alerted_level:g}"
            send_photo_via_telegram(photo_png, recipients, bot_token=bot_token, caption=caption)
            message = format_message(
                r["bundle"], alert_level=new_level_state.last_alerted_level,
                display_name=instrument.label, consensus_note=r["consensus_note"],
            )
            send_via_telegram(message, recipients, bot_token=bot_token)
            print(f"  [LEVEL] {instrument.label} ({instrument.symbol}): {reason}")

        # --- Локальные сетки (agents/local_grid_agent.py) ---
        try:
            series_for_local = CandleSeries(
                symbol=r["bundle"].symbol, exchange_or_source=r["bundle"].source_tag,
                timeframe=r["bundle"].timeframe, candles=r["candles"], fetched_via="", fetch_note="",
            )
            chain = run_local_grid_chain(series_for_local)
        except ValueError as e:
            print(f"  [LOCAL_GRID] {instrument.label}: пропущено ({e})")
            continue

        for local in _new_local_grid_events(instrument.symbol, chain, local_grid_state):
            local_grid_count += 1
            message = format_local_grid_message(instrument.symbol, chain.global_grid, local, display_name=instrument.label)
            send_via_telegram(message, recipients, bot_token=bot_token)
            print(f"  [LOCAL_GRID] {instrument.label} ({instrument.symbol}): №{local.seq} -> {local.state.value}")

    print()
    print(
        f"Итог: {len(instruments) - error_count}/{len(instruments)} успешно проверено, "
        f"{error_count} с ошибкой, {level_watch_count} уровневых алертов, {local_grid_count} алертов по локальным сеткам"
    )

    if LIVE:
        _save_screener_state(screener_state)
        _save_local_grid_state(local_grid_state)


def prime_state() -> None:
    """Одноразовое "остывание" памяти дедупликации перед первым боевым
    включением -- тот же принцип и то же обоснование, что и у
    broad_screener.py::prime_state() (см. докстринг там): без прайминга
    первый боевой прогон честно нашёл бы кучу "уже существующих" уровней и
    локальных сеток и разослал бы их все разом как единый залп. Пишет
    состояние БЕЗ учёта FIB_BOT_LIVE, никогда не отправляет ничего в
    Telegram."""
    instruments, skipped = _build_instruments()
    print("=" * 70)
    print(f"ПРАЙМИНГ ПАМЯТИ -- {len(instruments)} инструментов, ничего не отправляется")
    print(f"Пропущено при отборе (из просмотренного диапазона рейтинга): {len(skipped)}")
    print("=" * 70)

    screener_state = _load_screener_state()
    local_grid_state = _load_local_grid_state()
    primed_level = primed_local = error_count = 0

    for instrument in instruments:
        r = scan_instrument(instrument, fetch_fn=_load_binance_for_scan)
        if r["status"] != "OK":
            error_count += 1
            continue

        ok, _reason, new_level_state = should_send_level_watch(
            r["bundle"], screener_state.get(instrument.symbol, LevelWatchState()), watch_levels=WATCH_LEVELS
        )
        if ok:
            primed_level += 1
            screener_state[instrument.symbol] = new_level_state

        try:
            series_for_local = CandleSeries(
                symbol=r["bundle"].symbol, exchange_or_source=r["bundle"].source_tag,
                timeframe=r["bundle"].timeframe, candles=r["candles"], fetched_via="", fetch_note="",
            )
            chain = run_local_grid_chain(series_for_local)
        except ValueError:
            continue
        primed_local += len(_new_local_grid_events(instrument.symbol, chain, local_grid_state))

    _save_screener_state(screener_state)
    _save_local_grid_state(local_grid_state)
    print()
    print(
        f"Готово: {len(instruments) - error_count}/{len(instruments)} учтено, "
        f"{error_count} с ошибкой, {primed_level} уровней и {primed_local} локальных сеток отмечены как "
        f"уже известные (не будут отправлены при следующем боевом прогоне)."
    )


if __name__ == "__main__":
    if "--prime" in sys.argv:
        prime_state()
    else:
        if not LIVE:
            print(
                f"FIB_BOT_LIVE не задана -- запускаю в DRY RUN "
                f"(топ-{TOP_N} по капитализации, Binance Futures). "
                f"Ничего не уходит в Telegram и не пишется в output/crypto_*_state.json."
            )
        run_crypto_screener()
