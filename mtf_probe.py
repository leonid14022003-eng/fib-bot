"""
Разовый эксперимент (НЕ cron, НЕ отправляет ничего в Telegram): проверяет
идею Леонида от 6 сентября 2026 -- каскад по таймфреймам сверху вниз
(Месяц -> Неделя -> День -> ...), стоп на первом совпадении с уровнем 0.618+
для каждого инструмента.

Первая версия этого скрипта реземплировала дневные свечи в недельные/
месячные и прогоняла через build_global_fibo(find_oldest_unbroken_extremes)
-- тот же алгоритм, что уже в проде для дневного ФИБО. Результат: Месяц,
Неделя и День дали ПОБИТОВО одинаковую глубину отката на всех 15
инструментах -- не баг, а математическая неизбежность (максимум/минимум по
неделям равен максимуму/минимуму по дням внутри тех же недель, ресемплинг
глобальный экстремум не меняет). Ресемплинг + глобальный алгоритм -- ноль
новой информации по конструкции, а не "просто пока не повезло".

ЭТА версия использует build_local_fibo() (agents/fibo_agent.py, черновик с
27 августа) -- ищет структуру в ПОСЛЕДНИХ N барах конкретного ТФ, а не за
всю историю. История чисел окна (6 сентября 2026):

  1. Claude предложил рабочий дефолт: 24 мес / 52 нед ("52-week high/low"
     как финансовая конвенция + месяц вдвое длиннее недели по календарю).
     Живой прогон: 1 из 15 (Microsoft, неделя, откат 0.736) -- реальный
     кандидат, которого дневной/глобальный уровень не видит.
  2. Леонид попросил заменить на 36 мес / 78 нед. Живой прогон: 0 из 15,
     и резко выросло число "нет структуры" (11/15 на месяце, 10/15 на
     неделе, было 1/15 и 5/15). Причина не в рынке, а в самом алгоритме:
     build_local_fibo() берёт буквальный макс/мин ВСЕГО окна и требует
     минимум 5 баров слева от него ВНУТРИ этого же окна -- чем шире окно,
     тем чаще реальный экстремум периода утыкается в левый край и не
     проходит подтверждение. Более широкое окно тут статистически чаще
     даёт отказ, а не находку.
  3. По итогам обсуждения (Claude рекомендовал, Леонид согласился) --
     ВОЗВРАТ к 24 мес / 52 нед как рабочим числам. Основание: узнаваемая
     конвенция (52-week high/low) + эмпирически меньше отказов и хотя бы
     один реальный кандидат на этом снэпшоте. Это НЕ бэктест и не
     статистика (один снэпшок на 15 активов) -- подлежит пересмотру, когда
     будет накоплена реальная стата по ТФ (см. CLAUDE.md).

Дневной уровень НЕ переведён на локальное окно -- остаётся тем же глобальным
алгоритмом, что уже в проде (screener.py/orchestrator.py), для сравнения:
здесь важно увидеть, добавляет ли локальный месяц/неделя НОВЫЕ кандидаты
поверх того, что уже видит существующий дневной скринер, а не заменить его.

ВАЖНОЕ ОГРАНИЧЕНИЕ, которое всё ещё не решено: build_local_fibo(), как и
build_global_fibo(), возвращает ОДНУ структуру на окно (направление
определяется тем, какой из двух найденных экстремумов хронологически
раньше), а не "восходящую И нисходящую отдельно". Проверка обоих направлений
одновременно на одном окне -- отдельная задача, требует другого алгоритма
поиска точек (не в рамках этого прогона).

Запуск: python3 mtf_probe.py
"""
from __future__ import annotations

from agents.data_agent import load_fmp_daily, resample_candles
from agents.fibo_agent import build_global_fibo, build_local_fibo, find_oldest_unbroken_extremes
from analyst_report import ALL_INSTRUMENTS

WATCH_LEVELS = (0.618, 0.786, 1.0)
MONTHLY_LOOKBACK = 24  # 2 года месячных баров -- рабочее число, см. докстринг выше
WEEKLY_LOOKBACK = 52  # 1 год недельных баров ("52-week high/low") -- рабочее число
CASCADE = [("Месяц", "1M", MONTHLY_LOOKBACK), ("Неделя", "1W", WEEKLY_LOOKBACK)]


def _score(structure, current_price) -> dict:
    depth = (current_price - structure.point2.price) / (structure.point1.price - structure.point2.price)
    hit_level = None
    for lvl in sorted(WATCH_LEVELS, reverse=True):
        if depth >= lvl:
            hit_level = lvl
            break
    return {
        "ok": True,
        "direction": structure.direction.value,
        "depth": depth,
        "hit_level": hit_level,
        "point1": structure.point1.price,
        "point2": structure.point2.price,
    }


def _probe_global(series, current_price) -> dict:
    """Дневной уровень -- тот же алгоритм, что и в проде (глобальный, вся история)."""
    try:
        structure = build_global_fibo(series, extremes_fn=find_oldest_unbroken_extremes)
    except ValueError as e:
        return {"ok": False, "detail": str(e)}
    return _score(structure, current_price)


def _probe_local(series, current_price, lookback_bars: int) -> dict:
    """Месяц/неделя -- локальное окно последних lookback_bars баров этого ТФ."""
    if len(series.candles) < lookback_bars:
        return {
            "ok": False,
            "detail": f"недостаточно баров для окна {lookback_bars} (есть {len(series.candles)})",
        }
    structure = build_local_fibo(series, lookback_bars=lookback_bars)
    if structure is None:
        return {"ok": False, "detail": f"структура не подтвердилась в последних {lookback_bars} барах"}
    return _score(structure, current_price)


def run_probe() -> None:
    print("=" * 70)
    print(
        f"MTF PROBE -- Месяц({MONTHLY_LOOKBACK})/Неделя({WEEKLY_LOOKBACK}), "
        f"локальное окно + День (глобальный), {len(ALL_INSTRUMENTS)} инструментов"
    )
    print("Стоп на первом совпадении 0.618+, сверху вниз: Месяц -> Неделя")
    print("=" * 70)

    any_hit = 0
    for instrument in ALL_INSTRUMENTS:
        try:
            daily_series = load_fmp_daily(instrument.symbol, exchange_hint=instrument.exchange_hint)
        except Exception as e:
            print(f"{instrument.label:30} [{instrument.symbol:10}] DATA_ERROR: {e}")
            continue

        current_price = daily_series.candles[-1].close
        row = f"{instrument.label:30} [{instrument.symbol:10}]"
        stopped_at = None
        results = {}

        for tf_label, rule, lookback_bars in CASCADE:
            tf_series = resample_candles(daily_series, rule)
            res = _probe_local(tf_series, current_price, lookback_bars)
            results[tf_label] = res
            if res["ok"] and res["hit_level"] is not None:
                stopped_at = tf_label
                break

        # День -- всегда считаем, для сравнения (не участвует в "стоп на первом")
        results["День"] = _probe_global(daily_series, current_price)

        print(row)
        for tf_label in ("Месяц", "Неделя", "День"):
            res = results.get(tf_label)
            if res is None:
                print(f"    {tf_label:8} -- пропущено (каскад остановился раньше)")
                continue
            if not res["ok"]:
                print(f"    {tf_label:8} -- нет структуры: {res['detail'][:90]}")
                continue
            marker = " <-- СТОП, кандидат" if tf_label == stopped_at else ""
            hit = f"{res['hit_level']:.3f}" if res["hit_level"] is not None else "-"
            print(
                f"    {tf_label:8} -- {res['direction']:10} откат {res['depth']:.3f}"
                f" (уровень: {hit}){marker}"
            )

        if stopped_at:
            any_hit += 1

    print("=" * 70)
    print(f"Итог: {any_hit} из {len(ALL_INSTRUMENTS)} дали совпадение 0.618+ на Месяце или Неделе")
    print("(День выше -- только для сравнения, не входит в счёт каскада)")


if __name__ == "__main__":
    run_probe()
