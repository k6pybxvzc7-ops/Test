"""Tests for papertrade_v1.3.py. Run: python3 -m unittest test_papertrade_v1.3 (or python3 test_papertrade_v1.3.py)"""
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("papertrade", os.path.join(_HERE, "papertrade_v1.3.py"))
pt = importlib.util.module_from_spec(_spec)
sys.modules["papertrade"] = pt  # dataclasses resolve annotations via sys.modules
_spec.loader.exec_module(pt)

CSV = """Date,Open,High,Low,Close,Volume
2026-11-02,100,101,99,100,1000
2026-11-03,100,103,99,102,1000
2026-11-04,102,104,100,101,1000
2026-11-05,101,106,100,105,1000
2026-11-06,105,105,98,99,1000
2026-11-09,99,100,97,null,1000
2026-11-10,99,101,98,100,1000
"""
T0 = datetime(2026, 11, 2, 21, tzinfo=timezone.utc)
T1 = datetime(2026, 11, 3, 21, tzinfo=timezone.utc)


class AccountTests(unittest.TestCase):
    def test_round_trip_no_costs(self):
        a = pt.Account(10_000, 10_000)
        a.buy("aapl", 10, 100, T0)
        rt = a.sell("AAPL", None, 110, T1)
        self.assertAlmostEqual(rt.pnl, 100)
        self.assertAlmostEqual(rt.return_pct, 10)
        self.assertAlmostEqual(rt.holding_days, 1)
        self.assertAlmostEqual(a.cash, 10_100)
        self.assertEqual(a.positions, {})

    def test_costs_applied_both_sides(self):
        a = pt.Account(10_000, 10_000, pt.CostModel(commission=1, slippage_bps=100))
        a.buy("X", 10, 100, T0)            # fill 101, cost 1010 + 1
        self.assertAlmostEqual(a.cash, 10_000 - 1011)
        rt = a.sell("X", None, 100, T1)    # fill 99, proceeds 990 - 1
        self.assertAlmostEqual(rt.pnl, 990 - 1010 - 2)
        self.assertAlmostEqual(a.cash, 10_000 - 1011 + 989)

    def test_partial_sell_and_averaging(self):
        a = pt.Account(10_000, 10_000, pt.CostModel(commission=2))
        a.buy("X", 10, 100, T0)
        a.buy("X", 10, 110, T0)
        self.assertAlmostEqual(a.positions["X"].avg_price, 105)
        rt = a.sell("X", 5, 120, T1)
        self.assertAlmostEqual(rt.fees, 4 * 5 / 20 + 2)   # share of entry fees + exit fee
        self.assertAlmostEqual(a.positions["X"].shares, 15)

    def test_rejections(self):
        a = pt.Account(1_000, 1_000)
        with self.assertRaises(pt.TradeRejected):
            a.buy("X", 11, 100, T0)
        with self.assertRaises(pt.TradeRejected):
            a.sell("X", None, 100, T0)
        a.buy("X", 1, 100, T0)
        with self.assertRaises(pt.TradeRejected):
            a.sell("X", 2, 100, T0)

    def test_persistence_roundtrip(self):
        a = pt.Account(5_000, 5_000, pt.CostModel(1, 5))
        a.buy("X", 3, 50, T0)
        a.buy("Y", 2, 20, T0)
        a.sell("Y", None, 25, T1)
        with tempfile.TemporaryDirectory() as d:
            repo = pt.AccountRepository(os.path.join(d, "s.json"))
            repo.save(a)
            b = repo.load()
        self.assertEqual(a.to_dict(), b.to_dict())


class DataAndBacktestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = os.path.join(self.tmp.name, "x.csv")
        with open(self.csv, "w") as fh:
            fh.write(CSV)

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv_skips_null_rows(self):
        bars = pt.CsvPriceSource(self.csv).bars("X", date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(len(bars), 6)
        self.assertNotIn(date(2026, 11, 9), [b.day for b in bars])

    def test_backtest_hold1_close_to_close(self):
        bars = pt.CsvPriceSource(self.csv).bars("X", date(2026, 1, 1), date(2026, 12, 31))
        trades = pt.swing_backtest("X", bars, 1, "close", "close", 1000, pt.CostModel())
        closes = [b.close for b in bars]
        expected = [100 * (closes[i + 1] / closes[i] - 1) for i in range(len(closes) - 1)]
        self.assertEqual(len(trades), 5)
        for t, e in zip(trades, expected):
            self.assertAlmostEqual(t.return_pct, e)

    def test_backtest_hold2_nonoverlapping_and_dates(self):
        bars = pt.CsvPriceSource(self.csv).bars("X", date(2026, 1, 1), date(2026, 12, 31))
        t = pt.swing_backtest("X", bars, 2, "open", "close", 1000, pt.CostModel(), step=2)
        self.assertEqual([x.entry_time[:10] for x in t], ["2026-11-02", "2026-11-04"])
        self.assertAlmostEqual(t[0].return_pct, 1.0)            # open 100 -> close 101
        only = pt.swing_backtest("X", bars, 1, "close", "close", 1000, pt.CostModel(), {date(2026, 11, 4)})
        self.assertEqual(len(only), 1)
        self.assertAlmostEqual(only[0].return_pct, 100 * (105 / 101 - 1))

    def test_drawdown_and_compounding(self):
        self.assertAlmostEqual(pt.max_drawdown_pct([100, 120, 90, 130]), -25)
        bars = pt.CsvPriceSource(self.csv).bars("X", date(2026, 1, 1), date(2026, 12, 31))
        trades = pt.swing_backtest("X", bars, 1, "close", "close", 1000, pt.CostModel())
        curve = pt.sequential_equity(trades, 1000)
        self.assertAlmostEqual(curve[-1], 1000 * bars[-1].close / bars[0].close)

    def test_cli_end_to_end(self):
        state = os.path.join(self.tmp.name, "acct.json")
        run = lambda *a: pt.main(["--state", state, *a])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(run("init", "--cash", "10000", "--slippage-bps", "0"), 0)
            self.assertEqual(run("buy", "X", "--dollars", "1000", "--date", "2026-11-03", "--csv", self.csv), 0)
            self.assertEqual(run("sell", "X", "--date", "2026-11-05", "--csv", self.csv), 0)
            self.assertEqual(run("report"), 0)
            self.assertEqual(run("backtest", "X", "--csv", self.csv, "--start", "2026-11-01",
                                 "--end", "2026-11-10", "--hold", "1,2"), 0)
        acct = pt.AccountRepository(state).load()
        self.assertAlmostEqual(acct.closed[0].return_pct, 100 * (105 / 102 - 1))
        self.assertAlmostEqual(acct.closed[0].holding_days, 2)
        self.assertIn("hold 2 trading day(s)", out.getvalue())
        # weekend date surfaces a clear error
        self.assertEqual(run("buy", "X", "--shares", "1", "--date", "2026-11-07", "--csv", self.csv), 1)


class SpySource(pt.PriceSource):
    """Records the latest date ever requested from the underlying data."""
    def __init__(self, inner):
        self.inner, self.max_end, self.calls = inner, None, []

    def bars(self, ticker, start, end):
        self.calls.append(end)
        self.max_end = max(self.max_end or end, end)
        return self.inner.bars(ticker, start, end)

    def last_price(self, ticker):
        raise AssertionError("blind mode must never ask for a live quote")


class BlindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.csv = os.path.join(self.tmp.name, "x.csv")
        with open(self.csv, "w") as fh:
            fh.write(CSV)
        self.sess = os.path.join(self.tmp.name, "blind.json")
        self.spy = SpySource(pt.CsvPriceSource(self.csv))
        self._orig = (pt._blind_src, pt.market_now)
        pt._blind_src = lambda s: self.spy
        pt.market_now = lambda: datetime(2026, 11, 26, 12, 0)

    def tearDown(self):
        pt._blind_src, pt.market_now = self._orig
        self.tmp.cleanup()

    def run_(self, *a):
        with redirect_stdout(io.StringIO()) as out:
            rc = pt.main(["blind", "--session", self.sess, *a])
        return rc, out.getvalue()

    def horizon(self):
        return pt.BlindRepository(self.sess).load().horizon

    def test_guard_rejects_future_requests_and_quotes(self):
        g = pt.HorizonGuardedSource(pt.CsvPriceSource(self.csv), date(2026, 11, 3))
        with self.assertRaises(pt.LookAheadViolation):
            g.bars("X", date(2026, 11, 1), date(2026, 11, 4))
        with self.assertRaises(pt.LookAheadViolation):
            g.last_price("X")
        self.assertEqual([b.day for b in g.bars("X", date(2026, 11, 1), date(2026, 11, 3))][-1], date(2026, 11, 3))

    def test_guard_catches_leaky_source(self):
        class Leaky(pt.PriceSource):
            def bars(self, t, s, e):
                return pt.CsvPriceSource(CSV_PATH).bars(t, s, date(2026, 12, 31))
        global CSV_PATH
        CSV_PATH = self.csv
        with self.assertRaises(pt.LookAheadViolation):
            pt.HorizonGuardedSource(Leaky(), date(2026, 11, 3)).bars("X", date(2026, 11, 1), date(2026, 11, 3))

    def test_walk_forward_never_requests_past_horizon(self):
        rc, out = self.run_("start", "X", "--from", "2026-11-03", "--slippage-bps", "0")
        self.assertEqual(rc, 0)
        self.assertEqual(self.horizon(), "2026-11-02")
        self.assertNotIn("2026-11-03", out)                       # nothing past horizon printed
        self.assertLessEqual(self.spy.max_end, date(2026, 11, 2))
        self.assertEqual(self.run_("order", "buy", "--dollars", "1020", "--why", "test")[0], 0)
        rc, out = self.run_("next")
        self.assertEqual(self.horizon(), "2026-11-03")
        self.assertLessEqual(self.spy.max_end, date(2026, 11, 3))
        acct = pt.BlindRepository(self.sess).load().account
        self.assertAlmostEqual(acct.positions["X"].avg_price, 100.0)   # filled at 11-03 OPEN, not close
        self.assertAlmostEqual(acct.positions["X"].shares, 10.2)
        self.run_("order", "sell", "--why", "test")
        self.run_("next")
        self.assertEqual(self.horizon(), "2026-11-04")
        acct = pt.BlindRepository(self.sess).load().account
        self.assertAlmostEqual(acct.closed[0].exit_price, 102.0)        # 11-04 open
        self.assertLessEqual(self.spy.max_end, date(2026, 11, 4))

    def test_skips_missing_days_and_stops_at_present(self):
        self.run_("start", "X", "--from", "2026-11-06")
        for expected in ("2026-11-06", "2026-11-10"):                  # weekend + null row skipped
            self.assertEqual(self.run_("order", "hold", "--why", "t")[0], 0)
            self.run_("next")
            self.assertEqual(self.horizon(), expected)
        rc, _ = self.run_("next")
        self.assertEqual(rc, 1)                                         # no more data
        self.assertLessEqual(self.spy.max_end, date(2026, 11, 25))      # never asked for today

    def test_stop_loss_and_gap(self):
        self.run_("start", "X", "--from", "2026-11-05", "--slippage-bps", "0")
        self.run_("order", "buy", "--shares", "10", "--stop", "100", "--why", "t")
        self.run_("next")   # 11-05: O101 L100 -> bought 101, low touches 100 -> stopped at 100
        acct = pt.BlindRepository(self.sess).load().account
        self.assertAlmostEqual(acct.closed[0].exit_price, 100.0)
        self.run_("order", "buy", "--shares", "10", "--stop", "104", "--why", "t")
        self.run_("next")   # 11-06: O105 L98 -> bought 105, stop 104 -> filled at 104
        self.assertAlmostEqual(pt.BlindRepository(self.sess).load().account.closed[1].exit_price, 104.0)

    def test_same_day_reveal_only_after_settle(self):
        csv2 = os.path.join(self.tmp.name, "y.csv")
        with open(csv2, "w") as fh:
            fh.write("Date,Open,High,Low,Close\n2026-11-24,10,11,9,10\n2026-11-25,10,12,9,11\n")
        self.spy = SpySource(pt.CsvPriceSource(csv2))
        pt.market_now = lambda: datetime(2026, 11, 25, 15, 59)
        self.assertEqual(self.run_("start", "X", "--from", "2026-11-24")[0], 0)
        self.run_("next")
        self.assertEqual(self.horizon(), "2026-11-24")
        self.assertEqual(self.run_("next")[0], 1)                       # before settle: refused
        self.assertLessEqual(self.spy.max_end, date(2026, 11, 24))
        pt.market_now = lambda: datetime(2026, 11, 25, 16, 15)
        self.assertEqual(self.run_("next")[0], 0)                       # after settle: revealed
        self.assertEqual(self.horizon(), "2026-11-25")
        self.assertEqual(self.run_("next")[0], 1)                       # never beyond today

    def test_latest_revealable_day(self):
        self.assertEqual(pt.latest_revealable_day(datetime(2026, 11, 25, 16, 14)), date(2026, 11, 24))
        self.assertEqual(pt.latest_revealable_day(datetime(2026, 11, 25, 16, 15)), date(2026, 11, 25))


def _sess(cash=10_000, slippage=0.0, **kw):
    return pt.BlindSession("X", "2026-01-01", "2026-01-01", pt.Account(cash, cash, pt.CostModel(0, slippage)), **kw)


def _bar(o, h, l, c, d=date(2026, 1, 2)):
    return pt.Bar(d, o, h, l, c, 1000)


def _order(action, **kw):
    return {"action": action, "shares": None, "dollars": None, "risk_pct": None, "whole": False,
            "limit": None, "at": "open", **kw}


class OrderTypeTests(unittest.TestCase):
    def test_buy_limit_skips_gap_up(self):
        # regression for the 2026-11-23 SPCX fill: decided at close 127.79, next bar opened 140.04, low 133.10
        s = _sess(pending=_order("buy", dollars=3000, limit=128.0), stop=122.0)
        ev = pt.process_bar(s, _bar(140.04, 141.87, 133.10, 133.35))
        self.assertEqual(s.account.positions, {})
        self.assertIn("expired", ev[0])
        self.assertIsNone(s.pending)
        self.assertIsNone(s.stop)                                   # no position -> stop cleared

    def test_buy_limit_fills_at_limit_or_better(self):
        s = _sess(pending=_order("buy", shares=10, limit=100.0))
        pt.process_bar(s, _bar(104, 105, 99, 103))                  # trades down through the limit
        self.assertAlmostEqual(s.account.positions["X"].avg_price, 100.0)
        s = _sess(pending=_order("buy", shares=10, limit=100.0))
        pt.process_bar(s, _bar(97, 105, 96, 103))                   # opens below the limit -> open price
        self.assertAlmostEqual(s.account.positions["X"].avg_price, 97.0)

    def test_buy_limit_then_stop_same_bar(self):
        s = _sess(pending=_order("buy", shares=10, limit=100.0), stop=95.0)
        pt.process_bar(s, _bar(104, 105, 94, 96))
        rt = s.account.closed[0]
        self.assertAlmostEqual(rt.entry_price, 100.0)
        self.assertAlmostEqual(rt.exit_price, 95.0)
        self.assertEqual(s.bars_held, 0)

    def test_sell_limit_and_stop_priority(self):
        def held(**kw):
            s = _sess(**kw)
            s.account.buy("X", 10, 100, T0)
            s.bars_held = 1
            return s
        s = held(pending=_order("sell", limit=110.0), stop=95.0)
        pt.process_bar(s, _bar(101, 111, 99, 105))                  # target reached
        self.assertAlmostEqual(s.account.closed[0].exit_price, 110.0)
        s = held(pending=_order("sell", limit=110.0), stop=95.0)
        pt.process_bar(s, _bar(112, 113, 99, 105))                  # gap above the limit -> open
        self.assertAlmostEqual(s.account.closed[0].exit_price, 112.0)
        s = held(pending=_order("sell", limit=110.0), stop=95.0)
        ev = pt.process_bar(s, _bar(101, 111, 94, 105))             # both in range -> stop assumed first
        self.assertAlmostEqual(s.account.closed[0].exit_price, 95.0)
        self.assertTrue(any("void" in e for e in ev))
        s = held(pending=_order("sell", limit=110.0), stop=95.0)
        ev = pt.process_bar(s, _bar(101, 105, 99, 104))             # neither -> order expires, still long
        self.assertIn("X", s.account.positions)
        self.assertEqual(s.bars_held, 2)

    def test_market_at_close(self):
        s = _sess(pending=_order("buy", shares=10, at="close"))
        pt.process_bar(s, _bar(100, 105, 99, 103))
        self.assertAlmostEqual(s.account.positions["X"].avg_price, 103.0)
        s.pending = _order("sell", at="close")
        pt.process_bar(s, _bar(103, 108, 102, 107, date(2026, 1, 5)))
        self.assertAlmostEqual(s.account.closed[0].exit_price, 107.0)

    def test_risk_sizing_uses_actual_fill(self):
        s = _sess(pending=_order("buy", risk_pct=1.0), stop=95.0)
        pt.process_bar(s, _bar(100, 101, 99, 100))                  # risk $100 / $5 per share
        self.assertAlmostEqual(s.account.positions["X"].shares, 20.0)
        s = _sess(pending=_order("buy", risk_pct=1.0), stop=95.0)
        pt.process_bar(s, _bar(105, 106, 104, 105))                 # gap up: $10 per share -> half the size
        self.assertAlmostEqual(s.account.positions["X"].shares, 10.0)
        s = _sess(pending=_order("buy", risk_pct=1.0, dollars=500), stop=95.0)
        pt.process_bar(s, _bar(100, 101, 99, 100))                  # capped by --dollars
        self.assertAlmostEqual(s.account.positions["X"].shares, 5.0)
        s = _sess(cash=1000, pending=_order("buy", risk_pct=5.0), stop=99.0)
        pt.process_bar(s, _bar(100, 101, 99.5, 100))                # capped by cash
        self.assertAlmostEqual(s.account.positions["X"].shares, 10.0)
        s = _sess(pending=_order("buy", risk_pct=1.0), stop=95.0)
        ev = pt.process_bar(s, _bar(94, 96, 93, 95))                # opens below the stop -> rejected
        self.assertEqual(s.account.positions, {})
        self.assertIn("rejected", ev[0])

    def test_max_hold_auto_exit(self):
        s = _sess(pending=_order("buy", shares=10), max_hold=2)
        days = [date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6)]
        pt.process_bar(s, _bar(100, 102, 99, 101, days[0]))
        pt.process_bar(s, _bar(101, 104, 100, 103, days[1]))
        self.assertEqual(s.bars_held, 2)
        self.assertIn("X", s.account.positions)
        ev = pt.process_bar(s, _bar(105, 106, 104, 105, days[2]))
        self.assertAlmostEqual(s.account.closed[0].exit_price, 105.0)
        self.assertIn("max-hold", ev[0])
        self.assertIsNone(s.max_hold)

    def test_rejected_add_still_checks_stop(self):
        s = _sess(cash=1000, stop=95.0)
        s.account.buy("X", 10, 100, T0)
        s.bars_held = 1
        s.pending = _order("buy", shares=50)                        # cannot afford
        ev = pt.process_bar(s, _bar(100, 101, 94, 96))
        self.assertTrue(any("rejected" in e for e in ev))
        self.assertAlmostEqual(s.account.closed[0].exit_price, 95.0)

    def test_loads_v1_2_session_format(self):
        s = _sess()
        s.account.buy("X", 10, 100, datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc))
        s.revealed = [{"day": d, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}
                      for d in ("2026-01-02", "2026-01-05", "2026-01-06")]
        d = s.to_dict()
        del d["max_hold"], d["bars_held"]
        s2 = pt.BlindSession.from_dict(d)
        self.assertEqual((s2.bars_held, s2.max_hold), (2, None))

    def test_avg_true_range_includes_gaps(self):
        bars = [_bar(100, 101, 99, 100), _bar(110, 111, 109, 110, date(2026, 1, 5))]
        self.assertAlmostEqual(pt.avg_true_range(bars), 11.0)       # high 111 - prev close 100

    def test_cli_validation_and_limit_flow(self):
        tmp = tempfile.TemporaryDirectory()
        csv, sess = os.path.join(tmp.name, "x.csv"), os.path.join(tmp.name, "b.json")
        with open(csv, "w") as fh:
            fh.write(CSV)
        orig = pt.market_now
        pt.market_now = lambda: datetime(2026, 11, 26, 12, 0)
        try:
            run = lambda *a: pt.main(["blind", "--session", sess, *a])
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(run("start", "X", "--from", "2026-11-05", "--slippage-bps", "0", "--csv", csv), 0)
                self.assertEqual(run("order", "buy", "--risk-pct", "1", "--why", "t"), 1)           # no stop
                self.assertEqual(run("order", "buy", "--shares", "1", "--limit", "99", "--stop", "99.5",
                                     "--why", "t"), 1)                                             # stop >= limit
                self.assertEqual(run("order", "buy", "--shares", "1", "--limit", "99", "--at", "close",
                                     "--why", "t"), 1)
                self.assertEqual(run("order", "buy", "--risk-pct", "1", "--limit", "100.5", "--stop", "98.5",
                                     "--max-hold", "1", "--why", "t"), 0)
                self.assertEqual(run("next"), 0)        # 11-05: O101 L100 -> filled 100.5, 50 sh
                self.assertEqual(run("next"), 0)        # 11-06: max-hold 1 -> sold at open 105
                self.assertEqual(run("report"), 0)
            acct = pt.BlindRepository(sess).load().account
            self.assertAlmostEqual(acct.closed[0].shares, 50.0)
            self.assertAlmostEqual(acct.closed[0].entry_price, 100.5)
            self.assertAlmostEqual(acct.closed[0].exit_price, 105.0)
            self.assertIn("= 0.50x the average daily range ($4.00)", out.getvalue())
        finally:
            pt.market_now = orig
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
