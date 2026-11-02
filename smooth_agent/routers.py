"""Multi-router LLM layer for Smooth Agent.

A self-hosted Smooth Agent talks to whatever LLM endpoint its owner configures:
OpenAI, Anthropic, DeepSeek, GLM (Zhipu), Groq, OpenRouter, a local Smooth
Router / 9router, or any OpenAI-compatible or Anthropic-native gateway. The
owner brings their own key. This module is the single place that knows how to
reach each one, so the rest of the agent only ever asks for "a chat completion"
and never cares who serves it.

Design goals:
- Zero third-party dependencies (stdlib only), so `pip install` is light and a
  box with just Python can run it.
- One uniform request/response shape (OpenAI chat-completions), with an
  Anthropic translation path for Anthropic-native endpoints.
- Config from a dict / TOML / env, with every secret read from the environment
  (never written to disk by this module).
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Iterator


class RouterError(RuntimeError):
    """A provider call failed. `status` is the HTTP status when there was one."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


# Known provider presets: base URL + wire protocol. The owner only has to name
# the provider and give a key; everything else has a sane default they can
# still override. "openai" wire = /chat/completions; "anthropic" wire =
# /v1/messages with the Anthropic translation.
PRESETS: dict[str, dict[str, Any]] = {
    "openai": {"base_url": "https://api.openai.com/v1", "wire": "openai", "key_env": "OPENAI_API_KEY"},
    "anthropic": {"base_url": "https://api.anthropic.com", "wire": "anthropic", "key_env": "ANTHROPIC_API_KEY"},
    "deepseek": {"base_url": "https://api.deepseek.com/v1", "wire": "openai", "key_env": "DEEPSEEK_API_KEY"},
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "wire": "openai", "key_env": "GLM_API_KEY"},
    "groq": {"base_url": "https://api.groq.com/openai/v1", "wire": "openai", "key_env": "GROQ_API_KEY"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "wire": "openai", "key_env": "OPENROUTER_API_KEY"},
    "kimi": {"base_url": "https://api.moonshot.ai/v1", "wire": "openai", "key_env": "KIMI_API_KEY"},
    # Self-hosted gateways the owner may already run (see the Node Smooth Router / 9router).
    "smooth-router": {"base_url": "http://127.0.0.1:20150/v1", "wire": "openai", "key_env": "SMOOTH_ROUTER_KEY"},
    "9router": {"base_url": "http://127.0.0.1:20140/v1", "wire": "openai", "key_env": "NINEROUTER_KEY"},
    # Any OpenAI-compatible endpoint not listed above.
    "openai-compatible": {"base_url": "", "wire": "openai", "key_env": ""},
}

ANTHROPIC_VERSION = "2023-06-01"


@dataclass(frozen=True)
class Provider:
    """One configured LLM endpoint."""

    name: str  # the owner's label, e.g. "main" or "deepseek"
    provider: str  # a key in PRESETS
    model: str  # default model for this provider
    base_url: str = ""
    wire: str = "openai"  # "openai" | "anthropic"
    api_key: str = ""  # resolved secret; never logged
    key_env: str = ""  # env var the key came from (for diagnostics)
    headers: dict[str, str] = field(default_factory=dict)  # extra headers (e.g. OpenRouter referer)
    timeout: float = 120.0

    @classmethod
    def from_config(cls, name: str, cfg: dict[str, Any], *, env: dict[str, str] | None = None) -> "Provider":
        env = env if env is not None else dict(os.environ)
        provider = str(cfg.get("provider", name)).lower()
        preset = PRESETS.get(provider, PRESETS["openai-compatible"])
        base_url = str(cfg.get("base_url") or preset["base_url"]).rstrip("/")
        if not base_url:
            raise RouterError(f"provider '{name}': base_url is required for '{provider}'")
        wire = str(cfg.get("wire") or preset["wire"])
        key_env = str(cfg.get("key_env") or preset["key_env"] or "")
        # Precedence: explicit api_key in config (discouraged) > env var named by key_env.
        api_key = str(cfg.get("api_key") or (env.get(key_env, "") if key_env else ""))
        model = str(cfg.get("model") or "")
        if not model:
            raise RouterError(f"provider '{name}': a default model is required")
        headers = {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}
        return cls(
            name=name, provider=provider, model=model, base_url=base_url, wire=wire,
            api_key=api_key, key_env=key_env, headers=headers, timeout=float(cfg.get("timeout", 120.0)),
        )

    @property
    def ready(self) -> bool:
        """True when a key is present (or the endpoint is local and needs none)."""
        return bool(self.api_key) or self._is_local()

    def _is_local(self) -> bool:
        return "127.0.0.1" in self.base_url or "localhost" in self.base_url

    def redacted(self) -> dict[str, Any]:
        return {
            "name": self.name, "provider": self.provider, "model": self.model,
            "base_url": self.base_url, "wire": self.wire,
            "key": ("set" if self.api_key else ("not-needed" if self._is_local() else "MISSING")),
            "key_env": self.key_env,
        }


def _auth_headers(p: Provider) -> dict[str, str]:
    h = {"content-type": "application/json", **p.headers}
    if p.wire == "anthropic":
        if p.api_key:
            h["x-api-key"] = p.api_key
        h.setdefault("anthropic-version", ANTHROPIC_VERSION)
    elif p.api_key:
        h["authorization"] = f"Bearer {p.api_key}"
    return h


def _anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    """OpenAI message content (string or parts) -> Anthropic content blocks."""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url") if isinstance(part.get("image_url"), dict) else part.get("image_url")
            url = str(url or "")
            if url.startswith("data:") and ";base64," in url:
                media, data = url[5:].split(";base64,", 1)
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
            elif url:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
    return blocks


def _anthropic_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for t in tools or []:
        fn = t.get("function", t)
        out.append({"name": fn.get("name"), "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


def _to_anthropic_body(messages: list[dict[str, Any]], model: str, max_tokens: int, stream: bool,
                       extra: dict[str, Any]) -> dict[str, Any]:
    """Translate an OpenAI-style conversation (incl. tool calls and tool results) to an
    Anthropic /v1/messages body. Consecutive messages of the same role are merged,
    since Anthropic requires user/assistant to alternate."""
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            if isinstance(content, str) and content:
                system_parts.append(content)
            continue
        if role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id") or "call",
                           "content": content if isinstance(content, str) else json.dumps(content)}])
            continue
        if role == "assistant":
            blocks = _anthropic_blocks(content)
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                args = fn.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else {}
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                blocks.append({"type": "tool_use", "id": call.get("id") or "call", "name": fn.get("name", ""),
                               "input": args if isinstance(args, dict) else {"value": args}})
            push("assistant", blocks)
            continue
        push("user", _anthropic_blocks(content))

    body: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": out, "stream": stream}
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    extra = dict(extra)
    tools = extra.pop("tools", None)
    choice = extra.pop("tool_choice", None)
    if tools:
        body["tools"] = _anthropic_tools(tools)
        if choice in ("auto", None):
            body["tool_choice"] = {"type": "auto"}
        elif choice == "required":
            body["tool_choice"] = {"type": "any"}
        elif choice == "none":
            body.pop("tools", None)
        elif isinstance(choice, dict) and choice.get("function", {}).get("name"):
            body["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
    for k, v in extra.items():
        body.setdefault(k, v)
    return body


_STOP_REASON = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls", "max_tokens": "length"}


def _from_anthropic_message(j: dict[str, Any]) -> tuple[str, list[dict[str, Any]], dict[str, Any], str | None]:
    """Anthropic response -> (text, OpenAI-shape tool_calls, OpenAI assistant message, finish_reason)."""
    text_parts: list[str] = []
    calls: list[dict[str, Any]] = []
    for b in j.get("content") or []:
        if b.get("type") == "text":
            text_parts.append(b.get("text", ""))
        elif b.get("type") == "tool_use":
            calls.append({"id": b.get("id"), "type": "function",
                          "function": {"name": b.get("name", ""), "arguments": json.dumps(b.get("input") or {})}})
    text = "".join(text_parts)
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if calls:
        message["tool_calls"] = calls
    return text, calls, message, _STOP_REASON.get(j.get("stop_reason"), j.get("stop_reason"))


def _request(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 (trusted, owner-configured URL)
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")[:600]
        msg = text
        try:
            j = json.loads(text)
            msg = j.get("error", {}).get("message") or j.get("message") or text
        except Exception:
            pass
        raise RouterError(f"HTTP {e.code}: {msg}", status=e.code,
                          retryable=e.code in (408, 409, 429, 500, 502, 503, 504, 529)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RouterError(f"connect: {e}", retryable=True) from None


@dataclass
class Completion:
    text: str
    model: str
    provider: str
    usage: dict[str, Any] = field(default_factory=dict)
    first_ms: int | None = None
    # OpenAI-shape tool calls the model asked for: [{"id","type","function":{"name","arguments"(str)}}].
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str | None = None
    # The raw assistant message dict (OpenAI wire), ready to append to the conversation.
    message: dict[str, Any] = field(default_factory=dict)


class Router:
    """Holds the configured providers and makes chat calls, with an ordered
    fallback chain so one dead endpoint does not stop the agent."""

    def __init__(self, providers: list[Provider], *, default: str | None = None, fallbacks: list[str] | None = None):
        if not providers:
            raise RouterError("no providers configured")
        self.providers = {p.name: p for p in providers}
        self.default = default or providers[0].name
        if self.default not in self.providers:
            raise RouterError(f"default provider '{self.default}' is not configured")
        self.fallbacks = [f for f in (fallbacks or []) if f in self.providers]

    @classmethod
    def from_config(cls, cfg: dict[str, Any], *, env: dict[str, str] | None = None) -> "Router":
        provs = [Provider.from_config(name, pc, env=env) for name, pc in (cfg.get("providers") or {}).items()]
        return cls(provs, default=cfg.get("default"), fallbacks=cfg.get("fallbacks"))

    def chain(self, name: str | None = None) -> list[Provider]:
        first = name or self.default
        order = [first] + [f for f in self.fallbacks if f != first]
        return [self.providers[n] for n in order if n in self.providers]

    def complete(self, messages: list[dict[str, Any]], *, provider: str | None = None, model: str | None = None,
                 max_tokens: int = 1024, extra: dict[str, Any] | None = None) -> Completion:
        """Non-streaming completion. Tries the chosen provider, then fallbacks."""
        last: RouterError | None = None
        for p in self.chain(provider):
            if not p.ready:
                last = RouterError(f"provider '{p.name}' has no key ({p.key_env or 'n/a'})")
                continue
            try:
                return self._complete_one(p, messages, model or p.model, max_tokens, extra or {})
            except RouterError as e:
                last = e
                if not e.retryable:
                    # a bad request / auth error will fail the same way on a retry of this provider
                    continue
        raise last or RouterError("all providers failed")

    def _complete_one(self, p: Provider, messages, model, max_tokens, extra) -> Completion:
        t0 = time.time()
        if p.wire == "anthropic":
            body = _to_anthropic_body(messages, model, max_tokens, False, extra)
            resp = _request(f"{p.base_url}/v1/messages", _auth_headers(p), body, p.timeout)
            j = json.loads(resp.read().decode("utf-8"))
            text, calls, message, finish = _from_anthropic_message(j)
            u = j.get("usage", {}) or {}
            usage = {**u, "prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0),
                     "total_tokens": (u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0)}
            return Completion(text=text, model=model, provider=p.name, usage=usage, first_ms=int((time.time() - t0) * 1000),
                              tool_calls=calls, finish_reason=finish, message=message)
        body = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False, **extra}
        resp = _request(f"{p.base_url}/chat/completions", _auth_headers(p), body, p.timeout)
        raw = resp.read().decode("utf-8")
        # Some gateways append a trailing SSE "[DONE]" even to a non-stream body.
        raw = raw.replace("data: [DONE]", "").strip()
        j = json.loads(raw)
        choice = (j.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        return Completion(text=text, model=model, provider=p.name, usage=j.get("usage", {}) or {},
                          first_ms=int((time.time() - t0) * 1000),
                          tool_calls=list(message.get("tool_calls") or []),
                          finish_reason=choice.get("finish_reason"), message=message)

    def provider(self, name: str | None = None) -> Provider:
        p = self.providers.get(name or self.default)
        if not p:
            raise RouterError(f"provider '{name}' is not configured")
        return p

    def post(self, path: str, body: dict[str, Any], *, provider: str | None = None, timeout: float | None = None) -> tuple[bytes, str]:
        """POST to another endpoint of an OpenAI-compatible provider (images, audio, embeddings).
        Returns (raw body, content type)."""
        p = self.provider(provider)
        if p.wire != "openai":
            raise RouterError(f"provider '{p.name}' speaks the {p.wire} protocol; use an OpenAI-compatible provider for {path}")
        if not p.ready:
            raise RouterError(f"provider '{p.name}' has no key ({p.key_env or 'n/a'})")
        resp = _request(f"{p.base_url}/{path.lstrip('/')}", _auth_headers(p), body, timeout or p.timeout)
        return resp.read(), resp.headers.get("content-type", "")

    def stream(self, messages: list[dict[str, Any]], *, provider: str | None = None, model: str | None = None,
               max_tokens: int = 1024, extra: dict[str, Any] | None = None) -> Iterator[str]:
        """Stream text deltas from the first provider that answers."""
        p = self.chain(provider)[0]
        if not p.ready:
            raise RouterError(f"provider '{p.name}' has no key ({p.key_env or 'n/a'})")
        model = model or p.model
        extra = extra or {}
        if p.wire == "anthropic":
            body = _to_anthropic_body(messages, model, max_tokens, True, extra)
            resp = _request(f"{p.base_url}/v1/messages", _auth_headers(p), body, p.timeout)
            yield from _anthropic_text_deltas(_sse_events(resp))
        else:
            body = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": True, **extra}
            resp = _request(f"{p.base_url}/chat/completions", _auth_headers(p), body, p.timeout)
            yield from _openai_text_deltas(_sse_events(resp))


def _sse_events(resp) -> Iterator[str]:
    """Yield the `data:` payloads of an SSE byte stream."""
    buf = b""
    for chunk in iter(lambda: resp.read(1024), b""):
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.strip()
            if line.startswith(b"data:"):
                yield line[5:].strip().decode("utf-8", "replace")


def _openai_text_deltas(events: Iterable[str]) -> Iterator[str]:
    for data in events:
        if data == "[DONE]" or not data:
            continue
        try:
            j = json.loads(data)
        except json.JSONDecodeError:
            continue
        if j.get("error"):
            raise RouterError(f"upstream: {j['error'].get('message', 'error')}")
        delta = (j.get("choices") or [{}])[0].get("delta") or {}
        if delta.get("content"):
            yield delta["content"]


def _anthropic_text_deltas(events: Iterable[str]) -> Iterator[str]:
    for data in events:
        if not data:
            continue
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        if ev.get("type") == "error":
            raise RouterError(f"upstream: {ev.get('error', {}).get('message', 'error')}")
        if ev.get("type") == "content_block_delta":
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                yield d["text"]


__all__ = ["Router", "Provider", "Completion", "RouterError", "PRESETS", "replace"]
