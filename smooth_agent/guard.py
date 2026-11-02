"""Secret redaction and environment hygiene.

`redact()` runs over everything Smooth Agent prints or logs (tool calls, tool
results, errors), so an API key that shows up in a command's output does not end
up on the screen or in a log file.

Unlike the muse operator tool this came from, public on-chain addresses
(0x + 40 hex) are NOT shortened: they are public and the agent needs them whole.
Private keys (0x + 64 hex) and every known token shape are.

`scrubbed_env()` builds the environment for commands the agent runs, without the
owner's provider keys and other secrets, so `env` / `printenv` in a terminal
call cannot hand them to the model.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping


def _keep(n: int):
    return lambda m: m.group(0)[:n] + "…"


_REDACTIONS = (
    # 32-byte private keys / seeds in hex (keep a short prefix + suffix to tell them apart)
    (re.compile(r"\b0x[0-9a-fA-F]{64}\b"), lambda m: m.group(0)[:6] + "…" + m.group(0)[-4:]),
    (re.compile(r"(?i)(auth_token=|ct0=)[^;\s\"']+"), lambda m: m.group(1) + "…"),
    (re.compile(r"(?i)([?&](?:api[_-]?key|key|apikey|token|access_token|secret)=)[^&\s\"']+"), lambda m: m.group(1) + "…"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"), lambda m: m.group(1) + "…"),
    (re.compile(r"(?i)(x-api-key:\s*)\S+"), lambda m: m.group(1) + "…"),
    # Known token shapes by prefix: Anthropic (sk-ant-), OpenAI (sk-, sk-proj-), GitHub, Slack,
    # AWS key ids, Google API keys, Stripe, Hugging Face, Groq, xAI, router keys (sm-), JWTs.
    (re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{12,}"), _keep(10)),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{12,}"), _keep(7)),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), _keep(7)),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), _keep(11)),
    (re.compile(r"\bxox[bparse]-[A-Za-z0-9-]{10,}"), _keep(6)),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), _keep(8)),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{20,}"), _keep(8)),
    (re.compile(r"\b[rs]k_live_[A-Za-z0-9]{12,}"), _keep(8)),
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), _keep(5)),
    (re.compile(r"\bgsk_[A-Za-z0-9]{20,}"), _keep(6)),
    (re.compile(r"\bxai-[A-Za-z0-9]{20,}"), _keep(6)),
    (re.compile(r"\bsm-[a-f0-9]{24,}"), _keep(5)),
    (re.compile(r"\b(?:ey[A-Za-z0-9_-]{8,}\.){2}[A-Za-z0-9_-]{8,}"), _keep(6)),
    # Seed phrases written as "mnemonic: word word ..." / "seed phrase = ..."
    (re.compile(r"(?i)\b(mnemonic|seed[ _]?phrase|recovery[ _]?phrase)(\s*[:=]\s*)([a-z]+(?:\s+[a-z]+){11,23})"),
     lambda m: m.group(1) + m.group(2) + "…"),
    # Generic NAME_KEY = value / SECRET: value / PASSWORD=value assignments.
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:api[_-]?key|secret|token|passphrase|password|private[_-]?key))\b(\s*[:=]\s*)['\"]?([A-Za-z0-9_\-/+.=]{6,})"),
     lambda m: m.group(1) + m.group(2) + m.group(3)[:4] + "…"),
)


def redact(text: object) -> str:
    out = str(text)
    for pattern, repl in _REDACTIONS:
        out = pattern.sub(repl, out)
    return out


def redact_lines(lines: Iterable[str]) -> list[str]:
    return [redact(line) for line in lines]


# Env var names that hold secrets: provider keys and anything that looks like one.
_SECRET_NAME = re.compile(r"(?i)(api[_-]?key|secret|token|password|passphrase|private[_-]?key|mnemonic|credential|_pat$|^pat_)")
# Kept even though the name matches (harmless and often needed by tools).
_ALLOW = {"GPG_TTY", "SSH_AUTH_SOCK_TOKENLESS"}


def scrubbed_env(extra_secret_names: Iterable[str] = (), base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without secrets, for commands the agent runs."""
    env = dict(base if base is not None else os.environ)
    drop = {n for n in extra_secret_names if n}
    for name in list(env):
        if name in _ALLOW:
            continue
        if name in drop or _SECRET_NAME.search(name):
            env.pop(name, None)
    env["SMOOTH_AGENT"] = "1"
    return env


__all__ = ["redact", "redact_lines", "scrubbed_env"]
