#!/bin/bash
# Cron-запуск mtf_screener.py на VPS -- тот же принцип, что и у run_screener.sh
# для screener.py: подтягивает секреты из .env в окружение, включает
# FIB_BOT_LIVE и запускает скрипт, лог пишет в output/.
#
# По решению Леонида 6 сентября 2026 -- поэтапный запуск, НЕ сразу всё:
#   - MTF_SCREENER_INCLUDE_INTRADAY=0 -- Час/4Часа ПОКА выключены. С
#     интрадеем это ~1000 запросов к FMP в день сверх уже имеющихся ~800 у
#     трёх боевых веток -- лимит FMP Starter нигде не документирован
#     (см. universe.py), рисковать боевыми алертами ради непроверенной
#     новой ветки нельзя. Без интрадея эта ветка добавляет ~360
#     запросов/день -- тот же порядок, что и у уже проверенного
#     run_analyst_report.sh.
#   - MTF_SCREENER_SEND_REAL включён 10 сентября 2026, по прямому решению
#     Леонида в разговоре с Claude Code в тот же день: dry run отработал
#     4 дня (6-10 сентября) без ошибок, кандидаты стабильные (MSFT
#     месяц/нисходящий, NVDA неделя/нисходящий), алгоритм переводится в
#     боевой режим ВДОБАВОК к трём существующим веткам (IBM watch/скринер/
#     аналитик-дайджест), их не заменяет и не трогает.
set -a
source /root/fib-bot/.env
set +a
export FIB_BOT_LIVE=1
export MTF_SCREENER_INCLUDE_INTRADAY=0
export MTF_SCREENER_SEND_REAL=1
cd /root/fib-bot
python3 mtf_screener.py >> output/mtf_screener_cron.log 2>&1
RC=$?
if [ $RC -ne 0 ]; then
    # Аварийное уведомление о сбое ПРОЦЕССА (не рыночный сигнал) -- только
    # Леониду, см. agents/ops_agent.py. По его запросу 5 сентября 2026.
    tail -n 50 output/mtf_screener_cron.log | python3 notify_failure.py "run_mtf_screener.sh (mtf_screener.py, Месяц/Неделя, 15 инструментов)"
fi
