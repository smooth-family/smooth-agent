---
name: airdrop-tracker
description: Track airdrop eligibility, claim deadlines and token unlocks for the owner's own wallets — with change alerts and scam checks.
platforms: [linux, macos, windows]
metadata:
  smooth:
    tags: [airdrop, crypto, claim, vesting, deadlines, watch]
    category: blockchain
---

# Airdrop tracker

Keep the owner on top of the airdrops they're farming with their **own** wallets:
what they're eligible for, when a claim window opens or closes, when tokens
unlock, and whether a claim page is safe. Alert only when something changes.

This is tracking and safety for the owner's own addresses — not multi-account or
identity-evasion advice. If asked for that, decline and offer legitimate tracking
instead.

## Set up a watch per project

For each project the owner cares about, record the essentials with `memory`
(wallet address, project, official claim URL, any known dates) so they resurface.
Then watch the thing that changes:

- **A claim/points page or eligibility API** → `web_watch` it. The scheduler
  only wakes the model when the page actually changes, so this is cheap:

  ```
  web_watch  action:create  name:project-claim  url:https://airdrop.example/claim?addr=0x...  every:6h  deliver:telegram
  ```

- **A status API returning JSON** → `web_watch` with a `json_path`/`select` so a
  tiny field change (eligible: false → true) triggers an alert and nothing else.

## Deadlines and unlocks on the calendar

When a claim window or unlock date is known, don't rely on memory — put it where
it will nag:

- Near-term claim deadline → a `cronjob` that reminds a day and an hour before
  (deliver to chat), or a `calendar` event (asks first).
- Token unlock / vesting cliff → a reminder a few days ahead so the owner can
  plan around sell pressure. State the date and the owner's timezone explicitly.

## A reusable farming check

Save the routine as a workflow so the owner can run it per project by name:

```
workflow create
  name: airdrop-check
  description: Eligibility + deadline + safety check for one project
  steps: |
    1. Check {{project}}'s official claim/eligibility page for {{wallet}} and
       report the current status.
    2. Note any claim window or unlock date and how long until it.
    3. Run the safety checks below on the claim URL before suggesting any action.
  params: [{"name": "project"}, {"name": "wallet"}]
```

## Safety checks (always, before any claim)

Claiming is where people lose funds. Before the owner interacts with a claim
page, verify and warn plainly:

- **Official source** — confirm the domain against the project's known channels;
  typosquats and look-alike domains are the usual trap. Flag anything that does
  not match what's in `memory`.
- **No seed phrase, ever** — a real claim never asks for a seed phrase or private
  key. If a page does, stop and warn.
- **Read the approval** — a claim should not ask to approve unlimited spend of
  unrelated tokens. If the owner can read the contract/approval, help them check
  what it grants (use the contract-reading tools if available).
- When unsure, tell the owner to pause rather than claim.

## Exit note

When tokens are claimable or unlocking, give a neutral, factual picture (amount,
date, where it trades) so the owner decides. Don't give financial advice or price
predictions; surface the facts and the deadlines.
