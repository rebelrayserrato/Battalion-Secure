from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Optional


def source_reference(
    matter_id: str,
    document_name: str,
    *,
    page: Optional[int] = None,
    row: Optional[int] = None,
    section: Optional[str] = None,
    ordinal: int = 0,
) -> str:
    location = f"p{page}" if page is not None else f"r{row}" if row is not None else section or "body"
    digest = sha256(
        f"{matter_id}|{document_name}|{location}|{ordinal}".encode("utf-8")
    ).hexdigest()[:12]
    return f"SRC-{digest.upper()}"


@dataclass(frozen=True)
class SourceChunk:
    matter_id: str
    document_name: str
    file_type: str
    text: str
    source_ref: str
    page: Optional[int] = None
    row: Optional[int] = None
    section: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def citation(self) -> str:
        location = (
            f"page {self.page}"
            if self.page is not None
            else f"row {self.row}"
            if self.row is not None
            else f"section {self.section}"
            if self.section
            else "source chunk"
        )
        return f"{self.document_name}, {location} ({self.source_ref})"
