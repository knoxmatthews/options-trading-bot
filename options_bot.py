import os
import time
from datetime import datetime, timedelta
import pandas as pd
import talib

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data import StockHistoricalDataClient, TimeFrame

class OptionsBot:
    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        self.client = TradingClient(api_key, api_secret, paper=paper)
        self.data_client = StockHistoricalDataClient(api_key, api_secret)
        self.underlyings = ["SPY", "SPX", "QQQ", "AAPL", "TSLA", "NVDA"]
        self.risk_percent = 0.05

    def get_account_info(self):
        account = self.client.get_account()
        return {
            "buying_power": float(account.buying_power),
            "portfolio_value": float(account.portfolio_value)
        }

    def get_signal(self, symbol: str):
        """Momentum signal using SMA crossover"""
        try:
            bars = self.data_client.get_stock_bars(
                symbol, 
                TimeFrame.Minute,
                start=datetime.now() - timedelta(minutes=200)
            ).df
            
            if len(bars) < 50:
                return None
                
            bars['sma9'] = talib.SMA(bars['close'], 9)
            bars['sma21'] = talib.SMA(bars['close'], 21)
            
            if bars['sma9'].iloc[-1] > bars['sma21'].iloc[-1] and bars['sma9'].iloc[-2] <= bars['sma21'].iloc[-2]:
                return "BULLISH"
            elif bars['sma9'].iloc[-1] < bars['sma21'].iloc[-1] and bars['sma9'].iloc[-2] >= bars['sma21'].iloc[-2]:
                return "BEARISH"
        except Exception as e:
            print(f"Error getting signal for {symbol}: {e}")
        return None

    def find_best_contract(self, underlying: str, option_type: str = "call", days_max: int = 14):
        try:
            req = GetOptionContractsRequest(
                underlying_symbols=[underlying],
                type=option_type,
                expiration_date_gte=datetime.now().date(),
                expiration_date_lte=(datetime.now() + timedelta(days=days_max)).date(),
                limit=100
            )
            contracts = self.client.get_option_contracts(req)
            
            if not contracts:
                return None
                
            latest = self.data_client.get_latest_quote(underlying)
            price = (latest.ask_price + latest.bid_price) / 2 if latest else 450
            
            best = min(contracts, key=lambda c: abs(float(c.strike_price) - price))
            print(f"Selected {option_type.upper()} {best.symbol} | Strike: {best.strike_price}")
            return best
        except Exception as e:
            print(f"Contract search error for {underlying}: {e}")
            return None

    def place_order(self, contract_symbol: str, side: str = "buy", qty: int = 1):
        try:
            order = MarketOrderRequest(
                symbol=contract_symbol,
                qty=qty,
                side=OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY
            )
            submitted = self.client.submit_order(order)
            print(f"✅ ORDER PLACED → {side.upper()} {contract_symbol} | ID: {submitted.id}")
            return submitted
        except Exception as e:
            print(f"❌ Order failed: {e}")
            return None

    def run_scan(self):
        print(f"\n🚀 [{datetime.now()}] Starting scan...")
        account = self.get_account_info()
        print(f"Account → Buying Power: ${account['buying_power']:.2f} | Portfolio: ${account['portfolio_value']:.2f}")
        
        for symbol in self.underlyings:
            signal = self.get_signal(symbol)
            if not signal:
                continue
                
            print(f"{symbol} → {signal}")
            
            if signal == "BULLISH":
                contract = self.find_best_contract(symbol, "call")
                if contract:
                    self.place_order(contract.symbol, "buy")
            elif signal == "BEARISH":
                contract = self.find_best_contract(symbol, "put")
                if contract:
                    self.place_order(contract.symbol, "buy")

if __name__ == "__main__":
    api_key = os.getenv("ALPACA_KEY")
    api_secret = os.getenv("ALPACA_SECRET")
    
    if not api_key or not api_secret:
        print("❌ Missing ALPACA_KEY or ALPACA_SECRET!")
        exit(1)
    
    bot = OptionsBot(api_key, api_secret, paper=True)
    print("🤖 Options Bot Started (Paper Trading)")
    
    while True:
        try:
            bot.run_scan()
            print("💤 Sleeping 15 minutes...\n")
            time.sleep(900)
        except KeyboardInterrupt:
            print("Bot stopped.")
            break
        except Exception as e:
            print(f"Unexpected error: {e}")
            time.sleep(60)
