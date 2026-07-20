"""Per-client encrypted-storage ACL for the Battalion-Secure review engine.

RAYAAAA-303 (P2 of the RAYAAAA-296 client-facing pivot, Option B — on-box LUKS).

A ``Client`` (RAYAAAA-244) already gets its own uploaded-policy corpus keyed by
``client_id`` (RAYAAAA-245): raw files under ``POLICY_UPLOADS_DIR/<client_id>``
and a physically separate Chroma index under ``POLICY_INDEXES_DIR/<client_id>``.
Both roots live on the LUKS2-encrypted volume (RAYAAAA-221 —
``review_engine_data`` binds ``/mnt/luks-pii/review``), so a client's policy
documents are encrypted at rest with no new sub-processor and no new egress.

This module is the **single enforcement point** for the tenancy boundary at the
storage layer. Before RAYAAAA-303, ``client_id`` was interpolated straight into a
filesystem path (``POLICY_UPLOADS_DIR / client_id``) with no validation — a
crafted id such as ``"../other-client"`` or ``"/etc"`` could escape a client's
namespace. Every per-client path is now derived here from a *validated* id and
re-checked for containment, so:

* the namespace token is drawn solely from a validated ``client_id`` (no
  traversal, no separators, no absolute paths), and
* the resolved path is asserted to live under the intended per-client root
  (defence in depth — even a future bug in id validation cannot escape the root).

The P1 session-claim contract (RAYAAAA-302) authenticates *which* client a
request is acting as. This module exposes the seam that consumes that claim:
:class:`ClientScope` carries the authenticated ``client_id`` and its
:meth:`ClientScope.guard` rejects any attempt to touch a *different* client's
store (strictly per-tenant create/reject). Until P1 publishes, callers may
operate without a scope (owner-internal/SYNTHETIC); once P1 wires the claim in,
pass the resolved :class:`ClientScope` to the policy service methods and the
storage layer enforces the boundary for free.

SYNTHETIC / owner-internal data only until the Phase-4 gate (RAYAAAA-297 /
RAYAAAA-301). No real client PII.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from review_engine.config.settings import POLICY_INDEXES_DIR, POLICY_UPLOADS_DIR


class ClientAccessError(ValueError):
    """A ``client_id`` is malformed or a path would escape its client namespace."""


class CrossClientAccessError(ClientAccessError):
    """A request authenticated as client A tried to reach client B's store."""


# A client id is a stable namespace token. ``clients.id`` is either the
# system-minted ``CLI-<10 hex>`` (see ``ReviewDatabase._insert_client``) or a
# portal-supplied id capped at 64 chars. We accept the conservative intersection
# that is always safe as a single path component: alphanumerics plus ``._-``,
# 1..64 chars, and never the reserved ``.``/``..`` names. Anything else is
# rejected rather than sanitised — a silently rewritten id would collide two
# tenants into one namespace, which is exactly the failure this guards against.
_CLIENT_ID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")


def validate_client_id(client_id: str) -> str:
    """Return ``client_id`` unchanged iff it is a safe per-client namespace token.

    Raises :class:`ClientAccessError` for empty ids, ids containing path
    separators or traversal, absolute paths, or the reserved ``.``/``..`` names.
    Validation never rewrites the id (see module docstring).
    """
    if not isinstance(client_id, str) or not client_id:
        raise ClientAccessError("client_id must be a non-empty string")
    if client_id in (".", ".."):
        raise ClientAccessError(f"Refusing reserved client_id: {client_id!r}")
    if "/" in client_id or "\\" in client_id or "\x00" in client_id:
        raise ClientAccessError(f"client_id contains a path separator: {client_id!r}")
    if not _CLIENT_ID_RE.match(client_id):
        raise ClientAccessError(f"Unsafe client_id: {client_id!r}")
    return client_id


def assert_within(root: Path, path: Path) -> Path:
    """Return ``path`` iff it resolves to a location inside ``root``.

    A belt-and-braces containment check applied to *every* derived per-client
    path. ``root`` need not exist yet; the check is purely lexical over the
    resolved (symlink- and ``..``-collapsed) forms, so it holds before the
    directory is created.
    """
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    if path_resolved != root_resolved and root_resolved not in path_resolved.parents:
        raise ClientAccessError(
            f"Path {path_resolved} escapes client-storage root {root_resolved}"
        )
    return path_resolved


def client_policy_upload_dir(client_id: str) -> Path:
    """Absolute per-client raw-upload directory (created on demand).

    ``POLICY_UPLOADS_DIR/<validated client_id>``, asserted to live under
    ``POLICY_UPLOADS_DIR``. This is the sole path a client's raw policy files may
    be written to.
    """
    cid = validate_client_id(client_id)
    target = assert_within(POLICY_UPLOADS_DIR, POLICY_UPLOADS_DIR / cid)
    target.mkdir(parents=True, exist_ok=True)
    return target


def client_policy_index_root(client_id: str) -> Path:
    """Absolute per-client policy-index directory root.

    ``POLICY_INDEXES_DIR/<validated client_id>``, asserted under
    ``POLICY_INDEXES_DIR``. The directory itself is created by the Chroma
    ``PersistentClient`` (``PolicyLibraryIndex``); this only derives + guards the
    path so index storage cannot escape the client namespace either.
    """
    cid = validate_client_id(client_id)
    return assert_within(POLICY_INDEXES_DIR, POLICY_INDEXES_DIR / cid)


@dataclass(frozen=True)
class ClientScope:
    """The authenticated per-client scope resolved from a P1 session claim.

    RAYAAAA-302 (P1) authenticates a request as a specific client and hands the
    storage/service layer a ``ClientScope`` carrying the validated
    ``client_id``. Any per-tenant operation is then checked with
    :meth:`guard`: touching a *different* client's store raises
    :class:`CrossClientAccessError`. Construct via :meth:`for_client` so the id
    is validated at the boundary.
    """

    client_id: str

    @classmethod
    def for_client(cls, client_id: str) -> "ClientScope":
        return cls(validate_client_id(client_id))

    def guard(self, target_client_id: str) -> str:
        """Return ``target_client_id`` iff it is exactly this scope's client.

        The per-tenant create/reject gate: a session scoped to client A may only
        create/read/erase under client A. Both ids are validated so a malformed
        target is rejected before the equality check.
        """
        target = validate_client_id(target_client_id)
        if target != self.client_id:
            raise CrossClientAccessError(
                "Session scoped to client "
                f"{self.client_id!r} may not access client {target!r}"
            )
        return target

    @property
    def upload_dir(self) -> Path:
        return client_policy_upload_dir(self.client_id)

    @property
    def index_root(self) -> Path:
        return client_policy_index_root(self.client_id)


def require_client(scope: "ClientScope | None", target_client_id: str) -> str:
    """Validate ``target_client_id`` and, if ``scope`` is present, enforce it.

    The convenience the policy service uses at every per-client entry point:
    with no scope (owner-internal/SYNTHETIC, pre-P1) it just validates the id;
    with a scope it additionally rejects cross-client access. This keeps the P1
    integration a one-line seam per call site.
    """
    if scope is None:
        return validate_client_id(target_client_id)
    return scope.guard(target_client_id)
