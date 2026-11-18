# Levels & Ledger

A claude.ai artifact with two tabs:

- **Levels**: daily candles for QQQ, SPY and GLD with floor-pivot levels (Pivot, R1/R2, S1/S2), prior-day high/low and the expected move, plus mechanical if-then option setups priced with Black-Scholes estimates. These are rule-based setups, not recommendations.
- **Journal**: a private trade journal with deposits/withdrawals, logged trades, balance and P&L charts, a weekly summary and a Claude trade coach that reviews execution.

Live artifact: https://claude.ai/artifact/WZV5Am29mEcoJXXMBDnUBb

## How it works

`index.html` is published as an artifact and uses the claude.ai runtime (`db`, `user`, `sample` capabilities), so it only works inside claude.ai, not as a plain static page.

Market data comes from `fetch_market.py`, which pulls 90 daily candles from Yahoo Finance's chart API and writes `market.json`. That file is loaded into the artifact's database as `market/today`; the page computes every level from those candles. The script also replays the last finished session's setups on Yahoo's 5-minute bars (entry, target/stop/close exit, estimated option P&L; target and stop are measured from the entry price, so an entry that gaps or runs past its trigger is not left beyond its own target) and stores the results as `replay`, which the page shows under "How the last session's setups played out". It runs the same simulation over every session Yahoo keeps 5-minute bars for (about 60) as `backtest`: overall stats, breakdowns by grade/setup/side/ticker, a running P&L line and one-rule-at-a-time variants compared on the earlier two-thirds vs. the later third of sessions. A weekday scheduled task in the Claude desktop app runs this before the open.

Setups are graded A+ to D by how many of five checks they meet: with the 5-day trend, a calm market (20-day realized vol at or below 25%), a breakout/breakdown rather than a pivot setup, an open that gaps the trade's way, and an entry whose first 5-minute close is at least ¼ of the expected move past the trigger (5 = A+, 4 = A, 3 = B, 2 = C, 0–1 = D). Each check improved results in both the earlier and later halves of the November 2026 backtest; the previous four rules didn't separate winners. The page shows the grade confirmed so far and the best grade still possible, since the gap is only known at the open and the push at the trigger. The backtest compares "A or A+ only" and "A+ only" against the current rules on every run.

Each setup card also has a "Confirm before entry" checklist: trigger-candle volume at least 1.5× normal for that time of day, price on the trade's side of the session VWAP, 5-minute RSI(14) on the trade's side of 50, 5-minute 20 EMA beyond the 50 EMA with price beyond the 20 EMA, and daily ADX(14) of 25 or more. The page computes ADX from the daily candles; the others are checked on the chart at the trigger. The script measures all five at every simulated entry (`backtest.confirmations`), so each item shows whether it actually improved results, and the rule-change table tests requiring 3 or 4 of them.

Research tickers (`RESEARCH` in `fetch_market.py`, currently IWM, TLT and XLF) are simulated and graded on every run but not shown as cards. The backtest's "Research tickers" table shows their record overall and for grade A/A+ setups in the earlier vs. later sessions, as an out-of-sample check on the grade. Move a ticker into `TICKERS` (and the page's `TICKERS`) once its record holds up. A research ticker that fails to download is skipped without affecting the rest.

Every simulated trade pays estimated costs (half the bid/ask spread on entry and exit plus a $0.65 commission per side), so backtest results aren't flattered. The backtest also stores each session's full replay (`backtest.history`, packed as arrays) so the page can show any day, and each setup's record per ticker (`backtest.bySetupTicker`), which the setup cards show next to the grade.

The script stamps `scriptVersion` into the data. When it is older than the version `index.html` expects, the page shows a warning that the replay and backtest came from an outdated copy, so the scheduled task should always download the latest `fetch_market.py` from this repo before it runs:

```bash
curl -sSL https://raw.githubusercontent.com/jhughes281/levels-ledger/main/fetch_market.py -o fetch_market.py
```

When you change the simulation rules, bump `SCRIPT_VERSION` in `fetch_market.py` and `SCRIPT_VERSION` in `index.html` together.

```bash
python fetch_market.py market.json
```

The journal is stored per user under `data/users/<id>/ledger` and is visible only to the person who wrote it.
