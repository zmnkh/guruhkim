"""이중 인덱스 판정.

청크 하나가 "중요"가 되는 경우는 두 가지다.

1. 정확 일치: 중요 문서에만 있고 일반 문서에는 없는 문장이 그대로 들어 있다.
2. 의미 유사: 중요 인덱스와 충분히 비슷한데, 일반 인덱스에는 그만큼 비슷한 것이 없다.

중요 인덱스와 비슷하더라도 일반 인덱스에 같은 수준으로 비슷한 내용이 있으면
"중요 문서의 비핵심 데이터를 쓴 것"으로 보고 일반으로 돌린다(neutralized).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from .chunking import chunk_text, sentence_hash, split_sentences
from .store import Store


@dataclass
class Thresholds:
    important: float = 0.80  # 중요 인덱스와 이 이상 비슷해야 후보가 된다
    general: float = 0.80  # 일반 인덱스와 이 이상 비슷해야 상쇄 자격이 있다
    margin: float = 0.05  # 일반 유사도가 (중요 유사도 - margin) 이상이어야 상쇄된다
    min_hits: int = 1  # 중요 청크가 이만큼 있으면 문서를 중요로 판정

    @classmethod
    def for_embedder(cls, name: str) -> "Thresholds":
        # 해싱 임베딩은 유사도 분포가 낮게 깔려서 기준도 낮춘다.
        if name == "hashing":
            return cls(important=0.60, general=0.60, margin=0.10)
        return cls()


@dataclass
class ChunkResult:
    index: int
    text: str
    verdict: str  # "important" | "general"
    reason: str  # "exact" | "semantic" | "neutralized" | "no_match"
    sim_important: float
    sim_general: float
    matched_important_doc: str | None = None
    matched_general_doc: str | None = None
    exact_sentences: list[str] = field(default_factory=list)


@dataclass
class DocumentResult:
    verdict: str
    important_chunks: int
    total_chunks: int
    chunks: list[ChunkResult]

    def to_dict(self) -> dict:
        return asdict(self)


class Classifier:
    def __init__(self, store: Store, thresholds: Thresholds | None = None) -> None:
        self.store = store
        self.t = thresholds or Thresholds.for_embedder(store.embedder.name)

    def classify(self, text: str, max_chars: int = 200) -> DocumentResult:
        pieces = chunk_text(text, max_chars=max_chars)
        vectors = self.store.embedder.encode(pieces) if pieces else []
        imp = self.store.best_matches(vectors, "important")
        gen = self.store.best_matches(vectors, "general")

        general_hashes = self.store.sentence_hashes("general")
        exclusive = {
            h: doc
            for h, doc in self.store.sentence_hashes("important").items()
            if h not in general_hashes
        }

        results: list[ChunkResult] = []
        for i, piece in enumerate(pieces):
            (s_imp, m_imp), (s_gen, m_gen) = imp[i], gen[i]
            exact = [s for s in split_sentences(piece) if sentence_hash(s) in exclusive]
            imp_doc = m_imp["doc"] if m_imp else None

            if exact:
                verdict, reason = "important", "exact"
                imp_doc = exclusive[sentence_hash(exact[0])]
            elif s_imp < self.t.important:
                verdict, reason = "general", "no_match"
            elif s_gen >= max(self.t.general, s_imp - self.t.margin):
                verdict, reason = "general", "neutralized"
            else:
                verdict, reason = "important", "semantic"

            results.append(
                ChunkResult(
                    index=i,
                    text=piece,
                    verdict=verdict,
                    reason=reason,
                    sim_important=round(s_imp, 4),
                    sim_general=round(s_gen, 4),
                    matched_important_doc=imp_doc,
                    matched_general_doc=m_gen["doc"] if m_gen else None,
                    exact_sentences=exact,
                )
            )

        hits = sum(r.verdict == "important" for r in results)
        return DocumentResult(
            verdict="important" if hits >= self.t.min_hits else "general",
            important_chunks=hits,
            total_chunks=len(results),
            chunks=results,
        )
