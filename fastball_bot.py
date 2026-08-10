"""
SuperTrend-at-Open options signal bot.

At market open, checks SuperTrend(factor=1.75, ATR=10) on the underlying.
Uptrend -> buys the at-the-money call. Downtrend -> buys the at-the-money put.
Position is closed either when the underlying's SuperTrend flips against it,
or at end of day (0DTE-style, matching the existing iron-condor-bot's
same-day-expiration convention) — whichever comes first.

Runs as a persistent worker (Railway), not a cron job — GitHub Actions cron
wasn't reliable to the minute for curveball-bot, and catching the exact open
matters more here than for a 5-minute EMA check.

Env vars (names match ALPACA_API_KEY / ALPACA_API_SECRET already used by
curveball-bot and iron-condor-bot):
    ALPACA_API_KEY          required
    ALPACA_API_SECRET       required
    ALPACA_PAPER            "true"/"false", default "true"
    UNDERLYING_SYMBOL       default "SPY"
    ST_ATR_PERIOD           default "10"
    ST_MULTIPLIER           default "1.75"
    BAR_TIMEFRAME_MINUTES   default "5"
    MAX_PREMIUM_USD         default "500"   (dollar budget for premium per trade)
    ENTRY_WINDOW_START      default "09:30"
    ENTRY_WINDOW_END        default "09:35" (grace window for loop timing)
    EOD_CLOSE_TIME          default "15:45"
    POLL_SECONDS            default "20"

KNOWN LIMITATIONS (first version — expect to iterate, same as the other bots):
  - "Already traded today" is tracked in memory. A Railway restart mid-day
    could reset it; the bot still won't double-enter because it also checks
    for an existing open position first, but it could skip a day's trade if
    it restarts after already closing a position. Worth hardening with real
    persistence (a small database or Railway volume) once this is running.
  - ATM strike search widens to a nearby expiration if 0DTE isn't listed for
    a given day; double check this fallback is actually what you want.
  - Order fills, not just submissions, aren't explicitly confirmed before
    logging success — reasonable for paper trading, worth adding a fill
    check before this touches real money.
"""

import os
import time
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta

import pandas as pd
import pytz

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, MarketOrderRequest
from alpaca.trading.enums import ContractType, AssetStatus, OrderSide, OrderType, TimeInForce
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, OptionLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from supertrend import compute_supertrend

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("supertrend_options_bot")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
API_KEY = os.environ["ALPACA_API_KEY"]
API_SECRET = os.environ["ALPACA_API_SECRET"]
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"

UNDERLYING_SYMBOL = os.environ.get("UNDERLYING_SYMBOL", "SPY")
ST_ATR_PERIOD = int(os.environ.get("ST_ATR_PERIOD", "10"))
ST_MULTIPLIER = float(os.environ.get("ST_MULTIPLIER", "1.75"))
BAR_TIMEFRAME_MINUTES = int(os.environ.get("BAR_TIMEFRAME_MINUTES", "5"))
MAX_PREMIUM_USD = float(os.environ.get("MAX_PREMIUM_USD", "500"))
ENTRY_WINDOW_START = os.environ.get("ENTRY_WINDOW_START", "09:30")
ENTRY_WINDOW_END = os.environ.get("ENTRY_WINDOW_END", "09:35")
EOD_CLOSE_TIME = os.environ.get("EOD_CLOSE_TIME", "15:45")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "20"))

ET = pytz.timezone("America/New_York")

trading_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
stock_data_client = StockHistoricalDataClient(API_KEY, API_SECRET)
option_data_client = OptionHistoricalDataClient(API_KEY, API_SECRET)

# In-memory state — Alpaca's own positions are the source of truth on every
# loop; this just tracks what we don't want to re-derive every cycle.
state = {"traded_today": None, "position_side": None}


class _HealthHandler(BaseHTTPRequestHandler):
    """Answers any request with 200 OK. Exists only so Railway's default
    health check (which expects an HTTP port) doesn't mark this background
    worker as unhealthy and kill it — this bot has no actual web traffic."""

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass  # keep Railway's logs to our own log lines, not per-request noise


def _start_health_server():
    port = int(os.environ.get("PORT", "8080"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    log.info("Health check server listening on port %s", port)
    server.serve_forever()


def parse_hhmm(s: str):
    h, m = s.split(":")
    return int(h), int(m)


def now_et() -> datetime:
    return datetime.now(ET)


def in_entry_window(now: datetime) -> bool:
    sh, sm = parse_hhmm(ENTRY_WINDOW_START)
    eh, em = parse_hhmm(ENTRY_WINDOW_END)
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start <= now <= end


def past_eod_close(now: datetime) -> bool:
    eh, em = parse_hhmm(EOD_CLOSE_TIME)
    eod = now.replace(hour=eh, minute=em, second=0, microsecond=0)
    return now >= eod


def _bars_df(symbol: str, timeframe: TimeFrame, start=None, limit=None) -> pd.DataFrame:
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=timeframe, start=start, limit=limit)
    bars = stock_data_client.get_stock_bars(req).df
    if bars.empty:
        raise RuntimeError(f"No bars returned for {symbol}")
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol, level="symbol")
    return bars.sort_index()


def get_latest_uptrend_signal() -> bool:
    tf = TimeFrame(BAR_TIMEFRAME_MINUTES, TimeFrameUnit.Minute)
    bars = _bars_df(UNDERLYING_SYMBOL, tf, start=now_et() - timedelta(days=5))
    result = compute_supertrend(bars, atr_period=ST_ATR_PERIOD, multiplier=ST_MULTIPLIER)
    return bool(result["is_uptrend"].iloc[-1])


def get_underlying_price() -> float:
    bars = _bars_df(UNDERLYING_SYMBOL, TimeFrame(1, TimeFrameUnit.Minute), limit=1)
    return float(bars["close"].iloc[-1])


def find_atm_contract(option_type: ContractType):
    underlying_price = get_underlying_price()
    today = now_et().date()
    strike_lo = str(round(underlying_price * 0.90, 2))
    strike_hi = str(round(underlying_price * 1.10, 2))

    req = GetOptionContractsRequest(
        underlying_symbols=[UNDERLYING_SYMBOL],
        status=AssetStatus.ACTIVE,
        type=option_type,
        strike_price_gte=strike_lo,
        strike_price_lte=strike_hi,
        expiration_date=today,
    )
    contracts = trading_client.get_option_contracts(req).option_contracts

    if not contracts:
        log.info("No 0DTE contracts found for %s, widening to nearest expiration in the next 7 days", UNDERLYING_SYMBOL)
        req2 = GetOptionContractsRequest(
            underlying_symbols=[UNDERLYING_SYMBOL],
            status=AssetStatus.ACTIVE,
            type=option_type,
            strike_price_gte=strike_lo,
            strike_price_lte=strike_hi,
            expiration_date_gte=today,
            expiration_date_lte=today + timedelta(days=7),
        )
        contracts = trading_client.get_option_contracts(req2).option_contracts
        if not contracts:
            raise RuntimeError(f"No {option_type.value} contracts found for {UNDERLYING_SYMBOL}")
        min_exp = min(c.expiration_date for c in contracts)
        contracts = [c for c in contracts if c.expiration_date == min_exp]

    return min(contracts, key=lambda c: abs(c.strike_price - underlying_price))


def size_contract_qty(symbol: str) -> int:
    req = OptionLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = option_data_client.get_option_latest_quote(req)[symbol]
    ask = float(quote.ask_price)
    if ask <= 0:
        raise RuntimeError(f"No valid ask price for {symbol}")
    return max(int(MAX_PREMIUM_USD // (ask * 100)), 1)


def submit_buy(symbol: str, qty: int):
    order = MarketOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY,
        type=OrderType.MARKET, time_in_force=TimeInForce.DAY,
    )
    return trading_client.submit_order(order_data=order)


def get_open_option_position():
    for p in trading_client.get_all_positions():
        if p.asset_class.value == "us_option" and UNDERLYING_SYMBOL in p.symbol:
            return p
    return None


def wait_until_flat(timeout_s: int = 30) -> bool:
    """curveball-bot hit a real race condition closing and reopening too
    fast once — confirm flat before doing anything else with this symbol."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if get_open_option_position() is None:
            return True
        time.sleep(2)
    log.warning("Timed out waiting for position to confirm flat")
    return False


def try_enter():
    today = now_et().date()
    if state["traded_today"] == today or get_open_option_position() is not None:
        return

    try:
        is_uptrend = get_latest_uptrend_signal()
    except Exception:
        log.exception("Failed to compute SuperTrend signal")
        return

    option_type = ContractType.CALL if is_uptrend else ContractType.PUT
    side_label = "call" if is_uptrend else "put"
    log.info("SuperTrend at open: %s -> buying ATM %s", "uptrend" if is_uptrend else "downtrend", side_label)

    try:
        contract = find_atm_contract(option_type)
        qty = size_contract_qty(contract.symbol)
        order = submit_buy(contract.symbol, qty)
        log.info("Submitted BUY %s x%s (order id=%s)", contract.symbol, qty, order.id)
        state["position_side"] = side_label
    except Exception:
        log.exception("Entry failed")
        return
    finally:
        state["traded_today"] = today


def try_exit():
    pos = get_open_option_position()
    if pos is None:
        state["position_side"] = None
        return

    if state["position_side"] is None:
        # Resumed after a restart — ask Alpaca what this contract actually is
        # instead of guessing from the symbol string.
        contract = trading_client.get_option_contract(pos.symbol)
        state["position_side"] = contract.type.value

    now = now_et()
    should_exit, reason = False, ""

    if past_eod_close(now):
        should_exit, reason = True, "EOD close"
    else:
        try:
            is_uptrend = get_latest_uptrend_signal()
            if state["position_side"] == "call" and not is_uptrend:
                should_exit, reason = True, "trend flipped to downtrend"
            elif state["position_side"] == "put" and is_uptrend:
                should_exit, reason = True, "trend flipped to uptrend"
        except Exception:
            log.exception("Failed to check SuperTrend for exit — leaving position open this cycle")
            return

    if should_exit:
        log.info("Closing %s x%s — reason: %s", pos.symbol, pos.qty, reason)
        try:
            trading_client.close_position(pos.symbol)
            wait_until_flat()
        except Exception:
            log.exception("Exit order failed")
        finally:
            state["position_side"] = None


def main():
    log.info(
        "Starting SuperTrend-at-open bot | underlying=%s factor=%s atr=%s paper=%s",
        UNDERLYING_SYMBOL, ST_MULTIPLIER, ST_ATR_PERIOD, PAPER,
    )
    threading.Thread(target=_start_health_server, daemon=True).start()
    while True:
        try:
            clock = trading_client.get_clock()
            if not clock.is_open:
                time.sleep(POLL_SECONDS)
                continue

            now = now_et()
            if get_open_option_position() is not None:
                try_exit()
            elif in_entry_window(now):
                try_enter()

        except Exception:
            log.exception("Unhandled error in main loop — continuing")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
