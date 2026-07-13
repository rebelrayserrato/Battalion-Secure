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
    def __init__(self, matter_id: str, root: str | Path = INDEXES_DIR):
        self.matter_id = matter_id
        self.root = Path(root) / matter_id
        self.root.mkdir(parents=True, exist_ok=True)
        self.embedding = LocalEmbeddingFunction()
        self._collection = None

    def _get_collection(self):
        if self._collection is None:
            import chromadb

            client = chromadb.PersistentClient(path=str(self.root))
            name = "matter_" + hashlib.sha1(self.matter_id.encode()).hexdigest()[:16]
            self._collection = client.get_or_create_collection(name=name)
        return self._collection

    def build(self, chunks: list[SourceChunk]) -> int:
        collection = self._get_collection()
        existing = collection.get()
        if existing.get("ids"):
            collection.delete(ids=existing["ids"])
        if not chunks:
            return 0
        texts = [chunk.text for chunk in chunks]
        collection.add(
            ids=[chunk.source_ref for chunk in chunks],
            documents=texts,
            embeddings=self.embedding.encode(texts),
            metadatas=[
                {
                    "document_name": chunk.document_name,
                    "file_type": chunk.file_type,
                    "page": chunk.page if chunk.page is not None else -1,
                    "row": chunk.row if chunk.row is not None else -1,
                    "section": chunk.section or "",
                    "citation": chunk.citation,
                }
                for chunk in chunks
            ],
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
            rows.append(
                {
                    "source_ref": source_ref,
                    "text": text,
                    "citation": metadata["citation"],
                    "document_name": metadata.get("document_name", ""),
                    "page": metadata.get("page", -1),
                    "row": metadata.get("row", -1),
                    "section": metadata.get("section", ""),
                    "distance": float(distance),
                }
            )
        return rows
