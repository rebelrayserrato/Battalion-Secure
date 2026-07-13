"""Unit tests for the MCP multi-model connector (RAYAAAA-246).

No network is touched: every non-mock path uses an injected fake transport.
"""

from __future__ import annotations

import asyncio

import pytest

from review_engine.llm_connectors.providers import (
    AnthropicProvider,
    HermesProvider,
    MultiProviderClient,
    OpenAIProvider,
    ProviderRequest,
    ProviderResponse,
    build_default_client,
    build_provider,
)


def _openai_transport(reply="hi from openai"):
    """A fake transport that records the call and returns an OpenAI-shaped body."""
    calls = []

    def transport(url, headers, body):
        calls.append({"url": url, "headers": headers, "body": body})
        return {"choices": [{"message": {"content": reply}}]}

    transport.calls = calls
    return transport


def _anthropic_transport(reply="hi from claude"):
    calls = []

    def transport(url, headers, body):
        calls.append({"url": url, "headers": headers, "body": body})
        return {"content": [{"type": "text", "text": reply}]}

    transport.calls = calls
    return transport


# --- request rendering --------------------------------------------------------

def test_rendered_prompt_includes_context():
    req = ProviderRequest(prompt="What happened?", context=["chunk A", "chunk B"])
    rendered = req.rendered_prompt()
    assert "chunk A" in rendered and "chunk B" in rendered
    assert "[Question]" in rendered and "What happened?" in rendered


def test_rendered_prompt_without_context_is_bare():
    assert ProviderRequest(prompt="hello").rendered_prompt() == "hello"


# --- adapter request/response mapping ----------------------------------------

def test_openai_adapter_maps_request_and_response():
    transport = _openai_transport("answer-42")
    provider = OpenAIProvider(model="gpt-4o-mini", api_key="sk-test", transport=transport)
    resp = provider.generate(ProviderRequest(prompt="q", context=["ctx"]))
    assert resp.ok and resp.text == "answer-42"
    assert resp.provider == "openai" and resp.model == "gpt-4o-mini"
    assert not resp.mock
    call = transport.calls[0]
    assert call["url"].endswith("/chat/completions")
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["body"]["model"] == "gpt-4o-mini"
    # system + user messages, user carries rendered context
    roles = [m["role"] for m in call["body"]["messages"]]
    assert roles == ["system", "user"]
    assert "ctx" in call["body"]["messages"][1]["content"]


def test_hermes_uses_openai_schema_and_openrouter_default():
    transport = _openai_transport("hermes-reply")
    provider = HermesProvider(model="nousresearch/hermes-3", api_key="or-test", transport=transport)
    resp = provider.generate(ProviderRequest(prompt="q"))
    assert resp.text == "hermes-reply" and resp.provider == "hermes"
    assert "openrouter.ai" in transport.calls[0]["url"]


def test_anthropic_adapter_maps_request_and_response():
    transport = _anthropic_transport("claude-reply")
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="ak-test", transport=transport)
    resp = provider.generate(ProviderRequest(prompt="q", system="be terse"))
    assert resp.ok and resp.text == "claude-reply" and resp.provider == "anthropic"
    call = transport.calls[0]
    assert call["url"].endswith("/messages")
    assert call["headers"]["x-api-key"] == "ak-test"
    assert call["headers"]["anthropic-version"]
    assert call["body"]["system"] == "be terse"
    assert call["body"]["messages"][0]["role"] == "user"


# --- mock mode ----------------------------------------------------------------

def test_missing_key_forces_mock_and_no_transport_call():
    transport = _openai_transport()
    provider = OpenAIProvider(model="gpt-4o-mini", api_key=None, transport=transport)
    assert provider.mock is True
    resp = provider.generate(ProviderRequest(prompt="hello world"))
    assert resp.mock and resp.ok
    assert "MOCK" in resp.text
    assert transport.calls == []  # never hit the network


def test_explicit_mock_flag_overrides_key():
    provider = AnthropicProvider(model="claude-sonnet-5", api_key="ak-test", mock=True)
    resp = provider.generate(ProviderRequest(prompt="x"))
    assert resp.mock and "MOCK" in resp.text


# --- error handling -----------------------------------------------------------

def test_transport_exception_becomes_error_response():
    def boom(url, headers, body):
        raise TimeoutError("slow")

    provider = OpenAIProvider(model="gpt-4o-mini", api_key="sk-test", transport=boom)
    resp = provider.generate(ProviderRequest(prompt="q"))
    assert not resp.ok and resp.error and "network error" in resp.error
    assert resp.text == ""


def test_malformed_response_becomes_error():
    def bad(url, headers, body):
        return {"unexpected": True}

    provider = OpenAIProvider(model="gpt-4o-mini", api_key="sk-test", transport=bad)
    resp = provider.generate(ProviderRequest(prompt="q"))
    assert not resp.ok and "bad response" in resp.error


# --- fan-out ------------------------------------------------------------------

def _mixed_client():
    ok = OpenAIProvider(model="gpt-4o-mini", api_key="sk", transport=_openai_transport("A"))

    def boom(url, headers, body):
        raise ConnectionError("down")

    bad = HermesProvider(model="hermes", api_key="or", transport=boom)
    claude = AnthropicProvider(model="claude-sonnet-5", api_key="ak", transport=_anthropic_transport("C"))
    return MultiProviderClient({"openai": ok, "hermes": bad, "anthropic": claude})


def test_fan_out_sync_isolates_failures():
    results = _mixed_client().fan_out_sync(ProviderRequest(prompt="q"))
    assert set(results) == {"openai", "hermes", "anthropic"}
    assert results["openai"].text == "A" and results["openai"].ok
    assert results["anthropic"].text == "C" and results["anthropic"].ok
    assert not results["hermes"].ok  # one bad provider does not break the batch


def test_fan_out_async_returns_all_providers():
    results = asyncio.run(_mixed_client().fan_out(ProviderRequest(prompt="q")))
    assert set(results) == {"openai", "hermes", "anthropic"}
    assert results["openai"].ok and not results["hermes"].ok


def test_fan_out_subset_selection():
    results = _mixed_client().fan_out_sync(ProviderRequest(prompt="q"), providers=["openai"])
    assert list(results) == ["openai"]


def test_unknown_provider_raises():
    with pytest.raises(KeyError):
        _mixed_client().generate("gemini", ProviderRequest(prompt="q"))


# --- feature-flag gating / build ---------------------------------------------

def test_build_default_client_off_by_default_is_all_mock(monkeypatch):
    monkeypatch.delenv("MCP_CONNECTOR_ENABLED", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ak-real")
    monkeypatch.setenv("HERMES_API_KEY", "or-real")
    client = build_default_client()
    assert set(client.provider_names) == {"openai", "hermes", "anthropic"}
    # Flag off => forced mock even though keys are present (no egress).
    for name in client.provider_names:
        assert client.get(name).mock is True


def test_build_default_client_enabled_uses_real_mode_when_keyed(monkeypatch):
    monkeypatch.setenv("MCP_CONNECTOR_ENABLED", "1")
    monkeypatch.delenv("MCP_MOCK", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("HERMES_API_KEY", raising=False)
    client = build_default_client()
    assert client.get("openai").mock is False  # keyed + enabled => live
    assert client.get("anthropic").mock is True  # no key => still mock


def test_mcp_mock_forces_mock_even_when_enabled(monkeypatch):
    monkeypatch.setenv("MCP_CONNECTOR_ENABLED", "1")
    monkeypatch.setenv("MCP_MOCK", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real")
    assert build_default_client(["openai"]).get("openai").mock is True


def test_build_provider_reads_model_env(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4o")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    assert build_provider("openai").model == "gpt-4o"


def test_provider_response_failure_helper():
    resp = ProviderResponse.failure("openai", "m", "nope")
    assert not resp.ok and resp.error == "nope" and resp.text == ""
