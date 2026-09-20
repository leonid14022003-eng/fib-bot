"""
Широкий скринер (14 сентября 2026) -- пятая ветка, расширяет охват бота с
15 инструментов (screener.py, платный FMP) до широкого TradFi-универсума
(data/tradfi_universe.json, 248 инструментов из десктопного FibonacciDesk,
242 из них с бесплатным Yahoo-маппингом -- см. data/broad_universe.py) через
Yahoo Finance (agents/data_agent.py::load_yahoo_daily(), без ключа).

Объединяет "лучшие качества обеих версий" (решение Леонида, 14 сентября
2026), а не заменяет живую систему:
  - тот же боевой конвейер, что и у screener.py -- Data -> Fibo (5 баров +
    старейший непробитый экстремум) -> Price-Behavior -> Verification ->
    Chart -> Dispatch. Переиспользует screener.scan_instrument() как есть
    (см. докстринг там про fetch_fn -- уже спроектирован как точка
    расширения под другой источник данных, никакого форка не потребовалось).
  - ПЛЮС agents/local_grid_agent.py (PDF-алгоритм локальных сеток, коммит
    cbb2f71, 9 сентября) как новый, отдельно помеченный тип алерта --
    возможность, которой у живой системы вообще нет.

4 существующие боевые ветки (run_live.sh, run_screener.sh,
run_analyst_report.sh, run_mtf_screener.sh) этот файл не трогает и от них не
зависит -- отдельная память (BROAD_SCREENER_STATE_PATH /
BROAD_LOCAL_GRID_STATE_PATH ниже), отдельный вызов из cron (по отдельному
решению, см. план).

Инструменты из screener.py's INSTRUMENTS исключены отсюда по символу --
чтобы один и тот же тикер не шёл параллельно двумя источниками данных с
потенциально разными числами (см. _deduped_instruments() ниже).

Каденс -- задаётся снаружи (crontab), не этим файлом, как и у screener.py.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from agents.chart_agent import render_chart
from agents.data_agent import CandleSeries, load_yahoo_daily
from agents.dispatch_agent import (
    LevelWatchState,
    format_local_grid_message,
    format_message,
    send_photo_via_telegram,
    send_via_telegram,
    should_send_level_watch,
)
from agents.local_grid_agent import LocalState, run_local_grid_chain
from data.broad_universe import provider_symbol, yahoo_mappable_instruments
from recipients import ALL_RECIPIENTS
from screener import INSTRUMENTS, Instrument, scan_instrument

ROOT = Path(__file__).resolve().parent
BROAD_SCREENER_STATE_PATH = ROOT / "output" / "broad_screener_state.json"
BROAD_LOCAL_GRID_STATE_PATH = ROOT / "output" / "broad_local_grid_state.json"
LIVE = os.environ.get("FIB_BOT_LIVE") == "1"
WATCH_LEVELS = (0.618, 0.786, 1.0)  # тот же порог, что у screener.py -- не плодим разные критерии без нужды


def _deduped_instruments() -> list[Instrument]:
    """Широкий список минус то, что уже покрыто screener.py (по символу) --
    см. докстринг файла про риск двух источников на один тикер."""
    already_covered = {inst.symbol for inst in INSTRUMENTS}
    result = []
    for a in yahoo_mappable_instruments():
        symbol = provider_symbol(a)
        if symbol in already_covered:
            continue
        label = a.get("asset_name") or a.get("underlying_ticker")
        result.append(Instrument(label=label, symbol=symbol, exchange_hint=a.get("market", ""), source="yahoo"))
    return result


BROAD_INSTRUMENTS: list[Instrument] = _deduped_instruments()


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
    data = _load_json_state(BROAD_SCREENER_STATE_PATH)
    return {
        symbol: LevelWatchState(structure_id=v.get("structure_id"), last_alerted_level=v.get("last_alerted_level"))
        for symbol, v in data.items()
    }


def _save_screener_state(state: dict[str, LevelWatchState]) -> None:
    data = {
        symbol: {"structure_id": s.structure_id, "last_alerted_level": s.last_alerted_level}
        for symbol, s in state.items()
    }
    _save_json_state(BROAD_SCREENER_STATE_PATH, data)


def _load_local_grid_state() -> dict[str, LocalGridSeenState]:
    data = _load_json_state(BROAD_LOCAL_GRID_STATE_PATH)
    return {
        symbol: LocalGridSeenState(last_seq=v.get("last_seq", 0), last_state=v.get("last_state", ""))
        for symbol, v in data.items()
    }


def _save_local_grid_state(state: dict[str, LocalGridSeenState]) -> None:
    data = {symbol: {"last_seq": s.last_seq, "last_state": s.last_state} for symbol, s in state.items()}
    _save_json_state(BROAD_LOCAL_GRID_STATE_PATH, data)


def _new_local_grid_events(symbol: str, chain, state: dict[str, LocalGridSeenState]) -> list:
    """
    Какие LocalGrid-записи из цепочки ещё не были отправлены для этого
    символа. run_local_grid_chain() каждый раз пересчитывает ВСЮ цепочку с
    нуля (без памяти между вызовами, см. agents/local_grid_agent.py) --
    "новое с прошлого прогона" здесь определяется снаружи, по (seq, state):
    seq строго возрастает по ходу цепочки и никогда не откатывается назад
    (Шаг 9/10 всегда увеличивает seq), а внутри одного seq состояние может
    только прогрессировать вперёд (формирование -> зафиксирована ->
    завершена), поэтому "последний увиденный (seq, state)" -- достаточная
    память, без хранения полной истории.

    Если цепочка целиком обгоняет последний увиденный seq (сервис долго не
    запускался, или локальная сетка сформировалась и завершилась между двумя
    прогонами) -- эта конкретная запись просто отправляется сразу в своём
    актуальном состоянии, без попытки восстановить пропущенные промежуточные
    уведомления (тот же компромисс, что и у should_send_level_watch --
    память не бесконечная, честно фиксирует то, что видно СЕЙЧАС).
    """
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


def run_broad_screener() -> None:
    print("=" * 70)
    print(f"ШИРОКИЙ СКРИНЕР -- {len(BROAD_INSTRUMENTS)} инструментов (Yahoo Finance), порог {min(WATCH_LEVELS):g} и глубже")
    print("=" * 70)

    screener_state = _load_screener_state()
    local_grid_state = _load_local_grid_state()

    recipients = ALL_RECIPIENTS
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if LIVE else None

    level_watch_count = 0
    local_grid_count = 0
    error_count = 0

    for instrument in BROAD_INSTRUMENTS:
        r = scan_instrument(instrument, fetch_fn=load_yahoo_daily)
        if r["status"] != "OK":
            error_count += 1
            continue

        # --- Уровневый watch (тот же порог/логика, что у screener.py) ---
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

        # --- Локальные сетки (agents/local_grid_agent.py, новый тип алерта) ---
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
        f"Итог: {len(BROAD_INSTRUMENTS) - error_count}/{len(BROAD_INSTRUMENTS)} успешно проверено, "
        f"{error_count} с ошибкой, {level_watch_count} уровневых алертов, {local_grid_count} алертов по локальным сеткам"
    )

    if LIVE:
        _save_screener_state(screener_state)
        _save_local_grid_state(local_grid_state)


def prime_state() -> None:
    """
    Одноразовое "остывание" памяти дедупликации ПЕРЕД первым боевым
    включением (14 сентября 2026, по итогам dry run: 426 алертов по
    локальным сеткам за один первый прогон на 233 инструментах -- у
    local_grid_agent.py нет памяти между вызовами, он каждый раз пересчитывает
    всю цепочку заново, см. докстринг _new_local_grid_events выше).

    Проходит по всем инструментам той же логикой, что и run_broad_screener(),
    но НИКОГДА не вызывает render_chart/send_photo_via_telegram/
    send_via_telegram -- только считает "текущее" состояние (какой уровень
    уже достигнут, какая локальная сетка сейчас активна) и сохраняет его в
    output/broad_screener_state.json / output/broad_local_grid_state.json.
    После этого первый прогон run_broad_screener() (в т.ч. боевой, с
    FIB_BOT_LIVE=1) увидит эти состояния как "уже было", и пришлёт алерт
    только когда что-то РЕАЛЬНО изменится с этого момента -- как и было
    задумано для остальных 4 боевых веток с самого начала (см. историю
    output/screener_state.json).

    Пишет файлы памяти БЕЗ учёта FIB_BOT_LIVE (в отличие от
    run_broad_screener()) -- это отдельное, явно вызываемое действие
    (`python3 broad_screener.py --prime`), а не побочный эффект обычного
    прогона.
    """
    print("=" * 70)
    print(f"ПРАЙМИНГ ПАМЯТИ -- {len(BROAD_INSTRUMENTS)} инструментов, ничего не отправляется")
    print("=" * 70)

    screener_state = _load_screener_state()
    local_grid_state = _load_local_grid_state()
    primed_level = primed_local = error_count = 0

    for instrument in BROAD_INSTRUMENTS:
        r = scan_instrument(instrument, fetch_fn=load_yahoo_daily)
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
        # Возвращённые события сознательно отбрасываются -- нужен только
        # побочный эффект обновления local_grid_state внутри функции.
        primed_local += len(_new_local_grid_events(instrument.symbol, chain, local_grid_state))

    _save_screener_state(screener_state)
    _save_local_grid_state(local_grid_state)
    print()
    print(
        f"Готово: {len(BROAD_INSTRUMENTS) - error_count}/{len(BROAD_INSTRUMENTS)} учтено, "
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
                f"({len(BROAD_INSTRUMENTS)} инструментов, Yahoo Finance, без ключа). "
                f"Ничего не уходит в Telegram и не пишется в output/broad_*_state.json."
            )
        run_broad_screener()
