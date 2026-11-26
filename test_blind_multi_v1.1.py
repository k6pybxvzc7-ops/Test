"""Tests for blind_multi_v1.1.py. Run: python3 test_blind_multi_v1.1.py"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = """Date,Open,High,Low,Close,Volume
2026-11-02,100,101,99,100,1000
2026-11-03,100,103,99,102,1000
2026-11-04,102,104,100,101,1000
2026-11-05,101,106,100,105,1000
2026-11-06,105,105,98,99,1000
2026-11-10,99,101,98,100,1000
"""


class LockstepTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        for f in ("blind_multi_v1.1.py", "papertrade_v1.3.py"):
            shutil.copy(os.path.join(HERE, f), self.d)
        for t in ("AAA", "BBB"):
            with open(os.path.join(self.d, f"{t}.csv"), "w") as fh:
                fh.write(CSV)
        self.write_cfg({})

    def tearDown(self):
        shutil.rmtree(self.d)

    def write_cfg(self, extra):
        cfg = {"tickers": ["AAA", "BBB"], "from": "2026-11-04", "end": "2026-11-06", "cash": 10000,
               "slippage_bps": 0, "bars": 5, "csv": {"AAA": "AAA.csv", "BBB": "BBB.csv"}, **extra}
        with open(os.path.join(self.d, "run_config.json"), "w") as fh:
            json.dump(cfg, fh)

    def run_(self, *a):
        r = subprocess.run([sys.executable, os.path.join(self.d, "blind_multi_v1.1.py"), *a],
                           capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr

    def horizon(self, t):
        with open(os.path.join(self.d, f"{t}_session.json")) as fh:
            return json.load(fh)["horizon"]

    def test_full_run(self):
        for c in (("next",), ("status",), ("report",), ("order", "AAA", "hold", "--why", "t")):
            rc, out = self.run_(*c)                                           # before start: clean error
            self.assertEqual(rc, 1)
            self.assertIn("run: start", out)
            self.assertNotIn("Traceback", out)
        rc, out = self.run_("start")
        self.assertEqual(rc, 0, out)
        self.assertNotIn("2026-11-04", out.split("All sessions open")[0])     # nothing past the horizon shown
        self.assertEqual(self.run_("start")[0], 1)                            # cannot start twice
        rc, out = self.run_("next")                                          # no decisions yet
        self.assertEqual(rc, 1)
        self.assertIn("AAA", out)
        self.assertEqual(self.run_("order", "AAA", "buy", "--shares", "10", "--why", "t")[0], 0)
        rc, out = self.run_("next")                                          # BBB still undecided
        self.assertEqual(rc, 1)
        self.assertIn("['BBB']", out)
        self.assertEqual(self.horizon("AAA"), "2026-11-03")                  # nothing was revealed
        self.assertEqual(self.run_("order", "ZZZ", "hold", "--why", "t")[0], 1)
        self.assertEqual(self.run_("order", "BBB", "hold", "--why", "t")[0], 0)
        self.assertEqual(self.run_("next")[0], 0)
        self.assertEqual((self.horizon("AAA"), self.horizon("BBB")), ("2026-11-04", "2026-11-04"))
        for _ in range(2):
            for t in ("AAA", "BBB"):
                self.assertEqual(self.run_("order", t, "hold", "--why", "t")[0], 0)
            rc, out = self.run_("next")
            self.assertEqual(rc, 0, out)
        self.assertIn("Window complete", out)
        self.assertEqual(self.horizon("AAA"), "2026-11-06")
        self.assertEqual(self.run_("order", "AAA", "sell", "--why", "t")[0], 1)   # window over
        self.assertEqual(self.run_("next")[0], 1)                                 # hard stop
        self.assertEqual(self.horizon("AAA"), "2026-11-06")
        rc, out = self.run_("report")
        self.assertEqual(rc, 0)
        self.assertIn("COMBINED", out)
        # AAA: 10 sh bought at the 11-04 open (102), marked at the 11-06 close (99) -> 9970; BBB flat
        self.assertIn("19,970.00", out)
        self.assertEqual(self.run_("status")[0], 0)

    def test_diverged_sessions_refused(self):
        self.run_("start")
        prog = os.path.join(self.d, "papertrade_v1.3.py")
        sess = os.path.join(self.d, "AAA_session.json")
        r = subprocess.run([sys.executable, prog, "blind", "--session", sess, "next"], capture_output=True, cwd=self.d)
        self.assertEqual(r.returncode, 0)
        rc, out = self.run_("next")
        self.assertEqual(rc, 1)
        self.assertIn("not on the same day", out)


if __name__ == "__main__":
    unittest.main()
