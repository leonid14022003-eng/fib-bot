"""
Тесты для НОВОГО кода 24 сентября 2026 (журнал сигналов и сопровождение,
запрос Леонида "журнал и развязки"):
- agents/journal_agent.py: build_entry(), record_level_alert(), load_journal(),
  evaluate_entry(), format_outcome_message()
- outcome_tracker.py: merge_evaluation(), evaluate_all(), notify()
- agents/dispatch_agent.py: send_via_telegram() -- message_id в результате и
  ответ (reply) на исходное сообщение

Реальных сетевых вызовов нет (requests.post замокан). Запускается отдельно:
python3 tests/test_journal_agent.py
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.data_agent import Candle, CandleSeries
from agents.dispatch_agent import Recipient, send_via_telegram
from agents.journal_agent import (
    MAX_TRACK_BARS,
    build_entry,
    evaluate_entry,
    format_outcome_message,
    load_journal,
    record_level_alert,
)
from outcome_tracker import evaluate_all, merge_evaluation, notify
from test_pipeline import _mk_bundle  # нисходящая структура 200 -> 100 (short)
from test_risk_agent import _mk_ascending_bundle  # восходящая 100 -> 200 (long)

NOW = datetime(2026, 9, 24, 13, 20, tzinfo=timezone.utc)
SIGNAL_DAY = date(2026, 9, 23)
SENT = {
    "dry_run": False,
    "sent_to": [
        {"recipient": "A", "status": "sent", "message_id": 101},
        {"recipient": "B", "status": "ERROR 400: x"},
    ],
}


def _c(day: int, o: float, h: float, l: float, cl: float) -> Candle:
    return Candle(dt=SIGNAL_DAY + timedelta(days=day), open=o, high=h, low=l, close=cl, volume=1)


def _long_entry(**kw) -> dict:
    # вход 138.2, стоп 100, цель 200, 1R = 38.2, +1R = 176.4
    return build_entry(_mk_ascending_bundle(0.618), 0.618, "broad", send_result=SENT, now=NOW,
                       entry_date=SIGNAL_DAY, **kw)


def _short_entry() -> dict:
    # вход 161.8, стоп 200, цель 100, 1R = 38.2, +1R = 123.6
    return build_entry(_mk_bundle(0.618), 0.618, "screener", send_result=SENT, now=NOW, entry_date=SIGNAL_DAY)


class TestBuildEntry(unittest.TestCase):
    def test_long_geometry_and_delivery(self):
        e = _long_entry(display_name="Asc Corp")
        self.assertEqual(e["direction"], "long")
        self.assertAlmostEqual(e["entry"], 138.2)
        self.assertEqual((e["stop"], e["target"]), (100.0, 200.0))
        self.assertAlmostEqual(e["risk"], 38.2)
        self.assertEqual(e["entry_date"], "2026-09-23")
        self.assertEqual(e["message_ids"], {"A": 101})  # только у дошедших
        self.assertEqual(e["delivery"]["B"], "ERROR 400: x")
        self.assertEqual(e["source"], "fmp")  # source_tag "synthetic" -> по умолчанию fmp
        self.assertIn("|broad|ASC|1D|0.618", e["id"])

    def test_short_direction(self):
        self.assertEqual(_short_entry()["direction"], "short")

    def test_price_beyond_point1_is_untrackable(self):
        e = build_entry(_mk_ascending_bundle(1.05), 1.0, "broad", send_result=SENT, now=NOW)
        self.assertIsNone(e["risk"])
        self.assertEqual(evaluate_entry(e, [_c(1, 90, 95, 80, 90)]).status, "untrackable")


class TestRecordAndLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "journal.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_writes_nothing(self):
        out = record_level_alert(_mk_bundle(0.618), 0.618, "screener", {"dry_run": True, "sent_to": []}, path=self.path)
        self.assertIsNone(out)
        self.assertFalse(self.path.exists())

    def test_real_send_appends_one_line_each(self):
        record_level_alert(_mk_bundle(0.618), 0.618, "screener", SENT, path=self.path)
        record_level_alert(_mk_ascending_bundle(0.786), 0.786, "broad", SENT, source="yahoo", path=self.path)
        entries = load_journal(self.path)
        self.assertEqual([e["branch"] for e in entries], ["screener", "broad"])
        self.assertEqual(entries[1]["source"], "yahoo")

    def test_broken_line_is_skipped(self):
        record_level_alert(_mk_bundle(0.618), 0.618, "screener", SENT, path=self.path)
        with self.path.open("a", encoding="utf-8") as f:
            f.write('{"id": "недописано')
        self.assertEqual(len(load_journal(self.path)), 1)

    def test_journal_failure_never_raises(self):
        out = record_level_alert(object(), 0.618, "screener", SENT, path=self.path)
        self.assertIsNone(out)


class TestEvaluateLong(unittest.TestCase):
    def test_no_candles_is_open(self):
        ev = evaluate_entry(_long_entry(), [])
        self.assertEqual((ev.status, ev.bars, ev.events), ("open", 0, []))

    def test_plus1r_then_target(self):
        candles = [_c(1, 140, 150, 135, 148), _c(2, 150, 180, 149, 175), _c(4, 175, 201, 170, 199)]
        ev = evaluate_entry(_long_entry(), candles)
        self.assertEqual(ev.status, "target")
        self.assertEqual([e["kind"] for e in ev.events], ["plus1r", "target"])
        self.assertEqual(ev.events[0]["bar"], 2)
        self.assertAlmostEqual(ev.events[0]["price"], 176.4)
        self.assertAlmostEqual(ev.events[1]["r"], (200 - 138.2) / 38.2)

    def test_wick_below_point1_is_not_stop(self):
        ev = evaluate_entry(_long_entry(), [_c(1, 130, 132, 95, 105)])
        self.assertEqual(ev.status, "open")
        self.assertLess(ev.mae_r, -1.0)

    def test_close_below_point1_is_stop_with_close_r(self):
        ev = evaluate_entry(_long_entry(), [_c(1, 130, 132, 110, 115), _c(2, 110, 112, 90, 95)])
        self.assertEqual(ev.status, "stop")
        self.assertEqual(ev.events[-1]["kind"], "stop")
        self.assertAlmostEqual(ev.events[-1]["r"], (95 - 138.2) / 38.2)  # хуже -1R -- честно
        self.assertEqual(ev.events[-1]["date"], "2026-09-25")

    def test_same_bar_target_and_stop_close_counts_target(self):
        ev = evaluate_entry(_long_entry(), [_c(1, 140, 205, 90, 95)])
        self.assertEqual(ev.status, "target")

    def test_expired_after_max_bars(self):
        candles = [_c(i, 140, 145, 135, 140) for i in range(1, MAX_TRACK_BARS + 10)]
        ev = evaluate_entry(_long_entry(), candles)
        self.assertEqual(ev.status, "expired")
        self.assertEqual(ev.events[-1]["bar"], MAX_TRACK_BARS)
        self.assertEqual(ev.bars, MAX_TRACK_BARS)


class TestEvaluateShort(unittest.TestCase):
    def test_short_plus1r_and_stop(self):
        candles = [_c(1, 160, 162, 120, 125), _c(2, 150, 210, 148, 205)]
        ev = evaluate_entry(_short_entry(), candles)
        self.assertEqual([e["kind"] for e in ev.events], ["plus1r", "stop"])
        self.assertAlmostEqual(ev.events[0]["price"], 161.8 - 38.2)
        self.assertAlmostEqual(ev.events[1]["r"], -(205 - 161.8) / 38.2)

    def test_short_target(self):
        ev = evaluate_entry(_short_entry(), [_c(1, 150, 152, 99, 101)])
        self.assertEqual(ev.status, "target")


class TestMessages(unittest.TestCase):
    def test_messages_have_balanced_tags_and_key_numbers(self):
        e = _long_entry(display_name="Asc <Corp>")
        for event, needle in (
            ({"kind": "plus1r", "date": "2026-09-25", "price": 176.4, "r": 1.0, "bar": 2}, "+1R"),
            ({"kind": "target", "date": "2026-09-25", "price": 200.0, "r": 1.62, "bar": 3}, "+1.6R"),
            ({"kind": "stop", "date": "2026-09-25", "price": 95.0, "r": -1.13, "bar": 4}, "-1.1R"),
            ({"kind": "expired", "date": "2026-09-25", "price": 140.0, "r": 0.05, "bar": 250}, "250 торговых дней"),
        ):
            msg = format_outcome_message(e, event)
            self.assertIn(needle, msg)
            self.assertIn("&lt;Corp&gt;", msg)
            self.assertEqual(len(re.findall(r"<b>", msg)), len(re.findall(r"</b>", msg)))
            self.assertIn("покупка отката от 0.618 по 138.20", msg)


def _series(symbol: str, candles: list[Candle]) -> CandleSeries:
    return CandleSeries(symbol=symbol, exchange_or_source="t", timeframe="1D", candles=candles, fetched_via="", fetch_note="")


class TestTracker(unittest.TestCase):
    def test_evaluate_all_skips_signal_day_and_today_and_survives_fetch_error(self):
        e_ok = _long_entry()
        e_bad = dict(_short_entry(), id="bad", symbol="BAD")
        today = SIGNAL_DAY + timedelta(days=3)
        candles = [
            _c(0, 140, 999, 1, 140),     # свеча сигнала -- не считается
            _c(1, 140, 150, 135, 148),
            _c(3, 140, 999, 1, 140),     # сегодняшняя, не закрыта -- не считается
        ]

        def fetch(source, symbol, hint, months):
            if symbol == "BAD":
                raise ValueError("Yahoo: структурно некорректные свечи")
            return _series(symbol, candles)

        outcomes: dict = {}
        log = evaluate_all([e_ok, e_bad], outcomes, today, fetch=fetch)
        self.assertEqual(outcomes[e_ok["id"]]["status"], "open")
        self.assertEqual(outcomes[e_ok["id"]]["bars"], 1)
        self.assertNotIn("bad", outcomes)
        self.assertTrue(any("BAD" in line and "не получены" in line for line in log))

    def test_closed_entry_is_not_refetched(self):
        e = _long_entry()
        outcomes = {e["id"]: {"status": "stop", "events": [], "notified": {}}}
        fetch = MagicMock()
        evaluate_all([e], outcomes, SIGNAL_DAY + timedelta(days=5), fetch=fetch)
        fetch.assert_not_called()

    def test_merge_keeps_events_when_bar_disappears(self):
        e = _long_entry()
        first = evaluate_entry(e, [_c(1, 140, 180, 135, 170)])
        prev = merge_evaluation(None, first, SIGNAL_DAY)
        # на следующий день Yahoo потерял эту свечу -- событие не должно пропасть
        again = merge_evaluation(prev, evaluate_entry(e, []), SIGNAL_DAY)
        self.assertEqual([x["kind"] for x in again["events"]], ["plus1r"])
        closed = dict(again, status="stop")
        self.assertEqual(merge_evaluation(closed, evaluate_entry(e, []), SIGNAL_DAY)["status"], "stop")

    def test_notify_dry_run_does_not_mark_and_stale_is_marked(self):
        e = _long_entry()
        today = SIGNAL_DAY + timedelta(days=10)
        outcomes = {e["id"]: {"status": "open", "notified": {}, "events": [
            {"kind": "plus1r", "date": str(SIGNAL_DAY + timedelta(days=1)), "price": 176.4, "r": 1.0, "bar": 1},
            {"kind": "stop", "date": str(today - timedelta(days=1)), "price": 95.0, "r": -1.1, "bar": 7},
        ]}}
        notify([e], outcomes, today, bot_token=None, recipients=[Recipient("A", "1")])
        self.assertEqual(outcomes[e["id"]]["notified"], {"plus1r": "stale"})

    def test_notify_real_marks_sent_and_replies_to_original(self):
        e = _long_entry()
        today = SIGNAL_DAY + timedelta(days=2)
        outcomes = {e["id"]: {"status": "open", "notified": {}, "events": [
            {"kind": "plus1r", "date": str(SIGNAL_DAY + timedelta(days=1)), "price": 176.4, "r": 1.0, "bar": 1},
        ]}}
        resp = MagicMock(status_code=200, content=b"x")
        resp.json.return_value = {"ok": True, "result": {"message_id": 555}}
        with patch("requests.post", return_value=resp) as post:
            notify([e], outcomes, today, bot_token="T", recipients=[Recipient("A", "1"), Recipient("B", "2")])
        self.assertEqual(outcomes[e["id"]]["notified"], {"plus1r": "sent"})
        payloads = [call.kwargs["json"] for call in post.call_args_list]
        self.assertEqual(payloads[0]["reply_parameters"], {"message_id": 101, "allow_sending_without_reply": True})
        self.assertNotIn("reply_parameters", payloads[1])  # у B исходного message_id нет


class TestSendMessageId(unittest.TestCase):
    def test_message_id_captured_and_plain_send_unchanged(self):
        resp = MagicMock(status_code=200, content=b"x")
        resp.json.return_value = {"ok": True, "result": {"message_id": 42}}
        with patch("requests.post", return_value=resp) as post:
            result = send_via_telegram("<b>x</b>", [Recipient("A", "1")], bot_token="T")
        self.assertEqual(result["sent_to"], [{"recipient": "A", "status": "sent", "message_id": 42}])
        self.assertEqual(post.call_args.kwargs["json"], {"chat_id": "1", "text": "<b>x</b>", "parse_mode": "HTML"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
