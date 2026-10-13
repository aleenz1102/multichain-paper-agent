# Multi-Chain Paper Trading Agent

A simulation-only crypto paper-trading dashboard. No wallet connection, private keys, exchange credentials, or live-order endpoints are included.

## What it does
- Expanded simulated watchlist across Solana, Ethereum, Base, Arbitrum, BNB Chain, Celestia, and Hyperliquid, including selected tokens appearing on public trending lists
- Deterministic demo prices and sample signals
- Paper buy/sell orders with configurable fee and slippage assumptions
- Simulated portfolio, open positions, trade journal, and audit events
- Manual virtual wallet top-ups when you run out of simulation cash; each top-up is logged and moves no real funds
- Quick sell 25%, 50%, or 100% of an existing paper position
- Top-ups are excluded from net trading P&L so adding virtual cash does not appear as trading profit
- Net P&L, win rate, profit factor, and max drawdown metrics
- Risk guardrails: per-trade allocation cap, daily loss cap, max concurrent positions, pause switch
- SQLite persistence for trades, settings, and audit events

> **SIMULATION ONLY — NO REAL FUNDS.** Demo prices and signals are synthetic. Results are not predictions and do not establish that any strategy is profitable.

## Run in GitHub Codespaces (browser only)
1. Sign in to GitHub and create a new **public or private repository** named `multichain-paper-agent`.
2. Upload the contents of this folder to that repository (use **Add file → Upload files** on GitHub).
3. Open the repository, choose **Code → Codespaces → Create codespace on main**.
4. In the Codespaces terminal, run:
   ```bash
   pip install -r requirements.txt
   uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
5. When Codespaces asks whether to open port 8000, choose **Open in Browser**.

GitHub free Codespaces quotas are limited and can change; check your account's current included usage.


### One-command Windows launcher

From PowerShell, in the cloned repository folder, run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\run_windows.ps1
```

The launcher creates `.venv` if needed, installs the requirements, and binds Uvicorn to `127.0.0.1:8000` only. It does not enable automated paper trading or overwrite `paper_agent.db`. Keep the terminal open while using the app; press Ctrl+C to stop it. If the execution-policy command is blocked by your organization, follow your organization's PowerShell policy rather than changing it globally.

## Run locally (optional)
Python 3.10+:
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Open http://localhost:8000

## Simulation assumptions
- Initial virtual cash defaults to ₹100,000 (virtual units only; it is not a deposit or real capital).
- Buy and sell fees are charged as a percentage of notional.
- Slippage is charged against the execution price in basis points.
- No actual blockchain calls are made. Chains are labels in the synthetic market feed.
- The daily loss circuit breaker uses realized P&L from simulated SELL orders for the current UTC date. A separate portfolio drawdown circuit breaker uses a persisted equity high-water mark and includes unrealized P&L when valid marks are available.
- Demo market prices update deterministically on each request. This is not a live market feed.
- Profit metrics use completed paper trades and are indicative only.

## Safety
- No live-trading code exists in this starter.
- Do not add private keys or exchange secrets to this repository.
- Do not interpret simulated backtests or demo signals as investment advice.

## Automated paper mode

The current `main` branch includes the neon dashboard, public USDT market scanner, selected-market candles, research endpoints, and an opt-in paper scheduler. The scheduler runs a cycle immediately after automation is enabled or resumed, then on a 60-second cadence. Dashboard stats refresh every second and selected-market ticker/candle updates use the public Binance WebSocket stream; REST candle history refreshes periodically.

### Windows local run

Use PowerShell from the repository folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. To run the test suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Do not overwrite your existing `paper_agent.db` when checking out a development branch. Back it up before attempting any migration or reset. The reset button clears simulation records and is intentionally destructive.

### Automated simulation controls

- Automated paper mode is **disabled by default**. When enabled and unpaused, the in-process scheduler runs immediately, then checks for another cycle every 60 seconds. Cycles are deduplicated by completed-candle timestamp, so a one-second dashboard refresh cannot trigger duplicate entries.
- Keep Uvicorn at one worker while using the in-process scheduler. Multiple workers/processes can each start their own scheduler; this build is not a distributed scheduler.
- Daily realized-loss, per-entry size, max concurrent positions, max portfolio exposure, and portfolio drawdown checks are enforced in the paper order path. A pause/kill switch blocks orders. Public quotes or portfolio valuations that are stale/missing are blocked rather than silently replaced by demo prices.
- The default `synthetic` market mode is for UI and strategy experimentation only. `live_public` mode uses unauthenticated public pricing for the supported watchlist, but the order engine still only creates simulated fills.
- Public CoinGecko prices are requested in INR to match the virtual portfolio currency and are cached briefly to reduce repeated polling and rate-limit pressure. Unsupported or stale holdings make valuation incomplete and block new orders.
- An optional AI panel is advisory-only. To enable it, set `OPENAI_API_KEY` in your local PowerShell session before launching Uvicorn; optionally set `OPENAI_MODEL` and `OPENAI_BASE_URL`. The key is not accepted by the browser and is not written to SQLite. AI output is never parsed into orders.

Example (current PowerShell session only; the key is not saved permanently):

```powershell
$env:OPENAI_API_KEY = "your-key-here"
$env:OPENAI_MODEL = "gpt-4o-mini"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Operations and production-hardening notes

- Keep the app behind localhost or a trusted reverse proxy with authentication; the dashboard endpoints are not an authenticated multi-user service. Do not expose this instance directly to the public internet.
- Run a single Uvicorn worker while the in-process paper scheduler is enabled. Multi-worker deployment can create duplicate schedulers and paper cycles.
- Back up `paper_agent.db` and its backup file before upgrades. Database state is local and intentionally excluded from Git; this repository workflow never edits or commits either file.
- Configure an explicit dependency update process and review CI on every proposed change. CI validates unit tests and syntax on Linux and Windows, but is not a substitute for a browser/API smoke test with your existing local data.
- Automated mode is opt-in, public-data-only, and paper-only. If market data is stale/incomplete, the executor must stop rather than switch silently to demo prices.

## Multi-market paper terminal

The dashboard also includes a separate USD-denominated multi-market paper account and a market dropdown:

- **Crypto:** public Binance USDT spot scanner and completed candles.
- **Stocks:** selected US equities using Yahoo Finance's public chart endpoint.
- **Forex:** selected major pairs quoted in USD (EUR/USD, GBP/USD, AUD/USD, NZD/USD).
- **Commodities:** gold, silver, WTI, Brent, and natural-gas futures as fractional reference units.
- **Indices:** US index references (S&P 500, NASDAQ Composite, Dow Jones) quoted in USD.
- **Prediction markets:** active Polymarket binary-outcome probabilities via the public Gamma API. Manual paper orders are available; automated strategy cycles are deliberately disabled until reliable historical candles are supported.

The multi-market account starts with **$30 virtual USD on a fresh database** and is held in new `multi_market_settings` and `multi_market_trades` tables. It is separate from the legacy crypto ledger; startup only adds missing tables/settings and does not reset, overwrite, or migrate existing trade history. Existing database settings are retained. The new account uses one virtual USD balance shared across the listed market categories, public prices, simulated fractional units, fees/slippage assumptions, and risk guardrails.

New read-only / paper-only routes:

- `GET /api/multi-market/catalog`
- `GET /api/multi-market/instruments?market_type=stocks&limit=30`
- `GET /api/multi-market/portfolio`
- `GET /api/multi-market/settings` and `PATCH /api/multi-market/settings`
- `POST /api/multi-market/orders`
- `GET /api/multi-market/trades`
- `GET /api/multi-market/auto/status` and `POST /api/multi-market/auto/run`
- `POST /api/multi-market/pause` / `POST /api/multi-market/resume`

Automated multi-market paper trading is off by default, runs on a 60-second scheduler after the selected market is enabled, and uses completed-candle signals for supported Yahoo/crypto market feeds. If a quote or candle is stale/missing, the engine withholds the trade rather than replacing it with demo data. Each order is virtual-only. Market references, especially futures and indices, are fractional simulation units and should not be interpreted as executable broker contracts. Public provider outages or rate limits can temporarily make a category unavailable.

**Prediction-market automation remains disabled** because a reliable completed-candle history adapter is not yet enabled. This is intentional, not a silent fallback. No authenticated brokerage/exchange endpoints, wallets, API credentials, or live-order routes are introduced.

## Important readiness boundary

This remains a paper-trading research project, not a certified production trading service. Before relying on it for unattended testing, run the full tests and Windows smoke test locally, review the provider's API terms/rate limits, and monitor the app during a long paper session. No profitability is implied. Live trading, exchange credentials, wallets, and real order execution are deliberately out of scope.


## Autonomous paper research features

The current `main` branch includes:

- `GET /api/intelligence/universe`: ranks Binance public USDT spot tickers by 24-hour liquidity, momentum, trading activity, and an extreme-range penalty. It excludes stablecoin quotes and common leveraged-token suffixes. The score is a ranking heuristic, not a probability of profit.
- `GET /api/intelligence/news`: reads public CoinDesk and Cointelegraph RSS feeds with a five-minute cache and an explainable keyword sentiment score. RSS availability and source terms can be noisy; headlines are not a reliable prediction model.
- Automated paper cycles can select the highest-ranked supported assets and block a new BUY when a directly matched recent headline has sufficiently negative lexicon sentiment. Positive headlines never force a BUY. The current execution universe is restricted to symbols with configured price mappings in the existing paper ledger; the scanner can display a broader universe than the executor supports.
- `POST /api/research/walk-forward/{symbol}`: fetches completed public Binance candles and reports chronological train, validation, and test windows with configured simulated fees and slippage.
- `POST /api/research/walk-forward`: runs the same research report on supplied OHLCV candles.

### Research and operating caveats

This is still **paper-only research software, not a certified production trading system**. The walk-forward endpoint compares four simple candidate strategies on chronological training and validation windows, selects using a validation return/drawdown score, then evaluates the selected candidate on a held-out test window. This is a small heuristic selection procedure, not robust machine learning or proof of edge. The current backtest is an educational close-to-next-close model and still needs independent review of short collateral, funding, liquidation, exact segment accounting, and survivorship bias before its metrics are trusted. News scoring is a small keyword heuristic, not a trained financial NLP model. The public market scanner is not a guarantee that a listed asset is tradable through every venue.

Minimum starting balance for paper trading is **₹0 real money**; the virtual balance is configurable and only exists in SQLite. No Binance API key, signed request, wallet, or real order is present. Do not enable real trading by merely adding credentials; a separate reviewed execution adapter and a new safety review would be required.
