import math
import sys
import os
import shutil
from datetime import datetime
import numpy as np
import pandas as pd
from ib_insync import IB, Stock, LimitOrder, StopOrder, util

# Configuration
HOST = '127.0.0.1'
PORT = 7497 # 7497 for TWS Paper, 4002 for Gateway Paper
CLIENT_ID = 1

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
    try:
        os.makedirs('db_backups', exist_ok=True)
        if os.path.exists('portfolio.db'):
            backup_filename = f"db_backups/portfolio_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            shutil.copy2('portfolio.db', backup_filename)
            print("Local database backup created securely.")

        # 1. Setup & Connection
        print(f"Connecting to IBKR at {HOST}:{PORT}...")
        ib.connect(HOST, PORT, clientId=CLIENT_ID)
        print("Connected successfully.")
        
        # 2. Capital Retrieval
        print("Fetching account summary...")
        account_values = ib.accountSummary() # Using accountSummary to get overall net liq
        net_liq = None
        for val in account_values:
            if val.tag == 'NetLiquidation':
                net_liq = float(val.value)
                break
        
        if net_liq is None:
            # Fallback to accountValues if summary doesn't immediately have it
            for val in ib.accountValues():
                if val.tag == 'NetLiquidation':
                    net_liq = float(val.value)
                    break
                    
        if net_liq is None:
            print("Error: Could not retrieve Net Liquidation Value.")
            sys.exit(1)
            
        print(f"Current Net Liquidation Value: ${net_liq:,.2f}")

        # 3. Data Fetching
        ticker_symbol = input("Enter stock ticker to trade: ").strip().upper()
        if not ticker_symbol:
            print("Invalid ticker symbol.")
            sys.exit(1)

        print("Fetching current positions to prevent duplicates...")
        positions = ib.positions()
        for pos in positions:
            if pos.contract.symbol == ticker_symbol and pos.position > 0:
                print(f"\nCRITICAL ERROR: Position for {ticker_symbol} already exists.")
                print("Aborting to prevent doubling the 1.5% risk limit.")
                sys.exit(0)

        contract = Stock(ticker_symbol, 'SMART', 'USD')
        ib.qualifyContracts(contract)

        print(f"Fetching historical data for {ticker_symbol} (Lookback: 3 Years)...")
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
            print(f"Error: Failed to fetch historical data for {ticker_symbol}.")
            sys.exit(1)

        df = util.df(bars)
        
        # 4. Math & Indicators
        # 40-week and 10-week SMA
        df['SMA_40'] = df['close'].rolling(window=40).mean()
        df['SMA_10'] = df['close'].rolling(window=10).mean()

        # 14-week ATR (Wilder's Smoothing)
        df['ATR_14'] = calculate_wilders_atr(df, n=14)

        # Retrieve the latest values
        latest = df.iloc[-1]
        last_close = float(latest['close'])
        sma_40 = float(latest['SMA_40'])
        sma_10 = float(latest['SMA_10'])
        atr_14 = float(latest['ATR_14'])

        if math.isnan(sma_40) or math.isnan(sma_10) or math.isnan(atr_14):
            print("Error: Not enough historical data to calculate the 40-week and 10-week SMA and 14-week ATR.")
            sys.exit(1)

        # 5. Macro Filter (Hard Stop)
        print(f"\n--- Indicator Summary ---")
        print(f"Latest Weekly Close : ${last_close:.2f}")
        print(f"40-Week SMA         : ${sma_40:.2f}")
        print(f"10-Week SMA         : ${sma_10:.2f}")
        
        if last_close <= sma_40:
            print(f"\nMacro Filter Failed: Price (${last_close:.2f}) is below 40-week SMA (${sma_40:.2f}).")
            print("Halting script immediately.")
            sys.exit(0)
            
        extension_limit = sma_10 * 1.05
        if last_close > extension_limit:
            print(f"\nMacro Filter Failed: Price (${last_close:.2f}) is overextended (> 105% of 10-week SMA: ${sma_10:.2f}).")
            print("Halting script immediately.")
            sys.exit(0)
            
        print("Macro Filter Passed: Price is above 40-week SMA and not overextended.")

        # Request synchronous real-time market data to determine Limit (Ask) price
        print(f"Fetching real-time synchronous market data to determine Limit (Ask) price...")
        ib.reqMarketDataType(4)
        tickers = ib.reqTickers(contract)
        
        if not tickers:
            print("\nError: Failed to fetch a definitive synchronous Ask price (Invalid or empty ticker returned).")
            sys.exit(1)
            
        ticker = tickers[0]
        ask_price = None
        
        for price in [ticker.ask, ticker.last, ticker.close]:
            if price is not None and not math.isnan(price) and price > 0:
                ask_price = float(price)
                break
                
        if ask_price is None:
            print("\nError: Failed to fetch a valid price (Ask, Last, or Close are invalid or empty).")
            sys.exit(1)

        # 6. Risk Management Engine
        # Risk amount = Capital * 0.015
        risk_fraction = 0.015
        risk_amount = net_liq * risk_fraction
        
        # Shares = (Capital * 0.015) / (3 * ATR)
        shares_raw = risk_amount / (3 * atr_14)
        shares = math.floor(shares_raw)

        if shares <= 0:
            print("\nError: Calculated shares are 0. Capital or risk footprint is too small relative to 3x ATR.")
            sys.exit(1)

        # 7. Exit Calculation
        stop_price = last_close - (3 * atr_14)
        
        # 8. Order Staging & Verification Check
        print("\n" + "="*50)
        print(" VERIFICATION REQUIRED: ORDER SUMMARY")
        print("="*50)
        print(f"Total Capital    : ${net_liq:,.2f}")
        print(f"Target Ticker    : {ticker_symbol}")
        print(f"Current Price    : ${last_close:.2f}")
        print(f"Parent Limit (Ask): ${ask_price:.2f}")
        print(f"14-W ATR         : ${atr_14:.2f}")
        print(f"Target Shares    : {shares} (Risk: 1.5%)")
        print(f"Risk Amount ($)  : ${risk_amount:,.2f}")
        print(f"Stop Price       : ${stop_price:.2f}")
        print("="*50)

        confirm = input("\nTransmit this bracket order to IBKR? [y/n]: ").strip().lower()

        if confirm == 'y':
            # Parent: BUY LMT
            parent = LimitOrder('BUY', shares, round(ask_price, 2), tif='GTC', outsideRth=False)
            parent.transmit = False
            trade_parent = ib.placeOrder(contract, parent)

            # Child: SELL STP
            stop = StopOrder('SELL', shares, round(stop_price, 2), tif='GTC', outsideRth=False)
            stop.parentId = trade_parent.order.orderId
            stop.transmit = True
            trade_stop = ib.placeOrder(contract, stop)
            
            # Transmission Buffer
            ib.sleep(5)
            
            print(f"\nOrders placed successfully!")
            print(f"Parent LMT Order ID : {trade_parent.order.orderId}")
            print(f"Child STP Order ID  : {trade_stop.order.orderId}")
            
            # Automated Logging
            try:
                import sqlite3
                
                db_file = 'portfolio.db'
                conn = sqlite3.connect(db_file)
                cursor = conn.cursor()
                
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS positions (
                        Ticker TEXT,
                        Entry_Price REAL,
                        Current_Stop REAL
                    )
                ''')
                
                cursor.execute('''
                    INSERT INTO positions (Ticker, Entry_Price, Current_Stop)
                    VALUES (?, ?, ?)
                ''', (ticker_symbol, round(ask_price, 2), round(stop_price, 2)))
                
                conn.commit()
                conn.close()
                
                print(f"Logged trade securely to {db_file}")
            except Exception as e:
                print(f"Error logging trade to database: {e}")
                
        else:
            print("\nOrder transmission aborted by user.")

    except Exception as e:
        print(f"\nAn error occurred during execution: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("Disconnected from IBKR.")

if __name__ == '__main__':
    main()
