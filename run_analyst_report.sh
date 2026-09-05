#!/bin/bash
# Cron-запуск analyst_report.py -- полный анализ по всем 15 инструментам
# (IBM + скринер), реальная отправка в Telegram. Двойное согласие на
# реальную отправку живёт в analyst_report.py (TELEGRAM_BOT_TOKEN +
# ANALYST_REPORT_SEND_REAL=1) -- здесь оба явно выставлены, по решению
# Леонида 5 сентября 2026 ("при каждом cron тике" -- привязано к тику
# скринера, раз в час, а не к 15-минутному IBM-watch).
#
# ANALYST_REPORT_INCLUDE_INTRADAY=0 -- по решению Леонида в том же
# разговоре ("без intraday для крона -- дешевле по лимитам"): часовой
# автоматический прогон пропускает 1H/4H проверку (~30 запросов к FMP за
# прогон), ручной запуск (python3 analyst_report.py напрямую, без этой
# переменной) по-прежнему считает её.
set -a
source /root/fib-bot/.env
set +a
export FIB_BOT_LIVE=1
export ANALYST_REPORT_SEND_REAL=1
export ANALYST_REPORT_INCLUDE_INTRADAY=0
cd /root/fib-bot
python3 analyst_report.py >> output/analyst_report_cron.log 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    # Аварийное уведомление о сбое ПРОЦЕССА (не рыночный сигнал) -- только
    # Леониду, см. agents/ops_agent.py. По его запросу 5 сентября 2026.
    tail -n 50 output/analyst_report_cron.log | python3 notify_failure.py "run_analyst_report.sh (analyst_report.py, 15 инструментов)"
fi
