---
name: ai-agentic-mint-radar
description: Every 30 minutes, scan for new "AI agentic" / ".ai agent" projects with a free or paid mint on BNB Chain or another EVM chain, keep only projects that pass five quality filters and a contract risk score, then alert the owner. Read-only — never mints, approves or signs. Use when asked to watch or scan for new AI-agent mints, or when a 30-minute routine runs it.
platforms: [linux, macos, windows]
requires_bins: [python3]
metadata:
  smooth:
    tags: [mint, radar, AI-agent, BNB, BSC, EVM, scan, honeypot, rug, scam-check, security, alert]
    category: blockchain
    related_skills: [bsc-contract-audit, evm, airdrop-tracker]
---

# AI Agentic Mint Radar (30-minute scan)

Goal: alert only on projects that **pass every filter** below. If nothing passes, stay silent (never
send "no updates"). This skill is **read-only** — it never mints, approves, or signs anything, and
never asks for a private key, seed phrase, or session token.

## 0. One-time setup (Smooth Agent)

- **Schedule it.** Add a cron routine every 30 minutes with the `cronjob` tool (or
  `smooth-agent cron add`), prompt: *"Run the ai-agentic-mint-radar skill. Stay silent if nothing
  passes."* Deliver results to a channel:

  ```
  cronjob  action:create  name:mint-radar  cron:"*/30 * * * *"  deliver:telegram  \
           prompt:"Run the ai-agentic-mint-radar skill. Stay silent if nothing passes."
  ```

- **Keep state.** Store `seen.json` (handles and contracts already alerted) in the workspace with the
  `files` tool, so duplicates stay quiet. Re-alert only on a status change (mint just opened, mint
  almost sold out).
- **Tools to use.** `web` for search; `http_request` for APIs (X/Twitter recent-search if an
  `X_BEARER_TOKEN` is configured, a public EVM RPC, a block explorer, and a token-security API such as
  GoPlus or Honeypot.is); `scripts/rpc.py` from the bundled `evm` or `bsc-contract-audit` skill for
  on-chain reads; `send_message` to alert the owner.
- **Config (optional).** `CHAINS` (default BNB Chain), `MIN_MENTIONS` (default 10), `MIN_FOLLOWERS`
  (default 1000), `TIMEZONE`, alert destination (chat / Telegram / Slack).
- **Public RPC.** e.g. `https://bsc-dataseed.bnbchain.org` — send a `User-Agent` header.

## 1. Scan the feed (last 30–60 minutes)

Queries (combine with OR, drop reply and retweet spam):

- `"ai agentic" (mint OR "free mint" OR launch OR presale)`
- `".ai agent" OR "agent id" OR "agentic wallet" (mint OR claim)`
- `"ai agent" ("free mint" OR "fair mint" OR "public mint") (bnb OR bsc OR base)`
- `"agentic" (mint OR airdrop OR whitelist) -giveaway`

Add `-is:retweet` and take at most 25 results per query. Collect candidates: project handle, website
link, mint link, contract address (0x + 40 hex).

## 2. Filter 1: legit project

Passes only if all of these hold:

- The project's account is **verified** (blue/gold) and has at least `MIN_FOLLOWERS`. Record **name,
  @username, follower count**.
- It has a **website on its own domain**. **Reject** free hosting: `*.vercel.app, *.netlify.app,
  *.github.io, *.pages.dev, *.web.app, *.firebaseapp.com, *.carrd.co, *.wixsite.com, *.webflow.io,
  *.gitbook.io, *.notion.site, linktr.ee, *.replit.app, *.glitch.me, *.blogspot.com, *.wordpress.com`.
- The site is actually live (HTTP 200, not a parked or empty page).

## 3. Filter 2: mentioned by 10–20+ distinct accounts

- Search `@handle OR "domain.com" OR <contract>` over the last 24 hours.
- Count **unique accounts** (exclude the project itself; drop likely bots: account < 30 days old,
  < 50 followers, or identical copy-paste text).
- Passes at **≥ MIN_MENTIONS** unique accounts. Record who mentioned or discussed it: @username +
  followers + post link (sorted by followers, show at most 20).

## 4. Filter 3: mint type

From the official site, docs, official posts, or contract reads:

- **Free mint**: price 0 (gas only), or `price()/costOf()` returns 0.
- **Paid mint**: record unit price + payment token (BNB/USDT/ETH).
- Also record requirements (whitelist, Agent ID, per-wallet cap), open and close times, and the
  official mint link (must be on the same domain as the official site).

## 5. Filter 4: contract scan

For every contract (mint and token):

- **Verified source** on the explorer. Unverified counts as ⚠️ and is never called safe.
- On-chain reads via `eth_call`: `name()`, `symbol()`, `totalSupply()`, `owner()`, `paused()` where
  present.
- Honeypot and tax check via GoPlus
  (`https://api.gopluslabs.io/api/v1/token_security/<chainId>?contract_addresses=<addr>`) or
  Honeypot.is: honeypot, buy/sell tax, mintable, owner can change balances, blacklist, upgradeable
  proxy.
- Red flags: owner not renounced with `mint`/`setFee`/`blacklist` powers, upgradeable proxy, unlocked
  LP, top holder > 30%.
- **How many have minted**: read `totalMinted()/totalSupply()/minted()` or count `Transfer` events
  from `0x0` / `Minted` events. Show "X of Y (Z%)" when max supply is known.

> For a deep per-contract verdict, hand the address to the **`bsc-contract-audit`** skill (BSC) — it
> runs the full gates, scoring and the `DECISION: GO | CAUTION | BLOCK` line.

## 5b. Risk score (100 points)

Score every contract with this rubric. Prefer **RPC reads** (`eth_call`, `eth_getCode`,
`eth_getStorageAt`, `eth_getLogs`, `eth_getBalance`) as the source of truth; use the explorer only for
verified source and the security API as a cross-check. Award points only for what you actually proved;
anything unverifiable scores 0 for that item.

| Category | Pts | Full points only if |
| --- | --- | --- |
| **Contract security** | 30 | Source verified as an **exact match** (bytecode from `eth_getCode` matches the verified build); **zero** owner controls: no pause, blacklist, hidden or extra mint, fee/limit setters, or upgrade path |
| **Investor protection** | 25 | Deployer/owner **cannot withdraw** raised funds or user deposits; holders have a working exit (sell, redeem, or refund path); no funds locked without an exit; any refund terms are **enforced in code**, not just promised |
| **Anti-manipulation** | 20 | No insider/team allocation outside disclosed vesting; no discretionary timing (owner can't start, stop, or reprice at will); vesting is on-chain; holders not concentrated; liquidity locked or burned; no wash-trading signals |
| **Transparency** | 15 | Website claims (supply, price, refund, tax, vesting) **match the code**; team and/or audit publicly disclosed |
| **Market viability** | 10 | Real liquidity depth, organic volume, healthy holder spread, no abnormal price action |

### How to check over RPC
- **Exact match**: compare `eth_getCode(addr)` with the explorer's verified runtime bytecode (ignore
  the metadata hash at the end). For proxies, read the EIP-1967 slots with `eth_getStorageAt`
  (`0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc` implementation,
  `0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103` admin). A non-zero admin or
  implementation means an upgrade path (0 points for upgrades).
- **Owner controls**: call `owner()`, `getOwner()`, `paused()`; a zero address is renounced. Scan the
  bytecode/ABI for selectors such as `pause()` `0x8456cb59`, `unpause()` `0x3f4ba83a`,
  `mint(address,uint256)` `0x40c10f19`, `setFee`, `blacklist`, `setTax`, `withdraw()` `0x3ccfd60b`,
  `upgradeTo(address)` `0x3659cfe6`, `transferOwnership(address)` `0xf2fde38b`.
- **Withdrawable funds**: `eth_getBalance(contract)` and token `balanceOf(contract)` for raised
  BNB/USDT; check whether any owner-only function can send them out.
- **Exit and refunds**: confirm a callable sell, redeem, or refund function exists and simulate it with
  `eth_call` from a holder address where possible.
- **Holder concentration**: `eth_getLogs` on `Transfer`
  (`0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef`) to rebuild top holders; flag
  any non-LP, non-burn wallet above 10% (and > 30% as severe).
- **Liquidity lock**: read the pair (`getReserves()` `0x0902f1ac`), then the LP token's `balanceOf` for
  the dead address `0x000000000000000000000000000000000000dEaD` or a known locker contract.
- **Mint count**: `totalSupply()`/`totalMinted()` or count `Transfer` events from `0x0` / `Minted`
  events.
- **Wash signals**: many buys and sells between the same few wallets, or volume far above the number of
  unique traders.

### Grades
- **80–100 ✅ Strong**: alert normally.
- **60–79 ⚠️ Caution**: alert, list the lost points.
- **40–59 🟠 High risk**: alert only if the user opted in, with a warning.
- **< 40 ❌ Avoid**: never alert as an opportunity (optional short warning).
- **Hard fails** (score forced to 0, ❌): honeypot (cannot sell), owner can withdraw user funds, or
  hidden mint.

## 6. Alert (only projects passing filters 1–4)

Per project:

```
🚨 AI AGENTIC RADAR: <Project Name>
👤 @username · ✔️ verified · <N> followers
🌐 Web: <domain> (link)
🎟️ Mint: FREE / PAID <price> <token> · <requirements> · opens <time, timezone>
🔗 Mint link: <url>
📜 Contract: <0x…> (<chain>) · minted <X/Y (Z%)>
🛡️ Risk score: <NN>/100 <grade>
   Security <a>/30 · Protection <b>/25 · Anti-manip <c>/20 · Transparency <d>/15 · Market <e>/10
   Lost points: <short reasons or "none">
🗣️ Discussed by <M> unique accounts:
   @a (12k), @b (8.1k), @c (3.4k) … (max 20, with post links)
⏱️ <scan time, timezone>
DYOR. Not financial advice.
```

Sort projects by risk score, then mention count. Send with `send_message` to the owner's chat (or a
connected Telegram/Slack channel). Update `seen.json`.

## 7. Rules

- Never invent numbers. Followers, mentions, and mint counts must come from live data at scan time. If
  something can't be checked, write "unavailable".
- Take website and mint links only from the project's official account, never from replies (phishing
  risk).
- Never execute transactions. If the user wants to mint, that needs their explicit confirmation outside
  this skill.
