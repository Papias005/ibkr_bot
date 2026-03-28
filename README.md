# Systematic Trend-Following Architecture (IBKR)

## Overview
A high-precision, quantitative weekly trend-following execution engine built on the `ib_insync` framework for Interactive Brokers (IBKR) TWS/Gateway. 

The system operates strictly on end-of-week data to eliminate intraday noise. It systematically exploits cross-sectional momentum anomalies within the S&P 500, filtered for structural trend integrity and strictly bound by mathematical risk parity limits. The architecture is completely autonomous in its calculation and risk management phases, operating as a localized algorithmic state machine without reliance on external paid data feeds.

## Core Quantitative Logic
* **Macro Regime Filter:** Only assets trading above their 40-week Simple Moving Average ($SMA_{40}$) are eligible.
* **Momentum Ranking:** Eligible assets are sorted by their 26-week Rate of Change ($ROC_{26}$). The top decile forms the target universe.
* **Mean-Reversion Protection:** The system inherently rejects assets whose closing price exceeds 105% of their 10-week SMA ($SMA_{10}$), mathematically blocking blow-off top entries.
* **Sector Risk Parity:** The screener dynamically reads live open positions and cross-references GICS Sectors via Wikipedia scraping. It aggressively blocks new entries in sectors already held, preventing macroeconomic exposure overlap.
* **Volatility Normalization:** Risk is dynamically scaled using a 14-week Average True Range ($ATR_{14}$), strictly employing Wilder's Smoothing Method via pure Pandas vectorization.
* **Portfolio Heat Limit:** Position sizing is hardcoded to never exceed a maximum capital risk of 1.5% per trade. 
* **Asymmetric Payoff:** The system utilizes a dynamic mechanical trailing stop ($Close - 3 \times ATR_{14}$), functioning as a one-way ratchet to lock in unrealized gains until the trend structurally exhausts.

## System Modules

### 1. `screener.py` (Universe Selection & Risk Parity)
Dynamically scrapes the current S&P 500 constituents, maps them to their respective GICS Sectors, and queries live portfolio holdings to enforce Sector Risk Parity. Bypasses historical data requests for blocked sectors to preserve API pacing limits. Evaluates surviving tickers against the macro/momentum filters and outputs the top decile to a localized CSV. Includes an event-driven backoff sequence to safely handle IBKR Pacing Violations (Errors 162/420).

### 2. `main.py` (Execution Engine)
The primary execution module. Fetches real-time delayed synchronous market data to bypass missing Ask prices on Paper accounts. Validates all mathematical conditions independently (preventing weekend calculation latency), calculates the 1.5% risk constraints, and routes a Bracket Order (Parent Limit / Child Stop) to the exchange. Utilizes local SQLite tracking and an automated local DB backup protocol to prevent state corruption.

### 3. `tracker.py` (Risk Management Engine)
Executes the trailing stop protocol. Features strict timezone enforcement (`pytz` America/New_York) to differentiate between closed and in-progress weekly bars, preventing mid-week signal destruction. Scans live IBKR positions, recalculates the $3 \times ATR_{14}$ boundary, and modifies live exchange Stop orders if the mathematical threshold has moved favorably.

### 4. `verify_ledger.py` (Audit Protocol)
A local terminal utility that validates the `portfolio.db` SQLite schema and strictly outputs the historical footprint (Ticker, Entry Price, Current Stop) to terminal standard output.

## Prerequisites
* Python 3.8+
* Interactive Brokers TWS or IB Gateway (Configured for API connections on port `7497` or `4002`).
* Python dependencies frozen in `requirements.txt`. Install via:
  ```bash
  pip install -r requirements.txt
