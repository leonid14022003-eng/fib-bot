"""
Разовый превью-скрипт -- НЕ часть боевого пайплайна, НЕ ставится в cron.

Демонстрирует ПОЛНУЮ пару сообщений, которую реально шлёт бот при
срабатывании level-watch (orchestrator.py / screener.py, 26 августа):
сначала график (sendPhoto), затем ОТДЕЛЬНЫМ сообщением полный текстовый
анализ (sendMessage) -- именно то, что Леонид попросил проверить
27 августа ("давай работать над функционалом чтобы присылался график и
отдельным сообщением анализ").

Зачем отдельный скрипт, а не просто дождаться реального срабатывания:
эта пара (фото + текст) уже реализована и покрыта тестами (см.
agents/dispatch_agent.py, agents/chart_agent.py, 29/29 -> 36/36 тестов), и
даже подтверждена вживую -- но по отдельности. preview_chart.py
(26 августа) показал, что картинка сама по себе рендерится и доходит.
Ни разу не было живого показа ИМЕННО ПАРЫ сообщений вместе, потому что с
момента деплоя chart-фичи реальной глубокой коррекции (0.618+) по IBM не
происходило. Ждать её специально ради демонстрации не нужно -- как и в
preview_chart.py, alert_level форсируется (см. ниже), а РЕАЛЬНЫЕ котировки
и вся остальная цепочка (Data -> Structure/Fibo -> Price-Behavior ->
Verification -> Dispatch) остаются настоящими, не выдуманными (регламент,
раздел 2).

Что здесь НЕ по-настоящему -- только alert_level: он форсирован на 0.618
специально для демонстрации форматирования пары сообщений. Честно об этом
сказано и в подписи к фото, и отдельной строкой перед текстом. Реальная
(не форсированная) глубина коррекции печатается в консоли отдельно, для
сверки -- её же в это самое время можно увидеть в обычном (не превью)
выводе orchestrator.py/run_live.sh.

Что здесь по-настоящему -- всё остальное: котировки (load_fmp_daily),
структура ФИБО, чек-лист верификации, сам PNG (render_chart), сам текст
(format_message), и даже пометка Context Agent, если сейчас есть
отслеживаемое макрособытие в ближайшие 24 часа (build_context_note) --
ровно тот же путь, что и в orchestrator.py, за одним отличием:
output/alert_state.json (память level-watch между запусками cron) этот
скрипт НЕ читает и НЕ пишет -- иначе тестовый прогон испортил бы память
боевого бота, и реальное следующее срабатывание могло бы молча
проглотиться как "уже отправляли этот уровень".

Обновление 27 августа (вечер, по прямой просьбе Леонида "всё работает
давай проверочный в телеграм всем" -- после партии "доработка до уровня
практикующего трейдера"): добавлена настоящая независимая сверка
структур (find_fractal_swing_extremes + cross_check_structures, тот же
код, что и в orchestrator.py/screener.py) и её пометка уверенности
(format_consensus_note) теперь передаётся в format_message() -- то есть
получатели увидят её прямо в сообщении, а не только в консоли сервера.
Ориентир инвалидации отдельно форсировать не нужно -- format_message()
показывает его безусловно для любой структуры, форсирован только сам
alert_level (см. выше). Объём при пробое (recent_level_events) тоже не
форсируется -- появится в тексте, только если среди РЕАЛЬНЫХ недавних
событий по IBM на момент запуска реально найдётся подтверждённый пробой
с достаточной историей объёма; если нет -- честно не появится, это не
баг превью.

Запуск на сервере (тот же принцип, что и preview_chart.py/preview_format.py):
  set -a; source /root/fib-bot/.env; set +a
  cd /root/fib-bot && python3 preview_full_alert.py
"""

from __future__ import annotations

import os
from datetime import datetime

from agents.chart_agent import render_chart
from agents.context_agent import build_context_note, get_upcoming_macro_events
from agents.data_agent import load_fmp_daily
from agents.dispatch_agent import (
    AnalysisBundle,
    Recipient,
    format_message,
    send_photo_via_telegram,
    send_via_telegram,
)
from agents.fibo_agent import build_global_fibo, find_fractal_swing_extremes
from agents.price_behavior_agent import nearest_level, recent_level_events
from agents.verification_agent import cross_check_structures, format_consensus_note, run_checklist

SYMBOL = os.environ.get("FIB_BOT_SYMBOL", "IBM")
FORCED_ALERT_LEVEL = 0.618  # только для оформления превью, см. докстринг выше


def main() -> None:
    series = load_fmp_daily(SYMBOL)
    structure = build_global_fibo(series)
    current_price = series.candles[-1].close

    n = nearest_level(structure, current_price)
    events = recent_level_events(structure, series.candles, lookback=10)
    report = run_checklist(structure, series.exchange_or_source)

    real_position = (
        f"тестирует {n.nearest_level:g}"
        if n.is_testing
        else f"между {n.below_level:g} и {n.above_level:g}, ближе к {n.nearest_level:g}"
    )
    print(f"{series.symbol} | {series.exchange_or_source} | {series.timeframe}")
    print(f"Текущая цена: {current_price:.2f}")
    print(f"Реальная (НЕ форсированная) позиция цены сейчас: {real_position}")
    print(f"Чек-лист верификации: {'ВСЁ ПРОШЛО' if report.all_passed else 'ЕСТЬ ПРОБЛЕМЫ'}")
    print(f"Демонстрационный (форсированный) alert_level = {FORCED_ALERT_LEVEL:g} -- НЕ реальное срабатывание")

    # Настоящая независимая сверка (тот же код, что и в
    # orchestrator.py/screener.py, 27 августа) -- НЕ форсируется, реальный
    # результат на реальных данных на момент запуска.
    try:
        fractal_structure = build_global_fibo(series, extremes_fn=find_fractal_swing_extremes)
        consensus = cross_check_structures(structure, fractal_structure)
        consensus_agree, consensus_detail = consensus.agree, consensus.detail
    except ValueError as e:
        consensus_agree, consensus_detail = None, str(e)
    consensus_note = format_consensus_note(consensus_agree, consensus_detail)
    print(f"{consensus_note} [пока не блокирует отправку]")

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

    recipients = [
        Recipient(label="Леонид (@vaskodevasko, превью пары фото+текст)", telegram_chat_id="885989790"),
        Recipient(label="Сергей (@sergikvsl, превью пары фото+текст)", telegram_chat_id="1253087193"),
        Recipient(label="Pavel (превью пары фото+текст)", telegram_chat_id="980723803"),
    ]
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("TELEGRAM_BOT_TOKEN не задан в окружении -- см. .env")
        raise SystemExit(1)

    # 1. Фото -- сначала, в том же порядке, что и в боевом пайплайне
    # (orchestrator.py/screener.py, решение Леонида от 26 августа).
    photo_png = render_chart(
        structure, series.candles, current_price, series.symbol, series.timeframe,
        alert_level=FORCED_ALERT_LEVEL,
    )
    photo_caption = (
        f"🧪 Превью пары «график + текст» (не боевой алерт) -- {series.symbol}. "
        f"alert_level={FORCED_ALERT_LEVEL:g} форсирован для демонстрации, "
        f"реальная коррекция сейчас другая (см. консоль сервера). Следом "
        f"придёт отдельное текстовое сообщение -- именно так теперь "
        f"выглядит настоящее срабатывание бота."
    )
    photo_result = send_photo_via_telegram(photo_png, recipients, bot_token=bot_token, caption=photo_caption)
    print()
    print(f"--- Отправка графика (dry_run={photo_result['dry_run']}, {photo_result['photo_bytes']} байт) ---")
    for entry in photo_result["sent_to"]:
        print(f"  {entry['recipient']}: {entry['status']}")

    # 2. Текст -- ОТДЕЛЬНЫМ сообщением, следом, как и в боевом пайплайне.
    # Честная пометка о том, что это тест, идёт ПЕРЕД форматированным
    # сообщением, а не встроена внутрь него -- format_message() сам по
    # себе не знает о том, что alert_level здесь ненастоящий.
    disclaimer = (
        f"🧪 <i>Превью (не боевой алерт) -- пара «график + текст», "
        f"alert_level={FORCED_ALERT_LEVEL:g} форсирован для демонстрации. "
        f"Ориентир инвалидации, объём и пометка сверки ниже -- НЕ форсированы, "
        f"настоящие на текущий момент.</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )
    message = disclaimer + format_message(
        bundle, alert_level=FORCED_ALERT_LEVEL, consensus_note=consensus_note
    )

    # Context Agent (26 августа) -- тот же best-effort путь, что и в
    # orchestrator.py/screener.py: любая его ошибка не должна стоить
    # отправки самого превью.
    try:
        macro_events = get_upcoming_macro_events(datetime.now())
        note = build_context_note(macro_events)
        if note:
            message += "\n\n" + note
    except Exception as e:
        print(f"Context Agent: пропущено ({e})")

    result = send_via_telegram(message, recipients, bot_token=bot_token)
    print()
    print(f"--- Отправка текста (dry_run={result['dry_run']}) ---")
    for entry in result["sent_to"]:
        print(f"  {entry['recipient']}: {entry['status']}")

    print()
    print(
        "Память level-watch (output/alert_state.json) этим прогоном НЕ тронута -- "
        "это превью, не реальное срабатывание. Следующее настоящее срабатывание "
        "cron отработает как обычно, независимо от этого прогона."
    )


if __name__ == "__main__":
    main()
