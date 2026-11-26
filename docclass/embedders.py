"""임베딩 백엔드.

- bge-m3  : sentence-transformers로 GPU(H200 등)에서 실행하는 운영용 모델.
- hashing : 문자 n-gram 해싱. 모델 다운로드 없이 동작하는 테스트/데모용.
"""

from __future__ import annotations

import zlib
from typing import Protocol

import numpy as np

from .chunking import normalize


class Embedder(Protocol):
    name: str

    def encode(self, texts: list[str]) -> np.ndarray:
        """L2 정규화된 (len(texts), dim) float32 배열을 돌려준다."""
        ...


class HashingEmbedder:
    """문자 2~3-gram 빈도를 고정 차원으로 해싱한 벡터. 표면적으로 비슷한 글만 잡는다."""

    name = "hashing"

    def __init__(self, dim: int = 4096) -> None:
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            norm = normalize(text)
            for n in (2, 3):
                for i in range(len(norm) - n + 1):
                    # 내장 hash()는 실행마다 값이 달라지므로 crc32를 쓴다.
                    out[row, zlib.crc32(norm[i : i + n].encode("utf-8")) % self.dim] += 1.0
        np.sqrt(out, out=out)  # 자주 나오는 n-gram의 영향 완화
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


class SentenceTransformerEmbedder:
    """sentence-transformers 모델. CUDA가 있으면 GPU에서 돈다."""

    def __init__(self, model: str = "BAAI/bge-m3", device: str | None = None, batch_size: int = 64) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - 선택 의존성
            raise SystemExit(
                "sentence-transformers가 필요합니다: pip install 'docclass[gpu]'"
            ) from exc
        self.name = model
        self.batch_size = batch_size
        self._model = SentenceTransformer(model, device=device)

    def encode(self, texts: list[str]) -> np.ndarray:  # pragma: no cover - GPU 필요
        vectors = self._model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype=np.float32)


ALIASES = {"bge-m3": "BAAI/bge-m3"}


def make_embedder(name: str, device: str | None = None) -> Embedder:
    if name == "hashing":
        return HashingEmbedder()
    return SentenceTransformerEmbedder(ALIASES.get(name, name), device=device)
