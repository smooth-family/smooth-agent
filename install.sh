#!/usr/bin/env bash
# Smooth Agent installer.
#
#   curl -fsSL https://raw.githubusercontent.com/smooth-family/smooth-agent/main/install.sh | bash
#
# Options (flags, or the same as environment variables):
#   --source <path|git-url>   install from here      (SMOOTH_AGENT_SOURCE, default: the GitHub repo)
#   --ref <branch|tag>        git ref to install     (SMOOTH_AGENT_REF, default: main)
#   --method pipx|venv        how to install         (SMOOTH_AGENT_METHOD, default: pipx if present, else venv)
#   --browser                 add browser automation (SMOOTH_AGENT_EXTRAS=browser)
#   --no-setup                skip the setup wizard  (SMOOTH_AGENT_NO_SETUP=1)
#
# Everything runs as your user; nothing is installed system-wide.

set -euo pipefail

REPO_URL="https://github.com/smooth-family/smooth-agent.git"
MIN_PY_MINOR=9

# ---------------------------------------------------------------- output

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ] || [ -n "${FORCE_COLOR:-}" ]; then
  B=$'\033[1m'; D=$'\033[2m'; R=$'\033[0m'; G=$'\033[32m'; Y=$'\033[33m'; E=$'\033[31m'; C=$'\033[36m'
  c() { printf '\033[1;38;5;%sm' "$1"; }
else
  B=""; D=""; R=""; G=""; Y=""; E=""; C=""
  c() { :; }
fi
STEP=0
TOTAL=5

banner() {
  printf '\n'
  printf '  %s███████╗███╗   ███╗ ██████╗  ██████╗ ████████╗██╗  ██╗%s\n' "$(c 51)" "$R"
  printf '  %s██╔════╝████╗ ████║██╔═══██╗██╔═══██╗╚══██╔══╝██║  ██║%s\n' "$(c 45)" "$R"
  printf '  %s███████╗██╔████╔██║██║   ██║██║   ██║   ██║   ███████║%s\n' "$(c 39)" "$R"
  printf '  %s╚════██║██║╚██╔╝██║██║   ██║██║   ██║   ██║   ██╔══██║%s\n' "$(c 63)" "$R"
  printf '  %s███████║██║ ╚═╝ ██║╚██████╔╝╚██████╔╝   ██║   ██║  ██║%s\n' "$(c 99)" "$R"
  printf '  %s╚══════╝╚═╝     ╚═╝ ╚═════╝  ╚═════╝    ╚═╝   ╚═╝  ╚═╝%s\n' "$(c 135)" "$R"
  printf '  %sa g e n t   ·   self-hosted   ·   any model   ·   your keys%s\n\n' "$(c 141)" "$R"
}
step() { STEP=$((STEP + 1)); printf '\n  %s[%d/%d]%s %s%s%s\n' "$C" "$STEP" "$TOTAL" "$R" "$B" "$1" "$R"; }
ok()   { printf '        %s✓%s %s\n' "$G" "$R" "$1"; }
warn() { printf '        %s!%s %s\n' "$Y" "$R" "$1"; }
note() { printf '          %s%s%s\n' "$D" "$1" "$R"; }
die()  { printf '        %s✗%s %s\n\n' "$E" "$R" "$1" >&2; exit 1; }

# ---------------------------------------------------------------- helpers

find_python() {
  local cand v
  for cand in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      v=$("$cand" -c 'import sys; print(sys.version_info[0], sys.version_info[1])' 2>/dev/null || true)
      set -- $v
      if [ "${1:-0}" = 3 ] && [ "${2:-0}" -ge "$MIN_PY_MINOR" ]; then
        command -v "$cand"
        return 0
      fi
    fi
  done
  return 1
}

git_clone() {  # git_clone <url> <ref> <dest>
  GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 --branch "$2" "$1" "$3"
}

# ---------------------------------------------------------------- main

main() {
  local source="${SMOOTH_AGENT_SOURCE:-$REPO_URL}"
  local ref="${SMOOTH_AGENT_REF:-main}"
  local method="${SMOOTH_AGENT_METHOD:-}"
  local extras="${SMOOTH_AGENT_EXTRAS:-}"
  local no_setup="${SMOOTH_AGENT_NO_SETUP:-}"
  while [ $# -gt 0 ]; do
    case "$1" in
      --source) source=$2; shift 2 ;;
      --ref) ref=$2; shift 2 ;;
      --method) method=$2; shift 2 ;;
      --browser) extras=browser; shift ;;
      --no-setup) no_setup=1; shift ;;
      -h|--help) sed -n '2,16p' "$0" 2>/dev/null || true; exit 0 ;;
      *) die "unknown option: $1" ;;
    esac
  done

  local home="${SMOOTH_AGENT_HOME:-$HOME/.smooth-agent}"
  local bindir="$HOME/.local/bin"

  banner

  # 1 ---------------------------------------------------------------
  step "Checking this machine"
  local os; os="$(uname -s)"
  case "$os" in
    Linux|Darwin) ok "$os $(uname -m)" ;;
    *) warn "$os is untested — Linux and macOS are supported (Windows: use WSL)" ;;
  esac
  local py
  py="$(find_python)" || die "Python 3.$MIN_PY_MINOR+ not found. Install it (e.g. apt install python3 python3-venv / brew install python) and rerun."
  ok "$("$py" --version 2>&1) at $py"
  local is_git=""
  if [ ! -d "$source" ]; then
    is_git=1
    command -v git >/dev/null 2>&1 || die "git not found (needed to download Smooth Agent). Install git and rerun."
    ok "git $(git --version | awk '{print $3}')"
  fi
  if [ -z "$method" ]; then
    if command -v pipx >/dev/null 2>&1; then method=pipx; else method=venv; fi
  fi
  if [ "$method" = venv ] && ! "$py" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    die "Python's venv module is missing. Install it (Debian/Ubuntu: apt install python3-venv) or install pipx, then rerun."
  fi
  ok "install method: $method"

  # 2 ---------------------------------------------------------------
  step "Getting Smooth Agent"
  mkdir -p "$home"
  local src
  if [ -n "$is_git" ]; then
    src="$home/src"
    SA_TMP="$(mktemp -d)"  # global: the EXIT trap runs after main() returns
    trap 'rm -rf "${SA_TMP:-}"' EXIT
    git_clone "$source" "$ref" "$SA_TMP/smooth-agent" \
      || die "could not download $source ($ref). Check your internet connection and rerun."
    rm -rf "$src"
    mv "$SA_TMP/smooth-agent" "$src"
    ok "downloaded $ref → $src"
  else
    src="$(cd "$source" && pwd)"
    ok "using local source $src"
  fi
  [ -f "$src/pyproject.toml" ] || die "$src does not look like Smooth Agent (no pyproject.toml)"
  local version
  version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$src/pyproject.toml" | head -1)
  note "version ${version:-?}"

  # 3 ---------------------------------------------------------------
  step "Installing"
  local target="$src"
  [ -n "$extras" ] && target="${src}[${extras}]"
  local exe
  if [ "$method" = pipx ]; then
    pipx install --force --python "$py" "$target" >/dev/null 2>&1 || die "pipx install failed — rerun with --method venv"
    exe="$(pipx environment --value PIPX_BIN_DIR 2>/dev/null || echo "$bindir")/smooth-agent"
    ok "installed with pipx"
  else
    local venv="$home/venv"
    if [ ! -x "$venv/bin/python" ]; then
      "$py" -m venv "$venv" || die "could not create $venv"
    fi
    "$venv/bin/python" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$venv/bin/python" -m pip install --quiet --upgrade "$target" || die "pip install failed (see the output above)"
    mkdir -p "$bindir"
    ln -sf "$venv/bin/smooth-agent" "$bindir/smooth-agent"
    exe="$bindir/smooth-agent"
    ok "installed into $venv"
    note "linked $exe"
  fi
  if [ "$extras" = browser ]; then
    local pyexe
    pyexe="$(head -1 "$(readlink -f "$exe" 2>/dev/null || echo "$exe")" | sed 's/^#!//')"
    if "$pyexe" -m playwright install chromium >/dev/null 2>&1; then
      ok "browser (Chromium) ready"
    else
      warn "browser engine not installed — run: $pyexe -m playwright install chromium"
    fi
  fi
  "$exe" version >/dev/null 2>&1 || die "the installed command does not run: $exe"
  ok "$("$exe" version)"

  # 4 ---------------------------------------------------------------
  step "Command on your PATH"
  local found; found="$(command -v smooth-agent 2>/dev/null || true)"
  local bindir_of_exe; bindir_of_exe="$(dirname "$exe")"
  if [ -n "$found" ] && [ "$(readlink -f "$found" 2>/dev/null || echo "$found")" != "$(readlink -f "$exe" 2>/dev/null || echo "$exe")" ]; then
    warn "another 'smooth-agent' comes first on your PATH: $found"
    note "use the full path $exe, or put $bindir_of_exe earlier in PATH"
  elif [ -n "$found" ]; then
    ok "smooth-agent → $found"
  else
    warn "$bindir_of_exe is not on your PATH yet"
    local rc="$HOME/.bashrc"
    case "${SHELL:-}" in */zsh) rc="$HOME/.zshrc" ;; */fish) rc="$HOME/.config/fish/config.fish" ;; esac
    note "add it:  echo 'export PATH=\"$bindir_of_exe:\$PATH\"' >> $rc && exec \$SHELL"
  fi

  # 5 ---------------------------------------------------------------
  step "Setup"
  if [ -n "$no_setup" ]; then
    ok "skipped (--no-setup) — run: smooth-agent setup"
  elif [ -r /dev/tty ] && [ -t 1 ]; then
    note "pick a model provider, add your key, choose chat channels"
    printf '\n'
    SMOOTH_AGENT_HOME="$home" "$exe" setup </dev/tty || warn "setup did not finish — rerun: smooth-agent setup"
    return 0
  else
    ok "no terminal attached — run: smooth-agent setup"
  fi

  printf '\n  %sDone.%s Next:  %ssmooth-agent setup%s  →  %ssmooth-agent run%s\n\n' "$B" "$R" "$C" "$R" "$C" "$R"
}

main "$@"
