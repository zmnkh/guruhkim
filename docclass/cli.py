"""명령줄 도구: register / classify / list / remove."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .classifier import Classifier, Thresholds
from .embedders import make_embedder
from .extract import SUPPORTED, ExtractError, to_markdown
from .store import Store

LABEL_KO = {"important": "중요", "general": "일반"}
REASON_KO = {
    "exact": "중요 문서 전용 문장과 정확히 일치",
    "semantic": "중요 문서와 의미상 유사",
    "neutralized": "중요 문서와 비슷하지만 일반 문서에도 있는 내용",
    "no_match": "중요 문서와 무관",
}


def _open_store(args: argparse.Namespace) -> Store:
    saved = Store.saved_embedder_name(args.store)
    name = args.embedder or saved or "bge-m3"
    if saved and args.embedder and make_alias(args.embedder) != saved:
        sys.exit(f"저장소는 '{saved}' 임베딩으로 만들어졌습니다. --embedder를 빼거나 새 --store를 쓰세요.")
    return Store(args.store, make_embedder(saved or name, device=args.device))


def make_alias(name: str) -> str:
    from .embedders import ALIASES

    return ALIASES.get(name, name)


_failed = False


def _read(path: Path) -> str | None:
    """문서를 Markdown으로 읽는다. 실패하면 경고를 찍고 None."""
    global _failed
    try:
        text = to_markdown(path)
    except (ExtractError, OSError) as exc:
        print(f"{path}: 건너뜀 - {exc}", file=sys.stderr)
        _failed = True
        return None
    if not text.strip():
        print(f"{path}: 건너뜀 - 추출된 텍스트가 없습니다(스캔한 PDF라면 OCR이 필요합니다).", file=sys.stderr)
        _failed = True
        return None
    return text


def cmd_convert(args: argparse.Namespace) -> None:
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
    for path in args.files:
        text = _read(path)
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
    for path in args.files:
        text = _read(path)
        if text is None:
            continue
        count = store.add(str(path), args.label, text, max_chars=args.chunk_chars)
        print(f"[{LABEL_KO[args.label]}] {path}: 청크 {count}개 등록")
    store.save()


def cmd_remove(args: argparse.Namespace) -> None:
    store = _open_store(args)
    for path in args.files:
        print(f"{path}: {'삭제' if store.remove(str(path)) else '등록되어 있지 않음'}")
    store.save()


def cmd_list(args: argparse.Namespace) -> None:
    store = _open_store(args)
    for doc, label in sorted(store.documents().items()):
        print(f"[{LABEL_KO[label]}] {doc}")


def cmd_classify(args: argparse.Namespace) -> None:
    store = _open_store(args)
    t = Thresholds.for_embedder(store.embedder.name)
    for key in ("important", "general", "margin", "min_hits"):
        value = getattr(args, f"t_{key}")
        if value is not None:
            setattr(t, key, value)
    classifier = Classifier(store, t)

    reports = {}
    for path in args.files:
        text = _read(path)
        if text is None:
            continue
        result = classifier.classify(text, max_chars=args.chunk_chars)
        reports[str(path)] = result.to_dict()
        if args.json:
            continue
        print(f"{path}: {LABEL_KO[result.verdict]} 문서 "
              f"(중요 청크 {result.important_chunks}/{result.total_chunks})")
        for c in result.chunks:
            if c.reason == "no_match" and not args.verbose:
                continue
            preview = c.text[:60] + ("…" if len(c.text) > 60 else "")
            print(f"  #{c.index} [{LABEL_KO[c.verdict]}] {REASON_KO[c.reason]} "
                  f"(중요 {c.sim_important:.2f} / 일반 {c.sim_general:.2f})")
            print(f"      \"{preview}\"")
            if c.verdict == "important" and c.matched_important_doc:
                print(f"      출처: {c.matched_important_doc}")
    if args.json:
        json.dump(reports, sys.stdout, ensure_ascii=False, indent=2)
        print()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="docclass", description="중요/일반 문서 분류기")
    parser.add_argument("--store", default="store", help="인덱스 저장 디렉터리 (기본: ./store)")
    parser.add_argument("--embedder", help="bge-m3(기본), hashing, 또는 sentence-transformers 모델 이름")
    parser.add_argument("--device", help="cuda, cuda:0, cpu 등 (기본: 자동)")
    parser.add_argument("--chunk-chars", type=int, default=200, help="문장 하나의 최대 글자 수")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register", help="문서 등록")
    p.add_argument("--label", required=True, choices=["important", "general"])
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
    p.add_argument("-v", "--verbose", action="store_true", help="무관한 청크도 표시")
    p.add_argument("--t-important", type=float, help="중요 유사도 임계값")
    p.add_argument("--t-general", type=float, help="일반 유사도 임계값")
    p.add_argument("--t-margin", type=float, help="상쇄 허용 차이")
    p.add_argument("--t-min-hits", type=int, help="중요 판정에 필요한 중요 청크 수")
    p.set_defaults(func=cmd_classify)

    global _failed
    _failed = False
    args = parser.parse_args(argv)
    args.func(args)
    if _failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
