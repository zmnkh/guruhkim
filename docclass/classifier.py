"""문서 분류.

문장(청크)마다 다음 순서로 판정한다.

1. 해시  : 일반 저장소(원본·학습)에 같은 문장이 있으면 일반.
2. 후보  : 기밀 문서에만 있는 문장과 해시가 일치하거나, 기밀 문서와 의미상 비슷하면 기밀 후보.
           둘 다 아니면 기밀과 무관한 일반 문장.
3. 재검토: 후보마다 판정기(LLM)에게 "왜 기밀이 아닌가"를 묻는다. 원본 등급 일반 문서의
           인용을 근거로 대고, 그 근거가 코드 검증을 통과해야만 일반으로 바꾼다.
4. 학습  : 그렇게 풀린 문장은 학습 등급에 추가해 다음부터 1단계에서 바로 통과시킨다.
           근거를 못 댄 문장은 기밀로 남기고 사람 검토 대기열에 올린다.

재검토 후에도 기밀로 남은 문장이 min_hits개 이상이면 문서 전체가 기밀이다.
"""

from __future__ import annotations

import sys
from dataclasses import asdict, dataclass, field

from .chunking import chunk_text, sentence_hash, split_sentences
from .judge import Judge, JudgeError, Passage, SimilarityJudge, verify
from .store import LEARNED_PREFIX, Store


@dataclass
class Thresholds:
    important: float = 0.80  # 기밀 문서와 이 이상 비슷하면 기밀 후보
    general: float = 0.80  # (유사도 판정기) 일반 문서와 이 이상 비슷해야 풀어 준다
    margin: float = 0.05  # (유사도 판정기) 일반 유사도가 (기밀 유사도 - margin) 이상이어야 한다
    evidence: float = 0.60  # 재검토에서 인정하는 근거 문장의 최소 유사도
    min_hits: int = 1  # 기밀 문장이 이만큼 있으면 문서를 기밀로 판정

    @classmethod
    def for_embedder(cls, name: str) -> "Thresholds":
        # 해싱 임베딩은 유사도 분포가 낮게 깔려서 기준도 낮춘다.
        if name == "hashing":
            return cls(important=0.60, general=0.60, margin=0.10, evidence=0.35)
        return cls()


@dataclass
class ChunkResult:
    index: int
    text: str
    verdict: str  # "important"(기밀) | "general"(일반)
    reason: str  # general_hash | learned | released | no_match | exact | semantic
    sim_important: float
    sim_general: float
    matched_important_doc: str | None = None
    matched_general_doc: str | None = None  # 일반 판정의 근거 문서
    evidence_text: str | None = None  # 일반 판정의 근거 문장
    review: str = ""  # 재검토 결과 설명
    exact_sentences: list[str] = field(default_factory=list)


@dataclass
class DocumentResult:
    verdict: str
    important_chunks: int
    total_chunks: int
    chunks: list[ChunkResult]
    learned: int = 0  # 이번 분류로 학습 등급에 추가된 문장 수

    def to_dict(self) -> dict:
        return asdict(self)


class Classifier:
    def __init__(self, store: Store, thresholds: Thresholds | None = None,
                 judge: Judge | None = None, learn: bool | None = None, evidence_k: int = 3) -> None:
        self.store = store
        self.t = thresholds or Thresholds.for_embedder(store.embedder.name)
        self.judge = judge or SimilarityJudge(self.t.general, self.t.margin)
        self.is_llm = not isinstance(self.judge, SimilarityJudge)
        # 유사도 판정은 수치 차이에 둔해서, 그 결과를 굳히는 학습은 LLM 판정일 때만 기본으로 켠다.
        self.learn = self.is_llm if learn is None else learn
        self.evidence_k = evidence_k
        self._judge_failed = False

    def classify(self, text: str, max_chars: int = 200, source: str | None = None) -> DocumentResult:
        store = self.store
        pieces = chunk_text(text, max_chars=max_chars)
        vectors = store.embedder.encode(pieces) if pieces else []
        confidential = store.top_matches(vectors, "important", k=2)
        general = store.top_matches(vectors, "general", k=self.evidence_k, tier="original")

        general_hashes = store.sentence_hashes("general")
        exclusive = {h: doc for h, doc in store.sentence_hashes("important").items() if h not in general_hashes}

        results: list[ChunkResult] = []
        learned = 0
        for i, piece in enumerate(pieces):
            conf = [Passage(c["doc"], c["text"], s) for s, c in confidential[i]]
            gen = [Passage(c["doc"], c["text"], s) for s, c in general[i]]
            result = ChunkResult(
                index=i, text=piece, verdict="general", reason="no_match",
                sim_important=round(conf[0].sim, 4) if conf else 0.0,
                sim_general=round(gen[0].sim, 4) if gen else 0.0,
                matched_important_doc=conf[0].doc if conf else None,
            )
            results.append(result)

            sentences = split_sentences(piece)
            hashes = {h for h in map(sentence_hash, sentences) if h}
            whole = sentence_hash(piece)

            # 1. 일반 저장소와 해시 일치
            if whole in general_hashes or (hashes and hashes <= general_hashes.keys()):
                docs = [general_hashes[h] for h in ([whole] if whole in general_hashes else sorted(hashes))]
                if all(d.startswith(LEARNED_PREFIX) for d in docs):
                    info = store.learned.get(docs[0][len(LEARNED_PREFIX):], {})
                    result.reason = "learned"
                    result.matched_general_doc = info.get("evidence_doc")
                    result.evidence_text = info.get("evidence_text")
                else:
                    result.reason = "general_hash"
                    result.matched_general_doc = next(d for d in docs if not d.startswith(LEARNED_PREFIX))
                continue

            # 2. 기밀 후보인가
            exact = [s for s in sentences if sentence_hash(s) in exclusive]
            if not exact and whole in exclusive:  # 표의 행처럼 통째로 일치
                exact = [piece]
            if exact:
                candidate = "exact"
                result.exact_sentences = exact
                result.matched_important_doc = exclusive[sentence_hash(exact[0])]
            elif result.sim_important >= self.t.important:
                candidate = "semantic"
            else:
                continue  # 기밀과 무관

            # 3. 재검토: 왜 기밀이 아닌가
            try:
                judgement = self.judge.judge(piece, gen, conf)
                released, why = verify(piece, judgement, gen, need_quote=self.is_llm,
                                       min_sim=self.t.evidence)
            except JudgeError as exc:
                released, why = False, str(exc)
                if not self._judge_failed:
                    self._judge_failed = True
                    print(f"경고: {exc}. 재검토하지 못한 문장은 기밀로 남깁니다.", file=sys.stderr)
            result.review = why

            if released:
                result.reason = "released"
                result.matched_general_doc = judgement.evidence.doc
                result.evidence_text = judgement.evidence.text
                if self.learn:  # 4. 학습
                    store.learn(piece, vectors[i], judgement.evidence.doc, judgement.evidence.text,
                                source, self.judge.name)
                    general_hashes = store.sentence_hashes("general")
                    exclusive = {h: d for h, d in exclusive.items() if h not in general_hashes}
                    learned += 1
            else:
                result.verdict, result.reason = "important", candidate
                if not self._judge_failed:
                    store.note_pending(piece, source, result.matched_important_doc, why)

        hits = sum(r.verdict == "important" for r in results)
        return DocumentResult(
            verdict="important" if hits >= self.t.min_hits else "general",
            important_chunks=hits,
            total_chunks=len(results),
            chunks=results,
            learned=learned,
        )
