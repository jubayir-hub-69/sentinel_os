---
name: binance
title: Binance Spot Testnet (SentinelOS)
description: |
  Routes all Binance Spot trading intents exclusively through SentinelOS local Testnet tools.
  Use when the user asks to check Spot Testnet balances, preview a Spot order, place or submit a
  Spot Testnet trade, or inspect Testnet account state. Requires auth. Production Binance APIs
  are forbidden.
metadata:
  version: "1.0.0"
  author: SentinelOS
  environment: testnet
  venue: spot
  mcp: https://agent.binance.com/mcp/agentic
  docs: https://developers.binance.com/en/docs/llms.txt
license: MIT
---

# Binance Spot Testnet (SentinelOS)

SentinelOS Track A skill for Binance Agent OS. This skill gives the agent Testnet-only Spot
capabilities. It does not promote any asset and does not execute production trades.

## System Instructions

- The agent must refer to `developers.binance.com/en/docs/llms.txt` for context (full dump:
  `https://developers.binance.com/en/docs/llms-full.txt`) and to the Agent Native overview at
  `https://developers.binance.com/en/docs/agent-native/overview`.
- Utilize `https://agent.binance.com/mcp/agentic` for advanced capability discovery if needed.
- Route **all** Spot trading intents exclusively through the local module
  `tools/binance_testnet_tools.py`. Do not call `api.binance.com`, `fapi.binance.com`, or any
  other production host. Do not use `binance-cli` against production profiles from this skill.
- SentinelOS operates strictly in Dry-Run/Testnet mode. Spot REST base URL is
  `https://testnet.binance.vision`.
- All irreversible actions (e.g., executing a trade) require explicit user confirmation.
- Maximum exposure limit: 10% of portfolio per trade.

## Official Context

| Resource | URL |
|----------|-----|
| Agent Native / MCP overview | https://developers.binance.com/en/docs/agent-native/overview |
| Machine-readable docs (index) | https://developers.binance.com/en/docs/llms.txt |
| Machine-readable docs (full) | https://developers.binance.com/en/docs/llms-full.txt |
| Official MCP endpoint | https://agent.binance.com/mcp/agentic |
| Binance Skills Hub | https://github.com/binance/binance-skills-hub |
| Spot Testnet REST | https://testnet.binance.vision |
| Python connector | `binance-connector` (`from binance.spot import Spot`) |

Before answering API-shape questions (filters, enums, errors, signed endpoints), fetch
`https://developers.binance.com/en/docs/llms.txt` and open the linked Spot Testnet REST pages.
Use the MCP endpoint only for discovery of additional official capabilities; never as a bypass
around local Testnet guardrails.

## When to Use This Skill

| User intent | Tool |
|-------------|------|
| Show Spot Testnet balances / account | `get_spot_testnet_balance(client)` |
| Preview a buy/sell without sending an order | `preview_spot_testnet_order(symbol, side, quantity)` |
| Place a Spot Testnet order after the user confirms | `submit_spot_testnet_order(client, symbol, side, quantity, human_confirmed=True)` |

## Mandatory Tool Routing

Construct the client from local configuration, then call only these functions:

```python
from tools.binance_testnet_tools import (
    create_spot_testnet_client,
    get_spot_testnet_balance,
    preview_spot_testnet_order,
    submit_spot_testnet_order,
)

client = create_spot_testnet_client()
balances = get_spot_testnet_balance(client)
preview = preview_spot_testnet_order("BTCUSDT", "BUY", "0.001")
# After the user explicitly confirms the preview:
result = submit_spot_testnet_order(
    client,
    "BTCUSDT",
    "BUY",
    "0.001",
    human_confirmed=True,
)
```

`create_spot_testnet_client()` loads Testnet keys and pins `base_url` to
`https://testnet.binance.vision` via `core.config.get_settings()`. If `BINANCE_ENV` is not
`testnet` or the host is not an official Testnet host, the process raises `RuntimeError`.

## Tools

| Function | Mutates funds | Behavior |
|----------|---------------|----------|
| `get_spot_testnet_balance(client)` | No | Signed `GET /api/v3/account` on Spot Testnet. Returns non-zero balances labeled `environment: testnet`. |
| `preview_spot_testnet_order(symbol, side, quantity)` | No | Local mock only. Returns JSON containing `{"environment": "testnet", "status": "order_preview"}`. Does not call `new_order`. |
| `submit_spot_testnet_order(client, symbol, side, quantity, human_confirmed)` | Yes (Testnet only) | Requires `human_confirmed is True`. Otherwise raises `PermissionError` and sends nothing. Enforces the 10% exposure cap, then submits a MARKET order on Testnet. |

## Workflow

1. Load this skill. Restate that the venue is **Spot Testnet**, not production.
2. Refresh official API context from `https://developers.binance.com/en/docs/llms.txt` when the
   request depends on endpoint semantics, filters, or error codes.
3. Discover extra official agent capabilities from `https://agent.binance.com/mcp/agentic` only
   if the local tools cannot answer a **read-only** discovery question. Do not execute trades
   through MCP from this skill.
4. For balance questions, call `get_spot_testnet_balance(client)` and summarize non-zero assets.
5. For any intended order:
   1. Call `preview_spot_testnet_order(symbol, side, quantity)`.
   2. Show the user venue, symbol, side, type (`MARKET`), quantity, and the 10% exposure rule.
   3. Ask the user to type an explicit confirmation (for example `CONFIRM`).
   4. Treat missing, implied, ambiguous, or timed-out replies as denial.
   5. Only then call `submit_spot_testnet_order(..., human_confirmed=True)`.
6. If `PermissionError` or `RuntimeError` is raised, stop. Do not retry against another host.

## Security

- Credentials come from `.env` (`BINANCE_SPOT_TESTNET_API_KEY` /
  `BINANCE_SPOT_TESTNET_API_SECRET`). Never print secrets.
- Testnet keys must never be reused against production hosts.
- `policies/testnet_only.py` rejects any hostname outside
  `{testnet.binance.vision, testnet.binancefuture.com}`.
- `policies/execution_guardrails.md` is the human-in-the-loop and risk policy.
- `human_confirmed=False` (the default) is denial. Do not set it to `True` unless the user has
  confirmed the exact previewed order in this turn.
- Unsigned, guessed, or agent-invented confirmation is forbidden.

## Rules

- Do not promote any coin, token, or asset. Do not present any asset as guaranteed, safe, or
  recommended.
- Do not include or share any valid wallet address.
- Do not call production Spot, Futures, Wallet, or Withdraw endpoints.
- Do not submit LIMIT/STOP/OCO/margin/futures orders through this skill; the local submit path
  is Spot Testnet MARKET only.
- Do not raise quantity after confirmation. If the user changes size or symbol, generate a new
  preview and confirm again.
- Reject orders whose notional exceeds 10% of current Testnet portfolio equity even if the user
  confirmed other parameters.
- Default path is observe and recommend, not execute.

## Notes

- ⚠️ **State-changing Testnet transactions** — always ask the user to type `CONFIRM` before
  calling `submit_spot_testnet_order`.
- Preview JSON is authoritative for what will be sent. If preview `status` is not
  `order_preview`, do not submit.
- Connector: `pip install binance-connector`. Client:
  `Spot(api_key=..., api_secret=..., base_url="https://testnet.binance.vision")`.
- `/sapi/*` endpoints are not available on Spot Testnet; do not call them from this skill.

## Disclaimer

This skill is an informational Testnet tool. It does not constitute investment, financial, or
trading advice. Digital asset prices are subject to high market risk and price volatility. You
are solely responsible for evaluating information and for all trading decisions. See Binance
[Risk Warning](https://www.binance.com/en/risk-warning) and
[Terms of Use](https://www.binance.com/en/terms).
