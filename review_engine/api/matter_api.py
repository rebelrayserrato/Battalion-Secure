"""Internal matter-creation API for the Battalion-Secure review engine.

RAYAAAA-210 (Portal->Battalion "New Matter" producer).

This is a tiny FastAPI app that runs as a sidecar next to the Streamlit UI inside
the same container (see run_app.py) and shares the SAME sqlite store, so a matter
created here is immediately visible in the Streamlit workspace and is reachable by
the GDPR erasure/retention tooling (RAYAAAA-196).

Security posture (unchanged from the review engine's deploy scaffold):
  * It listens only on the internal container port 8600, exposed on the
    internal-only docker network. There is NO host port.
  * The ONLY ingress is nginx's ``/admin/review-engine/api/`` location, which is
    gated by the same ``auth_request`` owner-session probe as the Streamlit UI
    (RAYAAAA-205 authz route). nginx never proxies here without a valid owner
    session, so there is no unauthenticated path.
  * Defense in depth: if ``MATTER_API_TOKEN`` is set, a matching
    ``X-Internal-Token`` header is also required.

It creates only the matter *shell* (name/description/jurisdiction metadata). No
document bytes and no client PII beyond whatever the caller puts in the name pass
through here; on the live web line the producer flag is OFF, so only synthetic
fixtures ever reach it until DPIA sign-off (RAYAAAA-198).
"""

from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from review_engine.audits.database import ReviewDatabase
from review_engine.privacy.erasure import erase_matter

app = FastAPI(title="Battalion matter API", docs_url=None, redoc_url=None, openapi_url=None)

# One shared database handle. ReviewDatabase opens a fresh sqlite connection per
# call (with a busy timeout), so it is safe to reuse across requests and to share
# the file with the Streamlit process.
_db = ReviewDatabase()

_BASE = "/admin/review-engine/api"


def _require_token(x_internal_token: str | None = Header(default=None)) -> None:
    """Optional shared-secret gate. No-op unless MATTER_API_TOKEN is configured."""
    expected = os.environ.get("MATTER_API_TOKEN")
    if expected and x_internal_token != expected:
        raise HTTPException(status_code=403, detail="Invalid internal token.")


class CreateMatterRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    jurisdiction: str = Field(default="", max_length=200)
    # RAYAAAA-244: the producer may pass the SAME client identity the erasure
    # fan-out (RAYAAAA-207/223) uses to group a client's matters. Battalion
    # materializes/links a Client row keyed by that id — no parallel identity
    # store. Omitted (current producer) -> a 1:1 synthetic client is created.
    client_id: str | None = Field(default=None, max_length=64)


class MatterResponse(BaseModel):
    matter_id: str
    name: str
    created_at: str


class MatterErasureResponse(BaseModel):
    """Structured residual accounting the fan-out asserts on for fail-loud
    behaviour (RAYAAAA-212 AC2). ``clean`` is True iff nothing survived across all
    four stores; the counts mirror ``ErasureReport`` / the erase_cli JSON so the
    HTTP transport is a drop-in for the CLI one."""

    matter_id: str
    clean: bool
    sqlite_rows_deleted: int
    upload_bytes_deleted: int
    index_bytes_deleted: int
    report_bytes_deleted: int
    residual_sqlite_rows: int
    residual_upload_bytes: int
    residual_index_bytes: int
    residual_report_bytes: int

    @classmethod
    def from_report(cls, report) -> "MatterErasureResponse":
        return cls(
            matter_id=report.matter_id,
            clean=report.clean,
            sqlite_rows_deleted=report.sqlite_rows_deleted,
            upload_bytes_deleted=report.upload_bytes_deleted,
            index_bytes_deleted=report.index_bytes_deleted,
            report_bytes_deleted=report.report_bytes_deleted,
            residual_sqlite_rows=report.residual_sqlite_rows,
            residual_upload_bytes=report.residual_upload_bytes,
            residual_index_bytes=report.residual_index_bytes,
            residual_report_bytes=report.residual_report_bytes,
        )


@app.get(f"{_BASE}/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(f"{_BASE}/matters", response_model=MatterResponse, status_code=201)
def create_matter(
    body: CreateMatterRequest, _: None = Depends(_require_token)
) -> MatterResponse:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Matter name is required.")
    matter_id = _db.create_matter(
        name, body.description, body.jurisdiction, client_id=body.client_id
    )
    matter = _db.get_matter(matter_id) or {}
    return MatterResponse(
        matter_id=matter_id,
        name=matter.get("name", name),
        created_at=matter.get("created_at", ""),
    )


@app.delete(f"{_BASE}/matters/{{matter_id}}", response_model=MatterErasureResponse)
def erase_matter_endpoint(
    matter_id: str, _: None = Depends(_require_token)
) -> MatterErasureResponse:
    """GDPR Art.17 erase of a single matter across all four Battalion stores
    (RAYAAAA-212). This is the internal HTTP transport the main-app erasure
    fan-out (RAYAAAA-207) calls in place of the ``erase_cli`` docker shell-out.

    Owns its store (Conway): the review engine — not the web tier — performs the
    wipe, using the same verified ``erase_matter`` primitive as the CLI and the
    retention sweep, against the SAME shared sqlite store the sidecar writes to.

    Fail-loud contract (mirrors erase_cli's non-zero exit on residual): an erase
    that leaves ANY residual returns 500 with the residual counts in ``detail`` so
    the fan-out retries / fails loud and never records an orphaned matter as gone.
    A clean erase — including the idempotent no-op for an unknown/already-erased
    matter — returns 200 with an all-zero-residual report.
    """
    try:
        report = erase_matter(matter_id, database_path=_db.path)
    except ValueError as exc:
        # Unsafe matter id (path traversal etc.) — never reached the store.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    payload = MatterErasureResponse.from_report(report)
    if not report.clean:
        raise HTTPException(status_code=500, detail=payload.model_dump())
    return payload
