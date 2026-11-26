"""Tests for papertrade_v1.0.py. Run: python3 -m unittest test_papertrade_v1.0 (or python3 test_papertrade_v1.0.py)"""
import importlib.util
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("papertrade", os.path.join(_HERE, "papertrade_v1.0.py"))
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


if __name__ == "__main__":
    unittest.main()
