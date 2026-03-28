import math
import sys
import time
import os
import shutil
from datetime import datetime
import pytz
import numpy as np
import pandas as pd
from ib_insync import IB, Stock, util

# Configuration
HOST = '127.0.0.1'
PORT = 7497 # 7497 for TWS Paper, 4002 for Gateway Paper
CLIENT_ID = 2 # Using a different Client ID than main.py

def calculate_wilders_atr(df, n=14):
    """
    Calculates the Average True Range (ATR) strictly using Wilder's Smoothing Method
    via pure Pandas vectorization (eliminating row-by-row iteration).
    The first ATR value is a simple moving average of True Range,
    subsequent values are computed with an EWMA where alpha = 1 / N.
    """
    # 1. Input Validation: Forward-fill the data to model zero-volume periods and trading halts
    df_clean = df.copy()
    df_clean[['high', 'low', 'close']] = df_clean[['high', 'low', 'close']].ffill()

    # Calculate True Range (TR)
    prev_close = df_clean['close'].shift(1)
    tr1 = df_clean['high'] - df_clean['low']
    tr2 = (df_clean['high'] - prev_close).abs()
    tr3 = (df_clean['low'] - prev_close).abs()
    
    # Vectorized max across TR1, TR2, TR3
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

    # Initial Output Series constraint checks
    atr = pd.Series(np.nan, index=df.index, dtype=float)
    
    # 2. Data Cleansing: Drop any leading NaN rows from the TR calculation (such as the first element due to shift)
    tr_clean = tr.dropna()

    if len(tr_clean) >= n:
        # Seed the first valid ATR with the simple average (SMA) of TR
        sma = tr_clean.iloc[:n].mean()
        
        # Build the continuous feed vector for EWMA matching original index scope
        feed = tr.copy()
        
        # Determine the logical indices in the original tr Series for the valid tr_clean window
        first_valid_index = tr_clean.index[0]
        sma_index = tr_clean.index[n-1]
        
        # Mask everything before the SMA start point to NaN
        feed.loc[:sma_index] = np.nan
        # Inject the exact mathematical SMA output at the nth boundary
        feed.loc[sma_index] = sma
        
        # 3. Apply mathematical EWMA algorithm exactly per Wilder's semantics
        # Explicit ignore_na=False to enforce Wilder's signal decay
        atr = feed.ewm(alpha=1/n, adjust=False, ignore_na=False).mean()
        
        # Mask out the prefix elements matched natively to NaN 
        atr.iloc[:n-1] = np.nan

    return atr

def main():
    ib = IB()
    results = []

    try:
        os.makedirs('db_backups', exist_ok=True)
        if os.path.exists('portfolio.db'):
            backup_filename = f"db_backups/portfolio_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copy2('portfolio.db', backup_filename)
            print("Local database backup created securely.")

        print(f"Connecting to IBKR at {HOST}:{PORT}...")
        ib.connect(HOST, PORT, clientId=CLIENT_ID)
        print("Connected successfully.\n")

        print("Retrieving master order book...")
        ib.reqAllOpenOrders()
        ib.sleep(2.0)
        
        open_trades = ib.openTrades()

        print("Retrieving live positions...")
        positions = ib.positions()
        
        # Filter for Stock contracts with position > 0
        long_stock_positions = [
            p for p in positions 
            if p.contract.secType == 'STK' and p.position > 0
        ]

        if not long_stock_positions:
            print("No active long stock positions found. Exiting.")
            sys.exit(0)


        for index, pos in enumerate(long_stock_positions):
            ticker_symbol = pos.contract.symbol
            position_size = pos.position

            print(f"\nProcessing {ticker_symbol} (Position: {position_size})...")

            # 1. Map open long position to its corresponding active Stop Loss order
            stop_trades = [
                t for t in open_trades 
                if t.contract.symbol == ticker_symbol 
                and t.order.orderType in ['STP', 'STP LMT']
                and t.order.action == 'SELL'
            ]

            if not stop_trades:
                print(f"  Warning: No active SELL Stop Loss order found for {ticker_symbol}. Skipping.")
                continue

            # Take the first matching stop trade
            trade = stop_trades[0]
            current_stop_price = float(trade.order.auxPrice)

            # Qualify the contract
            contract = Stock(ticker_symbol, 'SMART', 'USD')
            ib.qualifyContracts(contract)

            # Respect exactly 2.0s pacing sleep
            if index > 0:
                ib.sleep(2.0)

            # 2. Fetch 3 years of weekly data
            bars = ib.reqHistoricalData(
                contract,
                endDateTime='',
                durationStr='3 Y',
                barSizeSetting='1 week',
                whatToShow='TRADES',
                useRTH=True,
                formatDate=1
            )

            if not bars:
                print(f"  Warning: Failed to fetch historical data for {ticker_symbol}.")
                continue

            df = util.df(bars)
            
            # 3. CRITICAL: Implement the Closed-Bar Rule correctly.
            ny_tz = pytz.timezone('America/New_York')
            now_ny = datetime.now(ny_tz)
            market_open_time = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
            
            if now_ny.weekday() == 0 and now_ny < market_open_time:
                is_weekly_bar_open = False
            elif now_ny.weekday() == 4 and now_ny.hour >= 16:
                is_weekly_bar_open = False
            elif now_ny.weekday() in [5, 6]:
                is_weekly_bar_open = False
            else:
                is_weekly_bar_open = True
            
            if is_weekly_bar_open:
                # Monday to Friday before 16:00 -> weekly bar is still incomplete, drop it
                if len(df) > 1:
                    df = df.iloc[:-1]
                else:
                    print(f"  Warning: Not enough closed bars for {ticker_symbol}.")
                    continue
            else:
                # Past Friday 16:00 or Weekend -> weekly bar finalized, do not drop
                if len(df) < 1:
                    print(f"  Warning: Not enough closed bars for {ticker_symbol}.")
                    continue
            
            # 4. Calculate 14-week ATR
            df['ATR_14'] = calculate_wilders_atr(df, n=14)

            # Retrieve the latest values from the appropriately truncated DataFrame
            latest = df.iloc[-1]
            last_closed_bar_close = float(latest['close'])
            atr_14 = float(latest['ATR_14'])

            if math.isnan(atr_14):
                print(f"  Warning: Not enough historical data to calculate 14-week ATR for {ticker_symbol}.")
                continue

            # 5. Calculate the New Potential Stop: Last_Closed_Bar_Close - (3 * ATR)
            new_potential_stop = last_closed_bar_close - (3 * atr_14)

            action_taken = "HELD"
            new_stop_val = current_stop_price

            # 6. Execution (The Ratchet Rule)
            if new_potential_stop > current_stop_price:
                new_stop_val = round(new_potential_stop, 2)
                
                # Update the order object
                trade.order.auxPrice = new_stop_val
                
                # Transmit the modification to the server
                ib.placeOrder(trade.contract, trade.order)
                action_taken = "MODIFIED"

            # 7. Terminal Output audit log
            results.append({
                'Ticker': ticker_symbol,
                'Position Size': position_size,
                'Old Stop': f"${current_stop_price:.2f}",
                'New Stop': f"${new_stop_val:.2f}",
                'Action Taken': action_taken
            })
            print(f"  Result: Old Stop = ${current_stop_price:.2f} | New Stop = ${new_stop_val:.2f} | Action = {action_taken}")

        # Final audit log printout
        if results:
            print("\n" + "="*85)
            print(" POSITION TRACKER SUMMARY")
            print("="*85)
            # Find max padding for pretty printing
            header = f"{'Ticker':<10} | {'Position Size':<15} | {'Old Stop':<12} | {'New Stop':<12} | Action Taken"
            print(header)
            print("-" * len(header))
            for res in results:
                print(f"{res['Ticker']:<10} | {res['Position Size']:<15} | {res['Old Stop']:<12} | {res['New Stop']:<12} | {res['Action Taken']}")
            print("="*85)
        else:
            print("\nNo actionable positions processed.")

    except Exception as e:
        print(f"\nAn error occurred during execution: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("Disconnected from IBKR.")

if __name__ == '__main__':
    main()
