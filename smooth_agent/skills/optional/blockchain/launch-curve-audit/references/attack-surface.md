# Attack surface by phase

Ordered by phase, because the phase decides the attacker's tools. Run each row against a fork with
funded actors; a row you cannot test is `unproven`, not clean.

---

## Phase A — pre-launch (sale deployed, not open)

The sale contract often sits live and funded before it opens. This is the highest-density window.

| # | Attack | Detect |
|---|---|---|
| A1 | Donate WBNB to the pair and `sync()` so the launch skim is skipped and the pool opens above the intended price | read the skim branch; is the ORE side computed from `lpBnb` or from reserves? |
| A2 | Donate the sale token directly to the pair to move the opening price down | same read, opposite side |
| A3 | Buy the entire early curve from one wallet before the UI opens | is there an `openTrading`/`start` flag, and can it be front-run? |
| A4 | Satisfy the stake gate with a large one-block stake before a top-up unlocks | is `stakeTime` reset on top-up, or weighted? |
| A5 | Pre-register identities after caps are computed but before they are frozen | when are caps snapshotted, and by whom? |
| A6 | `sync()` the pair repeatedly to prevent a skim from ever running | does the skim check `reserve == 0` only? |
| A7 | Place a dust bid to sit first in the book, then fill late | book ordering: price-time, or storage order? |
| A8 | Move the pair address to a wallet before launch via `setAmmPair` | is the setter restricted to factory pairs, one-shot, or live forever? |

## Phase B — during the sale

| # | Attack | Detect |
|---|---|---|
| B1 | Step-split purchases to pay below the integral | `cost(n)` vs `cost(n/2)*2`, and `curve_math.py --check` |
| B2 | Sandwhich any swap the sale performs with its own balance (`swapFees`) by triggering it when large | is the swap permissionless? is `amountOutMin` derived from reserves? |
| B3 | Reenter a refund or a bid cancel to double-count escrow | CEI, `nonReentrant`, refund accounting sides |
| B4 | Refund to a contract that reverts on receive, freezing the queue | does the refund loop isolate per-record, or revert all? |
| B5 | Refund to a contract that consumes all gas | is there a `try/catch` or a gas cap? |
| B6 | Cap snipe: fill the last unit in the final seconds to close a sale the team wanted open | intended? record it as behaviour |
| B7 | Buy at the cap to make every later purchase revert (overshoot rather than truncate) | cap boundary test |
| B8 | Use a smart wallet to dodge or exploit a `tx.origin` gate | EIP-7702 batching, 4337 bundlers |
| B9 | Overpay to push `raised` past the budget with a final purchase | last-unit budget check |
| B10 | Two identities, one wallet, to break a per-identity cap | book per identity and per wallet |

## Phase C — graduation

| # | Attack | Detect |
|---|---|---|
| C1 | Take the LP because it is custodied in an intermediate address | trace the LP holder at graduation; must be burn or verified locker |
| C2 | Prevent graduation by never calling launch, if it is owner-gated | `onlyOwner` on launch, or a registry read ahead of the deadline |
| C3 | Grace-period launch after a failed sale to mint into a pool at a stale price | is launch permissionless and unconditional after the deadline? |
| C4 | Mint into the pool above the budget because the seed is computed from live reserves | seed arithmetic |
| C5 | Leave `tradingOpen` false after graduation so nobody can sell into the pool | is the release in the same transaction? |
| C6 | Graduate while a token pause is active | pause interaction with launch |

## Phase D — post-launch coupling

| # | Attack | Detect |
|---|---|---|
| D1 | Owner mints the unissued supply in one transaction and dumps into the new pool | owner out of the minter set? minter list locked? |
| D2 | Off-chain reward amount over-issued until claims revert past the cap | on-chain budget, monotone carry-over, `totalAllocated` |
| D3 | Swap the minted token for a different one via a setter | immutable token? |
| D4 | Raise the submit/entry fee against unlimited approvals | `require` bound on the fee |
| D5 | Grow a reward array until a claim exceeds the block gas limit | cost per entry vs rounds per day |
| D6 | Mark an exchange deposit address as a pair to tax deposits | `setAmmPair` restricted to factory pairs |
| D7 | Pause transfers after the pool is seeded | live `paused`, and who can flip it |
| D8 | Change the tax to the cap right before a large sale | bound and timelock on the tax setter |

## Phase E — custody

| # | Condition | Why it is graded |
|---|---|---|
| E1 | Owner is an EOA across token, sale, pool | one key flips every path above |
| E2 | Proxy with an EOA admin, or a live `upgradeTo` | the audited code is not the deployed code tomorrow |
| E3 | `setMinter` still live after the list was "locked" | the lock is cosmetic |
| E4 | Multisig without a timelock | fast enough to front-run users |
| E5 | Fee recipient set to an EOA with no rotation path | stuck fees, no recovery |

## Scoring the surface

Count rows that fail, weighted: A1/A2 (opening price), B1 (undercharge), B3 (escrow), C1 (LP),
D1 (post-launch mint) are each sufficient for BLOCK or HOLD on their own. Everything else is
accumulative.
