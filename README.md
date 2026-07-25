# Fastball Bot

Directional options buyer — the opposite of Iron Condor Bot. Where Iron Condor
**sells** premium and profits when SPY stays in a range, Fastball **buys**
calls and puts and profits when SPY makes a real move.

## How it decides

Reuses the exact same signal engine as Curveball Bot (proven, not new):

- EMA 12 crosses above EMA 26 + RSI > 50 + volume confirmed + MACD turning up
  and bullish → **buy a call**
- Mirror image (EMA cross down, RSI < 50, MACD turning down and bearish) →
  **buy a put**

No forced trades. If nothing lines up, it does nothing that cycle. A forced
options trade decays toward zero every day it's wrong — that's a different
risk than a forced stock trade, so this bot only fires on real confluence.

## What it buys

- **ATM strike** — closest to the current SPY price
- **3–10 days to expiration** — enough time for the move to develop without
  paying for a month of unused time value. 0DTE is deliberately excluded:
  that's why Iron Condor *sells* 0DTE instead of buying it — fast decay
  favors the seller, not the buyer.
- **One position at a time** — no stacking calls and puts

## Position sizing

Risk-based, not flat:

```
risk_dollars = equity × 4%
contracts    = risk_dollars ÷ (real premium × 100)
```

Capped at 6 contracts regardless of math. If a real quote can't be fetched,
the trade is **skipped entirely** that cycle — for buying, guessing a price
could oversize the position, so skipping is the only safe default.

## Exits

Checked every cycle, whichever hits first:

- **+50%** on premium paid → close, take the win
- **-35%** on premium paid → close, cut the loss
- **≤2 days to expiration** → force close regardless of P&L, avoids the
  worst of theta decay in the final days

## Setup

1. **New Alpaca paper account** — this bot assumes it has its own dedicated
   account. It treats every open option position it sees as its own; if you
   ever point it at an account another bot also trades, that logic gets
   confused.
2. Fund the paper account to **$5,000** (Alpaca dashboard → Reset Account)
3. Generate **paper** API keys from that account
4. In this repo → Settings → Secrets and variables → Actions, add:
   - `FASTBALL_API_KEY`
   - `FASTBALL_API_SECRET`
5. Push all files to a new GitHub repo (or a new folder in an existing one —
   just make sure the workflow file path is `.github/workflows/fastball.yml`)
6. Actions tab → **Fastball Bot** → **Run workflow** to test manually

Runs automatically every 10 minutes during market hours after that.

## On backtesting

Real talk: there's no way to pull historical SPY options data into a sandbox
to run a numeric backtest against this exact logic — no financial data API
access from where this was built. Every Alpaca API call in this code was
verified against Alpaca's own published documentation and example code
before being written, which is the realistic ceiling of "testing" possible
without a live account. The actual backtest is watching the paper logs for
a few weeks, same as the other two bots — that's more reliable than a
synthetic backtest anyway, since it uses real fills and real spreads instead
of assumptions.

## Honest expectations

Buying options is structurally a lower win-rate, higher payout game than
selling them (Iron Condor). Most individual option buys that are wrong
expire worthless — that's normal, not a sign the bot is broken. The edge (if
there is one) shows up over many trades from the size of wins vs losses, not
from every single trade working out.
