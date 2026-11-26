# papertrade v1.0

Paper trade stocks and measure how 1–2 day swing trades would have performed. Single Python file, standard library only (Python 3.10+).

## Two workflows

| Workflow | What it answers | Commands |
|---|---|---|
| **Paper Account** (forward) | "I'm buying now (or bought on date D). How am I doing?" | `init`, `buy`, `sell`, `positions`, `history`, `report` |
| **Swing Backtest** (historical replay) | "If I had bought every day (or on specific days) and sold 1 or 2 trading days later, what would the results be?" | `backtest` |

## Quick start

```bash
# Paper Account
python3 papertrade_v1.0.py init --cash 10000 --slippage-bps 5
python3 papertrade_v1.0.py buy AAPL --dollars 2000          # fills at live price
python3 papertrade_v1.0.py positions                        # marked to market
python3 papertrade_v1.0.py sell AAPL                        # closes whole position
python3 papertrade_v1.0.py report

# Backfill a trade you "would have" taken
python3 papertrade_v1.0.py buy NVDA --shares 10 --date 2026-11-20 --at close
python3 papertrade_v1.0.py sell NVDA --date 2026-11-24 --at close

# Swing Backtest: compare holding 1 vs 2 days over the last year
python3 papertrade_v1.0.py backtest AAPL --hold 1,2
python3 papertrade_v1.0.py backtest AAPL --start 2026-06-01 --entry-at open --exit-at close --hold 1 --trades
python3 papertrade_v1.0.py backtest AAPL --dates 2026-11-03,2026-11-10 --hold 2 --trades

# Offline / other data: any CSV with Date,Open,High,Low,Close[,Volume]
python3 papertrade_v1.0.py backtest MYSTOCK --csv mystock.csv
```

Use `--state other.json` (or the `PAPERTRADE_STATE` environment variable) to keep several accounts. Run `python3 papertrade_v1.0.py <command> -h` for every option.

Tests: `python3 test_papertrade_v1.0.py -v`

## Domain language (ubiquitous language)

| Term | Meaning in this program |
|---|---|
| **Account** | Paper cash plus open Positions plus closed RoundTrips. Persisted as JSON (the *ledger*). |
| **Position** | Open holding in one ticker: shares, average fill price, first entry time, entry fees not yet realized. A second buy averages in. |
| **Fill** | Execution at a *quote* adjusted by the CostModel (buy fills higher, sell fills lower). |
| **Quote source priority** | `--price` (manual) > `--date` bar (historical open/close) > live quote. |
| **RoundTrip** | A closed trade: entry price → exit price, with shares, fees, P&L, return %, and holding days. |
| **CostModel** | Flat commission per fill ($) plus adverse slippage in basis points (1 bp = 0.01%). |
| **HoldingPeriod** | Paper Account: calendar days between fills. Backtest: `--hold N` *trading* days (bars). |
| **EntryPoint / ExitPoint** | Which bar price is used, `open` or `close`. Default close→close; `--hold 0 --entry-at open --exit-at close` = intraday. |
| **Swing trade** | A RoundTrip held ≤ `--max-days` (default 3 calendar days) in `report`. |
| **PriceSource** | `YahooPriceSource` (default, network) or `CsvPriceSource` (`--csv`). |
| **PerformanceReport** | Trades, win rate, total P&L, avg/median/stdev return, best/worst, avg win/loss, profit factor (gross wins ÷ gross losses), avg holding days. |
| **Overlapping vs non-overlapping** | By default the backtest opens one trade every day (overlapping samples, more data points). `--non-overlapping` steps forward by the hold length. |
| **Compounded return / max drawdown** | Computed from the *non-overlapping* chain of trades, reinvesting full equity each time. |

## Limitations

- **Data source.** The Yahoo Finance v8 chart endpoint is unofficial and undocumented. It can change, throttle, or stop working without notice, and the data is meant for personal use. If it breaks, export prices to CSV and use `--csv`.
- Fills are idealized at the bar's open or close, adjusted only by slippage. Gaps, partial fills, spreads, short selling, margin, dividends, and taxes are not modeled. Long positions only.
- Backtest timestamps use nominal US session times (14:30 / 21:00 UTC) for holding-period math only.
- Past performance in a backtest does not predict future results. The many daily samples in one ticker are not independent.
