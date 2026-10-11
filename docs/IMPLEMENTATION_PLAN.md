# Multi-Chain Paper Agent — Implementation Plan

## Locked requirements
- Paper trading only: no wallet connections, signing, exchange credentials, or real order endpoints.
- Preserve `paper_agent.db`; all schema changes must be additive, idempotent migrations. Never delete history during startup or upgrade.
- Strategy: combined (trend + momentum + mean-reversion), with market-regime filtering.
- Risk profile: balanced; evaluate signals every 5 minutes.
- Market data: free public sources, timestamped and explicitly labelled. Stale/missing data blocks new entries; never silently substitute synthetic prices for live prices.
- Simulated short selling, historical backtesting, alerts, and audit logs are in scope.

## Phase 0 — Baseline and safety
1. Back up the SQLite file before migration; run a read-only inventory of tables, row counts, and schema.
2. Add automated tests around current cash/position/P&L behavior before changing accounting.
3. Keep a prominent `PAPER_ONLY` mode and make all real-execution capabilities absent, not merely hidden in the UI.
4. Add a settings/config version and idempotent migrations. Test upgrades against a copy of a populated database.

**Acceptance:** startup does not remove or rewrite historical trades; all existing endpoints still work; paper-only health indicator is explicit.

## Phase 1 — Ledger and accounting (highest priority)
1. Introduce append-only cash ledger events for initial virtual cash, top-ups, fees, trade cashflows, borrow/funding charges, and adjustments.
2. Introduce explicit position lots/position lifecycle records. Keep old `trades` rows immutable and backfill derived state without deleting them.
3. Model `BUY`/`SELL` long-side trades and `SHORT_OPEN`/`SHORT_CLOSE` separately. A short opening creates a liability/position and reserves simulated margin; covering reduces it. Reject insufficient collateral.
4. Calculate equity, free cash, reserved margin, gross/net exposure, realized P&L, unrealized P&L, fees, slippage, and funding costs consistently.
5. Treat deposits/top-ups as external cashflows, never as trading profit. Use a cashflow-adjusted return metric for performance.

**Acceptance:** ledger reconciliation identity passes; long/short open-close round trips reconcile to costs; top-ups do not change trading P&L; old trade history remains queryable.

## Phase 2 — Free live market data and data quality
1. Add a provider interface and start with a free public price/candle provider; document API limits, attribution, asset-ID mapping, and terms.
2. Store price, provider, fetched timestamp, market timestamp when available, and quality/status. Cache responses to respect rate limits.
3. Add timeouts, retry/backoff, provider health, symbol mapping validation, and outlier checks.
4. Define stale thresholds by endpoint/timeframe. Stale/missing data pauses new entries; allow exits only when a trustworthy mark/price is available, otherwise freeze automation and alert.
5. Make synthetic mode a deliberate development setting. Never label synthetic prices as live.

**Acceptance:** mocked provider tests cover timeout, 429, invalid payload, missing token, stale cache, and recovery. API/UI show source and age of data.

## Phase 3 — Strategy and market regimes
1. Build candle-based indicators with warm-up handling: moving-average trend, momentum/ROC, RSI or normalized deviation, and volatility.
2. Create interpretable component scores and a combined signal with configurable weights/thresholds.
3. Classify regimes (trend, range, high-volatility, uncertain) and enable strategies only in suitable regimes.
4. Apply cooldowns, signal persistence, liquidity/volume gates, and duplicate-order/idempotency keys.
5. Every decision records input timestamp, indicator values, regime, score, reason, and action/no-action explanation.

**Acceptance:** deterministic fixtures yield stable expected signals; no look-ahead data is used; insufficient history means no trade.

## Phase 4 — Risk engine and simulated execution
1. Balanced defaults: per-trade risk budget, per-position notional cap, portfolio gross/net exposure caps, max positions, daily loss stop, and peak-to-trough equity drawdown stop.
2. Risk checks run before every entry and again immediately before simulated fill. Exits and emergency pause have priority.
3. Simulate spread, slippage, fees, partial fills, order latency, and configurable short borrow/funding assumptions. Report estimates separately from realized costs.
4. Short-specific margin, maintenance threshold, borrow availability assumption, cover costs, and liquidation-risk warnings.
5. Global pause state and emergency stop; stale data, ledger mismatch, scheduler exception, or breached risk limit stops new entries.

**Acceptance:** tests prove every limit blocks entries at boundaries and audit logs include the exact blocking reason.

## Phase 5 — Five-minute automation and recovery
1. Use a single scheduler/leader lock so reloads or multiple workers cannot execute duplicate cycles.
2. Each cycle: validate provider health → load candles → calculate regime/signals → evaluate risk → simulate eligible orders → persist decisions → emit alerts.
3. Persist scheduler state, last successful cycle, and idempotency keys. On restart, recover open positions and resume only after data/ledger checks pass.
4. Default automation OFF after first install and after unrecoverable errors; require explicit user start. Pause/resume is visible in the UI.
5. Do not promise uninterrupted running in Codespaces; stopped/suspended environments cannot execute scheduled cycles.

**Acceptance:** restart test produces no duplicate orders; scheduler errors auto-pause; manual pause takes effect immediately.

## Phase 6 — Backtesting and analytics
1. Reuse the exact strategy/risk/execution components used by paper automation.
2. Use timestamp-ordered historical OHLCV, no look-ahead bias, realistic fees/slippage, and explicit short financing assumptions.
3. Add train/validation/test periods or walk-forward evaluation; avoid tuning on the final test period.
4. Report total return, cashflow-adjusted return, annualized volatility, Sharpe/Sortino where sample size permits, max equity drawdown, profit factor, expectancy, turnover, exposure, win rate, and trade count.
5. Compare against buy-and-hold and a no-trade baseline; disclose data gaps and assumptions.

**Acceptance:** hand-calculated toy series match expected fills and metrics; strategy results reproduce from a saved configuration and dataset fingerprint.

## Phase 7 — Alerts, audit, and operations
1. Append-only audit events for settings, signals, blocked orders, orders, fills, data health, pauses, migrations, and recoveries.
2. In-app alert center first; optional external alerts can be added without storing secrets in source control.
3. CSV exports for trades, ledger, signals, backtests, and audit events.
4. Add health/status endpoint, DB backup guidance, log rotation, and an operational runbook.

**Acceptance:** all material state changes have audit events; export output is stable and contains no secrets.

## Delivery sequence
- Deliver 1: plan, baseline tests, accounting/position model and safe migration.
- Deliver 2: live provider + quality gates.
- Deliver 3: strategy/regime + risk engine.
- Deliver 4: scheduler/recovery + simulated execution.
- Deliver 5: backtesting, analytics, alerts, UI integration.

Do not enable unattended automation until phases 1–4 pass their tests. Do not infer profitability from backtests or simulated results.
