#!/bin/bash
# Cron-запуск crypto_screener.py на VPS -- тот же принцип, что и у
# run_broad_screener.sh для broad_screener.py.
#
# Поставлено на минуту :25 -- свободна от всех существующих триггеров
# (:00 -- run_live.sh и run_screener.sh, :05 -- run_analyst_report.sh,
# :10 -- run_mtf_screener.sh, :15/:30/:45 -- run_live.sh, :20 --
# run_broad_screener.sh) и идёт через 5 минут после broad_screener.sh,
# не одновременно с ним.
#
# Первое боевое включение 20 сентября 2026, по прямому решению Леонида:
# память заprimed тем же днём (python3 crypto_screener.py --prime) --
# первый боевой прогон должен увидеть 0 алертов, реагировать только на
# то, что реально изменится дальше.
set -a
source /root/fib-bot/.env
set +a
export FIB_BOT_LIVE=1
cd /root/fib-bot
python3 crypto_screener.py >> output/crypto_screener_cron.log 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    # Аварийное уведомление о сбое ПРОЦЕССА (не рыночный сигнал) -- только
    # Леониду, см. agents/ops_agent.py.
    tail -n 50 output/crypto_screener_cron.log | python3 notify_failure.py "run_crypto_screener.sh (crypto_screener.py, топ-50 крипты)"
fi
