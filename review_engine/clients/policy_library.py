"""Client-scoped policy-library index (RAYAAAA-245, Phase B).

Each :class:`~review_engine.clients` Client gets its OWN uploaded HR/company
policy corpus, indexed completely apart from any single Task's documents. This
module provides the retrieval half of that corpus: a thin, hard-scoped wrapper
over :class:`~review_engine.evidence.index.EvidenceIndex`.

The isolation boundary the issue requires is enforced *structurally*, not by
post-filtering:

* every client's policy index lives under its own subdirectory of
  ``POLICY_INDEXES_DIR`` keyed by the client id, and
* its Chroma collection carries the ``policy`` prefix keyed by the client id,

so it is impossible for a Task under Client X to reach Client Y's policy store —
the path and collection name are derived solely from the client id, and the
composed retriever only ever instantiates the linked client's index. A Task's
retrieval composes its own Task index with *only* its linked client's policy
index (see ``app.retrieval.make_client_scoped_retriever``).

Everything stays local (Chroma + local embeddings); SYNTHETIC / owner-internal
data only until the Phase 4 gate (RAYAAAA-196/198).
"""
from __future__ import annotations

from pathlib import Path

from review_engine.config.settings import POLICY_INDEXES_DIR
from review_engine.evidence.index import EvidenceIndex

# Collection namespace for policy libraries. Distinct from the Task index's
# "matter" prefix so the two never collide even if they ever shared a root.
POLICY_COLLECTION_PREFIX = "policy"


class PolicyLibraryIndex(EvidenceIndex):
    """A single Client's policy corpus, physically isolated per client id.

    ``client_id`` is used exactly where ``EvidenceIndex`` uses ``matter_id`` — as
    the storage key and the source-reference salt — but rooted at
    ``POLICY_INDEXES_DIR`` with the ``policy`` collection prefix so a client's
    policies never share a store with a Task's documents or another client's
    library.
    """

    def __init__(self, client_id: str, root: str | Path = POLICY_INDEXES_DIR):
        super().__init__(
            client_id, root=root, collection_prefix=POLICY_COLLECTION_PREFIX
        )
        # Expose the intent explicitly for callers/tests that reason about the
        # client rather than the (reused) matter_id attribute.
        self.client_id = client_id

    @classmethod
    def for_client(cls, client_id: str) -> "PolicyLibraryIndex":
        return cls(client_id)
