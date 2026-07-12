"""Local Tesseract OCR helpers (RAYAAAA-230).

All OCR runs against a locally-installed Tesseract binary — there is no cloud
OCR and no network egress, consistent with the engine's offline posture. Every
entry point degrades gracefully: if Pillow / pytesseract / the tesseract binary
is missing, or a page fails to render, the helpers log and return an empty
string rather than raising, so extraction of the remaining content still works.
"""
from __future__ import annotations

import io
import logging
from functools import lru_cache
from pathlib import Path

from review_engine.config.settings import OCR_ENABLED, OCR_LANG

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def ocr_available() -> bool:
    """True when OCR is enabled and the local toolchain is usable."""
    if not OCR_ENABLED:
        return False
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as exc:  # pragma: no cover - import environment specific
        logger.warning("OCR disabled: Python OCR libraries unavailable (%s)", exc)
        return False
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
    except Exception as exc:  # pragma: no cover - binary environment specific
        logger.warning("OCR disabled: local tesseract binary unavailable (%s)", exc)
        return False
    return True


def _run(image) -> str:
    import pytesseract

    try:
        return pytesseract.image_to_string(image, lang=OCR_LANG) or ""
    except Exception as exc:  # pragma: no cover - runtime OCR failure
        logger.warning("OCR failed for an image (%s)", exc)
        return ""


def ocr_image_file(path: str | Path) -> str:
    """OCR a standalone image file; returns '' if OCR is unavailable/failed."""
    if not ocr_available():
        return ""
    from PIL import Image

    try:
        with Image.open(path) as image:
            return _run(image).strip()
    except Exception as exc:  # pragma: no cover - corrupt/unsupported image
        logger.warning("Could not open image for OCR (%s)", exc)
        return ""


def ocr_png_bytes(data: bytes) -> str:
    """OCR a rendered page image (PNG bytes); '' if unavailable/failed."""
    if not ocr_available():
        return ""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            return _run(image).strip()
    except Exception as exc:  # pragma: no cover - render/decode failure
        logger.warning("Could not decode rendered page for OCR (%s)", exc)
        return ""
