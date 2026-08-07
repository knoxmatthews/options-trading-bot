# SuperTrend-at-Open Options Bot

At market open, checks SuperTrend(factor=1.75, ATR=10) on SPY. Uptrend ->
buys the at-the-money call. Downtrend -> buys the at-the-money put. Exits on
a SuperTrend flip against the position, or end of day (0DTE-style) —
whichever comes first.

## What's actually verified vs. not

**Verified against the real alpaca-py library** (installed and inspected
directly, not guessed from memory or docs alone):
- Every request/response class field name and type used here (`GetOptionContractsRequest`,
  `MarketOrderRequest`, `Position`, `OptionContract`, `Clock`, etc.)
- The `TimeFrame(amount, unit)` constructor — this is exactly what broke
  curveball-bot before, so it got extra attention here.
- `strike_price_gte`/`strike_price_lte` take **strings**, not floats — an
  easy mistake this avoided by checking instead of assuming.
- The SuperTrend calculation — sanity-tested against synthetic up/down/mixed
  price series before being trusted (see the test output from build time:
  correctly detects uptrend, downtrend, and flips).
- The whole file imports and runs its module-level setup cleanly with dummy
  credentials (syntax + wiring is sound).

**NOT verified — no live Alpaca account was available to test against:**
- Whether `get_option_contracts` actually returns 0DTE SPY contracts the way
  expected in a real market — the *shape* of the call is confirmed correct,
  the *live response* isn't.
- Real order fills and timing. Orders are submitted, not confirmed filled,
  before the bot logs success.
- Whatever Railway-specific quirks show up on first deploy.

Treat this the way curveball-bot and iron-condor-bot both went: a solid
first version that will need a round of real-world debugging, not a
finished, fire-and-forget bot.

## Setup

1. `pip install -r requirements.txt`
2. Set environment variables (Railway → your service → Variables):
   - `ALPACA_API_KEY`, `ALPACA_API_SECRET` — same names your other bots use
   - `ALPACA_PAPER=true` while testing
   - Everything else in `main.py`'s docstring is optional, with sane defaults
3. Deploy to Railway from this repo. **Important:** in the Railway
   dashboard, make sure this service is set up as a worker/background
   process, not a web service — it doesn't listen on a port. If Railway's
   default health check expects an HTTP port and the deploy fails for that
   reason, either disable the health check for this service or add a tiny
   HTTP endpoint as a workaround (ask me and I'll add one).

## Known limitations (see main.py docstring for the full list)

- "Already traded today" lives in memory, not a real database — a mid-day
  Railway restart could lose it. The bot still can't double-enter (it
  checks for an existing position first), but it could skip a trade after
  restarting post-close. Worth hardening with real persistence once this
  is confirmed running.
- Fallback to a later expiration if 0DTE isn't listed — double check this
  is actually the behavior you want before running it unattended.
