# Pricing the curve

Read the parameters, write the intended model, then check the code against it. Do the arithmetic
outside the contract with `scripts/curve_math.py`; never trust a comment for a price.

All examples assume wei per whole token and a supply in whole tokens.

## Curve families and their failure modes

| Family | Intended shape | Where it breaks |
|---|---|---|
| Linear P→kP | `price(x) = P(1 + (k-1)x/S)` | rounding down on cost, step-splitting, endpoint drift |
| Exponential `P·r^x` | cheap at the start, asymptote at the cap | fixed-point exponent, overshoot past the cap in one buy |
| Tranche / tiered | flat price per bracket | bracket boundary off-by-one, buying into the next bracket at the old price |
| LBP / Dutch | price decays over time | clock manipulation, buying after the decay floor |
| Fixed-price with cap | flat, cap-limited | cap admission, last-unit overshoot, refund on overflow |

Whatever the family: the cost function must be an integral over the price, monotone, and bounded by
the budget.

## The four checks that pay

**1. Undercharge by splitting.** In an increasing curve, `cost(a) + cost(b)` at consecutive ranges
should never be less than `cost(a+b)` unless the design says so. Rounding in the wrong direction
turns this into free money.

```bash
python3 scripts/curve_math.py --p <P> --supply <S> --check 1e18,10e18
# inspect the delta between total and stepped
```

Also call the contract: `cost(n)` vs `cost(n/2)` twice, and compare against the closed form. If no
quote getter exists, price from `raised`/`sold` deltas across a real transaction, or fork-call the
payable buy with `value` and read the state change. A
contract that undercuts by even 0.1% is exploitable by a bot in the first block.

**2. Overflow.** Any `p * x` before a division, with `x` in wei, overflows 256 bits once supply
approaches `2^96`. Require a 512-bit `mulDiv` (OpenZeppelin `Math.mulDiv`, Solady `fullMulDiv`) or an
explicitly documented safe range. Test at `S = 2^96` and at the cap with the largest single purchase.

**3. Cap admission.** At the cap: does a purchase revert, truncate, or overshoot? The correct answers
are revert-or-truncate by a stated rule, with the last unit inside the budget. Check the boundary at
`cap-1`, `cap`, `cap+1`, and a purchase larger than the remaining supply.

**4. Budget bypass.** A per-identity cap must be booked per identity *and* per wallet, before any
transfer. Two identities, one wallet: buy under both caps. Then one identity, two wallets. Then a
contract that re-enters. If the cap is checked against a balance, a transfer mid-purchase resets it.

## Opening price

Graduation usually seeds a pool at a formula price, e.g. `20/3·P`. The seed must be computed from
*booked* values (`lpBnb`), not from `getReserves()`, because reserves can be moved before launch:

- send WBNB to the pair, call `sync()` → reserves read `(0, X)`, the skim branch is skipped, the pool
  opens higher;
- send ORE directly to the pair → the ORE side is inflated and the opening price moves down.

Check: is the pair address derived from the factory, or set by an admin? Is the seed computed before
or after any reserve read? Is there a `skim` that a donation can disable?

## Deadline and graduation timing

- Launch must be permissionless after the deadline, including on an unsold, empty sale.
- A pause or an external registry read ahead of the deadline check can strand a sale forever.
- A deadline of `0` or `type(uint256).max` means "no deadline" — say so.

## Reading the numbers

Convert everything to BNB and to tokens. A price with 18 decimals is unreadable; a USD figure is
drift. Report:

- start and end price, and the multiple between them;
- the budget cap and the implied fully-diluted take;
- the largest single purchase the budget allows;
- the price at the current sold amount, and how far that is from the cap.

## Traps

- **"Curve" used for a fixed ladder.** Tranches are not a curve; the integral reasoning does not apply,
  and a boundary bug does.
- **Rounding "in the protocol's favour" claimed.** Verify direction per function; one `floor` on the
  wrong side is the whole bug.
- **A cap enforced only in the UI.** Call it: buy one unit past the per-wallet cap on a fork.
- **A `maxTx`-style limit presented as a curve property.** It is a separate, usually admin-mutable,
  gate.
