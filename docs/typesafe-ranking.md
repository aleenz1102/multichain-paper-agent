# Optional TypeSafe AI candidate ranking

TypeSafe is an optional **advisory ranking layer** for the multi-market paper engine.
It uses the official Python SDK (`typesafe-sdk`) and a bounded list of at most 12
instrument snapshots to prioritize candidates by observed activity, liquidity, and
momentum. It does not estimate guaranteed returns.

## Configure privately

Set these variables in the process environment or the ignored local `.env` file:

```dotenv
TYPESAFE_API_KEY=your_private_key_here
TYPESAFE_RANKING_ENABLED=1
```

Never commit `.env`, paste a key into GitHub, or put a key in browser JavaScript.
The integration does not log the key. It reports only whether a key is configured.
Keep TypeSafe disabled by omitting the enable flag or setting it to `0`.

Install dependencies with `python -m pip install -r requirements.txt`.
The backend status endpoint is `GET /api/multi-market/typesafe/status`.
The most recent automated cycle includes `typesafe_ranking` and `candidate_order`.

## Decision boundary

- TypeSafe can reorder the bounded candidate shortlist and add a research score.
- The deterministic completed-candle signal, trend/regime gate, ATR trade plan,
  fresh quote checks, position sizing, daily loss stop, exposure limit, and drawdown
  protections remain authoritative.
- TypeSafe scores never authorize orders or relax risk limits.
- Missing keys, disabled configuration, SDK/network errors, or malformed responses
  retain the original candidate order.
- No real-money order adapter, wallet, exchange key, or live execution is added.
- Existing database rows, account balances, risk settings, and backups are not migrated
  or reset by this integration.

## Validation

Unit tests mock the TypeSafe client; they do not need an API key or make billable API
calls. Before enabling the optional ranking in unattended runs, compare its ranking
with subsequent outcomes on a held-out paper-trading period. A score or confidence is
not a probability of profit.
