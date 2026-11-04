#!/usr/bin/env python3
"""Dependency-free JSON-RPC helper (stdlib only) for the bsc-contract-audit skill.

Every on-chain read goes through here. Needs RPC_URL in the environment.

  export RPC_URL=https://bsc-rpc.publicnode.com
  rpc.py chain          # must be 56
  rpc.py block
  rpc.py code   0xADDR
  rpc.py storage 0xADDR 0x3608...bbc
  rpc.py call   0xADDR "owner()"
  rpc.py call   0xADDR "balanceOf(address)" 0xHOLDER --raw
  rpc.py proxy  0xADDR
  rpc.py scan   0xADDR ../references/selectors.md
  rpc.py meta   0xADDR
  rpc.py holders 0xADDR [topN] [fromBlock]   # ERC20 concentration, no GoPlus (needs a premium RPC)
  rpc.py nbal   0xADDR          # native BNB balance in wei (escrow pool: held vs deposited)

`call` takes a Solidity signature (keccak computed locally) or a raw 0x selector.
Output is decoded per the declared return type: address, uint, bool, string, or a
comma-separated tuple such as uint112,uint112,uint32.
`--raw` prints the bare hex output.

Exit: 0 ok, 1 RPC/argument failure, 2 revert (missing function or missing arguments).
"""

import json
import os
import sys
import urllib.request

M64 = (1 << 64) - 1
RC = [1, 0x8082, 0x800000000000808A, 0x8000000080008000, 0x808B, 0x80000001,
      0x8000000080008081, 0x8000000000008009, 0x8A, 0x88, 0x80008009, 0x8000000A,
      0x8000808B, 0x800000000000008B, 0x8000000000008089, 0x8000000000008003,
      0x8000000000008002, 0x8000000000000080, 0x800A, 0x800000008000000A,
      0x8000000080008081, 0x8000000000008080, 0x80000001, 0x8000000080008008]
ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61],
       [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & M64 if n else x


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
                b[y][(2 * x + 3 * y) % 5] = _rol(st[x][y], ROT[x][y])
        for x in range(5):
            for y in range(5):
                st[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & b[(x + 2) % 5][y])
        st[0][0] ^= RC[rnd]


def keccak(data):
    st = [[0] * 5 for _ in range(5)]
    buf = bytearray(data) + b"\x01"
    while len(buf) % 136:
        buf += b"\x00"
    buf[-1] ^= 0x80
    for off in range(0, len(buf), 136):
        blk = buf[off:off + 136]
        for i in range(17):
            st[i % 5][i // 5] ^= int.from_bytes(blk[i * 8:i * 8 + 8], "little")
        _perm(st)
    out = b"".join(st[i % 5][i // 5].to_bytes(8, "little") for i in range(4))
    return out


def rpc(method, params):
    url = os.environ.get("RPC_URL")
    if not url:
        sys.exit("RPC_URL is not set")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url, body, {"content-type": "application/json", "user-agent": "sc-audit/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"RPC failed: {exc}")
    if "error" in payload:
        print(f"REVERT: {payload['error'].get('message')}", file=sys.stderr)
        sys.exit(2)
    return payload["result"]


# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def rpc_try(method, params):
    """Like rpc() but returns (ok, result_or_error) instead of exiting — for fallbacks."""
    url = os.environ.get("RPC_URL")
    if not url:
        return False, "RPC_URL is not set"
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        url, body, {"content-type": "application/json", "user-agent": "sc-audit/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if "error" in payload:
        return False, payload["error"].get("message", "rpc error")
    return True, payload["result"]


def selector(sig):
    if sig.startswith("0x") and len(sig) == 10:
        return sig[2:]
    if "(" not in sig:
        sys.exit(f"invalid signature: {sig}")
    sig = sig.split(")(")[0] + ")" if ")(" in sig else sig
    return keccak(sig.encode()).hex()[:8]


def pad_addr(a):
    return a.lower().replace("0x", "").rjust(64, "0")


def pad_uint(v):
    return format(int(str(v), 0), "x").rjust(64, "0")


def decode(hexdata, outype):
    if not hexdata or hexdata == "0x":
        return ""
    body = hexdata[2:]
    if "," in outype:
        return ", ".join(
            decode("0x" + body[i * 64:(i + 1) * 64], t)
            for i, t in enumerate(outype.split(","))
        )
    word = body[:64]
    if outype == "address":
        return "0x" + word[-40:]
    if outype == "bool":
        return str(int(word, 16) != 0)
    if outype.startswith("uint"):
        return str(int(word, 16))
    if outype == "string":
        off = int(word, 16) * 2
        ln = int(body[off:off + 64], 16) * 2
        return bytes.fromhex(body[off + 64:off + 64 + ln]).decode(errors="replace")
    return hexdata


def cmd_holders(addr, topn=10, from_block=0):
    """Reconstruct ERC20 holder balances from Transfer logs — a GoPlus-free concentration read.

    Needs a premium/archive RPC: completeness requires every Transfer since deployment,
    and public nodes cap eth_getLogs near 1000 blocks. Tries one full-range call, then
    falls back to windowed scans (window = env LOGS_WINDOW, default 5000).
    Caveat: reflection/rebase tokens (a `deliver()` selector) rewrite balances without a
    Transfer event, so the log-sum diverges from live balanceOf — confirm top holders with
    `rpc.py call 0xADDR "balanceOf(address)(uint256)" 0xHOLDER` when that is the case.
    """
    total = int(rpc("eth_call", [{"to": addr, "data": "0x18160ddd"}, "latest"]), 16)  # totalSupply()
    latest = int(rpc("eth_blockNumber", []), 16)
    bal = {}

    def apply_logs(logs):
        for lg in logs:
            topics = lg.get("topics", [])
            if len(topics) != 3:       # ERC20 Transfer has exactly 3 topics; ERC721 has 4 (skip)
                continue
            frm = "0x" + topics[1][-40:]
            to = "0x" + topics[2][-40:]
            data = lg.get("data", "0x")
            val = int(data[2:66], 16) if len(data) >= 66 else 0
            if not val:
                continue
            bal[frm] = bal.get(frm, 0) - val
            bal[to] = bal.get(to, 0) + val

    flt = {"fromBlock": hex(from_block), "toBlock": hex(latest),
           "address": addr, "topics": [TRANSFER_TOPIC]}
    ok, res = rpc_try("eth_getLogs", [flt])
    windowed = False
    if ok:
        apply_logs(res)
    else:
        windowed = True
        win = int(os.environ.get("LOGS_WINDOW", "5000"))
        print(f"# full-range getLogs failed ({str(res)[:60]}); windowing at {win} blocks",
              file=sys.stderr)
        start = from_block
        while start <= latest:
            end = min(start + win - 1, latest)
            ok2, res2 = rpc_try("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end),
                                                 "address": addr, "topics": [TRANSFER_TOPIC]}])
            if not ok2:
                sys.exit(f"# window {start}-{end} failed: {str(res2)[:80]}\n"
                         f"# lower LOGS_WINDOW or use an archive RPC")
            apply_logs(res2)
            start = end + 1

    zero = "0x0000000000000000000000000000000000000000"
    dead = "0x000000000000000000000000000000000000dead"
    holders = [(a, b) for a, b in bal.items() if b > 0 and a != zero]
    holders.sort(key=lambda kv: kv[1], reverse=True)
    print(f"total_supply   {total}")
    print(f"holder_count   {len(holders)}  (from Transfer logs{', windowed' if windowed else ''})")
    print(f"range          block {from_block} .. {latest}")
    print(f"\ntop {topn} (strip LP pair / lockers / burn before judging concentration):")
    for a, b in holders[:topn]:
        pct = (b * 10000 // total) / 100 if total else 0
        tag = "  [burn]" if a == dead else ""
        print(f"  {a}  {b}  {pct}%{tag}")


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd = argv[1]
    if cmd == "chain":
        print(int(rpc("eth_chainId", []), 16))
        return
    if cmd == "block":
        print(int(rpc("eth_blockNumber", []), 16))
        return
    if len(argv) < 3:
        sys.exit("need a contract address")
    addr = argv[2]
    if cmd == "code":
        print(rpc("eth_getCode", [addr, "latest"]))
    elif cmd == "nbal":
        print(int(rpc("eth_getBalance", [addr, "latest"]), 16))
    elif cmd == "meta":
        for sig, typ in (("name()", "string"), ("symbol()", "string"),
                         ("decimals()", "uint8"), ("totalSupply()", "uint256")):
            try:
                out = rpc("eth_call", [{"to": addr, "data": "0x" + selector(sig)}, "latest"])
                print(f"{sig:14} {decode(out, typ)}")
            except SystemExit:
                print(f"{sig:14} (revert)")
    elif cmd == "storage":
        print(rpc("eth_getStorageAt", [addr, argv[3], "latest"]))
    elif cmd == "proxy":
        imp = rpc("eth_getStorageAt", [addr, "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc", "latest"])[2:]
        adm = rpc("eth_getStorageAt", [addr, "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103", "latest"])[2:]
        bcn = rpc("eth_getStorageAt", [addr, "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50", "latest"])[2:]
        zero = "0" * 64
        if imp != zero:
            print("implementation 0x" + imp[-40:])
            if adm == zero:
                print("admin          0x" + "0" * 40 + "   (empty = UUPS pattern, admin lives in the implementation)")
            else:
                print("admin          0x" + adm[-40:])
        elif bcn != zero:
            print("beacon         0x" + bcn[-40:])
            impl = rpc("eth_call", [{"to": "0x" + bcn[-40:], "data": "0x5c60da1b"}, "latest"])
            print("implementation 0x" + impl[-40:])
        else:
            print("no EIP-1967 pattern (slots empty). Check Proxy/Implementation on the explorer,")
            print("or for a custom pattern: look for delegatecall in the bytecode (rpc.py code 0xADDR)")
            print("implementation 0x" + "0" * 40)
            print("admin          0x" + "0" * 40)
    elif cmd == "call":
        if len(argv) < 4:
            sys.exit("need a signature")
        data = "0x" + selector(argv[3])
        for arg in argv[4:]:
            if arg.startswith("--"):
                continue
            data += pad_addr(arg) if (arg.startswith("0x") and len(arg) <= 42) else pad_uint(arg)
        out = rpc("eth_call", [{"to": addr, "data": data}, "latest"])
        if "--raw" in argv or ")(" not in argv[3]:
            print(out)
            return
        print(decode(out, argv[3].split(")(")[1][:-1]))
    elif cmd == "holders":
        rest = [a for a in argv[3:] if not a.startswith("--")]
        topn = int(rest[0]) if len(rest) >= 1 else 10
        frm = int(rest[1], 0) if len(rest) >= 2 else 0
        cmd_holders(addr, topn, frm)
    elif cmd == "scan":
        src = argv[3]
        code = rpc("eth_getCode", [addr, "latest"]).lower()
        hits = 0
        with open(src) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 2:
                    continue
                label, sel = parts[0].strip(), parts[1].strip()
                n = code.count("63" + sel)
                if n:
                    hits += 1
                    print(f"HIT {label:34} 0x{sel} x{n}")
        print(f"\n{hits} selector hits (presence != live feature)")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
