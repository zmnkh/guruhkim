"""기밀 후보 재검토, 학습 등급, 검토 대기열."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from docclass import Classifier, HashingEmbedder, Store, Thresholds
from docclass.cli import main
from docclass.judge import Judgement, JudgeError, LLMJudge, Passage, parse_reply, verify
from docclass.store import text_id

S_MARKET = "국내 스마트홈 시장 규모는 2025년 기준 약 8조 원으로 추정됩니다."
S_COST = "차세대 허브 제품 오로라의 목표 원가는 대당 4만 2천 원입니다."
S_NOTICE = "본 문서의 무단 복제와 배포를 엄격히 금지합니다."
G_MARKET = "국내 스마트홈 시장은 2025년 기준 약 8조 원 규모라고 공개 통계에 나와 있습니다."
G_COST = "경쟁사 허브 제품의 원가는 대당 4만 5천 원 수준으로 알려져 있습니다."

CONFIDENTIAL = "\n\n".join([S_MARKET, S_COST, S_NOTICE])
GENERAL = "\n\n".join([G_MARKET, G_COST])


class EagerJudge:
    """무엇이든 "기밀 아님"이라며 가장 비슷한 일반 문장을 근거로 대는 LLM 흉내."""

    name = "llm:eager"

    def __init__(self):
        self.calls = []

    def judge(self, sentence, general, confidential):
        self.calls.append(sentence)
        if not general:
            return Judgement(False, reason="일반 문서 없음")
        return Judgement(True, general[0], general[0].text[:15], "일반 문서에 같은 내용이 있음")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "store", HashingEmbedder())
    s.add("기밀.txt", "important", CONFIDENTIAL)
    s.add("일반.txt", "general", GENERAL)
    return s


def by_text(result):
    return {c.text: c for c in result.chunks}


# ---- 재검토 -------------------------------------------------------------


def test_review_releases_only_what_general_evidence_supports(store):
    judge = EagerJudge()
    result = Classifier(store, judge=judge).classify(CONFIDENTIAL, source="신규.txt")
    chunks = by_text(result)

    market = chunks[S_MARKET]
    assert (market.verdict, market.reason) == ("general", "released")
    assert (market.matched_general_doc, market.evidence_text) == ("일반.txt", G_MARKET)

    # 판정기는 풀어 주려 했지만 검증에서 막힌다: 수치가 다르다 / 근거가 엉뚱하다
    assert (chunks[S_COST].verdict, chunks[S_COST].reason) == ("important", "exact")
    assert "수치" in chunks[S_COST].review and "2" in chunks[S_COST].review
    assert chunks[S_NOTICE].verdict == "important"
    assert "비슷하지 않음" in chunks[S_NOTICE].review

    assert result.verdict == "important" and result.important_chunks == 2
    assert len(judge.calls) == 3


def test_document_with_only_releasable_content_becomes_general(store):
    result = Classifier(store, judge=EagerJudge()).classify(S_MARKET)
    assert result.verdict == "general" and result.learned == 1


def test_paraphrase_of_confidential_is_reviewed_even_without_hash_match(store):
    paraphrase = "국내 스마트홈 시장 규모는 2025년 기준으로 약 8조 원 정도로 추정됩니다."
    judge = EagerJudge()
    chunk = Classifier(store, judge=judge).classify(paraphrase).chunks[0]
    assert judge.calls == [paraphrase]  # 해시는 안 맞지만 유사도로 후보가 된다
    assert chunk.reason == "released"

    leak = "차세대 허브 오로라 제품의 목표 원가는 대당 4만 2천 원으로 정했습니다."
    chunk = Classifier(store, judge=judge).classify(leak).chunks[0]
    assert (chunk.verdict, chunk.reason) == ("important", "semantic")


def test_llm_must_quote_the_evidence(store):
    class MisquotingJudge(EagerJudge):
        def judge(self, sentence, general, confidential):
            return Judgement(True, general[0], "근거 문서에 없는 지어낸 인용문입니다", "그럴듯한 이유")

    chunk = by_text(Classifier(store, judge=MisquotingJudge()).classify(S_MARKET))[S_MARKET]
    assert chunk.verdict == "important" and "인용문" in chunk.review


def test_evidence_must_come_from_the_store():
    general = [Passage("일반.txt", G_MARKET, 0.9)]
    forged = Judgement(True, Passage("일반.txt", S_COST, 0.99), S_COST[:15], "")
    assert verify(S_COST, forged, general, need_quote=True) == (False, "제시한 근거가 등록된 일반 문서에 없음")


def test_judge_failure_keeps_sentence_confidential(store, capsys):
    class DownJudge(EagerJudge):
        def judge(self, sentence, general, confidential):
            raise JudgeError("LLM 호출 실패 (http://x): 연결 거부")

    result = Classifier(store, judge=DownJudge()).classify(S_MARKET)
    assert result.verdict == "important"
    assert "LLM 호출 실패" in capsys.readouterr().err
    assert not store.learned and not store.pending  # 장애는 판단이 아니므로 기록하지 않는다


# ---- 학습 등급 ----------------------------------------------------------


def test_learned_sentence_passes_by_hash_next_time(store):
    judge = EagerJudge()
    classifier = Classifier(store, judge=judge)
    classifier.classify(S_MARKET, source="첫문서.txt")
    info = store.learned[text_id(S_MARKET)]
    assert (info["evidence_doc"], info["evidence_text"], info["source_doc"], info["judge"]) == (
        "일반.txt", G_MARKET, "첫문서.txt", "llm:eager")

    chunk = classifier.classify(S_MARKET).chunks[0]
    assert (chunk.verdict, chunk.reason) == ("general", "learned")
    assert (chunk.matched_general_doc, chunk.evidence_text) == ("일반.txt", G_MARKET)
    assert len(judge.calls) == 1  # 두 번째는 재검토 없이 통과


def test_learned_sentences_are_never_used_as_evidence(store):
    classifier = Classifier(store, judge=EagerJudge())
    classifier.classify(S_MARKET)
    assert text_id(S_MARKET) in store.learned

    seen = []

    class Spy(EagerJudge):
        def judge(self, sentence, general, confidential):
            seen.extend(p.text for p in general)
            return super().judge(sentence, general, confidential)

    Classifier(store, judge=Spy()).classify("국내 스마트홈 시장 규모는 2025년 기준으로 약 8조 원 정도로 추정됩니다.")
    assert seen and S_MARKET not in seen and set(seen) <= {G_MARKET, G_COST}


def test_learning_is_off_by_default_for_similarity_judge(store):
    store.add("일반2.txt", "general", "국내 스마트홈 시장 규모는 2025년 기준 약 8조 원으로 추정된다고 합니다.")
    loose = Thresholds(important=0.6, general=0.6, margin=0.3, evidence=0.35)
    classifier = Classifier(store, loose)
    result = classifier.classify(S_MARKET)
    assert classifier.learn is False
    assert result.chunks[0].reason == "released" and not store.learned
    assert Classifier(store, loose, learn=True).classify(S_MARKET).learned == 1
    # 기본 기준에서는 유사도만으로 정확 일치한 기밀 문장을 풀어 주지 않는다
    store.revoke(text_id(S_MARKET))
    assert Classifier(store).classify(S_MARKET).verdict == "important"


def test_removing_evidence_document_revokes_what_it_released(store):
    classifier = Classifier(store, judge=EagerJudge())
    classifier.classify(S_MARKET)
    assert store.remove("일반.txt") and store.last_revoked == [text_id(S_MARKET)]
    assert not store.learned
    assert classifier.classify(S_MARKET).verdict == "important"


def test_registering_confidential_document_revokes_overlapping_learned(store):
    classifier = Classifier(store, judge=EagerJudge())
    classifier.classify(S_MARKET)
    store.add("새기밀.txt", "important", "신규 사업 검토 자료입니다. " + S_MARKET)
    assert store.last_revoked == [text_id(S_MARKET)] and not store.learned
    # 무효가 된 문장은 다시 재검토를 받는다
    judge = EagerJudge()
    assert Classifier(store, judge=judge).classify(S_MARKET).chunks[0].reason == "released"
    assert judge.calls == [S_MARKET]


def test_revoke_and_persistence(store, tmp_path):
    Classifier(store, judge=EagerJudge()).classify(CONFIDENTIAL, source="신규.txt")
    store.save()
    loaded = Store(tmp_path / "store", HashingEmbedder())
    assert loaded.learned == store.learned and loaded.pending == store.pending
    assert Classifier(loaded, judge=EagerJudge()).classify(S_MARKET).chunks[0].reason == "learned"
    assert loaded.revoke(text_id(S_MARKET)) and not loaded.revoke(text_id(S_MARKET))
    assert len(loaded.chunks) == len(loaded.vectors) == 5


# ---- 검토 대기열 --------------------------------------------------------


def test_unsupported_sentences_queue_for_human_review(store):
    classifier = Classifier(store, judge=EagerJudge())
    classifier.classify(CONFIDENTIAL, source="a.txt")
    classifier.classify(S_NOTICE, source="b.txt")
    notice = store.pending[text_id(S_NOTICE)]
    assert (notice["count"], notice["docs"], notice["confidential_doc"]) == (2, ["a.txt", "b.txt"], "기밀.txt")
    assert text_id(S_COST) in store.pending and text_id(S_MARKET) not in store.pending

    # 사람이 승인하면 원본 등급 일반 문장이 된다
    assert store.approve(text_id(S_NOTICE), by="보안담당")
    chunk = classifier.classify(S_NOTICE).chunks[0]
    assert (chunk.verdict, chunk.reason) == ("general", "general_hash")
    approved = store.sentences["approved:" + text_id(S_NOTICE)]
    assert approved["tier"] == "original" and approved["meta"]["approved_by"] == "보안담당"

    # 기밀로 확정한 문장은 계속 기밀이고 목록에서 내려간다
    assert store.dismiss(text_id(S_COST))
    assert classifier.classify(S_COST).verdict == "important"
    assert store.pending[text_id(S_COST)]["status"] == "dismissed"


# ---- LLM 판정기 ---------------------------------------------------------


def test_parse_reply():
    general = [Passage("a", G_MARKET, 0.7), Passage("b", G_COST, 0.5)]
    ok = parse_reply('<think>고민</think>```json\n{"evidence": 2, "quote": "원가는 대당", "reason": "같음"}\n```', general)
    assert ok.release and ok.evidence is general[1] and ok.quote == "원가는 대당"
    for reply in ('{"evidence": null, "quote": "", "reason": "없음"}', '{"evidence": 3}', '{"evidence": true}',
                  '{"evidence": "1"}', "근거가 없습니다", "[1]", ""):
        assert not parse_reply(reply, general).release, reply


@pytest.fixture
def llm_server():
    """OpenAI 호환 API 흉내. 요청을 기록하고 정해 둔 답을 돌려준다."""
    state = {"requests": [], "reply": "", "status": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
            payload = json.dumps({"choices": [{"message": {"content": state["reply"]}}]}).encode()
            self.send_response(state["status"])
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/v1"
    yield state
    server.shutdown()


def test_llm_judge_http(llm_server):
    general = [Passage("일반.txt", G_MARKET, 0.63)]
    confidential = [Passage("기밀.txt", S_MARKET, 1.0)]
    llm_server["reply"] = json.dumps({"evidence": 1, "quote": "2025년 기준 약 8조 원", "reason": "같은 통계"})
    judge = LLMJudge(llm_server["url"], "test-model", api_key="k")
    judgement = judge.judge(S_MARKET, general, confidential)
    assert verify(S_MARKET, judgement, general, need_quote=True, min_sim=0.35) == (True, "같은 통계")

    request = llm_server["requests"][0]
    assert request["path"] == "/v1/chat/completions" and request["auth"] == "Bearer k"
    assert request["body"]["model"] == "test-model" and request["body"]["temperature"] == 0
    user = request["body"]["messages"][1]["content"]
    assert S_MARKET in user and f"1. {G_MARKET}" in user and "왜 기밀이 아닌지" in user

    llm_server["status"] = 500
    with pytest.raises(JudgeError):
        judge.judge(S_MARKET, general, confidential)
    with pytest.raises(JudgeError):
        LLMJudge("http://127.0.0.1:1/v1", "m", timeout=2).judge(S_MARKET, general, confidential)


def test_cli_review_workflow(tmp_path, capsys, llm_server, monkeypatch):
    for name, text in [("기밀.txt", CONFIDENTIAL), ("일반.txt", GENERAL), ("신규.txt", CONFIDENTIAL)]:
        (tmp_path / name).write_text(text, encoding="utf-8")
    base = ["--store", str(tmp_path / "s")]
    main(base + ["--embedder", "hashing", "register", "--label", "confidential", str(tmp_path / "기밀.txt")])
    main(base + ["register", "--label", "general", str(tmp_path / "일반.txt")])

    # LLM은 매번 1번 발췌를 근거로 댄다. 시장 문장만 검증을 통과한다.
    llm_server["reply"] = json.dumps({"evidence": 1, "quote": "약 8조 원", "reason": "공개 통계와 같음"})
    monkeypatch.setenv("DOCCLASS_LLM_URL", llm_server["url"])
    monkeypatch.setenv("DOCCLASS_LLM_MODEL", "test-model")
    capsys.readouterr()
    main(base + ["classify", str(tmp_path / "신규.txt")])
    out = capsys.readouterr().out
    assert "재검토: llm:test-model, 학습: 켬" in out
    assert "신규.txt: 기밀 문서 (기밀 문장 2/3, 새로 학습 1)" in out
    assert "재검토 결과 일반 문서에 같은 내용이 있음" in out and "일반으로 볼 근거 없음" in out
    assert len(llm_server["requests"]) == 3

    main(base + ["classify", str(tmp_path / "신규.txt")])
    assert "이전 재검토에서 일반으로 판정된 문장" in capsys.readouterr().out
    assert len(llm_server["requests"]) == 5  # 학습된 문장은 다시 묻지 않는다

    main(base + ["learned"])
    out = capsys.readouterr().out
    assert text_id(S_MARKET) in out and "llm:test-model" in out and "일반.txt" in out

    main(base + ["pending"])
    out = capsys.readouterr().out
    assert f"{text_id(S_NOTICE)}  2회" in out and text_id(S_COST) in out

    main(base + ["approve", text_id(S_NOTICE), "--by", "보안담당"])
    main(base + ["dismiss", text_id(S_COST)])
    main(base + ["revoke", text_id(S_MARKET)])
    main(base + ["pending"])
    assert "검토 대기 중인 문장이 없습니다." in capsys.readouterr().out
    main(base + ["list"])
    assert "학습된 일반 문장 0개, 검토 대기 0개" in capsys.readouterr().out

    monkeypatch.delenv("DOCCLASS_LLM_URL")
    with pytest.raises(SystemExit):
        main(base + ["--judge", "llm", "classify", str(tmp_path / "신규.txt")])
