#!/bin/bash
cd /root/fib-bot
set -a
source /root/fib-bot/.env
set +a
export FIB_BOT_LIVE=1
export FIB_BOT_SYMBOL=IBM
python3 orchestrator.py >> /root/fib-bot/output/cron.log 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    # Аварийное уведомление о сбое ПРОЦЕССА (не рыночный сигнал) -- только
    # Леониду, см. agents/ops_agent.py. По его запросу 5 сентября 2026.
    tail -n 50 /root/fib-bot/output/cron.log | python3 /root/fib-bot/notify_failure.py "run_live.sh (orchestrator.py, IBM)"
fi
