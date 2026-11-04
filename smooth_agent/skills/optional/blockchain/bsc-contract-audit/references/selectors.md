# Selector table for `scripts/rpc.py scan` and `scripts/rpc.py call`.
# Format: label<TAB>selector<TAB>note. Lines starting with # are ignored.
# Every selector is keccak256(signature)[0:4] — never hand-edit.
# A selector in bytecode is a lead only: confirm via ABI + onlyOwner before scoring.
# The bytecode pattern is the PUSH4 opcode (0x63) followed by the 4-byte selector.
#
# BSC weighting: the SafeMoon-fork cluster (trading gate + limit setters + tax setters + bot
# blacklist + auto-liquify) is the dominant rug shape on this chain. Treat any hit in those
# blocks as a live-owner lead until the source proves otherwise.
#
# --- access / ownership ---
owner()	8da5cb5b	address; 0x0 = renounced
transferOwnership(address)	f2fde38b	two-step into pendingOwner
renounceOwnership()	715018a6	removes every onlyOwner button
pendingOwner()	e30c3978	Ownable2Step left hanging
acceptOwnership()	79ba5097	pending owner never accepted
proxyAdmin()	3e47158c	admin of a transparent proxy
changeAdmin(address)	8f283970	swaps the proxy admin
# --- pause ---
paused()	5c975abb	bool; true stops transfers
pause()	8456cb59
unpause()	3f4ba83a
# --- blacklist / bots / cooldown ---
blacklist(address)	f9f92be4	USDT-style
isBlacklisted(address)	fe575a87
unBlacklist(address)	1a895266
addBlacklist(address)	9cfe42da
setBlacklist(address,bool)	153b0d1e
setBots(address,bool)	13b2a23e	SafeMoon-fork sniper blacklist
setBotBlacklist(address,bool)	37c9be87
setCooldown(uint256)	4fc3f41a	time gate between transfers
setCooldownEnabled(bool)	5932ead1
setAutomatedMarketMakerPair(address,bool)	9a7a23d6
setPair(address,bool)	86a22eff
excludeFromFee(address)	437823ec
setExcludedFromFee(address,bool)	6612e66f
setFeeExempt(address,bool)	8ebfc796
# --- mint / burn / rebase ---
mint(address,uint256)	40c10f19
mint(uint256)	a0712d68
mint(address,uint256,bytes)	94d008ef
burn(uint256)	42966c68
deliver(uint256)	3bd5d173	reflection balance rewrite
# --- upgrade ---
upgradeTo(address)	3659cfe6	UUPS
upgradeToAndCall(address,bytes)	4f1ef286	UUPS + init
# --- tax / fee setters ---
setTax(uint256,uint256)	667f6526
setFees(uint256,uint256)	0b78f9c0
setFee(uint256,uint256)	52f7c988
setTaxes(uint256,uint256)	c647b20e
setBuyTax(uint256)	dc1052e2
setSellFee(uint256)	8b4cee08
setBuyFee(uint256)	0cc835a3
# --- limit / trading-gate setters ---
setMaxTxAmount(uint256)	ec28438a
setMaxTxPercent(uint256)	d543dbeb
setMaxWallet(uint256)	5d0044ca
setMaxWalletSize(uint256)	ea1644d5
setMaxWalletPercent(uint256)	82bf293c
setWalletLimit(uint256)	f1d5f517
setLimits(uint256,uint256)	c4590d3f
removeLimits()	751039fc
setTradingEnabled(bool)	c2e5ec04
setTradingOpen(bool)	21c03a97
enableTrading()	8a8c523c
openTrading()	c9567bf9
setTradeStartTime(uint256)	a0b2e1f1	clock-based gate
maxTxAmount()	8c0b5e22	SafeMoon-fork getter
_maxTxAmount()	7d1db4a5
tradingEnabled()	4ada218b
tradingOpen()	ffb54a99
# --- auto-liquify / tax routing ---
setSwapAndLiquifyEnabled(bool)	c49b9a80
swapAndLiquify(uint256)	173865ad	owner-callable sell into pair
manualSwap()	51bc3c85
setMarketingWallet(address)	5d098b38
# --- fund extraction (vault / presale / tax token) ---
withdraw()	3ccfd60b
withdraw(uint256)	2e1a7d4d
emergencyWithdraw()	db2e21bc
rescueETH()	20800a00
rescueTokens(address,uint256)	57376198
sweep(address)	01681a62
refund()	590e1ae3
claim()	4e71d92d
release()	86d1a69f	vesting
# --- NFT / mint track ---
ownerOf(uint256)	6352211e	classification: reverts on BEP-20
tokenURI(uint256)	c87b56dd
supportsInterface(bytes4)	01ffc9a7	0x80ac58cd ERC721, 0xd9b67a26 ERC1155; reverts on old collections
mint(uint256)	a0712d68	also the public payable mint; try mint(1)
maxSupply()	d5abeb01	0/absent = uncapped
totalMinted()	a2309ff8
maxPerWallet()	453c2310
MAX_PER_WALLET()	0f2cdd6c
mintPrice()	6817c76c
MINT_PRICE()	c002d23d
cost()	13faede6
PRICE()	8d859f3e
revealed()	51830227
isRevealed()	54214f69
baseURI()	6c0360eb	https = team-controlled, ipfs = frozen
setBaseURI(string)	55f804b3	mutable after reveal = bait
setBaseURI(string,bool)	b64b21ca
toggleSale()	7d8966e4
setSaleState(uint256)	084c4088
saleIsActive()	eb8d2444
publicSaleActive()	bc8893b4
royaltyInfo(uint256,uint256)	2a55205a	EIP-2981
setRoyalty(uint256)	4209a2e1	mutable royalty
tokenOfOwnerByIndex(address,uint256)	2f745c59
# --- lock / deposit-refund allocation (escrow) ---
# You lock BNB/USDT for an NFT/token allocation; principal refunded after an unlock time.
# A working small refund is NOT proof. Confirm in source: (a) principal held in the contract,
# (b) only the depositor can retrieve it, (c) no owner path to the pool, (d) unlock immutable and
# withdraw not pausable, (e) user record zeroed before the transfer (reentrancy).
deposit()	d0e30db0	payable deposit (WETH-style) — the lock entry
deposit(uint256)	b6b55f25
deposit(address,uint256)	47e7ef24
lock()	f83d08ba
lock(uint256)	dd467064
lock(uint256,uint256)	1338736f
lockTokens(uint256,uint256)	89158d8e
stake(uint256)	a694fc3a
participate(uint256)	845c9306
commit(uint256)	f4f98ad5
pledge(uint256)	7326c9c0
contribute()	d7bb99ba
unlock()	a69df4b5	depositor pulls principal back
redeem()	be040fb0
withdrawPrincipal()	e1f06f54
claimRefund()	b5545a3c
getRefund()	b2d5ae44
unlockTime()	251c1aa3	fixed timestamp gate
lockDuration()	04554443
lockPeriod()	3fd8b02f
releaseTime()	b91d4001
claimableAt()	91e0a8aa
lockedUntil(address)	9bc289f1
setLockDuration(uint256)	4eb665af	DANGER: owner moves the goalposts
setUnlockTime(uint256)	dace4557	DANGER
updateUnlockTime(uint256)	8f5dd49b	DANGER
extendLock(uint256)	44ee3a1c	DANGER: traps principal longer
setWithdrawEnabled(bool)	9ec004a2	DANGER: pausable refund = soft trap
setClaimEnabled(bool)	92929a09	DANGER
toggleClaim()	d1129745	DANGER
pauseWithdraw(bool)	ebd462cb	DANGER
deposits(address)	fc7e286d	per-user principal record
depositOf(address)	23e3fbd5
depositsOf(address)	e3a9db1a
lockedOf(address)	a5f1e282
lockedBalance(address)	9ae697bf
allocation(address)	b81b8630	NFT/token allotment for the lock
allocationOf(address)	a4f82f27
hasClaimed(address)	73b2e80e
# owner drain of the pooled principal reuses the fund-extraction selectors above
# (withdraw / emergencyWithdraw / sweep / rescueETH / rescueTokens) — any of those reaching the
# pooled BNB/USDT is a drainable-escrow critical.
# --- plain reads ---
name()	06fdde03
symbol()	95d89b41
decimals()	313ce567
totalSupply()	18160ddd
balanceOf(address)	70a08231
transfer(address,uint256)	a9059cbb
approve(address,uint256)	095ea7b3
factory()	c45a0155
getPair(address,address)	e6a43905
getReserves()	0902f1ac	pair depth
getRoleMemberCount(bytes32)	ca15c873
hasRole(bytes32,address)	91d14854
