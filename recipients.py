"""Единый список получателей алертов бота.

Раньше один и тот же список повторялся отдельно в screener.py,
orchestrator.py, analyst_report.py, mtf_screener.py,
opportunity_scanner.py и broad_screener.py -- при смене chat_id или
состава получателей приходилось редактировать все файлы, и было легко
забыть один. Теперь редактируется только этот файл.
"""
from __future__ import annotations

from agents.dispatch_agent import Recipient

LEONID = Recipient(label="Леонид (@vaskodevasko)", telegram_chat_id="885989790")
SERGEY = Recipient(label="Сергей (@sergikvsl)", telegram_chat_id="1253087193")
PAVEL = Recipient(label="Pavel", telegram_chat_id="980723803")

ALL_RECIPIENTS = [LEONID, SERGEY, PAVEL]
