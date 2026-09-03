# SentinelOS Execution Guardrails

## Operating Mode

SentinelOS operates strictly in Dry-Run/Testnet mode.

All Binance connectivity is pinned to official Testnet hosts
(`testnet.binance.vision` for Spot, `testnet.binancefuture.com` for Futures).
Configuration and runtime checks are fail-closed: any attempt to use a
production API host raises `RuntimeError` and the action is aborted.

SentinelOS is built on **Binance Agent OS & MCP (Model Context Protocol)**
architecture. Official hosted MCP (`https://agent.binance.com/mcp/agentic`) is
used for capability discovery only and is never a bypass around these
guardrails.

**Local MCP wrapper (required for trade-related actions).** Binance's Testnet
guidance is to run your own MCP tool wrapper/server for trades. SentinelOS
exposes Spot/Futures Testnet tools on the official Python MCP SDK server
`mcp_server.py` (`sentinelos-testnet-mcp`). The CLI is an MCP host
(`core/mcp_host.py` + `mcp.Client`). The agent must not call Binance REST
wrappers directly. Every tool JSON includes `"environment": "testnet"`.

## Human-in-the-Loop Authorization

All irreversible actions (e.g., executing a trade) require explicit user confirmation.

No state-changing request — including order placement, order cancel that
cannot be safely undone, transfer, or withdrawal — may be sent until a
human has reviewed the intended action and confirmed it. The agent must
present venue (Spot or Futures Testnet), symbol, side, quantity, order
type, estimated notional, and any SL/TP, then wait for an affirmative
confirmation (`Y`).

Missing, implied, ambiguous, or timed-out confirmation is treated as denial.
The default path is observe and recommend, not execute.

## Risk Limits

Maximum exposure limit: 10% of portfolio per trade **or** 1000 USDT notional,
whichever is smaller.

These ceilings are hardcoded in `core/risk.py` and cannot be relaxed from
the environment. A proposed order whose notional value exceeds the effective
cap is rejected before it reaches the exchange, even if other parameters
were confirmed. The size must be reduced below the cap or the request
abandoned. The CLI surfaces this as a **SECURITY WARNING**.

Stop-loss and take-profit, when provided, must sit on the correct side of
the last Testnet price (BUY: SL below / TP above; SELL: SL above / TP below)
or the order is rejected.

## Emergency Kill-Switch

The `kill` command arms a process-wide latch, aborts pending tool work,
closes Testnet clients, writes a `KILL_SWITCH` audit event, and shuts the
agent down. There is no in-process resume path.

## Audit Trail

Every trade preview, confirmation, rejection, API call, risk block, and
error is appended to `audit_log.txt` with a UTC timestamp. Secrets are
never written. Operators inspect the log with the `history` command.

## Fail-Closed Defaults

- Testnet credentials must never be reused against production hosts.
- Unverified hosts, missing credentials, and unconfirmed actions abort the flow.
- Dry-run / paper execution is the only permitted automated path; live
  production trading is out of scope for SentinelOS on Track A.
- HTTP clients used for Futures do not follow redirects, so a Testnet URL
  cannot be bounced onto `fapi.binance.com`.
