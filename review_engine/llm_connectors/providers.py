"""MCP client connector: uniform multi-provider model abstraction.

Battalion acts as an MCP *client* connecting OUT to model backends:

* OpenAI  (Codex / GPT, ``chat/completions`` schema)
* Hermes  (Nous Hermes via a hosted OpenAI-compatible endpoint, e.g. OpenRouter)
* Anthropic (Claude, ``messages`` schema)

The B3 chat surface consumes :class:`MultiProviderClient` to route a prompt to
one provider or fan out to all of them simultaneously (powering the compare
view).

Design contract (RAYAAAA-246, synthetic-only):

* No third-party HTTP/SDK dependency — egress uses stdlib ``urllib`` so
  build/CI stays lean and matches the existing ``OllamaConnector``.
* Every provider supports a **mock mode** returning deterministic synthetic
  output with no network I/O, so tests/CI need no paid API keys.
* Credentials are read from the environment only; nothing secret is stored in
  source. A provider with no key silently falls back to mock mode.
* Real egress is gated behind the ``MCP_CONNECTOR_ENABLED`` feature flag and is
  OFF by default. When disabled every provider is forced to mock mode.
* Transport is injectable, so unit tests exercise the request/response mapping
  with a fake transport and never touch the network.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# A transport takes (url, headers, json-body) and returns the parsed JSON
# response dict. It is injectable so tests can supply canned responses.
Transport = Callable[[str, Mapping[str, str], dict], dict]

DEFAULT_TIMEOUT = 60.0


@dataclass
class ProviderRequest:
    """A model-agnostic request. ``context`` holds optional grounding snippets
    (e.g. retrieved SourceChunks from B2); each item is rendered into the
    prompt. Retrieval itself lives in B2 and is intentionally kept separate."""

    prompt: str
    context: Sequence[str] = field(default_factory=tuple)
    system: str = (
        "You are Battalion, an evidence-review assistant. Answer only from the "
        "provided context. Do not invent facts or assert that fraud occurred."
    )
    max_tokens: int = 1024
    temperature: float = 0.2

    def rendered_prompt(self) -> str:
        if not self.context:
            return self.prompt
        blocks = "\n\n".join(f"[Context {i + 1}]\n{c}" for i, c in enumerate(self.context))
        return f"{blocks}\n\n[Question]\n{self.prompt}"


@dataclass
class ProviderResponse:
    """Uniform response envelope returned by every provider/adapter."""

    provider: str
    model: str
    text: str = ""
    ok: bool = True
    error: str | None = None
    mock: bool = False
    latency_ms: float = 0.0

    @classmethod
    def failure(cls, provider: str, model: str, error: str, latency_ms: float = 0.0) -> "ProviderResponse":
        return cls(provider=provider, model=model, ok=False, error=error, latency_ms=latency_ms)


def _http_transport(url: str, headers: Mapping[str, str], body: dict, timeout: float) -> dict:
    payload = json.dumps(body).encode("utf-8")
    request = Request(url, data=payload, headers=dict(headers), method="POST")
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class BaseProvider:
    """Common timeout / error-handling / mock scaffolding.

    Subclasses implement :meth:`_build` (URL, headers, body) and
    :meth:`_parse` (extract text from the response dict)."""

    name = "base"

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        mock: bool = False,
        transport: Transport | None = None,
    ):
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.timeout = timeout
        # No key => mock mode, so the connector never emits an unauthenticated
        # request or leaks that a key is missing at runtime.
        self.mock = mock or not api_key
        self._transport = transport

    # --- to be overridden -------------------------------------------------
    default_base_url = ""

    def _build(self, request: ProviderRequest) -> tuple[str, dict[str, str], dict]:
        raise NotImplementedError

    def _parse(self, data: dict) -> str:
        raise NotImplementedError

    # --- public API -------------------------------------------------------
    def generate(self, request: ProviderRequest) -> ProviderResponse:
        started = time.monotonic()
        if self.mock:
            latency = (time.monotonic() - started) * 1000
            return ProviderResponse(
                provider=self.name,
                model=self.model,
                text=self._mock_text(request),
                mock=True,
                latency_ms=latency,
            )
        try:
            url, headers, body = self._build(request)
            transport = self._transport or (
                lambda u, h, b: _http_transport(u, h, b, self.timeout)
            )
            data = transport(url, headers, body)
            text = self._parse(data)
        except HTTPError as exc:  # non-2xx
            return ProviderResponse.failure(
                self.name, self.model, f"http {exc.code}: {exc.reason}",
                (time.monotonic() - started) * 1000,
            )
        except (URLError, TimeoutError, OSError) as exc:  # network/timeout
            return ProviderResponse.failure(
                self.name, self.model, f"network error: {exc}",
                (time.monotonic() - started) * 1000,
            )
        except (KeyError, ValueError, TypeError) as exc:  # malformed response
            return ProviderResponse.failure(
                self.name, self.model, f"bad response: {exc}",
                (time.monotonic() - started) * 1000,
            )
        return ProviderResponse(
            provider=self.name,
            model=self.model,
            text=text,
            latency_ms=(time.monotonic() - started) * 1000,
        )

    def _mock_text(self, request: ProviderRequest) -> str:
        preview = request.prompt.strip().splitlines()[0] if request.prompt.strip() else ""
        preview = preview[:80]
        return (
            f"[MOCK {self.name}:{self.model}] synthetic response to: {preview!r}. "
            f"({len(request.context)} context snippet(s) supplied.)"
        )


class _OpenAICompatibleProvider(BaseProvider):
    """OpenAI ``chat/completions`` schema — shared by OpenAI (Codex/GPT) and
    Hermes served through a hosted OpenAI-compatible gateway."""

    def _build(self, request: ProviderRequest) -> tuple[str, dict[str, str], dict]:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.rendered_prompt()},
            ],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        return f"{self.base_url}/chat/completions", headers, body

    def _parse(self, data: dict) -> str:
        return data["choices"][0]["message"]["content"].strip()


class OpenAIProvider(_OpenAICompatibleProvider):
    name = "openai"
    default_base_url = "https://api.openai.com/v1"


class HermesProvider(_OpenAICompatibleProvider):
    name = "hermes"
    # Default to OpenRouter, which serves Nous Hermes (and can proxy Codex +
    # Claude under one key) via the OpenAI-compatible schema.
    default_base_url = "https://openrouter.ai/api/v1"


class AnthropicProvider(BaseProvider):
    name = "anthropic"
    default_base_url = "https://api.anthropic.com/v1"
    anthropic_version = "2023-06-01"

    def _build(self, request: ProviderRequest) -> tuple[str, dict[str, str], dict]:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key or "",
            "anthropic-version": self.anthropic_version,
        }
        body = {
            "model": self.model,
            "system": request.system,
            "messages": [{"role": "user", "content": request.rendered_prompt()}],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        return f"{self.base_url}/messages", headers, body

    def _parse(self, data: dict) -> str:
        blocks = data["content"]
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


# --- provider registry / fan-out ----------------------------------------------

# Env var name and default model per provider. Keys are read from the
# environment at build time; nothing secret is stored here.
PROVIDER_SPECS: dict[str, dict[str, object]] = {
    "openai": {
        "cls": OpenAIProvider,
        "key_env": "OPENAI_API_KEY",
        "base_env": "OPENAI_BASE_URL",
        "model_env": "OPENAI_MODEL",
        "default_model": "gpt-4o-mini",
    },
    "hermes": {
        "cls": HermesProvider,
        "key_env": "HERMES_API_KEY",
        "base_env": "HERMES_BASE_URL",
        "model_env": "HERMES_MODEL",
        "default_model": "nousresearch/hermes-3-llama-3.1-70b",
    },
    "anthropic": {
        "cls": AnthropicProvider,
        "key_env": "ANTHROPIC_API_KEY",
        "base_env": "ANTHROPIC_BASE_URL",
        "model_env": "ANTHROPIC_MODEL",
        "default_model": "claude-sonnet-5",
    },
}


class MultiProviderClient:
    """Routes a request to a single provider or fans out to all of them.

    This is the object B3 (the chat surface) consumes."""

    def __init__(self, providers: Mapping[str, BaseProvider]):
        self._providers = dict(providers)

    @property
    def provider_names(self) -> list[str]:
        return list(self._providers)

    def get(self, name: str) -> BaseProvider:
        try:
            return self._providers[name]
        except KeyError:
            raise KeyError(f"unknown provider {name!r}; have {self.provider_names}") from None

    def generate(self, provider: str, request: ProviderRequest) -> ProviderResponse:
        return self.get(provider).generate(request)

    async def fan_out(
        self, request: ProviderRequest, providers: Iterable[str] | None = None
    ) -> dict[str, ProviderResponse]:
        """Async simultaneous fan-out. Each provider is isolated: one failing
        or slow provider never blocks or breaks the others."""
        import asyncio

        names = list(providers) if providers is not None else self.provider_names

        async def _one(name: str) -> ProviderResponse:
            provider = self.get(name)
            try:
                # Providers do blocking stdlib I/O; run each in a thread so the
                # fan-out is genuinely concurrent.
                return await asyncio.wait_for(
                    asyncio.to_thread(provider.generate, request),
                    timeout=provider.timeout + 5,
                )
            except asyncio.TimeoutError:
                return ProviderResponse.failure(name, provider.model, "timeout")
            except Exception as exc:  # never let one provider break the batch
                return ProviderResponse.failure(name, provider.model, f"error: {exc}")

        results = await asyncio.gather(*(_one(n) for n in names))
        return dict(zip(names, results))

    def fan_out_sync(
        self, request: ProviderRequest, providers: Iterable[str] | None = None
    ) -> dict[str, ProviderResponse]:
        """Thread-pool fan-out for synchronous callers (e.g. Streamlit in B3)."""
        names = list(providers) if providers is not None else self.provider_names
        results: dict[str, ProviderResponse] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
            futures = {pool.submit(self.get(n).generate, request): n for n in names}
            for future in concurrent.futures.as_completed(futures):
                name = futures[future]
                provider = self.get(name)
                try:
                    results[name] = future.result()
                except Exception as exc:  # pragma: no cover - generate() self-guards
                    results[name] = ProviderResponse.failure(name, provider.model, f"error: {exc}")
        return {n: results[n] for n in names}


def build_provider(name: str, *, force_mock: bool = False, transport: Transport | None = None) -> BaseProvider:
    spec = PROVIDER_SPECS[name]
    cls = spec["cls"]  # type: ignore[assignment]
    api_key = os.getenv(str(spec["key_env"]))
    base_url = os.getenv(str(spec["base_env"]))
    model = os.getenv(str(spec["model_env"]), str(spec["default_model"]))
    return cls(  # type: ignore[call-arg]
        model=model,
        api_key=api_key,
        base_url=base_url,
        mock=force_mock,
        transport=transport,
    )


def build_default_client(
    providers: Iterable[str] | None = None, *, transport: Transport | None = None
) -> MultiProviderClient:
    """Construct the client B3 consumes, honouring the feature flag.

    Real egress happens only when ``MCP_CONNECTOR_ENABLED=1``. Otherwise (the
    default) every provider is forced into mock mode, so the connector is inert
    and safe until the Phase C gate. ``MCP_MOCK=1`` also forces mock mode even
    when the flag is on (useful for staging/demo)."""
    enabled = os.getenv("MCP_CONNECTOR_ENABLED", "0") == "1"
    forced = os.getenv("MCP_MOCK", "0") == "1"
    force_mock = forced or not enabled
    names = list(providers) if providers is not None else list(PROVIDER_SPECS)
    built = {
        name: build_provider(name, force_mock=force_mock, transport=transport)
        for name in names
    }
    return MultiProviderClient(built)
