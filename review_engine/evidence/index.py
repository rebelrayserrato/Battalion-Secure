from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from review_engine.config.settings import EMBEDDING_MODEL, INDEXES_DIR
from review_engine.extraction.models import SourceChunk


class LocalEmbeddingFunction:
    """Uses a cached sentence-transformer; never downloads. Falls back to local hashing."""

    def __init__(self):
        self.model = None
        try:
            from sentence_transformers import SentenceTransformer

            self.model = SentenceTransformer(EMBEDDING_MODEL, local_files_only=True)
        except Exception:
            self.model = None

    def encode(self, texts: list[str]) -> list[list[float]]:
        if self.model is not None:
            return self.model.encode(texts, normalize_embeddings=True).tolist()
        vectors = []
        for text in texts:
            vector = [0.0] * 384
            for token in re.findall(r"[A-Za-z0-9$.-]+", text.lower()):
                digest = hashlib.sha256(token.encode("utf-8")).digest()
                vector[int.from_bytes(digest[:2], "big") % len(vector)] += 1.0
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            vectors.append([value / norm for value in vector])
        return vectors


class EvidenceIndex:
    def __init__(
        self,
        matter_id: str,
        root: str | Path = INDEXES_DIR,
        *,
        collection_prefix: str = "matter",
    ):
        # ``collection_prefix`` + a per-id subdirectory give each index its own
        # physical Chroma store AND a distinct collection name. The client-scoped
        # policy library (RAYAAAA-245) reuses this class with a separate root and
        # prefix so a Client's policies can never share a store with a Task's
        # documents or with another Client's library.
        self.matter_id = matter_id
        self.collection_prefix = collection_prefix
        self.root = Path(root) / matter_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.embedding = LocalEmbeddingFunction()
        self._collection = None

    def _get_collection(self):
        if self._collection is None:
            import chromadb

            client = chromadb.PersistentClient(path=str(self.root))
            name = (
                f"{self.collection_prefix}_"
                + hashlib.sha1(self.matter_id.encode()).hexdigest()[:16]
            )
            self._collection = client.get_or_create_collection(name=name)
        return self._collection

    def build(self, chunks: list[SourceChunk], *, metadata_extra=None) -> int:
        """(Re)build the collection from ``chunks``.

        ``metadata_extra`` is an optional ``SourceChunk -> dict`` hook whose keys
        are merged into each chunk's stored metadata. The jurisdiction-scoped law
        library (RAYAAAA-251) uses it to attach mandatory per-document provenance
        (source name/URL, effective version, retrieval date) so every retrieved
        law chunk carries its own citation-provenance stamp — the base Task /
        policy indexes pass nothing and are byte-for-byte unchanged.
        """
        collection = self._get_collection()
        existing = collection.get()
        if existing.get("ids"):
            collection.delete(ids=existing["ids"])
        if not chunks:
            return 0
        texts = [chunk.text for chunk in chunks]
        metadatas = []
        for chunk in chunks:
            metadata = {
                "document_name": chunk.document_name,
                "file_type": chunk.file_type,
                "page": chunk.page if chunk.page is not None else -1,
                "row": chunk.row if chunk.row is not None else -1,
                "section": chunk.section or "",
                "citation": chunk.citation,
            }
            if metadata_extra is not None:
                metadata.update(metadata_extra(chunk))
            metadatas.append(metadata)
        collection.add(
            ids=[chunk.source_ref for chunk in chunks],
            documents=texts,
            embeddings=self.embedding.encode(texts),
            metadatas=metadatas,
        )
        return len(chunks)

    def search(self, query: str, limit: int = 8) -> list[dict]:
        collection = self._get_collection()
        if collection.count() == 0:
            return []
        result = collection.query(
            query_embeddings=self.embedding.encode([query]),
            n_results=min(limit, collection.count()),
            include=["documents", "metadatas", "distances"],
        )
        rows = []
        for source_ref, text, metadata, distance in zip(
            result["ids"][0], result["documents"][0], result["metadatas"][0], result["distances"][0]
        ):
            row = {
                "source_ref": source_ref,
                "text": text,
                "citation": metadata["citation"],
                "document_name": metadata.get("document_name", ""),
                "page": metadata.get("page", -1),
                "row": metadata.get("row", -1),
                "section": metadata.get("section", ""),
                "distance": float(distance),
            }
            # Surface any additional stored metadata (e.g. RAYAAAA-251 law
            # provenance keys) without disturbing the fixed keys above.
            for key, value in metadata.items():
                row.setdefault(key, value)
            rows.append(row)
        return rows
