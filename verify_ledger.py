import sqlite3
import sys
import os

def verify_ledger():
    db_file = 'portfolio.db'
    conn = None
    try:
        if not os.path.exists(db_file):
            print(f"Error: Database file '{db_file}' not found.")
            sys.exit(1)
            
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        
        # Verify existence of the positions table
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='positions'")
        if not cursor.fetchone():
            print(f"Error: Table 'positions' does not exist in {db_file}.")
            sys.exit(1)
            
        print(f"Schema status: 'positions' table verified inside {db_file}.\n")
        
        # Retrieve all logged trades
        cursor.execute("SELECT * FROM positions")
        rows = cursor.fetchall()
        
        # Print cleanly formatted table
        header = f"{'Ticker':<10} | {'Entry_Price':<15} | {'Current_Stop':<15}"
        print(header)
        print("-" * len(header))
        
        if not rows:
            print("No trades currently logged in the ledger.")
        else:
            for row in rows:
                ticker = str(row[0])
                entry = float(row[1])
                stop = float(row[2])
                print(f"{ticker:<10} | ${entry:<14.2f} | ${stop:<14.2f}")
                
        print("\nAudit execution complete.")
        
    except sqlite3.Error as e:
        print(f"SQLite validation error occurred: {e}")
        sys.exit(1)
    finally:
        if conn:
            conn.close()

if __name__ == '__main__':
    verify_ledger()
