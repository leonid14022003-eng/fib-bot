#!/bin/bash
# Cron-запуск outcome_tracker.py (сопровождение разосланных алертов, 24
# сентября 2026, см. agents/journal_agent.py). Раз в день утром по UTC:
# вчерашние дневные свечи США и Азии к этому времени закрыты.
#
# Предлагаемая строка crontab (вносит Леонид -- автоклассификатор Claude Code
# блокирует правку crontab): 40 6 * * * bash /root/fib-bot/run_outcome_tracker.sh
# Минута :40 не пересекается с остальными ветками (:00/:05/:10/:15/:20/:30/:45).
#
# Реальная отправка сообщений о развязке -- OUTCOME_TRACKER_SEND_REAL=1,
# включена 24 сентября 2026 по прямому решению Леонида ("включи отправку
# развязок"). Без неё -- dry run: оценки считаются и сохраняются, в Telegram
# ничего не уходит.
set -a
source /root/fib-bot/.env
set +a
export OUTCOME_TRACKER_SEND_REAL=1
cd /root/fib-bot
python3 outcome_tracker.py >> output/outcome_tracker_cron.log 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    # Аварийное уведомление о сбое ПРОЦЕССА (не рыночный сигнал) -- только
    # Леониду, см. agents/ops_agent.py.
    tail -n 50 output/outcome_tracker_cron.log | python3 notify_failure.py "run_outcome_tracker.sh (outcome_tracker.py, сопровождение сигналов)"
fi
