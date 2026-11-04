# Invariant template

Write these as Foundry invariants on a fork, with randomized call order, several funded actors, and
repeated identities. Replace `<sale>`, `<token>`, `<pool>` with the deployment.

An invariant that needs a specific caller is not an invariant — note the caller and move it to the
checklist.

---

## Supply and cap

- **INV-1** `totalSupply() <= maxSupply()` after any sequence of buys, mints, burns, penalties.
- **INV-2** The sale mints exactly the booked sold amount at graduation, and the unsold remainder is
  burned or stays with the treasury by a stated rule.
- **INV-3** For any purchase range, `cost(a,b) >= closed_form(a,b)` — the buyer never pays less than
  the integral.
- **INV-4** `raised <= hardCap` and `sold <= saleSupply`, with the last unit admitted.
- **INV-5** Price is monotone non-decreasing in `sold`, and the endpoint equals the stated multiple.

## Escrow (when a bid book exists)

- **INV-6** `escrowTotal + feesAccrued == address(this).balance` at every step.
- **INV-7** Booked is cumulative, not per-record: refunding one record decrements both sides once.
- **INV-8** A refund that reverts in the recipient cannot block the queue.
- **INV-9** Filled bids cannot be re-filled, re-refunded, or cancelled.

## Graduation

- **INV-10** After the deadline, launch succeeds for any caller, including on an empty sale.
- **INV-11** The opening price equals the formula price regardless of pre-launch donations (donate,
  `sync()`, then compare).
- **INV-12** The LP is held by the burn address or a verified locker after launch, and no address can
  move it.
- **INV-13** `tradingOpen` is true in the same transaction that seeds the pool.
- **INV-14** Launch cannot run twice, and cannot run before the deadline.

## Emission coupling

- **INV-15** `totalAllocated() <= emissionCap()` after any claim sequence, both reward paths included.
- **INV-16** `emissionCap()` carries unused budget forward and never decreases.
- **INV-17** `emissionCap() == 0` before the first period starts.
- **INV-18** Claim gas does not grow with the number of accumulated entries.
- **INV-19** `isMinter(owner) == false` and `mintersLocked() == true` after the lock, and
  `setMinter` is inert.

## Token wiring

- **INV-20** Before launch, a transfer succeeds only wallet→wallet, and the pair holds no balance.
- **INV-21** `tax <= taxCap` for every transfer, both directions.
- **INV-22** A failing tax swap never reverts the user's transfer.
- **INV-23** `setAmmPair` accepts only factory-derived pairs, or is frozen after launch.

## Custody and config

- **INV-24** Owner is a Safe behind a timelock, or the delta is recorded as an accepted risk.
- **INV-25** The minted token address is immutable, or the setter is one-shot and already burned.
- **INV-26** Every user-facing parameter is `require`-bounded at set time.

---

## Reading a failure

- Holds on a fresh fork, fails after randomized calls → state accumulation; widen the run length.
- Fails only with a specific actor type (contract, smart wallet, multisig) → an access assumption, not
  necessarily a bug; grade it.
- Needs a whitelisted caller → record the caller in the report; it is a trust dependency.
- Fails only at the cap boundary → re-test `cap-1`, `cap`, `cap+1`, and `remaining+1`.

## Do not assert these as bugs

- Tax charged on add/remove liquidity — usually by design.
- `tx.origin` gating blocking smart wallets — accepted trade-off, unless a multisig is meant to join.
- Permissionless trigger functions that only help (e.g. a public `swapFees`), as long as slippage is
  protected.
- Book-only access for wallets without an identity, if the docs say so.
