"""
Старый режим should_send(mode="always"/"on_significant") остаётся в
dispatch_agent.py нетронутым (использовался раньше, покрыт тестами), но
в боевом прогоне больше не вызывается.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from agents.chart_agent import render_chart
from agents.context_agent import build_context_note, get_upcoming_macro_events
from agents.data_agent import load_fmp_daily, load_ibm_demo_daily
from agents.dispatch_agent import (
    AnalysisBundle,
    LevelWatchState,
    Recipient,
    format_message,
    send_photo_via_telegram,
    send_via_telegram,
    should_send_level_watch,
)
from agents.fibo_agent import build_global_fibo, build_local_fibo, find_fractal_swing_extremes, find_oldest_unbroken_extremes
from agents.intraday_agent import build_intraday_note
from agents.price_behavior_agent import nearest_level, recent_level_events
from agents.verification_agent import cross_check_structures, format_consensus_note, run_checklist

ROOT = Path(__file__).resolve().parent
ALERT_STATE_PATH = ROOT / "output" / "alert_state.json"
LIVE = os.environ.get("FIB_BOT_LIVE") == "1"
SYMBOL = os.environ.get("FIB_BOT_SYMBOL", "IBM")


def _load_alert_state(symbol: str) -> LevelWatchState:
    try:
        data = json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return LevelWatchState()
    raw = data.get(symbol)
    if not raw:
        return LevelWatchState()
    return LevelWatchState(
        structure_id=raw.get("structure_id"), last_alerted_level=raw.get("last_alerted_level")
    )


def _save_alert_state(symbol: str, state: LevelWatchState) -> None:
    try:
        data = json.loads(ALERT_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {}
    data[symbol] = {"structure_id": state.structure_id, "last_alerted_level": state.last_alerted_level}
    ALERT_STATE_PATH.parent.mkdir(exist_ok=True)
    ALERT_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_once() -> None:
    # 1. Data Agent
    if LIVE:
        series = load_fmp_daily(SYMBOL)
    else:
        series = load_ibm_demo_daily(ROOT / "data" / "ibm_daily_raw.txt")

    print("=" * 70)
    print("DATA AGENT")
    print("=" * 70)
    print(f"{series.symbol} | {series.exchange_or_source} | {series.timeframe}")
    print(f"Диапазон: {series.start} .. {series.end} ({len(series.candles)} свечей)")
    print(f"Получено через: {series.fetched_via}")
    print(f"Заметка: {series.fetch_note}")

    # 2. Structure / Fibo Agent -- глобальная структура
    print()
    print("=" * 70)
    print("STRUCTURE / FIBO AGENT -- глобальный ФИБО")
    print("=" * 70)
    global_structure = build_global_fibo(series, extremes_fn=find_oldest_unbroken_extremes)
    print(f"Направление: {global_structure.direction.value}")
    print(f"Точка 1 (100%) = {global_structure.point1.kind} "
          f"{global_structure.point1.price:.2f} на {global_structure.point1.dt}")
    print(f"Точка 2 (0%)   = {global_structure.point2.kind} "
          f"{global_structure.point2.price:.2f} на {global_structure.point2.dt}")
    print("Уровни:")
    for lv in sorted(global_structure.levels, key=lambda x: x.level):
        marker = "  <-- границы диапазона" if lv.level in (0.0, 1.0) else ""
        print(f"  {lv.level:>6.3f} = {lv.price:>8.2f}{marker}")

    # 2b. Локальный ФИБО (черновая версия, последние 20 свечей)
    print()
    print("=" * 70)
    print("STRUCTURE / FIBO AGENT -- локальный ФИБО (черновик, последние 40 свечей)")
    print("=" * 70)
    local_structure = build_local_fibo(series, lookback_bars=40)
    if local_structure is None:
        print("Локальная структура за последние 40 свечей не подтвердилась "
              "(не прошла правило 5 свечей слева) -- сознательно не показываем "
              "приблизительный вариант.")
    else:
        print(f"Направление: {local_structure.direction.value}")
        print(f"Точка 1 (100%) = {local_structure.point1.kind} "
              f"{local_structure.point1.price:.2f} на {local_structure.point1.dt}")
        print(f"Точка 2 (0%)   = {local_structure.point2.kind} "
              f"{local_structure.point2.price:.2f} на {local_structure.point2.dt}")

    # 3. Price-Behavior Agent (на глобальной структуре -- это то, что реально
    #    интересно на дневном ТФ; для локальной работает тем же кодом)
    print()
    print("=" * 70)
    print("PRICE-BEHAVIOR AGENT")
    print("=" * 70)
    current_price = series.candles[-1].close
    n = nearest_level(global_structure, current_price)
    print(f"Текущая цена (последнее закрытие, {series.end}): {current_price:.2f}")
    if n.is_testing:
        print(f"Цена тестирует уровень {n.nearest_level:g} ({n.nearest_price:.2f})")
    else:
        print(
            f"Между уровнями {n.below_level:g} ({n.below_price:.2f}) и "
            f"{n.above_level:g} ({n.above_price:.2f}), ближе к {n.nearest_level:g} "
            f"({n.nearest_price:.2f})"
        )
    events = recent_level_events(global_structure, series.candles, lookback=10)
    if events:
        print("Недавние взаимодействия с уровнями:")
        for e in events:
            print(f"  {e.dt}: {e.kind} @ {e.level:g} ({e.level_price:.2f}) -- {e.note}")
    else:
        print("За последние 10 свечей заметных тестов/пробоев ключевых уровней не найдено.")

    # 4. Verification Agent
    print()
    print("=" * 70)
    print("VERIFICATION AGENT")
    print("=" * 70)
    report = run_checklist(global_structure, series.exchange_or_source)
    for r in report.results:
        status = "OK " if r.passed else "FAIL"
        print(f"  [{status}] {r.name} -- {r.detail}")
    print(f"Итог чек-листа: {'ВСЁ ПРОШЛО' if report.all_passed else 'ЕСТЬ ПРОБЛЕМЫ'}")

    # Настоящая независимая сверка (27 августа, по прямому запросу Леонида
    # -- "настоящая сверка структур"): раньше здесь build_global_fibo()
    # вызывался ВТОРОЙ раз на ТЕХ ЖЕ данных -- детерминированная чистая
    # функция не может не совпасть сама с собой, это ничего не проверяло.
    # Теперь -- независимый алгоритм поиска точек разворота
    # (find_fractal_swing_extremes(), agents/fibo_agent.py): классический
    # фрактал/swing-point метод с подтверждением С ОБЕИХ сторон, в отличие
    # от счётного правила слева у основного метода. Согласие теперь
    # осмысленно; расхождение -- сигнал присмотреться (например, буквальный
    # глобальный экстремум может быть ещё не устоявшимся свежим выбросом).
    #
    # ПОКА НЕ БЛОКИРУЕТ ОТПРАВКУ -- расхождение только печатается,
    # verification-чек-лист (run_checklist выше) не тронут. Должно ли
    # расхождение блокировать реальный алерт -- решение за Леонидом, после
    # того как станет видно, как часто это реально происходит на живых
    # данных (см. project doc, 27 августа).
    try:
        fractal_structure = build_global_fibo(series, extremes_fn=find_fractal_swing_extremes)
        consensus = cross_check_structures(global_structure, fractal_structure)
        consensus_agree, consensus_detail = consensus.agree, consensus.detail
    except ValueError as e:
        consensus_agree, consensus_detail = None, str(e)
    # 27 августа (продолжение, по отзыву "брокера" -- см. project doc):
    # раньше эта строка только печаталась в консоль сервера и терялась --
    # теперь format_consensus_note() формулирует то же самое для человека, и
    # ниже (в блоке "if ok:") этот же текст уходит В САМОМ алерте получателю,
    # не только в лог. Отправку по-прежнему не блокирует.
    consensus_note = format_consensus_note(consensus_agree, consensus_detail)
    print(f"{consensus_note} [пока не блокирует отправку]")

    # 5. Dispatch Agent
    print()
    print("=" * 70)
    print("DISPATCH AGENT (финальный)")
    print("=" * 70)
    bundle = AnalysisBundle(
        symbol=series.symbol,
        source_tag=series.exchange_or_source,
        timeframe=series.timeframe,
        period_desc=f"{series.start} .. {series.end} ({len(series.candles)} дневных свечей)",
        structure=global_structure,
        nearest=n,
        recent_events=events,
        checklist=report,
    )
    alert_state = _load_alert_state(SYMBOL)
    ok, reason, new_alert_state = should_send_level_watch(bundle, alert_state)
    print(f"Решение об отправке (level-watch): {'ОТПРАВЛЯТЬ' if ok else 'НЕ ОТПРАВЛЯТЬ'} -- {reason}")

    # Нейтральный превью-вариант (без рамки алерта) -- для консоли и файла,
    # реально отправляемый вариант ниже строится отдельно, с alert_level,
    # если решение "ОТПРАВЛЯТЬ".
    message_preview = format_message(bundle)
    print()
    print("--- Финальное сообщение (формат раздела 24, оформление -- HTML для Telegram, 25 августа) ---")
    print(message_preview)

    if ok:
        # Получатели подтверждены 23 августа (все трое написали боту, chat_id
        # взяты из getUpdates). bot_token берём из окружения ТОЛЬКО в LIVE-режиме
        # -- иначе принудительно None (dry-run), даже если переменная вдруг
        # где-то случайно задана локально.
        recipients = [
            Recipient(label="Леонид (@vaskodevasko)", telegram_chat_id="885989790"),
            Recipient(label="Сергей (@sergikvsl)", telegram_chat_id="1253087193"),
            Recipient(label="Pavel", telegram_chat_id="980723803"),
        ]
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if LIVE else None

        # Внутридневное подтверждение (раздел 7.3 регламента, 2 сентября 2026,
        # agents/intraday_agent.py) -- считаем ТОЛЬКО здесь, когда решение
        # "ОТПРАВЛЯТЬ" уже принято, а не на каждом цикле cron (раз в 15 минут)
        # -- см. докстринг intraday_agent.py про стоимость лишних запросов к
        # FMP. Best-effort, тот же принцип, что и у Context Agent ниже: любая
        # ошибка (сеть, лимит, неожиданная схема ответа FMP) не должна стоить
        # настоящего алерта -- пропускаем строку, а не весь алерт.
        intraday_note = None
        try:
            intraday_note = build_intraday_note(series.symbol, current_price, exchange_hint=series.exchange_or_source)
        except Exception as e:
            print(f"Intraday Agent: пропущено ({e})")

        # Сначала график, потом полный текст -- в этом порядке, по прямому
        # решению Леонида 26 августа ("Фото + весь текущий текст"): картинка
        # ДОПОЛНЯЕТ текстовое сообщение, а не заменяет его -- детализация в
        # тексте не сокращается ни на строчку.
        photo_png = render_chart(
            global_structure,
            series.candles,
            current_price,
            series.symbol,
            series.timeframe,
            alert_level=new_alert_state.last_alerted_level,
        )
        photo_caption = f"{series.symbol} — коррекция дошла до {new_alert_state.last_alerted_level:g}"
        photo_result = send_photo_via_telegram(photo_png, recipients, bot_token=bot_token, caption=photo_caption)
        print()
        print(f"--- Отправка графика (dry_run={photo_result['dry_run']}, {photo_result['photo_bytes']} байт) ---")
        for entry in photo_result["sent_to"]:
            print(f"  {entry['recipient']}: {entry['status']}")

        message_to_send = format_message(
            bundle,
            alert_level=new_alert_state.last_alerted_level,
            consensus_note=consensus_note,
            intraday_note=intraday_note,
        )

        # Context Agent (26 августа, по запросу Леонида) -- необязательная
        # пометка о близких макрособытиях (ФРС/CPI/NFP/ВВП/ОПЕК+) поверх уже
        # готового текста, см. agents/context_agent.py. Best-effort: любая
        # ошибка агента (сеть, лимит, неожиданный ответ FMP) не должна
        # стоить настоящего алерта -- ловим и просто не добавляем пометку,
        # текст и график всё равно уходят как обычно.
        if LIVE:
            try:
                events = get_upcoming_macro_events(datetime.now())
                note = build_context_note(events)
                if note:
                    message_to_send += "\n\n" + note
            except Exception as e:
                print(f"Context Agent: пропущено ({e})")

        result = send_via_telegram(message_to_send, recipients, bot_token=bot_token)
        print()
        print(f"--- Отправка текста (dry_run={result['dry_run']}) ---")
        for entry in result["sent_to"]:
            print(f"  {entry['recipient']}: {entry['status']}")

        # Память о том, что уже отправлено, обновляем, только когда реально
        # отправили (а не в dry-run) -- иначе локальные тестовые прогоны
        # молча испортят состояние для боевого сервера.
        if LIVE:
            _save_alert_state(SYMBOL, new_alert_state)

    out_path = ROOT / "output" / "last_run_message.txt"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(message_preview, encoding="utf-8")
    print()
    print(f"Сообщение сохранено в {out_path}")


if __name__ == "__main__":
    run_once()
