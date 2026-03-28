import math
import sys
import pandas as pd
from ib_insync import IB, Stock, util

# Configuration
HOST = '127.0.0.1'
PORT = 7497 # TWS Paper
CLIENT_ID = 2 # Different ID from main.py

pacing_violation_flag = False

def pacing_error_handler(reqId, errorCode, errorString, contract):
    global pacing_violation_flag
    if errorCode in [162, 420]:
        pacing_violation_flag = True

def get_sp500_tickers():
    """
    Fetches the S&P 500 components from Wikipedia and mapping to GICS Sectors.
    Formats tickers for IBKR (e.g., BRK.B -> BRK B).
    """
    url = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
    try:
        tables = pd.read_html(url)
        df = tables[0]
        ticker_sector_map = {}
        for _, row in df.iterrows():
            ticker = row['Symbol'].replace('.', ' ')
            sector = row['GICS Sector']
            ticker_sector_map[ticker] = sector
        return ticker_sector_map
    except Exception as e:
        print(f"Error fetching S&P 500 tickers: {e}")
        sys.exit(1)

def main():
    ib = IB()
    ib.errorEvent += pacing_error_handler
    try:
        print(f"Connecting to IBKR Screener at {HOST}:{PORT}...")
        ib.connect(HOST, PORT, clientId=CLIENT_ID)
        print("Connected successfully.\n")

        universe_dict = get_sp500_tickers()
        
        positions = ib.positions()
        blocked_sectors = set()
        
        for pos in positions:
            symbol = pos.contract.symbol
            if pos.position != 0 and symbol in universe_dict:
                blocked_sectors.add(universe_dict[symbol])
                
        print(f"Active Portfolio Sectors Blocked: {blocked_sectors}")

        valid_tickers = []
        
        total_tickers = len(universe_dict)
        print(f"Starting screen of {total_tickers} tickers...")

        for idx, (ticker_symbol, sector) in enumerate(universe_dict.items()):
            if sector in blocked_sectors:
                print(f"[{idx+1}/{total_tickers}] Rejected: {ticker_symbol} (Sector Overlap: {sector})")
                continue
                
            print(f"[{idx+1}/{total_tickers}] Processing {ticker_symbol}...")
            
            try:
                contract = Stock(ticker_symbol, 'SMART', 'USD')
                ib.qualifyContracts(contract)

                # Fetch Historical Data using errorEvent Callback
                max_retries = 3
                retry_delays = [10, 20, 40]
                bars = None
                
                global pacing_violation_flag
                
                for attempt in range(max_retries + 1):
                    pacing_violation_flag = False
                    
                    bars = ib.reqHistoricalData(
                        contract,
                        endDateTime='',
                        durationStr='2 Y',
                        barSizeSetting='1 week',
                        whatToShow='TRADES',
                        useRTH=True,
                        formatDate=1
                    )
                    
                    if pacing_violation_flag:
                        if attempt < max_retries:
                            delay = retry_delays[attempt]
                            print(f"  Pacing Violation (Error 162/420) detected. Exponential backoff: Sleeping for {delay} seconds before retry...")
                            ib.sleep(delay)
                            continue
                        else:
                            print(f"  Max retries reached for {ticker_symbol} due to pacing violations. Skipping.")
                            bars = None
                            break
                    else:
                        break  # Success

                if not bars:
                    print(f"  Warning: No historical data returned for {ticker_symbol}. Skipping.")
                    continue

                df = util.df(bars)
                
                # Minimum data check (40 weeks for SMA + padding)
                if len(df) < 41:
                    print(f"  Warning: Insufficient historical data for {ticker_symbol} ({len(df)} weeks). Skipping.")
                    continue

                # Calculations
                df['SMA_40'] = df['close'].rolling(window=40).mean()
                df['SMA_10'] = df['close'].rolling(window=10).mean()
                
                # 26-week Rate of Change
                # Formula: (Current_Close - Close_26_weeks_ago) / Close_26_weeks_ago
                df['ROC_26'] = df['close'].pct_change(periods=26)

                latest = df.iloc[-1]
                last_close = float(latest['close'])
                sma_40 = float(latest['SMA_40'])
                sma_10 = float(latest['SMA_10'])
                roc_26 = float(latest['ROC_26'])

                if math.isnan(sma_40) or math.isnan(sma_10) or math.isnan(roc_26):
                    print(f"  Warning: NaN calculated for {ticker_symbol}. Skipping.")
                    continue

                # Macro Filter (Last Close > 40-week SMA)
                macro_pass = last_close > sma_40
                
                if macro_pass:
                    # Extension Limit Filter (Price is NOT overextended by > 5% of 10-week SMA)
                    extension_limit = sma_10 * 1.05
                    if last_close > extension_limit:
                        print(f"  Rejected: Overextended (Close: ${last_close:.2f} > 105% of SMA_10: ${sma_10:.2f})")
                    else:
                        print(f"  Passed: {ticker_symbol} | Close: ${last_close:.2f} > SMA: ${sma_40:.2f} | ROC: {roc_26:.2%}")
                        valid_tickers.append({
                            'Ticker': ticker_symbol,
                            'Close_Price': round(last_close, 2),
                            'SMA_40': round(sma_40, 2),
                            'SMA_10': round(sma_10, 2),
                            'ROC_26': roc_26,
                            'Macro_Status': 'Pass'
                        })
                else:
                    print(f"  Failed Macro Filter: {ticker_symbol}")

            except Exception as e:
                print(f"  Error processing {ticker_symbol}: {e}")
                # Reconnect or sleep heavily if a true pacing violation was hit, but
                # typically the loop catch prevents complete crash.
                ib.sleep(10.0)

        if not valid_tickers:
            print("\nNo tickers passed the macro filter.")
            sys.exit(0)

        # Create DataFrame of surviving tickers
        results_df = pd.DataFrame(valid_tickers)

        # Sort by 26-week ROC (Descending)
        results_df = results_df.sort_values(by='ROC_26', ascending=False).reset_index(drop=True)

        # Identify Top Decile (Top 10% of the ORIGINAL universe, not just survivors)
        # Note: If 100 tickers in universe, top decile is top 10.
        top_decile_count = max(1, math.ceil(total_tickers * 0.10))
        
        # We take the top N from our surviving list
        top_decile_df = results_df.head(top_decile_count)

        print("\n" + "="*50)
        print(f" SCREENING COMPLETE - TOP DECILE ({top_decile_count} Tickers)")
        print("="*50)
        
        # Format the ROC column as a percentage for display
        display_df = top_decile_df.copy()
        display_df['ROC_26'] = display_df['ROC_26'].apply(lambda x: f"{x:.2%}")
        print(display_df.to_string(index=False))
        print("="*50)

        # Export to CSV
        output_file = 'target_universe.csv'
        top_decile_df.to_csv(output_file, index=False)
        print(f"\nResults successfully exported to {output_file}")


    except Exception as e:
        print(f"\nCritical Error in Screener: {e}")
    finally:
        if ib.isConnected():
            ib.disconnect()
            print("Disconnected from IBKR Workspace.")

if __name__ == '__main__':
    main()
