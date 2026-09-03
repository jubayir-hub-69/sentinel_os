---
name: binance
title: Binance Testnet (SentinelOS)
description: |
  Routes all Binance Spot and USD-M Futures trading intents exclusively through
  SentinelOS local Testnet tools. Use when the user asks to check Testnet balances,
  analyze a Testnet symbol, preview or submit a Spot/Futures Testnet trade with
  optional SL/TP, inspect audit history, or trigger the kill-switch. Requires auth.
  Production Binance APIs are forbidden.
metadata:
  version: "1.2.0"
  author: SentinelOS
  environment: testnet
  venue: spot+futures
  architecture: Binance Agent OS & MCP (Model Context Protocol)
  mcp: https://agent.binance.com/mcp/agentic
  docs: https://developers.binance.com/en/docs/llms.txt
license: MIT
---

# Binance Testnet (SentinelOS)

SentinelOS Track A skill for **Binance Agent OS & MCP (Model Context Protocol)**.
This skill gives the agent Testnet-only Spot and USDⓈ-M Futures capabilities.
It does not promote any asset and does not execute production trades.

## System Instructions

- The agent must refer to `developers.binance.com/en/docs/llms.txt` for context (full dump:
  `https://developers.binance.com/en/docs/llms-full.txt`) and to the Agent Native overview at
  `https://developers.binance.com/en/docs/agent-native/overview`.
- Utilize `https://agent.binance.com/mcp/agentic` for advanced capability discovery if needed.
- Route **all** trading intents exclusively through the local modules
  `tools/binance_testnet_tools.py` and `tools/binance_futures_testnet_tools.py`.
  Do not call `api.binance.com`, `fapi.binance.com`, or any other production host.
- SentinelOS operates strictly in Dry-Run/Testnet mode. Spot REST base URL is
  `https://testnet.binance.vision`. Futures REST base URL is
  `https://testnet.binancefuture.com`.
- All irreversible actions (e.g., executing a trade) require explicit user confirmation (`Y`).
- Maximum exposure limit: 10% of portfolio per trade **or** 1000 USDT notional, whichever is smaller.
- `kill` must trip the emergency kill-switch and shut down. Do not resume trading in-process.

## Official Context

| Resource | URL |
|----------|-----|
| Agent Native / MCP overview | https://developers.binance.com/en/docs/agent-native/overview |
| Machine-readable docs (index) | https://developers.binance.com/en/docs/llms.txt |
| Machine-readable docs (full) | https://developers.binance.com/en/docs/llms-full.txt |
| Official MCP endpoint | https://agent.binance.com/mcp/agentic |
| Binance Skills Hub | https://github.com/binance/binance-skills-hub |
| Spot Testnet REST | https://testnet.binance.vision |
| Futures Testnet REST | https://testnet.binancefuture.com |
| Python connector | `binance-connector` (`from binance.spot import Spot`) |

Before answering API-shape questions (filters, enums, errors, signed endpoints), fetch
`https://developers.binance.com/en/docs/llms.txt` and open the linked Spot / USDⓈ-M
Testnet REST pages. Use the MCP endpoint only for discovery of additional official
capabilities; never as a bypass around local Testnet guardrails.

## When to Use This Skill

| User intent | Tool |
|-------------|------|
| Show Spot Testnet balances / account | `get_spot_testnet_balance(client)` |
| Show Futures Testnet balances | `get_futures_testnet_balance(client)` |
| Technical / sentiment reading | `analyze_symbol(symbol)` |
| Preview a Spot buy/sell (optional SL/TP) | `preview_spot_testnet_order(...)` |
| Place a Spot Testnet order after confirm | `submit_spot_testnet_order(..., human_confirmed=True)` |
| Preview a Futures buy/sell (optional SL/TP) | `preview_futures_testnet_order(...)` |
| Place a Futures Testnet order after confirm | `submit_futures_testnet_order(..., human_confirmed=True)` |
| Show recent audit events | `history` / `read_recent()` |
| Emergency halt | `kill` / `get_kill_switch().trip()` |

## Mandatory Tool Routing

```python
from tools.binance_testnet_tools import (
    create_spot_testnet_client,
    get_spot_testnet_balance,
    preview_spot_testnet_order,
    submit_spot_testnet_order,
)
from tools.binance_futures_testnet_tools import (
    create_futures_testnet_client,
    preview_futures_testnet_order,
    submit_futures_testnet_order,
)
from tools.market_analysis import analyze_symbol

spot = create_spot_testnet_client()
balances = get_spot_testnet_balance(spot)
preview = preview_spot_testnet_order("BTCUSDT", "BUY", "0.001", stop_loss="58000", take_profit="62000")
# After the user explicitly confirms the preview:
result = submit_spot_testnet_order(
    spot, "BTCUSDT", "BUY", "0.001",
    human_confirmed=True, stop_loss="58000", take_profit="62000",
)

futures = create_futures_testnet_client()
f_preview = preview_futures_testnet_order("BTCUSDT", "BUY", "0.001")
f_result = submit_futures_testnet_order(
    futures, "BTCUSDT", "BUY", "0.001", human_confirmed=True,
)

reading = analyze_symbol("BTCUSDT")
```

Clients load Testnet keys and pin hosts via `core.config.get_settings()`. If
`BINANCE_ENV` is not `testnet` or the host is not an official Testnet host, the
process raises `RuntimeError`.

## Tools

| Function | Mutates funds | Behavior |
|----------|---------------|----------|
| `get_spot_testnet_balance(client)` | No | Signed Spot Testnet account. |
| `get_futures_testnet_balance(client)` | No | Signed Futures Testnet account. |
| `analyze_symbol(symbol)` | No | Live Testnet tickers + Google Gemini. Model is selected dynamically via `client.models.list()` (first flash model that supports `generateContent`). |
| `preview_*_testnet_order(...)` | No | Local dry-run. Returns `{"environment": "testnet", "status": "order_preview"}`. Enforces size limits. Does not call `new_order`. |
| `submit_*_testnet_order(..., human_confirmed)` | Yes (Testnet only) | Requires `human_confirmed is True`. Enforces 10% / $1000 cap, then submits MARKET and optional SL/TP. |

## Workflow

1. Load this skill. Restate that the venue is **Spot and/or Futures Testnet**, not production, and that SentinelOS runs on **Binance Agent OS & MCP**.
2. Refresh official API context from `https://developers.binance.com/en/docs/llms.txt` when the
   request depends on endpoint semantics, filters, or error codes.
3. Discover extra official agent capabilities from `https://agent.binance.com/mcp/agentic` only
   if the local tools cannot answer a **read-only** discovery question. Do not execute trades
   through MCP from this skill.
4. For balance questions, call the matching Testnet balance tool.
5. For `analyze SYMBOL`, call `analyze_symbol` and show the Testnet snapshot plus Gemini bullets. The model is resolved at runtime from the Gemini catalog.
6. For any intended order:
   1. Call the matching `preview_*_testnet_order` (include `--sl` / `--tp` when provided).
   2. Show venue, symbol, side, type (`MARKET`), quantity, SL/TP, estimated notional, and the 10% / $1000 rule.
   3. Ask the user to type `Y` to confirm.
   4. Treat missing, implied, ambiguous, or timed-out replies as denial.
   5. Only then call `submit_*_testnet_order(..., human_confirmed=True)`.
7. If `PermissionError` or `RuntimeError` is raised, stop. Do not retry against another host.
8. If the user types `kill`, trip the kill-switch and shut down.

## Security

- Credentials come from `.env` (`BINANCE_SPOT_TESTNET_API_KEY` /
  `BINANCE_SPOT_TESTNET_API_SECRET` / `BINANCE_FUTURES_TESTNET_API_KEY` /
  `BINANCE_FUTURES_TESTNET_API_SECRET` / `LLM_API_KEY`). Never print secrets.
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
- Do not raise quantity after confirmation. If the user changes size, symbol, SL, or TP,
  generate a new preview and confirm again.
- Reject orders whose notional exceeds 10% of current Testnet portfolio equity **or** 1000 USDT
  even if the user confirmed other parameters.
- Default path is observe and recommend, not execute.

## Notes

- ⚠️ **State-changing Testnet transactions** — always ask the user to type `Y` before
  calling submit.
- Preview JSON is authoritative for what will be sent. If preview `status` is not
  `order_preview`, do not submit.
- Connector: `pip install binance-connector`. Spot client:
  `Spot(api_key=..., api_secret=..., base_url="https://testnet.binance.vision")`.
- Futures uses a local HMAC client against `https://testnet.binancefuture.com` so the Spot
  `binance` package namespace is not overwritten.
- `/sapi/*` endpoints are not available on Spot Testnet; do not call them from this skill.

## Disclaimer

This skill is an informational Testnet tool. It does not constitute investment, financial, or
trading advice. Digital asset prices are subject to high market risk and price volatility. You
are solely responsible for evaluating information and for all trading decisions. See Binance
[Risk Warning](https://www.binance.com/en/risk-warning) and
[Terms of Use](https://www.binance.com/en/terms).
