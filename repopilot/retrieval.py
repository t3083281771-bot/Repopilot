"""Code-aware lexical search, vector retrieval, and reciprocal rank fusion."""

from __future__ import annotations

from collections import Counter
import math
from pathlib import PurePosixPath
import re
from typing import TYPE_CHECKING

from .embeddings import embedding_profile, get_embedder, tokenize
from .models import Chunk

if TYPE_CHECKING:
    from .config import Settings
    from .store import Store


def _scored(chunk: Chunk, score: float) -> Chunk:
    return chunk.model_copy(update={"score": float(score), "embedding": []})


class Retriever:
    """The optional reranker is a lightweight lexical heuristic, not a cross-encoder."""

    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store

    def _chunks(self, repo_id: str) -> list[Chunk]:
        repository = self.store.get_repo(repo_id)
        if not repository:
            raise ValueError("Repository not found")
        if repository.get("status") != "ready":
            raise ValueError("Repository is not ready for retrieval")
        return self.store.get_chunks(repo_id)

    @staticmethod
    def _bm25(chunks: list[Chunk], query: str, k: int) -> list[Chunk]:
        terms = set(tokenize(query))
        if not chunks or not terms:
            return []
        documents = [Counter(tokenize(f"{item.path}\n{item.symbol}\n{item.symbol}\n{item.content}")) for item in chunks]
        lengths = [sum(document.values()) for document in documents]
        average = sum(lengths) / len(lengths) or 1.0
        frequencies = {term: sum(term in document for document in documents) for term in terms}
        scored = []
        for chunk, document, length in zip(chunks, documents, lengths):
            score = 0.0
            for term in terms:
                frequency = document.get(term, 0)
                if not frequency:
                    continue
                idf = math.log1p((len(chunks) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / average))
            if score > 0:
                scored.append(_scored(chunk, score))
        return sorted(scored, key=lambda item: (-item.score, item.path, item.start_line))[:k]

    @staticmethod
    def _symbols(chunks: list[Chunk], query: str, k: int) -> list[Chunk]:
        needles = set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)*", query.casefold()))
        query_terms = set(tokenize(query))
        scored = []
        for chunk in chunks:
            symbol = chunk.symbol.casefold()
            components = set(symbol.split("."))
            score = 0.0
            if symbol != "<module>":
                score += 20.0 * bool(symbol in needles)
                score += 10.0 * bool(components & needles)
            path_name = PurePosixPath(chunk.path).stem.casefold()
            score += 5.0 * bool(path_name in needles)
            shared = query_terms & set(tokenize(chunk.symbol))
            score += len(shared)
            if score:
                scored.append(_scored(chunk, score))
        return sorted(scored, key=lambda item: (-item.score, item.path, item.start_line))[:k]

    def search_symbols(self, repo_id: str, query: str, k: int = 5) -> list[Chunk]:
        if not 1 <= k <= 50:
            raise ValueError("k must be between 1 and 50")
        return self._symbols(self._chunks(repo_id), query, k)

    def search(self, repo_id: str, query: str, strategy: str = "hybrid", k: int = 5) -> list[Chunk]:
        if not 1 <= k <= 50:
            raise ValueError("k must be between 1 and 50")
        if not query.strip():
            return []
        if strategy not in {"bm25", "vector", "hybrid", "hybrid_rerank"}:
            raise ValueError("Unknown retrieval strategy")
        chunks = self._chunks(repo_id)
        if strategy == "bm25":
            return self._bm25(chunks, query, k)
        repository = self.store.get_repo(repo_id)
        if repository.get("embedding_profile") != embedding_profile(self.settings):
            raise ValueError("Embedding provider or model changed; re-import the repository before vector retrieval")
        vector = get_embedder(self.settings).embed_query(query)
        candidates = max(20, k * 4)
        vector_results = self.store.vector_search(repo_id, vector, candidates)
        if strategy == "vector":
            return [_scored(item, item.score) for item in vector_results[:k]]
        lists = [
            (self._bm25(chunks, query, candidates), 1.0),
            (vector_results, 1.0),
            (self._symbols(chunks, query, candidates), 1.25),
        ]
        scores: dict[str, float] = {}
        by_id: dict[str, Chunk] = {}
        for results, weight in lists:
            for rank, item in enumerate(results, 1):
                by_id[item.id] = item
                scores[item.id] = scores.get(item.id, 0.0) + weight / (60 + rank)
        if strategy == "hybrid_rerank":
            terms = set(tokenize(query))
            for chunk_id, item in by_id.items():
                body_terms = set(tokenize(f"{item.path} {item.symbol} {item.content}"))
                overlap = len(terms & body_terms) / max(1, len(terms))
                scores[chunk_id] += 0.03 * overlap
        ranked = sorted(by_id.values(), key=lambda item: (-scores[item.id], item.path, item.start_line))
        return [_scored(item, scores[item.id]) for item in ranked[:k]]

    def read_file(self, repo_id: str, path: str, start_line: int = 1, end_line: int = 100) -> dict:
        relative = PurePosixPath(path)
        if (
            not path or relative.is_absolute() or "\\" in path or ":" in path
            or ".." in relative.parts or path != relative.as_posix()
        ):
            raise ValueError("Invalid repository-relative file path")
        if start_line < 1 or end_line < start_line:
            raise ValueError("Invalid file line range")
        indexed = [chunk for chunk in self._chunks(repo_id) if chunk.path == path]
        if not indexed:
            raise ValueError("Only indexed source files can be read")
        # Chunks are the persisted source snapshot used by citations. Reconstruct
        # from those bytes rather than accessing a mutable or unavailable OS path.
        # The importer covers every nonempty line; omitted gaps are blank lines.
        lines: dict[int, str] = {}
        last_line = 0
        for chunk in indexed:
            if (
                chunk.start_line < 1 or chunk.end_line < chunk.start_line
                or chunk.end_line > self.settings.max_file_bytes + 1
            ):
                raise ValueError("Indexed file contains invalid source line positions")
            content_lines = chunk.content.split("\n")
            if len(content_lines) != chunk.end_line - chunk.start_line + 1:
                raise ValueError("Indexed file chunk does not match its source line range")
            for line_number, content in enumerate(content_lines, chunk.start_line):
                if line_number in lines and lines[line_number] != content:
                    raise ValueError("Indexed source chunks contain inconsistent overlapping lines")
                lines[line_number] = content
            last_line = max(last_line, chunk.end_line)
        reconstructed_bytes = sum(len(content.encode("utf-8")) for content in lines.values()) + max(0, last_line - 1)
        if reconstructed_bytes > self.settings.max_file_bytes:
            raise ValueError("Indexed file exceeds read budget")
        if start_line > last_line:
            raise ValueError("Starting line is outside the file")
        actual_end = min(end_line, start_line + 199, last_line)
        return {
            "path": path, "start_line": start_line, "end_line": actual_end,
            "content": "\n".join(lines.get(line_number, "") for line_number in range(start_line, actual_end + 1)),
            "source": "indexed_snapshot",
        }
