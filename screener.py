"""
Скринер по списку инструментов -- Задача №1 из claude/brief.md, часть с
"дай список, найди где коррекция глубокая".

В отличие от orchestrator.py (который непрерывно следит за ОДНИМ
инструментом, сейчас IBM, и присылает алерт по нему одному), этот скрипт
проходит по СПИСКУ инструментов (INSTRUMENTS ниже), для каждого строит тот
же самый пайплайн (Data -> Structure/Fibo -> Price-Behavior -> Verification)
и включает в шорт-лист только те, что реально дошли до заданной глубины
коррекции. Порог -- 0.618 и глубже (WATCH_LEVELS ниже), то же самое
решение, что Леонид принял 24 августа для точечного watch по IBM (см.
claude/multi-agent-architecture.md) -- специально тот же порог, чтобы не
плодить разные критерии "достаточно глубоко" без необходимости.

Список инструментов -- черновик из claude/brief.md (раздел "Черновой
список инструментов"), проверен вживую на сервере 24 августа. Из
изначальных 17 три пришлось убрать: нефть WTI (CLUSD), природный газ
(NGUSD) и Nasdaq 100 (^NDX) возвращали от FMP не "тикер не найден", а
`402 Payment Required` -- эти три актива просто не входят в план Starter
(остальные индексы и товары на том же плане отработали нормально, так что
дело не в формате тикера). По прямому решению Леонида (24 августа) эти
три инструмента убраны из списка, а не оставлены как вечно ошибающиеся --
если план FMP когда-нибудь поменяется, можно будет вернуть их обратно.

Ни один провал по одному инструменту не прерывает проверку остальных --
см. scan_instrument() -- так что даже если в будущем какой-то тикер снова
перестанет резолвиться, это не сломает остальной список.

Каденс задаётся снаружи (crontab на VPS), не этим файлом -- как и в
orchestrator.py. По решению Леонида (24 августа) скринер идёт РЕЖЕ, чем
точечный IBM-watch -- раз в час, а не раз в 15 минут (коррекция до 0.618
не случается настолько быстро, чтобы имело смысл проверять чаще).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agents.chart_agent import render_chart
from agents.context_agent import build_context_note, get_upcoming_macro_events
from agents.data_agent import load_fmp_daily
from agents.dispatch_agent import (
    AnalysisBundle,
    LevelWatchState,
    Recipient,
    format_message,
    retracement_fraction,
    send_photo_via_telegram,
    send_via_telegram,
    should_send_level_watch,
)
from agents.fibo_agent import build_global_fibo, find_fractal_swing_extremes, find_oldest_unbroken_extremes
from agents.intraday_agent import build_intraday_note
from agents.price_behavior_agent import nearest_level, recent_level_events
from agents.verification_agent import cross_check_structures, format_consensus_note, run_checklist

ROOT = Path(__file__).resolve().parent
SCREENER_STATE_PATH = ROOT / "output" / "screener_state.json"
LIVE = os.environ.get("FIB_BOT_LIVE") == "1"
WATCH_LEVELS = (0.618, 0.786, 1.0)  # тот же порог, что и у точечного watch по IBM (24 августа)


@dataclass(frozen=True)
class Instrument:
    label: str  # человеко-читаемое имя, для сообщений
    symbol: str  # тикер, как передаётся в load_fmp_daily / load_binance_daily
    exchange_hint: str
    source: str = "fmp"  # "fmp" (акции/индексы/товары) или "binance" (крипта) --
    # добавлено 5 сентября для opportunity_scanner.py, дефолт сохраняет старое
    # поведение для существующих 15 инструментов (все они -- "fmp")


# Черновик из claude/brief.md -- индексы, товары, мегакапы. Проверено вживую
# 24 августа: 14/17 рабочие на плане FMP Starter. Nasdaq 100, нефть WTI и
# природный газ убраны -- см. докстринг файла выше про 402 Payment Required.
INSTRUMENTS: list[Instrument] = [
    # Индексы
    Instrument("S&P 500", "^GSPC", "Индекс (US)"),
    Instrument("Dow Jones Industrial Average", "^DJI", "Индекс (US)"),
    Instrument("Russell 2000", "^RUT", "Индекс (US)"),
    # Товары
    Instrument("Золото", "GCUSD", "Товар"),
    Instrument("Серебро", "SIUSD", "Товар"),
    # Мегакапы (по разным секторам, как в брифе)
    Instrument("Apple", "AAPL", "NASDAQ"),
    Instrument("Microsoft", "MSFT", "NASDAQ"),
    Instrument("Nvidia", "NVDA", "NASDAQ"),
    Instrument("Tesla", "TSLA", "NASDAQ"),
    Instrument("Amazon", "AMZN", "NASDAQ"),
    Instrument("Alphabet (Google)", "GOOGL", "NASDAQ"),
    Instrument("Meta", "META", "NASDAQ"),
    Instrument("Berkshire Hathaway", "BRK-B", "NYSE"),
    Instrument("JPMorgan", "JPM", "NYSE"),
]


def scan_instrument(instrument: Instrument, fetch_fn=load_fmp_daily) -> dict:
    """
    Прогоняет ОДИН инструмент через Data -> Structure/Fibo -> Price-Behavior
    -> Verification. Никогда не бросает исключение наружу -- любая ошибка
    (плохой тикер, нет данных, структура не подтвердилась) превращается в
    статус в возвращаемом словаре, чтобы один проблемный тикер не обрывал
    проверку всего списка (регламент, раздел 2: честно показывать проблему,
    не маскировать и не выдумывать результат).

    fetch_fn -- параметр для тестов (позволяет подставить фиктивный
    источник данных вместо реального load_fmp_daily).
    """
    try:
        series = fetch_fn(instrument.symbol, exchange_hint=instrument.exchange_hint)
    except Exception as e:  # сеть, неверный тикер, пустой ответ FMP и т.п.
        return {"instrument": instrument, "status": "DATA_ERROR", "detail": str(e)}
    try:
        structure = build_global_fibo(series, extremes_fn=find_oldest_unbroken_extremes)
    except ValueError as e:  # экстремум не прошёл правило "5 свечей слева"
        return {"instrument": instrument, "status": "NO_STRUCTURE", "detail": str(e)}
    current_price = series.candles[-1].close
    n = nearest_level(structure, current_price)
    events = recent_level_events(structure, series.candles, lookback=10)
    report = run_checklist(structure, series.exchange_or_source)
    # Настоящая независимая сверка (27 августа, по запросу Леонида --
    # "настоящая сверка структур"), тот же метод, что и в orchestrator.py:
    # независимый фрактальный поиск точек разворота, сравнение через
    # cross_check_structures(). Best-effort -- НЕ блокирует сам скринер и
    # НЕ входит в run_checklist(), только печатается в run_screener() ниже.
    # 27 августа (продолжение, по отзыву "брокера" -- см. project doc):
    # format_consensus_note() -- тот же общий форматтер, что и у
    # orchestrator.py -- готовая строка, которую теперь можно передать не
    # только в консоль (см. run_screener() ниже), но и в само сообщение
    # получателю через format_message(consensus_note=...).
    try:
        fractal_structure = build_global_fibo(series, extremes_fn=find_fractal_swing_extremes)
        consensus = cross_check_structures(structure, fractal_structure)
        consensus_note = format_consensus_note(consensus.agree, consensus.detail)
    except ValueError as e:
        consensus_note = format_consensus_note(None, str(e))
    bundle = AnalysisBundle(
        symbol=series.symbol,
        source_tag=series.exchange_or_source,
        timeframe=series.timeframe,
        period_desc=f"{series.start} .. {series.end} ({len(series.candles)} дневных свечей)",
        structure=structure,
        nearest=n,
        recent_events=events,
        checklist=report,
    )
    return {
        "instrument": instrument,
        "status": "OK",
        "bundle": bundle,
        "fraction": retracement_fraction(bundle),
        # Свечи -- отдельно от bundle: AnalysisBundle их не хранит (см.
        # dispatch_agent.py), а render_chart() для картинки по кандидату
        # (26 августа) они нужны.
        "candles": series.candles,
        "consensus_note": consensus_note,  # 27 августа, см. комментарий выше -- не блокирует, только для наблюдения
    }


def _load_screener_state() -> dict[str, LevelWatchState]:
    try:
        data = json.loads(SCREENER_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return {
        symbol: LevelWatchState(
            structure_id=v.get("structure_id"), last_alerted_level=v.get("last_alerted_level")
        )
        for symbol, v in data.items()
    }


def _save_screener_state(state: dict[str, LevelWatchState]) -> None:
    data = {
        symbol: {"structure_id": s.structure_id, "last_alerted_level": s.last_alerted_level}
        for symbol, s in state.items()
    }
    SCREENER_STATE_PATH.parent.mkdir(exist_ok=True)
    SCREENER_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _format_shortlist_message(candidates: list[tuple[dict, LevelWatchState]]) -> str:
    """
    Общий шорт-лист как несколько HTML-карточек format_message() подряд,
    разделённых видимой чертой -- 25 августа, тот же визуальный редизайн,
    что и у точечного IBM-watch (см. agents/dispatch_agent.py). Каждая
    карточка сама несёт заголовок с именем инструмента и alert_level, так
    что отдельный "=== label ===" сверху больше не нужен -- не дублируем.

    Известное ограничение (не новое -- было и у старого текстового формата,
    просто не проявлялось: пока что кандидатом почти всегда была только
    Meta): у Telegram sendMessage лимит 4096 символов на одно сообщение.
    Если кандидатов в шорт-листе станет много одновременно, комбинированное
    сообщение может это превысить и send_via_telegram получит ERROR 400 на
    всех получателей сразу. Разбиение на несколько сообщений при
    превышении лимита пока не реализовано -- сознательно, чтобы не
    усложнять раньше, чем это реально понадобится (см. README).
    """
    header = f"🔎 <b>ФИБО-скринер</b> — {len(candidates)} инструмент(ов) с глубокой коррекцией (0.618+)"
    cards = [
        format_message(
            r["bundle"],
            alert_level=new_state.last_alerted_level,
            display_name=r["instrument"].label,
            consensus_note=r["consensus_note"],
            intraday_note=r.get("intraday_note"),
        )
        for r, new_state in candidates
    ]
    divider = "\n" + "─" * 24 + "\n"
    return header + "\n\n" + divider.join(cards)


def run_screener() -> None:
    print("=" * 70)
    print(f"ФИБО-СКРИНЕР -- {len(INSTRUMENTS)} инструментов, порог {min(WATCH_LEVELS):g} и глубже")
    print("=" * 70)

    state = _load_screener_state()
    results = [scan_instrument(instrument) for instrument in INSTRUMENTS]

    for r in results:
        inst = r["instrument"]
        if r["status"] == "OK":
            print(f"  {inst.label:30s} [{inst.symbol:10s}] отход {r['fraction']:.3f} -- OK")
            print(f"    {r['consensus_note']} [пока не блокирует]")
        else:
            print(f"  {inst.label:30s} [{inst.symbol:10s}] {r['status']}: {r['detail']}")

    candidates: list[tuple[dict, LevelWatchState]] = []
    for r in results:
        if r["status"] != "OK":
            continue
        symbol = r["instrument"].symbol
        ok, reason, new_state = should_send_level_watch(
            r["bundle"], state.get(symbol, LevelWatchState()), watch_levels=WATCH_LEVELS
        )
        print(f"    -> {r['instrument'].label}: {'КАНДИДАТ' if ok else 'пропуск'} -- {reason}")
        if ok:
            # Внутридневное подтверждение (раздел 7.3, 2 сентября 2026,
            # agents/intraday_agent.py) -- считаем ТОЛЬКО для реальных
            # кандидатов, не для всех INSTRUMENTS на каждом часовом прогоне.
            # В отличие от фрактальной сверки (переиспользует уже
            # полученные дневные свечи scan_instrument(), без новой сети),
            # это 2 НОВЫХ запроса к FMP (1H+4H) на кандидата -- считать их
            # для всех 14 инструментов каждый час было бы неоправданным
            # расходом лимита API ради подавляющего большинства
            # инструментов, которые в шорт-лист всё равно не попадут.
            # Best-effort, тот же принцип, что и у Context Agent ниже.
            intraday_note = None
            try:
                intraday_note = build_intraday_note(
                    symbol, r["bundle"].nearest.current_price, exchange_hint=r["bundle"].source_tag
                )
            except Exception as e:
                print(f"    Intraday Agent ({r['instrument'].label}): пропущено ({e})")
            r["intraday_note"] = intraday_note
            candidates.append((r, new_state))
            state[symbol] = new_state

    errors = [r for r in results if r["status"] != "OK"]
    print()
    print(
        f"Итог: {len(results) - len(errors)}/{len(results)} успешно проверено, "
        f"{len(errors)} с ошибкой, {len(candidates)} кандидатов в шорт-лист"
    )

    if not candidates:
        print("Кандидатов нет -- сообщение не отправляется.")
        return

    message = _format_shortlist_message(candidates)
    print()
    print("--- Шорт-лист (формат раздела 24 на каждого кандидата) ---")
    print(message)

    recipients = [
        Recipient(label="Леонид (@vaskodevasko)", telegram_chat_id="885989790"),
        Recipient(label="Сергей (@sergikvsl)", telegram_chat_id="1253087193"),
        Recipient(label="Pavel", telegram_chat_id="980723803"),
    ]
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if LIVE else None

    # Сначала графики -- по одному на кандидата (у каждого своя структура,
    # в одну картинку не объединить), потом как и раньше единое текстовое
    # сообщение-шортлист. Тот же порядок и принцип, что и в orchestrator.py
    # (26 августа, решение Леонида "Фото + весь текущий текст"). По
    # кандидату -- отдельный sendPhoto, не альбом (sendMediaGroup общается
    # с Bot API иначе и заметно сложнее) -- проще и достаточно, в духе
    # остального проекта: не усложнять раньше, чем реально понадобится
    # (см. докстринг _format_shortlist_message про лимит 4096 символов).
    print()
    print("--- Отправка графиков (по кандидату) ---")
    for r, new_state in candidates:
        photo_png = render_chart(
            r["bundle"].structure,
            r["candles"],
            r["bundle"].nearest.current_price,
            r["bundle"].symbol,
            r["bundle"].timeframe,
            alert_level=new_state.last_alerted_level,
            display_name=r["instrument"].label,
        )
        caption = f"{r['instrument'].label} ({r['instrument'].symbol}) — коррекция дошла до {new_state.last_alerted_level:g}"
        photo_result = send_photo_via_telegram(photo_png, recipients, bot_token=bot_token, caption=caption)
        print(f"  {r['instrument'].label}: dry_run={photo_result['dry_run']}, {photo_result['photo_bytes']} байт")
        for entry in photo_result["sent_to"]:
            print(f"    {entry['recipient']}: {entry['status']}")

    # Context Agent (26 августа) -- та же необязательная пометка, что и в
    # orchestrator.py, один раз поверх общего текста шорт-листа (не на
    # каждую карточку кандидата отдельно -- незачем повторять одно и то же
    # событие N раз). Best-effort, см. комментарий в orchestrator.py.
    if LIVE:
        try:
            events = get_upcoming_macro_events(datetime.now())
            note = build_context_note(events)
            if note:
                message += "\n\n" + note
        except Exception as e:
            print(f"Context Agent: пропущено ({e})")

    result = send_via_telegram(message, recipients, bot_token=bot_token)
    print()
    print(f"--- Отправка текста (dry_run={result['dry_run']}) ---")
    for entry in result["sent_to"]:
        print(f"  {entry['recipient']}: {entry['status']}")

    if LIVE:
        _save_screener_state(state)


if __name__ == "__main__":
    if not LIVE:
        print(
            f"FIB_BOT_LIVE не задана -- у скринера нет офлайн-демо-данных на "
            f"{len(INSTRUMENTS)} инструментов (в отличие от orchestrator.py/IBM, "
            f"там есть локальная фикстура). Без LIVE запускать бессмысленно -- "
            f"задай FIB_BOT_LIVE=1 и MARKET_DATA_API_KEY."
        )
        raise SystemExit(1)
    run_screener()
