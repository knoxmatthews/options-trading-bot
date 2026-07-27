"""
Fastball Bot - Directional Options Buyer (Calls & Puts)
---------------------------------------------------------
The opposite of Iron Condor Bot: instead of SELLING premium and
profiting from SPY staying in a range, this bot BUYS calls and puts
and profits from SPY making a real directional move.

Strategy:
- Reuses the same proven EMA9/21 + RSI + MACD confluence signal as
  Curveball Bot (no reason to invent an unproven new signal for a
  directional bet)
- BUY signal -> buy a call.  SELL signal -> buy a put.
- Targets options 3-10 days to expiration (0DTE decays too fast for
  BUYING - that's why Iron Condor sells 0DTE instead of buying it)
- ATM strike (closest to current price) - best balance of cost vs
  sensitivity to the move
- One position at a time - no stacking calls and puts
- Risk-based contract sizing - position size comes from real premium
  quotes, not a flat guess
- NO forced trades - unlike Curveball, a bad forced entry here decays
  toward zero every day it's wrong, so this bot only fires on real
  signal confluence

IMPORTANT: This bot assumes it has its OWN DEDICATED Alpaca account,
separate from Curveball and Iron Condor. It treats every open option
position in the account as its own - if you ever point this at an
account another bot also trades, the position-tracking logic below
will get confused.

GitHub Actions secrets needed: FASTBALL_API_KEY, FASTBALL_API_SECRET
"""

import os
import time
import logging
import datetime
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOptionContractsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, ContractType, AssetClass, AssetStatus
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, OptionLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed, OptionsFeed

# ── CONFIG ───────────────────────────────────────────────────────────────────
API_KEY    = os.environ.get("FASTBALL_API_KEY", "")
API_SECRET = os.environ.get("FASTBALL_API_SECRET", "")
PAPER      = True

UNDERLYING = "SPY"

# Position sizing - risk-based, not flat
RISK_PCT       = 0.10   # 10% of equity in premium paid per trade
MAX_CONTRACTS  = 6
MAX_BUDGET_OVERSHOOT = 1.5  # allow 1 contract even if it's up to 1.5x the target
                             # risk budget, but skip entirely beyond that

# Expiration & strike selection
MIN_DTE          = 3    # avoid 0-2 DTE - decay too fast for BUYING options
MAX_DTE          = 10   # keep it to roughly a weekly - don't pay for a month
                         # of unused time value
STRIKE_RANGE_PCT = 0.02 # look within 2% of current price to find the ATM strike

# Exit rules
TAKE_PROFIT_PCT       = 0.50   # close at +50% gain on premium paid
STOP_LOSS_PCT         = -0.35  # close at -35% loss on premium paid
CLOSE_DAYS_BEFORE_DTE = 2      # force close once within 2 days of expiry,
                                # regardless of P&L - avoids the theta cliff

# Signal settings - identical to Curveball's proven Gainz Style Algo v2
EMA_FAST   = 9
EMA_SLOW   = 21
RSI_PERIOD = 14
CROSSOVER_LOOKBACK = 3   # catches crossovers missed between delayed GH Actions runs

ET = ZoneInfo("America/New_York")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("fastball")

# ── INDICATORS - same proven logic as Curveball ─────────────────────────────

def ema(s: pd.Series, p: int) -> pd.Series:
    return s.ewm(span=p, adjust=False).mean()

def rsi(s: pd.Series, p: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = (-d.clip(upper=0)).rolling(p).mean().replace(0, 0.0001)
    return 100 - 100 / (1 + g / l)

def macd(s: pd.Series, fast: int = 9, slow: int = 21, signal: int = 9):
    macd_line   = ema(s, fast) - ema(s, slow)
    signal_line = ema(macd_line, signal)
    histogram   = macd_line - signal_line
    return macd_line, signal_line, histogram

def get_signal(df: pd.DataFrame):
    """
    Same confluence as Curveball's Gainz Style Algo v2:
    - BUY:  EMA9 crosses above EMA21 within last N bars + RSI>50 +
            volume confirmed + MACD turning up and bullish
    - SELL: mirror image for bearish
    No 'force' fallback here - see module docstring for why.
    Returns (signal, reason)
    """
    if len(df) < EMA_SLOW + CROSSOVER_LOOKBACK + 15:
        return None, "not enough bars"

    close  = df["close"]
    volume = df["volume"]

    e9    = ema(close, EMA_FAST)
    e21    = ema(close, EMA_SLOW)
    r      = rsi(close, RSI_PERIOD)
    avgvol = volume.rolling(20).mean()
    macd_line, macd_signal, macd_hist = macd(close)

    last_rsi    = r.iloc[-1]
    last_vol    = volume.iloc[-1]
    last_avgvol = avgvol.iloc[-1]

    if pd.isna(last_avgvol) or pd.isna(macd_hist.iloc[-1]):
        return None, "indicator not ready"

    vol_ok = last_vol > last_avgvol

    macd_turning_up   = macd_hist.iloc[-1] > macd_hist.iloc[-2]
    macd_turning_down = macd_hist.iloc[-1] < macd_hist.iloc[-2]
    macd_bullish      = macd_line.iloc[-1] > macd_signal.iloc[-1]
    macd_bearish      = macd_line.iloc[-1] < macd_signal.iloc[-1]

    bull_cross = False
    bear_cross = False
    for i in range(1, CROSSOVER_LOOKBACK + 1):
        idx, cur = -(i + 1), -i
        if len(e9) > abs(idx):
            if e9.iloc[idx] <= e9.iloc[idx] and e9.iloc[cur] > e21.iloc[cur]:
                bull_cross = True
            if e9.iloc[idx] >= e21.iloc[idx] and e9.iloc[cur] < e21.iloc[cur]:
                bear_cross = True

    if bull_cross and last_rsi > 50 and vol_ok and macd_turning_up and macd_bullish:
        return "buy", f"EMA9 x above EMA21 | RSI {last_rsi:.0f} | MACD turning up | vol confirmed"

    if bear_cross and last_rsi < 50 and vol_ok and macd_turning_down and macd_bearish:
        return "sell", f"EMA9 x below EMA21 | RSI {last_rsi:.0f} | MACD turning down | vol confirmed"

    return None, "no signal"

# ── BOT ──────────────────────────────────────────────────────────────────────

class FastballBot:
    def __init__(self):
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Missing FASTBALL_API_KEY or FASTBALL_API_SECRET")
        self.trade  = TradingClient(API_KEY, API_SECRET, paper=PAPER)
        self.sdata  = StockHistoricalDataClient(API_KEY, API_SECRET)
        self.odata  = OptionHistoricalDataClient(API_KEY, API_SECRET)
        if not PAPER:
            raise RuntimeError("PAPER=False. Refusing to run against a live account.")
        acct = self.trade.get_account()
        log.info(f"Connected | Equity=${acct.equity} | BP=${acct.buying_power} | Positions={len(self.trade.get_all_positions())}")

    # ── market timing ────────────────────────────────────────────────────────

    def is_market_open(self) -> bool:
        n = datetime.datetime.now(ET)
        if n.weekday() >= 5:
            return False
        o = n.replace(hour=9,  minute=30, second=0, microsecond=0)
        c = n.replace(hour=16, minute=0,  second=0, microsecond=0)
        return o <= n <= c

    def past_open_buffer(self, minutes: int = 5) -> bool:
        n = datetime.datetime.now(ET)
        buffer_end = n.replace(hour=9, minute=30, second=0, microsecond=0) + timedelta(minutes=minutes)
        return n >= buffer_end

    def before_late_cutoff(self) -> bool:
        """No new entries in the last 15 min - fills get worse, no benefit to entering that late."""
        n = datetime.datetime.now(ET)
        cutoff = n.replace(hour=15, minute=45, second=0, microsecond=0)
        return n < cutoff

    # ── data ─────────────────────────────────────────────────────────────────

    def get_bars(self) -> pd.DataFrame | None:
        end   = datetime.datetime.now(timezone.utc)
        start = end - timedelta(days=4)
        try:
            req  = StockBarsRequest(
                symbol_or_symbols = UNDERLYING,
                timeframe         = TimeFrame(5, TimeFrameUnit.Minute),
                start             = start,
                end               = end,
                feed              = DataFeed.IEX,  # free accounts can't query SIP
            )
            bars = self.sdata.get_stock_bars(req).df
            return bars.reset_index() if not bars.empty else None
        except Exception as e:
            log.error(f"Bar fetch failed: {e}")
            return None

    def get_spy_price(self) -> float | None:
        df = self.get_bars()
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])

    def get_option_quote(self, symbol: str) -> float | None:
        """
        Real bid/ask mid price for one option contract.
        Returns None on failure - for BUYING, a failed quote must SKIP the
        trade, never guess. Guessing cheap would oversize the position;
        guessing expensive would just be made up. Skipping is the only safe
        default when we can't see the real price.
        """
        try:
            req   = OptionLatestQuoteRequest(symbol_or_symbols=symbol, feed=OptionsFeed.INDICATIVE)
            quote = self.odata.get_option_latest_quote(req)[symbol]
            bid, ask = float(quote.bid_price), float(quote.ask_price)
            if bid <= 0 or ask <= 0:
                return None
            return (bid + ask) / 2
        except Exception as e:
            log.warning(f"Could not get quote for {symbol}: {e}")
            return None

    def find_contract(self, side: str):
        """
        side: 'call' or 'put'
        Finds the ATM contract within MIN_DTE-MAX_DTE days out.
        Returns the option contract object or None.
        """
        price = self.get_spy_price()
        if price is None:
            log.warning("Could not get SPY price.")
            return None

        today = datetime.date.today()
        min_exp = today + timedelta(days=MIN_DTE)
        max_exp = today + timedelta(days=MAX_DTE)

        try:
            req = GetOptionContractsRequest(
                underlying_symbols = [UNDERLYING],
                status             = AssetStatus.ACTIVE,
                type               = ContractType.CALL if side == "call" else ContractType.PUT,
                strike_price_gte   = str(round(price * (1 - STRIKE_RANGE_PCT), 2)),
                strike_price_lte   = str(round(price * (1 + STRIKE_RANGE_PCT), 2)),
                expiration_date_gte = min_exp,
                expiration_date_lte = max_exp,
            )
            contracts = self.trade.get_option_contracts(req).option_contracts
        except Exception as e:
            log.error(f"Contract fetch failed: {e}")
            return None

        if not contracts:
            log.warning(f"No {side} contracts found in DTE/strike window.")
            return None

        # Pick the nearest expiration in the window (cheapest time value while
        # still respecting MIN_DTE), then the strike closest to current price
        contracts.sort(key=lambda c: c.expiration_date)
        nearest_exp = contracts[0].expiration_date
        same_exp    = [c for c in contracts if c.expiration_date == nearest_exp]
        best        = min(same_exp, key=lambda c: abs(float(c.strike_price) - price))

        dte = (best.expiration_date - today).days
        log.info(f"Selected {side.upper()} {best.symbol} | strike {best.strike_price} | {dte} DTE")
        return best

    # ── positions ────────────────────────────────────────────────────────────

    def get_open_option_positions(self) -> list:
        return [p for p in self.trade.get_all_positions() if p.asset_class == AssetClass.US_OPTION]

    def calc_contracts(self, premium_mid: float, equity: float) -> int:
        risk_dollars  = equity * RISK_PCT
        per_contract  = premium_mid * 100
        if per_contract <= 0:
            return 0
        contracts = int(risk_dollars / per_contract)
        if contracts < 1:
            # Allow exactly 1 contract if it's not drastically over budget,
            # otherwise skip the trade entirely rather than force an
            # oversized entry.
            if per_contract <= risk_dollars * MAX_BUDGET_OVERSHOOT:
                contracts = 1
            else:
                return 0
        return min(contracts, MAX_CONTRACTS)

    def place_trade(self, side: str, reason: str):
        contract = self.find_contract(side)
        if not contract:
            return

        premium = self.get_option_quote(contract.symbol)
        if premium is None:
            log.warning(f"Skipping trade — could not get a real quote for {contract.symbol}")
            return

        acct   = self.trade.get_account()
        equity = float(acct.equity)
        qty    = self.calc_contracts(premium, equity)

        if qty < 1:
            log.info(f"Skipping — premium ${premium*100:.0f}/contract too expensive for risk budget")
            return

        cost = premium * 100 * qty
        try:
            order = self.trade.submit_order(MarketOrderRequest(
                symbol        = contract.symbol,
                qty           = qty,
                side          = OrderSide.BUY,
                time_in_force = TimeInForce.DAY,
            ))
            log.info(
                f"✅ BUY {qty}x {contract.symbol} ({side.upper()}) | "
                f"~${cost:.0f} total premium | {reason} | id={order.id}"
            )
        except Exception as e:
            log.error(f"❌ Order failed: {e}")

    def monitor_exit(self):
        """Check the open position (if any) against TP, SL, and DTE cutoff."""
        positions = self.get_open_option_positions()
        if not positions:
            return

        for pos in positions:
            pct = float(pos.unrealized_plpc) * 100

            # Check days to expiration via the contract's own record - safer
            # than parsing the OCC symbol string ourselves.
            dte = None
            try:
                contract = self.trade.get_option_contract(pos.symbol)
                dte = (contract.expiration_date - datetime.date.today()).days
            except Exception as e:
                log.warning(f"Could not check expiration for {pos.symbol}: {e}")

            should_close = False
            reason       = ""

            if pct >= TAKE_PROFIT_PCT * 100:
                should_close = True
                reason = f"take profit +{pct:.0f}%"
            elif pct <= STOP_LOSS_PCT * 100:
                should_close = True
                reason = f"stop loss {pct:.0f}%"
            elif dte is not None and dte <= CLOSE_DAYS_BEFORE_DTE:
                should_close = True
                reason = f"{dte} DTE remaining - avoiding theta cliff"

            if should_close:
                try:
                    self.trade.close_position(pos.symbol)
                    log.info(f"🔒 CLOSED {pos.symbol} | {reason} | P&L {pct:.1f}%")
                except Exception as e:
                    log.error(f"Close failed {pos.symbol}: {e}")

    # ── main ─────────────────────────────────────────────────────────────────

    def scan_cycle(self):
        """One full cycle: check exits, then look for a new entry if flat."""
        now = datetime.datetime.now(ET)

        if not self.is_market_open():
            return "closed"

        if not self.past_open_buffer():
            return "open_buffer"

        # Always check exits first, regardless of anything else
        self.monitor_exit()

        # One position at a time - if already holding, don't open another
        if self.get_open_option_positions():
            return "holding"

        if not self.before_late_cutoff():
            return "late_cutoff"

        df = self.get_bars()
        if df is None:
            return "no_data"

        signal, reason = get_signal(df)
        log.info(f"Signal: {signal or 'none'} | {reason}")

        if signal == "buy":
            self.place_trade("call", reason)
        elif signal == "sell":
            self.place_trade("put", reason)

        return "scanned"

    def run_once(self):
        """Single cycle then exit - used for manual/cron-triggered runs."""
        now = datetime.datetime.now(ET)
        log.info(f"━━ Scan {now.strftime('%Y-%m-%d %H:%M ET')} ━━")
        status = self.scan_cycle()
        log.info(f"Cycle result: {status}")

    def run_forever(self):
        """
        Persistent loop for Railway (or any always-on host).
        Exits are checked every EXIT_CHECK_SECONDS - much faster reaction
        than a cron-triggered bot can offer, since a bad move against an
        open position doesn't have to wait for the next scheduled trigger.
        New-entry signal scanning runs on a slower cadence since it's tied
        to 5-min bar data that doesn't change meaningfully faster than that.
        """
        EXIT_CHECK_SECONDS  = 30    # fast - protects an open position
        ENTRY_SCAN_SECONDS  = 300   # 5 min - matches the bar timeframe

        log.info(f"Fastball Bot starting persistent loop | exit checks every {EXIT_CHECK_SECONDS}s, entry scans every {ENTRY_SCAN_SECONDS}s")
        last_entry_scan = None

        while True:
            try:
                now = datetime.datetime.now(ET)

                if not self.is_market_open():
                    log.info("Market closed — sleeping 60s.")
                    time.sleep(60)
                    continue

                if self.past_open_buffer():
                    self.monitor_exit()

                do_entry_scan = (
                    last_entry_scan is None
                    or (now - last_entry_scan).total_seconds() >= ENTRY_SCAN_SECONDS
                )
                if do_entry_scan:
                    log.info(f"━━ Entry scan {now.strftime('%Y-%m-%d %H:%M ET')} ━━")
                    if not self.get_open_option_positions() and self.before_late_cutoff():
                        df = self.get_bars()
                        if df is not None:
                            signal, reason = get_signal(df)
                            log.info(f"Signal: {signal or 'none'} | {reason}")
                            if signal == "buy":
                                self.place_trade("call", reason)
                            elif signal == "sell":
                                self.place_trade("put", reason)
                    last_entry_scan = now

            except Exception as e:
                # A persistent process must never die from one bad cycle -
                # log it clearly and keep running, unlike a one-shot script
                # where a crash just fails that single run.
                log.error(f"💥 Cycle error (continuing): {e}")

            time.sleep(EXIT_CHECK_SECONDS)


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "loop"

    bot = FastballBot()
    if mode == "once":
        # Manual/cron-triggered single run: python fastball_bot.py once
        bot.run_once()
    else:
        # Default: persistent loop for Railway or any always-on host
        bot.run_forever()
