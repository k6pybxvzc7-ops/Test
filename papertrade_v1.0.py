#!/usr/bin/env python3
"""
papertrade v1.0 - paper trade stocks and measure 1-2 day swing trade performance.

Two workflows:
  1. Paper Account (forward): open/close simulated positions at live or stated
     prices, persisted to a JSON ledger, with a performance report.
  2. Swing Backtest (historical replay): "what if I had bought on day D and sold
     N trading days later", across a date range, with aggregate statistics.

Domain language (see README_v1.0.md for the full glossary):
  Account, Position, Fill, RoundTrip, CostModel, PriceSource, Bar,
  HoldingPeriod, EntryPoint/ExitPoint (open|close), PerformanceReport.

Stdlib only. Price data: Yahoo Finance v8 chart endpoint (unofficial, may change)
or a local CSV file (Date,Open,High,Low,Close[,Volume]).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone

VERSION = "1.0"
DEFAULT_STATE = "papertrade_state.json"


# --------------------------------------------------------------------------- #
# Market data                                                                  #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bar:
    """One daily OHLCV bar."""
    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class PriceSourceError(RuntimeError):
    pass


class PriceSource:
    def bars(self, ticker: str, start: date, end: date) -> list[Bar]:
        raise NotImplementedError

    def last_price(self, ticker: str) -> tuple[float, datetime]:
        raise NotImplementedError


class YahooPriceSource(PriceSource):
    """Yahoo Finance v8 chart endpoint. Unofficial/undocumented; personal use."""
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"

    def _get(self, ticker: str, params: dict) -> dict:
        url = self.URL.format(sym=urllib.parse.quote(ticker.upper())) + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (papertrade)"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.load(resp)
        except Exception as exc:  # surface the real error to the operator
            raise PriceSourceError(f"Yahoo request failed for {ticker}: {exc}") from exc
        chart = payload.get("chart", {})
        if chart.get("error"):
            raise PriceSourceError(f"Yahoo error for {ticker}: {chart['error']}")
        results = chart.get("result") or []
        if not results:
            raise PriceSourceError(f"Yahoo returned no data for {ticker}")
        return results[0]

    def bars(self, ticker: str, start: date, end: date) -> list[Bar]:
        p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
        p2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400
        res = self._get(ticker, {"period1": p1, "period2": p2, "interval": "1d", "events": "div,splits"})
        ts = res.get("timestamp") or []
        q = (res.get("indicators", {}).get("quote") or [{}])[0]
        gmtoff = res.get("meta", {}).get("gmtoffset", 0) or 0
        out = []
        for i, t in enumerate(ts):
            vals = [q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close")]
            if any(v is None for v in vals):
                continue  # Yahoo sometimes emits null rows (halts, partial days)
            d = datetime.fromtimestamp(t + gmtoff, tz=timezone.utc).date()
            vol = (q.get("volume") or [0] * len(ts))[i] or 0
            out.append(Bar(d, *map(float, vals), float(vol)))
        return [b for b in out if start <= b.day <= end]

    def last_price(self, ticker: str) -> tuple[float, datetime]:
        res = self._get(ticker, {"range": "1d", "interval": "1m"})
        meta = res.get("meta", {})
        price = meta.get("regularMarketPrice")
        if price is None:
            raise PriceSourceError(f"No regularMarketPrice for {ticker}")
        t = meta.get("regularMarketTime")
        when = datetime.fromtimestamp(t, tz=timezone.utc) if t else datetime.now(timezone.utc)
        return float(price), when


class CsvPriceSource(PriceSource):
    """Daily bars from a CSV with headers Date,Open,High,Low,Close[,Volume]."""

    def __init__(self, path: str):
        self.path = path
        self._bars: list[Bar] = []
        with open(path, newline="") as fh:
            reader = csv.DictReader(fh)
            cols = {c.lower().strip(): c for c in (reader.fieldnames or [])}
            need = ["date", "open", "high", "low", "close"]
            missing = [c for c in need if c not in cols]
            if missing:
                raise PriceSourceError(f"{path}: missing columns {missing}; found {reader.fieldnames}")
            for row in reader:
                try:
                    self._bars.append(Bar(
                        date.fromisoformat(row[cols["date"]].strip()[:10]),
                        float(row[cols["open"]]), float(row[cols["high"]]),
                        float(row[cols["low"]]), float(row[cols["close"]]),
                        float(row[cols["volume"]]) if "volume" in cols and row[cols["volume"]] else 0.0,
                    ))
                except (ValueError, KeyError):
                    continue  # skip malformed rows (e.g. "null")
        self._bars.sort(key=lambda b: b.day)
        if not self._bars:
            raise PriceSourceError(f"{path}: no usable rows")

    def bars(self, ticker: str, start: date, end: date) -> list[Bar]:
        return [b for b in self._bars if start <= b.day <= end]

    def last_price(self, ticker: str) -> tuple[float, datetime]:
        b = self._bars[-1]
        return b.close, datetime(b.day.year, b.day.month, b.day.day, 21, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Trading domain                                                               #
# --------------------------------------------------------------------------- #
@dataclass
class CostModel:
    """Commission per fill (flat $) and slippage in basis points against you."""
    commission: float = 0.0
    slippage_bps: float = 0.0

    def buy_price(self, quote: float) -> float:
        return quote * (1 + self.slippage_bps / 10_000)

    def sell_price(self, quote: float) -> float:
        return quote * (1 - self.slippage_bps / 10_000)


@dataclass
class Position:
    ticker: str
    shares: float
    avg_price: float          # average fill price incl. slippage
    entry_time: str           # ISO timestamp of first fill
    entry_fees: float = 0.0   # commissions paid on entry fills (not yet realized)


@dataclass
class RoundTrip:
    """A closed trade: entry fill(s) + exit fill."""
    ticker: str
    shares: float
    entry_price: float
    entry_time: str
    exit_price: float
    exit_time: str
    fees: float

    @property
    def cost_basis(self) -> float:
        return self.shares * self.entry_price

    @property
    def pnl(self) -> float:
        return self.shares * (self.exit_price - self.entry_price) - self.fees

    @property
    def return_pct(self) -> float:
        return 100 * self.pnl / self.cost_basis if self.cost_basis else 0.0

    @property
    def holding_days(self) -> float:
        a, b = datetime.fromisoformat(self.entry_time), datetime.fromisoformat(self.exit_time)
        return (b - a).total_seconds() / 86400


class TradeRejected(ValueError):
    pass


@dataclass
class Account:
    starting_cash: float
    cash: float
    costs: CostModel = field(default_factory=CostModel)
    positions: dict[str, Position] = field(default_factory=dict)
    closed: list[RoundTrip] = field(default_factory=list)
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def buy(self, ticker: str, shares: float, quote: float, when: datetime) -> Position:
        ticker = ticker.upper()
        if shares <= 0:
            raise TradeRejected("shares must be > 0")
        px = self.costs.buy_price(quote)
        total = shares * px + self.costs.commission
        if total > self.cash + 1e-9:
            raise TradeRejected(f"insufficient cash: need ${total:,.2f}, have ${self.cash:,.2f}")
        self.cash -= total
        pos = self.positions.get(ticker)
        if pos:
            new_sh = pos.shares + shares
            pos.avg_price = (pos.shares * pos.avg_price + shares * px) / new_sh
            pos.shares = new_sh
            pos.entry_fees += self.costs.commission
        else:
            pos = Position(ticker, shares, px, when.isoformat(timespec="seconds"), self.costs.commission)
            self.positions[ticker] = pos
        return pos

    def sell(self, ticker: str, shares: float | None, quote: float, when: datetime) -> RoundTrip:
        ticker = ticker.upper()
        pos = self.positions.get(ticker)
        if not pos:
            raise TradeRejected(f"no open position in {ticker}")
        shares = pos.shares if shares is None else shares
        if shares <= 0 or shares > pos.shares + 1e-9:
            raise TradeRejected(f"can sell between 0 and {pos.shares} shares of {ticker}")
        exit_px = self.costs.sell_price(quote)
        frac = shares / pos.shares
        entry_fees = pos.entry_fees * frac
        self.cash += shares * exit_px - self.costs.commission
        rt = RoundTrip(ticker, shares, pos.avg_price, pos.entry_time, exit_px,
                       when.isoformat(timespec="seconds"), entry_fees + self.costs.commission)
        self.closed.append(rt)
        pos.shares -= shares
        pos.entry_fees -= entry_fees
        if pos.shares <= 1e-9:
            del self.positions[ticker]
        return rt

    # persistence -------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "version": VERSION,
            "created": self.created,
            "starting_cash": self.starting_cash,
            "cash": self.cash,
            "costs": asdict(self.costs),
            "positions": {k: asdict(v) for k, v in self.positions.items()},
            "closed": [asdict(t) for t in self.closed],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        return cls(
            starting_cash=d["starting_cash"], cash=d["cash"], costs=CostModel(**d.get("costs", {})),
            positions={k: Position(**v) for k, v in d.get("positions", {}).items()},
            closed=[RoundTrip(**t) for t in d.get("closed", [])], created=d.get("created", ""),
        )


class AccountRepository:
    def __init__(self, path: str):
        self.path = path

    def exists(self) -> bool:
        return os.path.exists(self.path)

    def load(self) -> Account:
        if not self.exists():
            raise SystemExit(f"No account at {self.path}. Run: init --cash 10000")
        with open(self.path) as fh:
            return Account.from_dict(json.load(fh))

    def save(self, acct: Account) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(acct.to_dict(), fh, indent=2)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Performance                                                                  #
# --------------------------------------------------------------------------- #
def max_drawdown_pct(equity: list[float]) -> float:
    peak, mdd = float("-inf"), 0.0
    for v in equity:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, (v - peak) / peak)
    return 100 * mdd


def performance_report(trades: list[RoundTrip]) -> dict:
    if not trades:
        return {"trades": 0}
    rets = [t.return_pct for t in trades]
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_loss = -sum(losses)
    return {
        "trades": len(trades),
        "win_rate_pct": 100 * len(wins) / len(trades),
        "total_pnl": sum(pnls),
        "avg_return_pct": statistics.fmean(rets),
        "median_return_pct": statistics.median(rets),
        "stdev_return_pct": statistics.stdev(rets) if len(rets) > 1 else 0.0,
        "best_pct": max(rets),
        "worst_pct": min(rets),
        "avg_win": statistics.fmean(wins) if wins else 0.0,
        "avg_loss": statistics.fmean(losses) if losses else 0.0,
        "profit_factor": (sum(wins) / gross_loss) if gross_loss else float("inf"),
        "avg_holding_days": statistics.fmean(t.holding_days for t in trades),
    }


def print_report(rep: dict, title: str) -> None:
    print(f"\n== {title} ==")
    if not rep.get("trades"):
        print("  no closed trades")
        return
    fmt = {
        "trades": "{:.0f}", "win_rate_pct": "{:.1f}%", "total_pnl": "${:,.2f}",
        "avg_return_pct": "{:+.3f}%", "median_return_pct": "{:+.3f}%", "stdev_return_pct": "{:.3f}%",
        "best_pct": "{:+.2f}%", "worst_pct": "{:+.2f}%", "avg_win": "${:,.2f}", "avg_loss": "${:,.2f}",
        "profit_factor": "{:.2f}", "avg_holding_days": "{:.2f}",
    }
    for k, f in fmt.items():
        if k in rep:
            print(f"  {k:<20} {f.format(rep[k])}")
    for k, v in rep.items():
        if k not in fmt:
            print(f"  {k:<20} {v:+.2f}%" if isinstance(v, float) else f"  {k:<20} {v}")


# --------------------------------------------------------------------------- #
# Swing backtest                                                               #
# --------------------------------------------------------------------------- #
def _stamp(d: date, point: str) -> datetime:
    # nominal US session times in UTC, used only for holding-period math
    hh, mm = (14, 30) if point == "open" else (21, 0)
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)


def swing_backtest(ticker: str, bars: list[Bar], hold: int, entry_at: str, exit_at: str,
                   capital: float, costs: CostModel, entry_days: set[date] | None = None,
                   step: int = 1) -> list[RoundTrip]:
    """Buy on bar i at entry_at, sell on bar i+hold at exit_at. One trade per entry day.
    step=1 -> a trade every day (overlapping samples); step=hold -> back-to-back, non-overlapping."""
    if hold < 1 and not (hold == 0 and entry_at == "open" and exit_at == "close"):
        raise ValueError("hold must be >= 1 trading day (hold 0 allowed only for open->close)")
    trades: list[RoundTrip] = []
    i = 0
    while i + hold < len(bars):
        b_in, b_out = bars[i], bars[i + hold]
        if entry_days is None or b_in.day in entry_days:
            px_in = costs.buy_price(getattr(b_in, entry_at))
            px_out = costs.sell_price(getattr(b_out, exit_at))
            shares = (capital - costs.commission) / px_in
            trades.append(RoundTrip(ticker, shares, px_in, _stamp(b_in.day, entry_at).isoformat(),
                                    px_out, _stamp(b_out.day, exit_at).isoformat(), 2 * costs.commission))
            i += step
        else:
            i += 1
    return trades


def sequential_equity(trades: list[RoundTrip], capital: float) -> list[float]:
    """Compound non-overlapping trades in order (each trade reinvests full equity)."""
    eq, curve, last_exit = capital, [capital], None
    for t in sorted(trades, key=lambda t: t.entry_time):
        if last_exit and t.entry_time < last_exit:
            continue
        eq *= 1 + t.return_pct / 100
        curve.append(eq)
        last_exit = t.exit_time
    return curve


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def make_source(args) -> PriceSource:
    return CsvPriceSource(args.csv) if getattr(args, "csv", None) else YahooPriceSource()


def resolve_quote(src: PriceSource, ticker: str, price: float | None, on: str | None,
                  at: str) -> tuple[float, datetime, str]:
    """Price priority: explicit --price > historical bar on --date > live quote."""
    if on:
        d = date.fromisoformat(on)
        when = _stamp(d, at)
        if price is not None:
            return price, when, "manual"
        bars = src.bars(ticker, d, d)
        if not bars:
            raise PriceSourceError(f"no {ticker} bar on {d} (weekend/holiday?)")
        return getattr(bars[0], at), when, f"{at} of {d}"
    if price is not None:
        return price, datetime.now(timezone.utc), "manual"
    px, when = src.last_price(ticker)
    return px, when, "live"


def cmd_init(args, repo: AccountRepository):
    if repo.exists() and not args.force:
        raise SystemExit(f"{repo.path} already exists. Use --force to overwrite, or --state for a new file.")
    acct = Account(args.cash, args.cash, CostModel(args.commission, args.slippage_bps))
    repo.save(acct)
    print(f"Created account at {repo.path}: cash ${args.cash:,.2f}, commission ${args.commission}/fill, "
          f"slippage {args.slippage_bps} bps")


def cmd_quote(args, repo):
    px, when = make_source(args).last_price(args.ticker)
    print(f"{args.ticker.upper()} {px:,.4f}  (as of {when.isoformat(timespec='minutes')})")


def cmd_buy(args, repo: AccountRepository):
    acct = repo.load()
    px, when, how = resolve_quote(make_source(args), args.ticker, args.price, args.date, args.at)
    if args.shares is None and args.dollars is None:
        raise SystemExit("give --shares or --dollars")
    shares = args.shares if args.shares is not None else (args.dollars - acct.costs.commission) / acct.costs.buy_price(px)
    if args.whole:
        shares = float(int(shares))
    pos = acct.buy(args.ticker, shares, px, when)
    repo.save(acct)
    print(f"BUY  {shares:g} {pos.ticker} @ {acct.costs.buy_price(px):,.4f} ({how} quote {px:,.4f}) | "
          f"cash left ${acct.cash:,.2f}")


def cmd_sell(args, repo: AccountRepository):
    acct = repo.load()
    px, when, how = resolve_quote(make_source(args), args.ticker, args.price, args.date, args.at)
    rt = acct.sell(args.ticker, args.shares, px, when)
    repo.save(acct)
    print(f"SELL {rt.shares:g} {rt.ticker} @ {rt.exit_price:,.4f} ({how} quote {px:,.4f}) | "
          f"P&L ${rt.pnl:+,.2f} ({rt.return_pct:+.2f}%) held {rt.holding_days:.2f} days | cash ${acct.cash:,.2f}")


def cmd_positions(args, repo: AccountRepository):
    acct = repo.load()
    src = make_source(args)
    mkt_value = 0.0
    print(f"{'TICKER':<8}{'SHARES':>10}{'AVG':>12}{'LAST':>12}{'UNRL P&L':>14}{'%':>9}{'DAYS':>7}")
    for p in acct.positions.values():
        try:
            last, _ = src.last_price(p.ticker)
        except PriceSourceError as exc:
            print(f"{p.ticker:<8}{p.shares:>10g}{p.avg_price:>12,.4f}   price unavailable: {exc}")
            mkt_value += p.shares * p.avg_price
            continue
        exit_px = acct.costs.sell_price(last)
        unrl = p.shares * (exit_px - p.avg_price) - p.entry_fees - acct.costs.commission
        days = (datetime.now(timezone.utc) - datetime.fromisoformat(p.entry_time)).total_seconds() / 86400
        mkt_value += p.shares * last
        print(f"{p.ticker:<8}{p.shares:>10g}{p.avg_price:>12,.4f}{last:>12,.4f}{unrl:>+14,.2f}"
              f"{100 * unrl / (p.shares * p.avg_price):>+8.2f}%{days:>7.2f}")
    equity = acct.cash + mkt_value
    print(f"\ncash ${acct.cash:,.2f} | equity ${equity:,.2f} | "
          f"total return {100 * (equity / acct.starting_cash - 1):+.2f}% vs start ${acct.starting_cash:,.2f}")


def cmd_history(args, repo: AccountRepository):
    acct = repo.load()
    print(f"{'TICKER':<8}{'SHARES':>10}{'ENTRY':>12}{'EXIT':>12}{'P&L':>12}{'%':>9}{'DAYS':>7}  OPENED")
    for t in acct.closed:
        print(f"{t.ticker:<8}{t.shares:>10g}{t.entry_price:>12,.4f}{t.exit_price:>12,.4f}{t.pnl:>+12,.2f}"
              f"{t.return_pct:>+8.2f}%{t.holding_days:>7.2f}  {t.entry_time[:16]}")


def cmd_report(args, repo: AccountRepository):
    acct = repo.load()
    rep = performance_report(acct.closed)
    swings = [t for t in acct.closed if t.holding_days <= args.max_days]
    print_report(rep, "All closed trades")
    print_report(performance_report(swings), f"Swing trades (held <= {args.max_days} days)")
    print(f"\nrealized equity ${acct.cash + sum(p.shares * p.avg_price + p.entry_fees for p in acct.positions.values()):,.2f}"
          f" (open positions at cost) vs start ${acct.starting_cash:,.2f}")


def cmd_backtest(args, repo):
    src = make_source(args)
    end = date.fromisoformat(args.end) if args.end else date.today()
    start = date.fromisoformat(args.start) if args.start else end - timedelta(days=365)
    holds = [int(h) for h in str(args.hold).split(",")]
    # fetch a little past `end` so trades entered near `end` can still exit
    bars = src.bars(args.ticker, start, end + timedelta(days=10 + 2 * max(holds)))
    if len(bars) < 2:
        raise SystemExit(f"not enough bars for {args.ticker} between {start} and {end}")
    costs = CostModel(args.commission, args.slippage_bps)
    entry_days = {date.fromisoformat(d) for d in args.dates.split(",")} if args.dates else None
    entry_bars = [b for b in bars if b.day <= end]
    print(f"{args.ticker.upper()} {entry_bars[0].day} -> {entry_bars[-1].day}: {len(entry_bars)} trading days | "
          f"entry {args.entry_at} -> exit {args.exit_at} | ${args.capital:,.0f}/trade | "
          f"commission ${costs.commission}/fill, slippage {costs.slippage_bps} bps")
    bh = 100 * (entry_bars[-1].close / entry_bars[0].open - 1)
    print(f"buy & hold over period (first open -> last close): {bh:+.2f}%")
    for h in holds:
        trades = swing_backtest(args.ticker.upper(), bars, h, args.entry_at, args.exit_at, args.capital,
                                costs, entry_days, step=h if args.non_overlapping else 1)
        trades = [t for t in trades if date.fromisoformat(t.entry_time[:10]) <= end]
        rep = performance_report(trades)
        if trades:
            curve = sequential_equity(trades, args.capital)
            rep["compounded_return_pct"] = 100 * (curve[-1] / args.capital - 1)
            rep["max_drawdown_pct"] = max_drawdown_pct(curve)
        print_report(rep, f"hold {h} trading day(s)")
        if args.trades:
            for t in trades:
                print(f"    {t.entry_time[:10]} {t.entry_price:>10,.2f} -> {t.exit_time[:10]} "
                      f"{t.exit_price:>10,.2f}  {t.return_pct:+7.2f}%  ${t.pnl:+,.2f}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="papertrade", description=f"Paper trading & swing backtests (v{VERSION})")
    p.add_argument("--state", default=os.environ.get("PAPERTRADE_STATE", DEFAULT_STATE), help="account JSON file")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_src(sp):
        sp.add_argument("--csv", help="use this CSV (Date,Open,High,Low,Close) instead of Yahoo")
        return sp

    s = sub.add_parser("init", help="create a paper account")
    s.add_argument("--cash", type=float, default=10_000)
    s.add_argument("--commission", type=float, default=0.0, help="$ per fill")
    s.add_argument("--slippage-bps", type=float, default=5.0, help="adverse slippage per fill, basis points")
    s.add_argument("--force", action="store_true")

    with_src(sub.add_parser("quote", help="latest price")).add_argument("ticker")

    for name in ("buy", "sell"):
        s = with_src(sub.add_parser(name, help=f"{name} at live, historical (--date) or manual (--price) price"))
        s.add_argument("ticker")
        s.add_argument("--shares", type=float, help="default for sell: whole position")
        if name == "buy":
            s.add_argument("--dollars", type=float, help="spend this many dollars instead of --shares")
            s.add_argument("--whole", action="store_true", help="round shares down to whole number")
        s.add_argument("--price", type=float, help="fill at this quote instead of fetching")
        s.add_argument("--date", help="YYYY-MM-DD: fill at that day's bar (backfill a trade)")
        s.add_argument("--at", choices=["open", "close"], default="close", help="which bar price with --date")

    with_src(sub.add_parser("positions", help="open positions marked to market"))
    sub.add_parser("history", help="closed round trips")
    s = sub.add_parser("report", help="performance report of closed trades")
    s.add_argument("--max-days", type=float, default=3.0, help="swing filter: max holding days")

    s = with_src(sub.add_parser("backtest", help="replay 'buy, hold N days, sell' over history"))
    s.add_argument("ticker")
    s.add_argument("--start", help="YYYY-MM-DD (default: 1 year before end)")
    s.add_argument("--end", help="YYYY-MM-DD last entry day (default: today)")
    s.add_argument("--hold", default="1,2", help="trading days to hold, comma list (default 1,2)")
    s.add_argument("--entry-at", choices=["open", "close"], default="close")
    s.add_argument("--exit-at", choices=["open", "close"], default="close")
    s.add_argument("--dates", help="only enter on these YYYY-MM-DD dates (comma list)")
    s.add_argument("--capital", type=float, default=10_000, help="$ per trade")
    s.add_argument("--commission", type=float, default=0.0)
    s.add_argument("--slippage-bps", type=float, default=5.0)
    s.add_argument("--non-overlapping", action="store_true", help="step by hold length instead of daily")
    s.add_argument("--trades", action="store_true", help="list every trade")
    return p


COMMANDS = {"init": cmd_init, "quote": cmd_quote, "buy": cmd_buy, "sell": cmd_sell, "positions": cmd_positions,
            "history": cmd_history, "report": cmd_report, "backtest": cmd_backtest}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        COMMANDS[args.cmd](args, AccountRepository(args.state))
    except (TradeRejected, PriceSourceError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
