"""
Сопровождение разосланных алертов (24 сентября 2026) -- по запросу Леонида
"журнал и развязки". Читает журнал сигналов (output/signal_journal.jsonl,
его пишут боевые ветки, см. agents/journal_agent.py), раз в день досматривает
дневные свечи после каждого сигнала и сообщает о развязке: +1R, цель (точка
2), стоп (закрытие за точкой 1), истёк срок сопровождения.

Своя память -- output/signal_outcomes.json (оценка по каждой записи + какие
события уже разосланы). Журнал этот скрипт только читает.

Отбор свечей: только ЗАВЕРШЁННЫЕ -- дата строго меньше сегодняшней по UTC.
Запуск утром по UTC (до открытия Европы) видит закрытый вчерашний день и США,
и Азии. Свеча сигнала не считается (см. докстринг journal_agent.py).

Дыры в данных Yahoo (см. CLAUDE.md, 24 сентября): оценка каждый раз
пересчитывается по всей истории после сигнала, но однажды найденное событие
не забывается, даже если свеча потом пропала из ответа; закрытая запись
больше не пересчитывается.

Отправка: как у analyst_report.py/mtf_screener.py -- нужны ОБА условия,
TELEGRAM_BOT_TOKEN в окружении И OUTCOME_TRACKER_SEND_REAL=1. Иначе dry run:
печатает, что было бы отправлено, память о разосланном не трогает (оценки
сохраняет -- это просто данные). Событие старше STALE_DAYS не рассылается
никогда (помечается "stale"): при включении отправки не придёт пачка
новостей недельной давности.

Запуск:
    python3 outcome_tracker.py            # оценка + рассылка (или dry run)
    python3 outcome_tracker.py --report   # сводка по журналу, ничего не качает и не шлёт
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from agents.data_agent import load_fmp_daily, load_yahoo_daily
from agents.dispatch_agent import send_via_telegram
from agents.journal_agent import JOURNAL_PATH, evaluate_entry, format_outcome_message, load_journal
from recipients import ALL_RECIPIENTS

ROOT = Path(__file__).resolve().parent
OUTCOMES_PATH = ROOT / "output" / "signal_outcomes.json"
STALE_DAYS = 5
CLOSED = {"target", "stop", "expired", "untrackable"}


def _load_outcomes(path: Path = OUTCOMES_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_outcomes(data: dict, path: Path = OUTCOMES_PATH) -> None:
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _fetch_daily(source: str, symbol: str, exchange_hint: str, months_back: int):
    if source == "yahoo":
        return load_yahoo_daily(symbol, months_back=months_back, exchange_hint=exchange_hint)
    if source == "fmp":
        kwargs = {"exchange_hint": exchange_hint} if exchange_hint else {}
        return load_fmp_daily(symbol, months_back=months_back, **kwargs)
    raise ValueError(f"источник {source!r} трекер не поддерживает")


def merge_evaluation(prev: dict | None, ev, today: date) -> dict:
    """Новая оценка поверх старой: события только добавляются (по kind), статус
    закрытия не откатывается -- защита от пропавших свечей Yahoo."""
    prev = prev or {}
    events = list(prev.get("events", []))
    known = {e["kind"] for e in events}
    for e in ev.events:
        if e["kind"] not in known:
            events.append(e)
            known.add(e["kind"])
    status = prev.get("status") if prev.get("status") in CLOSED else ev.status
    return {
        "status": status,
        "events": events,
        "notified": prev.get("notified", {}),
        "bars": max(ev.bars, prev.get("bars", 0)),
        "last_close": ev.last_close if ev.last_close is not None else prev.get("last_close"),
        "last_r": ev.last_r if ev.last_r is not None else prev.get("last_r"),
        "mfe_r": ev.mfe_r if ev.mfe_r is not None else prev.get("mfe_r"),
        "mae_r": ev.mae_r if ev.mae_r is not None else prev.get("mae_r"),
        "updated": today.isoformat(),
    }


def evaluate_all(entries: list[dict], outcomes: dict, today: date, fetch=_fetch_daily) -> list[str]:
    """Пересчитать все незакрытые записи. Одна загрузка свечей на (источник,
    символ). Возвращает строки для лога; сбой одного символа не прерывает
    остальные."""
    log: list[str] = []
    groups: dict[tuple[str, str], list[dict]] = {}
    for e in entries:
        if outcomes.get(e["id"], {}).get("status") in CLOSED:
            continue
        if not e.get("risk"):
            outcomes[e["id"]] = merge_evaluation(outcomes.get(e["id"]), evaluate_entry(e, []), today)
            continue
        groups.setdefault((e["source"], e["symbol"]), []).append(e)

    for (source, symbol), group in groups.items():
        earliest = min(date.fromisoformat(e["entry_date"]) for e in group)
        months_back = max(2, math.ceil((today - earliest).days / 30.44) + 1)
        try:
            series = fetch(source, symbol, group[0].get("exchange_hint", ""), months_back)
        except Exception as ex:  # сеть, лимит, битые свечи -- повторим завтра
            log.append(f"  {symbol} ({source}): свечи не получены -- {type(ex).__name__}: {str(ex)[:160]}")
            continue
        for e in group:
            start = date.fromisoformat(e["entry_date"])
            after = [c for c in series.candles if start < c.dt < today]
            ev = evaluate_entry(e, after)
            outcomes[e["id"]] = merge_evaluation(outcomes.get(e["id"]), ev, today)
            o = outcomes[e["id"]]
            r = f"{o['last_r']:+.2f}R" if o["last_r"] is not None else "—"
            log.append(f"  {symbol:10s} {e['alert_level']:<5g} {e['direction']:5s} {o['status']:8s} {o['bars']:>3} дн. сейчас {r}")
    return log


def notify(entries: list[dict], outcomes: dict, today: date, bot_token: str | None, recipients=None) -> list[str]:
    """Разослать ещё не разосланные события. bot_token None -- dry run: только
    печать, отметки "sent" не ставятся. Устаревшие события помечаются "stale"
    в любом режиме."""
    recipients = recipients if recipients is not None else ALL_RECIPIENTS
    log: list[str] = []
    by_id = {e["id"]: e for e in entries}
    for entry_id, o in outcomes.items():
        entry = by_id.get(entry_id)
        if entry is None:
            continue
        notified = o.setdefault("notified", {})
        for event in o.get("events", []):
            kind = event["kind"]
            if kind in notified:
                continue
            age = (today - date.fromisoformat(event["date"])).days
            if age > STALE_DAYS:
                notified[kind] = "stale"
                log.append(f"  {entry['symbol']}: {kind} от {event['date']} устарело ({age} дн.) -- не рассылаем")
                continue
            message = format_outcome_message(entry, event)
            result = send_via_telegram(
                message, recipients, bot_token=bot_token, reply_to_message_ids=entry.get("message_ids") or None
            )
            statuses = ", ".join(f"{s['recipient']}: {s['status']}" for s in result["sent_to"])
            log.append(f"  {entry['symbol']}: {kind} (dry_run={result['dry_run']})\n{message}\n    {statuses}")
            if not result["dry_run"] and any(s["status"].startswith("sent") for s in result["sent_to"]):
                notified[kind] = "sent"
    return log


def report(entries: list[dict], outcomes: dict) -> str:
    """Сводка по журналу. Малая выборка -- проценты тут не статистика, а счёт."""
    lines = [f"Журнал: {len(entries)} сигналов ({JOURNAL_PATH.name})"]
    if not entries:
        return lines[0] + " -- пока пусто."
    rows = []
    for e in entries:
        o = outcomes.get(e["id"], {})
        kinds = {ev["kind"] for ev in o.get("events", [])}
        rows.append((e, o, kinds))
        # закрытый сигнал -- R на момент закрытия, открытый -- по последнему закрытию
        r = next((ev["r"] for ev in o.get("events", []) if ev["kind"] == o.get("status")), o.get("last_r"))
        lines.append(
            f"  {e['ts'][:10]} {e['branch']:9s} {e['symbol']:10s} {e['timeframe']:6s} {e['alert_level']:<5g} "
            f"{e['direction']:5s} вход {e['entry']:<10.4g} стоп {e['stop']:<10.4g} "
            f"{o.get('status', 'не оценён'):9s} {o.get('bars', 0):>3} дн."
            + (f" R {r:+.2f}" if r is not None else "")
        )
    lines.append("")
    for label, subset in (("все", rows), ("long", [x for x in rows if x[0]["direction"] == "long"]),
                          ("short", [x for x in rows if x[0]["direction"] == "short"])):
        tracked = [x for x in subset if x[1].get("bars", 0) > 0]
        if not tracked:
            lines.append(f"{label}: оценённых сигналов нет")
            continue
        closed = [x for x in tracked if x[1].get("status") in {"target", "stop", "expired"}]
        count = {k: sum(1 for x in closed if x[1]["status"] == k) for k in ("target", "stop", "expired")}
        plus1r = sum(1 for x in tracked if "plus1r" in x[2])
        closing_r = [next(ev["r"] for ev in x[1]["events"] if ev["kind"] == x[1]["status"]) for x in closed]
        avg_closed = sum(closing_r) / len(closing_r) if closing_r else None
        lines.append(
            f"{label}: оценено {len(tracked)}, +1R достигнут у {plus1r}; закрыто {len(closed)} "
            f"(цель {count['target']}, стоп {count['stop']}, истёк {count['expired']})"
            + (f", средний R закрытых {avg_closed:+.2f}" if avg_closed is not None else "")
        )
    lines.append("Выборка мала -- это счёт, не оценка преимущества (см. research/ в CLAUDE.md).")
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    entries = load_journal()
    outcomes = _load_outcomes()
    if "--report" in argv:
        print(report(entries, outcomes))
        return

    today = datetime.now(timezone.utc).date()
    send_real = bool(os.environ.get("TELEGRAM_BOT_TOKEN")) and os.environ.get("OUTCOME_TRACKER_SEND_REAL") == "1"
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN") if send_real else None
    print("=" * 70)
    print(f"СОПРОВОЖДЕНИЕ СИГНАЛОВ -- {today}, записей в журнале: {len(entries)}, "
          f"реальная отправка: {'ДА' if send_real else 'НЕТ (dry run)'}")
    print("=" * 70)
    for line in evaluate_all(entries, outcomes, today):
        print(line)
    for line in notify(entries, outcomes, today, bot_token):
        print(line)
    _save_outcomes(outcomes)
    print(f"Оценки сохранены в {OUTCOMES_PATH}")


if __name__ == "__main__":
    main(sys.argv[1:])
