"""명령줄 도구.

문서   : register / remove / list / convert
분류   : classify
검토   : pending / approve / dismiss   (근거가 없어 기밀로 남은 문장)
학습   : learned / revoke              (자동으로 일반 처리된 문장)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from pathlib import Path

from .classifier import Classifier, Thresholds
from .embedders import ALIASES, make_embedder
from .extract import EXTRACTORS, SUPPORTED, ExtractError, to_markdown
from .judge import LLMJudge, SimilarityJudge
from .store import LEARNED_PREFIX, Store

LABEL_KO = {"important": "기밀", "general": "일반"}
REASON_KO = {
    "general_hash": "일반 문서에 있는 문장",
    "learned": "이전 재검토에서 일반으로 판정된 문장",
    "released": "재검토 결과 일반 문서에 같은 내용이 있음",
    "no_match": "기밀 문서와 무관",
    "exact": "기밀 문서에만 있는 문장과 일치",
    "semantic": "기밀 문서와 의미상 유사",
}

_failed = False


def _open_store(args: argparse.Namespace) -> Store:
    saved = Store.saved_embedder_name(args.store)
    if saved and args.embedder and ALIASES.get(args.embedder, args.embedder) != saved:
        sys.exit(f"저장소는 '{saved}' 임베딩으로 만들어졌습니다. --embedder를 빼거나 새 --store를 쓰세요.")
    return Store(args.store, make_embedder(saved or args.embedder or "bge-m3", device=args.device))


def _make_judge(args: argparse.Namespace, t: Thresholds):
    url = args.llm_url or os.environ.get("DOCCLASS_LLM_URL")
    model = args.llm_model or os.environ.get("DOCCLASS_LLM_MODEL")
    kind = args.judge
    if kind == "auto":
        kind = "llm" if url and model else "similarity"
    if kind == "similarity":
        return SimilarityJudge(t.general, t.margin)
    if not (url and model):
        sys.exit("LLM 재검토에는 --llm-url과 --llm-model(또는 DOCCLASS_LLM_URL, DOCCLASS_LLM_MODEL)이 필요합니다.")
    return LLMJudge(url, model, api_key=os.environ.get("DOCCLASS_LLM_API_KEY"))


def _read(path: Path, args: argparse.Namespace) -> str | None:
    """문서를 Markdown으로 읽는다. 실패하면 경고를 찍고 None."""
    global _failed
    try:
        text = to_markdown(path, extractor=args.extractor)
    except (ExtractError, OSError) as exc:
        print(f"{path}: 건너뜀 - {exc}", file=sys.stderr)
        _failed = True
        return None
    if not text.strip():
        print(f"{path}: 건너뜀 - 추출된 텍스트가 없습니다(스캔한 PDF라면 OCR이 필요합니다).", file=sys.stderr)
        _failed = True
        return None
    return text


def _short(text: str | None, width: int = 60) -> str:
    text = text or ""
    return text[:width] + ("…" if len(text) > width else "")


# ---- 문서 ---------------------------------------------------------------


def cmd_convert(args: argparse.Namespace) -> None:
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        text = _read(path, args)
        if text is None:
            continue
        if args.out:
            target = args.out / (path.name + ".md")
            target.write_text(text, encoding="utf-8")
            print(f"{path} -> {target}")
        else:
            if len(args.files) > 1:
                print(f"<!-- {path} -->")
            print(text)


def cmd_register(args: argparse.Namespace) -> None:
    store = _open_store(args)
    label = "important" if args.label == "confidential" else args.label
    for path in args.files:
        text = _read(path, args)
        if text is None:
            continue
        count = store.add(str(path), label, text, max_chars=args.chunk_chars)
        print(f"[{LABEL_KO[label]}] {path}: 문장 {count}개 등록")
        if store.last_revoked:
            print(f"  이 문서와 겹치는 학습 문장 {len(store.last_revoked)}개를 무효로 했습니다. 다음 분류 때 다시 재검토합니다.")
    store.save()


def cmd_remove(args: argparse.Namespace) -> None:
    store = _open_store(args)
    for path in args.files:
        print(f"{path}: {'삭제' if store.remove(str(path)) else '등록되어 있지 않음'}")
        if store.last_revoked:
            print(f"  이 문서를 근거로 풀렸던 학습 문장 {len(store.last_revoked)}개도 무효로 했습니다.")
    store.save()


def cmd_list(args: argparse.Namespace) -> None:
    store = _open_store(args)
    documents = store.documents()
    for doc, info in sorted(documents.items()):
        if info["tier"] == "original":
            print(f"[{LABEL_KO[info['label']]}] {doc}")
    open_items = sum(1 for p in store.pending.values() if p["status"] == "open")
    print(f"학습된 일반 문장 {len(store.learned)}개, 검토 대기 {open_items}개")


# ---- 분류 ---------------------------------------------------------------


def cmd_classify(args: argparse.Namespace) -> None:
    store = _open_store(args)
    t = Thresholds.for_embedder(store.embedder.name)
    for key in ("important", "general", "margin", "evidence", "min_hits"):
        value = getattr(args, f"t_{key}")
        if value is not None:
            setattr(t, key, value)
    judge = _make_judge(args, t)
    classifier = Classifier(store, t, judge=judge, learn=args.learn)
    if not args.json:
        print(f"재검토: {judge.name}, 학습: {'켬' if classifier.learn else '끔'}")

    reports = {}
    for path in args.files:
        text = _read(path, args)
        if text is None:
            continue
        result = classifier.classify(text, max_chars=args.chunk_chars, source=str(path))
        reports[str(path)] = result.to_dict()
        if args.json:
            continue
        print(f"{path}: {LABEL_KO[result.verdict]} 문서 "
              f"(기밀 문장 {result.important_chunks}/{result.total_chunks}"
              + (f", 새로 학습 {result.learned}" if result.learned else "") + ")")
        for c in result.chunks:
            if c.reason == "no_match" and not args.verbose:
                continue
            print(f"  #{c.index} [{LABEL_KO[c.verdict]}] {REASON_KO[c.reason]} "
                  f"(기밀 {c.sim_important:.2f} / 일반 {c.sim_general:.2f})")
            print(f"      \"{_short(c.text)}\"")
            if c.verdict == "important":
                print(f"      출처: {c.matched_important_doc}")
                if c.review:
                    print(f"      일반으로 볼 근거 없음: {c.review}")
            elif c.matched_general_doc:
                print(f"      근거: {c.matched_general_doc}"
                      + (f" \"{_short(c.evidence_text)}\"" if c.evidence_text else ""))
    store.save()  # 학습 문장과 검토 대기열
    if args.json:
        json.dump(reports, sys.stdout, ensure_ascii=False, indent=2)
        print()


# ---- 검토 대기열 --------------------------------------------------------


def cmd_pending(args: argparse.Namespace) -> None:
    store = _open_store(args)
    items = [(i, p) for i, p in store.pending.items() if p["status"] == "open"]
    items.sort(key=lambda x: -x[1]["count"])
    if not items:
        print("검토 대기 중인 문장이 없습니다.")
    for pending_id, p in items:
        print(f"{pending_id}  {p['count']}회  \"{_short(p['text'], 80)}\"")
        print(f"    기밀 출처: {p.get('confidential_doc')}  나온 문서: {', '.join(p['docs']) or '-'}")
        print(f"    사유: {p.get('why')}")


def cmd_approve(args: argparse.Namespace) -> None:
    store = _open_store(args)
    for pending_id in args.ids:
        ok = store.approve(pending_id, args.by)
        print(f"{pending_id}: {'일반으로 승인 (' + args.by + ')' if ok else '대기열에 없음'}")
    store.save()


def cmd_dismiss(args: argparse.Namespace) -> None:
    store = _open_store(args)
    for pending_id in args.ids:
        print(f"{pending_id}: {'기밀로 확정' if store.dismiss(pending_id) else '대기열에 없음'}")
    store.save()


# ---- 학습 문장 ----------------------------------------------------------


def cmd_learned(args: argparse.Namespace) -> None:
    store = _open_store(args)
    items = list(store.learned.items())
    if args.sample:
        items = random.sample(items, min(args.sample, len(items)))
    if not items:
        print("학습된 문장이 없습니다.")
    for learned_id, info in items:
        print(f"{learned_id}  {info['at']}  {info['judge']}")
        print(f"    문장: \"{_short(info['text'], 80)}\"")
        print(f"    근거: {info['evidence_doc']} \"{_short(info['evidence_text'], 80)}\"")
        print(f"    처음 나온 문서: {info['source_doc']}")


def cmd_revoke(args: argparse.Namespace) -> None:
    store = _open_store(args)
    for learned_id in args.ids:
        learned_id = learned_id.removeprefix(LEARNED_PREFIX)
        print(f"{learned_id}: {'학습 취소' if store.revoke(learned_id) else '학습 목록에 없음'}")
    store.save()


# ---- 진입점 -------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="docclass", description="기밀/일반 문서 분류기")
    parser.add_argument("--store", default="store", help="인덱스 저장 디렉터리 (기본: ./store)")
    parser.add_argument("--embedder", help="bge-m3(기본), hashing, 또는 sentence-transformers 모델 이름")
    parser.add_argument("--device", help="cuda, cuda:0, cpu 등 (기본: 자동)")
    parser.add_argument("--extractor", choices=EXTRACTORS, default="auto",
                        help="문서 추출기: auto(기본, Docling 우선), docling, builtin")
    parser.add_argument("--chunk-chars", type=int, default=200, help="문장 하나의 최대 글자 수")
    parser.add_argument("--judge", choices=["auto", "llm", "similarity"], default="auto",
                        help="기밀 후보 재검토 방식. auto는 LLM 설정이 있으면 llm, 없으면 similarity")
    parser.add_argument("--llm-url", help="OpenAI 호환 API 주소, 예: http://localhost:8000/v1 (또는 DOCCLASS_LLM_URL)")
    parser.add_argument("--llm-model", help="LLM 모델 이름 (또는 DOCCLASS_LLM_MODEL)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="문서 등록")
    p.add_argument("--label", required=True, choices=["confidential", "important", "general"],
                   help="confidential(기밀, important와 같음) 또는 general(일반)")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_register)

    p = sub.add_parser("convert", help="문서를 Markdown으로 변환해 확인 (" + " ".join(SUPPORTED) + ")")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("-o", "--out", type=Path, help="이 디렉터리에 <파일명>.md로 저장 (없으면 화면 출력)")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("remove", help="등록 해제")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("list", help="등록된 문서 목록")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("classify", help="문서 분류")
    p.add_argument("files", nargs="+", type=Path)
    p.add_argument("--json", action="store_true", help="전체 판정 근거를 JSON으로 출력")
    p.add_argument("-v", "--verbose", action="store_true", help="무관한 문장도 표시")
    p.add_argument("--learn", action=argparse.BooleanOptionalAction, default=None,
                   help="재검토로 풀린 문장을 학습 등급에 추가 (기본: LLM 재검토일 때만 켬)")
    p.add_argument("--t-important", type=float, help="기밀 후보가 되는 유사도")
    p.add_argument("--t-general", type=float, help="(similarity 재검토) 일반 유사도 기준")
    p.add_argument("--t-margin", type=float, help="(similarity 재검토) 허용 차이")
    p.add_argument("--t-evidence", type=float, help="재검토 근거 문장의 최소 유사도")
    p.add_argument("--t-min-hits", type=int, help="문서를 기밀로 판정하는 기밀 문장 수")
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("pending", help="근거가 없어 기밀로 남은 문장 목록 (자주 나온 순)")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("approve", help="대기 문장을 일반으로 승인 (원본 등급에 추가)")
    p.add_argument("ids", nargs="+")
    p.add_argument("--by", required=True, help="승인자 이름 (감사 기록)")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("dismiss", help="대기 문장을 기밀로 확정하고 목록에서 내림")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_dismiss)

    p = sub.add_parser("learned", help="자동으로 일반 처리된 학습 문장과 그 근거")
    p.add_argument("--sample", type=int, help="무작위로 N개만 (표본 점검용)")
    p.set_defaults(func=cmd_learned)

    p = sub.add_parser("revoke", help="학습 문장 취소")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_revoke)

    global _failed
    _failed = False
    args = parser.parse_args(argv)
    args.func(args)
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
