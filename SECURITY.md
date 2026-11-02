# Security

Smooth Agent runs on your own machine with your keys, so please report security problems
privately rather than in a public issue.

- Use GitHub's **"Report a vulnerability"** (Security → Advisories) on this repository, or
  email **dev@smooth.family**.
- Include what you found, how to reproduce it, and what an attacker could do with it.
- We aim to answer within a few days and will credit you in the release notes unless you
  prefer otherwise.

Good things to know when you run it:

- Only ids in a channel's `owners` list get answers. Keep that list to yourself.
- `[tools] terminal = true` and `files = "full"` give the agent real power on the machine.
  Leave `confirm = true` on unless you trust every message that reaches the agent.
- Keys live in `~/.smooth-agent/.env` (mode 600). They are kept out of the environment of shell
  commands and hidden in printed output. With `files = "full"`, reading `.env` or other secret
  files asks you first — unless `confirm = false`, in which case the agent can read them.
