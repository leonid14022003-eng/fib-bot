"""
Оркестратор: Data -> Structure/Fibo -> Price-Behavior -> Verification -> Dispatch.

Два режима, переключаются переменной окружения FIB_BOT_LIVE:

  - FIB_BOT_LIVE не задана (по умолчанию, локальный прогон/тесты):
    инструмент IBM, демо-данные Alpha Vantage из data/ibm_daily_raw.txt,
    Telegram в режиме DRY RUN (ничего реально не уходит). Безопасно
    запускать сколько угодно раз, ничего не потратит и никому не пришлёт.

  - FIB_BOT_LIVE=1 (продакшен -- GitHub Actions / VPS): инструмент из
    FIB_BOT_SYMBOL (по умолчанию тоже IBM -- пока это единственный
    инструмент, реально провалидированный на живых данных; полный список
    инструментов для скринера из Задачи №1 брифа ещё не утверждён Леонидом,
    см. claude/brief.md), котировки через load_fmp_daily() (нужен
    MARKET_DATA_API_KEY), реальная отправка в Telegram (нужен
    TELEGRAM_BOT_TOKEN). Секреты ТОЛЬКО из окружения, никогда не в коде.

  Оба режима запускают один и тот же пайплайн агентов и одну и ту же
  логику -- разница исключительно в источнике данных и в том, уходит ли
  сообщение реально. Каденс "каждые 15 минут" задаётся снаружи (cron в
  .github/workflows/fib-bot.yml или systemd/cron на VPS), не этим файлом.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.data_agent import load_fmp_daily, load_ibm_demo_daily
from agents.dispatch_agent import AnalysisBundle, Recipient, format_message, send_via_telegram, should_send
from agents.fibo_agent import build_global_fibo, build_local_fibo
from agents.price_behavior_agent import nearest_level, recent_level_events
from agents.verification_agent import cross_check_structures, run_checklist

ROOT = Path(__file__).resolve().parent

LIVE = os.environ.get("FIB_BOT_LIVE") == "1"
SYMBOL = os.environ.get("FIB_BOT_SYMBOL", "IBM")


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
    global_structure = build_global_fibo(series)
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

    # Демонстрация cross-check (раздел "несколько агентов сверяются"):
    # прогоняем структуру второй раз на тех же данных -- должны совпасть 1:1.
    repeat_structure = build_global_fibo(series)
    consensus = cross_check_structures(global_structure, repeat_structure)
    print(f"Повторный независимый прогон структуры: "
          f"{'СОВПАЛ' if consensus.agree else 'РАСХОЖДЕНИЕ'} -- {consensus.detail}")

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

    ok, reason = should_send(bundle, mode="always")
    print(f"Решение об отправке: {'ОТПРАВЛЯТЬ' if ok else 'НЕ ОТПРАВЛЯТЬ'} -- {reason}")

    message = format_message(bundle)
    print()
    print("--- Финальное сообщение (формат раздела 24) ---")
    print(message)

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
        result = send_via_telegram(message, recipients, bot_token=bot_token)
        print()
        print(f"--- Отправка (dry_run={result['dry_run']}) ---")
        for entry in result["sent_to"]:
            print(f"  {entry['recipient']}: {entry['status']}")

    out_path = ROOT / "output" / "last_run_message.txt"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(message, encoding="utf-8")
    print()
    print(f"Сообщение сохранено в {out_path}")


if __name__ == "__main__":
    run_once()
