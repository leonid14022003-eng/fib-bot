"""
Тесты для НОВОГО кода 20 сентября 2026 (крипто-скринер -- топ-50 по
капитализации CoinGecko среди Binance USDT perpetual, по прямому решению
Леонида, аналог фичи 0.19.0 десктопной FibonacciDesk):
- data/crypto_universe.py: _base_asset(), build_crypto_universe()
- crypto_screener.py: _load_binance_for_scan(), _new_local_grid_events()

Тот же принцип, что и в tests/test_broad_screener.py (unittest.TestCase +
patch("requests.get", ...)). Запускается отдельно:
python3 tests/test_crypto_screener.py
"""
from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.local_grid_agent import Direction, ExtremePoint, LocalGrid, LocalState
from data.crypto_universe import _base_asset, build_crypto_universe


def _mock_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.raise_for_status.side_effect = None
    return resp


def _exchange_info(symbols):
    return {"symbols": symbols}


def _perp(symbol, status="TRADING", contract_type="PERPETUAL", quote="USDT"):
    return {"symbol": symbol, "status": status, "contractType": contract_type, "quoteAsset": quote}


def _coin(coin_id, symbol, rank):
    return {"id": coin_id, "symbol": symbol, "name": coin_id.capitalize(), "market_cap_rank": rank}


class TestBaseAsset(unittest.TestCase):
    def test_plain_symbol(self):
        self.assertEqual(_base_asset("BTCUSDT"), "BTC")

    def test_strips_thousand_multiplier(self):
        self.assertEqual(_base_asset("1000SHIBUSDT"), "SHIB")

    def test_strips_million_multiplier(self):
        self.assertEqual(_base_asset("1000000BABYDOGEUSDT"), "BABYDOGE")

    def test_non_usdt_quote_returns_none(self):
        self.assertIsNone(_base_asset("BTCBUSD"))
        self.assertIsNone(_base_asset("BTCUSDC"))


class TestBuildCryptoUniverse(unittest.TestCase):
    def _run(self, coingecko_pages, binance_symbols, **kwargs):
        binance_payload = _exchange_info(binance_symbols)
        responses = [_mock_response(binance_payload)] + [_mock_response(p) for p in coingecko_pages]
        with patch("requests.get", side_effect=responses):
            return build_crypto_universe(**kwargs)

    def test_matches_top_coins_with_unambiguous_binance_contract(self):
        coins = [_coin("bitcoin", "btc", 1), _coin("ethereum", "eth", 2)]
        symbols = [_perp("BTCUSDT"), _perp("ETHUSDT")]
        matched, skipped = self._run([coins], symbols, top_n=50, candidate_pages=1, per_page=100)
        self.assertEqual([m["binance_symbol"] for m in matched], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(skipped, [])

    def test_skips_coin_with_no_binance_perpetual(self):
        coins = [_coin("bitcoin", "btc", 1), _coin("nocoin", "nope", 2)]
        symbols = [_perp("BTCUSDT")]
        matched, skipped = self._run([coins], symbols, top_n=50, candidate_pages=1, per_page=100)
        self.assertEqual([m["symbol"] for m in matched], ["BTC"])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["symbol"], "NOPE")
        self.assertIn("нет Binance", skipped[0]["reason"])

    def test_skips_ambiguous_base_asset(self):
        # Один и тот же базовый тикер после снятия множителя -- два разных
        # реальных контракта Binance (искусственный, но проверяет защиту).
        coins = [_coin("bitcoin", "btc", 1)]
        symbols = [_perp("BTCUSDT"), _perp("1000BTCUSDT")]
        matched, skipped = self._run([coins], symbols, top_n=50, candidate_pages=1, per_page=100)
        self.assertEqual(matched, [])
        self.assertEqual(len(skipped), 1)
        self.assertIn("неоднозначно", skipped[0]["reason"])

    def test_ignores_non_perpetual_and_non_trading_and_non_usdt(self):
        coins = [_coin("bitcoin", "btc", 1)]
        symbols = [
            _perp("BTCUSDT", status="BREAK"),
            _perp("BTCBUSD"),
            _perp("BTCUSD_PERP", quote="USD"),
        ]
        matched, skipped = self._run([coins], symbols, top_n=50, candidate_pages=1, per_page=100)
        self.assertEqual(matched, [])
        self.assertEqual(skipped[0]["reason"], "нет Binance USDT perpetual")

    def test_stops_at_top_n_even_with_more_candidates(self):
        coins = [_coin(f"coin{i}", f"c{i}", i) for i in range(1, 6)]
        symbols = [_perp(f"C{i}USDT") for i in range(1, 6)]
        matched, _skipped = self._run([coins], symbols, top_n=3, candidate_pages=1, per_page=100)
        self.assertEqual(len(matched), 3)
        self.assertEqual([m["symbol"] for m in matched], ["C1", "C2", "C3"])

    def test_paginates_coingecko_until_enough_candidates_or_pages_exhausted(self):
        page1 = [_coin("bitcoin", "btc", 1)]
        page2 = [_coin("ethereum", "eth", 2)]
        symbols = [_perp("BTCUSDT"), _perp("ETHUSDT")]
        matched, _skipped = self._run(
            [page1, page2], symbols, top_n=50, candidate_pages=2, per_page=1
        )
        self.assertEqual({m["symbol"] for m in matched}, {"BTC", "ETH"})


class TestLoadBinanceForScan(unittest.TestCase):
    def test_delegates_to_load_binance_daily_ignoring_exchange_hint(self):
        import crypto_screener

        fake_series = object()
        with patch("crypto_screener.load_binance_daily", return_value=fake_series) as mock_load:
            result = crypto_screener._load_binance_for_scan("BTCUSDT", exchange_hint="кто угодно")
        mock_load.assert_called_once_with("BTCUSDT", market="futures")
        self.assertIs(result, fake_series)


class TestNewLocalGridEvents(unittest.TestCase):
    def _point(self, day, price, kind):
        return ExtremePoint(dt=date(2020, 1, 1) + timedelta(days=day), price=price, kind=kind, index=day)

    def _local(self, seq, state, point1_price=100.0, point2_price=50.0):
        p1 = self._point(0, point1_price, "HIGH")
        p2 = self._point(seq, point2_price, "LOW")
        return LocalGrid(
            seq=seq, direction=Direction.DOWN, point1=p1, state=state,
            launch_date=p1.dt, point2_preliminary=p2,
            confirmed_date=p2.dt if state != LocalState.FORMING else None,
            point2_final=p2 if state != LocalState.FORMING else None,
        )

    @dataclass
    class _FakeChain:
        completed: list
        current: object

    def test_first_run_reports_everything_seen(self):
        import crypto_screener

        chain = self._FakeChain(completed=[self._local(1, LocalState.EXITED)], current=self._local(2, LocalState.FORMING))
        state: dict = {}
        new_events = crypto_screener._new_local_grid_events("TEST", chain, state)
        self.assertEqual([e.seq for e in new_events], [1, 2])
        self.assertEqual(state["TEST"].last_seq, 2)
        self.assertEqual(state["TEST"].last_state, LocalState.FORMING.value)

    def test_no_repeat_when_nothing_changed(self):
        import crypto_screener

        chain = self._FakeChain(completed=[], current=self._local(1, LocalState.FORMING))
        state: dict = {}
        crypto_screener._new_local_grid_events("TEST", chain, state)
        new_events = crypto_screener._new_local_grid_events("TEST", chain, state)
        self.assertEqual(new_events, [])

    def test_state_progression_within_same_seq_is_reported(self):
        import crypto_screener

        state: dict = {}
        forming_chain = self._FakeChain(completed=[], current=self._local(1, LocalState.FORMING))
        crypto_screener._new_local_grid_events("TEST", forming_chain, state)

        fixed_chain = self._FakeChain(completed=[], current=self._local(1, LocalState.FIXED))
        new_events = crypto_screener._new_local_grid_events("TEST", fixed_chain, state)
        self.assertEqual(len(new_events), 1)
        self.assertEqual(new_events[0].state, LocalState.FIXED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
