"""
Разовый превью-скрипт -- НЕ часть боевого пайплайна, НЕ ставится в cron.
Показывает Леониду, как теперь выглядит визуальный редизайн сообщений
(25 августа, по его прямому запросу "чтобы это не выглядело просто как
слова и цифры, а чтобы глазу было приятно"), на РЕАЛЬНЫХ текущих данных
IBM -- ровно те же данные и цифры, что дал бы прямо сейчас боевой
orchestrator.py. Никаких выдуманных котировок (регламент, раздел 2).
 
Отправляется ТОЛЬКО Леониду (chat_id 885989790), не Сергею и не Pavel --
это превью формата на утверждение, а не настоящий сигнал.
 
Единственное, что здесь не по-настоящему -- alert_level=0.618 во втором
блоке ("как выглядит при срабатывании") форсирован специально для
демонстрации визуального стиля алерта; сама подпись блока честно
проговаривает, что это демонстрация, а не реальное срабатывание.
 
Запуск на сервере (тот же принцип, что и run_live.sh):
  set -a; source /root/fib-bot/.env; set +a
  cd /root/fib-bot && python3 preview_format.py
"""
 
from __future__ import annotations
 
import os
 
from agents.data_agent import load_fmp_daily
from agents.dispatch_agent import AnalysisBundle, Recipient, format_message, send_via_telegram
from agents.fibo_agent import build_global_fibo
from agents.price_behavior_agent import nearest_level, recent_level_events
from agents.verification_agent import run_checklist
 
SYMBOL = os.environ.get("FIB_BOT_SYMBOL", "IBM")
 
 
def main() -> None:
    series = load_fmp_daily(SYMBOL)
    structure = build_global_fibo(series)
    current_price = series.candles[-1].close
    n = nearest_level(structure, current_price)
    events = recent_level_events(structure, series.candles, lookback=10)
    report = run_checklist(structure, series.exchange_or_source)
 
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
 
    intro = (
        "🧪 <b>Превью нового оформления сообщений</b>\n"
        "Реальные данные IBM (не выдуманные), но это НЕ настоящий алерт -- "
        "просто чтобы вы увидели новый вид перед тем, как это пойдёт в бой."
    )
    card_neutral = (
        "<b>Вариант 1 — обычная сводка (без триггера):</b>\n" + format_message(bundle)
    )
    card_alert = (
        "<b>Вариант 2 — как выглядит при реальном срабатывании</b> "
        "(уровень 0.618 здесь взят для примера оформления, реальная глубина "
        "коррекции сейчас другая -- смотрите цифру \"Глубина коррекции\" внутри):\n"
        + format_message(bundle, alert_level=0.618)
    )
    divider = "\n" + "─" * 24 + "\n"
    message = intro + "\n\n" + card_neutral + divider + card_alert
 
    print(f"Длина сообщения: {len(message)} символов (лимит Telegram -- 4096)")
 
    recipients = [Recipient(label="Леонид (превью формата)", telegram_chat_id="885989790")]
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("TELEGRAM_BOT_TOKEN не задан в окружении -- см. .env")
        raise SystemExit(1)
 
    result = send_via_telegram(message, recipients, bot_token=bot_token)
    print(f"--- Отправка (dry_run={result['dry_run']}) ---")
    for entry in result["sent_to"]:
        print(f"  {entry['recipient']}: {entry['status']}")
 
 
if __name__ == "__main__":
    main()
