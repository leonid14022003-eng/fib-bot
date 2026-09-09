"""
MTF Screener -- каскад Месяц -> Неделя -> Час -> 4 часа по 15 инструментам
(IBM + 14 из screener.py), с проверкой ОБОИХ направлений на каждом ТФ.

Прямое продолжение разговора 6 сентября 2026 (Леонид: "ИИ берёт актив,
начинает анализ с месячного графика, смотрит восходящую/нисходящую ФИБО...
не находит цену возле нужного уровня -- переключается на недельный график.
Проводит всё то же самое в обе стороны... По итогу получим больше активов и
точек входа"). История экспериментов и обоснование чисел окна (24
месячных / 52 недельных бара) и алгоритма проверки обоих направлений
(build_dual_direction_fibo, agents/fibo_agent.py) -- см. mtf_probe.py и чат
от 6 сентября 2026.

ЧЕМ ЭТО ОТЛИЧАЕТСЯ от существующих screener.py/orchestrator.py: те дают
ОДНУ структуру на дневном/глобальном окне (find_oldest_unbroken_extremes).
Этот скрипт -- ДОПОЛНИТЕЛЬНЫЙ, более свежий срез: локальное окно на
месячном и недельном ТФ, независимо в обе стороны. Не заменяет и не
трогает дневной уровень (тот как был, так и остаётся в screener.py/
orchestrator.py, раздел 25 -- пороги и методика дневного уровня не менялись).

Своя память дедупликации -- output/mtf_state.json, ОТДЕЛЬНО от
output/alert_state.json (IBM) и output/screener_state.json (дневной
скринер), ключ "СИМВОЛ|ТФ|направление" -- так что новый месячный/недельный
слой не может задеть память уже боевых веток.

Час/4-часовой уровень (INCLUDE_INTRADAY, включён по умолчанию) -- ДВА
дополнительных запроса к FMP на инструмент сверх дневного (~30 запросов на
полный прогон по 15 инструментам, та же оценка, что и у
ANALYST_REPORT_INCLUDE_INTRADAY) -- отключается MTF_SCREENER_INCLUDE_INTRADAY=0,
если бюджет FMP уже занят другими ветками в этом же часе.

ВАЖНО: окна здесь ШИРЕ, чем потолок раздела 7.3 (15/60 торговых дней) в
intraday_agent.py -- НЕ то же самое использование. Раздел 7.3 был задуман
для ПОДТВЕРЖДЕНИЯ уже принятого дневного алерта (там достаточно узкого
окна). Здесь окно используется, чтобы С НУЛЯ построить ПЕРВИЧНУЮ структуру
(find_global_extremes внутри build_dual_direction_fibo) -- на живой
проверке 6 сентября 2026 узкое окно (23/90 календарных дней) регулярно
давало "экстремум слишком близко к левому краю" (у 5 из 15 инструментов на
1H, у 2 из 12 на 4H), потому что литеральный макс/мин узкого окна попадал в
первые ~5 баров и не проходил confirmation. Расширение до 40/130
календарных дней (см. HOURLY_DAYS_BACK/FOUR_HOUR_DAYS_BACK ниже) вылечило
5 из 6 живых кейсов при повторной проверке -- НЕ гарантия на 100% (это
свойство алгоритма "буквальный экстремум окна", а не число окна: всегда
может найтись момент, когда истинный экстремум расширенного окна снова
окажется у левого края) -- оставшиеся редкие отказы это честное
"структура не подтвердилась" (раздел 2), не баг, дальше по каскаду не
блокирует. Не пытаться "долечить" бесконечным расширением окна -- это
разные вещи с раздел 7.3 и трогать те константы не нужно.

Индексы (^GSPC, ^DJI, ^RUT) НЕ имеют 4H на тарифе FMP Starter -- проверено
живьём 6 сентября 2026, все три отвечают 402 Payment Required (та же
картина, что и у Nasdaq 100/WTI/газа на дневном уровне, screener.py).
INDEX_NO_4H_ON_STARTER ниже пропускает попытку заранее, а не тратит вызов
на заведомо неудачный запрос.

НЕ в crontab. Реальная отправка требует ДВОЙНОГО явного согласия (тот же
принцип, что и в analyst_report.py) -- TELEGRAM_BOT_TOKEN в окружении И
MTF_SCREENER_SEND_REAL=1 -- по умолчанию всегда DRY RUN, ничего не уходит
в Telegram, даже если FIB_BOT_LIVE=1 стоит только для загрузки реальных
котировок FMP.

Запуск (только чтение котировок FMP, без Telegram):
    MARKET_DATA_API_KEY=... FIB_BOT_LIVE=1 python3 mtf_screener.py
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from agents.data_agent import load_fmp_daily, load_fmp_intraday, resample_candles
from agents.dispatch_agent import (
    AnalysisBundle,
    LevelWatchState,
    Recipient,
    format_message,
    send_via_telegram,
    should_send_level_watch,
)
from agents.fibo_agent import StructureScope, build_dual_direction_fibo, build_dual_direction_local_fibo
from agents.price_behavior_agent import nearest_level, recent_level_events
from agents.verification_agent import run_checklist
from analyst_report import ALL_INSTRUMENTS
from screener import Instrument

ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "output" / "mtf_state.json"
LIVE = os.environ.get("FIB_BOT_LIVE") == "1"
INCLUDE_INTRADAY = os.environ.get("MTF_SCREENER_INCLUDE_INTRADAY", "1") != "0"
WATCH_LEVELS = (0.618, 0.786, 1.0)  # тот же порог, что и у дневного screener.py/orchestrator.py

# Числа окна -- см. mtf_probe.py для полной истории решения (6 сентября
# 2026): 24 мес / 52 нед выбраны после живого сравнения с 36/78 --
# заметно меньше отказов "нет структуры" и хотя бы один реальный кандидат
# на тестовом прогоне (MSFT, неделя). Не бэктест, подлежит пересмотру.
MONTHLY_LOOKBACK = 24
WEEKLY_LOOKBACK = 52
RESAMPLE_CASCADE = [("Месяц", "1M", MONTHLY_LOOKBACK), ("Неделя", "1W", WEEKLY_LOOKBACK)]

# ШИРЕ, чем потолок раздела 7.3 (23/90 календарных дней) -- см. докстринг
# файла выше за причиной и живой проверкой 6 сентября 2026.
HOURLY_DAYS_BACK = 40
FOUR_HOUR_DAYS_BACK = 130
INTRADAY_CASCADE = [("Час", "1hour", HOURLY_DAYS_BACK), ("4 часа", "4hour", FOUR_HOUR_DAYS_BACK)]

# Проверено живьём 6 сентября 2026: все три индекса отвечают 402 Payment
# Required на /stable/historical-chart/4hour на тарифе FMP Starter --
# пропускаем заранее, не тратя вызов (см. докстринг файла).
INDEX_NO_4H_ON_STARTER = {"^GSPC", "^DJI", "^RUT"}

RECIPIENTS = [
    Recipient(label="Леонид (@vaskodevasko)", telegram_chat_id="885989790"),
    Recipient(label="Сергей (@sergikvsl)", telegram_chat_id="1253087193"),
    Recipient(label="Pavel", telegram_chat_id="980723803"),
]


def _load_state() -> dict[str, LevelWatchState]:
    if not STATE_PATH.exists():
        return {}
    raw = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {k: LevelWatchState(**v) for k, v in raw.items()}


def _save_state(state: dict[str, LevelWatchState]) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    raw = {k: asdict(v) for k, v in state.items()}
    STATE_PATH.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_bundle(symbol, source_tag, tf_label, period_desc, structure, current_price, window_candles) -> AnalysisBundle:
    n = nearest_level(structure, current_price)
    events = recent_level_events(structure, window_candles, lookback=10)
    report = run_checklist(structure, source_tag)
    return AnalysisBundle(
        symbol=symbol,
        source_tag=source_tag,
        timeframe=tf_label,
        period_desc=period_desc,
        structure=structure,
        nearest=n,
        recent_events=events,
        checklist=report,
    )


def _evaluate_tf(
    instrument: Instrument,
    tf_label: str,
    dual,
    window_candles: list,
    source_tag: str,
    period_desc: str,
    current_price: float,
    state: dict[str, LevelWatchState],
    hits: list,
    updated_keys: dict,
    trace: list,
) -> None:
    """Общая часть для любого ТФ каскада: прогоняет ascending/descending из
    уже построенного DualDirectionResult через checklist + should_send_level_watch,
    мутирует hits/updated_keys/trace на месте (см. вызывающий код)."""
    for direction_label, structure, none_reason in (
        ("восходящий", dual.ascending, dual.ascending_reason),
        ("нисходящий", dual.descending, dual.descending_reason),
    ):
        state_key = f"{instrument.symbol}|{tf_label}|{direction_label}"
        if structure is None:
            trace.append(f"{tf_label}/{direction_label}: {none_reason}")
            continue
        bundle = _build_bundle(
            instrument.symbol, source_tag, tf_label, period_desc, structure, current_price, window_candles
        )
        key_state = state.get(state_key, LevelWatchState())
        ok, send_reason, new_key_state = should_send_level_watch(bundle, key_state, WATCH_LEVELS)
        updated_keys[state_key] = new_key_state
        trace.append(f"{tf_label}/{direction_label}: {send_reason}")
        if ok:
            hits.append((state_key, bundle, new_key_state, send_reason))


def scan_instrument_mtf(
    instrument: Instrument, state: dict[str, LevelWatchState], fetch_fn=load_fmp_daily
) -> dict:
    """
    Каскад для ОДНОГО инструмента: Месяц -> Неделя -> Час -> 4 часа (два
    последних -- только если INCLUDE_INTRADAY). На каждом ТФ независимо
    проверяет ascending и descending (build_dual_direction_*),
    останавливается на ПЕРВОМ ТФ, где хотя бы одно направление даёт НОВЫЙ
    (не задублированный, should_send_level_watch) алерт 0.618+. Как и
    screener.scan_instrument() -- никогда не бросает исключение наружу,
    любая проблема -- честный статус в возвращаемом словаре (раздел 2).

    Возвращает {"instrument", "status", ...}:
      "DATA_ERROR"   -- не удалось получить дневные котировки (без них нет
                        даже месяца/недели -- дальше по каскаду не идём)
      "NO_CANDIDATE" -- ни на одном ТФ каскада нет НОВОГО алерта 0.618+
                        (detail -- построчный разбор по каждому ТФ/направлению;
                        сбой ТОЛЬКО intraday-запроса тоже попадает сюда
                        строкой в trace, а не прерывает весь инструмент)
      "CANDIDATE"    -- нашли хотя бы один новый алерт ("hits" -- список
                        кортежей (state_key, bundle, new_key_state, reason);
                        обычно один, но оба направления одного ТФ МОГУТ
                        совпасть одновременно -- тогда оба идут в hits)
    В обоих случаях "updated_keys" -- state-записи, которые нужно слить в
    общий словарь состояния (только для ТФ, которые реально проверялись в
    этом прогоне -- пропущенные из-за ранней остановки каскада не трогаем).
    """
    try:
        daily_series = fetch_fn(instrument.symbol, exchange_hint=instrument.exchange_hint)
    except Exception as e:
        return {"instrument": instrument, "status": "DATA_ERROR", "detail": str(e)}

    current_price = daily_series.candles[-1].close
    trace: list[str] = []
    hits: list[tuple[str, AnalysisBundle, LevelWatchState, str]] = []
    updated_keys: dict[str, LevelWatchState] = {}

    for tf_label, rule, lookback in RESAMPLE_CASCADE:
        tf_series = resample_candles(daily_series, rule)
        try:
            dual = build_dual_direction_local_fibo(tf_series, lookback_bars=lookback)
        except Exception as e:  # не ожидается (ValueError уже перехвачена внутри build_dual_direction_local_fibo),
            trace.append(f"{tf_label}: непредвиденная ошибка построения структуры -- {e}")
            continue
        if dual is None:
            trace.append(f"{tf_label}: структура не подтвердилась (мало баров или нет confirmed HIGH/LOW в окне)")
            continue

        window_candles = tf_series.candles[-lookback:]
        period_desc = f"{window_candles[0].dt} .. {window_candles[-1].dt} ({lookback} баров, {tf_label.lower()})"
        source_tag = f"{daily_series.exchange_or_source} (ресемплировано локально в {tf_label.lower()} -- окно {lookback} баров)"

        tf_hit_count_before = len(hits)
        _evaluate_tf(
            instrument, tf_label, dual, window_candles, source_tag, period_desc,
            current_price, state, hits, updated_keys, trace,
        )
        if len(hits) > tf_hit_count_before:
            return {"instrument": instrument, "status": "CANDIDATE", "hits": hits, "updated_keys": updated_keys, "trace": trace}

    if INCLUDE_INTRADAY:
        for tf_label, interval, days_back in INTRADAY_CASCADE:
            if interval == "4hour" and instrument.symbol in INDEX_NO_4H_ON_STARTER:
                trace.append(f"{tf_label}: пропуск -- индексы не имеют 4H на тарифе FMP Starter (проверено 6 сентября 2026)")
                continue
            try:
                intraday_series = load_fmp_intraday(
                    instrument.symbol, interval, exchange_hint=instrument.exchange_hint, days_back=days_back
                )
            except Exception as e:
                trace.append(f"{tf_label}: не удалось получить внутридневные свечи -- {e}")
                continue
            try:
                dual = build_dual_direction_fibo(intraday_series, scope=StructureScope.LOCAL)
            except ValueError as e:
                trace.append(f"{tf_label}: структура не подтвердилась -- {e}")
                continue

            window_candles = intraday_series.candles
            period_desc = f"{window_candles[0].dt} .. {window_candles[-1].dt} ({len(window_candles)} баров, {tf_label.lower()}, окно {days_back} календарных дней)"
            source_tag = f"{intraday_series.exchange_or_source}"

            tf_hit_count_before = len(hits)
            _evaluate_tf(
                instrument, tf_label, dual, window_candles, source_tag, period_desc,
                current_price, state, hits, updated_keys, trace,
            )
            if len(hits) > tf_hit_count_before:
                return {"instrument": instrument, "status": "CANDIDATE", "hits": hits, "updated_keys": updated_keys, "trace": trace}

    # Ни один ТФ каскада не дал нового хита (hits пуст на этой строке --
    # оба цикла выше возвращаются немедленно, как только что-то находят).
    return {"instrument": instrument, "status": "NO_CANDIDATE", "detail": trace, "updated_keys": updated_keys}


def run_mtf_screener() -> None:
    state = _load_state()
    send_real = bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and os.environ.get("MTF_SCREENER_SEND_REAL") == "1"
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if send_real else None

    print("=" * 70)
    print(
        f"MTF SCREENER -- Месяц({MONTHLY_LOOKBACK})/Неделя({WEEKLY_LOOKBACK})"
        f"{'/Час/4Часа' if INCLUDE_INTRADAY else ''}, оба направления, {len(ALL_INSTRUMENTS)} инструментов"
    )
    print(f"Реальная отправка: {'ДА' if send_real else 'НЕТ (dry run)'}")
    print("=" * 70)

    any_candidate = False
    for instrument in ALL_INSTRUMENTS:
        label = f"{instrument.label:30} [{instrument.symbol:10}]"
        result = scan_instrument_mtf(instrument, state)

        if result["status"] == "DATA_ERROR":
            print(f"{label} DATA_ERROR: {result['detail']}")
            continue

        state.update(result["updated_keys"])

        if result["status"] == "NO_CANDIDATE":
            print(f"{label} -- нет нового кандидата")
            for line in result["detail"]:
                print(f"    {line}")
            continue

        any_candidate = True
        print(f"{label} -- НОВЫЙ КАНДИДАТ")
        for line in result["trace"]:
            print(f"    {line}")

        for state_key, bundle, new_key_state, reason in result["hits"]:
            print(f"    >>> {state_key}: {reason}")
            message = format_message(
                bundle,
                alert_level=new_key_state.last_alerted_level,
                display_name=instrument.label,
            )
            send_result = send_via_telegram(message, RECIPIENTS, bot_token=bot_token)
            print(f"    --- отправка (dry_run={send_result['dry_run']}) ---")
            for entry in send_result["sent_to"]:
                print(f"        {entry['recipient']}: {entry['status']}")

    print("=" * 70)
    print(f"Итог: {'есть новые кандидаты' if any_candidate else 'новых кандидатов нет'}")

    # Память обновляем, только если реально отправляли -- тот же принцип,
    # что и в orchestrator.py (_save_alert_state только при LIVE): иначе
    # локальные тестовые прогоны молча испортят состояние для боевого сервера.
    if send_real:
        _save_state(state)
        print(f"Состояние сохранено в {STATE_PATH}")
    else:
        print("Dry run -- output/mtf_state.json НЕ тронут")


if __name__ == "__main__":
    run_mtf_screener()
