from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
MATTERS_DIR = DATA_DIR / "matters"
UPLOADS_DIR = DATA_DIR / "uploads"
PROCESSED_DIR = DATA_DIR / "processed"
INDEXES_DIR = DATA_DIR / "indexes"
SAMPLES_DIR = DATA_DIR / "samples"
DATABASE_PATH = DATA_DIR / "review_engine.sqlite3"

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".csv", ".xlsx"}
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150
EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def ensure_directories() -> None:
    for path in (
        DATA_DIR,
        MATTERS_DIR,
        UPLOADS_DIR,
        PROCESSED_DIR,
        INDEXES_DIR,
        SAMPLES_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)
