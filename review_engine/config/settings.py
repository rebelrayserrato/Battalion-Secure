import os
from pathlib import Path


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
MATTERS_DIR = DATA_DIR / "matters"
UPLOADS_DIR = DATA_DIR / "uploads"
PROCESSED_DIR = DATA_DIR / "processed"
INDEXES_DIR = DATA_DIR / "indexes"
SAMPLES_DIR = DATA_DIR / "samples"
# RAYAAAA-245 (Phase B): a Client's uploaded HR/company policy corpus lives
# entirely apart from any single Task's documents. Policy uploads and the
# client-scoped Chroma indexes get their own directory trees, keyed by client
# id, so Client X's policy library is physically isolated from Client Y's and
# from every Task index (the cross-client isolation boundary is enforced by
# this scoping, not by post-filtering).
POLICY_UPLOADS_DIR = DATA_DIR / "policy_uploads"
POLICY_INDEXES_DIR = DATA_DIR / "policy_indexes"
# RAYAAAA-251 (Phase C): a jurisdiction-scoped LAW reference corpus (statute /
# regulation text uploaded from official government publishers, keyed by US
# state or ``federal``). This is public-domain law, PII-free, and is kept in its
# OWN directory trees keyed by JURISDICTION — never by client_id or matter_id —
# so client-data erasure (fan-out + 90-day idle, which sweep only matter-keyed
# stores) can never touch the law corpus. See ``review_engine/law``.
LAW_UPLOADS_DIR = DATA_DIR / "law_uploads"
LAW_INDEXES_DIR = DATA_DIR / "law_indexes"
# RAYAAAA-275 (RAYAAAA-270 P3): the "Pending Review" staging area for laws that
# the web-ingest pipeline (RAYAAAA-274 P2) fetched from official government
# publishers but that have NOT yet been approved into the live law index. Nothing
# here is retrievable/citable — it is a holding pen keyed by jurisdiction; only an
# explicit owner Approve moves a record through the RAYAAAA-251 provenance-enforced
# upload into ``LAW_UPLOADS_DIR``/the live index. Kept apart from the live corpus so
# a pending item can never leak into a grounded answer.
LAW_STAGING_DIR = DATA_DIR / "law_staging"
DATABASE_PATH = DATA_DIR / "review_engine.sqlite3"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
# RAYAAAA-230: OCR (scanned PDFs + standalone images) and safe ZIP ingestion.
# PDF/DOCX/TXT/CSV/XLSX handling is unchanged; these are additive.
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".csv", ".xlsx", ".zip"} | IMAGE_EXTENSIONS
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150
EMBEDDING_MODEL = "all-MiniLM-L6-v2"

# --- OCR (local Tesseract only — no cloud OCR, no egress) -------------------
# OCR is a *fallback*: it runs for standalone images and for PDF pages whose
# native text layer yields fewer than OCR_MIN_NATIVE_CHARS characters.
OCR_ENABLED = _env_flag("REVIEW_ENGINE_OCR_ENABLED", True)
OCR_LANG = os.getenv("REVIEW_ENGINE_OCR_LANG", "eng")
OCR_DPI = _env_int("REVIEW_ENGINE_OCR_DPI", 200)
OCR_MIN_NATIVE_CHARS = _env_int("REVIEW_ENGINE_OCR_MIN_NATIVE_CHARS", 40)

# --- ZIP ingestion safety guards (zip-bomb / traversal defence) ------------
ZIP_MAX_FILES = _env_int("REVIEW_ENGINE_ZIP_MAX_FILES", 512)
ZIP_MAX_TOTAL_BYTES = _env_int("REVIEW_ENGINE_ZIP_MAX_TOTAL_BYTES", 512 * 1024 * 1024)
ZIP_MAX_RATIO = _env_int("REVIEW_ENGINE_ZIP_MAX_RATIO", 100)

# --- MCP multi-model connector (RAYAAAA-246, Phase B1) ---
# Real egress to OpenAI / Anthropic / Hermes is OFF by default. Flip
# MCP_CONNECTOR_ENABLED=1 (behind the internal auth gate) to allow live calls;
# otherwise every provider runs in deterministic mock/stub mode. MCP_MOCK=1
# forces mock even when the connector is enabled. API keys are read from the
# environment only (OPENAI_API_KEY / ANTHROPIC_API_KEY / HERMES_API_KEY) and are
# never stored in source. Synthetic-only until the Phase C gate.
MCP_CONNECTOR_ENABLED = _env_flag("MCP_CONNECTOR_ENABLED", False)
MCP_FORCE_MOCK = _env_flag("MCP_MOCK", False)

# --- Cross-Task owner-scoped assistant (RAYAAAA-247, Phase B2) ---
# The "sees everything" personal-assistant surface reads ACROSS the owner's
# Tasks (vs. today's single-Task Chat). It is an OWNER-INTERNAL capability and
# is OFF by default. "Sees everything" == everything the OWNER is entitled to
# see, NOT a tenant-isolation bypass: the retriever still enforces the
# RAYAAAA-241/244/245 per-client isolation boundary structurally (an answer
# framed around one Client can never reach another Client's documents).
#
# Two gates guard it (defense in depth, mirroring the matter-API posture):
#   1. CROSS_TASK_ASSISTANT_ENABLED feature flag (OFF by default), and
#   2. CROSS_TASK_ASSISTANT_TOKEN internal shared secret — when set, callers
#      must present a matching token. Read from the environment only; never
#      stored in source. Synthetic / owner-internal data only until the Phase 4
#      gate (RAYAAAA-196/198).
CROSS_TASK_ASSISTANT_ENABLED = _env_flag("CROSS_TASK_ASSISTANT_ENABLED", False)
CROSS_TASK_ASSISTANT_TOKEN = os.getenv("CROSS_TASK_ASSISTANT_TOKEN")

# --- Web-connected law ingest (RAYAAAA-270) --------------------------------
# Master feature flag for the official-gov-source web law-ingest capability
# (egress proxy P1 / fetch+extract pipeline P2 / this "Pending Review" queue P3).
# OFF by default and INERT until the full RAYAAAA-270 cutover clears its gates
# (Counsel Cond A–E on RAYAAAA-271, CTO egress conditions on RAYAAAA-272, Sec/QA
# no-PII-egress verify RAYAAAA-276, and the DPIA addendum RAYAAAA-277). When OFF:
# the pipeline performs no egress and the Law Library shows no Pending Review
# queue, so nothing web-fetched can exist, let alone reach the live index.
# Turning the flag ON never bypasses owner approval — auto-add stays FORBIDDEN
# (RAYAAAA-243 / Counsel + CTO-5); the flag only makes the staged queue visible.
LAW_WEB_INGEST_ENABLED = _env_flag("LAW_WEB_INGEST_ENABLED", False)


def ensure_directories() -> None:
    for path in (
        DATA_DIR,
        MATTERS_DIR,
        UPLOADS_DIR,
        PROCESSED_DIR,
        INDEXES_DIR,
        SAMPLES_DIR,
        POLICY_UPLOADS_DIR,
        POLICY_INDEXES_DIR,
        LAW_UPLOADS_DIR,
        LAW_INDEXES_DIR,
        LAW_STAGING_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
