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


class MatterResponse(BaseModel):
    matter_id: str
    name: str
    created_at: str


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
    matter_id = _db.create_matter(name, body.description, body.jurisdiction)
    matter = _db.get_matter(matter_id) or {}
    return MatterResponse(
        matter_id=matter_id,
        name=matter.get("name", name),
        created_at=matter.get("created_at", ""),
    )
