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


# ---- 표 ----------------------------------------------------------------

_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEPARATOR = re.compile(r"^\s*\|(\s*:?-{3,}:?\s*\|)+\s*$")
_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_MD_MARKER = re.compile(r"^\s*(#{1,6}|[-*+])\s+")

# 표의 행은 문장보다 짧아도 의미가 있으므로 기준을 낮춘다.
MIN_ROW_CHARS = 8


def _cells(line: str) -> list[str]:
    return [c.strip().replace("\\|", "|") for c in _CELL_SPLIT.split(line.strip())[1:-1]]


def _pack(parts: list[str], max_chars: int) -> list[str]:
    """'열: 값' 조각을 max_chars까지 이어 붙인다. 넘치면 첫 조각(행의 키)을 반복해 나눈다."""
    units: list[str] = []
    current: list[str] = []
    for part in parts:
        while len(part) > max_chars:
            units.append(part[:max_chars])
            part = part[max_chars:]
        if current and len("; ".join(current + [part])) > max_chars:
            units.append("; ".join(current))
            key = parts[0]
            current = [key] if len(key) + len(part) + 2 <= max_chars else []
        current.append(part)
    if current:
        units.append("; ".join(current))
    return units


def table_units(rows: list[list[str]], max_chars: int = 200) -> list[str]:
    """Markdown 표의 행들을 임베딩 단위로 바꾼다.

    - 일반 표: 행마다 "머리글1: 값1; 머리글2: 값2". 행만 따로 떼어도 뜻이 통한다.
    - 2열 표: 항목-값 표로 보고 첫 행을 포함해 행마다 "항목: 값".
    """
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    units: list[str] = []
    if width <= 2 or len(rows) == 1:
        joiner = ": " if width == 2 else "; "
        for row in rows:
            units.extend(_pack([joiner.join(c for c in row if c)], max_chars))
    else:
        header = rows[0]
        for row in rows[1:]:
            parts = [f"{h}: {v}" if h and h != v else v for h, v in zip(header, row) if v]
            units.extend(_pack(parts, max_chars))
    return [u for u in units if len(u) >= MIN_ROW_CHARS]


# ---- 청크 --------------------------------------------------------------


def chunk_text(text: str, max_chars: int = 200, min_chars: int = MIN_FINGERPRINT_CHARS) -> list[str]:
    """문장 단위 청크를 만든다. Markdown 표는 행 단위로 만든다(table_units).

    문단 전체를 한 청크로 묶으면 중요 문장 하나가 주변 문장에 희석되어 놓치기 쉽다.
    그래서 문장을 기본 단위로 하되, min_chars보다 짧은 문장은 같은 줄의 이웃 문장에
    붙이고, 붙일 곳이 없는 짧은 줄(제목 등)은 버린다. max_chars보다 긴 문장은 자른다.
    """
    chunks: list[str] = []
    table: list[list[str]] = []
    for line in text.splitlines() + [""]:
        if _TABLE_ROW.match(line):
            if not _TABLE_SEPARATOR.match(line):
                table.append(_cells(line))
            continue
        if table:
            chunks.extend(table_units(table, max_chars))
            table = []
        line = _MD_MARKER.sub("", line)
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


def chunk_hashes(chunk: str) -> set[str]:
    """청크의 정확 일치용 해시: 청크 전체와 그 안의 문장들."""
    hashes = {sentence_hash(s) for s in split_sentences(chunk)} | {sentence_hash(chunk)}
    hashes.discard(None)
    return hashes
