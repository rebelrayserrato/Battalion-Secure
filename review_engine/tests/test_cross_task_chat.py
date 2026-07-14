"""Tests for the multi-model cross-Task assistant (RAYAAAA-248, Phase B3).

Covers the two headline behaviours the issue calls out — model ROUTING (one
model) and FAN-OUT compare (all models simultaneously) — plus the guardrails
inherited from B1/B2/232:

* retrieval happens ONCE and its provenance is shared across every model's answer;
* no evidence retrieved => no model is called (like 232);
* a provider without a key degrades to an inert MOCK answer rather than erroring;
* one failing provider never breaks a fan-out (the other columns still render);
* the B2 auth gate (feature flag OFF by default) is enforced by ``create``; and
* the fan-out RENDERING emits one column per model + the shared provenance.

Synthetic / owner-internal only (Phase 4 gate: RAYAAAA-196/198).
"""
from __future__ import annotations

import pytest

from review_engine.app import cross_task_chat as ctc
from review_engine.app.cross_task import CrossTaskAccessError
from review_engine.app.cross_task_chat import (
    ModelAnswer,
    MultiModelAssistant,
    provider_for_label,
    model_label,
)
from review_engine.llm_connectors.providers import (
    MultiProviderClient,
    ProviderRequest,
    ProviderResponse,
)


# --- fakes ------------------------------------------------------------------


def _row(source_ref, text, *, matter_id, matter_name, client_id="c1", client_name="Acme", origin="task", distance=0.1):
    return {
        "source_ref": source_ref,
        "citation": f"{source_ref}.txt ({source_ref})",
        "text": text,
        "distance": distance,
        "matter_id": matter_id,
        "matter_name": matter_name,
        "client_id": client_id,
        "client_name": client_name,
        "origin": origin,
    }


def _fixed_retriever(rows):
    """A retriever that records its calls and returns fixed rows."""
    calls = []

    def retriever(question, limit):
        calls.append((question, limit))
        return list(rows)

    retriever.calls = calls
    return retriever


class _FakeProvider:
    """Records the request it receives and returns a scripted response."""

    def __init__(self, name, *, model="fake-1", mock=False, ok=True, text=None, error=None, boom=False):
        self.name = name
        self.model = model
        self._mock = mock
        self._ok = ok
        self._text = text if text is not None else f"{name} answer"
        self._error = error
        self._boom = boom
        self.seen: list[ProviderRequest] = []
        self.timeout = 60.0

    def generate(self, request: ProviderRequest) -> ProviderResponse:
        self.seen.append(request)
        if self._boom:  # exercise MultiProviderClient's per-provider isolation
            raise RuntimeError("provider exploded")
        if not self._ok:
            return ProviderResponse.failure(self.name, self.model, self._error or "boom", 1.0)
        return ProviderResponse(
            provider=self.name, model=self.model, text=self._text, mock=self._mock, latency_ms=2.0
        )


def _client(**providers) -> MultiProviderClient:
    return MultiProviderClient(providers)


def _three():
    return _client(
        openai=_FakeProvider("openai", model="gpt", text="codex says X [SRC-1]"),
        hermes=_FakeProvider("hermes", model="herm", text="hermes says X [SRC-1]"),
        anthropic=_FakeProvider("anthropic", model="claude", text="claude says X [SRC-1]"),
    )


ROWS = [
    _row("SRC-1", "overtime dispute noted", matter_id="m1", matter_name="Task One"),
    _row("SRC-2", "hours logged", matter_id="m2", matter_name="Task Two", distance=0.2),
]


# --- label mapping ----------------------------------------------------------


def test_label_mapping_matches_owner_names():
    assert model_label("openai") == "Codex"
    assert model_label("hermes") == "Hermes"
    assert model_label("anthropic") == "Claude"
    assert provider_for_label("Claude") == "anthropic"
    # Unknown falls through unchanged (defensive).
    assert model_label("mystery") == "mystery"
    assert provider_for_label("mystery") == "mystery"


# --- routing (single model) -------------------------------------------------


def test_route_to_single_model_calls_only_that_provider():
    client = _three()
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), client)

    result = assistant.ask("overtime?", "Claude")  # by owner-facing label

    assert not result.compared
    assert [a.provider for a in result.answers] == ["anthropic"]
    assert result.answers[0].label == "Claude"
    assert result.answers[0].text == "claude says X [SRC-1]"
    # Only the routed provider was invoked.
    assert len(client.get("anthropic").seen) == 1
    assert client.get("openai").seen == []
    assert client.get("hermes").seen == []


def test_route_accepts_registry_name_too():
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), _three())
    result = assistant.ask("q", ["openai"])
    assert [a.label for a in result.answers] == ["Codex"]


def test_context_is_grounded_and_provenance_shared():
    client = _three()
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), client)
    result = assistant.ask("q", None)

    # Every model saw the SAME grounded context, citation-prefixed.
    for name in ("openai", "hermes", "anthropic"):
        seen = client.get(name).seen[0]
        assert seen.context[0].startswith("[SRC-1.txt (SRC-1)]")
        assert "overtime dispute noted" in seen.context[0]
        assert seen.system == ctc.ASSISTANT_SYSTEM
    # Provenance is derived once and shared; maps SRC -> Task.
    refs = [s.source_ref for s in result.provenance]
    assert refs == ["SRC-1", "SRC-2"]
    assert result.provenance[0].matter_name == "Task One"


# --- fan-out (compare all) --------------------------------------------------


def test_fan_out_compares_all_models_in_order():
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), _three())
    result = assistant.ask("q")  # providers=None => all

    assert result.compared
    assert [a.label for a in result.answers] == ["Codex", "Hermes", "Claude"]
    assert all(a.ok for a in result.answers)
    # Only retrieved once even though three models answered.
    assert len(assistant._retriever.calls) == 1


def test_fan_out_subset_compares_only_chosen_models():
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), _three())
    result = assistant.ask("q", ["Codex", "Claude"])
    assert [a.label for a in result.answers] == ["Codex", "Claude"]
    assert result.compared


def test_fan_out_isolates_a_failing_provider():
    client = _client(
        openai=_FakeProvider("openai", text="ok"),
        hermes=_FakeProvider("hermes", boom=True),  # raises inside generate
        anthropic=_FakeProvider("anthropic", ok=False, error="http 500"),
    )
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), client)
    result = assistant.ask("q")

    by_label = {a.label: a for a in result.answers}
    assert by_label["Codex"].ok
    assert not by_label["Hermes"].ok  # crash -> failure envelope, not exception
    assert not by_label["Claude"].ok
    assert by_label["Claude"].error == "http 500"
    # All three columns are present despite two failing.
    assert len(result.answers) == 3


# --- graceful degradation ---------------------------------------------------


def test_missing_key_degrades_to_mock_not_error():
    client = _client(
        openai=_FakeProvider("openai", mock=True, text="[MOCK]"),
        hermes=_FakeProvider("hermes", text="real"),
        anthropic=_FakeProvider("anthropic", text="real"),
    )
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), client)
    result = assistant.ask("q")
    codex = next(a for a in result.answers if a.label == "Codex")
    assert codex.ok and codex.mock  # graceful: mock, not a failure


def test_no_evidence_never_calls_a_model():
    client = _three()
    assistant = MultiModelAssistant(_fixed_retriever([]), client)
    result = assistant.ask("q")
    assert not result.grounded
    assert result.answers == []
    assert result.notice == ctc.NO_EVIDENCE_MESSAGE
    for name in client.provider_names:
        assert client.get(name).seen == []


def test_empty_question_is_noticed_not_asked():
    client = _three()
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), client)
    result = assistant.ask("   ")
    assert result.answers == []
    assert result.notice == ctc.EMPTY_QUESTION_MESSAGE
    assert assistant._retriever.calls == []  # never even retrieved


def test_unknown_provider_raises():
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), _three())
    with pytest.raises(KeyError):
        assistant.ask("q", ["nope"])


def test_empty_explicit_selection_raises():
    assistant = MultiModelAssistant(_fixed_retriever(ROWS), _three())
    with pytest.raises(ValueError):
        assistant.ask("q", [])


# --- auth gate (B2) ---------------------------------------------------------


class _FakeDb:
    def list_matters(self):
        return []


def test_create_is_blocked_when_flag_off(monkeypatch):
    monkeypatch.delenv("CROSS_TASK_ASSISTANT_ENABLED", raising=False)
    with pytest.raises(CrossTaskAccessError):
        MultiModelAssistant.create(_FakeDb(), client=_three())


def test_create_succeeds_when_flag_on(monkeypatch):
    monkeypatch.setenv("CROSS_TASK_ASSISTANT_ENABLED", "1")
    monkeypatch.delenv("CROSS_TASK_ASSISTANT_TOKEN", raising=False)
    assistant = MultiModelAssistant.create(_FakeDb(), client=_three())
    assert assistant.model_labels == ["Codex", "Hermes", "Claude"]


# --- fan-out RENDERING ------------------------------------------------------


class _FakeColumn:
    def __init__(self, rec):
        self._rec = rec

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeStreamlit:
    """Minimal recorder standing in for the streamlit module in the view."""

    def __init__(self):
        self.markdown_calls: list[str] = []
        self.write_calls: list[str] = []
        self.caption_calls: list[str] = []
        self.error_calls: list[str] = []
        self.warning_calls: list[str] = []
        self.columns_arg = None

    def markdown(self, text, **kw):
        self.markdown_calls.append(text)

    def write(self, text):
        self.write_calls.append(text)

    def caption(self, text):
        self.caption_calls.append(text)

    def error(self, text):
        self.error_calls.append(text)

    def warning(self, text):
        self.warning_calls.append(text)

    def columns(self, n):
        self.columns_arg = n
        return [_FakeColumn(self) for _ in range(n)]

    def expander(self, *a, **kw):
        return _FakeColumn(self)


def _make_answers():
    return [
        ModelAnswer("openai", "Codex", "gpt", "codex text", True, False, None, 2.0),
        ModelAnswer("hermes", "Hermes", "herm", "", True, True, None, 1.0),  # mock
        ModelAnswer("anthropic", "Claude", "claude", "", False, False, "http 500", 3.0),
    ]


def test_render_fan_out_emits_a_column_per_model(monkeypatch):
    from review_engine.app import assistant_view
    from review_engine.app.cross_task_chat import AssistantResult
    from review_engine.app.cross_task import CrossTaskSource

    fake = _FakeStreamlit()
    monkeypatch.setattr(assistant_view, "st", fake)

    src = CrossTaskSource("SRC-1", "SRC-1.txt", "text", 0.1, "m1", "Task One", "c1", "Acme", "task")
    result = AssistantResult("q", True, [src], _make_answers())
    assistant_view._render_result(result)

    # One column per model in the compare.
    assert fake.columns_arg == 3
    # Each model's label header rendered.
    joined = "\n".join(fake.markdown_calls)
    assert "Codex" in joined and "Hermes" in joined and "Claude" in joined
    # The mock model is labelled as mock; the failed model surfaces its error.
    assert any("Mock response" in c for c in fake.caption_calls)
    assert any("http 500" in e for e in fake.error_calls)
    # Shared provenance line rendered.
    assert any("Task One" in m for m in fake.markdown_calls)


def test_render_single_model_uses_no_columns(monkeypatch):
    from review_engine.app import assistant_view
    from review_engine.app.cross_task_chat import AssistantResult

    fake = _FakeStreamlit()
    monkeypatch.setattr(assistant_view, "st", fake)

    answer = ModelAnswer("anthropic", "Claude", "claude", "just claude", True, False, None, 2.0)
    result = AssistantResult("q", True, [], [answer])
    assistant_view._render_result(result)

    assert fake.columns_arg is None  # single route => no side-by-side columns
    assert "just claude" in fake.write_calls
