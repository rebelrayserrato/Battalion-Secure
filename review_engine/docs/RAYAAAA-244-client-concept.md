# RAYAAAA-244 — First-class Client concept + Task→Client link + client-level jurisdiction

Phase A of RAYAAAA-241. **SYNTHETIC / owner-internal only** — no real client PII is
wired here (standing Phase 4 gate, RAYAAAA-196 / 198).

## What changed

1. **Client model** (`clients` table): `id`, `display_name`, `state`
   (jurisdiction), `created_at`, `updated_at`. `state` is a **validated** value
   from a controlled US-state list (`review_engine/clients/jurisdictions.py`) —
   never free text.
2. **Task ↔ Client link**: `matters.client_id` FK → `clients.id`. Every Task
   resolves to **exactly one** Client. Legacy/producer creates with no client get
   a 1:1 synthetic client automatically, so the invariant always holds.
3. **Jurisdiction promoted to the Client**: `matters.jurisdiction` is now
   **derived from the client** at read time (`get_matter` / `list_matters` join
   `clients` and return `COALESCE(c.state, m.jurisdiction)`), so a Task's
   jurisdiction can **never diverge** from its client's. `update_client_state`
   also refreshes the stored column defensively.
4. **Minimal Streamlit UI** (`app/main.py`, sidebar, consistent with
   RAYAAAA-228): a "Create a client" form (name + US-state picker) and a Client
   picker on the "Create a task" form. The Task header shows its client and
   derived jurisdiction.

## Identity mapping — why there is no second identity store

The GDPR erasure fan-out (RAYAAAA-196 / 207 / 223) already establishes the
authoritative notion of *client identity*: the main app resolves a client to the
set of Battalion `matter_id`s it owns and erases each one (Battalion side:
`DELETE /admin/review-engine/api/matters/{matter_id}`). The client→matters
grouping is the fan-out's identity mapping.

This change **reuses that same identity** rather than inventing a parallel one:

- The Battalion **`clients.id` is the same client identifier the fan-out uses to
  group a client's matters.** `create_matter(..., client_id=<that id>)` (exposed
  on the producer API `POST .../matters` via the optional `client_id` field)
  **materializes/links** a `clients` row keyed by that identity on first use, and
  reuses it for every subsequent matter with the same id. So the local
  `matters.client_id` grouping is exactly the fan-out's client→matter grouping,
  now persisted Battalion-side.
- No new identity keyspace, no client-PII lookup table, no email/phone mapping is
  introduced. `display_name` + `state` are synthetic metadata only.
- Erasure is unchanged and still keyed by `matter_id`; the `clients` table holds
  no document PII and is intentionally **not** swept by `erase_matter` (erasing
  one matter must not disturb a client that owns others). Real-client-PII handling
  on the Client record is a **Phase 4** concern (RBAC/retention/DPIA), explicitly
  out of scope here.

## Backfill migration (zero data loss)

`ReviewDatabase.initialize()` → `_ensure_client_link()` runs on every boot and is
idempotent:

- Adds `matters.client_id` if the column is absent (`ALTER TABLE`).
- For each matter with no client, creates a **1:1** client named after the matter
  and carrying the matter's old free-text jurisdiction **normalized** to a state
  code (or `UNSPECIFIED_STATE` = `"US"` when unrecognizable). No matters,
  documents, chunks, entities, findings, or audit rows are dropped or rewritten
  except to stamp `client_id` + the derived state.
- When the original jurisdiction free-text can't be normalized, it is **preserved
  verbatim** in a `client_backfill` audit-log entry, so nothing is lost.

`UNSPECIFIED_STATE` (`"US"`, "unspecified / federal") is an explicit validated
member of the controlled list so a client whose jurisdiction is genuinely unknown
carries a canonical value instead of NULL/free text.

## Tests

`review_engine/tests/test_clients.py` — model, validation, link (exactly one
client per Task), jurisdiction derivation + no-divergence, identity-reuse hook,
and the backfill migration (zero data loss + idempotency). Synthetic-only.

## Branch / rebase note

Branched off `RAYAAAA-240-integration` (the consolidated integration line), **not
literal `main`** — `main` is stale and lacks the erasure-fanout identity mapping
(RAYAAAA-196/207/223), the matter/Task model (RAYAAAA-228), and the sidecar API
this work reuses. Once RAYAAAA-240 lands to `main`, this rebases cleanly onto it.
**Not merged** — handed back to CTO for review.
