"""Cross-Task, owner-scoped retrieval + provenance (RAYAAAA-247, Phase B2).

Today's Chat (RAYAAAA-232) is bound to ONE Task's Chroma index. This module
provides a view ACROSS the owner's Tasks for the new "sees everything" personal
assistant surface — but "sees everything" means everything the OWNER is entitled
to see, **not** a tenant-isolation bypass.

The security boundary is inherited, not reinvented. It reuses the exact
per-client isolation primitives from the RAYAAAA-241 epic:

* the first-class Client concept + ``matters.client_id`` link (RAYAAAA-244), and
* the physically-isolated per-client policy library (RAYAAAA-245).

The HARD assertion the issue requires — *an answer framed around one Client must
never pull another Client's document* — is enforced **structurally**: when a
``client_id`` scope is supplied, only that Client's Tasks and only that Client's
policy library are ever *instantiated and queried*. A different Client's index is
never even opened; there is no post-filtering to get wrong (see the tests in
``tests/test_cross_task.py``).

Erasure (RAYAAAA-196) is respected for free: retrieval reads the LIVE, on-disk
per-Task Chroma indexes and the live matters table, so an erased/anonymized
Task — whose rows and index tree ``erase_matter`` deletes — simply cannot
resurface here.

Every retrieved row carries first-class provenance (``matter_id`` /
``matter_name`` / ``client_id`` / ``origin`` alongside the existing
``source_ref``) so a cross-Task answer can always say *which Task and which SRC
chunk* backed it.

Access is gated (defense in depth): the ``CROSS_TASK_ASSISTANT_ENABLED`` feature
flag is OFF by default, and an optional ``CROSS_TASK_ASSISTANT_TOKEN`` internal
shared secret can be required on top. Synthetic / owner-internal data only until
the Phase 4 gate (RAYAAAA-196/198).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Optional

from review_engine.evidence.index import EvidenceIndex

# A cross-Task retriever has the same (question, limit) -> rows shape the
# RagChatService (RAYAAAA-232) already consumes, so the assistant surface can
# reuse the existing grounded-answer machinery unchanged.
CrossTaskRetriever = Callable[[str, int], list[dict]]


class CrossTaskAccessError(PermissionError):
    """Raised when the cross-Task assistant is used without authorization."""


# --- auth gate --------------------------------------------------------------
#
# Read from the environment at call time (like ``build_default_client`` in the
# MCP connector) so deploys/tests can toggle the gate without a re-import.


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def assistant_enabled() -> bool:
    """True only when the owner-internal cross-Task assistant is switched on."""
    return _flag("CROSS_TASK_ASSISTANT_ENABLED", False)


def authorize(token: Optional[str] = None) -> None:
    """Enforce the two access gates; raise ``CrossTaskAccessError`` otherwise.

    1. The feature flag must be ON (it is OFF by default).
    2. If ``CROSS_TASK_ASSISTANT_TOKEN`` is configured, ``token`` must match it.
    """
    if not assistant_enabled():
        raise CrossTaskAccessError(
            "cross-Task assistant is disabled (CROSS_TASK_ASSISTANT_ENABLED is OFF)"
        )
    expected = os.getenv("CROSS_TASK_ASSISTANT_TOKEN")
    if expected and token != expected:
        raise CrossTaskAccessError("invalid or missing internal auth token")


# --- provenance -------------------------------------------------------------


@dataclass(frozen=True)
class CrossTaskSource:
    """Per-answer provenance for one retrieved chunk in the cross-Task view.

    Carries WHICH Task (``matter_id``/``matter_name``) and WHICH SRC chunk
    (``source_ref``) backed an answer, plus the Client it belongs to and whether
    it came from a Task document or the Client's policy library (``origin``)."""

    source_ref: str
    citation: str
    text: str
    distance: float
    matter_id: Optional[str]
    matter_name: str
    client_id: Optional[str]
    client_name: str
    origin: str  # "task" | "policy"

    @classmethod
    def from_row(cls, row: dict) -> "CrossTaskSource":
        return cls(
            source_ref=row["source_ref"],
            citation=row.get("citation", row["source_ref"]),
            text=row.get("text", ""),
            distance=float(row.get("distance", 0.0)),
            matter_id=row.get("matter_id"),
            matter_name=row.get("matter_name", ""),
            client_id=row.get("client_id"),
            client_name=row.get("client_name", ""),
            origin=row.get("origin", "task"),
        )

    def label(self) -> str:
        """Human-readable provenance line for surfacing under an answer."""
        where = self.matter_name or self.matter_id or "policy library"
        who = self.client_name or self.client_id or "unlinked"
        kind = "policy" if self.origin == "policy" else "Task"
        return f"{self.citation} — {kind}: {where} · Client: {who}"


def provenance(rows: list[dict]) -> list[CrossTaskSource]:
    return [CrossTaskSource.from_row(row) for row in rows]


# --- owner-scoped retrieval -------------------------------------------------


def visible_matters(db, client_id: Optional[str] = None) -> list[dict]:
    """The Tasks the owner may see, optionally hard-scoped to one Client.

    When ``client_id`` is given, matters for every OTHER client are dropped
    *before* any index is opened — this is the structural cross-client boundary,
    not a post-filter over already-retrieved chunks.
    """
    matters = db.list_matters()
    if client_id is not None:
        matters = [m for m in matters if m.get("client_id") == client_id]
    return matters


def make_owner_scoped_retriever(
    db,
    *,
    client_id: Optional[str] = None,
    include_policies: bool = True,
    task_index_factory: Optional[Callable] = None,
    policy_index_factory: Optional[Callable] = None,
) -> CrossTaskRetriever:
    """Build a ``(question, limit)`` retriever spanning the owner's Tasks.

    * ``client_id`` — when set, retrieval is HARD-scoped to that Client's Tasks
      and policy library only. Other clients' indexes are never instantiated, so
      an answer framed around this Client can never surface another's document.
    * ``include_policies`` — also fold in each in-scope Client's policy library
      (RAYAAAA-245), attributed with ``origin="policy"``. Each Client's library
      is queried at most once even when the Client owns several Tasks.
    * The index factories are injectable purely so the isolation boundary can be
      proven in tests without chromadb; they default to the real on-disk stores.

    Every returned row is tagged with provenance (``matter_id`` / ``matter_name``
    / ``client_id`` / ``client_name`` / ``origin``) and the merged result is
    ordered by ascending distance and truncated to ``limit``.
    """
    make_task_index = task_index_factory or EvidenceIndex
    if policy_index_factory is not None:
        make_policy_index = policy_index_factory
    else:
        from review_engine.clients.policy_library import PolicyLibraryIndex

        make_policy_index = PolicyLibraryIndex

    def _tag(row: dict, *, origin: str, matter: Optional[dict], cid, cname: str) -> dict:
        row.setdefault("origin", origin)
        row["matter_id"] = matter.get("id") if matter else None
        row["matter_name"] = matter.get("name", "") if matter else ""
        row["client_id"] = cid
        row["client_name"] = cname
        return row

    def retriever(question: str, limit: int) -> list[dict]:
        rows: list[dict] = []
        queried_policy_clients: set = set()
        for matter in visible_matters(db, client_id):
            matter_id = matter["id"]
            cid = matter.get("client_id")
            cname = matter.get("client_name", "")
            for row in make_task_index(matter_id).search(question, limit) or []:
                rows.append(_tag(row, origin="task", matter=matter, cid=cid, cname=cname))
            if include_policies and cid and cid not in queried_policy_clients:
                queried_policy_clients.add(cid)
                for row in make_policy_index(cid).search(question, limit) or []:
                    rows.append(
                        _tag(row, origin="policy", matter=None, cid=cid, cname=cname)
                    )
        rows.sort(key=lambda r: r.get("distance", 0.0))
        return rows[:limit]

    return retriever


# --- assistant service ------------------------------------------------------


class _CapturingRetriever:
    """Wraps a retriever so the answering service and the caller share the exact
    same retrieved rows — provenance is read from what actually backed the
    answer, never a second (possibly divergent) query."""

    def __init__(self, inner: CrossTaskRetriever):
        self._inner = inner
        self.last_rows: list[dict] = []

    def __call__(self, question: str, limit: int) -> list[dict]:
        self.last_rows = self._inner(question, limit) or []
        return self.last_rows


class OwnerAssistant:
    """Owner-scoped, cross-Task grounded assistant.

    Reuses the RAYAAAA-232 ``RagChatService`` grounding/generation contract
    (answers drawn ONLY from retrieved chunks, graceful degradation when Ollama
    is unavailable) but over the cross-Task retriever, and surfaces per-answer
    provenance across Tasks/Clients.
    """

    def __init__(self, retriever: CrossTaskRetriever, connector=None):
        from review_engine.app.rag_chat import RagChatService

        self._captured = _CapturingRetriever(retriever)
        self._rag = RagChatService(retriever=self._captured, connector=connector)

    @classmethod
    def create(
        cls,
        db,
        *,
        token: Optional[str] = None,
        client_id: Optional[str] = None,
        include_policies: bool = True,
        connector=None,
    ) -> "OwnerAssistant":
        """Authorize, then wire the assistant to the owner's live indexes.

        Raises ``CrossTaskAccessError`` unless the feature flag is on (and the
        internal token matches, when one is configured)."""
        authorize(token)
        retriever = make_owner_scoped_retriever(
            db, client_id=client_id, include_policies=include_policies
        )
        return cls(retriever, connector=connector)

    def answer(self, question: str, limit: int = 6) -> dict:
        """Return ``{"answer": RagAnswer, "provenance": [CrossTaskSource, ...]}``.

        The provenance list is derived from the SAME rows that grounded the
        answer, so every cited SRC maps back to its Task/Client."""
        rag_answer = self._rag.answer(question, limit)
        return {
            "answer": rag_answer,
            "provenance": provenance(self._captured.last_rows),
        }

    def retrieve(self, question: str, limit: int = 6) -> list[CrossTaskSource]:
        """Retrieval-only path (no generation): the provenance-tagged sources."""
        return provenance(self._captured(question, limit))
