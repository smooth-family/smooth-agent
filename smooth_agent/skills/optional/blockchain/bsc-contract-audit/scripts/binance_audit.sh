#!/usr/bin/env bash
# Binance Web3 token security audit — BSC (chain 56) only.
#
#   ./binance_audit.sh 0xADDR
#
# Prints the raw JSON and a normalized summary. Exit 0 whenever the API answered,
# 2 when it answered with no data, 1 on transport failure.
#
# Works on tokens AND NFT contracts: the API accepts an ERC-721/1155 address and still
# returns the contract/trade/scam check set, but several checks (tax, honeypot, liquidity)
# are token-shaped and mean little for an NFT. Read it as one input, not a verdict.
#
# Indonesian ISPs (Telkomsel/IndiHome) hijack binance.com DNS to a block page, which
# surfaces as "certificate subject name mismatch". This script resolves through DoH and
# pins the IP with --resolve, so it works behind the hijack.

set -uo pipefail

ADDR="${1:-}"
[ -n "$ADDR" ] || { echo "usage: $0 0xADDR" >&2; exit 1; }

ADDR_LC=$(printf '%s' "$ADDR" | tr 'A-Z' 'a-z')
URL="https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit"

ips=$(curl -s --max-time 15 -H 'accept: application/dns-json' \
  'https://cloudflare-dns.com/dns-query?name=web3.binance.com&type=A' \
  | grep -o '"data":"[0-9.]*"' | cut -d'"' -f4 | head -3)

if [ -z "$ips" ]; then
  echo "DoH lookup failed; falling back to system DNS" >&2
  resolve=()
else
  resolve=()
  for ip in $ips; do resolve+=(--resolve "web3.binance.com:443:$ip"); done
fi

REQ_ID=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)
BODY="{\"binanceChainId\":\"56\",\"contractAddress\":\"$ADDR_LC\",\"requestId\":\"$REQ_ID\"}"

resp=$(curl -s --max-time 30 "${resolve[@]}" -X POST "$URL" \
  -H 'content-type: application/json' \
  -H 'source: agent' \
  -H 'Accept-Encoding: identity' \
  -H 'User-Agent: binance-web3/1.4 (Skill)' \
  -d "$BODY")

if [ -z "$resp" ]; then echo "transport failure" >&2; exit 1; fi

printf '%s\n' "$resp" | python3 -m json.tool 2>/dev/null || printf '%s\n' "$resp"
printf '\n--- summary ---\n'

printf '%s' "$resp" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    print("unparseable response"); sys.exit(2)
data = d.get("data") or {}
if not data.get("hasResult") or not data.get("isSupported"):
    print("no data for this address on chain 56")
    sys.exit(2)
ex = data.get("extraInfo") or {}
print("riskLevel      %s %s" % (data.get("riskLevel"), data.get("riskLevelEnum")))
print("buy/sell tax   %s / %s" % (ex.get("buyTax"), ex.get("sellTax")))
print("verified       %s" % ex.get("isVerified"))
hits = []
for item in data.get("riskItems") or []:
    for x in item.get("details") or []:
        if x.get("isHit"):
            hits.append("%s: %s (%s)" % (item.get("id"), x.get("title"), x.get("riskType")))
print("hits           %d" % len(hits))
for h in hits:
    print("  - " + h)
'
