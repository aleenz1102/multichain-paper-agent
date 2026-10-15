# Free native Python opportunity ranking

The multi-market paper engine uses deterministic local Python scoring. No AI provider,
API key, model download, or additional package is required.

The ranker orders a bounded shortlist (up to 12 instruments), while retaining every symbol with a bot-managed open position, using the market snapshot
fields available from public-data adapters:

- 40%: reported 24-hour momentum, capped so extreme moves do not dominate.
- 25%: relative reported volume within the shortlist, normalized logarithmically.
- 20%: quote freshness/data quality.
- 15%: reported candle-history depth.

The API endpoint `GET /api/multi-market/ranking/status` reports that native ranking is
available. Automated cycle responses include `opportunity_ranking` and `candidate_order`,
including per-candidate score and reasons.

## Safety boundary

- Ranking only reorders candidates; it does not generate a buy/sell signal.
- Existing completed-candle strategy gates, ATR stop/target plan, freshness validation,
  virtual order checks, position sizing, and risk limits remain authoritative.
- Scores are not calibrated probabilities of profit. Validate with historical and
  out-of-sample paper results before drawing conclusions.
- All orders remain paper-only; no real-money adapter is introduced.
- No database rows, balances, settings, or backup files are reset or migrated.
