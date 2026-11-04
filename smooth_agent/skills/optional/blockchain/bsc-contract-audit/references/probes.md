# Raw probes — BSC (chain 56)

Everything this skill does, written without `cast` so it runs on a clean machine. Use
`scripts/rpc.py` when available; the `curl` recipes below are the manual equivalent.

BSC only. Set the endpoint once:

```bash
export RPC_URL=https://bsc-rpc.publicnode.com
ADDR_LC=$(printf '%s' "$ADDR" | tr 'A-Z' 'a-z')   # APIs key on lowercase
```

Backups: `https://bsc-dataseed.binance.org`, `https://bsc-dataseed1.defibit.io`, `https://1rpc.io/bnb`.
Public nodes cap `eth_getLogs` near 1000 blocks and rate-limit under load; wide scans need a paid RPC
or an explorer index.

## Read state

```bash
rpc() { curl -s -H 'content-type: application/json' -d "$1" "$RPC_URL"; }
CALL() {  # CALL <to> <data-hex>
  rpc "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_call\",\"params\":[{\"to\":\"$1\",\"data\":\"$2\"},\"latest\"]}"
}

CALL $ADDR 0x8da5cb5b          # owner()      -> 32-byte word; 0x0 = renounced
CALL $ADDR 0x5c975abb          # paused()     -> 0/1
CALL $ADDR 0xe30c3978          # pendingOwner()
CALL $ADDR 0x3e47158c          # proxyAdmin()
rpc "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_getCode\",\"params\":[\"$ADDR\",\"latest\"]}"
rpc "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_getStorageAt\",\"params\":[\"$ADDR\",\"0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc\",\"latest\"]}"
```

A reverting `eth_call` means the function is missing **or** needs arguments — not a renounce. Only a
successful call returning the zero address is a renounce.

EIP-1967 slots:

| Slot | Holds |
|---|---|
| `0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc` | implementation |
| `0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50` | beacon |
| `0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103` | admin |

Empty both slots does not mean "not a proxy" — USDC, USDe, and most BSC memecoins use custom proxy
patterns. Cross-check `Proxy` / `Implementation` on BscScan and `is_proxy` from GoPlus before
concluding.

## Source verification

```bash
# Sourcify — authoritative status, no key. Values: exact_match | match | none
curl -s "https://sourcify.dev/server/v2/contract/56/$ADDR_LC" \
  | jq -r '{match,creation:.creationMatch,runtime:.runtimeMatch}'
# full-match archive (200 = present)
curl -sI "https://repo.sourcify.dev/contracts/full_match/56/$ADDR_LC/metadata.json" | head -1

# Etherscan v2 unified endpoint, chainid=56 (one ETHERSCAN_KEY covers BSC)
curl -s "https://api.etherscan.io/v2/api?chainid=56&module=contract&action=getsourcecode&address=$ADDR_LC&apikey=$ETHERSCAN_KEY" \
  | jq -r '.result[0] | {ContractName,CompilerVersion,Proxy,Implementation,SimilarMatch,src:(.SourceCode|length)}'
```

BscScan's old `api.bscscan.com` returns "Invalid API URL endpoint, use api.bscscan.com" for v1-style
calls; use the v2 unified host above. Manual fallback: open `https://bscscan.com/address/<addr>#code`
and read the banner — `Exact Match`, `Similar Match`, or unverified.

Fields: `SourceCode` empty = unverified, starting `{{` = standard-JSON multi-file. `SimilarMatch`
filled = **not** an exact match. `ContractName` should match the name the site uses.

## Honeypot and tax shield

```bash
curl -s "https://api.honeypot.is/v2/IsHoneypot?address=$ADDR_LC&chainID=56" \
  | jq '{honeypot:.honeypotResult.isHoneypot,buy:.simulationResult.buyTax,sell:.simulationResult.sellTax,transfer:.simulationResult.transferTax,flags:.flags}'

curl -s "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses=$ADDR_LC" \
  | jq --arg a "$ADDR_LC" '.result[$a] | {is_honeypot,is_mintable,can_take_back_ownership,hidden_owner,owner_change_balance,transfer_pausable,trading_cooldown,is_proxy,selfdestruct,external_call,buy_tax,sell_tax,holder_count,creator_percent,creator_address}'
```

GoPlus fields that matter: `hidden_owner` (owner hidden behind a getter), `can_take_back_ownership`
(renounce is reversible), `owner_change_balance` (owner rewrites holder balances), `transfer_pausable`,
`trading_cooldown`, `is_mintable`, `is_proxy`, `selfdestruct`, `external_call`.

## Holders and LP

Primary (no GoPlus) — reconstruct concentration from Transfer logs with a premium RPC:

```bash
python3 scripts/rpc.py holders $ADDR 20          # top 20 + holder_count + % of supply
# large history + free RPC? widen the log window and expect many calls:
LOGS_WINDOW=20000 python3 scripts/rpc.py holders $ADDR 20
```

Reflection/rebase tokens (a `deliver()` selector) move balances without a Transfer event, so the
log-sum drifts from live state — re-read any suspect top holder with
`rpc.py call $ADDR "balanceOf(address)(uint256)" 0xHOLDER`.

Optional shortcut if you already have a GoPlus key (one call for holders + LP tag + is_locked):

```bash
curl -s "https://api.gopluslabs.io/api/v1/token_security/56?contract_addresses=$ADDR_LC" \
  | jq --arg a "$ADDR_LC" '.result[$a] | {holders:.holders[:20], lp:.lp_holders, holder_count}'
```

Resolve the pair, then measure depth in BNB and check who holds the LP:

```bash
FACTORY=0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73
WBNB=0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c
PAIR=$(python3 scripts/rpc.py call $FACTORY "getPair(address,address)(address)" $ADDR $WBNB)
python3 scripts/rpc.py call $PAIR "getReserves()(uint112,uint112,uint32)"
python3 scripts/rpc.py call $PAIR "totalSupply()(uint256)"
python3 scripts/rpc.py call $PAIR "balanceOf(address)(uint256)" 0x000000000000000000000000000000000000dEaD
```

`getReserves()` returns `(reserve0, reserve1, timestamp)` ordered by token address — WBNB is
`reserve1` if WBNB sorts lower, so identify which side is WBNB before reading depth. Depth is the
WBNB side in wei.

Known BSC lockers and burn — confirm the address from the service's own app page, then compare
`lp_holders[].tag`:

| Name | Address |
|---|---|
| Burn | `0x000000000000000000000000000000000000dEaD` |
| UNCX locker | `0xc765bddb93b0d1c1a88282ba0fa6b2d00e3e0c83` |
| DXLock | `0xe2fe530c047f2d85298b07d9333c05737f1435fb` |
| PancakeSwap V3 position manager | `0x46A15B0b27311cedF172AB29E4f4766fbE7F4364` |

PancakeSwap V3 LP is an NFT: pull `tokenId` from `IncreaseLiquidity` / `Transfer` logs on the
position manager, then `ownerOf(tokenId)`. An unlocked position is withdrawable liquidity no matter
what the site says.

## Market data

```bash
curl -s "https://api.dexscreener.com/latest/dex/tokens/$ADDR_LC" \
  | jq '.pairs[] | {dexId,pairAddress,liq:.liquidity.usd,vol24:.volume.h24,buys:.txns.h24.buys,sells:.txns.h24.sells,created:.pairCreatedAt,chg:.priceChange}'
```

`buys` far from `sells`, or `vol24 / liq > 5`, is a wash signal. `pairCreatedAt` under 24h means the
history is too short to judge.

## Transfer logs

```bash
TOPIC=0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
rpc "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_getLogs\",\"params\":[{\"address\":\"$ADDR\",\"fromBlock\":\"0x...\",\"toBlock\":\"latest\",\"topics\":[\"$TOPIC\"]}]}"
```

Start from the deployment block (Etherscan v2 `getcontractcreation`, or the first tx on BscScan). For
older contracts, window the range and concatenate; public nodes reject wide spans.

## Rate-limit behavior

- Sourcify and honeypot.is tolerate light serial use; back off on HTTP 429.
- GoPlus is keyed per lowercase address; a checksummed address returns `null` for every field. That
  looks like "clean" if you do not notice.
- DexScreener returns pairs across chains — filter `chainId == "bsc"` before reading anything.

## NFT / mint track

Classify before probing. `ownerOf(1)` or `tokenURI(1)` returning data means NFT. `supportsInterface`
reverts on older collections (PancakeSquad) — a revert proves nothing.

```bash
python3 scripts/rpc.py call $ADDR "ownerOf(uint256)(address)" 1
python3 scripts/rpc.py call $ADDR "tokenURI(uint256)(string)" 1
python3 scripts/rpc.py call $ADDR "totalSupply()(uint256)"
python3 scripts/rpc.py call $ADDR "maxSupply()(uint256)"          # 0 / revert = uncapped or absent
```

Price and sale gate — names vary, try in order:

```bash
for s in "mintPrice()" "cost()" "MINT_PRICE()" "PRICE()" "price()"; do
  printf '%s -> ' "$s"; python3 scripts/rpc.py call $ADDR "$s(uint256)" 2>&1 | head -1
done
python3 scripts/rpc.py call $ADDR "mint(uint256)" 1 --raw      # revert reason = the real gate
python3 scripts/rpc.py call $ADDR "maxPerWallet()(uint256)"
python3 scripts/rpc.py call $ADDR "totalMinted()(uint256)"
```

`eth_call` cannot attach `value`, so a payable mint that checks `msg.value` will revert on the amount
rather than the gate. Read the price, then compare revert reasons across `mint(1)` and `mint(max)` to
separate "amount wrong" from "sale closed".

Reveal and metadata:

```bash
python3 scripts/rpc.py call $ADDR "revealed()(bool)"      # also isRevealed(), saleState()
python3 scripts/rpc.py call $ADDR "baseURI()(string)"
python3 scripts/rpc.py call $ADDR "tokenURI(uint256)(string)" 1
```

IPFS CID in the URI = frozen art. `https://…` = team-controlled. Record which.

Royalty (EIP-2981):

```bash
python3 scripts/rpc.py call $ADDR "royaltyInfo(uint256,uint256)(address,uint256)" 1 1000000000000000000
```

Second value / 1e18 = royalty percent. Above 10% or a live `setRoyalty` costs the resale points.

Minters and bags — `Transfer` from the zero address is a mint:

```bash
TOPIC=0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef
ZERO=0x0000000000000000000000000000000000000000000000000000000000000000
rpc "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_getLogs\",\"params\":[{\"address\":\"$ADDR\",\"fromBlock\":\"0x...\",\"toBlock\":\"latest\",\"topics\":[\"$TOPIC\",\"$ZERO\"]}]}"
```

Count distinct `to` addresses for unique minters against minted supply; that ratio is the real spread.
Team bags: `balanceOf($DEV)`. Enumerable collections also answer
`tokenOfOwnerByIndex(address,uint256)`.

Marketplaces: Element (`api.element.market/openapi/...`) and OpenSea v2 both require an API key —
without one, read the collection page by hand for floor, bids, and 7-day sales, and cite the URL.
`totalSupply / maxSupply` fill rate is the on-chain stand-in for demand.

## Binance Web3 security audit

```bash
./scripts/binance_audit.sh 0xADDR
```

Raw equivalent — the host is DNS-hijacked on Indonesian ISPs, so resolve through DoH and pin the IP:

```bash
IP=$(curl -s -H 'accept: application/dns-json' \
  'https://cloudflare-dns.com/dns-query?name=web3.binance.com&type=A' \
  | jq -r '.Answer[] | select(.type==1) | .data' | head -1)

curl -s --resolve "web3.binance.com:443:$IP" \
  -X POST 'https://web3.binance.com/bapi/defi/v1/public/wallet-direct/security/token/audit' \
  -H 'content-type: application/json' -H 'source: agent' \
  -H 'Accept-Encoding: identity' -H 'User-Agent: binance-web3/1.4 (Skill)' \
  -d "{\"binanceChainId\":\"56\",\"contractAddress\":\"$ADDR_LC\",\"requestId\":\"$(uuidgen)\"}"
```

Accept the result only when `data.hasResult` and `data.isSupported` are both true. `riskLevel` 0–5 maps
to LOW…HIGH, but it is a prior, not a verdict. Hits are `data.riskItems[].details[]` entries with
`isHit: true`; `riskType` is `RISK` or `CAUTION`.

Semantics trap: check titles read as claims, and `isHit: false` means the risk was **not** found —
`title: "Contract Renounced", isHit: false` means the contract is *not* renounced.

The check set is token-shaped (honeypot, tax, liquidity). On an NFT, `Liquidity Risk Not Found` and
`Unusual Buy/Sell Tax Not Found` say almost nothing. `Minting Risk Not Found` and `Upgrade Risk Not
Found` are the ones that matter there.

Forbidden: quoting `riskLevel` alone, or flattening a 26-check response into "LOW = safe".
