"""문서를 문장과 청크로 나누고, 정확 일치용 문장 해시를 만든다."""

from __future__ import annotations

import hashlib
import re
import unicodedata

_SENTENCE_END = re.compile(r"(?<=[.!?。？！])\s+|\n+")
_WHITESPACE = re.compile(r"\s+")

# 이보다 짧은 문장은 "감사합니다." 같은 흔한 문구일 가능성이 높아 정확 일치 대상에서 뺀다.
MIN_FINGERPRINT_CHARS = 20


def normalize(text: str) -> str:
    """공백·전각문자·대소문자 차이를 없앤 비교용 문자열."""
    text = unicodedata.normalize("NFKC", text)
    return _WHITESPACE.sub(" ", text).strip().lower()


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_END.split(text) if s and s.strip()]


def sentence_hash(sentence: str) -> str | None:
    """정확 일치 판정용 해시. 너무 짧은 문장은 None."""
    norm = normalize(sentence)
    if len(norm) < MIN_FINGERPRINT_CHARS:
        return None
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def chunk_text(text: str, max_chars: int = 200, min_chars: int = MIN_FINGERPRINT_CHARS) -> list[str]:
    """문장 단위 청크를 만든다.

    문단 전체를 한 청크로 묶으면 중요 문장 하나가 주변 문장에 희석되어 놓치기 쉽다.
    그래서 문장을 기본 단위로 하되, min_chars보다 짧은 문장은 같은 줄의 이웃 문장에
    붙이고, 붙일 곳이 없는 짧은 줄(제목 등)은 버린다. max_chars보다 긴 문장은 자른다.
    """
    chunks: list[str] = []
    for line in text.splitlines():
        units: list[str] = []
        carry = ""
        for sentence in split_sentences(line):
            sentence = f"{carry} {sentence}".strip()
            carry = ""
            if len(sentence) < min_chars:
                carry = sentence
                continue
            while len(sentence) > max_chars:
                units.append(sentence[:max_chars])
                sentence = sentence[max_chars:]
            if len(sentence) >= min_chars or not units:
                units.append(sentence)
            else:
                units[-1] = f"{units[-1]} {sentence}"
        if carry and units:
            units[-1] = f"{units[-1]} {carry}"
        chunks.extend(u for u in units if len(u) >= min_chars)
    return chunks
