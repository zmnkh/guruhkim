from pathlib import Path

import pytest

from docclass import Classifier, HashingEmbedder, Store
from docclass.chunking import chunk_text, sentence_hash
from docclass.cli import main

EXAMPLES = Path(__file__).parent.parent / "examples"


def read(*parts: str) -> str:
    return (EXAMPLES.joinpath(*parts)).read_text(encoding="utf-8")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store", HashingEmbedder())
    s.add("전략", "important", read("important", "2027_신제품_전략.txt"))
    s.add("소개", "general", read("general", "회사소개_및_시장자료.txt"))
    return s


def test_only_non_important_data_is_general(store):
    result = Classifier(store).classify(read("incoming", "시장동향_메모.txt"))
    assert result.verdict == "general"
    assert any(c.reason == "neutralized" for c in result.chunks)


def test_important_data_is_important(store):
    result = Classifier(store).classify(read("incoming", "협력사_전달_메일.txt"))
    assert result.verdict == "important"
    assert any(c.reason == "exact" for c in result.chunks)


def test_unrelated_is_general(store):
    result = Classifier(store).classify(read("incoming", "점심_공지.txt"))
    assert result.verdict == "general"
    assert all(c.reason == "no_match" for c in result.chunks)


def test_important_document_itself_is_important(store):
    assert Classifier(store).classify(read("important", "2027_신제품_전략.txt")).verdict == "important"


def test_general_document_itself_is_general(store):
    assert Classifier(store).classify(read("general", "회사소개_및_시장자료.txt")).verdict == "general"


def test_reworded_important_data_is_caught_semantically(store):
    text = "차세대 허브 '오로라' 출시 목표 원가는 대당 4만 2천 원, 목표 판매가는 12만 9천 원으로 정했습니다."
    result = Classifier(store).classify(text)
    assert result.verdict == "important"
    assert result.chunks[0].reason == "semantic"


def test_without_general_registration_shared_text_is_important(tmp_path):
    s = Store(tmp_path / "store", HashingEmbedder())
    s.add("전략", "important", read("important", "2027_신제품_전략.txt"))
    assert Classifier(s).classify(read("incoming", "시장동향_메모.txt")).verdict == "important"


def test_registration_order_does_not_matter(tmp_path):
    s = Store(tmp_path / "store", HashingEmbedder())
    s.add("소개", "general", read("general", "회사소개_및_시장자료.txt"))
    s.add("전략", "important", read("important", "2027_신제품_전략.txt"))
    c = Classifier(s)
    assert c.classify(read("incoming", "시장동향_메모.txt")).verdict == "general"
    assert c.classify(read("incoming", "협력사_전달_메일.txt")).verdict == "important"


def test_save_load_and_replace(store, tmp_path):
    store.save()
    loaded = Store(tmp_path / "store", HashingEmbedder())
    assert loaded.documents() == {"전략": "important", "소개": "general"}
    assert loaded.vectors.shape == store.vectors.shape
    before = len(loaded.chunks)
    loaded.add("소개", "general", read("general", "회사소개_및_시장자료.txt"))
    assert len(loaded.chunks) == before
    assert loaded.remove("소개") and not loaded.remove("소개")
    assert Classifier(loaded).classify(read("incoming", "시장동향_메모.txt")).verdict == "important"


def test_chunking_and_hash():
    text = "가나다라마바사아자차카타파하" * 40
    chunks = chunk_text(text, max_chars=100)
    assert len(chunks) > 1 and all(len(c) <= 100 for c in chunks)
    assert chunk_text("제목\n\n네. 이 문장은 스무 글자를 넘는 충분히 긴 문장입니다. 끝.") == [
        "네. 이 문장은 스무 글자를 넘는 충분히 긴 문장입니다. 끝."
    ]
    assert sentence_hash("짧은 문장.") is None
    assert sentence_hash("공백이   다르고 ABC 대소문자가 다른 문장입니다.") == sentence_hash(
        "공백이 다르고 abc 대소문자가 다른 문장입니다."
    )


def test_cli(tmp_path, capsys):
    base = ["--store", str(tmp_path / "s"), "--embedder", "hashing"]
    main(base + ["register", "--label", "important", str(EXAMPLES / "important" / "2027_신제품_전략.txt")])
    main(base + ["register", "--label", "general", str(EXAMPLES / "general" / "회사소개_및_시장자료.txt")])
    capsys.readouterr()
    main(["--store", str(tmp_path / "s"), "classify", *map(str, sorted((EXAMPLES / "incoming").glob("*.txt")))])
    out = capsys.readouterr().out
    assert "시장동향_메모.txt: 일반 문서" in out
    assert "협력사_전달_메일.txt: 중요 문서" in out
    assert "점심_공지.txt: 일반 문서" in out
