#!/usr/bin/env bash
# Lifecycle and economic snapshot for a launch-curve deployment.
#
#   ./snapshot.sh roles.env
#
# Prints every reading that grades a curve launch, marks absent calls as `absent`, and writes
# nothing. Record the block number with any verdict: the snapshot is invalid after a deploy, a
# launch, or an ownership change.
#
# roles.env — copy roles.example.env. RPC_URL comes from the environment.

set -uo pipefail

ROLES="${1:-roles.env}"
[ -f "$ROLES" ] || { echo "usage: $0 roles.env" >&2; exit 1; }
# shellcheck disable=SC1090
. "$ROLES"
: "${RPC_URL:?export RPC_URL first}"
: "${TOKEN:?TOKEN missing in $ROLES}"

SELDIR=$(cd "$(dirname "$0")" && pwd)

call() { # call <addr> <sig> [arg...]
  local to="$1" orig_sig="$2" sig="$2"; shift 2
  case "$sig" in *")("* ) sig="${sig%%)*})" ;; esac
  local sel
  sel=$(SELDIR="$SELDIR" python3 - "$sig" <<'PY'
import os, sys
sys.path.insert(0, os.environ["SELDIR"])
from curve_math import _selector
print(_selector(sys.argv[1]))
PY
)
  local data="0x$sel"
  for a in "$@"; do
    case "$a" in
      0x*) data="$data$(printf '%s' "${a#0x}" | tr 'A-F' 'a-f' | sed 's/^/000000000000000000000000/')" ;;
      *)   data="$data$(printf '%064x' "$a")" ;;
    esac
  done
  local out
  out=$(curl -s --max-time 20 -H 'content-type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_call\",\"params\":[{\"to\":\"$to\",\"data\":\"$data\"},\"latest\"]}" \
    "$RPC_URL")
  if printf '%s' "$out" | jq -e '.result and .result != "0x"' >/dev/null 2>&1; then
    local r; r=$(printf '%s' "$out" | jq -r .result)
    if [ "$orig_sig" != "$sig" ] && [[ "$orig_sig" == *")(address)"* ]]; then
      printf '0x%s\n' "${r: -40}"
    elif [ ${#r} -eq 66 ]; then
      python3 -c "print(int('$r',16))"
    else
      printf '%s\n' "$r"
    fi
  else
    echo absent
  fi
}

balance() {
  curl -s --max-time 20 -H 'content-type: application/json' \
    -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_getBalance\",\"params\":[\"$1\",\"latest\"]}" "$RPC_URL" \
    | jq -r '.result' | xargs -I{} python3 -c "print(int('{}',16)/1e18)"
}

block=$(curl -s --max-time 20 -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_blockNumber","params":[]}' "$RPC_URL" | jq -r '.result' | xargs -I{} python3 -c "print(int('{}',16))")
echo "block $block  $(date -u +%FT%TZ)"

echo
echo "--- roles"
printf 'token      %s\n' "$TOKEN"
for r in SALE POOL BIDBOOK; do [ -n "${!r:-}" ] && printf '%-10s %s\n' "$(echo "$r" | tr 'A-Z' 'a-z')" "${!r}"; done
owner=$(call "$TOKEN" "owner()(address)")
printf 'owner      %s  code=' "$owner"
if [ "$owner" != absent ]; then
  code=$(curl -s -H 'content-type: application/json' -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"eth_getCode\",\"params\":[\"$owner\",\"latest\"]}" "$RPC_URL" | jq -r .result | tr -d '\n')
  if [ "$code" = "0x" ]; then echo "0 (EOA)"; else echo "$(( (${#code} - 2) / 2 )) bytes (contract)"; fi
fi

echo
echo "--- lifecycle"
printf 'tradingOpen        %s\n' "$(call "$TOKEN" 'tradingOpen()(bool)')"
[ -n "${SALE:-}" ] && {
  printf 'launched           %s\n' "$(call "$SALE" 'launched()(bool)')"
  dl=$(call "$SALE" 'deadline()(uint256)')
  printf 'deadline           %s\n' "$dl"
  [ "$dl" != absent ] && printf 'deadline UTC       %s (%+d s)\n' \
    "$(python3 -c "import datetime;print(datetime.datetime.fromtimestamp($dl, datetime.timezone.utc).isoformat())")" \
    "$(( dl - $(date +%s) ))"
  printf 'HARD_CAP wei       %s\n' "$(call "$SALE" 'HARD_CAP()(uint256)')"
  printf 'sold               %s\n' "$(call "$SALE" 'sold()(uint256)')"
  printf 'raised wei         %s\n' "$(call "$SALE" 'raised()(uint256)')"
  printf 'sale BNB balance   %s\n' "$(balance "$SALE")"
}
[ -n "${BIDBOOK:-}" ] && printf 'bidbook BNB        %s\n' "$(balance "$BIDBOOK")"

echo
echo "--- supply"
printf 'totalSupply        %s\n' "$(call "$TOKEN" 'totalSupply()(uint256)')"
printf 'maxSupply          %s\n' "$(call "$TOKEN" 'maxSupply()(uint256)')"

echo
echo "--- privilege"
printf 'isMinter(owner)    %s\n' "$( [ "$owner" != absent ] && call "$TOKEN" 'isMinter(address)(bool)' "$owner")"
printf 'mintersLocked      %s\n' "$(call "$TOKEN" 'mintersLocked()(bool)')"
printf 'paused             %s\n' "$(call "$TOKEN" 'paused()(bool)')"
[ -n "${QUIZ:-}" ] && {
  printf 'emissionCap        %s\n' "$(call "$QUIZ" 'emissionCap()(uint256)')"
  printf 'totalAllocated     %s\n' "$(call "$QUIZ" 'totalAllocated()(uint256)')"
  printf 'submitFee wei      %s\n' "$(call "$QUIZ" 'submitFee()(uint256)')"
}

echo
echo "absent = selector not present or reverted; it is not proof of the feature's absence"
