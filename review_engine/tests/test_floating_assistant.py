"""Tests for the floating local-LLM personal assistant (RAYAAAA-259).

Covers the wiring that turns RAYAAAA-247's owner-scoped cross-Task retrieval + the
RAYAAAA-232 grounded answering contract into the owner's always-here widget, backed
by the LOCAL model (RAYAAAA-258) — NOT the cancelled external MCP path. The
guarantees under test:

* **Brain selection** — the connector is the local ``OllamaConnector`` only when
  ``LOCAL_ASSISTANT_ENABLED`` is on; otherwise ``None`` (degraded path).
* **Access gate (AC5)** — fails closed: ``CROSS_TASK_ASSISTANT_ENABLED`` OFF ⇒
  ``CrossTaskAccessError`` and no assistant is built.
* **Grounded answer + citations (AC2)** — the local model's text is returned with
  per-Task/Client provenance and the standard disclaimer.
* **Graceful degradation (AC4)** — model off / unavailable / erroring ⇒ verbatim
  grounded excerpts + a clear notice, never a hang or a crash.
* **HARD cross-client isolation (AC3)** — a client-scoped ask never opens, let
  alone cites, another Client's index (inherited from RAYAAAA-247, re-proven end
  to end through this surface).
* **No evidence ⇒ no generation** — nothing retrieved ⇒ the model is never called.

Streamlit-free (drives the logic module directly with fakes) so it runs anywhere;
run inside the review-engine container per the RAYAAAA-245 gotcha for the full
suite. SYNTHETIC / owner-internal data only (Phase 4 gate: RAYAAAA-196/198).
"""
from __future__ import annotations

import pytest

from review_engine.app.cross_task import CrossTaskAccessError, OwnerAssistant
from review_engine.app.local_assistant import (
    ASSISTANT_DISCLAIMER,
    FloatingAssistant,
    build_local_connector,
)
from review_engine.app.rag_chat import MODEL_UNAVAILABLE_NOTICE, NO_EVIDENCE_MESSAGE
from review_engine.app.retrieval import HUMAN_REVIEW_NOTE


# --- helpers (mirror test_cross_task's fakes) -------------------------------


def _row(source_ref, text, distance=0.1):
    return {
        "source_ref": source_ref,
        "text": text,
        "citation": f"doc.txt ({source_ref})",
        "distance": distance,
    }


class _FakeRegistry:
    """Registry of per-id fake indexes that record which id gets queried."""

    def __init__(self):
        self.store: dict[str, list[dict]] = {}
        self.queried: list[str] = []

    def add(self, key: str, rows: list[dict]):
        self.store.setdefault(key, []).extend(rows)

    def factory(self):
        registry = self

        class _Idx:
            def __init__(self, key: str):
                self.key = key

            def search(self, query: str, limit: int) -> list[dict]:
                registry.queried.append(self.key)
                rows = registry.store.get(self.key, [])
                terms = set(query.lower().split())
                scored = sorted(
                    rows, key=lambda r: -len(terms & set(r["text"].lower().split()))
                )
                out = []
                for position, row in enumerate(scored[:limit]):
                    clone = dict(row)
                    clone["distance"] = 0.1 * position
                    out.append(clone)
                return out

        return _Idx


class _FakeDB:
    def __init__(self, matters: list[dict]):
        self._matters = matters

    def list_matters(self) -> list[dict]:
        return list(self._matters)


def _matter(mid, name, client_id, client_name=""):
    return {"id": mid, "name": name, "client_id": client_id, "client_name": client_name}


class _FakeConnector:
    """Stand-in for OllamaConnector with controllable availability / behaviour."""

    def __init__(self, *, available=True, answer="drafted answer [SRC-1]", raises=False):
        self._available = available
        self._answer = answer
        self._raises = raises
        self.calls = 0

    def available(self) -> bool:
        return self._available

    def answer_from_context(self, question, contexts) -> str:
        self.calls += 1
        if self._raises:
            raise RuntimeError("model timed out")
        return self._answer


def _owner_assistant(db, *, client_id=None, connector=None, registry=None):
    """Build an OwnerAssistant over injected fake indexes (no chromadb)."""
    from review_engine.app.cross_task import make_owner_scoped_retriever

    registry = registry or _FakeRegistry()
    retriever = make_owner_scoped_retriever(
        db,
        client_id=client_id,
        include_policies=False,
        task_index_factory=registry.factory(),
    )
    return OwnerAssistant(retriever, connector=connector)


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Most tests need the feature flag on; auth is proven separately below."""
    monkeypatch.setenv("CROSS_TASK_ASSISTANT_ENABLED", "1")
    monkeypatch.delenv("CROSS_TASK_ASSISTANT_TOKEN", raising=False)


# --- brain selection --------------------------------------------------------


def test_build_local_connector_off_by_default(monkeypatch):
    monkeypatch.delenv("LOCAL_ASSISTANT_ENABLED", raising=False)
    assert build_local_connector() is None


def test_build_local_connector_on_returns_ollama(monkeypatch):
    monkeypatch.setenv("LOCAL_ASSISTANT_ENABLED", "1")
    from review_engine.llm_connectors.ollama import OllamaConnector

    connector = build_local_connector()
    assert isinstance(connector, OllamaConnector)


# --- access gate (AC5, fail closed) -----------------------------------------


def test_create_fails_closed_when_flag_off(monkeypatch):
    monkeypatch.delenv("CROSS_TASK_ASSISTANT_ENABLED", raising=False)
    with pytest.raises(CrossTaskAccessError):
        FloatingAssistant.create(_FakeDB([]))


def test_create_requires_matching_token(monkeypatch):
    monkeypatch.setenv("CROSS_TASK_ASSISTANT_TOKEN", "s3cret")
    with pytest.raises(CrossTaskAccessError):
        FloatingAssistant.create(_FakeDB([]), token="wrong")
    # matching token is accepted (no exception)
    FloatingAssistant.create(_FakeDB([]), token="s3cret", connector=None)


# --- grounded answer + citations (AC2) --------------------------------------


def test_answer_uses_local_model_and_cites_sources():
    registry = _FakeRegistry()
    registry.add("MAT-1", [_row("SRC-1", "termination notice is thirty days")])
    db = _FakeDB([_matter("MAT-1", "Alpha review", "CLI-A", "Acme")])
    connector = _FakeConnector(answer="Notice is thirty days [SRC-1]")
    assistant = FloatingAssistant(_owner_assistant(db, connector=connector, registry=registry))

    reply = assistant.ask("termination notice")

    assert reply.model_used is True
    assert reply.grounded is True
    assert connector.calls == 1
    assert reply.answer == "Notice is thirty days [SRC-1]"
    # provenance maps the cited SRC back to its Task + Client
    assert [s.source_ref for s in reply.sources] == ["SRC-1"]
    assert reply.sources[0].matter_name == "Alpha review"
    assert reply.sources[0].client_id == "CLI-A"
    # the standard disclaimer travels with every reply
    assert reply.disclaimer == HUMAN_REVIEW_NOTE == ASSISTANT_DISCLAIMER


# --- graceful degradation (AC4) ---------------------------------------------


def test_degrades_to_excerpts_when_model_disabled():
    registry = _FakeRegistry()
    registry.add("MAT-1", [_row("SRC-1", "severance is one month")])
    db = _FakeDB([_matter("MAT-1", "Alpha", "CLI-A")])
    # connector=None models "LOCAL_ASSISTANT_ENABLED off"
    assistant = FloatingAssistant(_owner_assistant(db, connector=None, registry=registry))

    reply = assistant.ask("severance")

    assert reply.model_used is False
    assert reply.grounded is True  # excerpts are still grounded evidence
    assert reply.notice == MODEL_UNAVAILABLE_NOTICE
    assert "severance is one month" in reply.answer
    assert "SRC-1" in reply.answer
    assert reply.sources  # citations still shown


def test_degrades_when_model_unavailable():
    registry = _FakeRegistry()
    registry.add("MAT-1", [_row("SRC-1", "liability cap is fifty thousand")])
    db = _FakeDB([_matter("MAT-1", "Alpha", "CLI-A")])
    connector = _FakeConnector(available=False)
    assistant = FloatingAssistant(_owner_assistant(db, connector=connector, registry=registry))

    reply = assistant.ask("liability cap")

    assert reply.model_used is False
    assert connector.calls == 0  # never called an unavailable model
    assert reply.notice == MODEL_UNAVAILABLE_NOTICE
    assert "liability cap is fifty thousand" in reply.answer


def test_degrades_when_model_raises_does_not_hang_or_crash():
    registry = _FakeRegistry()
    registry.add("MAT-1", [_row("SRC-1", "notice period is sixty days")])
    db = _FakeDB([_matter("MAT-1", "Alpha", "CLI-A")])
    connector = _FakeConnector(available=True, raises=True)
    assistant = FloatingAssistant(_owner_assistant(db, connector=connector, registry=registry))

    reply = assistant.ask("notice period")

    # a model error falls back to grounded excerpts rather than surfacing an error
    assert reply.model_used is False
    assert reply.notice == MODEL_UNAVAILABLE_NOTICE
    assert "notice period is sixty days" in reply.answer


# --- no evidence => no generation -------------------------------------------


def test_no_evidence_never_calls_model():
    db = _FakeDB([_matter("MAT-1", "Empty", "CLI-A")])
    connector = _FakeConnector()
    assistant = FloatingAssistant(_owner_assistant(db, connector=connector))

    reply = assistant.ask("anything at all")

    assert reply.grounded is False
    assert reply.model_used is False
    assert connector.calls == 0
    assert reply.answer == NO_EVIDENCE_MESSAGE
    assert reply.sources == []


# --- HARD cross-client isolation (AC3) --------------------------------------


def test_client_scope_never_cites_another_clients_document():
    registry = _FakeRegistry()
    registry.add("MAT-X", [_row("SRC-XTASK", "vacation policy details")])
    registry.add("MAT-Y", [_row("SRC-YTASK", "vacation policy secret details")])
    db = _FakeDB(
        [
            _matter("MAT-X", "X matter", "CLI-X"),
            _matter("MAT-Y", "Y matter", "CLI-Y"),
        ]
    )
    connector = _FakeConnector(answer="Client X vacation [SRC-XTASK]")
    # scope hard-bound to Client X only
    assistant = FloatingAssistant(
        _owner_assistant(db, client_id="CLI-X", connector=connector, registry=registry)
    )

    reply = assistant.ask("vacation policy")

    refs = {s.source_ref for s in reply.sources}
    assert refs == {"SRC-XTASK"}
    assert "SRC-YTASK" not in refs
    # Client Y's index was never even opened (structural isolation, not a filter)
    assert "MAT-Y" not in registry.queried
    assert registry.queried == ["MAT-X"]
    assert all(s.client_id == "CLI-X" for s in reply.sources)


def test_all_clients_scope_spans_every_task():
    registry = _FakeRegistry()
    registry.add("MAT-X", [_row("SRC-XTASK", "overtime dispute alpha")])
    registry.add("MAT-Y", [_row("SRC-YTASK", "overtime dispute beta")])
    db = _FakeDB(
        [
            _matter("MAT-X", "X matter", "CLI-X"),
            _matter("MAT-Y", "Y matter", "CLI-Y"),
        ]
    )
    connector = _FakeConnector(answer="Both mention overtime [SRC-XTASK][SRC-YTASK]")
    assistant = FloatingAssistant(
        _owner_assistant(db, client_id=None, connector=connector, registry=registry)
    )

    reply = assistant.ask("overtime dispute")

    refs = {s.source_ref for s in reply.sources}
    assert refs == {"SRC-XTASK", "SRC-YTASK"}
