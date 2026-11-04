# Levels & Ledger

A claude.ai artifact with two tabs:

- **Levels**: daily candles for QQQ, SPY and GLD with floor-pivot levels (Pivot, R1/R2, S1/S2), prior-day high/low and the expected move, plus mechanical if-then option setups priced with Black-Scholes estimates. These are rule-based setups, not recommendations.
- **Journal**: a private trade journal with deposits/withdrawals, logged trades, balance and P&L charts, a weekly summary and a Claude trade coach that reviews execution.

Live artifact: https://claude.ai/artifact/WZV5Am29mEcoJXXMBDnUBb

## How it works

`index.html` is published as an artifact and uses the claude.ai runtime (`db`, `user`, `sample` capabilities), so it only works inside claude.ai, not as a plain static page.

Market data comes from `fetch_market.py`, which pulls 90 daily candles from Yahoo Finance's chart API and writes `market.json`. That file is loaded into the artifact's database as `market/today`; the page computes every level from those candles. The script also replays the last finished session's setups on Yahoo's 5-minute bars (entry, target/stop/close exit, estimated option P&L) and stores the results as `replay`, which the page shows under "How the last session's setups played out". It runs the same simulation over every session Yahoo keeps 5-minute bars for (about 60) as `backtest`: overall stats, breakdowns by grade/setup/side/ticker, a running P&L line and one-rule-at-a-time variants compared on the earlier two-thirds vs. the later third of sessions. A weekday scheduled task in the Claude desktop app runs this before the open.

```bash
python fetch_market.py market.json
```

The journal is stored per user under `data/users/<id>/ledger` and is visible only to the person who wrote it.
