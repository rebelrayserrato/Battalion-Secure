# RAYAAAA-245 — Per-client policy library + client-scoped retrieval (Phase B)

Engineering child of RAYAAAA-241 (Phase B), building on Phase A's first-class
`Client` model (RAYAAAA-244). **Synthetic / owner-internal data only** until the
Phase 4 gate (RAYAAAA-196/198). Branch: `RAYAAAA-245-policy-library` off `main`.

## What this adds

Each `Client` gets its own uploaded HR/company **policy corpus**, indexed apart
from any single Task's documents. A Task's grounded retrieval — for both **Chat**
and the **policy-audit / before-you-sign** review — composes exactly two sources:

1. the Task's own document index, and
2. the linked Client's policy library index — **and nothing else**.

## Isolation boundary (the hard requirement)

Cross-client isolation is enforced by **scoping**, not post-filtering:

- Policy uploads live under `data/policy_uploads/<client_id>/`.
- Each client's policy index is `PolicyLibraryIndex(client_id)` — a physically
  separate Chroma store at `data/policy_indexes/<client_id>/` with a collection
  named `policy_<sha1(client_id)>` (distinct root **and** collection from the
  Task `matter_*` indexes).
- `app.retrieval.make_client_scoped_retriever(db)` resolves a Task's linked
  `client_id` and instantiates **only** that client's policy index. Another
  client's store is never even queried.

Proven by `tests/test_policy_library.py`:
- `test_composed_retriever_never_queries_other_clients` — records queried ids;
  Client Y's store is never touched for a Client X Task query.
- `test_real_chroma_cross_client_isolation` — real on-disk Chroma stores for X
  and Y; Y's uniquely-worded policy is not retrievable from an X Task.

## Reuse (no parallel systems)

- Ingestion reuses `extraction.extract_document` (incl. RAYAAAA-230 OCR/image/ZIP)
  and the `SourceChunk` model — the `client_id` is passed where `matter_id` goes,
  so it salts the source-reference and keys the chunk store.
- Indexing reuses `evidence.index.EvidenceIndex` via a thin `collection_prefix`
  parameter; `PolicyLibraryIndex` is a subclass with the `policy` prefix + policy
  root.
- Identity reuses the Phase A `Client` id (same id the GDPR erasure fan-out uses).

## Persistence

- `client_policy_documents(client_id, name, path, …)` — uploaded policy files.
- `policy_chunks(source_ref, client_id, document_name, …)` — extracted chunks.
- DB API: `add_policy_document`, `list_policy_documents`,
  `replace_policy_document_chunks`, `get_policy_chunks`, `delete_policy_document`.

## UI

- New sidebar view **"Client policy library"**: pick a client, upload/list/
  process/delete policy docs (separate from the Task **Documents** tab).
- **Chat** and **Policy audit** tabs now retrieve over the composed index and
  cite SRC from both origins (`origin` = `task` | `policy`).

## Policy-audit uses the client's own policies (SCOPE 3)

`policy_audit.checklist_from_policies(policy_chunks)` derives a client-specific
checklist from the linked client's policy documents (one grounded item per
policy doc, query = that doc's own text), appended to the generic
`DEFAULT_CHECKLIST`. Still evidence-bound: findings only cite SRC IDs that local
retrieval returns; no fabrication. Graceful when the client has no policy library
(generic checklist only) and when Ollama is unavailable (verbatim excerpts).

## Out of scope

Law/jurisdiction grounding (Phase C, RAYAAAA-243, gated on Counsel). Not touched.
