"""등록된 문서의 청크 벡터와 문장 해시를 디렉터리에 보관한다.

일반 문서 쪽은 두 등급으로 나뉜다.
- original : 사람이 등록했거나 승인한 것. 해시 일치에도, 재검토의 근거로도 쓴다.
- learned  : 재검토에서 일반으로 판정되어 자동으로 추가된 문장. 해시 일치에만 쓴다.
             근거로는 쓰지 않는다. 그래야 "비슷한 것의 비슷한 것"으로 번지지 않는다.

프로토타입용 단순 저장소다. 문서가 수십만 건을 넘으면 벡터 DB(Qdrant, Milvus 등)로 바꾼다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import numpy as np

from .chunking import chunk_hashes, chunk_text, normalize
from .embedders import Embedder

LABELS = ("important", "general")
LEARNED_PREFIX = "learned:"
APPROVED_PREFIX = "approved:"


def text_id(text: str) -> str:
    """문장을 가리키는 짧은 ID. 학습 문장과 검토 대기 항목에 쓴다."""
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path, embedder: Embedder) -> None:
        self.path = Path(path)
        self.embedder = embedder
        self.chunks: list[dict] = []  # {"doc", "label", "text", "tier"}
        self.vectors = np.zeros((0, 0), dtype=np.float32)
        self.sentences: dict[str, dict] = {}  # doc -> {"label", "hashes", "tier", "meta"?}
        self.learned: dict[str, dict] = {}  # id -> 학습 문장의 출처 기록
        self.pending: dict[str, dict] = {}  # id -> 근거가 없어 기밀로 남은 문장(사람 검토 대기)
        self.last_revoked: list[str] = []  # 직전 add/remove로 무효가 된 학습 문장 ID
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
        self.learned = meta.get("learned", {})
        self.pending = meta.get("pending", {})
        for item in list(self.chunks) + list(self.sentences.values()):
            item.setdefault("tier", "original")  # 등급이 없던 예전 저장소
        self.vectors = np.load(self.path / "vectors.npy")

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        meta = {
            "embedder": self.embedder.name,
            "chunks": self.chunks,
            "sentences": self.sentences,
            "learned": self.learned,
            "pending": self.pending,
        }
        (self.path / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        np.save(self.path / "vectors.npy", self.vectors)

    # ---- 등록 ----------------------------------------------------------

    def _drop(self, doc: str) -> bool:
        keep = [i for i, c in enumerate(self.chunks) if c["doc"] != doc]
        existed = len(keep) != len(self.chunks) or doc in self.sentences
        if len(keep) != len(self.chunks):
            self.vectors = self.vectors[keep]
            self.chunks = [self.chunks[i] for i in keep]
        self.sentences.pop(doc, None)
        return existed

    def _insert(self, doc: str, label: str, tier: str, pieces: list[str], vectors, meta: dict | None = None) -> None:
        if pieces:
            vectors = np.asarray(vectors, dtype=np.float32).reshape(len(pieces), -1)
            self.vectors = vectors if self.vectors.size == 0 else np.vstack([self.vectors, vectors])
            self.chunks.extend({"doc": doc, "label": label, "text": p, "tier": tier} for p in pieces)
        hashes = sorted(set().union(*(chunk_hashes(p) for p in pieces)))
        entry = {"label": label, "hashes": hashes, "tier": tier}
        if meta:
            entry["meta"] = meta
        self.sentences[doc] = entry

    def remove(self, doc: str) -> bool:
        """문서를 지운다. 그 문서를 근거로 풀렸던 학습 문장도 함께 무효로 한다."""
        self.last_revoked = [i for i, info in self.learned.items() if info["evidence_doc"] == doc]
        for learned_id in self.last_revoked:
            self.revoke(learned_id)
        return self._drop(doc)

    def add(self, doc: str, label: str, text: str, max_chars: int = 200) -> int:
        """문서를 등록한다. 같은 doc 이름이 있으면 교체한다. 등록된 청크 수를 돌려준다.

        기밀 문서를 등록하면, 그 문서와 겹치는 학습 문장을 무효로 한다(last_revoked).
        과거에 풀린 문장이 새 기밀을 조용히 상쇄하지 않고 다시 재검토를 받게 하려는 것이다.
        """
        if label not in LABELS:
            raise ValueError(f"label은 {LABELS} 중 하나여야 합니다: {label}")
        self.remove(doc)
        revoked = list(self.last_revoked)
        pieces = chunk_text(text, max_chars=max_chars)
        self._insert(doc, label, "original", pieces, self.embedder.encode(pieces) if pieces else [])
        if label == "important":
            new_hashes = set(self.sentences[doc]["hashes"])
            for learned_id in list(self.learned):
                if new_hashes & set(self.sentences[LEARNED_PREFIX + learned_id]["hashes"]):
                    self.revoke(learned_id)
                    revoked.append(learned_id)
        self.last_revoked = revoked
        return len(pieces)

    # ---- 학습 등급 -----------------------------------------------------

    def learn(self, text: str, vector, evidence_doc: str, evidence_text: str,
              source_doc: str | None, judge: str) -> str:
        """재검토에서 일반으로 판정된 문장을 학습 등급에 추가하고 출처를 기록한다."""
        learned_id = text_id(text)
        self._drop(LEARNED_PREFIX + learned_id)
        self._insert(LEARNED_PREFIX + learned_id, "general", "learned", [text], [vector])
        self.learned[learned_id] = {
            "text": text,
            "evidence_doc": evidence_doc,
            "evidence_text": evidence_text,
            "source_doc": source_doc,
            "judge": judge,
            "at": _now(),
        }
        self.pending.pop(learned_id, None)
        return learned_id

    def revoke(self, learned_id: str) -> bool:
        if self.learned.pop(learned_id, None) is None:
            return False
        self._drop(LEARNED_PREFIX + learned_id)
        return True

    # ---- 검토 대기열 ---------------------------------------------------

    def note_pending(self, text: str, source_doc: str | None, confidential_doc: str | None, why: str) -> str:
        """근거가 없어 기밀로 남은 문장을 기록한다. 자주 나오는 것을 사람이 승인할 수 있게."""
        pending_id = text_id(text)
        item = self.pending.setdefault(pending_id, {
            "text": text, "count": 0, "docs": [], "status": "open", "first_seen": _now(),
        })
        item["count"] += 1
        if source_doc and source_doc not in item["docs"]:
            item["docs"].append(source_doc)
        item.update(confidential_doc=confidential_doc, why=why, last_seen=_now())
        return pending_id

    def approve(self, pending_id: str, by: str) -> bool:
        """대기 중인 문장을 사람이 일반으로 승인한다. 원본 등급에 들어가 근거로도 쓰인다."""
        item = self.pending.pop(pending_id, None)
        if item is None:
            return False
        meta = {"approved_by": by, "at": _now(), "seen_in": item["docs"], "count": item["count"]}
        doc = APPROVED_PREFIX + pending_id
        self._drop(doc)
        self._insert(doc, "general", "original", [item["text"]], self.embedder.encode([item["text"]]), meta)
        return True

    def dismiss(self, pending_id: str) -> bool:
        """대기 항목을 "기밀이 맞음"으로 닫는다. 다시 나와도 목록에 올리지 않는다."""
        if pending_id not in self.pending:
            return False
        self.pending[pending_id]["status"] = "dismissed"
        return True

    # ---- 조회 ----------------------------------------------------------

    def documents(self) -> dict[str, dict]:
        return {doc: {"label": info["label"], "tier": info["tier"]} for doc, info in self.sentences.items()}

    def sentence_hashes(self, label: str) -> dict[str, str]:
        """해당 라벨의 문장 해시 -> 그 문장이 있는 문서. 원본 등급 문서를 우선한다."""
        out: dict[str, str] = {}
        for tier in ("original", "learned"):
            for doc, info in self.sentences.items():
                if info["label"] == label and info["tier"] == tier:
                    for h in info["hashes"]:
                        out.setdefault(h, doc)
        return out

    def top_matches(self, queries, label: str, k: int = 1, tier: str | None = None) -> list[list[tuple[float, dict]]]:
        """각 질의 벡터에 대해 가장 비슷한 청크 k개를 (유사도, 청크)로, 유사도 내림차순."""
        idx = [i for i, c in enumerate(self.chunks)
               if c["label"] == label and (tier is None or c["tier"] == tier)]
        if not idx or len(queries) == 0:
            return [[] for _ in range(len(queries))]
        sims = np.asarray(queries) @ self.vectors[idx].T
        order = np.argsort(-sims, axis=1)[:, :k]
        return [[(float(sims[row, col]), self.chunks[idx[col]]) for col in cols] for row, cols in enumerate(order)]
