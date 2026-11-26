"""등록된 문서의 청크 벡터와 문장 해시를 디렉터리에 보관한다.

프로토타입용 단순 저장소다. 문서가 수십만 건을 넘으면 벡터 DB(Qdrant, Milvus 등)로 바꾼다.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .chunking import chunk_hashes, chunk_text
from .embedders import Embedder

LABELS = ("important", "general")


class Store:
    def __init__(self, path: str | Path, embedder: Embedder) -> None:
        self.path = Path(path)
        self.embedder = embedder
        self.chunks: list[dict] = []  # {"doc", "label", "text"}
        self.vectors = np.zeros((0, 0), dtype=np.float32)
        self.sentences: dict[str, dict[str, list[str]]] = {}  # doc -> {"label", "hashes"}
        self._load()

    # ---- 저장/불러오기 -------------------------------------------------

    @staticmethod
    def saved_embedder_name(path: str | Path) -> str | None:
        meta = Path(path) / "meta.json"
        if not meta.exists():
            return None
        return json.loads(meta.read_text(encoding="utf-8"))["embedder"]

    def _load(self) -> None:
        meta_path = self.path / "meta.json"
        if not meta_path.exists():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta["embedder"] != self.embedder.name:
            raise ValueError(
                f"이 저장소는 '{meta['embedder']}' 임베딩으로 만들어졌습니다. "
                f"'{self.embedder.name}'과 섞어 쓸 수 없습니다."
            )
        self.chunks = meta["chunks"]
        self.sentences = meta["sentences"]
        self.vectors = np.load(self.path / "vectors.npy")

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        meta = {"embedder": self.embedder.name, "chunks": self.chunks, "sentences": self.sentences}
        (self.path / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        np.save(self.path / "vectors.npy", self.vectors)

    # ---- 등록 ----------------------------------------------------------

    def remove(self, doc: str) -> bool:
        keep = [i for i, c in enumerate(self.chunks) if c["doc"] != doc]
        existed = len(keep) != len(self.chunks) or doc in self.sentences
        if len(keep) != len(self.chunks):
            self.vectors = self.vectors[keep]
            self.chunks = [self.chunks[i] for i in keep]
        self.sentences.pop(doc, None)
        return existed

    def add(self, doc: str, label: str, text: str, max_chars: int = 200) -> int:
        """문서를 등록한다. 같은 doc 이름이 있으면 교체한다. 등록된 청크 수를 돌려준다."""
        if label not in LABELS:
            raise ValueError(f"label은 {LABELS} 중 하나여야 합니다: {label}")
        self.remove(doc)
        pieces = chunk_text(text, max_chars=max_chars)
        if pieces:
            new = self.embedder.encode(pieces)
            self.vectors = new if self.vectors.size == 0 else np.vstack([self.vectors, new])
            self.chunks.extend({"doc": doc, "label": label, "text": p} for p in pieces)
        hashes = sorted(set().union(*(chunk_hashes(p) for p in pieces)))
        self.sentences[doc] = {"label": label, "hashes": hashes}
        return len(pieces)

    # ---- 조회 ----------------------------------------------------------

    def documents(self) -> dict[str, str]:
        return {doc: info["label"] for doc, info in self.sentences.items()}

    def sentence_hashes(self, label: str) -> dict[str, str]:
        """해당 라벨의 문장 해시 -> 그 문장이 처음 등록된 문서."""
        out: dict[str, str] = {}
        for doc, info in self.sentences.items():
            if info["label"] == label:
                for h in info["hashes"]:
                    out.setdefault(h, doc)
        return out

    def best_matches(self, queries: np.ndarray, label: str) -> list[tuple[float, dict | None]]:
        """각 질의 벡터에 대해 해당 라벨에서 가장 비슷한 청크와 코사인 유사도."""
        idx = [i for i, c in enumerate(self.chunks) if c["label"] == label]
        if not idx or len(queries) == 0:
            return [(0.0, None)] * len(queries)
        sims = queries @ self.vectors[idx].T
        best = sims.argmax(axis=1)
        return [(float(sims[row, col]), self.chunks[idx[col]]) for row, col in enumerate(best)]
