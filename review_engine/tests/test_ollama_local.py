"""RAYAAAA-258: local-LLM connector wiring (flag + env-driven endpoint/model).

No network is touched. These assert only the config surface the sibling assistant
issue flips on: the LOCAL_ASSISTANT_ENABLED gate (default OFF) and that the
connector resolves its endpoint/model from the environment so the sealed VPS stack
can point it at http://ollama:11434 / hermes3:3b without editing call sites, while
the unset-env default stays the pre-258 loopback (inert) behaviour.
"""

from __future__ import annotations

import importlib

from review_engine.llm_connectors import ollama as ollama_mod
from review_engine.llm_connectors.ollama import OllamaConnector, local_assistant_enabled


def test_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("LOCAL_ASSISTANT_ENABLED", raising=False)
    assert local_assistant_enabled() is False


def test_flag_on_only_for_exact_1(monkeypatch):
    monkeypatch.setenv("LOCAL_ASSISTANT_ENABLED", "1")
    assert local_assistant_enabled() is True
    for value in ("0", "", "true", "yes", "on"):
        monkeypatch.setenv("LOCAL_ASSISTANT_ENABLED", value)
        assert local_assistant_enabled() is False


def test_defaults_are_loopback_when_env_unset(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    reloaded = importlib.reload(ollama_mod)
    try:
        conn = reloaded.OllamaConnector()
        assert conn.base_url == "http://127.0.0.1:11434"
        assert conn.model == "llama3.2"
    finally:
        importlib.reload(ollama_mod)


def test_env_points_connector_at_sealed_ollama_service(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "hermes3:3b")
    reloaded = importlib.reload(ollama_mod)
    try:
        conn = reloaded.OllamaConnector()
        assert conn.base_url == "http://ollama:11434"
        assert conn.model == "hermes3:3b"
    finally:
        importlib.reload(ollama_mod)


def test_explicit_args_override_and_strip_trailing_slash():
    conn = OllamaConnector(model="custom", base_url="http://host:9/")
    assert conn.model == "custom"
    assert conn.base_url == "http://host:9"
