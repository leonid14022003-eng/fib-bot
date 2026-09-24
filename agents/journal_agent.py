"""
Journal Agent (24 сентября 2026) -- журнал отправленных алертов и оценка их
развязки. По запросу Леонида "журнал и развязки": до этого все цифры о
качестве сигналов бота были только из бэктеста (research/), а о судьбе
реально разосланных алертов никто не узнавал.

Две части:
  1. ЗАПИСЬ (record_level_alert) -- боевые ветки (orchestrator.py,
     screener.py, mtf_screener.py, broad_screener.py) после РЕАЛЬНОЙ
     отправки уровневого алерта дописывают одну строку JSON в
     output/signal_journal.jsonl. Только дописывают (append под flock) --
     ветки идут по cron в разные минуты и могут пересечься по времени.
     Ошибка журнала никогда не роняет и не задерживает сам алерт: всё
     внутри try/except, в лог пишется предупреждение.
  2. ОЦЕНКА (evaluate_entry) -- чистая функция без I/O: по записи журнала
     и дневным свечам ПОСЛЕ даты сигнала говорит, что произошло. Её
     вызывает outcome_tracker.py (отдельный скрипт, свой файл состояния).

Геометрия -- та же, что в тексте алерта и в agents/risk_agent.py: вход =
цена из алерта, стоп = точка 1, цель = точка 2, 1R = |вход - точка 1|.
Восходящая структура -> покупка отката (long), нисходящая -> продажа
отскока (short).

События (каждое фиксируется один раз, по дате свечи):
  - "plus1r"  -- тень свечи дошла до вход ± 1R (касание, как лимитный ордер);
  - "target"  -- тень дошла до точки 2 (касание); сопровождение закрыто;
  - "stop"    -- ЗАКРЫТИЕ свечи за точкой 1 (так алерт и формулирует
                 инвалидацию, так же считает backtest.py); закрыто, R по
                 цене закрытия -- может быть хуже -1R;
  - "expired" -- MAX_TRACK_BARS свечей без цели и без стопа; закрыто, R по
                 последнему закрытию.
Внутри одной свечи порядок: +1R -> цель -> стоп. Касание тенью случилось
раньше закрытия той же свечи, поэтому свеча, которая дошла до цели и
закрылась за точкой 1, засчитывается как цель (редкий случай огромной свечи;
честно видно по MAE в отчёте).

Свеча сигнала в оценку не входит: цена в алерте -- её закрытие (или цена
внутри ещё не закрытой свечи у FMP), и что было в ней ПОСЛЕ отправки, по
дневным данным не восстановить. Считаем со следующей свечи.
"""
from __future__ import annotations

import fcntl
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from agents.dispatch_agent import _esc, _fmt_price
from agents.risk_agent import build_risk_plan

ROOT = Path(__file__).resolve().parent.parent
JOURNAL_PATH = ROOT / "output" / "signal_journal.jsonl"

# 250 торговых дней ~ год: самый длинный горизонт исследования 21 сентября
# (60/120/250 дней). Цель (точка 2) -- масштаб всего движения, до неё за
# 60 дней доходит ~2% покупок отката, поэтому короткий срок оборвал бы
# большую часть развязок искусственно.
MAX_TRACK_BARS = 250

_DIRECTION_BY_STRUCTURE = {"восходящий": "long", "нисходящий": "short"}
_DIRECTION_RU = {"long": "покупка отката", "short": "продажа отскока"}


def _source_kind(source_tag: str) -> str:
    """Откуда трекеру брать свечи для этой записи -- тот же источник, что и у
    алерта, чтобы цены сопровождения совпадали с ценами в сообщении."""
    if "Yahoo" in source_tag:
        return "yahoo"
    if "Binance" in source_tag:
        return "binance"
    return "fmp"


def build_entry(
    bundle,
    alert_level: float,
    branch: str,
    display_name: str | None = None,
    exchange_hint: str = "",
    source: str | None = None,
    send_result: dict | None = None,
    now: datetime | None = None,
    entry_date: date | None = None,
) -> dict:
    """Запись журнала по одному уровневому алерту. Чистая функция (без I/O).

    entry_date -- дата свечи, чьё закрытие стоит в алерте как цена. Если не
    передана, берётся дата сигнала по UTC (для веток, где свечи под рукой
    нет) -- трекер тогда начнёт со следующего календарного дня.
    """
    now = now or datetime.now(timezone.utc)
    s, n = bundle.structure, bundle.nearest
    entry = n.current_price
    plan = build_risk_plan(entry, s.point1.price, s.point2.price)
    structure_id = f"{s.point1.dt}|{s.point2.dt}|{s.direction.value}"
    result = send_result or {}
    delivery = {e["recipient"]: e["status"] for e in result.get("sent_to", [])}
    message_ids = {e["recipient"]: e["message_id"] for e in result.get("sent_to", []) if e.get("message_id")}
    return {
        "id": f"{now:%Y%m%dT%H%M%SZ}|{branch}|{bundle.symbol}|{bundle.timeframe}|{alert_level:g}",
        "ts": now.isoformat(timespec="seconds"),
        "branch": branch,
        "symbol": bundle.symbol,
        "display_name": display_name or bundle.symbol,
        "source": source or _source_kind(bundle.source_tag),
        "exchange_hint": exchange_hint,
        "timeframe": bundle.timeframe,
        "structure_id": structure_id,
        "direction": _DIRECTION_BY_STRUCTURE.get(s.direction.value, "long"),
        "alert_level": alert_level,
        "entry": entry,
        "entry_date": (entry_date or now.date()).isoformat(),
        "stop": s.point1.price,
        "target": s.point2.price,
        "point1_date": str(s.point1.dt),
        "point2_date": str(s.point2.dt),
        # None -- цена не между точками (уровень 1.0 и дальше): плана вход/стоп/
        # цель нет, запись остаётся в журнале, но не сопровождается.
        "risk": abs(entry - s.point1.price) if plan is not None else None,
        "stop_pct": round(plan.stop_pct, 2) if plan is not None else None,
        "rr": round(plan.rr, 2) if plan is not None else None,
        "delivery": delivery,
        "message_ids": message_ids,
    }


def append_entry(entry: dict, path: Path = JOURNAL_PATH) -> None:
    """Дописать одну строку. flock -- на случай, если две ветки cron пишут
    одновременно; одна запись = один write()."""
    path.parent.mkdir(exist_ok=True)
    line = (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def record_level_alert(
    bundle,
    alert_level: float,
    branch: str,
    send_result: dict,
    display_name: str | None = None,
    exchange_hint: str = "",
    source: str | None = None,
    entry_date: date | None = None,
    path: Path = JOURNAL_PATH,
) -> dict | None:
    """Точка входа для боевых веток. В dry run не пишет ничего (журнал --
    только о том, что реально ушло людям). Никогда не бросает исключение:
    сбой журнала не должен стоить алерта."""
    try:
        if send_result.get("dry_run", True):
            return None
        entry = build_entry(
            bundle, alert_level, branch, display_name=display_name, exchange_hint=exchange_hint,
            source=source, send_result=send_result, entry_date=entry_date,
        )
        append_entry(entry, path)
        return entry
    except Exception as e:  # журнал -- вспомогательный, алерт важнее
        print(f"  ВНИМАНИЕ: запись в журнал сигналов не удалась ({type(e).__name__}: {e})")
        return None


def load_journal(path: Path = JOURNAL_PATH) -> list[dict]:
    """Все записи. Битая/недописанная строка пропускается (её допишут --
    прочитается в следующий раз), а не роняет трекер."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    entries = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


@dataclass
class Evaluation:
    status: str  # "open" | "target" | "stop" | "expired" | "untrackable"
    events: list[dict] = field(default_factory=list)  # {"kind", "date", "price", "r", "bar"}
    bars: int = 0
    last_close: float | None = None
    last_r: float | None = None
    mfe_r: float | None = None  # лучшая точка по тени, в R
    mae_r: float | None = None  # худшая точка по тени, в R


def evaluate_entry(entry: dict, candles_after: list, max_bars: int = MAX_TRACK_BARS) -> Evaluation:
    """Что произошло с сигналом. candles_after -- ЗАВЕРШЁННЫЕ дневные свечи
    строго после entry_date, по возрастанию даты (отбор -- на вызывающем)."""
    risk = entry.get("risk")
    if not risk or risk <= 0:
        return Evaluation(status="untrackable")
    sign = 1.0 if entry["direction"] == "long" else -1.0
    e_price, stop, target = entry["entry"], entry["stop"], entry["target"]
    ev = Evaluation(status="open")
    got_plus1r = False

    for i, c in enumerate(candles_after[:max_bars], start=1):
        fav = c.high if sign > 0 else c.low
        adv = c.low if sign > 0 else c.high
        fav_r = sign * (fav - e_price) / risk
        adv_r = sign * (adv - e_price) / risk
        close_r = sign * (c.close - e_price) / risk
        ev.mfe_r = fav_r if ev.mfe_r is None else max(ev.mfe_r, fav_r)
        ev.mae_r = adv_r if ev.mae_r is None else min(ev.mae_r, adv_r)
        ev.bars, ev.last_close, ev.last_r = i, c.close, close_r
        dt = str(c.dt)

        if not got_plus1r and fav_r >= 1.0:
            got_plus1r = True
            ev.events.append({"kind": "plus1r", "date": dt, "price": e_price + sign * risk, "r": 1.0, "bar": i})
        if sign * (fav - target) >= 0:
            r = sign * (target - e_price) / risk
            ev.events.append({"kind": "target", "date": dt, "price": target, "r": r, "bar": i})
            ev.status = "target"
            return ev
        if sign * (c.close - stop) < 0:
            ev.events.append({"kind": "stop", "date": dt, "price": c.close, "r": close_r, "bar": i})
            ev.status = "stop"
            return ev
        if i == max_bars:
            ev.events.append({"kind": "expired", "date": dt, "price": c.close, "r": close_r, "bar": i})
            ev.status = "expired"
            return ev
    return ev


def _fmt_date(iso: str) -> str:
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d.%m")
    except ValueError:
        return iso


def format_outcome_message(entry: dict, event: dict) -> str:
    """Короткое сообщение о развязке -- 2 строки, в стиле компактного алерта.
    Уходит ответом (reply) на исходный алерт, если его message_id известен."""
    symbol = _esc(entry["symbol"])
    name = entry.get("display_name") or entry["symbol"]
    title = f"{_esc(name)} ({symbol})" if name != entry["symbol"] else symbol
    kind, r = event["kind"], event["r"]
    if kind == "plus1r":
        head = f"✅ <b>{title}</b> — +1R: цена дошла до {_fmt_price(event['price'])}"
    elif kind == "target":
        head = f"🎯 <b>{title}</b> — цель (точка 2) {_fmt_price(event['price'])} достигнута · {r:+.1f}R"
    elif kind == "stop":
        head = (
            f"🛑 <b>{title}</b> — сценарий отменён: закрытие {_fmt_price(event['price'])} "
            f"за точкой 1 ({_fmt_price(entry['stop'])}) · {r:+.1f}R"
        )
    else:
        head = (
            f"⌛ <b>{title}</b> — {event['bar']} торговых дней без цели и стопа, "
            f"сопровождение закончено · сейчас {r:+.1f}R"
        )
    tail = (
        f"Сигнал {_fmt_date(entry['ts'])}: {_DIRECTION_RU.get(entry['direction'], entry['direction'])}"
        f" от {entry['alert_level']:g} по {_fmt_price(entry['entry'])} · {_esc(entry['timeframe'])}"
        f" · {_fmt_date(event['date'])}, {event['bar']}-й торговый день"
    )
    return head + "\n" + tail
