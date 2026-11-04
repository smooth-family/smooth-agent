#!/usr/bin/env python3
"""Reference linear launch curve: price P at zero sold, 5P at the supply cap.

Verifies the arithmetic a launch contract must satisfy, and optionally compares the
on-chain `cost`-style view against the closed form.

  curve_math.py --p 1000000000000000 --supply 1050000e18 --check 1e18,10e18
  curve_math.py --p ... --supply ... --rpc URL --to 0xSALE --sig "cost(uint256)" --check 1e18

Checks, in order:
  1. monotonicity — every later unit is at least as expensive as the earlier one
  2. integral consistency — sum of marginal prices never exceeds the closed-form total
  3. rounding direction — total rounds up (protocol-favouring), never down
  4. cap admission — the cumulative cost of the requested units stays under the budget

Exit 0 when all pass, 1 on any violation, 2 on input error.

The constants are a spec, not a claim about any deployment: read P and the supply cap from
the contract or its constructor args, then pass them here.
"""

import argparse
import sys

PRECISION = 10**18


def parse_amount(text):
    """Accept 1e18, 1_000e18, plain integers."""
    t = text.strip().replace("_", "")
    if "e" in t:
        base, exp = t.split("e")
        return int(float(base) * 10 ** int(exp))
    return int(t)


def price_at(sold_units, p, supply_units):
    """Marginal price in wei per whole token: linear P -> 5P over the cap."""
    if supply_units == 0:
        return p
    return p + (4 * p * sold_units) // supply_units


def cost_closed(a_units, b_units, p, supply_units):
    """Exact integral over [a, b] in whole tokens, rounded up, returned in wei.

    price(x) = P + 4P·x/S  ->  ∫ = P(b-a) + 2P(b²-a²)/S, rational until the last step.
    """
    num = p * (b_units - a_units) * supply_units + 2 * p * (b_units * b_units - a_units * a_units)
    return (num + supply_units - 1) // supply_units


def cost_stepped(a, b, p, supply, step):
    """Discrete sum of marginal prices — the attacker's route to underpay."""
    total = 0
    for i in range(a, b, step):
        chunk = min(step, b - i)
        total += price_at(i, p, supply) * chunk
    return total


def check(p, supply, amounts, budget, rpc=None, to=None, sig=None, dec=18):
    fails = []
    unit = 10 ** dec
    supply_t = supply // unit

    # 1. monotonicity
    probes = [supply_t * i // 10 for i in range(0, 11)]
    prices = [price_at(x, p, supply_t) for x in probes]
    if any(b < a for a, b in zip(prices, prices[1:])):
        fails.append("non-monotone: a later unit is cheaper than an earlier one")
    print(f"price at 0%: {prices[0]}")
    print(f"price at 100%: {prices[-1]}")
    if prices[-1] != 5 * p:
        fails.append(f"endpoint is {prices[-1]/p:.4f}x P, expected 5.0000x")

    # 2/3/4. per-amount economics
    sold = 0
    for amount in amounts:
        at = amount // unit
        total = cost_closed(sold // unit, sold // unit + at, p, supply_t)
        stepped = cost_stepped(sold // unit, sold // unit + at, p, supply_t, max(1, at // 100))
        print(f"\namount {amount}: total {total}, stepped {stepped}")
        if stepped > total:
            fails.append(f"stepped sum exceeds integral for {amount} (payer undercharged)")
        if budget is not None and total > budget:
            fails.append(f"{amount} costs {total} which exceeds budget {budget}")
        if rpc and to and sig:
            onchain = rpc_cost(rpc, to, sig, amount)
            if onchain is None:
                print("  on-chain: unavailable")
            else:
                print(f"  on-chain: {onchain} (delta {onchain - total})")
                if onchain < total:
                    fails.append(f"on-chain cost under closed form by {total - onchain}")
        sold += amount
        if sold > supply:
            fails.append(f"cumulative {sold} passes the cap {supply}")
            break

    print()
    if fails:
        for f in fails:
            print("FAIL", f)
        return 1
    print("PASS monotone, rounding, no undercharge, inside budget")
    return 0


def rpc_cost(url, to, sig, amount):
    import json
    import urllib.request

    sel = sig.split("(")[0]
    # cost(uint256) and quote(uint256) share this encoding: one uint argument
    data = "0x" + _selector(sig) + format(amount, "x").rjust(64, "0")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                       "params": [{"to": to, "data": data}, "latest"]}).encode()
    req = urllib.request.Request(url, body, {"content-type": "application/json",
                                             "user-agent": "curve-math/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            out = json.load(resp)
    except Exception:
        return None
    if "error" in out or not out.get("result") or out["result"] == "0x":
        return None
    return int(out["result"], 16)


_M64 = (1 << 64) - 1
_RC = [1, 0x8082, 0x800000000000808A, 0x8000000080008000, 0x808B, 0x80000001,
       0x8000000080008081, 0x8000000000008009, 0x8A, 0x88, 0x80008009, 0x8000000A,
       0x8000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
       0x8000000000008002, 0x8000000000000080, 0x800A, 0x800000008000000A,
       0x8000000080008081, 0x8000000000008080, 0x80000001, 0x8000000080008008]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
        [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _M64 if n else x


def _perm(st):
    for rnd in range(24):
        c = [st[x][0] ^ st[x][1] ^ st[x][2] ^ st[x][3] ^ st[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                st[x][y] ^= d[x]
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rol(st[x][y], _ROT[x][y])
        for x in range(5):
            for y in range(5):
                st[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        st[0][0] ^= _RC[rnd]


def _selector(sig):
    st = [[0] * 5 for _ in range(5)]
    buf = bytearray(sig.encode()) + b"\x01"
    while len(buf) % 136:
        buf += b"\x00"
    buf[-1] ^= 0x80
    for off in range(0, len(buf), 136):
        blk = buf[off:off + 136]
        for i in range(17):
            st[i % 5][i // 5] ^= int.from_bytes(blk[i * 8:i * 8 + 8], "little")
        _perm(st)
    return b"".join(st[i % 5][i // 5].to_bytes(8, "little") for i in range(4))[:4].hex()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--p", required=True, help="start price per token, wei")
    ap.add_argument("--supply", required=True, help="sale cap in token base units")
    ap.add_argument("--check", required=True, help="comma-separated amounts to price")
    ap.add_argument("--budget", help="optional budget per amount, wei")
    ap.add_argument("--rpc")
    ap.add_argument("--to")
    ap.add_argument("--sig", default="cost(uint256)")
    ap.add_argument("--decimals", type=int, default=18)
    a = ap.parse_args()
    try:
        p = parse_amount(a.p)
        supply = parse_amount(a.supply)
        amounts = [parse_amount(x) for x in a.check.split(",")]
        budget = parse_amount(a.budget) if a.budget else None
    except ValueError as exc:
        print(f"bad number: {exc}")
        return 2
    if p <= 0 or supply <= 0:
        return 2
    return check(p, supply, amounts, budget, a.rpc, a.to, a.sig, a.decimals)


if __name__ == "__main__":
    sys.exit(main())
