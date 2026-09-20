#!/bin/bash
# Cron-запуск broad_screener.py на VPS -- тот же принцип, что и у
# run_screener.sh для screener.py: подтягивает секреты из .env в
# окружение, включает FIB_BOT_LIVE и запускает скрипт, лог пишет в
# output/.
#
# Поставлено на минуту :20 -- НЕ на предлагавшуюся ранее :15, потому что
# run_live.sh идёт по */15 * * * * и тоже срабатывает в :15 (и в :00,
# :30, :45) -- совпадение по минуте с уже боевой веткой. :20 не
# пересекается ни с одним из существующих триггеров (:00 -- run_live.sh
# и run_screener.sh, :05 -- run_analyst_report.sh, :10 --
# run_mtf_screener.sh, :15/:30/:45 -- run_live.sh).
#
# Первое боевое включение 20 сентября 2026, по прямому решению Леонида:
# память заprimed заново тем же днём (python3 broad_screener.py --prime,
# состояние от 14 сентября устарело за 6 дней) -- первый боевой прогон
# должен увидеть 0 алертов, реагировать только на то, что реально
# изменится дальше.
set -a
source /root/fib-bot/.env
set +a
export FIB_BOT_LIVE=1
cd /root/fib-bot
python3 broad_screener.py >> output/broad_screener_cron.log 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    # Аварийное уведомление о сбое ПРОЦЕССА (не рыночный сигнал) -- только
    # Леониду, см. agents/ops_agent.py.
    tail -n 50 output/broad_screener_cron.log | python3 notify_failure.py "run_broad_screener.sh (broad_screener.py, 233 инструмента)"
fi
