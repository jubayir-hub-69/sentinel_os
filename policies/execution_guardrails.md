# SentinelOS Execution Guardrails

## Operating Mode

SentinelOS operates strictly in Dry-Run/Testnet mode.

All Binance connectivity is pinned to official Testnet hosts
(`testnet.binance.vision` for Spot, `testnet.binancefuture.com` for Futures).
Configuration and runtime checks are fail-closed: any attempt to use a
production API host raises `RuntimeError` and the action is aborted.

## Human-in-the-Loop Authorization

All irreversible actions (e.g., executing a trade) require explicit user confirmation.

No state-changing request — including order placement, order cancel that
cannot be safely undone, transfer, or withdrawal — may be sent until a
human has reviewed the intended action and confirmed it. The agent must
present venue (Spot or Futures Testnet), symbol, side, quantity, order
type, and estimated notional, then wait for an affirmative confirmation.

Missing, implied, ambiguous, or timed-out confirmation is treated as denial.
The default path is observe and recommend, not execute.

## Risk Limits

Maximum exposure limit: 10% of portfolio per trade.

A proposed order whose notional value exceeds 10% of current Testnet
portfolio equity is rejected before it reaches the exchange, even if other
parameters were confirmed. The size must be reduced below the cap or the
request abandoned.

## Fail-Closed Defaults

- Testnet credentials must never be reused against production hosts.
- Unverified hosts, missing credentials, and unconfirmed actions abort the flow.
- Dry-run / paper execution is the only permitted automated path; live
  production trading is out of scope for SentinelOS on Track A.
