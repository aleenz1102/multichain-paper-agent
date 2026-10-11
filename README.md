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
- The daily loss circuit breaker uses realized P&L from simulated SELL orders for the current UTC date; it does not yet auto-trigger on unrealized losses.
- Demo market prices update deterministically on each request. This is not a live market feed.
- Profit metrics use completed paper trades and are indicative only.

## Safety
- No live-trading code exists in this starter.
- Do not add private keys or exchange secrets to this repository.
- Do not interpret simulated backtests or demo signals as investment advice.
