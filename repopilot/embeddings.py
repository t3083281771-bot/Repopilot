"""Pluggable embeddings. Hash embeddings are a lexical demo, not semantics."""

from __future__ import annotations

from collections import Counter
import hashlib
import math
import re
import threading
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .config import Settings


def tokenize(text: str) -> list[str]:
    """Keep complete code identifiers and their snake/camel case components."""
    tokens: list[str] = []
    for word in re.findall(r"[A-Za-z_][A-Za-z_0-9]*|\d+|[\u3400-\u9fff]+", text):
        tokens.append(word.casefold())
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|\b)|[A-Z]?[a-z]+|\d+", word)
        if len(parts) > 1 or "_" in word:
            tokens.extend(part.casefold() for part in parts)
    return tokens


def _normalize(vector: list[float]) -> list[float]:
    if not vector or any(not math.isfinite(value) for value in vector):
        raise ValueError("Embedding must contain finite numeric values")
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


class HashEmbedder:
    """Deterministic hashed token vectors for offline lexical demonstrations."""

    description = "Lexical hash demo; does not provide semantic embeddings"

    def __init__(self, dimensions: int = 384):
        if dimensions < 32:
            raise ValueError("Hash dimensions must be at least 32")
        self.dimensions = dimensions

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * self.dimensions
            for token, frequency in Counter(tokenize(text)).items():
                digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
                position = int.from_bytes(digest[:4], "big") % self.dimensions
                sign = 1.0 if digest[4] & 1 else -1.0
                vector[position] += sign * (1.0 + math.log(frequency))
            vectors.append(_normalize(vector))
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


_MODEL_CACHE: dict[str, object] = {}
_MODEL_LOCK = threading.Lock()


class SentenceTransformerEmbedder:
    description = "Semantic embeddings from an optional sentence-transformers model"

    def __init__(self, model: str):
        self.model_name = model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "Install the optional semantic extra to use sentence-transformers"
            ) from exc
        with _MODEL_LOCK:
            if self.model_name not in _MODEL_CACHE:
                _MODEL_CACHE[self.model_name] = SentenceTransformer(
                    self.model_name, trust_remote_code=False
                )
            model = _MODEL_CACHE[self.model_name]
        values = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [_normalize([float(value) for value in row]) for row in values]

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


class APIEmbedder:
    description = "Semantic embeddings from a configured OpenAI-compatible API"

    def __init__(self, settings: Settings):
        self.base_url = settings.embedding_base_url.rstrip("/")
        self.model = settings.embedding_model
        self.api_key = settings.embedding_api_key
        self.timeout = settings.request_timeout
        if not self.base_url or not self.model:
            raise ValueError("API embeddings require EMBEDDING_BASE_URL and EMBEDDING_MODEL")
        if not self.base_url.startswith(("https://", "http://")):
            raise ValueError("Embedding base URL must use HTTP or HTTPS")

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        vectors: list[list[float]] = []
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            for offset in range(0, len(texts), 64):
                batch = texts[offset:offset + 64]
                response = client.post(
                    f"{self.base_url}/embeddings",
                    headers=headers,
                    json={"model": self.model, "input": batch},
                )
                if response.is_error:
                    raise RuntimeError(f"Embedding API returned HTTP {response.status_code}")
                data = response.json().get("data", [])
                if len(data) != len(batch):
                    raise ValueError("Embedding API returned an unexpected number of vectors")
                indices = [item.get("index") for item in data]
                if sorted(indices) != list(range(len(batch))):
                    raise ValueError("Embedding API returned invalid vector indices")
                for item in sorted(data, key=lambda row: row["index"]):
                    vectors.append(_normalize([float(value) for value in item["embedding"]]))
        if len({len(vector) for vector in vectors}) != 1:
            raise ValueError("Embedding API returned inconsistent dimensions")
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


def get_embedder(settings: Settings):
    if settings.embedding_provider == "hash":
        return HashEmbedder()
    if settings.embedding_provider == "sentence-transformers":
        return SentenceTransformerEmbedder(settings.embedding_model)
    if settings.embedding_provider == "api":
        return APIEmbedder(settings)
    raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")


def embedding_profile(settings: Settings) -> str:
    """Non-secret identity used to prevent comparing incompatible vector spaces."""
    if settings.embedding_provider == "hash":
        return "hash:hash-v1-384"
    if settings.embedding_provider == "sentence-transformers":
        return f"sentence-transformers:{settings.embedding_model}"
    if settings.embedding_provider == "api":
        return f"api:{settings.embedding_model}:{settings.embedding_base_url.rstrip('/')}"
    raise ValueError(f"Unsupported embedding provider: {settings.embedding_provider}")
