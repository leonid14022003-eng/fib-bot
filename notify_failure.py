"""
CLI-обёртка над agents.ops_agent.notify_failure() -- вызывается из
run_live.sh / run_screener.sh / run_analyst_report.sh, когда python3
<job> завершился с ненулевым кодом выхода.

Использование (из самих *.sh):
    tail -n 50 <log> | python3 notify_failure.py "<имя джоба>"

Хвост лога читается из stdin, уведомление уходит ТОЛЬКО Леониду (см.
agents/ops_agent.py про то, почему не всем троим).
"""
from __future__ import annotations

import os
import sys

from agents.ops_agent import notify_failure

if __name__ == "__main__":
    job_name = sys.argv[1] if len(sys.argv) > 1 else "неизвестный джоб"
    log_tail = sys.stdin.read()
    result = notify_failure(job_name, log_tail, bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"))
    for entry in result["sent_to"]:
        print(f"notify_failure: {entry['recipient']}: {entry['status']}")
