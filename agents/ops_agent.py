"""
Ops Agent
=========
Аварийное уведомление о сбое САМОГО ПРОЦЕССА (не рыночный сигнал) -- когда
run_live.sh / run_screener.sh / run_analyst_report.sh завершаются с
ненулевым кодом выхода (сеть недоступна, упавший необработанным
исключением скрипт, лимит FMP и т.п.).

Отдельно от dispatch_agent.py: рыночные алерты уходят всем троим
(Леонид/Сергей/Pavel) -- это то, ради чего бот существует. Сбой самого
процесса -- операционная проблема, не торговый сигнал, и уходит ТОЛЬКО
Леониду, чтобы не путать Сергея и Pavel сообщениями "бот сломался", на
которые они не могут и не должны реагировать. По прямому запросу Леонида,
5 сентября 2026 ("чтобы всё работало") -- до этого момента упавший cron
был виден только в файле лога, который никто не читает проактивно.

Best-effort по духу, что и Context/Intraday агенты: если сама попытка
уведомить не удалась (нет сети, невалидный токен) -- notify_failure()
просто возвращает честный статус через send_via_telegram(), ничего не
бросает наружу. Вызывающий cron-скрипт и так уже в ветке "что-то пошло не
так" -- плодить вторую необработанную ошибку поверх первой незачем.
"""
from __future__ import annotations

import html

from agents.dispatch_agent import Recipient, send_via_telegram

_OPS_RECIPIENT = Recipient(label="Леонид (@vaskodevasko)", telegram_chat_id="885989790")

# Лимит на хвост лога в сообщении -- у Telegram sendMessage лимит 4096
# символов на текст (см. dispatch_agent.py, send_via_telegram), оставляем
# запас под остальной текст самого уведомления.
LOG_TAIL_LIMIT = 3000


def notify_failure(job_name: str, log_tail: str, bot_token: str | None) -> dict:
    """
    bot_token=None -> DRY RUN (тот же принцип, что и у send_via_telegram
    везде в проекте) -- ничего реально не уходит, удобно для тестов и
    ручной проверки логики без реальной отправки.
    """
    truncated = log_tail[-LOG_TAIL_LIMIT:] if log_tail else "(лог пуст)"
    message = (
        f"⚠️ <b>{html.escape(job_name)}</b> завершился с ошибкой (exit code != 0).\n"
        f"Последние строки лога:\n<pre>{html.escape(truncated)}</pre>"
    )
    return send_via_telegram(message, [_OPS_RECIPIENT], bot_token=bot_token)
