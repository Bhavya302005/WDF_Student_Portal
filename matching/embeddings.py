from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import math
import re


class EmbeddingProvider(ABC):
    model_name: str
    version: str

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        """Embed search queries (candidate profiles); symmetric models reuse embed."""
        return self.embed(texts)


class FastEmbedProvider(EmbeddingProvider):
    """BAAI/bge-small-en-v1.5 via ONNX (fastembed): 384-d, CPU-only, no torch."""

    # BGE retrieval convention: queries carry an instruction, passages do not.
    QUERY_INSTRUCTION = "Represent this sentence for searching relevant passages: "

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5", version: str = "bge-small-en-v1.5",
                 batch_size: int = 64) -> None:
        import threading
        self.model_name, self.version, self.batch_size = model_name, version, batch_size
        self._model = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    from fastembed import TextEmbedding
                    self._model = TextEmbedding(self.model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return [vector.tolist() for vector in self._load().embed(texts, batch_size=self.batch_size)]

    def embed_query(self, texts: list[str]) -> list[list[float]]:
        return self.embed([self.QUERY_INSTRUCTION + text for text in texts])


class HashingEmbeddingProvider(EmbeddingProvider):
    """Dependency-free deterministic fallback; replace with BGE in production."""

    def __init__(self, dimensions: int = 384, version: str = "hashing-v1") -> None:
        self.dimensions = dimensions
        self.model_name = "feature-hashing"
        self.version = version

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = re.findall(r"[\w+#.]+", text.casefold())
        features = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            vector[index] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(x * x for x in vector)) or 1.0
        return [x / norm for x in vector]


def cosine(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    raw = sum(a * b for a, b in zip(left, right))
    return max(0.0, min(1.0, (raw + 1.0) / 2.0))
