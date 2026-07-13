# Battalion-Secure review engine — internal-only Streamlit service (RAYAAAA-194).
#
# This image is NEVER published to the host. It runs on the internal docker
# network only and is reachable exclusively through the owner-portal-auth-gated
# nginx route (auth_request -> /api/admin/review-engine-authz). See the deploy
# scaffold in rayserr-services-website (docker-compose review-engine service +
# docker/nginx/review-engine.conf).
#
# Security posture baked in (RAYAAAA-193 CVE gate conditions):
#   - Chroma embedded-only (PersistentClient; no `chroma run`, no listener).
#   - Telemetry OFF (ANONYMIZED_TELEMETRY=False) — no outbound analytics.
#   - No runtime network egress required: embeddings use local_files_only and
#     fall back to deterministic hashing, so the container runs behind an egress
#     firewall with zero external calls.
#   - Non-root runtime user; data lives in a mounted named volume.
FROM python:3.12-slim AS base

# Build deps for PyMuPDF / reportlab / sentence-transformers wheels. curl is kept
# in the final image only for the container healthcheck. tesseract-ocr is the
# runtime OCR engine for RAYAAAA-230 (pytesseract is only the Python wrapper).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libglib2.0-0 libgl1 curl \
        tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY review_engine/requirements.txt ./review_engine/requirements.txt
RUN pip install --no-cache-dir -r review_engine/requirements.txt

COPY review_engine/ ./review_engine/

# RAYAAAA-227: Streamlit resolves the per-project config at $CWD/.streamlit, and
# the container CWD is /app (WORKDIR), so the theme in review_engine/.streamlit
# would otherwise be ignored. Surface it at /app/.streamlit so the RAYSERR theme
# actually applies. Server keys still come from the ENV block below.
RUN cp -r review_engine/.streamlit /app/.streamlit

# Non-root runtime user owns /app so the mounted data volume inherits it.
RUN useradd -m -u 10001 appuser \
    && mkdir -p /app/review_engine/data \
    && chown -R appuser:appuser /app
USER appuser

# Streamlit served under the portal subpath; bound to all interfaces INSIDE the
# container only (the compose service uses `expose`, never `ports`, so this is
# not an 0.0.0.0:port on the host).
ENV STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501 \
    STREAMLIT_SERVER_BASE_URL_PATH=admin/review-engine \
    STREAMLIT_SERVER_ENABLE_CORS=false \
    STREAMLIT_SERVER_ENABLE_XSRF_PROTECTION=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    # Chroma: disable product telemetry (RAYAAAA-193 fix) — no posthog egress.
    ANONYMIZED_TELEMETRY=False \
    CHROMA_TELEMETRY_IMPL=none \
    # HuggingFace / sentence-transformers: never touch the network at runtime.
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    # Optional Ollama summariser stays disabled by default (RAYAAAA-192 posture).
    REVIEW_ENGINE_OLLAMA_ENABLED=0 \
    PYTHONUNBUFFERED=1

EXPOSE 8501
# RAYAAAA-210: matter-API sidecar port (internal only; nginx auth gate is ingress).
EXPOSE 8600
# Healthcheck covers BOTH processes: the sidecar must answer before the container
# is "healthy", so the portal producer never races an unstarted API.
HEALTHCHECK --interval=15s --timeout=5s --retries=6 --start-period=90s \
    CMD curl -fsS http://127.0.0.1:8501/admin/review-engine/_stcore/health \
        && curl -fsS http://127.0.0.1:8600/admin/review-engine/api/health || exit 1

# run_app.py starts the matter-API sidecar (uvicorn :8600) then `streamlit run
# app/main.py`, both inheriting the ENV above.
CMD ["python", "review_engine/run_app.py"]
