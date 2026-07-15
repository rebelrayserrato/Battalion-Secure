"""Local-LLM personal-assistant wiring for the floating widget (RAYAAAA-259).

Turns the RAYAAAA-247 owner-scoped cross-Task retrieval + the RAYAAAA-232 grounded
answering contract into the owner's always-here "Ask me anything" assistant,
backed by the on-box local model stood up in RAYAAAA-258 (Ollama + ``hermes3:3b``,
attached only to the sealed internal net, no external egress) — **NOT** the
cancelled external MCP multi-model path (RAYAAAA-242/246/248).

Why this module exists on top of RAYAAAA-247's ``OwnerAssistant``:

* **Brain selection.** ``OwnerAssistant`` already wires the cross-Task retriever
  to ``RagChatService`` (answers drawn ONLY from retrieved chunks; graceful
  degradation to verbatim excerpts when the model is unavailable). All this
  module adds is *which brain* — the LOCAL ``OllamaConnector`` when the
  RAYAAAA-258 flag (``LOCAL_ASSISTANT_ENABLED``) is on, and ``None`` (⇒ grounded
  extractive fallback) otherwise. No external provider is ever contacted.
* **Isolation is inherited, not reinvented.** The retriever comes straight from
  ``make_owner_scoped_retriever`` (RAYAAAA-247), so the HARD per-client boundary
  (a different Client's index is never opened) and erasure-safety (RAYAAAA-196)
  hold exactly as proven there — this module never touches the boundary.
* **Normalized reply.** ``ask`` returns a small ``AssistantReply`` the floating
  Streamlit widget renders directly: the grounded answer, the per-chunk
  provenance (Task + SRC + Client) for citations, the model/degradation state,
  and the standard disclaimer.

Access is gated by RAYAAAA-247's ``authorize`` (``CROSS_TASK_ASSISTANT_ENABLED``
OFF by default + optional internal token). SYNTHETIC / owner-internal data only
until the Phase 4 PII gate (RAYAAAA-196/198) — unchanged by this issue.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from review_engine.app.cross_task import CrossTaskSource, OwnerAssistant
from review_engine.app.rag_chat import RagAnswer
from review_engine.app.retrieval import HUMAN_REVIEW_NOTE
from review_engine.llm_connectors.ollama import OllamaConnector, local_assistant_enabled

# AC2: the same "AI assists, human review required / not legal advice" disclaimer
# used everywhere else in the Review Engine (retrieval.py), rendered under every
# answer. Reused verbatim so the assistant never invents its own weaker wording.
ASSISTANT_DISCLAIMER = HUMAN_REVIEW_NOTE

# Sentinel so callers (and tests) can distinguish "use the default local brain"
# from an explicit ``connector=None`` (force the extractive/degraded path).
_DEFAULT = object()


def build_local_connector() -> Optional[OllamaConnector]:
    """The local model brain, or ``None`` when it must not be called.

    Returns an ``OllamaConnector`` — env-resolved to the sealed on-box
    ``hermes3:3b`` in the VPS stack (``OLLAMA_BASE_URL=http://ollama:11434``) —
    only when the RAYAAAA-258 flag ``LOCAL_ASSISTANT_ENABLED`` is on. When it is
    off, we return ``None`` so ``OwnerAssistant``/``RagChatService`` degrade to
    grounded verbatim excerpts (the AC4 "model unavailable" path) instead of
    contacting any model. No external provider is ever an option here.
    """
    if not local_assistant_enabled():
        return None
    return OllamaConnector()


@dataclass(frozen=True)
class AssistantReply:
    """Normalized reply for the floating widget: grounded answer + citations.

    * ``sources`` — RAYAAAA-247 provenance: which Task + SRC + Client backed the
      answer, used to render citations under it.
    * ``model_used`` — the local model drafted the prose; ``False`` means the
      grounded verbatim-excerpt fallback was shown (model off/down/slow/errored).
    * ``notice`` — the human-facing degradation notice when ``model_used`` is
      ``False`` (e.g. "Local drafting model unavailable …").
    """

    question: str
    answer: str
    sources: list[CrossTaskSource]
    grounded: bool
    model_used: bool
    notice: Optional[str]
    disclaimer: str = ASSISTANT_DISCLAIMER


class FloatingAssistant:
    """The owner's always-here, cross-Task assistant backed by the LOCAL model.

    A thin adapter over RAYAAAA-247's ``OwnerAssistant`` that (a) selects the local
    brain and (b) normalizes each answer into an ``AssistantReply``. Construct via
    :meth:`create`, which enforces the RAYAAAA-247 auth gate.
    """

    def __init__(self, assistant: OwnerAssistant):
        self._assistant = assistant

    @classmethod
    def create(
        cls,
        db,
        *,
        token: Optional[str] = None,
        client_id: Optional[str] = None,
        include_policies: bool = True,
        connector=_DEFAULT,
    ) -> "FloatingAssistant":
        """Authorize, pick the local brain, wire to the owner's live indexes.

        Raises ``CrossTaskAccessError`` (via ``OwnerAssistant.create`` →
        ``authorize``) unless ``CROSS_TASK_ASSISTANT_ENABLED`` is on and the
        internal token matches when one is configured — i.e. it fails closed.

        ``connector`` defaults to the local Ollama brain (or ``None`` when the
        RAYAAAA-258 flag is off); tests inject a fake to drive the model / degraded
        paths deterministically without a running model.
        """
        connector = build_local_connector() if connector is _DEFAULT else connector
        assistant = OwnerAssistant.create(
            db,
            token=token,
            client_id=client_id,
            include_policies=include_policies,
            connector=connector,
        )
        return cls(assistant)

    def ask(self, question: str, limit: int = 6) -> AssistantReply:
        """Answer a question across the owner's Tasks, grounded + cited.

        Retrieval + grounding + graceful degradation are all inherited from
        ``OwnerAssistant``/``RagChatService``: no evidence ⇒ the model is never
        called; model unavailable/slow/errored ⇒ verbatim source excerpts with a
        notice. This only unwraps the result into an ``AssistantReply``."""
        result = self._assistant.answer(question, limit=limit)
        rag: RagAnswer = result["answer"]
        return AssistantReply(
            question=question,
            answer=rag.text,
            sources=result["provenance"],
            grounded=rag.grounded,
            model_used=rag.model_used,
            notice=rag.notice,
        )
