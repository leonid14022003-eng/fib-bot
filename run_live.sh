#!/bin/bash
cd /root/fib-bot
set -a
source /root/fib-bot/.env
set +a
export FIB_BOT_LIVE=1
export FIB_BOT_SYMBOL=IBM
python3 orchestrator.py >> /root/fib-bot/output/cron.log 2>&1
