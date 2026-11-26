#!/usr/bin/env python3
"""
blind_multi v1.0 - run several papertrade blind sessions in lockstep.

One blind session per ticker, all advanced together one trading day at a time, so the
trader never knows more about one instrument's future than about another's. Reads
run_config.json from its own directory:

  {"tickers": ["AAA", "BBB"], "from": "YYYY-MM-DD", "end": "YYYY-MM-DD",
   "cash": 10000, "slippage_bps": 5, "bars": 40,
   "program": "papertrade_v1.3.py", "csv": {"AAA": "aaa.csv"}}      (csv optional)

Commands:
  start                    open all sessions and show each ticker's history
  order TICKER <args...>   place one decision (same arguments as `papertrade blind order`)
  next                     reveal the next trading day for ALL tickers; refused unless every
                           ticker has a decision for the current day, and after the end date
  view TICKER [args...]    show bars up to the horizon for one ticker
  status                   one line per ticker
  report                   each ticker's report plus a combined summary
"""
import json
import os
import subprocess
import sys

VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))


def config() -> dict:
    with open(os.path.join(HERE, "run_config.json")) as fh:
        cfg = json.load(fh)
    cfg["tickers"] = [t.upper() for t in cfg["tickers"]]
    return cfg


def sess_path(t: str) -> str:
    return os.path.join(HERE, f"{t}_session.json")


def load(t: str) -> dict:
    with open(sess_path(t)) as fh:
        return json.load(fh)


def pt(cfg: dict, t: str, *args: str) -> int:
    prog = os.path.join(HERE, cfg.get("program", "papertrade_v1.3.py"))
    return subprocess.run([sys.executable, prog, "blind", "--session", sess_path(t), *args], cwd=HERE).returncode


def fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 1


def need_ticker(cfg: dict, argv: list[str]) -> str | None:
    if not argv or argv[0].upper() not in cfg["tickers"]:
        fail(f"give one of {cfg['tickers']}")
        return None
    return argv[0].upper()


def horizons(cfg: dict) -> dict:
    return {t: load(t)["horizon"] for t in cfg["tickers"]}


def equity(s: dict) -> tuple[float, float | None]:
    """(equity after exit costs, last close)"""
    acct, t = s["account"], s["ticker"]
    last = s["revealed"][-1]["close"] if s["revealed"] else None
    pos = acct["positions"].get(t)
    if not pos:
        return acct["cash"], last
    px = last if last is not None else pos["avg_price"]
    liq = pos["shares"] * px * (1 - acct["costs"]["slippage_bps"] / 10_000) - acct["costs"]["commission"]
    return acct["cash"] + liq, last


def cmd_start(cfg: dict, argv: list[str]) -> int:
    existing = [t for t in cfg["tickers"] if os.path.exists(sess_path(t))]
    if existing:
        return fail(f"already started: {existing}")
    for t in cfg["tickers"]:
        print(f"\n{'=' * 30} {t} {'=' * 30}")
        args = ["start", t, "--from", cfg["from"], "--cash", str(cfg["cash"]),
                "--slippage-bps", str(cfg["slippage_bps"]), "--bars", str(cfg.get("bars", 40))]
        if cfg.get("csv", {}).get(t):
            args += ["--csv", cfg["csv"][t]]
        if pt(cfg, t, *args):
            return fail(f"start failed for {t}")
    print(f"\nAll sessions open. Trading window: {cfg['from']} -> {cfg['end']}. "
          f"Place one decision per ticker, then run: next")
    return 0


def cmd_order(cfg: dict, argv: list[str]) -> int:
    t = need_ticker(cfg, argv)
    if not t:
        return 1
    if load(t)["horizon"] >= cfg["end"]:
        return fail(f"the window ended on {cfg['end']}; no further orders")
    return pt(cfg, t, "order", *argv[1:])


def cmd_next(cfg: dict, argv: list[str]) -> int:
    hz = horizons(cfg)
    if len(set(hz.values())) != 1:
        return fail(f"sessions are not on the same day: {hz}")
    h = next(iter(hz.values()))
    if h >= cfg["end"]:
        return fail(f"the window ended on {cfg['end']}; run: report")
    missing = [t for t in cfg["tickers"]
               if not any(j["horizon"] == h and j["event"].startswith("decide") for j in load(t)["journal"])]
    if missing:
        return fail(f"no decision yet at close of {h} for: {missing} (use order TICKER hold|buy|sell ...)")
    for t in cfg["tickers"]:
        print(f"\n{'=' * 30} {t} {'=' * 30}")
        if pt(cfg, t, "next"):
            return fail(f"next failed for {t}; sessions may now be on different days: {horizons(cfg)}")
    hz = horizons(cfg)
    if len(set(hz.values())) != 1:
        return fail(f"trading calendars diverged: {hz}")
    h = next(iter(hz.values()))
    print(f"\nAll tickers now at close of {h}." +
          (" Window complete: place no more orders, run: report" if h >= cfg["end"] else ""))
    return 0


def cmd_view(cfg: dict, argv: list[str]) -> int:
    t = need_ticker(cfg, argv)
    return pt(cfg, t, "view", *argv[1:]) if t else 1


def cmd_status(cfg: dict, argv: list[str]) -> int:
    print(f"{'TICKER':<7}{'HORIZON':<12}{'CLOSE':>10}{'EQUITY':>12}{'RET%':>8}  POSITION / STOP / PENDING")
    for t in cfg["tickers"]:
        s = load(t)
        eq, last = equity(s)
        pos = s["account"]["positions"].get(t)
        ptxt = f"{pos['shares']:g} sh @ {pos['avg_price']:,.2f}, bar {s.get('bars_held', 0)}" if pos else "flat"
        pend = s["pending"]
        pend_txt = (f"{pend['action']}" + (f" limit {pend['limit']}" if pend.get("limit") is not None else "")) \
            if pend else "none"
        print(f"{t:<7}{s['horizon']:<12}{(f'{last:,.2f}' if last is not None else '-'):>10}{eq:>12,.2f}"
              f"{100 * (eq / s['account']['starting_cash'] - 1):>+8.2f}  {ptxt} / stop {s['stop']} / pending {pend_txt}")
    return 0


def cmd_report(cfg: dict, argv: list[str]) -> int:
    for t in cfg["tickers"]:
        print(f"\n{'=' * 30} {t} {'=' * 30}")
        pt(cfg, t, "report")
    print(f"\n{'=' * 30} COMBINED {'=' * 30}")
    print(f"{'TICKER':<7}{'EQUITY':>12}{'RET%':>8}{'TRADES':>8}{'WINS':>6}{'OPEN':>6}{'BUY&HOLD%':>11}")
    tot_start = tot_eq = 0.0
    bhs = []
    for t in cfg["tickers"]:
        s = load(t)
        acct = s["account"]
        eq, last = equity(s)
        closed = acct["closed"]
        wins = sum(1 for c in closed if c["shares"] * (c["exit_price"] - c["entry_price"]) - c["fees"] > 0)
        bh = 100 * (s["revealed"][-1]["close"] / s["revealed"][0]["open"] - 1) if s["revealed"] else 0.0
        bhs.append(bh)
        tot_start += acct["starting_cash"]
        tot_eq += eq
        print(f"{t:<7}{eq:>12,.2f}{100 * (eq / acct['starting_cash'] - 1):>+8.2f}{len(closed):>8}{wins:>6}"
              f"{('yes' if acct['positions'] else 'no'):>6}{bh:>+11.2f}")
    print(f"{'TOTAL':<7}{tot_eq:>12,.2f}{100 * (tot_eq / tot_start - 1):>+8.2f}{'':>20}"
          f"{sum(bhs) / len(bhs):>+11.2f}  (equal-weight buy & hold)")
    print("equity is after exit costs for any open position")
    return 0


COMMANDS = {"start": cmd_start, "order": cmd_order, "next": cmd_next, "view": cmd_view,
            "status": cmd_status, "report": cmd_report}


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in COMMANDS:
        print(__doc__)
        return 1
    return COMMANDS[argv[0]](config(), argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
