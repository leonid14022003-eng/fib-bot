"""
Разовый превью-скрипт -- НЕ часть боевого пайплайна, НЕ ставится в cron.
Показывает, как теперь выглядит график (26 августа, по прямому запросу
Леонида: "давай задний фон изменим с белого на чёрный чтобы было удобнее
глазу и сделаем так чтобы бот присылал графики"), на РЕАЛЬНЫХ текущих
данных IBM -- ровно те же данные и цифры, что дал бы прямо сейчас боевой
orchestrator.py. Никаких выдуманных котировок (регламент, раздел 2).

Первая версия этого скрипта (первый прогон, 26 августа) слала ТОЛЬКО
Леониду -- превью на утверждение, тот же принцип, что и у
preview_format.py (25 августа). Он посмотрел, подтвердил скриншотом, что
всё отрендерилось правильно -- после этого Леонид явно попросил
разослать всем троим ("отправь всем кто используют бот"), поэтому список
получателей ниже расширен на Сергея и Pavel.

Единственное, что здесь не по-настоящему -- alert_level=0.618 форсирован
специально для демонстрации (реальная глубина коррекции сейчас может быть
другая) -- подпись к фото честно об этом говорит.

Запуск на сервере (тот же принцип, что и preview_format.py):
  set -a; source /root/fib-bot/.env; set +a
  cd /root/fib-bot && python3 preview_chart.py
"""

from __future__ import annotations

import os

from agents.chart_agent import render_chart
from agents.data_agent import load_fmp_daily
from agents.dispatch_agent import Recipient, send_photo_via_telegram
from agents.fibo_agent import build_global_fibo

SYMBOL = os.environ.get("FIB_BOT_SYMBOL", "IBM")


def main() -> None:
    series = load_fmp_daily(SYMBOL)
    structure = build_global_fibo(series)
    current_price = series.candles[-1].close

    png = render_chart(
        structure,
        series.candles,
        current_price,
        series.symbol,
        series.timeframe,
        alert_level=0.618,
    )
    print(f"График готов: {len(png)} байт")

    caption = (
        f"🧪 Превью нового графика бота (не боевой алерт) -- {series.symbol}, реальные данные. "
        f"alert_level=0.618 здесь взят для примера оформления, реальная глубина "
        f"коррекции сейчас может быть другая. При реальном срабатывании бот теперь "
        f"присылает такую картинку вместе с обычным текстовым сообщением."
    )

    recipients = [
        Recipient(label="Леонид (@vaskodevasko, превью графика)", telegram_chat_id="885989790"),
        Recipient(label="Сергей (@sergikvsl, превью графика)", telegram_chat_id="1253087193"),
        Recipient(label="Pavel (превью графика)", telegram_chat_id="980723803"),
    ]
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("TELEGRAM_BOT_TOKEN не задан в окружении -- см. .env")
        raise SystemExit(1)

    result = send_photo_via_telegram(png, recipients, bot_token=bot_token, caption=caption)
    print(f"--- Отправка (dry_run={result['dry_run']}, {result['photo_bytes']} байт) ---")
    for entry in result["sent_to"]:
        print(f"  {entry['recipient']}: {entry['status']}")


if __name__ == "__main__":
    main()
