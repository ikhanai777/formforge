"""Claude API access for the orchestrator (spec sections 5.2 and 5.3).

Three things this layer owns, all of which are cost or reliability decisions
rather than plumbing:

* **Tiering and escalation.** A template fill is a Haiku job; freeform codegen is
  a Sonnet job; a script that has failed three times is an Opus job. Escalating
  the whole conversation once, rather than retrying forever at the same tier, is
  what keeps a pathological request from burning unbounded tokens.
* **Prompt caching.** The DFM rules, the API cheat-sheet and the registry
  summary are a stable ~10k-token prefix shared by every codegen call. Cached,
  they cost a tenth as much; uncached they dominate the bill. The prefix must be
  byte-identical between calls, so everything that varies goes after it.
* **A working offline path.** `OfflineClient` runs the same interface with no
  API key: template matching is lexical, parameters come from schema defaults
  and the numbers in the prompt. This is not a stub -- it is what makes the loop
  testable in CI and demonstrable without credentials, and it degrades to
  "template path only" rather than to "broken".

Model IDs are resolved against `GET /v1/models` at startup rather than trusted
blindly, because a hardcoded ID that has been retired fails at request time, in
production, on a user's generation.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)


class Tier(str, Enum):
    """Which model handles a step, by how hard the step is."""

    FAST = "fast"
    STANDARD = "standard"
    ESCALATED = "escalated"


# Defaults, overridable by environment so a deployment can pin exact snapshots
# without a code change (spec section 5.3).
DEFAULT_MODELS: dict[Tier, str] = {
    Tier.FAST: os.environ.get("FORMFORGE_MODEL_FAST", "claude-haiku-4-5"),
    Tier.STANDARD: os.environ.get("FORMFORGE_MODEL_STANDARD", "claude-sonnet-5"),
    Tier.ESCALATED: os.environ.get("FORMFORGE_MODEL_ESCALATED", "claude-opus-5"),
}

# USD per million tokens, (input, output). Used for the per-generation cost
# accounting the `models` table records; not authoritative for billing.
PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-fable-5": (10.00, 50.00),
}
# Cache reads bill at roughly a tenth of the input rate; writes at 1.25x.
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class Usage:
    """Token and cost accounting for one call, or summed over a generation."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0
    models_used: list[str] = field(default_factory=list)

    def add(self, other: Usage) -> Usage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.calls += other.calls
        self.cost_usd = round(self.cost_usd + other.cost_usd, 6)
        for model in other.models_used:
            if model not in self.models_used:
                self.models_used.append(model)
        return self

    @property
    def cache_hit_rate(self) -> float:
        """Share of input tokens served from cache.

        Worth surfacing: a rate near zero means something is silently
        invalidating the prefix, and the codegen path is costing several times
        what it should.
        """
        total = self.input_tokens + self.cache_read_tokens
        return round(self.cache_read_tokens / total, 4) if total else 0.0

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cache_hit_rate": self.cache_hit_rate,
            "cost_usd": round(self.cost_usd, 5),
            "models_used": self.models_used,
        }


def price(model: str, usage: Usage) -> float:
    """Estimate the USD cost of one call."""
    rates = PRICING.get(model)
    if rates is None:
        # An unpriced model is a real possibility after a release; report zero
        # rather than guessing a rate that would silently skew the cost metric.
        return 0.0
    input_rate, output_rate = rates
    return round(
        (
            usage.input_tokens * input_rate
            + usage.cache_read_tokens * input_rate * CACHE_READ_MULTIPLIER
            + usage.cache_write_tokens * input_rate * CACHE_WRITE_MULTIPLIER
            + usage.output_tokens * output_rate
        )
        / 1_000_000,
        6,
    )


@dataclass
class Completion:
    """One model response, normalised across the real and offline clients."""

    text: str
    usage: Usage
    model: str
    stop_reason: str | None = None
    parsed: Any = None
    refused: bool = False
    refusal_category: str | None = None

    def json_block(self) -> dict | None:
        """Parse a JSON object out of the response text.

        Models reliably emit valid JSON when asked, but wrap it in prose or a
        fenced block often enough that a bare `json.loads` is not safe.
        """
        if self.parsed is not None:
            return self.parsed if isinstance(self.parsed, dict) else None
        return extract_json(self.text)


def extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a response, fences and prose included."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates = [fenced.group(1)] if fenced else []
    candidates.append(text.strip())

    brace = text.find("{")
    if brace >= 0:
        depth = 0
        for index in range(brace, len(text)):
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[brace : index + 1])
                    break

    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return None


def extract_code(text: str) -> str:
    """Pull a Python code block out of a response.

    Falls back to the whole response, because a model that was told to emit only
    code sometimes does exactly that.
    """
    if not text:
        return ""
    fenced = re.findall(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    if fenced:
        return max(fenced, key=len).strip()
    return text.strip()


# ---------------------------------------------------------------------------
# Client interface
# ---------------------------------------------------------------------------


class LLMClient(Protocol):
    """What the orchestrator needs from a model, real or offline."""

    @property
    def available(self) -> bool:
        """Can this client actually reach a model?"""
        ...

    def complete(
        self,
        *,
        system: list[dict] | str,
        messages: list[dict],
        tier: Tier = Tier.STANDARD,
        max_tokens: int = 8000,
        effort: str = "high",
        purpose: str = "",
    ) -> Completion: ...


class AnthropicClient:
    """The real client.

    Adaptive thinking is left on and depth is steered with `effort`: the
    parametric-codegen task is exactly the kind of constrained-reasoning problem
    where thinking pays for itself, and a wrong fillet radius costs a whole
    repair iteration.
    """

    def __init__(
        self,
        models: dict[Tier, str] | None = None,
        *,
        api_key: str | None = None,
        verify_models: bool = True,
    ):
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.models = dict(models or DEFAULT_MODELS)
        self.total = Usage()
        if verify_models:
            self._verify_models()

    @property
    def available(self) -> bool:
        return True

    def _verify_models(self) -> None:
        """Check configured IDs against the live model list.

        A retired ID fails at request time, mid-generation, for a user. Checking
        once at startup turns that into a log line at boot.
        """
        try:
            listed = {model.id for model in self.client.models.list()}
        except Exception as exc:
            log.warning("could not list models to verify configured IDs: %s", exc)
            return
        for tier, model_id in self.models.items():
            if model_id not in listed:
                log.warning(
                    "configured %s model %r is not in the account's model list; "
                    "requests using it will fail",
                    tier.value,
                    model_id,
                )

    def complete(
        self,
        *,
        system: list[dict] | str,
        messages: list[dict],
        tier: Tier = Tier.STANDARD,
        max_tokens: int = 8000,
        effort: str = "high",
        purpose: str = "",
    ) -> Completion:
        model = self.models[tier]
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "output_config": {"effort": effort},
        }

        try:
            response = self.client.messages.create(**request)
        except self._anthropic.APIStatusError as exc:
            raise LLMError(
                f"{purpose or 'request'} failed on {model}: {exc}",
                retryable=exc.status_code >= 500 or exc.status_code == 429,
            ) from exc
        except self._anthropic.APIConnectionError as exc:
            raise LLMError(f"could not reach the Claude API: {exc}", retryable=True) from exc

        usage = self._usage_from(response, model)
        self.total.add(usage)

        refused = response.stop_reason == "refusal"
        category = None
        if refused and getattr(response, "stop_details", None):
            category = getattr(response.stop_details, "category", None)

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        return Completion(
            text=text,
            usage=usage,
            model=model,
            stop_reason=response.stop_reason,
            refused=refused,
            refusal_category=category,
        )

    def _usage_from(self, response: Any, model: str) -> Usage:
        raw = response.usage
        usage = Usage(
            input_tokens=getattr(raw, "input_tokens", 0) or 0,
            output_tokens=getattr(raw, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
            calls=1,
            models_used=[model],
        )
        usage.cost_usd = price(model, usage)
        return usage


class OpenAICompatibleClient:
    """Any endpoint that speaks OpenAI's chat-completions API.

    Which is nearly all of them: Ollama, LM Studio, llama.cpp's server, vLLM,
    OpenRouter, Groq, Together. That makes this the free-model path -- point it
    at a model running on your own machine and nothing leaves it.

    Written against urllib rather than the openai package on purpose. The
    geometry image is deliberately minimal, and one more dependency for a
    handful of JSON POSTs is not a trade worth making.

    Two things are genuinely lost against the Anthropic path, and both are
    reported rather than papered over:

    * **Prompt caching.** The ~10k-token DFM prefix is re-sent every call. On a
      local model that costs latency instead of money, which is the better side
      to be on, but it is why a local run feels slower per step.
    * **Refusal signalling.** There is no `stop_reason == "refusal"`, so a
      model that declines simply returns prose. `policy.py` screens before any
      of this runs, so the safety gate does not depend on it.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        models: dict[Tier, str] | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("FORMFORGE_LLM_BASE_URL", "http://127.0.0.1:11434/v1")
        ).rstrip("/")
        # Local servers ignore the key but many reject a missing header, so a
        # placeholder is friendlier than a 401 nobody expects.
        self.api_key = api_key or os.environ.get("FORMFORGE_LLM_API_KEY", "not-needed")
        fallback = os.environ.get("FORMFORGE_LLM_MODEL", "qwen2.5-coder:7b")
        self.models = models or {
            Tier.FAST: os.environ.get("FORMFORGE_LLM_MODEL_FAST", fallback),
            Tier.STANDARD: os.environ.get("FORMFORGE_LLM_MODEL_STANDARD", fallback),
            Tier.ESCALATED: os.environ.get("FORMFORGE_LLM_MODEL_ESCALATED", fallback),
        }
        self.timeout_s = timeout_s or float(os.environ.get("FORMFORGE_LLM_TIMEOUT", "300"))
        # OpenRouter asks for these to attribute traffic, and sends them to its
        # public leaderboards. Optional everywhere and omitted when unset, so a
        # local server never sees a header it did not ask for.
        self.referer = os.environ.get("FORMFORGE_LLM_REFERER", "")
        self.title = os.environ.get("FORMFORGE_LLM_TITLE", "")
        self.total = Usage()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        if self.referer:
            headers["HTTP-Referer"] = self.referer
        if self.title:
            headers["X-Title"] = self.title
        return headers

    def list_models(self) -> list[dict]:
        """What the endpoint actually offers, right now.

        Hardcoding model IDs is how a working config rots: a free tier's
        lineup changes without notice, and an ID that has been retired fails at
        request time rather than at configuration time. This is the same reason
        the Anthropic client resolves its own IDs rather than trusting them.

        Normalised across the two shapes in the wild -- OpenAI's bare
        `{"data": [{"id": ...}]}` and OpenRouter's, which adds pricing and
        modalities -- because callers should not have to care which they hit.
        """
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{self.base_url}/models", headers=self._headers(), method="GET"
        )
        if request.type not in {"http", "https"}:
            raise LLMError(f"{self.base_url!r} is not an http(s) URL")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise LLMError(
                f"could not list models: HTTP {exc.code} "
                f"{exc.read()[:200].decode(errors='replace')}"
            ) from exc
        except OSError as exc:
            raise LLMError(f"could not reach {self.base_url}: {exc}", retryable=True) from exc

        out: list[dict] = []
        for entry in payload.get("data") or []:
            model_id = entry.get("id")
            if not model_id:
                continue
            pricing = entry.get("pricing") or {}
            # A model is free when the endpoint prices it at zero. Falling back
            # to the `:free` suffix only when there is no pricing block at all,
            # because the price is the fact and the suffix is a convention.
            if pricing:
                try:
                    free = float(pricing.get("prompt", 1)) == 0.0 and (
                        float(pricing.get("completion", 1)) == 0.0
                    )
                except (TypeError, ValueError):
                    free = False
            else:
                free = model_id.endswith(":free")
            modalities = (entry.get("architecture") or {}).get("input_modalities") or []
            out.append(
                {
                    "id": model_id,
                    "free": free,
                    "vision": "image" in modalities,
                    "context": entry.get("context_length"),
                }
            )
        return sorted(out, key=lambda m: (not m["free"], m["id"]))

    @property
    def available(self) -> bool:
        return True

    # -- translation ----------------------------------------------------
    @staticmethod
    def _flatten_system(system: list[dict] | str) -> str:
        """Anthropic's cached system blocks collapse to one string.

        The cache_control markers go with them; there is nothing on the far
        side that would honour them.
        """
        if isinstance(system, str):
            return system
        return "\n\n".join(
            block.get("text", "") for block in system if block.get("type") == "text"
        )

    @staticmethod
    def _content(raw: Any) -> Any:
        """Anthropic content blocks to OpenAI ones, images included."""
        if isinstance(raw, str):
            return raw
        parts: list[dict] = []
        for block in raw:
            kind = block.get("type")
            if kind == "text":
                parts.append({"type": "text", "text": block.get("text", "")})
            elif kind == "image":
                source = block.get("source") or {}
                if source.get("type") == "base64":
                    media = source.get("media_type", "image/png")
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media};base64,{source.get('data', '')}"
                            },
                        }
                    )
        if len(parts) == 1 and parts[0]["type"] == "text":
            return parts[0]["text"]
        return parts

    def complete(
        self,
        *,
        system: list[dict] | str,
        messages: list[dict],
        tier: Tier = Tier.STANDARD,
        max_tokens: int = 8000,
        effort: str = "high",
        purpose: str = "",
    ) -> Completion:
        import urllib.error
        import urllib.request

        model = self.models[tier]
        payload: list[dict] = []
        prompt = self._flatten_system(system)
        if prompt:
            payload.append({"role": "system", "content": prompt})
        for message in messages:
            payload.append(
                {
                    "role": message.get("role", "user"),
                    "content": self._content(message.get("content", "")),
                }
            )

        body = json.dumps(
            {"model": model, "messages": payload, "max_tokens": max_tokens, "stream": False}
        ).encode()
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=self._headers(),
            method="POST",
        )
        if request.type not in {"http", "https"}:
            raise LLMError(f"{self.base_url!r} is not an http(s) URL")

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                data = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:400].decode(errors="replace")
            raise LLMError(
                f"{purpose or 'request'} failed on {model}: HTTP {exc.code} {detail}",
                retryable=exc.code >= 500 or exc.code == 429,
            ) from exc
        except OSError as exc:
            raise LLMError(
                f"could not reach a model server at {self.base_url}: {exc}. "
                f"Start one, or set FORMFORGE_LLM_BASE_URL to where yours is.",
                retryable=True,
            ) from exc

        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{model} returned no choices", retryable=True)
        text = (choices[0].get("message") or {}).get("content") or ""

        raw = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw.get("prompt_tokens") or 0),
            output_tokens=int(raw.get("completion_tokens") or 0),
            calls=1,
            models_used=[model],
        )
        # Zero rather than unknown: a model on your own hardware has no per
        # token price, and the stats table should not imply one.
        usage.cost_usd = 0.0
        self.total.add(usage)
        return Completion(
            text=text,
            usage=usage,
            model=model,
            stop_reason=choices[0].get("finish_reason"),
        )


class LLMError(Exception):
    """A model call failed in a way the caller may want to retry."""

    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class OfflineClient:
    """A deterministic stand-in for when no API key is configured.

    It cannot write geometry, so the freeform path is unavailable and says so.
    What it *can* do is the template path end to end: the registry's lexical
    matcher picks a template and the numbers in the prompt fill its schema. That
    covers the majority of real traffic by design (spec section 6.2), which is
    why this is worth having rather than a hard failure.

    Every method reports `offline: true` in its output so nothing downstream can
    mistake a heuristic answer for a model's.
    """

    def __init__(self) -> None:
        self.total = Usage()

    @property
    def available(self) -> bool:
        return False

    def complete(
        self,
        *,
        system: list[dict] | str,
        messages: list[dict],
        tier: Tier = Tier.STANDARD,
        max_tokens: int = 8000,
        effort: str = "high",
        purpose: str = "",
    ) -> Completion:
        raise LLMError(
            f"no Claude API client is configured, so {purpose or 'this step'} "
            "cannot run. Set ANTHROPIC_API_KEY (or run `ant auth login`) to "
            "enable intent parsing, freeform code generation and visual "
            "critique. The template path works without it."
        )


def build_client(
    *,
    prefer_offline: bool = False,
    models: dict[Tier, str] | None = None,
) -> LLMClient:
    """Return the best available client, falling back to offline.

    Deliberately never raises: a missing key is a reduced-capability mode, not a
    crash, and the caller finds out by checking `.available`.
    """
    if prefer_offline or os.environ.get("FORMFORGE_OFFLINE") == "1":
        return OfflineClient()

    # An explicit backend wins; otherwise a configured base URL is taken as the
    # user having pointed us at their own model, which is the only reason to
    # set it.
    backend = os.environ.get("FORMFORGE_LLM_BACKEND", "").strip().lower()
    if not backend and os.environ.get("FORMFORGE_LLM_BASE_URL"):
        backend = "openai"
    if backend in {"openai", "openai-compatible", "ollama", "local"}:
        return OpenAICompatibleClient(models=models)
    if backend == "offline":
        return OfflineClient()

    try:
        import anthropic  # noqa: F401
    except ImportError:
        log.info("the anthropic package is not installed; running offline")
        return OfflineClient()
    try:
        return AnthropicClient(models=models)
    except Exception as exc:
        log.info("no usable Claude API credentials (%s); running offline", exc)
        return OfflineClient()


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------


def cached_system(*blocks: str) -> list[dict]:
    """Build a system prompt whose stable prefix is cached.

    The cache breakpoint goes on the last block, so everything before it is
    cached together. Callers must pass the stable blocks first and anything
    request-specific last, or the prefix changes every call and the cache never
    hits (spec section 5.2).
    """
    parts = [text for text in blocks if text]
    if not parts:
        return []
    payload = [{"type": "text", "text": text} for text in parts]
    payload[-1]["cache_control"] = {"type": "ephemeral"}
    return payload


def image_block(path: str | Path, media_type: str = "image/png") -> dict:
    """A base64 image content block, for the visual critique step."""
    data = base64.standard_b64encode(Path(path).read_bytes()).decode("ascii")
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": data},
    }


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}
