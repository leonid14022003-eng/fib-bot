#!/bin/bash
# Cron-запуск screener.py на VPS. Тот же принцип, что и у run_live.sh для
# orchestrator.py: подтягивает секреты из .env в окружение, включает
# FIB_BOT_LIVE и запускает скрипт, лог пишет в output/.
#
# В crontab поставлен РЕЖЕ, чем точечный IBM-watch (0 * * * * -- раз в час,
# а не раз в 15 минут) -- решение Леонида от 24 августа: коррекция до 0.618
# не случается настолько быстро, чтобы имело смысл проверять чаще.
set -a
source /root/fib-bot/.env
set +a
export FIB_BOT_LIVE=1
cd /root/fib-bot
python3 screener.py >> output/screener_cron.log 2>&1
