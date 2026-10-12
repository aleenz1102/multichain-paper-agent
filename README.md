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

## Automated paper mode (feature branch)

The neon dashboard and opt-in scheduler are being developed on `feat/neon-paper-agent-dashboard` and tracked in [PR #3](https://github.com/aleenz1102/multichain-paper-agent/pull/3). These changes are not merged into `main` yet.

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

- Automated paper mode is **disabled by default**. Enable it explicitly from the dashboard; then the in-process scheduler evaluates the supported crypto symbols every five minutes while the engine is not paused.
- Keep Uvicorn at one worker while using the in-process scheduler. Multiple workers/processes can each start their own scheduler; this build is not a distributed scheduler.
- Daily realized-loss, per-entry size, max concurrent positions, max portfolio exposure, and portfolio drawdown checks are enforced in the paper order path. A pause/kill switch blocks orders. Public quotes or portfolio valuations that are stale/missing are blocked rather than silently replaced by demo prices.
- The default `synthetic` market mode is for UI and strategy experimentation only. `live_public` mode uses unauthenticated public pricing for the supported watchlist, but the order engine still only creates simulated fills.
- An optional AI panel is advisory-only. To enable it, set `OPENAI_API_KEY` in your local PowerShell session before launching Uvicorn; optionally set `OPENAI_MODEL` and `OPENAI_BASE_URL`. The key is not accepted by the browser and is not written to SQLite. AI output is never parsed into orders.

Example (current PowerShell session only; the key is not saved permanently):

```powershell
$env:OPENAI_API_KEY = "your-key-here"
$env:OPENAI_MODEL = "gpt-4o-mini"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

### Important readiness boundary

This remains a paper-trading research project, not a certified production trading service. Before relying on it for unattended testing, run the full tests and Windows smoke test locally, review the provider's API terms/rate limits, and monitor the app during a long paper session. No profitability is implied. Live trading, exchange credentials, wallets, and real order execution are deliberately out of scope.
